"""The visual spec path in the agent core (VSL-01, VSL-02, VSL-09 to VSL-17, VSL-22, VSL-29).

A model never draws: the only path to a picture is a visual spec of a catalogue kind. The validator runs the stages of
TABLE VSL-15a in order and stops at the first fatal error; resolution turns a valid request into a resolved spec
deterministically (cached by hash); every claim number traces to a check, a known or pack data; anything rejected
falls back along the ladder (repaired, the topic's default visual, then text only), never blocking the explanation.

A template kind may name a preset from an installed pack by `ref` (VSL-29): the preset's values come first and the
request's own params override them field by field. A library kind names a diagram library entry (VSL-22), whose parts,
sequences and sanitised SVG the resolution carries to the client. The model is told what was wrong in one sentence
and may correct a request once when it failed at a stage it can fix (VSL-17, `feedback`)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from functools import cache
from typing import Any

from jsonschema import Draft202012Validator

from agent_core.assets import chemistry_kinds
from agent_core.assets.generated.kind_schemas import KIND_SCHEMAS
from agent_core.assets.generated.sim_scenarios import SIM_SCENARIOS
from agent_core.assets.generated.visual_catalogue import VISUAL_CATALOGUE
from agent_core.assets.visual_kinds import (
    LIBRARY_KINDS,
    Part,
    View,
    add_elements,
    alt_of,
    build_alt,
    claim_numbers,
    layers_of,
    parts_of,
    sequence_steps,
)
from agent_core.contracts import errors as contract_errors
from agent_core.contracts import schema_ids

ENVELOPE = "visuals/visual-spec"
ENGINE_FIELDS = ("visual_id", "status", "computed", "parts", "alt", "repairs")
REQUEST_FIELDS = ("spec_version", "type", "ref", "params", "numbers_from", "steps", "labels", "caption", "presentation")
FREEFORM = frozenset({"svg", "path", "html", "points_raw", "canvas", "mermaid", "tikz", "image", "image_url", "d"})
UNIT_SPELLINGS = {
    "newtons": "N",
    "newton": "N",
    "metres": "m",
    "meters": "m",
    "metre": "m",
    "seconds": "s",
    "kilograms": "kg",
    "joules": "J",
    "watts": "W",
    "volts": "V",
    "ohms": "\u03a9",
    "amps": "A",
}
TEXT_ONLY = "I can't draw this one, so here it is in words."
MARKUP = re.compile(
    r"<svg\b.*?</svg>|<(html|canvas|div|script|style)\b.*?</\1>|```(mermaid|tikz|svg|html)\b.*?```|"
    r"\\begin\{tikzpicture\}.*?\\end\{tikzpicture\}",
    re.S | re.I,
)
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
STAGES = ("S1", "S2", "S3", "S4", "S5")
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESOLVED_BYTES = 256 * 1024
# The validator stage of each fatal code (TABLE VSL-15a); the model may correct stages 1 to 6 and 8 once (VSL-17).
STAGE_OF = {
    "visual_unparseable": 1, "visual_envelope_invalid": 2, "visual_engine_field": 2, "visual_unknown_field": 2,
    "visual_kind_unknown": 3, "visual_kind_not_enabled": 3, "visual_param_invalid": 4, "visual_ref_missing": 5,
    "visual_ref_unapproved": 5, "visual_part_unknown": 6, "visual_resolution_failed": 7, "visual_resolution_timeout": 7,
    "visual_untraced_number": 8, "visual_too_large": 9,
}  # fmt: skip
RETRYABLE_STAGES = frozenset({1, 2, 3, 4, 5, 6, 8})
# Kinds whose parts depend on what resolution computes: their step ids are checked again once resolved.
DYNAMIC_PARTS = frozenset({
    "function_graph", "particle_diagram", "financial_statements", "bank_reconciliation", "trial_balance", "ray_diagram",
    "circuit", "balanced_equation", "t_account", "electron_shells", "projectile_sim", "circuit_sim", "titration_sim",
    "gas_laws_sim", "function_transform_sim", "electrolysis_sim", "rates_sim", "equilibrium_sim", "population_sim",
    "punnett_square", "diagram_comparison",
}) | chemistry_kinds.DYNAMIC_PARTS  # fmt: skip
# Optional S3 simulations held back until their own fidelity gate passes (M27, VSL-46): never advertised, and a request
# for one is refused at stage 3. Each reason is the record the plan asks for when a simulation is deferred.
UNAVAILABLE_KINDS = {
    "enzyme_sim": (
        "Its temperature and pH factors must come from a named, versioned, source-backed enzyme asset in an installed "
        "pack (content schema 1.6 science_assets); no Biology pack is installed and the pack build does not yet load "
        "science assets, so there is no preset to resolve and no fidelity set to check against."
    ),
    "osmosis_sim": (
        "Its permeability, geometry and plant or animal limits must come from a source-backed osmosis asset in an "
        "installed pack (content schema 1.6 science_assets); no Biology pack is installed and the pack build does not "
        "yet load science assets, so there is no preset to resolve."
    ),
}
# Pack-backed science definitions (VSL-46): the kind, the asset kind it names and the params field naming it.
ASSET_KINDS = {"cycle_diagram": ("cycle", "cycle_id"), "classification_key": ("classification_key", "key_id")}
# A library kind's params name its diagram again; it must be the request's `ref` (VSL-22).
LIBRARY_PARAM_REFS = {"microscope_view": "diagram_id", "diagram_comparison": "left_ref"}
SHELL_CAPACITIES = "chem.shell_capacities"
TIMES = "\u00d7"  # the multiplication sign before a microscope scale magnification
SUBJECT_CODES = {"mathematics": "mth", "physics": "phy", "chemistry": "chm", "biology": "bio", "accounting": "acc"}


class VisualError(Exception):
    def __init__(self, code: str, detail: str = "", hint: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.hint = hint  # what would be valid instead, for the model's one-sentence feedback (VSL-17)

    @property
    def stage(self) -> int:
        return STAGE_OF.get(self.code, 7)

    @property
    def retryable(self) -> bool:
        return self.stage in RETRYABLE_STAGES


@dataclass
class VisualContext:
    """What a visual may draw on in this session."""

    subject: str
    level: str
    stage: str = "S1"
    check_values: dict[str, list[Fraction]] = field(default_factory=dict)  # chk_ id -> values in its result
    check_claims: dict[str, str] = field(default_factory=dict)  # chk_ id -> the checked expression, canonical
    knowns: dict[str, Fraction] = field(default_factory=dict)  # kn_ id -> value
    pack_values: dict[str, Fraction] = field(default_factory=dict)  # pack:<id>/<table>/<key> -> value
    # ref -> a diagram library entry (VSL-22) with its sanitised `svg`, or {approved, licence, parts} for a reference
    diagrams: dict[str, dict[str, Any]] = field(default_factory=dict)
    # ref -> a template preset (VSL-29): {kind, params, pack_id, approved, licence}
    presets: dict[str, dict[str, Any]] = field(default_factory=dict)
    conventions: dict[str, str] = field(default_factory=dict)  # pack convention key -> value (sign conventions, g)
    # id -> a pack science asset (content schema 1.6 `science_assets`: cycles, keys, spectra), with its pack_id
    science_assets: dict[str, dict[str, Any]] = field(default_factory=dict)
    board_steps: frozenset[str] | None = None  # the session board's step ids, when a board exists (VSL-20)
    locale: str = "en"
    default_visual: dict[str, Any] | None = None  # the topic's default visual request
    max_request_bytes: int = MAX_REQUEST_BYTES  # visual_request_max_kb (VSL-09)
    max_resolved_bytes: int = MAX_RESOLVED_BYTES  # visual_resolved_max_kb (VSL-09)
    relative_tolerance: float = 1e-9  # visual_relative_tolerance (VSL-12)


@dataclass
class VisualResult:
    status: str  # ok, repaired, fallback, text_only
    resolved: dict[str, Any] | None
    error: str | None = None
    repairs: list[str] = field(default_factory=list)
    alt: str = ""
    feedback: str | None = None  # the one sentence for the model when it may correct the request (VSL-17)
    retryable: bool = False


def kinds() -> dict[str, dict[str, Any]]:
    return {k["kind"]: k for k in VISUAL_CATALOGUE["kinds"]}


def advertised_kinds(subject: str, level: str, stage: str) -> list[str]:
    """The kinds a Coach may name for this session (VSL-02 whitelist when advertising)."""
    limit = STAGES.index(stage)
    return sorted(
        k["kind"]
        for k in VISUAL_CATALOGUE["kinds"]
        if subject in k["subjects"]
        and level in k["levels"]
        and STAGES.index(k["stage"]) <= limit
        and k["kind"] not in UNAVAILABLE_KINDS
    )


def resolved_of(record: Any) -> dict[str, Any] | None:
    """A pack record's figure as the pack build resolved and checked it (content `visual.resolved`), or None."""
    visual = record.get("visual") if isinstance(record, dict) else None
    resolved = visual.get("resolved") if isinstance(visual, dict) else None
    return resolved if isinstance(resolved, dict) and resolved.get("visual_id") else None


