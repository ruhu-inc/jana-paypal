"""Engine scope: the turn or job a piece of work runs in (ARC-20, OBS-04).

The engine opens a scope for every turn and background job. The router refuses any model
call made outside one, so every model call is traced, budgeted and gated by the engine.
The scope lives in `trace` because the router may import `trace` but never `engine`."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

ScopeKind = Literal["turn", "job"]


@dataclass(frozen=True)
class EngineScope:
    kind: ScopeKind
    id: str


_current: ContextVar[EngineScope | None] = ContextVar("engine_scope", default=None)


def current_scope() -> EngineScope | None:
    return _current.get()


@contextmanager
def engine_scope(kind: ScopeKind, scope_id: str) -> Iterator[EngineScope]:
    scope = EngineScope(kind, scope_id)
    token = _current.set(scope)
    try:
        yield scope
    finally:
        _current.reset(token)
