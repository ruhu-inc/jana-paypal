"""agent_core.engine: the turn engine (M4). Only the engine opens the scope model calls need."""

from collections.abc import Iterator
from contextlib import contextmanager

from agent_core.trace import EngineScope, engine_scope


@contextmanager
def turn_scope(turn_id: str) -> Iterator[EngineScope]:
    with engine_scope("turn", turn_id) as scope:
        yield scope


@contextmanager
def job_scope(job_id: str) -> Iterator[EngineScope]:
    """Background work (titles, compaction, reflection, judging) runs as an engine job (ARC-20)."""
    with engine_scope("job", job_id) as scope:
        yield scope


__all__ = ["job_scope", "turn_scope"]
