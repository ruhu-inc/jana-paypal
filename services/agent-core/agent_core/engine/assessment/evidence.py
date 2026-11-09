"""ASM-10 adapter for the learner's projection extension point.

Keeps the diagnostic skip weight local to that opportunity; ordinary incorrect attempts retain LRN-06 weighting.
The existing diagnostic_item event identifies it on replay, without a second learner-store writer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from agent_core.learner.projections import Context, MasteryProjection, Projection, default_projections
from agent_core.learner.service import Learner
from agent_core.store.events import StoredEvent


class DiagnosticMasteryProjection(MasteryProjection):
    def __init__(self, weight: float) -> None:
        self.weight = weight
        self._closing = False
        self.code_version = f"{MasteryProjection.code_version}/asm10-asm11.2/{weight}"

    def _closed(self, ctx: Context, event: StoredEvent) -> None:
        pending = False
        if event.session_id is not None:
            if ctx.sessions is not None:
                pending = any(
                    e.type == "diagnostic_item"
                    and e.payload.get("response") == "not_learned"
                    and e.payload.get("attempt_id") == event.payload["attempt_id"]
                    for e in ctx.sessions.get(event.session_id, [])
                )
            else:
                pending = (
                    ctx.conn.execute(
                        "SELECT 1 FROM events WHERE type='diagnostic_item' AND session_id=?"
                        " AND json_extract(payload,'$.response')='not_learned'"
                        " AND json_extract(payload,'$.attempt_id')=? AND seq<=? LIMIT 1",
                        (event.session_id, event.payload["attempt_id"], event.seq),
                    ).fetchone()
                    is not None
                )
        if pending:
            ctx = replace(ctx, params=replace(ctx.params, obs_terminal_incorrect_w=self.weight))
        self._closing = True
        try:
            super()._closed(ctx, event)
        finally:
            self._closing = False

    def _load(self, ctx: Context, kc_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        loaded = set(kc_ids)
        if self._closing:
            # The base projection requests the direct KCs and their immediate neighbours. Include the remaining
            # configured frontier in its read set, so observe()'s deeper evidence is included in its atomic save.
            prerequisites, dependents = ctx.content.prerequisites(), ctx.content.dependents()
            frontier = set(loaded)
            for _ in range(1, ctx.params.mastery_implied_depth):
                following = {
                    other for kc in frontier for other in [*prerequisites.get(kc, ()), *dependents.get(kc, ())]
                } - loaded
                loaded.update(following)
                frontier = following
        return super()._load(ctx, sorted(loaded))


def assessment_projections(settings: Any) -> tuple[Projection, ...]:
    return tuple(
        DiagnosticMasteryProjection(settings.diagnostic_not_learned_weight) if isinstance(p, MasteryProjection) else p
        for p in default_projections(settings)
    )


def install(learner: Learner, settings: Any) -> None:
    replacement = DiagnosticMasteryProjection(settings.diagnostic_not_learned_weight)
    if any(
        p.name == replacement.name and p.code_version == replacement.code_version for p in learner.projector.projections
    ):
        return
    learner.projector.projections = tuple(
        replacement if isinstance(p, MasteryProjection) else p for p in learner.projector.projections
    )
    learner.projector.ensure_current()
