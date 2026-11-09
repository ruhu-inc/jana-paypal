"""Dedupe of pure calls and the terminal-failure guard (TOL-13).

A call to a tool marked `dedupe` (pure, idempotent compute and reads) is keyed by the sha256 of its name, version and
canonical arguments: keys sorted, numbers normalised (2.0 is 2, -0.0 is 0), strings trimmed. A repeat within the
session returns the stored result, marked `cached`, with the same checks and no worker call. The store is a bounded
LRU per session. A worker result also depends on the conventions sent with the call (the pushed skill's plausibility
bands, VER-26), so a stored result is filed under those as well: a repeat after the bands change runs again rather
than coming back with a stale or missing `implausible` warning.

A call that failed with a non-retryable error is never run again unchanged in the same turn: the repeat is answered
with `repeated_failure_blocked`, quoting the first error."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

# Error codes after which an identical call in the same turn cannot succeed (TOL-13).
NON_RETRYABLE = frozenset({"domain_error", "limit_exceeded", "policy_denied", "unavailable"})


def canonical(value: Any) -> Any:
    """Arguments in canonical form: sorted keys, normalised numbers, trimmed strings."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return int(value)
        return 0 if value == 0 else value
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return {str(k).strip(): canonical(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]).strip())}
    if isinstance(value, list | tuple):
        return [canonical(v) for v in value]
    return value


def cacheable(spec: Any) -> bool:
    """Dedupe tools whose result cannot change within a session: worker compute and the pack index reads. Memory
    reads are left out (a forgotten note must not come back from a stored result, MEM-14), and so is `skill`, whose
    every delivery is traced (SKL-13)."""
    if not spec.meta.dedupe:
        return False
    return bool(spec.meta.executor == "worker" or (spec.toolset == "knowledge" and spec.name != "skill"))


def call_key(name: str, version: int, arguments: dict[str, Any], conventions: dict[str, Any] | None = None) -> str:
    """The sha256 of name, version and canonical arguments (64 hex digits, the tool-result `dedupe_key`). Given
    non-empty `conventions`, the key covers them too: the dedupe store's key for a result that depends on them."""
    keyed: dict[str, Any] = {"name": name, "version": version, "args": canonical(arguments)}
    if conventions:
        keyed["conventions"] = canonical(conventions)
    text = json.dumps(
        keyed,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class DedupeCache:
    """Successful results of dedupe tools, per session, least recently used first out."""

    capacity: int
    _sessions: dict[str, OrderedDict[str, dict[str, Any]]] = field(default_factory=dict)

    def get(self, session_id: str, key: str) -> dict[str, Any] | None:
        entries = self._sessions.get(session_id)
        if entries is None or key not in entries:
            return None
        entries.move_to_end(key)
        return copy.deepcopy(entries[key])

    def put(self, session_id: str, key: str, result: dict[str, Any]) -> None:
        if self.capacity <= 0:
            return
        entries = self._sessions.setdefault(session_id, OrderedDict())
        entries[key] = copy.deepcopy(result)
        entries.move_to_end(key)
        while len(entries) > self.capacity:
            entries.popitem(last=False)

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def size(self, session_id: str) -> int:
        return len(self._sessions.get(session_id, ()))


def served_from_cache(stored: dict[str, Any], call_id: str) -> dict[str, Any]:
    """A stored result answering a repeat: the new call's id, no time spent, `cached: true`, the same checks."""
    result = copy.deepcopy(stored)
    result["meta"] = {**result["meta"], "call_id": call_id, "duration_ms": 0, "cached": True}
    return result
