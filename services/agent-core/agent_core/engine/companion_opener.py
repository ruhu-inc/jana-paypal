"""The Last time card (SPEC §19.5.2, W8 phase 1): facts by code, worded by the engine's templates, no model call.

The facts come from the learner database only, never from transcript text: the last ended session of any mode (its
day, mode and subject), the learner model's review cards due by the end of today, the nearest exam's distance in
days (the profile's exam date or one the learner set on this computer), and whether a research pack became ready
since the learner was last in Study. Code picks at most three, in §19.5.2's order (a check-in, phase 3; the last
session; due cards; the exam; a ready pack) and words them with `companion.opener.*` in
`content/agent/templates/engine.en.yaml`. With no session ever ended and nothing due, it is `first_time`. Every
number in the text is one of the chosen facts, beside its label. Phase 2 adds the fast-model wording; offline, or with
no candidate, the template stays the wording, always.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from agent_core.engine.exam_dates import ExamDesk
from agent_core.learner.service import Learner
from agent_core.memory.queries import due_reviews, profile_summary
from agent_core.memory.reflect_input import EVIDENCE_TYPES

DEFAULT_TEMPLATES = Path(__file__).resolve().parents[4] / "content" / "agent" / "templates" / "engine.en.yaml"
MAX_FACTS = 3
SUBJECTS = ("mathematics", "physics", "chemistry", "biology", "accounting")


@dataclass(frozen=True)
class OpenerTemplates:
    texts: Mapping[str, str]
    modes: Mapping[str, str]

    def text(self, name: str, **fields: Any) -> str:
        return str(self.texts[name]).format(**fields)


@cache
def _templates(path: str) -> OpenerTemplates:
    data = yaml.safe_load(Path(path).read_text())
    opener = data["companion"]["opener"]
    modes = {str(k): str(v) for k, v in (opener.get("modes") or {}).items()}
    texts = {str(k): str(v) for k, v in opener.items() if k != "modes"}
    # The thread's own engine-written lines (phase 2, §19.5.1): the hand-over to the tutor, the canned turn.
    for k, v in (data["companion"].get("turn") or {}).items():
        if isinstance(v, dict):  # a group of lines, as `made.flashcards`
            texts.update({f"{k}.{kk}": str(vv) for kk, vv in v.items()})
        else:
            texts[str(k)] = str(v)
    # Check-ins (phase 3, §19.5.5): one line per kind, as `checkin.<kind>`.
    texts.update({f"checkin.{k}": str(v) for k, v in (data["companion"].get("checkin") or {}).items()})
    missing = [k for k in ("first_time", "last_time", "due_only", "pack_ready") if k not in texts]
    if missing:
        raise ValueError(f"companion.opener templates missing: {missing}")
    return OpenerTemplates(texts, modes)


def load_templates(content_root: Path | None = None) -> OpenerTemplates:
    path = content_root / "templates" / "engine.en.yaml" if content_root is not None else DEFAULT_TEMPLATES
    return _templates(str(path if path.exists() else DEFAULT_TEMPLATES))


@dataclass(frozen=True)
class Facts:
    """§19.3.3's opener facts. `study_cards_due` and `next_step` are phase 2, `checkin_id` phase 3: 0 and null."""

    last_session_date: str | None
    last_session_mode: str | None
    last_subject: str | None
    learner_cards_due: int
    exam_distance_days: int | None
    pack_ready: bool
    today: str
    study_cards_due: int = 0  # Study's own due cards (phase 2): said with the learner model's as "cards due"
    next_step: str | None = None  # the rolling summary's next step (phase 2)

    @property
    def cards_due(self) -> int:
        return self.learner_cards_due + self.study_cards_due

    def view(self) -> dict[str, Any]:
        return {"last_session_date": self.last_session_date, "last_session_mode": self.last_session_mode,
                "last_subject": self.last_subject, "next_step": self.next_step,
                "learner_cards_due": self.learner_cards_due, "study_cards_due": self.study_cards_due,
                "exam_distance_days": self.exam_distance_days, "pack_ready": self.pack_ready,
                "checkin_id": None}  # fmt: skip


def _day(learner: Learner, ms: int) -> str:
    day = learner.projector.ctx.params.local_day(ms)
    return (date(1970, 1, 1) + timedelta(days=int(day))).isoformat()


