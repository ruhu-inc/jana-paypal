"""`build_prompt`: the one way a model request's messages and tools are assembled (PRM-09 to PRM-14, PRM-07, TOL-06).

Pure and deterministic: the same loaded files, model capabilities and session state give byte-identical output.
No clock, counter or random value appears above tier 8; the only date is the session date in tier 7, passed in.

Tiers, in order: 1 soul (by role), 2 role body, 3 guidance blocks, skill index and family overlay, 4 output contract
| breakpoint A | 5 topic context, 6 learner snapshot, 7 session facts | breakpoint B | 8 per-turn `<tutor-context>`,
9 transcript. Tiers 1 to 7 form the system message; tier 8 rides on the last student message; tier 9 is the messages.

In tiers 2 to 5 every line that names, in backticks, a tool the role cannot call this turn is dropped and reported."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from jsonschema import Draft202012Validator

from agent_core.assets.agent import PLACEHOLDER, AgentAssets
from agent_core.assets.skills import Skill, capped_slice, wrap
from agent_core.config import Settings

TIER_NAMES = (
    "soul",
    "role",
    "guidance",
    "output_contract",
    "topic_context",
    "learner_snapshot",
    "session_facts",
    "tutor_context",
    "transcript",
)
PROMPT_MODES = ("full", "compact", "minimal")
TEACHING_SKILL_ROLES = ("coach", "diagnoser", "solver", "checker")  # the pushed topic skill (SKL-08, AGT-01 inputs)
LANGUAGES = ("en", "fr", "pt", "ar")
SPOKEN_FORM = "spoken_form"  # the guidance kind of the spoken-form overlay (PRM-26)
_TOOL_MENTION = re.compile(r"`([a-z][a-z0-9_]{1,39})`")
_ROLE_PROJECTION = re.compile(
    r"(?ms)^<!-- jana:projection ([a-z_]+) -->\n(.*?)^<!-- jana:end-projection -->\n?"
)


def role_projection(role: str, body: str, schema: dict[str, Any] | None) -> str:
    """Select a whole authored phase from the engine-owned output contract, never from learner text.

    A proposal must not be instructed to call tools or invent a completed plan, and a checked-text reply
    must not be instructed to generate the board/provenance fields the engine owns. Other phases retain
    the normal role body; a role without authored projections remains compatible.
    """
    phases: dict[str, str] = {}

    def collect(match: re.Match[str]) -> str:
        phases[match[1]] = match[2].strip()
        return ""

    normal = _ROLE_PROJECTION.sub(collect, body).strip()
    fields = set((schema or {}).get("properties", {}))
    phase = (
        "arithmetic" if role == "solver" and fields in ({"expression", "unit"}, {"calculations"})
        else "evidence" if role == "diagnoser" and fields == {"misconception_ref"}
        else "intake" if role == "diagnoser" and {"knowns", "unknowns"} <= fields and "inference" not in fields
        and "quote" not in (schema or {})["properties"]["knowns"].get("items", {}).get("properties", {})
        else "checked_text" if role == "coach"
        and fields & {"say", "operation_question", "repair_question", "confirmation"}
        and "board" not in fields
        else ""
    )
    return phases.get(phase, normal)


def canonical_family(reported: str) -> str:
    """Map runtime architecture spelling to authored overlay spelling; preserve unknown families."""
    return {"qwen35": "qwen3.5"}.get(reported, reported)


class PromptError(ValueError):
    def __init__(self, message: str, *, tier: int | None = None, actual: int | None = None, limit: int | None = None):
        super().__init__(message)
        self.budget = {"tier": tier, "actual": actual, "limit": limit} if tier is not None else None


@dataclass(frozen=True)
class Message:
    role: str
    content: str
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: tuple[Any, ...] = ()


@dataclass(frozen=True)
class Passage:
    id: str
    text: str
    rank: int  # 1 is the highest-ranked
    cite_id: str | None = None


@dataclass(frozen=True)
class PromptSession:
    """Session state the builder reads. Built by the engine from its own state, never from model text."""

    mode: str = "guided"
    subject: str = "physics"
    level: str = "O"
    language: str = "en"
    session_date: str = "2026-01-01"
    teaching_skill: Skill | None = None
    # SKL-15: the pedagogy skill pushed with the teaching skill (the first its `pedagogy` names, never a conflicting
    # one), to the Coach only.
    pedagogy_skill: Skill | None = None
    passages: tuple[Passage, ...] = ()
    glossary: tuple[str, ...] = ()
    # Tier 6: the learner block the engine rendered from the session's frozen snapshot for this call's variant (LRN-05,
    # MEM-03, MEM-13), already fitted to the tier's allowance, with the cuts it made (for `PromptBuild.dropped`).
    learner_block: str = ""
    learner_dropped: tuple[dict[str, str], ...] = ()
    tutor_context: str | None = None
    transcript: tuple[Message, ...] = ()
    task: str | None = None  # a delegation's task (delegated roles see no transcript)
    hardware_class: str = "mid"  # low, mid, high; "cloud" for a cloud candidate
    # Tools available in this session (stage, subject, level, connectivity): gates which skills are offered (SKL-23).
    session_tools: frozenset[str] | None = None
    # A voice turn (VOC-37): the spoken-form overlay joins tier 3 (PRM-26); outside voice mode it is absent.
    voice: bool = False
    # PRM-03, D99: the open profile's chosen tutor name, which fills the soul's placeholder in place of the identity
    # file's default; None keeps the default.
    tutor_name: str | None = None
    computation: bool = False  # engine-owned Solver phase, before any live calculation check
    checked_hint_rung: int | None = None  # only this rung of the ladder for a checked-board reply
    checked_complete: bool = False  # no further hints on an immediately completed question


@dataclass(frozen=True)
class Tier:
    index: int
    name: str
    text: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()

    @property
    def bytes(self) -> int:
        return len(self.text.encode())

    @property
    def tokens(self) -> int:
        return math.ceil(len(self.text) / 4)  # about 4 characters per token (PRM-09)


@dataclass(frozen=True)
class PromptBuild:
    role: str
    prompt_mode: str
    tiers: tuple[Tier, ...]
    messages: tuple[Message, ...]
    tools: tuple[dict[str, Any], ...]
    cache_breakpoints: tuple[int, int]  # character offsets in the system message, after tiers 4 and 7
    dropped: tuple[dict[str, str], ...]
    skills_delivered: tuple[dict[str, str], ...]

    @property
    def tool_set_sha256(self) -> str:
        canonical = json.dumps(list(self.tools), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def fingerprints(self) -> dict[str, Any]:
        """Per tier sha256, bytes and estimated tokens, and the tool-set sha256 (PRM-13, OBS-02). No student text."""
        return {
            "tiers": [
                {"tier": t.index, "name": t.name, "sha256": t.sha256, "bytes": t.bytes, "tokens": t.tokens}
                for t in self.tiers
            ],
            "tools_sha256": self.tool_set_sha256,
        }

    def sizes(self) -> dict[str, dict[str, int]]:
        return {t.name: {"chars": len(t.text), "tokens": t.tokens} for t in self.tiers}


def prompt_mode_for(role_manifest_mode: str, hardware_class: str, override: str | None = None) -> str:
    """PRM-14: delegated roles always minimal; by_class follows the hardware class; the router may override."""
    if override in PROMPT_MODES:
        return str(override)
    if role_manifest_mode == "minimal":
        return "minimal"
    return {"low": "minimal", "mid": "compact", "high": "full", "cloud": "full"}.get(hardware_class, "compact")


def budgets(settings: Settings, mode: str, hardware_class: str) -> list[int]:
    table = {
        "full": settings.prompt_budget_full,
        "compact": settings.prompt_budget_compact,
        "minimal": settings.prompt_budget_minimal,
    }[mode]
    values = list(table)
    if hardware_class == "cloud" and mode == "full":
        values[4], values[5] = settings.prompt_budget_cloud_topic, settings.prompt_budget_cloud_snapshot
    return values


def _drop_tool_lines(
    text: str, callable_tools: set[str], known_tools: set[str], tier: int, dropped: list[dict[str, str]], source: str
) -> str:
    kept = []
    for line in text.splitlines():
        missing = [t for t in _TOOL_MENTION.findall(line) if t in known_tools and t not in callable_tools]
        if missing:
            dropped.append({"tier": str(tier), "item": source, "reason": f"names unavailable tool {missing[0]}"})
            continue
        kept.append(line)
    return "\n".join(kept)


def output_contract(schema: dict[str, Any] | None, output_mode: str, limit: int = 10_000) -> str:
    """Tier 4: a compact nested JSON shape when it fits, then field/types, then names. The full schema and gates
    enforce types, conditions and evidence; the example teaches field names even on an unconstrained tool call."""
    if output_mode == "text" or not schema:
        return "Reply in plain text."
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    text_reply = str(schema.get("$id", "")).endswith("/tutor-turn")

    def shape(spec: dict[str, Any], depth: int = 0) -> Any:
        # A shallow list of top-level names loses required fields inside steps/final_answer. With tools offered
        # the first reply is unconstrained, so the model must see those names before the decode-only retry.
        if depth > 8 or any(key in spec for key in ("$ref", "anyOf", "oneOf")):
            raise ValueError("a compact example cannot represent this schema")
        if "const" in spec:
            return spec["const"]
        kind = spec.get("type")
        kinds = kind if isinstance(kind, list) else [kind]
        if "enum" in spec:
            return spec["enum"][0]
        if "null" in kinds:
            return None
        if "object" in kinds:
            if not spec.get("properties"):
                raise ValueError("the object has no fixed shape")
            sample = {
                name: shape(child, depth + 1)
                for name, child in spec["properties"].items()
                if name in spec.get("required", [])
            }
            # A complete plan requires a non-null final answer even though a blocked plan permits null.
            # Honour conditional type refinements instead of teaching an invalid minimal example.
            for branch in spec.get("allOf", []):
                condition = branch.get("if")
                if condition and Draft202012Validator(condition).is_valid(sample):
                    for name, refinement in branch.get("then", {}).get("properties", {}).items():
                        if name in sample and "type" in refinement:
                            sample[name] = shape({**spec["properties"][name], **refinement}, depth + 1)
            return sample
        if "array" in kinds:
            if spec.get("maxItems") == 0:
                return []
            if text_reply and not spec.get("minItems", 0):
                return []
            return [shape(spec.get("items", {}), depth + 1)]
        if "string" in kinds:
            return "..."
        if "null" in kinds:
            return None
        if "integer" in kinds or "number" in kinds:
            return spec.get("minimum", 1)
        if "boolean" in kinds:
            return True
        raise ValueError("the field has no simple type")

    try:
        example = json.dumps(shape(schema), separators=(",", ":"), ensure_ascii=False)
        instruction = "Reply with compact JSON on one line. Keep this shape; replace sample values:\n" + example
        if len(instruction) <= limit:
            return instruction
    except ValueError:
        pass  # Rich unions/refs retain the field/type summary; the full decoder schema remains authoritative.

    def render(detail: int) -> str:
        if detail == 0:
            return "Reply with one compact JSON object on one line, with the fields: " + ", ".join(properties) + "."
        lines = ["Reply with one compact JSON object on one line (no indentation), with these fields:"]
        for name, spec in properties.items():
            kind = spec.get("type") or ("enum" if "enum" in spec else "object")
            kinds = "|".join(kind) if isinstance(kind, list) else kind
            enum = ""
            if detail == 2 and spec.get("enum"):
                enum = " one of " + ", ".join(str(v) for v in spec["enum"] if v is not None)
            lines.append(f"- {name} ({kinds}{', required' if name in required else ''}){enum}")
        return "\n".join(lines)

    for detail in (2, 1, 0):
        text = render(detail)
        if len(text) <= limit:
            return text
    return render(0)[:limit]


def build_prompt(
    role: str,
    family: str,
    session: PromptSession,
    tools: Sequence[dict[str, Any]],
    callable_tools: set[str],
    known_tools: set[str],
    assets: AgentAssets,
    settings: Settings,
    output_schema: dict[str, Any] | None = None,
    prompt_mode_override: str | None = None,
) -> PromptBuild:
    """`tools` are the model-facing schemas after node and availability filtering (TOL-06); `callable_tools` their
    names; `known_tools` every tool name of the catalogue (so a line naming a hidden one can be recognised)."""
    family = canonical_family(family)
    if session.language not in LANGUAGES:
        raise PromptError("unsupported_locale")
    manifest = assets.roles.get(role)
    if manifest is None:
        raise PromptError(f"no role manifest for {role}")
    mode = prompt_mode_for(str(manifest.meta["prompt_mode"]), session.hardware_class, prompt_mode_override)
    limits = budgets(settings, mode, session.hardware_class)
    dropped: list[dict[str, str]] = []
    delegated = manifest.meta["prompt_mode"] == "minimal"

    # Tier 1: the soul as the role is allowed to see it (PRM-04).
    soul_level = str(manifest.meta["soul"])
    soul = assets.soul.named(session.tutor_name).exposure(soul_level)
    if soul_level != "full":
        soul = soul[: settings.prompt_team_soul_max_chars]
    # Tier 2: the selected whole authored role phase (never cut; an oversize phase fails CI).
    body = _drop_tool_lines(role_projection(role, manifest.body, output_schema), callable_tools,
                            known_tools, 2, dropped, f"role:{role}")  # fmt: skip
    # Tier 3: guidance blocks by priority then id, the always-on skill index, the family overlay. The spoken-form
    # overlay is selected here, by the voice flag, never by the voice sidecar (PRM-26).
    blocks = [
        b
        for b in assets.guidance
        if b.applies(role, family, session.mode, mode, callable_tools, session.language)
        and (b.meta["kind"] != SPOKEN_FORM or session.voice)
    ]
    for block in assets.guidance:
        if block not in blocks:
            dropped.append(
                {
                    "tier": "3",
                    "item": f"guidance:{block.id}",
                    "reason": _why_not(block, role, family, session, mode, callable_tools),
                }
            )
    blocks.sort(key=lambda b: (-int(b.meta["priority"]), b.id))
    overlays = [
        b for b in assets.families if b.applies(role, family, session.mode, mode, callable_tools, session.language)
    ]
    filled = guidance_values(settings)
    parts = [
        (b, _drop_tool_lines(_fill(b.body, filled), callable_tools, known_tools, 3, dropped, f"guidance:{b.id}"))
        for b in blocks
    ]
    index_text = ""
    indexed: list[dict[str, str]] = []
    if "skill" in callable_tools:
        available = set(session.session_tools) if session.session_tools is not None else None
        lines = assets.skills.index(role, available, settings.skill_index_max_lines, settings.skill_index_max_chars)
        if lines:
            index_text = "Skills you can load with `skill`:\n" + "\n".join(lines)
            indexed = [{"name": s.name, "kind": s.kind, "version": s.version, "sha256": s.sha256, "route": "index"}
                       for s in assets.skills.indexed(role, available)]  # fmt: skip
    overlay_text = "\n".join(
        _drop_tool_lines(o.body, callable_tools, known_tools, 3, dropped, f"family:{o.id}") for o in overlays
    )
    # PRM-26: a voice turn MUST carry the spoken-form overlay, so like the family overlay it is never a budget victim.
    spoken = [t for b, t in parts if b.meta["kind"] == "spoken_form"]
    parts = [(b, t) for b, t in parts if b.meta["kind"] != "spoken_form"]
    while parts and len(_join([t for _, t in parts] + spoken + [index_text, overlay_text])) > limits[2]:
        # Overflow step 1: lowest-priority blocks first; essential blocks are cut last.
        victim = min(parts, key=lambda p: (bool(p[0].meta.get("essential")), int(p[0].meta["priority"]), p[0].id))
        parts.remove(victim)
        dropped.append({"tier": "3", "item": f"guidance:{victim[0].id}", "reason": "over the tier budget"})
    guidance = _join([t for _, t in parts] + spoken + [index_text, overlay_text])
    # Tier 4: output contract.
    if role == "solver" and tools and session.computation:
        contract = (
            "Current phase: computation. Call an available calculation tool now. Do not return a JSON "
            "solution plan yet. Substitute supplied numeric knowns, leaving only the requested unknowns. "
            "Wait for the tool results; the plan's JSON contract applies afterwards."
        )
    else:
        contract = output_contract(output_schema, str(manifest.meta["output_mode"]), limits[3])
    # Tier 5: topic context (pushed teaching skill, glossary, ranked passages).
    delivered: list[dict[str, str]] = []
    topic_parts: list[str] = []
    cap = settings.skill_push_max_chars
    checked_text = role == "coach" and bool({"say", "operation_question", "repair_question", "confirmation"} & set(
        (output_schema or {}).get("properties", {}))) and "board" not in (output_schema or {}).get("properties", {})
    arithmetic = role == "solver" and set((output_schema or {}).get("properties", {})) in (
        {"expression", "unit"}, {"calculations"}
    )
    if session.teaching_skill is not None and role in TEACHING_SKILL_ROLES:
        skill = session.teaching_skill
        given = wrap(skill, capped_slice(skill, role, cap, dropped, checked_text=checked_text,
                                        hint_rung=session.checked_hint_rung, complete=session.checked_complete,
                                        arithmetic=arithmetic))
        topic_parts.append(_drop_tool_lines(given, callable_tools, known_tools, 5, dropped, f"skill:{skill.name}"))
        delivered.append(
            {
                "name": skill.name,
                "kind": skill.kind,
                "version": skill.version,
                "sha256": skill.sha256,
                "route": "pushed",
            }
        )
    pedagogy_text = ""
    if session.pedagogy_skill is not None and role == "coach" and session.teaching_skill is not None:
        guide = session.pedagogy_skill
        pedagogy_text = _drop_tool_lines(wrap(guide, capped_slice(guide, role, cap, dropped,
                                                               checked_text=checked_text)), callable_tools,
                                         known_tools, 5, dropped, f"skill:{guide.name}")  # fmt: skip
    glossary_lines = list(session.glossary) if not delegated else []
    passages = sorted(session.passages, key=lambda p: (p.rank, p.id)) if not delegated else []
    kept_passages = list(passages)

    def glossary_text() -> str:
        heading = "Terms as this course defines them (use these, not your own):"
        return heading + "\n" + "\n".join(glossary_lines) if glossary_lines else ""

    def topic_size() -> int:
        return len(_join(topic_parts[:1] + [pedagogy_text, glossary_text()] + [_passage(p) for p in kept_passages]))

    while len(kept_passages) > 1 and topic_size() > limits[4]:
        cut = kept_passages.pop()  # Overflow step 2: passages beyond the top-ranked ones.
        dropped.append({"tier": "5", "item": f"passage:{cut.id}", "reason": "over the tier budget"})
    while len(glossary_lines) > 1 and topic_size() > limits[4]:
        # Then the course definitions beyond the first, least relevant first (the engine ranks the question's terms
        # first), before any pushed skill.
        line = glossary_lines.pop()
        dropped.append({"tier": "5", "item": "glossary:" + line[2:].split(":")[0], "reason": "over the tier budget"})
    if pedagogy_text and topic_size() > limits[4]:
        # The pushed pedagogy skill goes before any of the teaching skill: it stays loadable from the index (SKL-07).
        assert session.pedagogy_skill is not None
        pedagogy_text = ""
        dropped.append({"tier": "5", "item": f"skill:{session.pedagogy_skill.name}", "reason": "over the tier budget"})
    elif pedagogy_text and session.pedagogy_skill is not None:
        guide = session.pedagogy_skill
        delivered.append({"name": guide.name, "kind": guide.kind, "version": guide.version, "sha256": guide.sha256,
                          "route": "pushed"})  # fmt: skip
    if delivered:
        # Then the pushed skill's last sections, whole sections at a time, until the tier fits (SKL-08 caps the
        # skill; a small prompt mode keeps its first sections rather than failing the call).
        pushed = session.teaching_skill
        assert pushed is not None
        sections = re.split(r"(?m)^(?=## )", capped_slice(
            pushed, role, cap, [], checked_text=checked_text,
            hint_rung=session.checked_hint_rung, complete=session.checked_complete, arithmetic=arithmetic,
        ).strip())
        while len(sections) > 1 and topic_size() > limits[4]:
            sections.pop()
            topic_parts[0] = _drop_tool_lines(wrap(pushed, "".join(sections)), callable_tools, known_tools, 5, [],
                                              f"skill:{pushed.name}")  # fmt: skip
            if not any(d.get("item") == f"skill:{pushed.name}" and d["reason"] == "over the tier budget"
                       for d in dropped):  # fmt: skip
                dropped.append({"tier": "5", "item": f"skill:{pushed.name}", "reason": "over the tier budget"})
    topic = _join(
        topic_parts[:1]
        + [pedagogy_text, glossary_text()]
        + [
            _drop_tool_lines(_passage(p), callable_tools, known_tools, 5, dropped, f"passage:{p.id}")
            for p in kept_passages
        ]
    )
    # Tier 6: the learner block (never for delegated roles; PRM-14, MEM-20). The engine fits it to the tier's
    # allowance in the fixed cut order (index lines, then notes, then the third KC; MEM-13), so it arrives within the
    # budget; the cuts it made are reported with this build's.
    snapshot = ""
    if not delegated and session.learner_block:
        snapshot = session.learner_block
        dropped.extend(dict(d) for d in session.learner_dropped)
    # Tier 7: session facts; never a clock.
    facts = f"Session: {session.session_date}; mode {session.mode}; subject {session.subject}; level {session.level}."
    # Tier 8: the per-turn block (built by the engine from its state).
    tutor_context = f"<tutor-context>\n{session.tutor_context}\n</tutor-context>" if session.tutor_context else ""

    stable = [soul, body, guidance, contract]
    if any(PLACEHOLDER.search(text) for text in stable):  # PRM-02: never a prompt with a hole in it
        raise PromptError("unfilled_placeholder")
    for index, (text, limit) in enumerate(zip(stable, limits[:4], strict=True), start=1):
        if len(text) > limit and index != 3:
            raise PromptError(
                f"tier {index} ({TIER_NAMES[index - 1]}) is {len(text)} characters, over its budget {limit}"
            )
    tiers = [
        Tier(i + 1, TIER_NAMES[i], t)
        for i, t in enumerate([soul, body, guidance, contract, topic, snapshot, facts, tutor_context])
    ]
    for number, text in ((5, topic), (6, snapshot), (8, tutor_context)):
        if len(text) > limits[number - 1]:
            raise PromptError(
                "a context tier is over its budget after the overflow cuts",
                tier=number,
                actual=len(text),
                limit=limits[number - 1],
            )

    system_a = _join([soul, body, guidance, contract])
    # D128: on a lean class the topic (tier 5) opens the first user message instead, so the system prompt and the
    # tools the template renders after it stay one prefix for the whole session, read once by the runtime's warm-up;
    # the topic is read once per problem. The learner block stays in the system prompt, where a cloud call swaps it.
    lean = session.hardware_class in settings.lean_classes
    system = _join([system_a, snapshot, facts] if lean else [system_a, topic, snapshot, facts])
    breakpoint_a = len(system_a)
    breakpoint_b = len(system)
    transcript = list(session.transcript) if not delegated else []
    if delegated and session.task:
        transcript = [Message("user", session.task)]
    if lean and topic:
        transcript = _with_topic(transcript, topic)
    transcript = _with_tutor_context(transcript, tutor_context)
    tiers.append(Tier(9, TIER_NAMES[8], json.dumps([m.content for m in transcript], ensure_ascii=False)))
    messages = (Message("system", system), *transcript)
    return PromptBuild(
        role,
        mode,
        tuple(tiers),
        messages,
        tuple(tools),
        (breakpoint_a, breakpoint_b),
        tuple(dropped),
        tuple(delivered + indexed),
    )


def _with_topic(transcript: list[Message], topic: str) -> list[Message]:
    """The topic context at the head of the first user message (a new user message when there is none)."""
    for index, message in enumerate(transcript):
        if message.role == "user":
            return [*transcript[:index], replace(message, content=_join([topic, message.content])),
                    *transcript[index + 1:]]  # fmt: skip
    return [Message("user", topic), *transcript]


def guidance_values(settings: Settings) -> dict[str, str]:
    """The engine-filled `{{name}}` values of guidance blocks: the spoken form's limits (PRM-26, VOC-37)."""
    return {"spoken_max_chars": str(settings.spoken_max_chars),
            "spoken_max_sentences": str(settings.spoken_max_sentences)}  # fmt: skip


