"""The per-profile event log: append-only, validated, ordered by local sequence and by clock (DAT-04, DAT-16, LRN-01).

`append` validates the event in its contract (envelope) form and inserts it inside the caller's transaction, so a
projection updated in the same transaction can never run ahead of the log. The `events` triggers reject every change
except payload erasure, which `erase` performs. Events received from the student's other devices are applied with
`apply_remote`: the whole batch is refused (nothing stored, the clock untouched) when any stamp is too far ahead,
and applying a batch twice, or overlapping batches, stores each event once.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import cache, cached_property
from typing import Any

from agent_core.contracts import document, validate
from agent_core.store.generated.event_registry import SYNC_CLASS
from agent_core.store.hlc import HybridClock, Stamp
from agent_core.store.ids import new_ulid
from agent_core.store.sqlite import transaction

CONTRACT = "learning/learning-event"
SCHEMA_VERSION = "1.0"
COLUMNS = (
    "seq, event_id, profile_id, device_id, origin_seq, hlc, type, schema_version, source, sync_class,"
    " occurred_at, recorded_at, session_id, turn_id, kc_ids, payload, erased_at"
)
MERGE_ORDER = "hlc, device_id, origin_seq"


def iso_ms(ms: int) -> str:
    """UTC milliseconds as the contracts' date-time (millisecond precision, Z)."""
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def ms_from_iso(text: str) -> int:
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return int(moment.timestamp() * 1000 + (0.5 if moment.timestamp() >= 0 else -0.5))


@dataclass(frozen=True)
class StoredEvent:
    seq: int
    event_id: str
    profile_id: str
    device_id: str
    origin_seq: int
    hlc: str
    type: str
    schema_version: str
    source: str
    sync_class: str
    occurred_at: int
    recorded_at: int
    session_id: str | None
    turn_id: str | None
    kc_ids: list[Any] | None
    payload: dict[str, Any]
    erased_at: int | None

    @staticmethod
    def from_row(row: Sequence[Any]) -> StoredEvent:
        values = list(row)
        values[14] = json.loads(values[14]) if values[14] is not None else None
        values[15] = json.loads(values[15])
        return StoredEvent(*values)

    @property
    def erased(self) -> bool:
        return self.erased_at is not None

    @cached_property
    def stamp(self) -> Stamp:
        # StoredEvent is immutable. Re-marks/migrations construct a new event, so its timestamp cannot inherit
        # another event's cached value; repeated projection reads need not parse the same HLC again.
        return Stamp.parse(self.hlc)

    @property
    def order(self) -> tuple[str, str, int]:
        return (self.hlc, self.device_id, self.origin_seq)

    def envelope(self) -> dict[str, Any]:
        """The contract form: `origin_seq` is the envelope's `seq`, times are date-time strings."""
        out: dict[str, Any] = {
            "event_id": self.event_id,
            "profile_id": self.profile_id,
            "seq": self.origin_seq,
            "device_id": self.device_id,
            "hlc": self.hlc,
            "occurred_at": iso_ms(self.occurred_at),
            "recorded_at": iso_ms(self.recorded_at),
            "source": self.source,
            "schema_version": self.schema_version,
            "type": self.type,
            "payload": self.payload,
        }
        if self.session_id is not None:
            out["session_id"] = self.session_id
        return out


@cache
def _branches() -> dict[str, str]:
    """The contract's `oneOf` branch for each event type, and for erased payloads."""
    out = {}
    for index, branch in enumerate(document(CONTRACT)["oneOf"]):
        kind = branch["properties"]["type"]
        out[kind.get("const", "erased")] = f"{CONTRACT}#/oneOf/{index}"
    return out


def validate_event(envelope: dict[str, Any]) -> None:
    """Validate against the contract. The branches are told apart by `type` (and the erased branch by its payload),
    so checking the one branch that can match gives the `oneOf` result at a fraction of the cost."""
    branches = _branches()
    payload = envelope.get("payload")
    erased = isinstance(payload, dict) and payload.get("erased") is True
    name = branches.get("erased" if erased else str(envelope.get("type")))
    validate(name or CONTRACT, envelope)


def kc_ids_of(payload: dict[str, Any]) -> list[Any] | None:
    """The knowledge components an event touches, for the `kc_ids` index column."""
    if isinstance(payload.get("kc_ids"), list):
        return list(payload["kc_ids"])
    if isinstance(payload.get("kc_id"), str):
        return [payload["kc_id"]]
    return None