def facts(learner: Learner, settings: Any, now_ms: int) -> Facts:
    """The opener's facts from the open learner database, by code only."""
    conn = learner.conn
    today = _day(learner, now_ms)
    # The last ended session that holds learning (an attempt, a hint, a check, Study activity, …): the hand-off's
    # scripted interview and any session the learner opened and left without learning never count (§19 fix).
    last = conn.execute(
        "SELECT mode, subject, ended_at FROM sessions s WHERE ended_at IS NOT NULL AND EXISTS (SELECT 1 FROM events e"
        " WHERE e.session_id = s.session_id AND e.erased_at IS NULL AND e.type IN (SELECT value FROM json_each(?)))"
        " ORDER BY ended_at DESC, session_id DESC LIMIT 1",
        (json.dumps([t for t in EVIDENCE_TYPES if t != "memory_requested"]),),
    ).fetchone()
    due = len(due_reviews(conn, today))
    days: list[int] = []
    summary = profile_summary(conn, today=today, settings=settings)
    if summary.exam_distance and summary.exam_distance.startswith("in "):
        days.append(int(summary.exam_distance.split()[1]))
    offset = learner.projector.ctx.params.learner_day_offset_minutes
    countdown = ExamDesk(conn, settings, clock_ms=lambda: now_ms, day_offset_minutes=offset).countdown(now_ms)
    if countdown is not None:
        days.append(int(countdown["days"]))
    # A pack that became ready after the learner last left Study (or ever, before a first Study visit).
    left = conn.execute("SELECT max(ended_at) FROM sessions WHERE mode = 'study'").fetchone()[0]
    ready = conn.execute(
        "SELECT 1 FROM study_research WHERE json_extract(body, '$.status') = 'ready' AND updated_at > ? LIMIT 1",
        (int(left or 0),),
    ).fetchone()
    return Facts(
        last_session_date=_day(learner, int(last[2])) if last else None,
        last_session_mode=str(last[0]) if last else None,
        last_subject=str(last[1]) if last and last[1] in SUBJECTS else None,
        learner_cards_due=due,
        exam_distance_days=min(days) if days else None,
        pack_ready=ready is not None,
        today=today,
    )


def chosen(found: Facts) -> list[str]:
    """At most three facts, in §19.5.2's order: (a check-in, phase 3), the last session, due cards, the exam, a ready
    pack."""
    order = [
        ("last_session", found.last_session_date is not None),
        ("due", found.cards_due > 0),
        ("exam", found.exam_distance_days is not None),
        ("pack_ready", found.pack_ready),
    ]
    return [name for name, present in order if present][:MAX_FACTS]


def word(found: Facts, picked: list[str], templates: OpenerTemplates, *, thread: bool = False) -> str:
    """The template wording of the chosen facts: every number in it is a chosen fact, beside its label. `thread`:
    the first bubble of **Your tutor**, which with nothing to say greets the learner (`first_time_thread`) rather
    than describing the card (`first_time`)."""
    if not picked:
        greeting = thread and "first_time_thread" in templates.texts
        return templates.text("first_time_thread" if greeting else "first_time")
    clauses = []
    for name in picked:
        if name == "due":
            n = found.cards_due
            clauses.append(templates.text("due_one" if n == 1 else "due", count=n))
        elif name == "exam":
            n = int(found.exam_distance_days or 0)
            key = "exam_today" if n == 0 else "exam_one" if n == 1 else "exam"
            clauses.append(templates.text(key, days=n))
        elif name == "pack_ready":
            clauses.append(templates.text("pack_ready"))
    if "last_session" in picked:
        mode = str(found.last_session_mode)
        subject = found.last_subject or templates.text("no_subject_study" if mode == "study" else "no_subject")
        when = templates.text("when_today") if found.last_session_date == found.today else templates.text(
            "when_date", date=found.last_session_date)  # fmt: skip
        head = templates.text("last_time", subject=subject, where=templates.modes.get(mode, templates.modes["other"]),
                              when=when)  # fmt: skip
        return head + ("; " + templates.text("joiner").join(clauses) if clauses else "") + "."
    body = templates.text("due_only", facts=templates.text("joiner").join(clauses))
    return body[:1].upper() + body[1:]


@dataclass
class OpenerDesk:
    """GET /v1/companion/opener: the facts recomputed on every open, worded by the templates (phase 1)."""

    learner: Learner
    settings: Any
    clock_ms: Callable[[], int] = lambda: int(datetime.now(UTC).timestamp() * 1000)
    templates: OpenerTemplates | None = None
    # Study's own due cards (phase 2), counted the way the thread's opener counts them, so Home and Your tutor say
    # the same number (the 2 November rehearsal: Home "1 card is due", the thread "11 cards are due").
    study_cards: Callable[[int], int] | None = None

    def opener(self) -> dict[str, Any]:
        now = self.clock_ms()
        found = facts(self.learner, self.settings, now)
        if self.study_cards is not None:
            found = replace(found, study_cards_due=self.study_cards(now))
        text = word(found, chosen(found), self.templates or load_templates())
        return {"opener_id": found.today, "text": text[:240], "facts": found.view()}
