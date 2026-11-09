"""The companion thread's rolling summary row (`companion_summary`, SPEC §19.5.3, W8 phase 2): the empty body a new
thread, and a thread restored from a profile archive, starts from. The fold that fills it from the learning events is
`memory.write.fold_companion_summary`; an archive's restore (`store.companion_archive`) drops the archived body and
leaves the fold to replay it, so the shape lives here where both can reach it (ARC-18)."""

from __future__ import annotations

from typing import Any


def summary_body() -> dict[str, Any]:
    """An empty `CompanionSummary`."""
    return {"version": 0, "cursor_seq": 0, "updated_at": None, "topics": [], "misconceptions": [], "next_step": None,
            "episodes": 0}  # fmt: skip
