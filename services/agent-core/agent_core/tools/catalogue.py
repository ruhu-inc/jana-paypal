"""The model-facing tool layer: role grants and manifests, visibility, profiles and description rules (TOL-06,
TOL-07, TOL-16, TOL-17, PRV-15).

A model sees a tool only when (a) its role's register grant covers it, (b) its role manifest lists it (a manifest
narrows a grant, never widens it), (c) the flow node allows it, and (d) it is available now (TOL-06). `ui` tools and
the hidden `invalid` tool are never listed to any model. The small profile offers only small-model-friendly tools,
with their small descriptions and flat schemas, at most `tool_small_max_tools` per request."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from agent_core.config import Settings
from agent_core.tools.policy import denial, granted
from agent_core.tools.registry import EARLY_TOOLS, Registry
from agent_core.tools.spec import InitCtx, SessionCtx, ToolSpec

NEVER_LISTED = frozenset({"invalid"})
# Tools some build carries ahead of their register stage (registry.EARLY_TOOLS): offered to no role before that stage.
EARLY = frozenset().union(*EARLY_TOOLS.values())
# LIST TOL-07a lead words, in order; the eighth section is required only for tools whose UI adds something.
SECTIONS = (
    ("what", re.compile(r"^[A-Z][^.]*\.")),
    ("use_when", re.compile(r"\bUse when\b[^.;]*;\s*not for\b", re.I)),
    ("parameters", re.compile(r"\bParameters?:")),
    ("returns", re.compile(r"\bReturns\b")),
    ("limits", re.compile(r"\bLimits?:|\bNo deadline\b")),
    ("recovery", re.compile(r"Never repeat an unchanged failing call")),
    ("cost", re.compile(r"Batch independent calls|never spend a round-trip", re.I)),
)
UI_ADDS = re.compile(r"\bThe UI adds\b")


def description_errors(text: str, limit: int, interactive_ui: bool = False) -> list[str]:
    """TOL-07 template lint: the sections by their lead words, in order, within the character budget."""
    errors = []
    if len(text) > limit:
        errors.append(f"{len(text)} characters (at most {limit})")
    position = 0
    for name, pattern in SECTIONS:
        if name == "cost" and interactive_ui:
            continue  # an interactive card is never batched
        match = pattern.search(text, position if name != "what" else 0)
        if not match:
            errors.append(f"section '{name}' missing or out of order")
            continue
        position = match.end()
    if interactive_ui and not UI_ADDS.search(text):
        errors.append("section 'ui_adds' missing")
    return errors


def flat_schema_errors(schema: dict[str, Any], settings: Settings) -> list[str]:
    """TOL-07 small profile: no anyOf/oneOf, no arrays of objects, no nested objects with their own properties,
    at most N properties and string enums of at most M values."""
    errors = []
    properties = schema.get("properties", {})
    if len(properties) > settings.tool_small_max_properties:
        errors.append(f"{len(properties)} properties (at most {settings.tool_small_max_properties})")

    def walk(node: Any, path: str, depth: int) -> None:
        if isinstance(node, dict):
            for key in ("anyOf", "oneOf", "allOf"):
                if key in node:
                    errors.append(f"{path}: uses {key}")
            if "enum" in node and len(node["enum"]) > settings.tool_small_max_enum:
                errors.append(f"{path}: enum of {len(node['enum'])} values")
            if node.get("type") == "array" and isinstance(node.get("items"), dict):
                if node["items"].get("type") == "object" or "properties" in node["items"]:
                    errors.append(f"{path}: array of objects")
            if depth > 0 and "properties" in node:
                errors.append(f"{path}: nested object")
            for key, value in node.get("properties", {}).items():
                walk(value, f"{path}.{key}", depth + 1)
            if isinstance(node.get("items"), dict):
                walk(node["items"], f"{path}[]", depth + 1)
            extra = node.get("additionalProperties")
            if isinstance(extra, dict):
                walk(extra, f"{path}{{}}", depth + 1)

    walk(schema, "params", 0)
    return errors


# --- grants and manifests (TOL-16, PRM-05, PRM-06, SEC-04) ---------------------------------------------------------


def manifest_grant_errors(
    role: str,
    manifest_tools: Iterable[str],
    manifest_toolsets: Iterable[str],
    toolsets: dict[str, Any],
    grants: dict[str, list[str]],
) -> list[str]:
    """A manifest may narrow its role's grant, never widen it; each tool must be in one of its listed toolsets."""
    by_tool = {t["name"]: name for name, ts in toolsets.items() for t in ts["tools"]}
    rules = grants.get(role, [])
    errors = []
    for tool in manifest_tools:
        toolset = by_tool.get(tool)
        if toolset is None:
            errors.append(f"{role}: tool {tool} is not in the tool register")
        elif toolset not in manifest_toolsets:
            errors.append(f"{role}: tool {tool} is in toolset {toolset}, which the manifest does not list")
        elif f"-{tool}" in rules or not (tool in rules or f"{toolset}:*" in rules):
            errors.append(f"{role}: tool {tool} is outside the role's grant")
        elif toolset == "ui":
            errors.append(f"{role}: ui tools are applied by the engine, never listed for a role")
    return errors