class EventLog:
    def __init__(self, conn: sqlite3.Connection, profile_id: str, device_id: str, clock: HybridClock) -> None:
        self.conn = conn
        self.profile_id = profile_id
        self.device_id = device_id
        self.clock = clock

    @classmethod
    def open(
        cls, conn: sqlite3.Connection, profile_id: str, device_id: str, wall_ms: Callable[[], int], max_skew_ms: int
    ) -> EventLog:
        """The log of an open learner database; the clock resumes above every stamp already stored."""
        row = conn.execute("SELECT max(hlc) FROM events").fetchone()
        last = Stamp.parse(row[0]) if row[0] is not None else Stamp(0, 0)
        return cls(conn, profile_id, device_id, HybridClock(wall_ms, max_skew_ms, last))

    def append(
        self,
        type_: str,
        payload: dict[str, Any],
        *,
        source: str,
        session_id: str | None = None,
        turn_id: str | None = None,
        occurred_at: int | None = None,
    ) -> StoredEvent:
        """Validate and append one local event, inside the caller's transaction if one is open."""
        if self.conn.in_transaction:
            return self._append(type_, payload, source, session_id, turn_id, occurred_at)
        with transaction(self.conn):
            return self._append(type_, payload, source, session_id, turn_id, occurred_at)

    def _append(
        self,
        type_: str,
        payload: dict[str, Any],
        source: str,
        session_id: str | None,
        turn_id: str | None,
        occurred_at: int | None,
    ) -> StoredEvent:
        now = self.clock.wall_ms()
        origin_seq = self.conn.execute(
            "SELECT coalesce(max(origin_seq), 0) + 1 FROM events WHERE device_id = ?", (self.device_id,)
        ).fetchone()[0]
        event = StoredEvent(
            seq=0,
            event_id=new_ulid(now),
            profile_id=self.profile_id,
            device_id=self.device_id,
            origin_seq=origin_seq,
            hlc=self.clock.now().encode(),
            type=type_,
            schema_version=SCHEMA_VERSION,
            source=source,
            sync_class=SYNC_CLASS[type_],
            occurred_at=occurred_at if occurred_at is not None else now,
            recorded_at=now,
            session_id=session_id,
            turn_id=turn_id,
            kc_ids=kc_ids_of(payload),
            payload=payload,
            erased_at=None,
        )
        validate_event(event.envelope())
        return self._insert(event, ignore=False)

    def _insert(self, event: StoredEvent, *, ignore: bool) -> StoredEvent:
        verb = "INSERT OR IGNORE" if ignore else "INSERT"
        cursor = self.conn.execute(
            f"{verb} INTO events (event_id, profile_id, device_id, origin_seq, hlc, type, schema_version, source,"
            " sync_class, occurred_at, recorded_at, session_id, turn_id, kc_ids, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.profile_id,
                event.device_id,
                event.origin_seq,
                event.hlc,
                event.type,
                event.schema_version,
                event.source,
                event.sync_class,
                event.occurred_at,
                event.recorded_at,
                event.session_id,
                event.turn_id,
                json.dumps(event.kc_ids) if event.kc_ids is not None else None,
                json.dumps(event.payload, separators=(",", ":"), ensure_ascii=False),
            ),
        )
        if cursor.rowcount == 0:
            return event
        return replace(event, seq=int(cursor.lastrowid or 0))

    def apply_remote(
        self, batch: Sequence[dict[str, Any]], on_stored: Callable[[StoredEvent], None] | None = None
    ) -> list[StoredEvent]:
        """Store events recorded on the student's other devices, exactly as recorded. Returns the newly stored ones.

        Every envelope is validated and every stamp admitted before anything changes: a refused batch (an invalid
        event, another profile's event, or a stamp too far ahead) leaves the log and the clock as they were."""
        stamps = []
        for envelope in batch:
            validate_event(envelope)
            if envelope["profile_id"] != self.profile_id:
                raise ValueError("an event of another profile")
            stamp = Stamp.parse(envelope["hlc"])
            self.clock.admit(stamp)
            stamps.append(stamp)
        probe = HybridClock(self.clock.wall_ms, self.clock.max_skew_ms, self.clock.last)
        for stamp in stamps:
            probe.receive(stamp)  # an out-of-range merge fails here, before anything is stored
        now = self.clock.wall_ms()
        stored = []
        with transaction(self.conn):
            for envelope in batch:
                event = StoredEvent(
                    seq=0,
                    event_id=envelope["event_id"],
                    profile_id=envelope["profile_id"],
                    device_id=envelope["device_id"],
                    origin_seq=envelope["seq"],
                    hlc=envelope["hlc"],
                    type=envelope["type"],
                    schema_version=envelope["schema_version"],
                    source=envelope["source"],
                    sync_class=SYNC_CLASS[envelope["type"]],
                    occurred_at=ms_from_iso(envelope["occurred_at"]),
                    recorded_at=ms_from_iso(envelope["recorded_at"]) if "recorded_at" in envelope else now,
                    session_id=envelope.get("session_id"),
                    turn_id=None,
                    kc_ids=kc_ids_of(envelope["payload"]),
                    payload=envelope["payload"],
                    erased_at=None,
                )
                inserted = self._insert(event, ignore=True)
                if inserted.seq:
                    stored.append(inserted)
                    if on_stored is not None:
                        on_stored(inserted)  # projections change in the transaction that stores the event
        self.clock.last = max(self.clock.last, probe.last)
        return stored

    def erase(self, event_id: str, now_ms: int) -> None:
        """Replace one event's payload with the erasure tombstone; id, type, sequence and stamp stay (DAT-09)."""
        tombstone = json.dumps({"erased": True, "erased_at": iso_ms(now_ms)}, separators=(",", ":"))
        self.conn.execute(
            "UPDATE events SET payload = ?, erased_at = ? WHERE event_id = ?", (tombstone, now_ms, event_id)
        )

    def get(self, event_id: str) -> StoredEvent | None:
        row = self.conn.execute(f"SELECT {COLUMNS} FROM events WHERE event_id = ?", (event_id,)).fetchone()  # noqa: S608
        return StoredEvent.from_row(row) if row else None

    def by_seq(self, after: int = 0) -> Iterator[StoredEvent]:
        """Events in local order."""
        rows = self.conn.execute(f"SELECT {COLUMNS} FROM events WHERE seq > ? ORDER BY seq", (after,))  # noqa: S608
        for row in rows:
            yield StoredEvent.from_row(row)

    def merged(self) -> Iterator[StoredEvent]:
        """Events in the cross-device order (hlc, device_id, origin_seq) (LRN-39)."""
        for row in self.conn.execute(f"SELECT {COLUMNS} FROM events ORDER BY {MERGE_ORDER}"):  # noqa: S608
            yield StoredEvent.from_row(row)
