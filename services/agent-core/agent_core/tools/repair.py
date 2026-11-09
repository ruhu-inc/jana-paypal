"""The repair pipeline before validation (TOL-08) and the strikes that stop repeated malformed calls (TOL-09).

Steps, in order: (1) unique call ids `call_<turn>_<n>`, keeping the provider's id; (2) tool names: leaked tags and
prefixes stripped, CamelCase and hyphens to snake_case, `_tool` dropped, fuzzy match to a visible tool at ≥ 0.7 with
no runner-up within 0.05; (3) argument JSON: control characters, code fences and trailing commas removed, brackets
re-balanced, and arguments cut off mid-string refused as `truncated_args`; (4) type coercion only where the schema
wants another type; (5) tool aliases, then schema validation with an error that names the field, the rule and an
accepted example. Every repair is recorded; content that is missing is never guessed."""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from agent_core.config import Settings

UNPARSED = "__unparsed__"  # the router's marker for arguments that were not a JSON object
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
ALIASES: dict[str, dict[str, str]] = {
    "*": {"expr": "expression", "eq": "equations", "equation": "equations", "paramsSmall": "params"},
    "math_check_equivalence": {"expression_a": "a", "expression_b": "b", "first": "a", "second": "b"},
    "math_check_step": {"previous": "prev", "line": "next", "current": "next"},
    "units_convert": {"value": "quantity", "to_unit": "to", "target": "to"},
    "math_solve": {"variables_to_solve": "unknowns", "unknown": "unknowns"},
    "skill": {"name": "skill_name", "skill": "skill_name"},
}


@dataclass(frozen=True)
class RawCall:
    """A tool call as the model emitted it, after normalisation (ids may repeat; names may be wrong)."""

    provider_id: str
    name: str
    arguments: dict[str, Any]
    truncated: bool = False  # the reply hit its length limit while writing this call


@dataclass
class Repaired:
    call_id: str
    provider_id: str
    name_emitted: str
    name: str | None  # None when the name could not be resolved
    arguments: dict[str, Any] | None
    repairs: list[str] = field(default_factory=list)
    error: str | None = None  # a TOL-10 code when repair failed
    detail: str = ""


def rewrite_ids(calls: Sequence[RawCall], turn: int) -> list[str]:
    """Step 1: `call_<turn>_<n>` for every call, in order (providers such as llama.cpp reuse one id)."""
    return [f"call_{turn}_{n}" for n in range(len(calls))]


def _snake(name: str) -> str:
    name = re.sub(r"</?tool_call>|^functions\.|^tools?\.", "", name.strip())
    name = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower().replace("-", "_").replace(" ", "_")
    return re.sub(r"_tool$", "", name)


def resolve_name(name: str, visible: Sequence[str], settings: Settings) -> tuple[str | None, list[str]]:
    """Step 2. Returns (tool name or None, repairs applied)."""
    if name in visible:
        return name, []
    cleaned = _snake(name)
    if cleaned in visible:
        return cleaned, [f"name:{name}->{cleaned}"]
    scored = sorted(((difflib.SequenceMatcher(None, cleaned, v).ratio(), v) for v in visible), reverse=True)
    if not scored or scored[0][0] < settings.tool_name_match_min:
        return None, []
    if len(scored) > 1 and scored[0][0] - scored[1][0] < settings.tool_name_match_margin:
        return None, []  # two candidates equally close: never guessed
    return scored[0][1], [f"name:{name}->{scored[0][1]}(fuzzy {scored[0][0]:.2f})"]


def repair_json(raw: str) -> tuple[dict[str, Any] | None, list[str], str | None]:
    """Step 3. Returns (arguments, repairs, error code). Arguments cut off inside a string are `truncated_args`."""
    repairs = []
    text = raw
    if _CONTROL.search(text):
        text = _CONTROL.sub("", text)
        repairs.append("json:control_characters")
    if text.strip().startswith("```"):
        text = _FENCE.sub("", text.strip())
        repairs.append("json:code_fence")
    if _TRAILING_COMMA.search(text):
        text = _TRAILING_COMMA.sub(r"\1", text)
        repairs.append("json:trailing_comma")
    try:
        value = json.loads(text)
        return (value, repairs, None) if isinstance(value, dict) else (None, repairs, "invalid_args")
    except json.JSONDecodeError:
        pass
    if _inside_string(text):
        return None, repairs, "truncated_args"
    balanced = _balance(text)
    if balanced != text:
        try:
            value = json.loads(balanced)
        except json.JSONDecodeError:
            return None, repairs, "invalid_args"
        if isinstance(value, dict):
            return value, [*repairs, "json:balanced_brackets"], None
    return None, repairs, "invalid_args"


def _inside_string(text: str) -> bool:
    inside = escaped = False
    for ch in text:
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            inside = not inside
    return inside


def _balance(text: str) -> str:
    stack: list[str] = []
    inside = escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
        elif ch == '"':
            inside = not inside
        elif not inside and ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif not inside and ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    return text.rstrip().rstrip(",") + "".join(reversed(stack))