def _fill(body: str, values: dict[str, str]) -> str:
    return PLACEHOLDER.sub(lambda m: values.get(m.group(1).strip(), m.group(0)), body)


def _why_not(block: Any, role: str, family: str, session: PromptSession, mode: str, available: set[str]) -> str:
    m = block.meta
    if (
        m["kind"] == SPOKEN_FORM
        and not session.voice
        and block.applies(role, family, session.mode, mode, available, session.language)
    ):
        return "not a voice turn"
    if role not in m["applies_to"] and "*" not in m["applies_to"]:
        return f"not for role {role}"
    if "*" not in m["models"] and family not in m["models"]:
        return f"not for model family {family}"
    if not set(m.get("requires_tools", [])) <= available:
        return "a required tool is unavailable"
    if session.mode and m.get("modes") and session.mode not in m["modes"]:
        return f"not for mode {session.mode}"
    if m.get("prompt_modes") and mode not in m["prompt_modes"]:
        return f"not for prompt mode {mode}"
    if mode == "minimal" and not m.get("essential"):
        return "not essential (minimal prompt mode)"
    return "locale"


def _passage(p: Passage) -> str:
    cite = f' cite="{p.cite_id}"' if p.cite_id else ""
    note = "Use the cite attribute in output cites; id is full source provenance.\n" if p.cite_id else ""
    return f'<passage id="{p.id}"{cite}>\n{note}{p.text}\n</passage>'


def _join(parts: Sequence[str]) -> str:
    return "\n\n".join(p for p in parts if p)


def _with_tutor_context(transcript: list[Message], block: str) -> list[Message]:
    """Append the per-turn block to the last student message only; earlier ones never carry it (PRM-12, PRM-15)."""
    if not block:
        return transcript
    for i in range(len(transcript) - 1, -1, -1):
        if transcript[i].role == "user" and not transcript[i].content.startswith("<app-event"):  # a student message
            transcript = list(transcript)
            transcript[i] = replace(transcript[i], content=f"{transcript[i].content}\n\n{block}")
            break
    return transcript


@dataclass(frozen=True)
class Snapshot:
    """A built prompt as committed for review (PRM-22, EVL-09)."""

    text: str
    tier_sha256: list[str] = field(default_factory=list)

    @classmethod
    def of(cls, build: PromptBuild) -> Snapshot:
        rendered = "\n".join(f"===== {m.role} =====\n{m.content}" for m in build.messages)
        rendered += "\n===== tools =====\n" + json.dumps(list(build.tools), indent=1, sort_keys=True)
        return cls(rendered + "\n", [t.sha256 for t in build.tiers])