def role_tools(
    role: str,
    manifest_tools: Iterable[str],
    registry: Registry,
    grants: dict[str, list[str]],
    stage: str = "S1",
    released: frozenset[str] = frozenset(),
) -> list[str]:
    """Registered tools of `stage` or earlier the role may call: manifest ∩ grant, minus ui and hidden tools (PRM-06).
    A tool the build carries ahead of its stage is left out until the caller's stage reaches it, unless the session
    released it (`released`, early-carried tools only). Text in skills, guidance, transcripts or tool results is not an
    input here, so it can never add a tool."""
    out = []
    for name in manifest_tools:
        spec = registry.specs.get(name)
        if spec is None or name in NEVER_LISTED or spec.meta.risk == "ui":
            continue
        if name in EARLY and spec.stage > stage and name not in released:
            continue
        if granted(role, spec, grants):
            out.append(name)
    return out


# --- availability (TOL-06) ---------------------------------------------------------------------------------------


@dataclass
class Availability:
    """`available(ctx)` evaluated when the prompt is built and cached for the turn with a grace window, so a tool
    does not flicker mid-turn."""

    check: Callable[[ToolSpec, SessionCtx], bool]
    grace_s: float
    clock: Callable[[], float] = time.monotonic
    _cache: dict[str, tuple[float, bool]] = field(default_factory=dict)

    def __call__(self, spec: ToolSpec, session: SessionCtx) -> bool:
        now = self.clock()
        held = self._cache.get(spec.name)
        if held is not None and now - held[0] < self.grace_s:
            return held[1]
        value = self.check(spec, session) and _session_allows(spec, session)
        self._cache[spec.name] = (now, value)
        return value

    def reset(self) -> None:
        self._cache.clear()


def _session_allows(spec: ToolSpec, session: SessionCtx) -> bool:
    """Wrong level or subject, a stage not shipped, cloud-only on the local tier."""
    meta = spec.meta
    if spec.stage > session.stage and not (spec.name in EARLY and spec.name in session.released):
        return False
    if meta.executor == "cloud" and "cloud_tier" not in session.enabled_online_features:
        return False
    if meta.risk == "compute":
        subjects = meta.subjects
        if session.subject not in subjects and "general" not in subjects:
            return False
        if session.level not in meta.levels:
            return False
    return True


def visible_tools(
    role: str,
    names: Iterable[str],
    registry: Registry,
    session: SessionCtx,
    available: Callable[[ToolSpec, SessionCtx], bool],
    grants: dict[str, list[str]],
    settings: Settings,
) -> list[ToolSpec]:
    """The tools a model is offered this call, in register order, after every filter."""
    out = []
    for name in names:
        spec = registry.specs.get(name)
        if spec is None or name in NEVER_LISTED or spec.meta.risk == "ui":
            continue
        if denial(role, spec, session, grants) is not None or not available(spec, session):
            continue
        if session.profile == "small" and not spec.meta.small_model_friendly:
            continue
        out.append(spec)
    if session.profile == "small":
        out = out[: settings.tool_small_max_tools]
    return out


def model_definitions(specs: Iterable[ToolSpec], ctx: InitCtx) -> list[dict[str, Any]]:
    return [s.model_definition(ctx) for s in specs]


# --- no open web (PRV-15) ------------------------------------------------------------------------------------------

_URL_NAME = re.compile(r"(^|_)(url|uri|href|link|website|web_address)s?($|_)")


def open_web_errors(
    registry_entries: dict[str, tuple[str, dict[str, Any]]], schemas: dict[str, dict[str, Any]]
) -> list[str]:
    """No tool but `python_run` uses the network; no argument is typed or named as a URL."""
    errors = []
    for name, (_, entry) in registry_entries.items():
        if entry.get("usesNetwork") and name != "python_run":
            errors.append(f"{name}: usesNetwork is true")
    for name, schema in schemas.items():
        for path, node in _walk_properties(schema):
            if node.get("format") in ("uri", "url", "iri", "uri-reference") or _URL_NAME.search(path.split(".")[-1]):
                errors.append(f"{name}: argument {path} is a URL")
    return errors


def _walk_properties(schema: dict[str, Any], prefix: str = "") -> Iterable[tuple[str, dict[str, Any]]]:
    for key, node in schema.get("properties", {}).items():
        if isinstance(node, dict):
            yield f"{prefix}{key}", node
            yield from _walk_properties(node, f"{prefix}{key}.")
            if isinstance(node.get("items"), dict):
                yield from _walk_properties(node["items"], f"{prefix}{key}[].")
