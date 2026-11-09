"""The Study screen's learning session and the activity it records (SPEC §19.3.2, §19.4; W8 phase 1).

One `sessions` row of mode `study` per profile per day: the screen's open starts it (or resumes the day's row, as
touching an idle-ended learning session begins a new learning session in it, LRN-01), every Study command keeps it
alive, and it ends when the screen closes or after `STUDY_IDLE_MS` without activity. Its start and end are the
`session_started` and `session_ended` events every learning session has; its end queues the Reflector's `study_end`
write point through `on_end`.

What the learner did is recorded as `study_activity` events citing that session: activity, never mastery (JM-D22,
JM-D26). The mastery, review and misconception projections ignore the type; the Reflector and the knows view read
it. Each activity has one commit seam, after Study's own transaction has committed and never inside it
(`Learner.record_batch` refuses a nested transaction): a write asked for while a transaction is open waits in
`pending` until `flush`. A `ref` already in the log is never recorded again, so a repeated submission records
nothing. An aid finished while no Study session is open is recorded by the next session's open, at the time it was
made (`sweep`): no session, no event. A failing write is counted and logged and never breaks the Study command.

The session also freezes the learner snapshot the Study requests carry (`learner_block`, the cloud variant's data
form of §19.3.1), once per session, as the Coach does once per tutoring session (MEM-03).
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from agent_core.engine.study_topics import kc_tags, matching_topics
from agent_core.learner.service import Learner, NewEvent
from agent_core.memory.snapshot import (
    LEARNER_BLOCK_KCS,
    LEARNER_BLOCK_MISCONCEPTIONS,
    FrozenMemory,
    freeze,
    learner_block_data,
)
from agent_core.store.ids import new_ulid

log = logging.getLogger(__name__)

STUDY_IDLE_MS = 20 * 60_000  # SPEC §19.3.2: a Study session ends after 20 minutes without activity
HARDWARE = ("content_only", "low", "mid", "high")
OUTCOMES = {"correct": "correct", "incorrect": "incorrect", "partial": "incorrect", "skipped": "skipped",
            "unverifiable": "unverifiable"}  # fmt: skip
SUBJECT_CODES = {"mth": "mathematics", "phy": "physics", "chm": "chemistry", "bio": "biology", "acc": "accounting"}


class _NoLabels:
    """Labels when no pack is installed: the snapshot then names KCs by id and has no topics."""

    nodes: Mapping[str, Any] = {}

    def kc_statement(self, kc_id: str) -> str | None:
        return None

    def topic_of(self, kc_id: str) -> str | None:
        return None

    def kcs_in_topic(self, topic_id: str) -> Sequence[str]:
        return ()

    def misconception_name(self, misconception_id: str) -> str | None:
        return None

    def prerequisites(self) -> Mapping[str, Sequence[str]]:
        return {}


@dataclass(frozen=True)
class _Waiting:
    """An activity asked for while a transaction was open: written by `flush` once it has committed."""

    payload: dict[str, Any]
    occurred_at: int
    open_session: bool


@dataclass
class _Visit:
    learner: Learner
    session_id: str  # bare, as stored
    day: int
    last_activity: int
    active_ms: int = 0
    frozen: FrozenMemory | None = None
    subjects: Counter[str] = field(default_factory=Counter)


@dataclass
class StudyVisits:
    """The open profile's Study session of the day and its activity events."""

    learner: Callable[[], Learner | None]  # the open learner database's learner service, or None
    clock: Callable[[], int]
    settings: Any
    content: Callable[[], Any] = lambda: None  # the installed packs' index (labels and topics), or None
    hardware_class: Callable[[], str] = lambda: "mid"
    # (session id with its `ses_` prefix, subject or None, session date): the `study_end` write point (§19.4)
    on_end: Callable[[str, str | None, str], None] | None = None
    pending: list[_Waiting] = field(default_factory=list)
    failures: int = 0
    _visit: _Visit | None = None
    _block: tuple[int, int, dict[str, Any]] | None = None  # (learner id, day, block) outside a session

    # --- the session -----------------------------------------------------------------------------------------------

    def _day(self, learner: Learner, ms: int) -> int:
        return int(learner.projector.ctx.params.local_day(ms))

    def _current(self) -> _Visit | None:
        """The open visit when it belongs to the open learner database; one left by another profile is dropped (its
        session ends `crash_recovered` the next time that database opens, LRN-01)."""
        learner = self.learner()
        if self._visit is not None and self._visit.learner is not learner:
            self._visit = None
        return self._visit

    @property
    def session_id(self) -> str | None:
        visit = self._current()
        return f"ses_{visit.session_id}" if visit is not None else None

    def open(self, now: int | None = None) -> tuple[str, bool]:
        """Open the day's Study session, or keep the open one: (session id, resumed). Raises LookupError when no
        learner database is open."""
        learner = self.learner()
        if learner is None:
            raise LookupError("no learner database is open")
        now = self.clock() if now is None else now
        visit = self._current()
        if visit is not None and now - visit.last_activity >= STUDY_IDLE_MS:
            self.end("idle_timeout", now)
            visit = None
        if visit is not None:
            if self._day(learner, now) == visit.day:
                visit.active_ms += now - visit.last_activity
                visit.last_activity = now
                return f"ses_{visit.session_id}", True
            self.end("student_left", now)  # past midnight: tomorrow is a new session
        return self._start(learner, now)

    def _start(self, learner: Learner, now: int) -> tuple[str, bool]:
        conn, day = learner.conn, self._day(learner, now)
        row = conn.execute(
            "SELECT session_id, started_at FROM sessions WHERE mode = 'study' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        resumed = row is not None and self._day(learner, int(row[1])) == day
        sid = str(row[0]) if resumed and row is not None else new_ulid(now)
        hardware = self.hardware_class()
        payload = {"session_id": sid, "mode": "study", "entry": "study",
                   "hardware_class": hardware if hardware in HARDWARE else "mid"}  # fmt: skip

        def row_write(c: Any, _: Any) -> None:
            if resumed:
                c.execute("UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE session_id = ?", (sid,))
            else:
                c.execute("INSERT INTO sessions (session_id, mode, hardware_class, started_at) VALUES (?, 'study', ?,"
                          " ?)", (sid, payload["hardware_class"], now))  # fmt: skip

        learner.record_batch(
            [NewEvent("session_started", payload, "engine", session_id=sid, occurred_at=now)], extra=row_write
        )
        visit = _Visit(learner, sid, day, now)
        try:
            source = self.content() or _NoLabels()
            visit.frozen = freeze(conn, source, self.settings, subject=None, topic_id=None,
                                  session_date=_iso_day(day), kcs_max=LEARNER_BLOCK_KCS,
                                  misconceptions_max=LEARNER_BLOCK_MISCONCEPTIONS)  # fmt: skip
        except Exception:  # a snapshot that cannot be read leaves Study requests without a block, never fails them
            log.exception("study snapshot failed")
        self._visit = visit
        self.sweep(now)
        return f"ses_{sid}", resumed

    def touch(self, now: int | None = None) -> str | None:
        """A Study command: the screen is open, so its session is open (opened if it was not)."""
        try:
            return self.open(now)[0]
        except LookupError:
            return None

    def end(self, reason: str, now: int | None = None, *, notify: bool = True) -> str | None:
        """End the open Study session (`student_left` at the screen's close, `idle_timeout` at its last activity,
        `interrupted` when the app or profile closes) and, with `notify`, queue its `study_end` write point (a
        closing profile's is queued by the Reflector's backlog when it next opens, AGT-26). Returns its id."""
        visit = self._current()
        if visit is None:
            return None
        self.flush()
        now = self.clock() if now is None else now
        idle = reason == "idle_timeout"
        ended_at = visit.last_activity if idle else max(now, visit.last_activity)
        active = visit.active_ms + (0 if idle else min(max(0, now - visit.last_activity), STUDY_IDLE_MS))
        top = visit.subjects.most_common(2)
        subject = top[0][0] if top and (len(top) == 1 or top[0][1] > top[1][1]) else None
        sid = visit.session_id
        self._visit = None

        def row_write(c: Any, _: Any) -> None:
            c.execute("UPDATE sessions SET ended_at = ?, end_reason = ?, subject = coalesce(?, subject)"
                      " WHERE session_id = ?", (ended_at, reason, subject, sid))  # fmt: skip

        try:
            payload = {"session_id": sid, "reason": reason, "active_ms": max(0, active)}
            ended = NewEvent("session_ended", payload, "engine", session_id=sid, occurred_at=ended_at)
            visit.learner.record_batch([ended], extra=row_write)
        except Exception:
            self.failures += 1
            log.exception("study session end failed")
            return None
        if self.on_end is not None and notify:
            try:
                self.on_end(f"ses_{sid}", subject, _iso_day(visit.day))
            except Exception:  # the Reflector's queue never breaks a session's end
                log.exception("study end hook failed")
        return f"ses_{sid}"

    def idle_check(self, now: int | None = None) -> str | None:
        """End the open Study session once it has been quiet for `STUDY_IDLE_MS`."""
        visit = self._current()
        now = self.clock() if now is None else now
        if visit is not None and now - visit.last_activity >= STUDY_IDLE_MS:
            return self.end("idle_timeout", now)
        return None

    # --- the learner block (§19.3.1) ---------------------------------------------------------------------------

    def learner_block(self) -> dict[str, Any] | None:
        """The learner block for a Study request: the open session's frozen snapshot, else one frozen for the day on
        the open learner database; None when no learner database is open or the snapshot cannot be read."""
        learner = self.learner()
        if learner is None:
            return None
        visit = self._current()
        if visit is not None and visit.frozen is not None:
            return learner_block_data(visit.frozen)
        day = self._day(learner, self.clock())
        if self._block is not None and self._block[:2] == (id(learner), day):
            return self._block[2]
        try:
            frozen = freeze(learner.conn, self.content() or _NoLabels(), self.settings, subject=None, topic_id=None,
                            session_date=_iso_day(day), kcs_max=LEARNER_BLOCK_KCS,
                            misconceptions_max=LEARNER_BLOCK_MISCONCEPTIONS)  # fmt: skip
        except Exception:
            log.exception("study snapshot failed")
            return None
        block = learner_block_data(frozen)
        self._block = (id(learner), day, block)
        return block

    # --- activity (§19.3.2) ---------------------------------------------------------------------------------------

    def _tags(self, text: str) -> list[dict[str, Any]]:
        try:
            return kc_tags(self.content(), text)
        except Exception:  # tags are a hint for the Reflector; an unreadable pack leaves them empty
            log.exception("study kc tags failed")
            return []

    def _recorded(self, learner: Learner, refs: Sequence[str]) -> set[str]:
        if not refs:
            return set()
        marks = ", ".join("?" for _ in refs)
        query = (
            "SELECT json_extract(payload, '$.ref') FROM events WHERE type = 'study_activity' AND erased_at IS NULL"  # noqa: S608
            f" AND json_extract(payload, '$.ref') IN ({marks})"  # placeholders only
        )
        rows = learner.conn.execute(query, tuple(refs)).fetchall()
        return {str(r[0]) for r in rows}

    def record(self, activities: Sequence[dict[str, Any]], *, occurred_at: int | None = None,
               open_session: bool = True) -> int:  # fmt: skip
        """Record activities (`study_activity` payloads) in the open Study session; with `open_session` a closed
        screen's session is opened first (a Study command means the screen is open). Inside an open transaction
        they wait, session and all, until `flush`. Returns how many were recorded or queued; a ref already recorded
        counts nothing."""
        learner = self.learner()
        if learner is None or not activities:
            return 0
        when = self.clock() if occurred_at is None else occurred_at
        waiting = {w.payload["ref"] for w in self.pending}
        done = self._recorded(learner, [a["ref"] for a in activities]) | waiting
        new = []
        for activity in activities:
            if activity["ref"] not in done:
                done.add(activity["ref"])
                new.append(_Waiting(dict(activity), when, open_session))
        self.pending.extend(new)
        self.flush()
        return len(new)

    def flush(self) -> None:
        """Write what waits, once no transaction is open on the learner database (the seam after Study's own): the
        session is opened first when an activity may open it; one that may not, with no session open, waits for the
        next open's `sweep`."""
        learner = self.learner()
        if learner is None or not self.pending or learner.conn.in_transaction:
            return
        waiting, self.pending = self.pending, []
        if any(w.open_session for w in waiting):
            self.touch()
        visit = self._current()
        if visit is None:
            return
        fresh = self._recorded(learner, [w.payload["ref"] for w in waiting])
        events = []
        for item in waiting:
            if item.payload["ref"] in fresh:
                continue
            for tag in item.payload["kc_tags"]:
                subject = SUBJECT_CODES.get(tag["kc_id"].split(".")[1])
                if subject is not None:
                    visit.subjects[subject] += 1
            events.append(NewEvent("study_activity", item.payload, "engine", session_id=visit.session_id,
                                   occurred_at=item.occurred_at))  # fmt: skip
        if not events:
            return
        try:
            learner.record_batch(events)
        except Exception:
            self.failures += 1
            log.exception("study activity write failed")

    def quiz(self, attempt_id: str, items: Sequence[tuple[int, str, str]], material: str = "") -> int:
        """A submitted quiz: one `quiz_item` per item, `(index, item text, verdict)`; an omitted answer is
        `skipped`. The text (stem, key and explanation) only chooses the topic tags; it is never stored. One question is
        too little text to name a topic by itself, so `material`, the quiz's source, names the topics and the question
        names the skill within them. Without this, no quiz answer was ever linked to a skill."""
        return self.record([
            _payload("quiz_item", f"att:{attempt_id}:{index}", self._item_tags(text, material),
                     outcome=OUTCOMES.get(verdict))
            for index, text, verdict in items
        ])  # fmt: skip

    def _item_tags(self, text: str, material: str) -> list[dict[str, Any]]:
        tags = self._tags(text)
        if tags or not material:
            return tags
        try:
            content = self.content()
            topics = matching_topics(content, material)["items"]
            if not topics:
                return []
            return kc_tags(content, text, within=topics, require_overlap=True, min_overlap=2, close=0.8)
        except Exception:  # tags are a hint: an unreadable pack leaves them empty
            log.exception("study kc tags failed")
            return []

    def card(self, card_id: str, text: str, rating: str) -> int:
        """A Study card review, self-rated: one `card_review`. Study's own scheduler is unchanged."""
        return self.record([_payload("card_review", f"card:{card_id}", self._tags(text), rating=rating)])

    def aid(self, aid_id: str, kind: str, count: int | None, text: str, *, made_at: int | None = None,
            open_session: bool = False) -> int:  # fmt: skip
        """An aid that finished: one `aid_made`, recorded only while a Study session is open (else at the next
        open, by `sweep`)."""
        payload = _payload("aid_made", f"aid:{aid_id}", self._tags(text), aid_kind=kind,
                           count=count if count and count > 0 else None)  # fmt: skip
        return self.record([payload], occurred_at=made_at, open_session=open_session)

    def source_added(self, source_id: str, text: str, *, open_session: bool = False) -> int:
        """A source the learner added (SPEC §19.3.2, §20.3): one `source_added`, ref `src:<source_id>`, tagged
        from the title's topics; never the title or text. As an aid is, it is recorded while a Study session is
        open (the Study screen's `enter` opens one before any source is added there)."""
        payload = _payload("source_added", f"src:{source_id}", self._tags(text))
        return self.record([payload], open_session=open_session)

    def sweep(self, now: int | None = None) -> int:
        """Aids finished since the profile's first Study session while no Study session was open: recorded now,
        each at the time it was made (§19.3.2). Older aids are never back-filled."""
        learner = self.learner()
        if learner is None or self._current() is None:
            return 0
        conn = learner.conn
        first = conn.execute("SELECT min(started_at) FROM sessions WHERE mode = 'study'").fetchone()[0]
        if first is None:
            return 0
        rows = conn.execute("SELECT aid_id, body, created_at FROM study_aids WHERE created_at >= ? ORDER BY created_at",
                            (int(first),)).fetchall()  # fmt: skip
        recorded = self._recorded(learner, [f"aid:{r[0]}" for r in rows])
        count = 0
        for aid_id, body, created in rows:
            if f"aid:{aid_id}" in recorded:
                continue
            aid = json.loads(body)
            count += self.aid(aid_id, str(aid.get("kind")), aid_count(conn, aid), aid_text(aid), made_at=int(created))
        return count


def _payload(
    activity: str,
    ref: str,
    tags: list[dict[str, Any]],
    *,
    outcome: str | None = None,
    rating: str | None = None,
    aid_kind: str | None = None,
    count: int | None = None,
) -> dict[str, Any]:
    return {"activity": activity, "ref": ref, "kc_tags": tags, "outcome": outcome, "rating": rating,
            "aid_kind": aid_kind, "count": count}  # fmt: skip


def _iso_day(day: int) -> str:
    return (date(1970, 1, 1) + timedelta(days=day)).isoformat()


def aid_count(conn: Any, aid: Mapping[str, Any]) -> int | None:
    """The number of cards or questions an aid holds (the "(10)" of "study aid: flashcards (10)"), or None."""
    if aid.get("deck_id"):
        row = conn.execute("SELECT count(*) FROM study_flashcards WHERE deck_id = ?", (aid["deck_id"],)).fetchone()
        return int(row[0]) or None
    if aid.get("quiz_id"):
        row = conn.execute("SELECT body FROM study_quizzes WHERE quiz_id = ?", (aid["quiz_id"],)).fetchone()
        return len(json.loads(row[0])["view"]["items"]) or None if row else None
    return None


def aid_text(aid: Mapping[str, Any]) -> str:
    """What an aid is about, for its topic tags only: its title and first claims (never stored or sent)."""
    claims = [str(c.get("text", "")) for c in aid.get("claims", [])[:12] if isinstance(c, Mapping)]
    return "\n".join([str(aid.get("title", "")), *claims])
