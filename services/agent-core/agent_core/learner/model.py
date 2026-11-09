"""Reading the learner model at a moment: what the planner, the policies and the progress views see.

The projections store each KC's state as of its last event. Two inputs of TABLE LRN-09a change with time alone: FSRS
retrievability (a mastered KC whose card has decayed below the bar is `needs_review`) and the stuck cap's revisit
deferral (a `moved_on` KC returns when its revisit date arrives). `kc_views` re-evaluates the ordered state rules
with both at `now`, so every reader shows the same state. Due cards are capped by the exam window here (LRN-13),
because the exam date is profile data, not an event. Nothing here writes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from agent_core.learner.fsrs import Card, exam_capped_due, scheduler_of
from agent_core.learner.mastery import BANDS, DAY_MS, STATE_LABELS, MasteryParams, StateInputs, kc_state, subject_of
from agent_core.learner.projections import day_number, local_date, week_start
from agent_core.store.events import ms_from_iso


@dataclass(frozen=True)
class KcView:
    kc_id: str
    subject: str
    state: str
    p_mastery: float
    n_eff: float
    attempts: int
    hints_used: int
    bottom_outs: int
    fading_stage: int
    stuck_count: int
    moved_on_at: int | None
    revisit_at: int | None
    last_seen_at: int | None

    @property
    def band(self) -> str:
        return BANDS[self.state]

    @property
    def label(self) -> str:
        return STATE_LABELS[self.state]


def cards(conn: sqlite3.Connection) -> dict[str, Card]:
    rows = conn.execute(
        "SELECT kc_id, state, stability, difficulty, due_at, reps, lapses, last_review_at FROM review_cards"
    )
    return {r[0]: Card(*r[1:]) for r in rows}


def kc_views(conn: sqlite3.Connection, params: MasteryParams, now: int) -> dict[str, KcView]:
    """Every KC with evidence, its TABLE LRN-09a state evaluated at `now`."""
    scheduler = scheduler_of(params)
    held = cards(conn)
    out: dict[str, KcView] = {}
    rows = conn.execute(
        "SELECT kc_id, status, p_mastery, n_eff, attempts, hints_used, bottom_outs, fading_stage, stuck_count,"
        " moved_on_at, mastery_check_state, mastery_check, last_seen_at FROM kc_mastery ORDER BY kc_id"
    )
    for kc, _status, p_k, n_eff, attempts, hints, bottoms, fading, stuck, moved, check_state, check, seen in rows:
        record = (json.loads(check) or {}) if check else {}
        retry = record.get("retry_after")
        card = held.get(kc)
        revisit = moved + int(params.stuck_revisit_days * DAY_MS) if moved is not None else None
        state = kc_state(
            StateInputs(
                direct_observations=attempts,
                p_mastery=p_k,
                n_eff=n_eff,
                confirmed=check_state == "passed",
                failed_since_confirmed=bool(record.get("review_failed")),
                low_retrievability=card is not None
                and scheduler.retrievability(card, now) < params.mastery_review_retrievability,
                deferral_active=revisit is not None and now < revisit,
                retry_lock_active=retry is not None and now < ms_from_iso(retry),
            ),
            params,
        )
        out[kc] = KcView(kc, subject_of(kc), state, p_k, n_eff, attempts, hints, bottoms, fading or 0, stuck, moved,
                         revisit, seen)  # fmt: skip
    return out


@dataclass(frozen=True)
class DueCard:
    kc_id: str
    due_ms: int  # capped by the exam window
    retrievability: float


def due_cards(
    conn: sqlite3.Connection, params: MasteryParams, now: int, exam_days: Mapping[str, int], window_days: int
) -> list[DueCard]:
    """Cards due by `now` (their exam-capped due date), lowest retrievability first, then by KC id."""
    scheduler = scheduler_of(params)
    out = []
    for kc, card in cards(conn).items():
        due = exam_capped_due(card, exam_days.get(subject_of(kc)), window_days)
        if due <= now:
            out.append(DueCard(kc, due, scheduler.retrievability(card, now)))
    return sorted(out, key=lambda d: (d.retrievability, d.kc_id))


def exam_day_ms(text: str | None) -> int | None:
    """An exam date (`YYYY-MM-DD`) as the UTC midnight it starts."""
    if not text:
        return None
    return (date.fromisoformat(text) - date(1970, 1, 1)).days * DAY_MS


@dataclass(frozen=True)
class Streak:
    """The streak as the student sees it today (LRN-33): `days` is 0 when missed days outran the freezes, which the
    view shows neutrally as a new streak to start."""

    days: int
    freezes_left: int
    last_active_date: str | None
    counted_today: bool


def streak(conn: sqlite3.Connection, params: MasteryParams, now: int, weekly_freezes: int) -> Streak:
    row = conn.execute(
        "SELECT streak_days, freezes_left, last_active_date FROM engagement_state WHERE id = 1"
    ).fetchone()
    if row is None or row[2] is None:
        return Streak(0, 0, None, False)
    days, freezes, last = row
    today = params.local_day(now)
    previous = day_number(last)
    if previous >= today:
        return Streak(days, freezes, last, previous == today)
    mondays = sum(1 for d in range(previous + 1, today + 1) if week_start(d) == d)
    available = min(params.streak_max_freezes, freezes + mondays * weekly_freezes)
    missed = today - previous - 1  # today itself can still count
    if missed > available:
        return Streak(0, available, last, False)
    return Streak(days, available - missed, last, False)


def active_days_by_week(conn: sqlite3.Connection, params: MasteryParams, now: int, weeks: int) -> list[int]:
    """Days that counted towards the streak in each of the last `weeks` whole weeks, oldest first (LRN-27)."""
    this_week = week_start(params.local_day(now))
    out = []
    for back in range(weeks, 0, -1):
        begin = this_week - 7 * back
        out.append(conn.execute(
            "SELECT count(*) FROM engagement_daily WHERE streak_length > 0 AND freeze_used = 0 AND day >= ?"
            " AND day < ?", (local_date(begin), local_date(begin + 7))).fetchone()[0])  # fmt: skip
    return out


def first_active_day(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT min(day) FROM engagement_daily WHERE streak_length > 0").fetchone()
    return day_number(row[0]) if row and row[0] else None
