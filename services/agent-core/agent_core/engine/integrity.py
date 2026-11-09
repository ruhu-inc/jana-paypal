"""Integrity controls (PED-19): one question at a time for bulk pastes, and the adults' weekly counts.

The answer policy (PED-09) and the mock lock (AGT-24) are enforced in `tutor_flows`. Here: a paste of several
questions is split and worked one at a time, and the weekly integrity summary adults see is built from
`integrity_signal` events as counts only, never transcript content (TCH-12 reads it in S3)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

# A numbered question at the start of a line: "1.", "2)", "Q3", "(4)", "Question 5:".
_NUMBERED = re.compile(r"(?m)^\s*(?:q(?:uestion)?\s*\d{1,2}\s*[).:]?|\(?\d{1,2}[).:])\s+(?=\S)", re.I)


def split_questions(text: str) -> list[str]:
    """The separate questions in a paste: numbered lines first, else one question per question mark."""
    starts = [m.start() for m in _NUMBERED.finditer(text)]
    if len(starts) >= 2:
        parts = [text[a:b] for a, b in zip(starts, [*starts[1:], len(text)], strict=True)]
        return [p.strip() for p in parts if p.strip()]
    asked = [q.strip() for q in re.findall(r"[^?]+\?", text) if len(q.strip()) > 8]
    return asked if len(asked) >= 2 else [text.strip()]


# The counts adults see (PED-19): solution views, bottom-out hints and gaming flags.
SUMMARY_KINDS = {"solution_view": "solution_views", "bottom_out": "bottom_out_hints", "hint_burst": "gaming_flags",
                 "fast_guess_run": "gaming_flags"}  # fmt: skip


def _day(occurred: Any) -> date:
    if isinstance(occurred, int | float):
        return datetime.fromtimestamp(occurred / 1000, UTC).date()
    return datetime.fromisoformat(str(occurred).replace("Z", "+00:00")).date()


def weekly_summary(events: Iterable[Mapping[str, Any]], week_start: date) -> dict[str, Any]:
    """The week's integrity counts from stored events (`type`, `payload`, `occurred_at`). Only `integrity_signal`
    events are read, and only their kind: the summary holds numbers and the week, nothing a student wrote."""
    counts = dict.fromkeys(sorted(set(SUMMARY_KINDS.values())), 0)
    end = week_start + timedelta(days=7)
    for event in events:
        if event.get("type") != "integrity_signal":
            continue
        if not week_start <= _day(event.get("occurred_at")) < end:
            continue
        key = SUMMARY_KINDS.get(str((event.get("payload") or {}).get("kind")))
        if key is not None:
            counts[key] += 1
    return {"week_start": week_start.isoformat(), **counts}
