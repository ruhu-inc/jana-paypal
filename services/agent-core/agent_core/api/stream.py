"""Sequenced delivery of a session's events: replay ring, per-connection send queues and SSE framing (API-01, API-05,
API-16, API-19, API-21).

- `Ring` keeps the serialised envelopes of one session, bounded by events and bytes. An event too large to retain
  raises the truncation watermark; a cursor below it, or before the oldest retained event, cannot resume.
- `RingPool` holds every session's ring under a process-wide byte cap and session count; the least recently active
  ring is dropped first (its watermark becomes its last seq, so its clients reload the snapshot).
- `Connection` is one client's bounded send queue. When it fills, queued deltas of the same part merge and queued
  whole-record updates for the same key are replaced by the newer one; lifecycle events are never dropped. If it
  still overflows the connection is marked lagging: the writer sends `stream.closing` and closes, and the client
  resumes from its cursor. A slow client never slows the engine.
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

PROTOCOL = {"major": 1, "minor": 1}
NEVER_DROPPED = frozenset({"part.start", "part.end", "error", "egress.notice"})
DELTAS = frozenset({"text.delta", "reasoning.delta"})


def frame(envelope: dict[str, Any]) -> bytes:
    """One SSE frame: `id: <epoch>:<seq>`, `event: <type>`, `data: <envelope>` (API-01)."""
    data = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return f"id: {envelope['epoch']}:{envelope['seq']}\nevent: {envelope['event']['type']}\ndata: {data}\n\n".encode()


def control(payload: dict[str, Any]) -> bytes:
    """A control frame: unsequenced, connection-scoped, `event: control` and no id (API-05)."""
    return f"event: control\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


HEARTBEAT = b": hb\n\n"


def parse_cursor(value: str | None) -> tuple[str, int] | None:
    """`<epoch>:<seq>` from `Last-Event-ID` or the `cursor` query; None if absent. Raises ValueError if malformed."""
    if value is None or value == "":
        return None
    epoch, sep, seq = value.rpartition(":")
    if not sep or not epoch or not seq.isdigit():
        raise ValueError(value)
    return epoch, int(seq)


@dataclass
class Ring:
    max_events: int
    max_bytes: int
    max_event_bytes: int
    entries: deque[tuple[int, bytes]] = field(default_factory=deque)
    size: int = 0
    watermark: int = 0  # every seq at or below this may be missing from the ring
    last_seq: int = 0

    def add(self, seq: int, data: bytes) -> int:
        """Store one frame; returns the bytes freed by eviction (negative growth is reported to the pool)."""
        self.last_seq = seq
        if len(data) > self.max_event_bytes:
            self.watermark = seq
            return 0
        self.entries.append((seq, data))
        self.size += len(data)
        freed = 0
        while len(self.entries) > self.max_events or self.size > self.max_bytes:
            freed += self.evict_one()
        return freed

    def evict_one(self) -> int:
        seq, data = self.entries.popleft()
        self.size -= len(data)
        self.watermark = max(self.watermark, seq)
        return len(data)

    def clear(self) -> int:
        freed = self.size
        self.entries.clear()
        self.size = 0
        self.watermark = self.last_seq
        return freed

    def after(self, seq: int) -> list[bytes] | None:
        """Every frame after `seq`, or None when that cannot be done from the ring (the cursor expired)."""
        if seq > self.last_seq or seq < self.watermark:
            return None
        return [data for s, data in self.entries if s > seq]


@dataclass
class RingPool:
    max_bytes: int
    max_sessions: int
    ring_events: int
    ring_bytes: int
    event_bytes: int
    rings: OrderedDict[str, Ring] = field(default_factory=OrderedDict)
    total: int = 0

    def ring(self, session_id: str) -> Ring:
        ring = self.rings.get(session_id)
        if ring is None:
            ring = Ring(self.ring_events, self.ring_bytes, self.event_bytes)
            self.rings[session_id] = ring
        self.rings.move_to_end(session_id)
        return ring

    def add(self, session_id: str, seq: int, data: bytes) -> None:
        ring = self.ring(session_id)
        before = ring.size
        ring.add(seq, data)
        self.total += ring.size - before
        while (self.total > self.max_bytes or len(self.rings) > self.max_sessions) and len(self.rings) > 1:
            oldest_id, oldest = next(iter(self.rings.items()))
            if oldest_id == session_id:
                break
            self.total -= oldest.clear()
            if len(self.rings) > self.max_sessions:
                del self.rings[oldest_id]
        while self.total > self.max_bytes and ring.entries:
            self.total -= ring.evict_one()

    def rewrite(self, session_id: str, seq: int, data: bytes) -> bool:
        """Replace the stored frame of one event (a forgotten note's announcement loses its text, LRN-37). Returns
        whether the ring held it."""
        ring = self.rings.get(session_id)
        if ring is None:
            return False
        for index, (held, old) in enumerate(ring.entries):
            if held == seq:
                ring.entries[index] = (seq, data)
                ring.size += len(data) - len(old)
                self.total += len(data) - len(old)
                return True
        return False

    def forget(self, session_id: str) -> None:
        ring = self.rings.pop(session_id, None)
        if ring is not None:
            self.total -= ring.size


@dataclass
class _Item:
    seq: int
    type: str
    key: tuple[Any, ...] | None  # merge or replace key, None for events that are never merged
    envelope: dict[str, Any]
    size: int


def _merge_key(event: dict[str, Any]) -> tuple[Any, ...] | None:
    kind = event["type"]
    if kind in DELTAS:
        return (kind, event["part_id"])
    if kind == "board.step":
        return (kind, event["board_id"], event["step_id"])
    if kind == "tool.state":
        return (kind, event["call_id"])
    if kind in ("status", "board.knowns", "session.updated"):
        return (kind,)
    return None


class Connection:
    """One client's bounded send queue (API-21)."""

    def __init__(self, max_events: int, max_bytes: int) -> None:
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.items: deque[_Item | bytes] = deque()  # bytes: a pre-framed replay frame
        self.size = 0
        self.lagging = False
        self.closed = False
        self.wake = asyncio.Event()

    def put_frames(self, frames: list[bytes]) -> None:
        for data in frames:
            self.items.append(data)
            self.size += len(data)
        self.wake.set()

    def put(self, envelope: dict[str, Any]) -> None:
        if self.lagging or self.closed:
            return
        event = envelope["event"]
        size = len(json.dumps(envelope, separators=(",", ":"))) + 64
        self.items.append(_Item(envelope["seq"], event["type"], _merge_key(event), envelope, size))
        self.size += size
        if len(self.items) > self.max_events or self.size > self.max_bytes:
            self._relieve()
            if len(self.items) > self.max_events or self.size > self.max_bytes:
                self.lagging = True
        self.wake.set()

    def _relieve(self) -> None:
        """Merge deltas of the same part and keep only the newest whole-record update per key. A merged or replaced
        item keeps the later position and seq, so a client's cursor never skips content it has not received."""
        kept: deque[_Item | bytes] = deque()
        latest: dict[tuple[Any, ...], _Item] = {}
        for item in reversed(self.items):
            if isinstance(item, bytes):  # a replay frame: sent as it is, and a barrier
                latest.clear()
                kept.appendleft(item)
                continue
            if item.type in DELTAS:
                newer = latest.get(item.key) if item.key else None
                if newer is not None:
                    merged = item.envelope["event"]["delta"] + newer.envelope["event"]["delta"]
                    newer.envelope = {**newer.envelope, "event": {**newer.envelope["event"], "delta": merged}}
                    newer.size += item.size
                    continue
                if item.key:
                    latest[item.key] = item
                kept.appendleft(item)
                continue
            # Any other event is a barrier for deltas: text never moves across it (API-18 order).
            latest = {k: v for k, v in latest.items() if k[0] not in DELTAS}
            if item.type in ("board.await", "board.reset"):  # it reads the board's steps: no replacement across it
                board_id = item.envelope["event"]["board_id"]
                latest = {k: v for k, v in latest.items() if not (k[0] == "board.step" and k[1] == board_id)}
            if item.key is not None and item.type not in NEVER_DROPPED:
                newer = latest.get(item.key)
                # A settled step clears the board's await and a later "awaiting" one does not restore it: keep both.
                clears = (item.type == "board.step" and newer is not None
                          and newer.envelope["event"]["status"] == "awaiting"
                          and item.envelope["event"]["status"] != "awaiting")  # fmt: skip
                if newer is not None and not clears:
                    continue  # an older whole-record update superseded by a newer one
                latest[item.key] = item
            kept.appendleft(item)
        self.items = kept
        self.size = sum(len(i) if isinstance(i, bytes) else i.size for i in kept)

    def close(self) -> None:
        self.closed = True
        self.wake.set()

    async def frames(self, heartbeat_s: float) -> AsyncIterator[bytes]:
        """The bytes to write: queued frames in order, `: hb` while idle, `stream.closing` when lagging."""
        while True:
            while self.items:
                if self.lagging:
                    self.items.clear()
                    yield control({"type": "stream.closing", "reason": "lagging"})
                    return
                item = self.items.popleft()
                if isinstance(item, bytes):
                    self.size -= len(item)
                    yield item
                else:
                    self.size -= item.size
                    yield frame(item.envelope)
            if self.lagging:
                yield control({"type": "stream.closing", "reason": "lagging"})
                return
            if self.closed:
                return
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), heartbeat_s)
            except TimeoutError:
                yield HEARTBEAT
