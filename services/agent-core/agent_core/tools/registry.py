"""The tool registry (TOL-05): one static list grouped by toolset, checked at registration and against the tool
register, with a fingerprint of the schema set for the trace of every model request (OBS-04)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from agent_core.tools.spec import ToolMeta, ToolSpec

NAME = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
STAGES = ("S1", "S2", "S3", "S4", "S5")
# Argument names that collide with chat templates (TOL-07).
RESERVED_ARGUMENTS = frozenset({"items", "type", "function", "name", "role", "content", "tool"})
OFFLINE_RISKS = frozenset({"compute", "read", "ui", "memory_write"})
# Tools a build carries ahead of their register stage, by build stage (TOL-23 groundwork for M15): the memory tools
# ride in the S1 build so the engine can bind their handlers. Each keeps its register stage, so it is offered only to
# roles and sessions of that stage, and like any host tool only once its executor is registered (TOL-06).
# The companion's study toolset (SPEC §19.3.3, W8) rides the same way: released only to a companion session.
EARLY_TOOLS: dict[str, frozenset[str]] = {
    "S1": frozenset(
        {
            "memory_recall",
            "session_recall",
            "memory_note",
            "progress_update",
            "study_search",
            "progress_read",
            "plan",
            "make_aid",
            "start_research",
        }
    ),
}


class RegistrationError(ValueError):
    """A tool cannot be registered; the message names it."""


def _refs(node: Any) -> Iterable[str]:
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            yield node["$ref"]
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


def _argument_names(schema: dict[str, Any]) -> set[str]:
    names = set((schema.get("properties") or {}).keys())
    for value in (schema.get("properties") or {}).values():
        if isinstance(value, dict) and value.get("type") == "object":
            names |= _argument_names(value)
    return names


def spec_errors(spec: ToolSpec) -> list[str]:
    """Registration checks for one tool (TOL-03, TOL-04, TOL-05, TOL-07)."""
    errors = []
    if not NAME.match(spec.name):
        errors.append("the name must match ^[a-z][a-z0-9_]{1,39}$")
    for label, schema in (("params", spec.params), ("paramsSmall", spec.params_small)):
        if schema is None:
            continue
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as error:
            errors.append(f"{label} is not a valid JSON Schema: {error.message}")
            continue
        external = [r for r in _refs(schema) if not r.startswith("#")]
        if external:
            errors.append(f"{label} uses $ref outside the document: {external[0]}")
        reserved = sorted(_argument_names(schema) & RESERVED_ARGUMENTS)
        if reserved:
            errors.append(f"{label} uses reserved argument names: {', '.join(reserved)}")
    meta = spec.meta
    if meta.interactive and meta.timeout_ms is not None:
        errors.append("an interactive tool has no deadline (timeoutMs must be null)")
    if not meta.interactive and meta.timeout_ms is None:
        errors.append("a non-interactive tool needs a deadline (timeoutMs)")
    if meta.dedupe and (not meta.idempotent or meta.side_effects):
        errors.append("dedupe needs an idempotent tool without side effects")
    if meta.uses_network and meta.risk not in ("egress", "external"):
        errors.append("only egress or external tools may use the network")
    if meta.uses_network and not meta.offline_behaviour:
        errors.append("a tool that uses the network must declare its offline behaviour")
    if meta.risk in OFFLINE_RISKS and meta.uses_network:
        errors.append(f"a {meta.risk} tool must work with no network")
    if spec.stage not in STAGES:
        errors.append(f"unknown stage {spec.stage}")
    if not spec.describe_ok():
        errors.append("the description is empty")
    return errors


@dataclass(frozen=True)
class Registry:
    specs: dict[str, ToolSpec]

    def __getitem__(self, name: str) -> ToolSpec:
        return self.specs[name]

    def __contains__(self, name: object) -> bool:
        return name in self.specs

    def by_toolset(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for spec in self.specs.values():
            groups.setdefault(spec.toolset, []).append(spec.name)
        return groups

    @property
    def fingerprint(self) -> str:
        """sha256 of the canonical schema set (logged per model request, OBS-04)."""
        canonical = {n: {"version": s.version, "params": s.params, "paramsSmall": s.params_small}
                     for n, s in sorted(self.specs.items())}  # fmt: skip
        return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def register_entries(
    register: dict[str, Any], stage: str, device: bool = True, early: frozenset[str] = frozenset()
) -> dict[str, tuple[str, dict[str, Any]]]:
    """The register's tools shipped by `stage` or earlier, plus the named `early` tools shipped ahead of their stage:
    name -> (toolset, entry). A device or school-server build never carries a cloud-executed tool such as
    `python_run` (SEC-09)."""
    shipped = STAGES[: STAGES.index(stage) + 1]
    return {t["name"]: (toolset, t) for toolset, group in register["toolsets"].items() for t in group["tools"]
            if (t["stage"] in shipped or t["name"] in early)
            and not (device and t["executor"] == "cloud")}  # fmt: skip


def build_entries(
    register: dict[str, Any], stage: str, early: frozenset[str] = frozenset()
) -> dict[str, tuple[str, dict[str, Any]]]:
    """The tools a device build of `stage` registers: its stage's register entries, the tools offered early (`early`,
    offered with the build's stage) and the tools carried early but held back (`EARLY_TOOLS`, which keep their
    register stage, so no session or role of an earlier stage is offered them)."""
    shipped = register_entries(register, stage, early=early)
    held = EARLY_TOOLS.get(stage, frozenset())
    return {name: found for name, found in register_entries(register, STAGES[-1]).items()
            if name in shipped or name in held}  # fmt: skip


def build(
    specs: Iterable[ToolSpec], register: dict[str, Any], stage: str, early: frozenset[str] = frozenset()
) -> Registry:
    """Register every spec, failing on the first problem with the tool's name (TOL-05)."""
    by_name: dict[str, ToolSpec] = {}
    for spec in specs:
        if spec.name in by_name:
            raise RegistrationError(f"{spec.name}: registered twice")
        problems = spec_errors(spec)
        if problems:
            raise RegistrationError(f"{spec.name}: {problems[0]}")
        by_name[spec.name] = spec
    expected = build_entries(register, stage, early=early)
    for name in sorted(set(by_name) - set(expected)):
        raise RegistrationError(f"{name}: in code but not in the tool register for {stage}")
    for name in sorted(set(expected) - set(by_name)):
        raise RegistrationError(f"{name}: in the tool register for {stage} but not in code")
    for name, (toolset, entry) in expected.items():
        spec = by_name[name]
        if spec.toolset != toolset:
            raise RegistrationError(f"{name}: toolset {spec.toolset} differs from the register's {toolset}")
        if spec.meta != ToolMeta.from_register(entry):
            raise RegistrationError(f"{name}: metadata differs from the tool register")
    return Registry(by_name)
