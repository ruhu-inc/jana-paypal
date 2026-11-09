"""Structured output for roles with an output schema (MDL-15).

Mode, best first: (1) decode-time constraint (a llama.cpp grammar from the schema on local models, native JSON
schema elsewhere); (2) the schema as a single forced tool; (3) the schema in the prompt. Output is validated against
the full schema; on failure exactly one correction turn is sent with the errors verbatim and without the schema."""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from agent_core.providers import model_json
from agent_core.providers.base import Message, ModelCapabilities, ToolSchema

OUTPUT_TOOL = "emit_output"
# Keywords a decode-time grammar cannot express; they are enforced by validation instead.
GRAMMAR_UNSUPPORTED = (
    "if",
    "then",
    "else",
    "not",
    "unevaluatedProperties",
    "dependentRequired",
    "dependentSchemas",
    "contentEncoding",
    "format",
    "$comment",
    "description",
    "examples",
    "title",
    "default",
)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
CORRECTION = "Your previous reply did not match the required output format. Correct these problems and reply again:"


# The request parameter each mode depends on: a mode whose parameter the endpoint rejected is skipped (MDL-04).
MODE_PARAM = {"grammar": "response_format", "json_schema": "response_format", "tool": "tool_choice"}


def pick_mode(caps: ModelCapabilities, rejected: Collection[str] = ()) -> str:
    if caps.locality == "local" and caps.grammar and MODE_PARAM["grammar"] not in rejected:
        return "grammar"
    if caps.json_schema and MODE_PARAM["json_schema"] not in rejected:
        return "json_schema"
    if caps.tools and MODE_PARAM["tool"] not in rejected:
        return "tool"
    return "prompt"


# Size keywords a llama.cpp grammar expands into repetition rules; they are enforced by validation instead.
# Small limits are kept, because a model that overruns them costs a correction retry (MDL-15, G79): a pattern made
# only of plain character classes (`^chk_[A-Za-z0-9]{3,32}$`, `^[a-z0-9][a-z0-9._-]{2,80}$`), `maxItems` up to
# KEEP_MAX_ITEMS and `maxLength` up to KEEP_MAX_LENGTH. Richer patterns (alternation, negated classes) stay out: the
# runtime drops a grammar it cannot compile, without an error.
SIMPLE_ID_PATTERN = re.compile(r"^\^[a-z]{2,8}_\[[A-Za-z0-9_-]+\]\{\d+,\d+\}\$$")
CLASS_PATTERN = re.compile(r"^\^(\[[^\]\\^][^\]\\]*\](\{\d+,\d+\}|\*|\+)?)+\$$")
KEEP_MAX_ITEMS = 8
KEEP_MAX_LENGTH = 600
GRAMMAR_SIZE = (
    "maxItems",
    "minItems",
    "maxLength",
    "minLength",
    "maxProperties",
    "minProperties",
    "pattern",
    "uniqueItems",
)


def decode_schema(schema: Any, base: str | None = None, depth: int = 0, root: Any = None) -> Any:
    """The schema a decoder can compile (MDL-15): every `$ref` inlined (cross-contract ones too), conditional and
    size keywords removed. Validation still uses the full schema, so nothing is lost but grammar complexity."""
    if not isinstance(schema, (dict, list)):
        return schema
    # Nested regions/tables need more than twelve traversal levels. Never replace a primitive such as
    # "type": "string" with {}, which makes llama.cpp silently discard an invalid grammar.
    if depth > 32:
        return {} if isinstance(schema, dict) else []
    base = base or (schema.get("$id") if isinstance(schema, dict) else None)
    root = schema if root is None else root
    if isinstance(schema, dict):
        if "$ref" in schema:
            target, target_base, target_root = _resolve(schema["$ref"], base, root)
            rest = {k: v for k, v in schema.items() if k != "$ref"}
            merged = {**target, **rest} if isinstance(target, dict) else target
            return decode_schema(merged, target_base, depth + 1, target_root)
        kept = {
            k: (
                {name: decode_schema(child, base, depth + 1, root) for name, child in v.items()}
                if k in ("properties", "patternProperties") and isinstance(v, dict)
                else decode_schema(v, base, depth + 1, root)
            )
            for k, v in schema.items()
            if k not in GRAMMAR_UNSUPPORTED and k not in GRAMMAR_SIZE and k not in ("$defs", "$id", "$schema")
        }
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and (SIMPLE_ID_PATTERN.match(pattern) or CLASS_PATTERN.match(pattern)):
            kept["pattern"] = pattern
        if isinstance(schema.get("maxItems"), int) and schema["maxItems"] <= KEEP_MAX_ITEMS:
            kept["maxItems"] = schema["maxItems"]
        if isinstance(schema.get("maxLength"), int) and schema["maxLength"] <= KEEP_MAX_LENGTH:
            kept["maxLength"] = schema["maxLength"]
        return kept
    if isinstance(schema, list):
        return [decode_schema(v, base, depth + 1, root) for v in schema]
    return schema