def strip_markup(text: str) -> tuple[str, bool]:
    """Drawing markup in model text is shown as text with the markup removed (VSL-01)."""
    cleaned, n = MARKUP.subn("", text)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip(), n > 0


# --- the validator pipeline (TABLE VSL-15a) -------------------------------------------------------------------------


def validate(
    request: str | dict[str, Any],
    ctx: VisualContext,
    resolver: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Stages 1 to 9. Returns (resolved spec, repairs) or raises `VisualError` with the first fatal code."""
    repairs: list[str] = []
    spec = _parse(request, repairs)  # 1
    _envelope(spec)  # 2
    kind = _kind(spec, ctx)  # 3
    spec = _params(spec, kind, repairs, ctx)  # 4
    entry = _library_entry(spec, ctx)  # 5
    parts = _ref_and_parts(spec, ctx, entry)
    spec = _steps(spec, parts, repairs, ctx)  # 6
    computed: dict[str, Any] = {}
    if resolver is not None:  # 7
        try:
            computed = resolver(spec)
        except TimeoutError as error:
            raise VisualError("visual_resolution_timeout", str(error)) from None
        except Exception as error:  # a tool failed: never a crash, always a code
            raise VisualError("visual_resolution_failed", type(error).__name__) from None
    try:
        computed = {**computed, **_local(spec, ctx, entry, computed)}
    except (KeyError, ValueError) as error:
        raise VisualError("visual_resolution_failed", str(error)[:80]) from None
    view = View(spec["type"], spec["params"], computed, spec.get("steps", []), entry, ctx.locale)
    final = parts_of(view)
    spec = _final_parts(spec, final, repairs, entry)
    _numbers(spec, ctx, computed, repairs)  # 8
    resolved = _resolve(spec, final, computed, repairs, ctx, entry)
    if len(json.dumps(resolved).encode()) > ctx.max_resolved_bytes:  # 9
        raise VisualError("visual_too_large", "resolved spec")
    return resolved, repairs


def _parse(request: str | dict[str, Any], repairs: list[str]) -> dict[str, Any]:
    if isinstance(request, dict):
        copied: dict[str, Any] = json.loads(json.dumps(request))
        return copied
    text = request
    if len(text.encode()) > MAX_REQUEST_BYTES:
        raise VisualError("visual_too_large", "request")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        fixed = re.sub(r",\s*([}\]])", r"\1", text)
        try:
            value = json.loads(fixed)
        except json.JSONDecodeError as error:
            raise VisualError("visual_unparseable", error.msg) from None
        repairs.append("parse:trailing_comma")
    if not isinstance(value, dict):
        raise VisualError("visual_unparseable", "not an object")
    return value


def _envelope(spec: dict[str, Any]) -> None:
    engine = [f for f in ENGINE_FIELDS if f in spec]
    if engine:
        raise VisualError("visual_engine_field", engine[0])
    unknown = [f for f in spec if f not in REQUEST_FIELDS]
    if unknown:
        raise VisualError("visual_unknown_field", unknown[0])
    freeform = _freeform(spec.get("params", {}))
    if freeform:
        raise VisualError("visual_unknown_field", freeform)
    if len(json.dumps(spec).encode()) > MAX_REQUEST_BYTES:
        raise VisualError("visual_too_large", "request")
    if spec.get("type") not in kinds():
        raise VisualError("visual_kind_unknown", str(spec.get("type"))[:40])
    # Errors inside params belong to stage 4 (the kind's params schema), not to the envelope.
    problems = [p for p in contract_errors(ENVELOPE, spec) if not p.startswith("$.params")]
    if problems:
        raise VisualError("visual_envelope_invalid", problems[0])


def _freeform(node: Any, path: str = "params") -> str | None:
    if isinstance(node, dict):
        for key, value in node.items():
            # `d` is SVG path data only as text: function_transform_sim's `parameters.d` is a slider object (VSL-46).
            if key in FREEFORM and not (key == "d" and isinstance(value, dict)):
                return f"{path}.{key}"
            found = _freeform(value, f"{path}.{key}")
            if found:
                return found
    if isinstance(node, list):
        for n, value in enumerate(node):
            found = _freeform(value, f"{path}[{n}]")
            if found:
                return found
    return None


def _kind(spec: dict[str, Any], ctx: VisualContext) -> dict[str, Any]:
    kind = kinds().get(spec["type"])
    if kind is None:
        raise VisualError("visual_kind_unknown", spec["type"])
    allowed = advertised_kinds(ctx.subject, ctx.level, ctx.stage)
    if spec["type"] not in allowed:
        raise VisualError("visual_kind_not_enabled", spec["type"], hint=", ".join(allowed[:10]))
    return kind


def _params(spec: dict[str, Any], kind: dict[str, Any], repairs: list[str], ctx: VisualContext) -> dict[str, Any]:
    """Stage 4: a template preset's values under the request's own (field by field, VSL-29), unit repairs, then the
    kind's params schema: the specification's where it has one, else the registered renderer's (VSL-09); and every
    element a step adds must be one the kind's schema lists (VSL-10)."""
    params = spec.get("params", {})
    ref = spec.get("ref")
    if ref is not None and spec["type"] not in LIBRARY_KINDS:
        preset = ctx.presets.get(ref)
        if preset is None or preset.get("kind") != spec["type"]:
            ours = sorted(r for r, p in ctx.presets.items() if p.get("kind") == spec["type"])
            raise VisualError("visual_ref_missing", ref, hint=", ".join(ours[:6]))
        params = {**json.loads(json.dumps(preset.get("params", {}))), **(params if isinstance(params, dict) else {})}
    params = _scenario_start(spec["type"], params)
    params = _repair_units(params, repairs)
    params = _repair_numbers(spec["type"], params, repairs)
    spec = {**spec, "params": params}
    problems = kind_param_errors(spec["type"], params)
    if problems:
        raise VisualError("visual_param_invalid", problems[0])
    named = LIBRARY_PARAM_REFS.get(spec["type"])
    if named is not None and params.get(named) != ref:
        raise VisualError("visual_param_invalid", f"params.{named} must be the visual's ref", hint=str(ref))
    allowed = add_elements(spec["type"])
    for step in spec.get("steps", []):
        for add in step.get("add", []):
            if add.get("element") not in allowed:
                raise VisualError("visual_param_invalid", f"{add.get('element')} cannot be added to a {spec['type']}",
                                  hint=", ".join(allowed))  # fmt: skip
    return spec


def _scenario_start(kind: str, params: Any) -> Any:
    """A predict, observe, explain scenario (VSL-47) sets the starting values its usual-guess case depends on: they
    replace the request's own, so a model can name the scenario but never change what the student predicts about."""
    if not isinstance(params, dict) or "predict" not in params:
        return params
    scenario = SIM_SCENARIOS.get(str(params["predict"]))
    if scenario is None or scenario["kind"] != kind:
        ours = sorted(i for i, s in SIM_SCENARIOS.items() if s["kind"] == kind)
        raise VisualError("visual_param_invalid", f"no {kind} scenario {params['predict']}", hint=", ".join(ours))
    return {**params, **json.loads(json.dumps(scenario["start"]))}


def scenarios_for(misconception_id: str) -> list[dict[str, Any]]:
    """The predict scenarios built around a misconception's usual guess (VSL-47), for the Coach's note."""
    return [s for s in SIM_SCENARIOS.values() if s["misconception_id"] == misconception_id]


@cache
def _renderer_validator(kind: str) -> Draft202012Validator:
    from agent_core.contracts._validation import _FORMATS, _registry

    return Draft202012Validator(KIND_SCHEMAS[kind], registry=_registry(), format_checker=_FORMATS)


def kind_param_errors(kind: str, params: Any) -> list[str]:
    """The kind's params against its schema (VSL-15 stage 4); [] for a kind that has none."""
    schema_name = f"visuals/kinds/{kind}"
    if f"https://jana.dev/contracts/{schema_name}" in schema_ids() or schema_name in schema_ids():
        return contract_errors(schema_name, params)
    if kind in KIND_SCHEMAS:
        return sorted(f"{e.json_path}: {e.message}" for e in _renderer_validator(kind).iter_errors(params))
    return []


def _kind_validator(kind: str) -> Any:
    schema_name = f"visuals/kinds/{kind}"
    if f"https://jana.dev/contracts/{schema_name}" in schema_ids() or schema_name in schema_ids():
        from agent_core.contracts._validation import validator

        return validator(schema_name)
    return _renderer_validator(kind) if kind in KIND_SCHEMAS else None


def _repair_numbers(kind: str, params: Any, repairs: list[str]) -> Any:
    """A JSON number where the kind takes an exact number as a string (`"t": 0` for `"t": "0"`) is the same value
    written another way: it becomes its string when that string fits the field's pattern. Models write numbers; the
    kinds keep exact values as strings. Only a number changes, so anything else that does not fit is still refused."""
    check = _kind_validator(kind)
    if check is None or not isinstance(params, dict):
        return params
    for _ in range(64):  # one field per pass: each repair can only remove an error
        for error in check.iter_errors(params):
            value = error.instance
            if (error.validator != "type" or error.validator_value != "string" or isinstance(value, bool)
                    or not isinstance(value, int | float) or "pattern" not in error.schema):  # fmt: skip
                continue
            text = _exact(value)
            if text is None or re.search(str(error.schema["pattern"]), text) is None:
                continue
            node = params
            *parents, last = list(error.absolute_path)
            for key in parents:
                node = node[key]
            node[last] = text
            repairs.append(f"number:{error.json_path}->{text}")
            break
        else:
            return params
    return params


def _exact(value: int | float) -> str | None:
    """A JSON number as an exact decimal string ("20" for 20.0, "2.5" for 2.5), or None when it is not finite."""
    if isinstance(value, int):
        return str(value)
    if value != value or value in (float("inf"), float("-inf")):
        return None
    text = format(Decimal(repr(value)).normalize(), "f")
    return text[:-2] if text.endswith(".0") else text


def _repair_units(node: Any, repairs: list[str]) -> Any:
    if isinstance(node, dict):
        return {k: _repair_units(v, repairs) for k, v in node.items()}
    if isinstance(node, list):
        return [_repair_units(v, repairs) for v in node]
    if isinstance(node, str):
        match = re.fullmatch(r"(-?[0-9.]+)\s*([A-Za-z]+)", node.strip())
        if match and match.group(2).lower() in UNIT_SPELLINGS:
            repaired = f"{match.group(1)} {UNIT_SPELLINGS[match.group(2).lower()]}"
            repairs.append(f"unit:{node}->{repaired}")
            return repaired
    return node


def _approved(entry: dict[str, Any]) -> bool:
    """In an installed pack after the automated checks (D65); a reference may still say it is not approved, and an
    entry may be a draft or retired."""
    if "approved" in entry:
        return bool(entry["approved"])
    return str((entry.get("review") or {}).get("status", "approved")) not in ("draft", "retired")


def _library_entry(spec: dict[str, Any], ctx: VisualContext) -> dict[str, Any] | None:
    """Stage 5 for library kinds: the entry the ref names, approved and licensed (both entries of a comparison, a pair
    the library declares); for a cycle or a key, the pack's science asset its params name."""
    if spec["type"] in ASSET_KINDS:
        return _science_asset(spec, ctx)
    if spec["type"] == "diagram_comparison":
        left = _diagram(str(spec.get("ref")), ctx)
        right = _diagram(str(spec["params"].get("right_ref")), ctx)
        if comparison_differences(left, right) is None:
            declared = [str(c.get("id")) for c in left.get("compare_with", [])]
            raise VisualError("visual_param_invalid", f"{left.get('id')} is not paired with {right.get('id')}",
                              hint=", ".join(declared))  # fmt: skip
        return left
    if spec["type"] not in LIBRARY_KINDS:
        if spec.get("ref") is not None:
            preset = ctx.presets[spec["ref"]]
            if not _approved(preset) or not preset.get("licence", True):
                raise VisualError("visual_ref_unapproved", spec["ref"])
        return None
    return _diagram(str(spec.get("ref")), ctx)


def _diagram(ref: str, ctx: VisualContext) -> dict[str, Any]:
    diagram = ctx.diagrams.get(ref)
    if diagram is None:
        raise VisualError("visual_ref_missing", ref, hint=", ".join(sorted(ctx.diagrams)[:6]))
    if not _approved(diagram) or not diagram.get("licence"):
        raise VisualError("visual_ref_unapproved", ref)
    return diagram


def comparison_differences(left: dict[str, Any], right: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The differences the library declares between two diagrams, as left and right part ids (either entry may
    declare the pair); None when neither does."""
    for this, other, here, there in ((left, right, "left", "right"), (right, left, "right", "left")):
        for declared in this.get("compare_with", []):
            if declared.get("id") == other.get("id"):
                return [
                    {
                        here: f"{here}.{d['this_part']}",
                        there: f"{there}.{d['other_part']}" if d.get("other_part") else None,
                    }
                    for d in declared.get("differences", [])
                ]
    return None


def _science_asset(spec: dict[str, Any], ctx: VisualContext) -> dict[str, Any]:
    """Stage 5 for a pack-backed cycle or key (VSL-46): the asset exists, is of the right kind and is licensed."""
    asset_kind, field_name = ASSET_KINDS[spec["type"]]
    ref = str(spec["params"].get(field_name))
    asset = ctx.science_assets.get(ref)
    if asset is None or asset.get("kind") != asset_kind:
        ours = sorted(i for i, a in ctx.science_assets.items() if a.get("kind") == asset_kind)
        raise VisualError("visual_ref_missing", ref, hint=", ".join(ours[:6]))
    if not _approved(asset) or not asset.get("licence"):
        raise VisualError("visual_ref_unapproved", ref)
    return asset


def _ref_and_parts(spec: dict[str, Any], ctx: VisualContext, entry: dict[str, Any] | None = None) -> list[str]:
    """The parts a request can name before resolution."""
    if entry is None and spec["type"] in LIBRARY_KINDS:
        entry = _library_entry(spec, ctx)
    view = View(spec["type"], spec.get("params", {}), {}, spec.get("steps", []), entry, ctx.locale)
    return [p.id for p in parts_of(view)]


def _steps(
    spec: dict[str, Any], parts: list[str], repairs: list[str], ctx: VisualContext | None = None
) -> dict[str, Any]:
    """Stage 6: unknown part ids are dropped from a step (logged); a spec whose every id is unknown is fatal. Parts a
    kind derives from its resolution are checked again once it is resolved; a board binding names a board step."""
    named = [p for step in spec.get("steps", []) for key in ("highlight", "reveal", "hide") for p in step.get(key, [])]
    provisional = spec["type"] in DYNAMIC_PARTS
    hint = ", ".join(parts[:8])
    if named and parts and not any(p in parts for p in named) and not provisional:
        raise VisualError("visual_part_unknown", "no step names a part of this visual", hint=hint)
    if named and not parts and not provisional:
        raise VisualError("visual_part_unknown", "this visual has no parts to name")
    board = ctx.board_steps if ctx is not None else None
    steps = []
    for step in spec.get("steps", []):
        kept = dict(step)
        for key in ("highlight", "reveal", "hide"):
            if key in step:
                good = [p for p in step[key] if p in parts or provisional]
                for dropped in [p for p in step[key] if p not in good]:
                    repairs.append(f"part:{dropped} dropped")
                kept[key] = good
        if "board_step" in step and board is not None and step["board_step"] not in board:
            repairs.append(f"board_step:{step['board_step']} unbound")
            del kept["board_step"]
        steps.append(kept)
    return {**spec, "steps": steps}


def _final_parts(
    spec: dict[str, Any], parts: list[Part], repairs: list[str], entry: dict[str, Any] | None
) -> dict[str, Any]:
    """Once resolved: ids still unknown are dropped (all unknown is fatal); `reveal` names only parts the entry hides
    at first and `hide` never names an essential part (VSL-10)."""
    ids = {p.id for p in parts}
    declared = [p for p in (entry or {}).get("parts", []) if isinstance(p, dict)]
    hidden = {p["id"] for p in declared if p.get("initially_hidden")}
    essential = {p["id"] for p in declared if p.get("essential")}
    steps, named, unknown = [], 0, 0
    for step in spec.get("steps", []):
        kept = dict(step)
        for key in ("highlight", "reveal", "hide"):
            if key not in step:
                continue
            good = []
            for p in step[key]:
                named += 1
                if p not in ids:
                    unknown += 1
                    if f"part:{p} dropped" not in repairs:
                        repairs.append(f"part:{p} dropped")
                elif key == "reveal" and declared and p not in hidden:
                    repairs.append(f"reveal:{p} is shown already")
                elif key == "hide" and p in essential:
                    repairs.append(f"hide:{p} is essential")
                else:
                    good.append(p)
            kept[key] = good
        steps.append(kept)
    if named and unknown == named and spec["type"] in DYNAMIC_PARTS:
        raise VisualError("visual_part_unknown", "no step names a part of this visual", hint=", ".join(sorted(ids)[:8]))
    return {**spec, "steps": steps}


# --- local resolution: library entries and pack reference data (VSL-13, N3) -----------------------------------------


LIBRARY_KEEP = ("id", "version", "title", "parts", "sequences", "regions", "alt_text", "licence", "attribution",
                "scale")  # fmt: skip


def _local(
    spec: dict[str, Any], ctx: VisualContext, entry: dict[str, Any] | None, computed: dict[str, Any] | None = None
) -> dict[str, Any]:
    if spec["type"] in ASSET_KINDS and entry is not None:
        return _asset_definition(spec, entry)
    if entry is not None and "svg" in entry:
        out: dict[str, Any] = {"svg": entry["svg"], "entry": {k: entry[k] for k in LIBRARY_KEEP if k in entry}}
        if spec["type"] == "reaction_mechanism":
            chemistry_kinds.mechanism_check(spec["params"], entry)
        sequence = spec["params"].get("sequence")
        if sequence is not None and not any(s.get("id") == sequence for s in entry.get("sequences", [])):
            raise ValueError(f"{entry.get('id')} has no sequence {sequence}")
        if spec["type"] == "diagram_comparison":
            right = _diagram(str(spec["params"]["right_ref"]), ctx)
            out["right"] = {"svg": right["svg"], "entry": {k: right[k] for k in LIBRARY_KEEP if k in right}}
            out["differences"] = comparison_differences(entry, right) or []
        if spec["type"] == "microscope_view":
            out.update(_microscope_scale(spec, entry, computed or {}))
        return out
    if spec["type"] == "electron_shells":
        return _shells(spec["params"], ctx)
    if spec["type"] == "orbital_boxes":
        return chemistry_kinds.orbital_boxes(spec["params"], lambda key: _pack_value(ctx, key))
    if spec["type"] == "spectrum_chart":
        return chemistry_kinds.spectrum(spec["params"], ctx.science_assets)
    if spec["type"] == "periodic_table" and spec["params"].get("show_relative_mass"):
        highlight = spec["params"].get("highlight") or {}
        labels = {}
        for symbol in highlight.get("values", []) if highlight.get("kind") == "elements" else []:
            ref, value = _pack_value(ctx, f"chem.ar_{symbol.lower()}")
            labels[f"el_{symbol}"] = _plain(value)
            spec.setdefault("numbers_from", [])
            if ref not in spec["numbers_from"]:
                spec["numbers_from"].append(ref)
        return {"labels": labels} if labels else {}
    return {}


def _pack_value(ctx: VisualContext, key: str) -> tuple[str, Fraction]:
    for ref, value in sorted(ctx.pack_values.items()):
        if ref.endswith(f"/conventions/{key}"):
            return ref, value
    raise KeyError(f"no pack value {key}")


def _plain(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else str(float(value))


def _shells(params: dict[str, Any], ctx: VisualContext) -> dict[str, Any]:
    """Electron arrangements from the pack's proton numbers and shell capacities (N3): no tool computes them."""

    def arrangement(symbol: str, charge: int) -> tuple[list[int], list[str]]:
        ref, z = _pack_value(ctx, f"chem.z_{symbol.lower()}")
        capacities_ref = next((r for r in ctx.pack_values if r.endswith(f"/conventions/{SHELL_CAPACITIES}")), None)
        capacities = [2, 8, 8, 2]
        electrons = int(z) - charge
        if not 0 < electrons <= sum(capacities):
            raise ValueError(f"{symbol}: {electrons} electrons is outside the shell model")
        shells = []
        for capacity in capacities:
            if electrons <= 0:
                break
            shells.append(min(capacity, electrons))
            electrons -= capacity
        return shells, [ref] + ([capacities_ref] if capacities_ref else [])

    shells, refs = arrangement(params["element"], int(params.get("charge", 0)))
    out: dict[str, Any] = {
        "shells": shells,
        "pack_refs": refs,
        "labels": {f"shell_{n}": str(c) for n, c in enumerate(shells, 1)},
    }
    if params.get("partner"):
        partner, more = arrangement(params["partner"], 0)
        out["partner"] = {"element": params["partner"], "shells": partner}
        out["pack_refs"] = list(dict.fromkeys(refs + more))
    return out


# --- pack-backed Biology definitions and the microscope's scale (VSL-05, VSL-46, N0 and N3) -------------------------


def _asset_definition(spec: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
    """A cycle or key from the pack, checked as the resolution draws it: its links resolve, a cycle's stages all lie on
    the cycle from the start, and a key is a tree from its start whose every question has two different branches."""
    definition = asset.get("definition") or {}
    if spec["type"] == "cycle_diagram":
        _check_cycle(definition, spec["params"].get("start_stage"))
    else:
        _check_key(definition, spec["params"].get("start_node"))
    keep = ("id", "version", "title", "licence", "attribution")
    return {"definition": definition, "asset": {k: asset[k] for k in keep if k in asset}}


def _check_cycle(definition: dict[str, Any], start: str | None) -> None:
    stages = [str(s["id"]) for s in definition.get("stages", [])]
    if len(set(stages)) != len(stages):
        raise ValueError("a cycle stage is listed twice")
    after: dict[str, list[str]] = {s: [] for s in stages}
    for link in definition.get("links", []):
        if link["from"] not in after or link["to"] not in after:
            raise ValueError(f"cycle link {link['from']} -> {link['to']} names no stage")
        after[link["from"]].append(link["to"])
    if start is not None and start not in after:
        raise ValueError(f"the cycle has no stage {start}")
    if not stages:
        raise ValueError("the cycle has no stages")
    first = start or stages[0]
    reached, todo = {first}, [first]
    while todo:
        for nxt in after[todo.pop()]:
            if nxt not in reached:
                reached.add(nxt)
                todo.append(nxt)
    if reached != set(stages) or not any(first in after[s] for s in stages):
        raise ValueError("the stages do not form one cycle from the start")


def _check_key(definition: dict[str, Any], start: str | None) -> None:
    nodes = {str(n["id"]): n for n in definition.get("nodes", [])}
    groups = {str(t["id"]) for t in definition.get("terminals", [])}
    if len(nodes) != len(definition.get("nodes", [])) or nodes.keys() & groups:
        raise ValueError("a key node or group id is used twice")
    root = str(start or definition.get("root"))
    if root not in nodes:
        raise ValueError(f"the key has no question {root}")
    seen: set[str] = set()
    todo = [root]
    while todo:
        node = nodes[todo.pop()]
        if node["yes"] == node["no"]:
            raise ValueError(f"question {node['id']} has one branch twice")
        for branch in (node["yes"], node["no"]):
            if branch not in nodes and branch not in groups:
                raise ValueError(f"question {node['id']} leads to {branch}, which the key does not have")
            if branch in seen or branch == root:
                raise ValueError(f"{branch} is reached by two paths")
            seen.add(branch)
            if branch in nodes:
                todo.append(branch)


def _microscope_scale(spec: dict[str, Any], entry: dict[str, Any], computed: dict[str, Any]) -> dict[str, Any]:
    """The scale bar from the library's calibrated metadata (N3): its length per 100 drawing pixels. Lengths taken from
    the pack must give the entry's own magnification; a question's traced givens may describe another drawing."""
    scale = entry.get("scale")
    if not scale:
        raise ValueError(f"{entry.get('id')} has no calibrated scale")
    params = spec["params"]
    stated = str(scale["magnification"]).lstrip(TIMES)
    if str(params.get("lengths_from", "")).startswith("pack:") and computed.get("magnification") is not None:
        if Fraction(stated) != Fraction(str(computed["magnification"])):
            raise ValueError(f"the lengths give {computed['magnification']} times, not the entry's {stated}")
    labels = dict(computed.get("labels") or {})
    if params.get("show_scale", True):
        labels["scale_bar"] = str(scale["length_per_100px"])
    return {"scale_bar": {"length": str(scale["length_per_100px"]), "drawing_px": 100}, "labels": labels}


# --- claim numbers (VSL-12) -----------------------------------------------------------------------------------------


def _claim_numbers(spec: dict[str, Any]) -> list[tuple[str, str]]:
    """(written number, where) for every claim number a student could read; presentation numbers are exempt."""
    params = spec.get("params", {})
    found = claim_numbers(str(spec.get("type")), params if isinstance(params, dict) else {})
    for text_key in ("caption",):
        for match in re.finditer(r"-?[0-9]+(?:\.[0-9]+)?", spec.get(text_key, "")):
            found.append((match.group(0), text_key))
    return found


def _preset_values(spec: dict[str, Any], ctx: VisualContext) -> list[Fraction]:
    """A preset's own values are signed pack data (N3): the numbers it sets trace to the preset record."""
    preset = ctx.presets.get(str(spec.get("ref"))) if spec["type"] not in LIBRARY_KINDS else None
    if preset is None:
        return []
    values = []
    for written, _ in claim_numbers(spec["type"], preset.get("params", {})):
        try:
            values.append(Fraction(written))
        except (ValueError, ZeroDivisionError):
            continue
    return values


def _numbers(spec: dict[str, Any], ctx: VisualContext, computed: dict[str, Any],
             repairs: list[str] | None = None) -> None:  # fmt: skip
    """VSL-12: every claim number equals a referenced value at its shown precision; claims bind to their check. A
    number that equals none of the cited values but is one of the problem's own knowns (the statement's givens) is
    traced to that known: it is added to `numbers_from` and the repair recorded (the model left a citation out)."""
    sources: list[Fraction] = _preset_values(spec, ctx)
    for ref in [*spec.get("numbers_from", []), *_param_sources(spec)]:
        if ref.startswith("chk_"):
            sources += ctx.check_values.get(ref, [])
        elif ref.startswith("kn_") and ref in ctx.knowns:
            sources.append(ctx.knowns[ref])
        elif ref.startswith("pack:") and ref in ctx.pack_values:
            sources.append(ctx.pack_values[ref])
    for written, where in _claim_numbers(spec):
        try:
            traced = any(_shown_equal(written, value, ctx.relative_tolerance) for value in sources)
        except ArithmeticError:
            traced = False
        if not traced:
            known = next((ref for ref, value in ctx.knowns.items() if _given_equal(written, value, ctx)), None)
            if known is None:
                raise VisualError("visual_untraced_number", f"{where} = {written}", hint=written)
            spec["numbers_from"] = [*spec.get("numbers_from", []), known]
            sources.append(ctx.knowns[known])
            if repairs is not None:
                repairs.append(f"numbers_from:+{known}")
    caption = spec.get("caption", "")
    if "=" in caption:
        claim = _canonical(caption)
        cited = [ctx.check_claims.get(r) for r in spec.get("numbers_from", []) if r.startswith("chk_")]
        if not any(c is not None and c == claim for c in cited):
            raise VisualError("visual_untraced_number", f"caption claim {caption[:60]!r} differs from its check")


def _given_equal(written: str, value: Fraction, ctx: VisualContext) -> bool:
    try:
        return _shown_equal(written, value, ctx.relative_tolerance)
    except ArithmeticError:
        return False


def _param_sources(spec: dict[str, Any]) -> list[str]:
    """A source a kind's params cite for their own numbers (a microscope question's `lengths_from`, VSL-46)."""
    params = spec.get("params")
    cited = params.get("lengths_from") if isinstance(params, dict) else None
    return [cited] if isinstance(cited, str) else []


def _shown_equal(written: str, value: Fraction, tolerance: float = 1e-9) -> bool:
    """Exact within 1e-9 relative, or equal at the precision the label shows (significant figures as displayed)."""
    if "/" in written:
        return Fraction(written) == value
    shown = Decimal(written)
    exact = Decimal(value.numerator) / Decimal(value.denominator)
    if abs(shown - exact) <= Decimal(str(tolerance)) * max(abs(exact), Decimal(1)):
        return True
    exponent = shown.as_tuple().exponent
    decimals = -exponent if isinstance(exponent, int) else 0
    return exact.quantize(Decimal(1).scaleb(-max(decimals, 0)), rounding=ROUND_HALF_UP) == shown


def _canonical(claim: str) -> str:
    text = claim.replace("\u2212", "-").replace("\u00d7", "*").replace(" ", "")
    return text.lower()


def _resolve(
    spec: dict[str, Any],
    parts: list[Part],
    computed: dict[str, Any],
    repairs: list[str],
    ctx: VisualContext,
    entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The resolved spec: deterministic for the same request and sources (VSL-13)."""
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    visual_id = "vis_" + "".join(CROCKFORD[int(digest[i : i + 2], 16) % 32] for i in range(0, 52, 2))
    steps = spec.get("steps", [])
    if entry is not None and not steps:
        steps = sequence_steps(View(spec["type"], spec.get("params", {}), computed, [], entry, ctx.locale))
    view = View(spec["type"], spec.get("params", {}), computed, steps, entry, ctx.locale)
    preset_ref = []
    if spec.get("ref") is not None and spec["type"] not in LIBRARY_KINDS:
        preset = ctx.presets.get(spec["ref"], {})
        preset_ref = [f"pack:{preset.get('pack_id', 'pack.unknown')}/visual_presets/{spec['ref']}"]
    sources = [
        *spec.get("numbers_from", []),
        *_param_sources(spec),
        *computed.get("check_ids", []),
        *computed.get("pack_refs", []),
        *preset_ref,
    ]
    resolved = {
        "spec_version": 1,
        "type": spec["type"],
        "params": spec.get("params", {}),
        # The resolution tools' check ids join the request's sources (VSL-13), with pack references for N3 data.
        "numbers_from": list(dict.fromkeys(sources))[:40],
        "steps": steps,
        "labels": spec.get("labels", "shown"),
        "visual_id": visual_id,
        "status": "repaired" if repairs else "ok",
        "fallback_reason": None,
        "parts": [p.record() for p in parts][:400],
        "alt": alt_of(view, parts),
        "computed": {k: v for k, v in computed.items() if k != "pack_refs"},
        "repairs": repairs[:20],
        "resolution_hash": f"sha256:{digest}",
    }
    if spec.get("presentation") == "build":
        # Built layer by layer in the order the kind declares (VSL-41); a kind with none shows whole.
        resolved["presentation"] = "build"
        resolved["layers"] = layers_of(view, parts)
        if not steps and resolved["layers"]:  # each build step's alt line names what it added (VSL-14)
            resolved["alt"] = {**resolved["alt"], "steps": build_alt(resolved["layers"], parts, ctx.locale)}
    if "ref" in spec:
        resolved["ref"] = spec["ref"]
    if "caption" in spec:
        resolved["caption"] = spec["caption"]
    return resolved


def alt_text(spec: dict[str, Any]) -> str:
    """A text alternative built from the request's valid parts (the text-only rung of VSL-16)."""
    params = spec.get("params", {}) if isinstance(spec.get("params"), dict) else {}
    kind = str(spec.get("type", "visual")).replace("_", " ")
    bits = [f"A {kind}"]
    if params.get("object"):
        bits.append(f"of a {params['object']}")
    forces = [
        f"{f.get('name', f.get('id'))} {f.get('magnitude', '')}".strip()
        for f in params.get("forces", [])
        if isinstance(f, dict)
    ]
    if forces:
        bits.append("with forces: " + ", ".join(forces))
    return " ".join(bits) + "."


def feedback(error: VisualError) -> str:
    """One sentence telling the model what was wrong and how to fix it, and that nothing was shown (VSL-17)."""
    detail = error.detail.split(":")[0] if error.code == "visual_part_unknown" else error.detail
    fixes = {
        "visual_part_unknown": f"a step names parts this visual does not have; use one of {error.hint}",
        "visual_kind_not_enabled": f"{detail} is not available in this session; use one of {error.hint}",
        "visual_kind_unknown": f"{detail} is not a catalogue kind",
        "visual_ref_missing": f"{detail} is not installed" + (f"; use one of {error.hint}" if error.hint else ""),
        "visual_ref_unapproved": f"{detail} cannot be shown yet",
        "visual_untraced_number": f"{error.hint or detail} does not match any cited check or known; cite the check "
                                  "that computed it or leave the number out",
        "visual_param_invalid": f"the params do not fit the kind ({detail[:120]})"
                                + (f"; allowed elements: {error.hint}" if error.hint else ""),
        "visual_unknown_field": f"{detail} is not a field of a visual spec",
        "visual_engine_field": f"{detail} is set by the engine, not by you",
    }  # fmt: skip
    reason = fixes.get(error.code, f"the visual was not valid ({error.code})")
    return f"Nothing was shown: {reason}."


@dataclass
class Resolver:
    """Resolution with its cache: the same request resolves to the same bytes with one tool call (VSL-13)."""

    tools: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: int = 0

    def __call__(self, spec: dict[str, Any]) -> dict[str, Any]:
        key = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        if key not in self.cache:
            self.calls += 1
            self.cache[key] = self.tools(spec) if self.tools else {}
        return self.cache[key]


def render(
    request: str | dict[str, Any],
    ctx: VisualContext,
    resolver: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> VisualResult:
    """The fallback ladder (VSL-16): the request, else the topic's default visual, else text only. Never raises."""
    retryable, sentence = False, None
    try:
        resolved, repairs = validate(request, ctx, resolver)
        return VisualResult(resolved["status"], resolved, None, repairs, resolved["alt"]["summary"])
    except VisualError as error:
        code = error.code
        retryable, sentence = error.retryable, feedback(error)
    except Exception:  # anything unexpected is still a fallback, never a crash
        code = "visual_resolution_failed"
    if ctx.default_visual is not None:
        try:
            resolved, repairs = validate(ctx.default_visual, ctx, resolver)
            resolved = {**resolved, "status": "fallback", "fallback_reason": code}
            return VisualResult("fallback", resolved, code, repairs, resolved["alt"]["summary"], sentence, retryable)
        except VisualError:
            pass
    words = alt_text(request) if isinstance(request, dict) else ""
    return VisualResult("text_only", None, code, [], f"{TEXT_ONLY} {words}".strip(), sentence, retryable)