def coerce(arguments: dict[str, Any], schema: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Step 4: only where the schema wants another type. Ambiguous values (`"1,5"`) are kept as they are."""
    properties = schema.get("properties", {})
    out = dict(arguments)
    repairs = []
    for key, value in arguments.items():
        wanted = properties.get(key, {}).get("type")
        types = wanted if isinstance(wanted, list) else [wanted]
        new = _coerce_value(value, types, properties.get(key, {}))
        if new is not _KEEP:
            out[key] = new
            repairs.append(f"coerce:{key}")
    return out, repairs


_KEEP = object()


def _coerce_value(value: Any, types: list[Any], spec: dict[str, Any]) -> Any:
    if any(_is_type(value, t) for t in types if t) or not any(types):
        return _KEEP
    if isinstance(value, str):
        text = value.strip()
        if "integer" in types and re.fullmatch(r"[+-]?\d+", text):
            return int(text)
        if "number" in types and re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", text):
            return float(text) if any(c in text for c in ".eE") else int(text)
        if "boolean" in types and text.lower() in ("true", "false"):
            return text.lower() == "true"
        if ("array" in types and text.startswith("[")) or ("object" in types and text.startswith("{")):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return _KEEP
            return parsed if any(_is_type(parsed, t) for t in types) else _KEEP
    if "array" in types and not isinstance(value, list):
        item_type = spec.get("items", {}).get("type")
        if item_type is None or _is_type(value, item_type):
            return [value]
    return _KEEP


def _is_type(value: Any, kind: Any) -> bool:
    return {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
        "null": value is None,
    }.get(kind, False)


def normalize(tool: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Step 5a: the tool's aliases (`expr` → `expression`)."""
    table = {**ALIASES["*"], **ALIASES.get(tool, {})}
    out: dict[str, Any] = {}
    repairs = []
    for key, value in arguments.items():
        target = table.get(key, key)
        if target != key and target not in arguments:
            repairs.append(f"alias:{key}->{target}")
            out[target] = value
        else:
            out[key] = value
    return out, repairs


def format_validation_error(error: Any, schema: dict[str, Any]) -> str:
    """Field, rule and an accepted example (OpenScience formatValidationError pattern)."""
    where = "/".join(str(p) for p in error.absolute_path) or (
        error.message.split("'")[1] if error.validator == "required" and "'" in error.message else "arguments"
    )
    field_schema = schema.get("properties", {}).get(str(where), {})
    example = field_schema.get("examples", [None])[0] if field_schema.get("examples") else _example(field_schema)
    hint = f"; for example {json.dumps(example)}" if example is not None else ""
    return f"{where}: {error.validator} rule failed ({error.message[:160]}){hint}"


def _example(spec: dict[str, Any]) -> Any:
    if "enum" in spec:
        return spec["enum"][0]
    kind = spec.get("type")
    examples: dict[str, Any] = {"string": "x + 1", "integer": 3, "number": 2.5, "boolean": True, "array": ["x + 1 = 3"]}
    return examples.get(kind) if isinstance(kind, str) else None


def repair_call(
    raw: RawCall, call_id: str, visible: Sequence[str], schemas: dict[str, dict[str, Any]], settings: Settings
) -> Repaired:
    """Steps 2 to 5 for one call."""
    result = Repaired(call_id, raw.provider_id, raw.name, None, None)
    if raw.provider_id and raw.provider_id != call_id:
        result.repairs.append("id:rewritten")
    name, repairs = resolve_name(raw.name, visible, settings)
    result.repairs += repairs
    if name is None:
        result.error, result.detail = "unknown_tool", f"there is no tool named {raw.name[:60]}"
        return result
    result.name = name
    arguments = raw.arguments
    if UNPARSED in arguments:
        parsed, repairs, error = repair_json(str(arguments[UNPARSED]))
        result.repairs += repairs
        if error or parsed is None:
            code = "truncated_args" if raw.truncated or error == "truncated_args" else "invalid_args"
            result.error, result.detail = code, "the arguments are not a complete JSON object"
            return result
        arguments = parsed
    schema = schemas[name]
    arguments, repairs = normalize(name, arguments)
    result.repairs += repairs
    arguments, repairs = coerce(arguments, schema)
    result.repairs += repairs
    problems = sorted(Draft202012Validator(schema).iter_errors(arguments), key=lambda e: [str(p) for p in e.path])
    if problems:
        result.error, result.detail = "invalid_args", format_validation_error(problems[0], schema)
        result.arguments = arguments
        return result
    result.arguments = arguments
    return result


@dataclass
class Strikes:
    """Malformed calls seen this turn (TOL-09): the second identical malformed call blocks further attempts."""

    seen: dict[str, int] = field(default_factory=dict)

    @staticmethod
    def key(raw: RawCall) -> str:
        return raw.name + "\x00" + json.dumps(raw.arguments, sort_keys=True, default=str)

    def blocked(self, raw: RawCall) -> bool:
        return self.seen.get(self.key(raw), 0) >= 2

    def record_malformed(self, raw: RawCall) -> None:
        self.seen[self.key(raw)] = self.seen.get(self.key(raw), 0) + 1