def _resolve(ref: str, base: str | None, root: Any) -> tuple[Any, str | None, Any]:
    from agent_core.contracts import document

    uri, _, pointer = ref.partition("#")
    doc_id = uri or base
    if uri and not uri.startswith("http") and base:
        doc_id = base.rsplit("/", 1)[0] + "/" + uri  # a relative reference such as kinds/free_body
    target_root = root if not uri else document(str(doc_id))
    node: Any = target_root
    for part in [p for p in pointer.split("/") if p]:
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node, doc_id, target_root


def request_settings(mode: str, schema: dict[str, Any], name: str) -> tuple[dict[str, Any], list[ToolSchema]]:
    """Provider settings and extra tools for a mode. Prompt mode adds nothing here (see `prompt_instruction`)."""
    if mode in ("grammar", "json_schema"):
        # llama-server compiles a JSON schema to a grammar at decode time, so both modes send the schema.
        return {"structured": {"mode": "json_schema", "schema": decode_schema(schema), "name": name}}, []
    if mode == "tool":
        tool = ToolSchema(OUTPUT_TOOL, "Return the output.", decode_schema(schema))
        return {"structured": {"mode": "tool", "tool": OUTPUT_TOOL}}, [tool]
    return {}, []


def prompt_instruction(schema: dict[str, Any]) -> Message:
    return Message("system", "Reply with one JSON object that matches this JSON Schema:\n" + json.dumps(schema))


@dataclass(frozen=True)
class Checked:
    value: dict[str, Any] | None
    errors: list[str]


def parsed(text: str) -> Any:
    """The reply's JSON value, valid or not, or None when it is not JSON (a fence around it is allowed)."""
    body = text.strip()
    fenced = _FENCE.match(body)
    try:
        return model_json.loads(fenced.group(1) if fenced else body)
    except json.JSONDecodeError:
        return None


def _without_empty_optional(value: Any, schema: Any, resolver: Any) -> Any:
    """The reply without the optional strings it left empty where the contract wants a non-empty one, at any depth: an
    empty value says nothing, so it reads as absent instead of costing a correction round trip. Muse Spark's native
    JSON schema does not enforce `minLength`, nor a pattern the decoding schema leaves out, so its tutor turns send
    `"sketch_wanted": ""` and, in a visual, `"ref": ""`. A required field is never dropped, so its emptiness still
    fails validation, and a non-empty value is held to the field's rules as before."""
    schema, resolver = _dereferenced(schema, resolver)
    if isinstance(value, list) and isinstance(schema, dict) and isinstance(schema.get("items"), dict):
        return [_without_empty_optional(v, schema["items"], resolver) for v in value]
    if not isinstance(value, dict) or not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        return value
    required = set(schema.get("required") or ())
    props = schema["properties"]
    out = {}
    for name, item in value.items():
        sub = props.get(name)
        if item == "" and name not in required and isinstance(sub, dict) and _refuses_empty(sub, resolver):
            continue
        out[name] = _without_empty_optional(item, sub, resolver) if isinstance(item, dict | list) else item
    return out


def _dereferenced(schema: Any, resolver: Any) -> tuple[Any, Any]:
    """A subschema through its `$ref`s (the contract validators' roots are only a reference to the contract), with the
    resolver for references inside it; an unresolvable one is left for the validator to report."""
    for _ in range(8):
        if not isinstance(schema, dict) or "$ref" not in schema or resolver is None:
            break
        try:
            found = resolver.lookup(schema["$ref"])
        except Exception:
            break
        schema, resolver = found.contents, found.resolver
    return schema, resolver


def _refuses_empty(schema: Any, resolver: Any) -> bool:
    schema, _ = _dereferenced(schema, resolver)
    if not isinstance(schema, dict):
        return False
    pattern = schema.get("pattern")
    return int(schema.get("minLength") or 0) >= 1 or (isinstance(pattern, str) and re.search(pattern, "") is None)


def check(text: str, validator: Draft202012Validator) -> Checked:
    body = text.strip()
    fenced = _FENCE.match(body)
    try:
        value = model_json.loads(fenced.group(1) if fenced else body)  # LaTeX backslashes kept, not read as escapes
    except json.JSONDecodeError as error:
        return Checked(None, [f"the reply is not valid JSON ({error.msg} at character {error.pos})"])
    value = _without_empty_optional(value, validator.schema, validator._resolver)  # type: ignore[attr-defined]  # private jsonschema attribute
    problems = sorted(validator.iter_errors(value), key=lambda e: [str(p) for p in e.absolute_path])
    errors = [f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message[:300]}" for e in problems[:10]]
    if not errors and not isinstance(value, dict):  # `null` or a bare list can pass a loose schema: never valid
        errors = [f"(root): the reply must be one JSON object, not {type(value).__name__}"]
    return Checked(value if not errors else None, errors)


def correction(previous: str, errors: list[str]) -> list[Message]:
    """The one correction turn: the previous reply and the errors verbatim, never the schema."""
    return [Message("assistant", previous), Message("user", "\n".join([CORRECTION, *(f"- {e}" for e in errors)]))]
