"""The diagram branch of the authoring pipeline (VSL-34, CUR-19): library entries and template presets.

A library entry's source is `diagrams/<id>.yaml` in the diagram entry format (VSL-22) beside the SVG its `svg_path`
names. Before a pack build accepts it, the drawing is normalised (comments and metadata out, coordinates rounded,
data-part ids kept), sanitised (VSL-23), checked part for part against the entry (VSL-22, CUR-29) and linted (VSL-34:
names and label anchors, the stroke style, token colours, named content colours, label size and overlapping labels);
its licence passes the licence gate (VSL-24, CUR-23). Golden renders in light, dark and 150 % text are the renderer
package's job (VSL-35) and run on every change to a drawing.

A preset's source is `presets/<id>.yaml` (VSL-29): named starting params for a template or graph kind, in the same
`dgm.` namespace. Its params must satisfy the kind's schema, and the build resolves it through the engine's one
validator with the verification tools (build checks `chk_build_…`, CUR-20), so every number it shows is traced and its
pre-resolved spec renders with no model and no network (VSL-36).

Both are carried into the pack only when the bundled content-pack contract can hold them (VSL-22, VSL-29); with an
older contract the build keeps them out of the pack and lists them in its report, after every check has run.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

from agent_core.assets.visuals import VisualContext, VisualError, kinds, validate
from agent_core.config import Settings
from agent_core.contracts import document
from agent_core.contracts import errors as contract_errors
from agent_core.store.svg import lint_problems, normalise, parts_problems, sanitise_problems

ENTRY = "visuals/diagram-entry"
DIAGRAM_REF = "content/content-pack#/$defs/DiagramRef"
PRESET = "content/content-pack#/$defs/VisualPreset"
PRESET_RENDERERS = frozenset({"template", "graph", "board"})
SUBJECTS = {"mth": "mathematics", "phy": "physics", "chm": "chemistry", "bio": "biology", "acc": "accounting"}
NOT_COMMERCIAL = ("-NC", "-ND")


def diagram_svg(pack_dir: Path, entry: dict[str, Any]) -> tuple[str | None, list[str]]:
    """The normalised drawing an entry names, or the reason there is none."""
    path = pack_dir / str(entry.get("svg_path", ""))
    if not path.is_file() or pack_dir.resolve() not in path.resolve().parents:
        return None, [f"{entry.get('id')}: no drawing at {entry.get('svg_path')}"]
    return normalise(path.read_text(encoding="utf-8")), []


def diagram_problems(entry: dict[str, Any], svg: str | None, settings: Settings) -> list[str]:
    """Every check a library entry must pass before a build accepts it."""
    rid = str(entry.get("id"))
    problems = [f"{rid}: {e}" for e in contract_errors(ENTRY, entry)]
    licence = str(entry.get("licence", ""))
    if any(mark in licence.upper() for mark in NOT_COMMERCIAL):
        problems.append(f"{rid}: licence {licence} is non-commercial or no-derivatives and cannot ship (VSL-24)")
    if svg is None or problems:
        return problems
    problems += [f"{rid}: {p}" for p in sanitise_problems(svg, settings.visual_library_svg_max_kb * 1024)]
    if problems:
        return problems
    problems += [f"{rid}: {p}" for p in parts_problems(entry, svg)]
    problems += [f"{rid}: {p}" for p in lint_problems(svg, entry)]
    if len(entry.get("parts", [])) > settings.visual_library_max_parts:
        problems.append(f"{rid}: {len(entry['parts'])} parts (at most {settings.visual_library_max_parts}, VSL-33)")
    return problems


def _strip(value: Any, schema: dict[str, Any], defs: dict[str, Any]) -> Any:
    """`value` with every property the schema node does not declare removed (recursively through objects and arrays)."""
    while isinstance(schema, dict) and "$ref" in schema and str(schema["$ref"]).startswith("#/$defs/"):
        schema = defs.get(str(schema["$ref"]).rsplit("/", 1)[-1], {})
    if isinstance(value, dict) and isinstance(schema, dict) and "properties" in schema:
        props = schema["properties"]
        return {k: _strip(v, props[k], defs) for k, v in value.items() if k in props}
    if isinstance(value, list) and isinstance(schema, dict) and isinstance(schema.get("items"), dict):
        return [_strip(v, schema["items"], defs) for v in value]
    return value


def pack_record(entry: dict[str, Any], svg: str) -> tuple[dict[str, Any] | None, str | None]:
    """The diagrams-table record for an entry under the bundled contract: the entry with its sanitised SVG, less any
    field the contract's DiagramRef does not declare (VSL-22: an entry stripped of its library-only fields is a
    DiagramRef). None, with the reason, when the contract cannot carry a drawing yet."""
    pack = document("content/content-pack")
    ref = pack["$defs"]["DiagramRef"]
    if "svg" not in ref.get("properties", {}):
        return None, "the content-pack contract has no DiagramRef.svg (VSL-22)"
    record = _strip({**entry, "svg": svg}, ref, pack["$defs"])
    problems = contract_errors(DIAGRAM_REF, record)
    if problems:
        return None, f"not a DiagramRef under this contract ({problems[0]})"
    return record, None


Call = Callable[[str, dict[str, Any]], dict[str, Any]]


def preset_context(
    pack: dict[str, Any], conventions: list[dict[str, Any]], presets: dict[str, dict[str, Any]], stage: str = "S2"
) -> VisualContext:
    """The build's visual context: the pack's subject and conventions, its presets, and pack values for N3 data."""
    values: dict[str, Fraction] = {}
    for convention in conventions:
        try:
            values[f"pack:{pack['pack_id']}/conventions/{convention['key']}"] = Fraction(str(convention["value"]))
        except (ValueError, ZeroDivisionError):
            continue
    return VisualContext(
        SUBJECTS.get(str(pack.get("subject")), str(pack.get("subject"))),
        str(pack.get("level", "o")).upper(),
        stage,
        pack_values=values,
        presets={rid: {**preset, "pack_id": pack["pack_id"]} for rid, preset in presets.items()},
        conventions={c["key"]: str(c["value"]) for c in conventions},
    )


def preset_problems(
    preset: dict[str, Any], ctx: VisualContext, resolve: Callable[[dict[str, Any]], dict[str, Any]], settings: Settings
) -> tuple[list[str], dict[str, Any] | None]:
    """A preset's checks (VSL-29): a template or graph kind of this subject, params valid for the kind, and a clean
    resolution through the validator with the verification tools. Returns (problems, the pre-resolved spec)."""
    rid = str(preset.get("id"))
    kind = kinds().get(str(preset.get("kind")))
    if kind is None or kind["renderer"] not in PRESET_RENDERERS:
        return [f"{rid}: {preset.get('kind')} is not a template or graph kind (VSL-29)"], None
    if str(preset.get("subject")) not in {k for k, v in SUBJECTS.items() if v in kind["subjects"]}:
        return [f"{rid}: {preset.get('kind')} is not a kind for {preset.get('subject')}"], None
    licence = str(preset.get("licence", ""))
    if any(mark in licence.upper() for mark in NOT_COMMERCIAL):
        return [f"{rid}: licence {licence} is non-commercial or no-derivatives and cannot ship (VSL-24)"], None
    request = {"spec_version": 1, "type": preset["kind"], "ref": rid, "params": {}}
    try:
        resolved, _ = validate(request, ctx, resolve)
    except VisualError as error:
        return [f"{rid}: does not resolve ({error.code}: {error.detail})"], None
    if len(resolved["parts"]) > settings.visual_template_max_parts:
        return [f"{rid}: {len(resolved['parts'])} parts (at most {settings.visual_template_max_parts}, VSL-33)"], None
    return [], resolved


def preset_record(preset: dict[str, Any], resolved: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    pack = document("content/content-pack")
    if "VisualPreset" not in pack["$defs"] or "visual_presets" not in pack["properties"]:
        return None, "the content-pack contract has no visual_presets table (VSL-29)"
    record = {**preset, **({"resolved": resolved} if resolved is not None else {})}
    record = _strip(record, pack["$defs"]["VisualPreset"], pack["$defs"])
    problems = contract_errors(PRESET, record)
    if problems:
        return None, f"not a VisualPreset under this contract ({problems[0]})"
    return record, None


def build_call(prefix: str) -> Call:
    """The build's tool call for preset resolution: the worker function in process, with a build check id (CUR-20)."""
    from pack_builder.verify import run_check

    counter = {"n": 0}

    def call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        counter["n"] += 1
        check_id = f"chk_build_{prefix}_{counter['n']}"
        check = run_check(check_id, {"tool": tool, "args": json.loads(json.dumps(args))})
        return {"value": check.value, "check": {"check_id": check_id, "function": check.function}}

    return call
