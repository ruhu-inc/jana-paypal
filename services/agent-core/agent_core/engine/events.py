"""The session event stream as the engine writes it (API-06, API-08, API-10; providers built early for M5).

Every event is wrapped in the envelope of the events contract and validated before it is appended. Parts follow
the lifecycle of API-08: started, streaming, then exactly one `part.end`; every part opened in a turn is ended
before `turn.end`. Only accepted content ever becomes an event: the engine buffers candidates privately and emits
nothing for a rejected one (API-10).

Deltas are coalesced per part (API-18): `text.delta` and `reasoning.delta` wait in a buffer that is flushed when it
reaches `flush_bytes`, when any other event is emitted (so order is kept), and on `flush()`, which the API calls on
its flush timer. Coalesced deltas are what the frames, the replay ring and every client receive. `sink` sees each
sequenced envelope as it is appended."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_core.contracts import errors as contract_errors
from agent_core.trace import new_ulid

CONTRACT = "api/events"


def new_id(prefix: str) -> str:
    return f"{prefix}_{new_ulid()}"


class EventContractError(AssertionError):
    pass


@dataclass
class EventStream:
    """One session's event log. `frames` is what the client receives (and the replay ring holds)."""

    session_id: str
    epoch: str
    clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)
    validate: bool = True
    frames: list[dict[str, Any]] = field(default_factory=list)
    turn_id: str | None = None
    flush_bytes: int = 4096
    sink: Callable[[dict[str, Any]], None] | None = None
    # Called with a part's kind before it opens; raises to refuse it (AGT-01: only the Coach addresses the student).
    part_guard: Callable[[str], None] | None = None
    seq: int = 0
    _open: dict[str, dict[str, Any]] = field(default_factory=dict)
    _index: int = 0
    _pending: dict[tuple[str, str], list[str]] = field(default_factory=dict)  # (type, part_id) -> deltas
    _pending_bytes: dict[tuple[str, str], int] = field(default_factory=dict)

    def emit(self, event: dict[str, Any], *, turn_id: str | None = None) -> dict[str, Any]:
        """Append one event; `turn_id` places an out-of-turn event in the turn it answers (a Reflector outcome)."""
        self.flush()
        return self._append(event, turn_id)

    def delta(self, kind: str, part_id: str, delta: str) -> None:
        """Buffer a `text.delta` or `reasoning.delta` for coalescing (API-18)."""
        key = (kind, part_id)
        self._pending.setdefault(key, []).append(delta)
        self._pending_bytes[key] = self._pending_bytes.get(key, 0) + len(delta.encode())
        if self._pending_bytes[key] >= self.flush_bytes:
            self._flush_one(key)

    def flush(self) -> None:
        for key in list(self._pending):
            self._flush_one(key)

    def _flush_one(self, key: tuple[str, str]) -> None:
        parts = self._pending.pop(key, None)
        self._pending_bytes.pop(key, None)
        if parts:
            self._append({"type": key[0], "part_id": key[1], "delta": "".join(parts)})

    def _append(self, event: dict[str, Any], turn_id: str | None = None) -> dict[str, Any]:
        self.seq += 1
        envelope = {
            "v": 1,
            "channel": "session",
            "seq": self.seq,
            "epoch": self.epoch,
            "session_id": self.session_id,
            "turn_id": turn_id if turn_id is not None else self.turn_id,
            "ts": self.clock_ms(),
            "event": event,
        }
        if self.validate:
            problems = contract_errors(CONTRACT, envelope)
            if problems:
                raise EventContractError(f"{event.get('type')}: {problems[0]}")
        self.frames.append(envelope)
        if event["type"] == "turn.end":
            self.turn_id = None  # later events (a title change, a notice) belong to no turn
        if self.sink is not None:
            self.sink(envelope)
        return envelope

    # --- parts (API-08) ------------------------------------------------------------------------------------

    def start_part(self, kind: str) -> str:
        if self.part_guard is not None:
            self.part_guard(kind)
        part_id = new_id("prt")
        self.emit({"type": "part.start", "part_id": part_id, "kind": kind, "index": self._index})
        self._index += 1
        self._open[part_id] = {"kind": kind, "started": time.monotonic(), "text": ""}
        return part_id

    def text(self, part_id: str, delta: str) -> None:
        self._open[part_id]["text"] += delta
        self.delta("text.delta", part_id, delta)

    def reset(self, part_id: str) -> None:
        """`text.reset` clears only an empty placeholder; after an accepted delta it is refused (API-10)."""
        if self._open[part_id]["text"]:
            raise EventContractError("text.reset after an accepted delta is not allowed")
        self.emit({"type": "text.reset", "part_id": part_id, "reason": "regenerating"})

    def end_part(self, part_id: str, status: str = "complete") -> None:
        held = self._open.pop(part_id)
        duration = int((time.monotonic() - held["started"]) * 1000)
        self.emit({"type": "part.end", "part_id": part_id, "status": status, "duration_ms": duration})

    def end_open_parts(self, status: str) -> list[str]:
        ended = list(self._open)
        for part_id in ended:
            self.end_part(part_id, status)
        return ended

    def open_parts(self) -> list[str]:
        return list(self._open)

    def begin_turn(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self._index = 0

    # --- views ---------------------------------------------------------------------------------------------

    def scrub_saved(self, note_id: str) -> list[int]:
        """A forgotten note's `memory.saved` frames lose their text (LRN-37); the frames stay, so sequence numbers and
        replay are unchanged. Returns the sequence numbers changed. The envelope objects are changed in place, so a
        connection's queue that holds one sends it without the text too."""
        changed = []
        for envelope in self.frames:
            event = envelope["event"]
            if event["type"] == "memory.saved" and event.get("note_id") == note_id and event.get("text"):
                event["text"] = ""
                changed.append(int(envelope["seq"]))
        return changed

    def frame_at(self, seq: int) -> dict[str, Any]:
        return next(f for f in self.frames if f["seq"] == seq)

    def of_turn(self, turn_id: str) -> list[dict[str, Any]]:
        self.flush()
        return [f["event"] for f in self.frames if f["turn_id"] == turn_id]

    def student_text(self, turn_id: str) -> str:
        return "".join(e["delta"] for e in self.of_turn(turn_id) if e["type"] == "text.delta")
