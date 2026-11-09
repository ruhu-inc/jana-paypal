"""The app channel (API-22): `GET /v1/events` streams app-wide events with its own sequence.

App events (TABLE API-06a, channel `app`: connectivity status, model runtime, model downloads, pack installs, jobs,
settings changes, captures and app-level egress notices) are numbered in one sequence per core epoch and framed like
session events (`id: <epoch>:<seq>`), kept in a bounded replay ring and fanned out to each connection's bounded queue,
with the same replay, heartbeat and reconnect rules as a session stream (API-16, API-17, API-19, API-21). A cursor from
another epoch, or one older than the ring holds, is refused so the client reloads its app state.

The channel needs only the launch token and carries no profile data beyond profile ids and setting keys: every event
is validated against the contract's `app_event` before it is sequenced, so a field the contract does not list never
leaves the core.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from agent_core.api.errors import ApiError
from agent_core.api.stream import PROTOCOL, Connection, Ring, control, frame, parse_cursor
from agent_core.config import Settings
from agent_core.contracts import errors as contract_errors

APP_EVENT = "api/events#/$defs/app_event"


class AppChannel:
    def __init__(self, settings: Settings, epoch: str, clock_ms: Callable[[], int], server_version: str) -> None:
        self.settings = settings
        self.epoch = epoch
        self.clock_ms = clock_ms
        self.server_version = server_version
        self.seq = 0
        self.ring = Ring(settings.api_ring_events, int(settings.api_ring_mb * 1024 * 1024),
                         int(settings.api_ring_event_kb * 1024))  # fmt: skip
        self.connections: set[Connection] = set()
        self.rejected: list[str] = []  # event types refused by the contract check (a programming error, logged)

    def publish(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Sequence, store and fan out one app event; an event outside the contract is dropped and recorded."""
        problems = contract_errors(APP_EVENT, event)
        if problems:
            self.rejected.append(str(event.get("type")))
            return None
        self.seq += 1
        envelope = {"v": 1, "channel": "app", "seq": self.seq, "epoch": self.epoch, "session_id": None,
                    "turn_id": None, "ts": self.clock_ms(), "event": event}  # fmt: skip
        self.ring.add(self.seq, frame(envelope))
        for connection in list(self.connections):
            connection.put(envelope)
        return envelope

    def open(self, cursor_text: str | None) -> tuple[Connection, int, int | None]:
        """Register a connection at a cursor (API-17); 409 when it cannot resume."""
        try:
            cursor = parse_cursor(cursor_text)
        except ValueError:
            raise ApiError("invalid_cursor", "cursor must be <epoch>:<seq>") from None
        epoch, seq = cursor if cursor is not None else (self.epoch, self.seq)
        if epoch != self.epoch:
            raise ApiError("epoch_mismatch", "the core restarted; reload the app state", details={"epoch": self.epoch})
        replay = [] if seq == self.seq else self.ring.after(seq)
        if replay is None:
            raise ApiError("cursor_expired", "events after this cursor are no longer kept; reload the app state")
        connection = Connection(self.settings.api_queue_events, int(self.settings.api_queue_mb * 1024 * 1024))
        connection.put_frames(replay)
        self.connections.add(connection)
        return connection, len(replay), seq if cursor is not None else None

    async def stream(self, connection: Connection, replayed: int, resumed_from: int | None) -> AsyncIterator[bytes]:
        try:
            yield control({"type": "stream.hello", "protocol": PROTOCOL, "server_version": self.server_version,
                           "epoch": self.epoch, "channel": "app", "session_id": None, "resumed_from": resumed_from,
                           "replayed": replayed})  # fmt: skip
            async for data in connection.frames(self.settings.api_heartbeat_s):
                yield data
        finally:
            self.connections.discard(connection)

    def close(self) -> None:
        for connection in list(self.connections):
            connection.close()
