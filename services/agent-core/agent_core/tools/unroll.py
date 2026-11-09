"""Recursive argument schemas unrolled to a fixed depth for the model (SPEC §16).

Muse Spark answers 400 "Recursive JSON schemas are not currently supported" to a request offering a tool whose
parameters refer to themselves (phys_circuit's nested series and parallel groups). A model therefore sees each loop
of `$defs` copied once per level and cut below the last level; the host and the worker still check every call against
the tool's own schema, which accepts everything the unrolled one does. A loop is entered through a definition that
something outside the loop refers to, and each pass through that definition is one level."""

from __future__ import annotations

from typing import Any

DEPTH = 4  # nested groups a model may write; the worker allows 8, and a deeper circuit fits the netlist form
_DEFS = "#/$defs/"
_CUT = object()


def _names(node: Any) -> set[str]:
    """The `$defs` names a node refers to."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        own = {ref[len(_DEFS) :]} if isinstance(ref, str) and ref.startswith(_DEFS) else set()
        return own.union(*(_names(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(_names(v) for v in node))
    return set()


def loops(defs: dict[str, Any]) -> set[str]:
    """The definitions that refer back to themselves, directly or through others."""
    edges = {name: _names(body) & defs.keys() for name, body in defs.items()}

    def returns(start: str) -> bool:
        seen: set[str] = set()
        todo = list(edges[start])
        while todo:
            name = todo.pop()
            if name == start:
                return True
            if name not in seen:
                seen.add(name)
                todo.extend(edges[name])
        return False

    return {name for name in defs if returns(name)}


def unrolled(schema: dict[str, Any], depth: int = DEPTH) -> dict[str, Any]:
    """`schema` with no recursive reference: itself when it has none. Raises ValueError for a loop that can only be
    cut somewhere other than an `anyOf` or `oneOf` alternative."""
    defs = schema.get("$defs") or {}
    looped = loops(defs)
    if not looped:
        return schema
    outside = {k: v for k, v in schema.items() if k != "$defs"}
    entries = looped & set().union(_names(outside), *(_names(defs[n]) for n in defs if n not in looped))
    copies: dict[str, Any] = {}
    todo: list[tuple[str, int]] = []

    def target(name: str, level: int) -> str | None:
        if name not in looped:
            return _DEFS + name
        level += name in entries
        if level > depth:
            return None
        key = f"{name}_{level}"
        if key in defs:
            raise ValueError(f"$defs already holds {key}")
        if key not in copies:
            copies[key] = None
            todo.append((name, level))
        return _DEFS + key

    def rewrite(node: Any, level: int) -> Any:
        if isinstance(node, list):
            items = [rewrite(v, level) for v in node]
            if _CUT in items:
                raise ValueError("a recursive reference can only be cut from anyOf or oneOf")
            return items
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith(_DEFS):
                to = target(value[len(_DEFS) :], level)
                if to is None:
                    return _CUT
                out[key] = to
            elif key in ("anyOf", "oneOf") and isinstance(value, list):
                kept = [c for c in (rewrite(v, level) for v in value) if c is not _CUT]
                if not kept:
                    return _CUT
                out[key] = kept
            else:
                out[key] = rewrite(value, level)
                if out[key] is _CUT:
                    raise ValueError("a recursive reference can only be cut from anyOf or oneOf")
        return out

    result = rewrite(outside, 0)
    kept_defs = {n: rewrite(body, 0) for n, body in defs.items() if n not in looped}
    while todo:
        name, level = todo.pop()
        body = rewrite(defs[name], level)
        if body is _CUT:
            raise ValueError(f"{name} has nothing left at level {level}")
        copies[f"{name}_{level}"] = body
    return {**result, "$defs": {**kept_defs, **copies}}
