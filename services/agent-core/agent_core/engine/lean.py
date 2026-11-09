"""The lean turn on the low hardware class (D128, AGT-28, MDL-16a).

A CPU-only minimum laptop reads a prompt at about 18 tokens/s and writes at about 7 (an 8B-class model at 4-bit, the
jana-hw-low measurement of 2026-10-21), so every written token costs about 0.14 s and every model call several
seconds. On the lean classes the engine asks for less and decides more itself:

- Structured roles decode a lean shape of their contract: the fields the engine only stores or drops (the
  Diagnoser's free inference, prerequisite gaps, confidence and each known's quote for typed questions; a plan
  step's reason and KC) are left out of the decoder and the prompt's output contract, and the engine writes their
  neutral values before the full contract validates the result. The same lean shape every call keeps the role's
  cached prompt prefix (MDL-22).
- The session's title is the topic's name, the title role's own fallback, with no model call.
- The Coach is offered no tools (its teaching skill is already pushed into its prompt, SKL-08) and no visual, so a
  reply is one constrained call rather than an unconstrained call, a tool round and a decode-only retry.

Nothing here weakens a gate: every reply still meets the full contract and passes every API-10 gate."""

from __future__ import annotations

import copy
from typing import Any

# Fields left out of the decoder on a lean class, with the value the engine writes in their place. A nested field is
# "parent[].child" (each item of an array).
LEAN_FIELDS: dict[str, dict[str, Any]] = {
    "agents/diagnosis": {"inference": "", "prerequisite_gap_refs": [], "confidence": "medium", "knowns[].quote": ""},
    # A plan step's reason and KC are not kept in the engine's plan (`state.PlanStep`); the Coach writes the board.
    "agents/solution-plan": {"steps[].reason": "", "steps[].kc_ref": None},
}
# Arrays the lean decoder keeps empty (the engine shows nothing in their place).
LEAN_EMPTY: dict[str, tuple[str, ...]] = {"agents/tutor-turn": ("visual",)}


def lean(hardware_class: str | None, classes: tuple[str, ...]) -> bool:
    return hardware_class in classes


def decode_schema(schema_name: str, schema: dict[str, Any], *, keep_quotes: bool = False,
                  tolerant: bool = False) -> dict[str, Any]:  # fmt: skip
    """The lean shape of `schema` for decoding and for the prompt's output contract. `keep_quotes` keeps each
    known's quote (a photo's givens must quote the confirmed text, `team.photo_knowns_valid`). `tolerant` is the shape
    a reply is checked against: the left-out fields are optional rather than absent, so a reply written without the
    decoder (a call with tools offered) that has them anyway is not rejected for it."""
    dropped = [f for f in LEAN_FIELDS.get(schema_name, {}) if not (keep_quotes and f == "knowns[].quote")]
    empty = LEAN_EMPTY.get(schema_name, ())
    if not dropped and not empty:
        return schema
    out = copy.deepcopy(schema)
    for field in dropped:
        _drop(out, field.split("[]."), keep=tolerant)
    for name in empty:
        out["properties"][name] = {**out["properties"][name], "maxItems": 0}
    return out


def completed(schema_name: str, value: Any) -> Any:
    """A lean reply with the engine's values for the fields its decoder left out (absent ones only)."""
    fields = LEAN_FIELDS.get(schema_name)
    if not fields or not isinstance(value, dict):
        return value
    out = dict(value)
    for field, default in fields.items():
        parent, _, child = field.partition("[].")
        if not child:
            out.setdefault(parent, copy.deepcopy(default))
        elif isinstance(out.get(parent), list):
            out[parent] = [{**item, child: item.get(child, default)} if isinstance(item, dict) else item
                           for item in out[parent]]  # fmt: skip
    return out


def _drop(schema: dict[str, Any], path: list[str], keep: bool = False) -> None:
    head, rest = path[0], path[1:]
    if rest:
        _drop(schema["properties"][head]["items"], rest, keep)
        return
    if not keep:
        schema["properties"].pop(head, None)
    if head in schema.get("required", []):
        schema["required"] = [r for r in schema["required"] if r != head]
