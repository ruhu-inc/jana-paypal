"""The learner's event log with its projections: every fact is appended, then folded, in one transaction (LRN-01).

Writes go through `record_batch` (or `record`, a batch of one): the events are appended and folded, then an optional
`extra` step runs on the same connection, all in one transaction that commits or rolls back as a whole. The
transaction never spans an `await`: `extra` is a plain function, and a coroutine handed in its place is refused. A
transaction is never nested; a batch started while one is open on the connection is refused.

The episodic layer has a single writer (MEM-01): events that write or delete a note, or correct one, are accepted
only with the `NOTES_WRITER` capability, which `agent_core.memory.write` alone holds. Two paths that append below this
service stay as they are: the retention job's expiry of old notes (DAT-08) and events received from the student's
other devices (`receive`), which were checked where they were recorded.
"""

from __future__ import annotations

import inspect
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from agent_core.learner.mastery import MasteryParams
from agent_core.learner.projections import ContentIndex, Projection, Projector
from agent_core.store.events import EventLog, StoredEvent
from agent_core.store.sqlite import transaction


@dataclass(frozen=True, eq=False)
class Capability:
    """An unforgeable write permission: compared by identity, so only the object itself grants it."""

    name: str


# Held only by agent_core.memory.write (MEM-01); importing it anywhere else breaks the single-writer rule.
NOTES_WRITER = Capability("notes_writer")
NOTE_EVENTS = frozenset({"note_written", "note_deleted"})


def needs_notes_writer(type_: str, payload: dict[str, Any]) -> bool:
    """Whether an event writes the episodic layer: a note written or deleted, or a correction aimed at a note."""
    return type_ in NOTE_EVENTS or (type_ == "correction" and payload.get("target") == "note")


class WriterRefused(PermissionError):
    """An event of a layer whose writer capability the caller does not hold (MEM-01)."""


class NestedTransaction(RuntimeError):
    """A batch was started while a transaction was already open on the connection."""


@dataclass(frozen=True)
class NewEvent:
    """One event to append: its type, payload and envelope fields (the log assigns ids, sequence and stamps)."""

    type: str
    payload: dict[str, Any]
    source: str
    session_id: str | None = None
    turn_id: str | None = None
    occurred_at: int | None = None


# Runs inside the batch's transaction after every event is folded; gets the connection and the stored events.
Extra = Callable[[sqlite3.Connection, list[StoredEvent]], object]


class Learner:
    def __init__(self, log: EventLog, projector: Projector) -> None:
        self.log = log
        self.projector = projector

    @classmethod
    def open(
        cls,
        conn: sqlite3.Connection,
        *,
        profile_id: str,
        device_id: str,
        wall_ms: Callable[[], int],
        max_skew_ms: int,
        params: MasteryParams,
        content: ContentIndex | None = None,
        projections: Sequence[Projection] | None = None,
    ) -> Learner:
        log = EventLog.open(conn, profile_id, device_id, wall_ms, max_skew_ms)
        return cls(log, Projector(conn, params, content, projections, wall_ms))

    @property
    def conn(self) -> sqlite3.Connection:
        return self.log.conn

    def record(
        self, type_: str, payload: dict[str, Any], *, writer: Capability | None = None, **envelope: Any
    ) -> StoredEvent:
        return self.record_batch([NewEvent(type_, payload, **envelope)], writer=writer)[0]

    def record_batch(
        self, events: Sequence[NewEvent], *, extra: Extra | None = None, writer: Capability | None = None
    ) -> list[StoredEvent]:
        """Append and fold `events` in order, then run `extra`, in one transaction (LRN-01, MEM-01).

        Every event is checked against the writer rule before anything is written. A failure anywhere (an invalid
        event, a refused writer, an error in `extra`) rolls the whole batch back."""
        for event in events:
            if needs_notes_writer(event.type, event.payload) and writer is not NOTES_WRITER:
                raise WriterRefused(f"{event.type} needs the notes writer (MEM-01)")
        if self.conn.in_transaction:
            raise NestedTransaction("a learner write was started inside an open transaction")
        stored: list[StoredEvent] = []
        with transaction(self.conn):
            for event in events:
                appended = self.log.append(
                    event.type,
                    event.payload,
                    source=event.source,
                    session_id=event.session_id,
                    turn_id=event.turn_id,
                    occurred_at=event.occurred_at,
                )
                self.projector.apply(appended)
                stored.append(appended)
            if extra is not None:
                result = extra(self.conn, stored)
                if inspect.isawaitable(result):
                    close = getattr(result, "close", None)
                    if callable(close):
                        close()  # never awaited: the coroutine is discarded, not left pending
                    raise TypeError("a learner write may not await inside its transaction")
        return stored

    def supersede(self, events: Sequence[NewEvent], *, extra: Extra | None = None) -> list[StoredEvent]:
        """Re-mark (ASM-23): append verdicts that each name the verdict they supersede, with the misconception
        evidence the corrected marking found on those attempts (CUR-36), then rebuild the projections once from the
        log and run `extra`, all in one transaction. The original verdicts are never edited; every reader takes each
        superseded verdict as its replacement and each attempt's evidence from its latest marking
        (`store.supersession`)."""
        remarked = {e.payload.get("attempt_id") for e in events if e.type == "verdict"}
        for event in events:
            if event.type == "verdict" and event.payload.get("supersedes"):
                continue
            if event.type == "misconception_evidence" and event.payload.get("attempt_id") in remarked:
                continue
            raise ValueError("ASM-23: a re-mark is a verdict naming the verdict it supersedes, with its evidence")
        if self.conn.in_transaction:
            raise NestedTransaction("a learner write was started inside an open transaction")
        with transaction(self.conn):
            stored = [
                self.log.append(e.type, e.payload, source=e.source, session_id=e.session_id, turn_id=e.turn_id,
                                occurred_at=e.occurred_at)
                for e in events
            ]  # fmt: skip
            if stored:
                self.projector.replay_all(self.projector.projections)
            if extra is not None:
                result = extra(self.conn, stored)
                if inspect.isawaitable(result):
                    close = getattr(result, "close", None)
                    if callable(close):
                        close()
                    raise TypeError("a learner write may not await inside its transaction")
        return stored

    def erase(self, event_id: str, now_ms: int) -> None:
        """Erase one event's payload (DAT-09) and replay the projections, which may have used it."""
        with transaction(self.conn):
            self.log.erase(event_id, now_ms)
            self.projector.replay_all(self.projector.projections)

    def receive(self, batch: Sequence[dict[str, Any]]) -> list[StoredEvent]:
        """Events from the student's other devices: stored once each, folded (or replayed) as they land."""
        return self.log.apply_remote(batch, on_stored=self.projector.apply)
