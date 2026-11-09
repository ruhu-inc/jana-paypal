"""Cloud voice: the core's online Realtime connection (VOC-28) and its side of the desktop voice tunnel.

The voice bridge talks to whatever `RealtimeConnection` it is given; this module gives it one to our hosted voice
endpoint, with the same frames as the local sidecar's (VOC-25, VOC-27), so neither the bridge nor the UI changes. The
connection is the `cloud_voice` feature of the connectivity layer (ARC-06, ARC-07): it opens only while the switch is
on, the open profile holds an active `cloud_voice` consent and a subscription covers it, and its first-use notice
was accepted (ARC-10, PRV-05). Before the socket is dialled the session stream hears `egress.notice started` naming
`audio` and `transcript`, so no audio frame leaves before the student is told (VOC-28); the ledger records the
connection, and `ended` follows its close.

Transport (SEC-31): TLS 1.2 or later against the bundled CA set, no proxy, no compression, and the server's chain is
checked against our pinned keys right after the handshake, before the upgrade request (which carries the access
token) is written.

The language-model slot (VOC-31): the endpoint forwards each chat-completions request its pipeline would send to the
engine adapter as a `jana.voice_llm.request` frame. This connection answers it from the core's own adapter, in
process, and streams back the adapter's chunks: the tutor turn, the learner model and every gate stay on the device;
only the accepted spoken words go up, to be spoken. Tunnel frames never reach the bridge."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sqlite3
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from websockets.asyncio.client import ClientConnection, connect

from agent_core.net.client import _chain, spki_pin, tls_context
from agent_core.net.policy import EgressRefused
from agent_core.net.voice import VoiceTransportError

FEATURE = "cloud_voice"
PAYLOAD = "voice_stream"
PATH = "/v1/voice/realtime"
REQUEST_ID = re.compile(r"^vlr_[0-9a-f]{24}$")
MAX_TUNNEL = 8  # forwarded requests in flight at once
# The session subjects the endpoint may be told (`?subject=`): curriculum names only, never learner data. They order
# speech to text's vocabulary bias (the subject's glossary terms first).
SUBJECTS = ("mathematics", "physics", "chemistry", "accounting")
# The adapter's reply to one forwarded request: (status, a JSON body) or (200, the SSE lines it streams).
Respond = Callable[[Any], Awaitable[tuple[int, dict[str, Any] | AsyncIterator[str]]]]


class CloudVoice:
    """The `cloud_voice` path for the open profile. `profile()` is the profile's public id, `learner()` its open
    learner database, `announce(session_id, event)` writes to a session's stream and `respond(body)` is the engine
    adapter (VOC-30) called in process."""

    def __init__(self, layer: Any, profile: Callable[[], str | None], learner: Callable[[], sqlite3.Connection | None],
                 announce: Callable[[str, dict[str, Any]], None], respond: Respond, *,
                 context: ssl.SSLContext | None = None) -> None:  # fmt: skip
        self.layer, self.profile, self.learner = layer, profile, learner
        self.announce, self.respond = announce, respond
        self.context = context
        # The session's current subject, by session id (set by the app): named in the URL for the vocabulary bias.
        self.subject_of: Callable[[str], str | None] | None = None

    def url(self, host: str, session_id: str) -> str:
        """The endpoint's address for this session: the path, and the session's subject when it is a curriculum one."""
        subject = self.subject_of(session_id) if self.subject_of is not None else None
        return f"wss://{host}{PATH}" + (f"?subject={subject}" if subject in SUBJECTS else "")

    def effective(self) -> bool:
        """Switched on, covered by the subscription and consented for the open profile (ARC-11, PRV-05, D70). The
        first-use notice is separate: `notice()`."""
        return bool(self.layer.voice_effective(self.profile(), self.learner()))

    def notice(self) -> str | None:
        """The open profile's answer to the cloud voice notice: "accepted", "declined" or None (not asked yet)."""
        profile = self.profile()
        return self.layer.notice_answer(profile, FEATURE) if profile is not None else None

    def ready(self) -> bool:
        """Voice may go online now; otherwise it stays on this computer (VOC-28: never needed with a speech pack)."""
        return self.effective() and self.notice() == "accepted" and self.layer.usable()

    def host(self) -> str:
        return str(self.layer.host_for(FEATURE))

    async def open(self, session_id: str) -> CloudRealtimeConnection:
        """A connection for this session, or `VoiceTransportError` with nothing sent. `EgressRefused` from the
        layer's checks becomes `cloud_voice_refused`."""
        profile = self.profile()
        if not self.effective():
            raise VoiceTransportError("cloud_voice_refused")
        host = self.host()
        try:
            current, host = self.layer.client.admit(FEATURE, f"https://{host}{PATH}", PAYLOAD, credential=True,
                                                    profile_id=profile)  # fmt: skip
            token = await self.layer.credential.token(FEATURE, host)
        except EgressRefused:
            raise VoiceTransportError("cloud_voice_refused") from None
        except Exception:  # credential refused, network down: reason codes only, never the reply (OBS-19)
            raise VoiceTransportError("voice_endpoint_unavailable") from None
        row = current.features[FEATURE]
        notice = {"type": "egress.notice", "feature": FEATURE, "destination_label": row.destination_label,
                  "data_classes": list(row.data_classes)}  # fmt: skip
        self.announce(session_id, {**notice, "state": "started"})
        self.layer.client._ledger(FEATURE, host, len(PATH), PAYLOAD, profile)
        pins = current.pins_for(host)
        release = current.release
        refused: list[str] = []

        class Pinned(ClientConnection):
            def connection_made(self, transport: asyncio.BaseTransport) -> None:
                ssl_object = transport.get_extra_info("ssl_object")
                if pins or release:
                    offered = {spki_pin(der) for der in _chain(ssl_object)} if ssl_object is not None else set()
                    if not pins or not offered & set(pins):
                        refused.append("pin_mismatch" if pins else "no_pin")
                        # Dropped before the upgrade request: neither the token nor a frame is written (SEC-31).
                        if isinstance(transport, asyncio.Transport):
                            transport.abort()
                        else:
                            transport.close()
                        return
                super().connection_made(transport)

        dial = (self.layer.connect or {}).get(host)
        try:
            socket = await connect(
                self.url(host, session_id),
                additional_headers={"Authorization": "Bearer " + token},
                create_connection=Pinned,
                ssl=self.context or self.layer.context or tls_context(),
                server_hostname=host,
                proxy=None,
                compression=None,
                max_size=1_000_000,
                max_queue=64,
                open_timeout=self.layer.settings.net_connect_timeout_s,
                close_timeout=2,
                # VOC-29: three missed heartbeats end the connection, so the bridge can fall back to the sidecar.
                ping_interval=1.0,
                ping_timeout=3.0,
                **({"host": dial[0], "port": dial[1]} if dial else {}),
            )
        except Exception:
            self.announce(session_id, {**notice, "state": "ended"})
            if refused:
                self.layer.client._refuse(EgressRefused(refused[0], FEATURE, host))
                raise VoiceTransportError("cloud_voice_refused") from None
            self.layer.network_failure()
            raise VoiceTransportError("voice_endpoint_unavailable") from None
        return CloudRealtimeConnection(
            socket, self.respond, lambda: self.announce(session_id, {**notice, "state": "ended"})
        )


class CloudRealtimeConnection:
    """The bridge's `RealtimeConnection` to the hosted endpoint. Tunnel requests are answered here and never
    returned by `receive`."""

    path = "cloud_voice"  # the turn trace's voice path (VOC-22)

    def __init__(self, socket: ClientConnection, respond: Respond, ended: Callable[[], None]) -> None:
        self._socket, self._respond, self._ended = socket, respond, ended
        self._tunnel: dict[str, asyncio.Task[None]] = {}
        self._send = asyncio.Lock()
        self._closed = False

    async def send(self, event: dict[str, Any]) -> None:
        await self._write(event)

    async def _write(self, event: dict[str, Any]) -> None:
        try:
            async with self._send:
                await self._socket.send(json.dumps(event, separators=(",", ":")))
        except Exception:
            raise VoiceTransportError("voice_send_failed") from None

    async def receive(self) -> dict[str, Any]:
        while True:
            try:
                raw = await self._socket.recv()
                if not isinstance(raw, str):
                    raise ValueError
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError
            except Exception:
                raise VoiceTransportError("voice_receive_failed") from None
            kind = event.get("type")
            if kind == "jana.voice_llm.request":
                self._forward(event)
            elif kind == "jana.voice_llm.cancel":
                task = self._tunnel.pop(str(event.get("request_id")), None)
                if task is not None:
                    task.cancel()
            else:
                return event

    def _forward(self, event: dict[str, Any]) -> None:
        request_id = event.get("request_id")
        if not isinstance(request_id, str) or not REQUEST_ID.match(request_id) or request_id in self._tunnel:
            return  # malformed or repeated: nothing runs (a constant outcome, never logged with content)
        if len(self._tunnel) >= MAX_TUNNEL:
            self._done_later(request_id, "error")
            return
        task = asyncio.get_running_loop().create_task(self._answer(request_id, event.get("body")))
        self._tunnel[request_id] = task
        task.add_done_callback(lambda _: self._tunnel.pop(request_id, None))

    def _done_later(self, request_id: str, status: str) -> None:
        async def done() -> None:
            with contextlib.suppress(VoiceTransportError):
                await self._write({"type": "jana.voice_llm.done", "request_id": request_id, "status": status})

        asyncio.get_running_loop().create_task(done())

    async def _answer(self, request_id: str, body: Any) -> None:
        status = "error"
        try:
            code, reply = await self._respond(body)
            if code == 200 and not isinstance(reply, dict):
                async for line in reply:
                    for data in _data(line):
                        await self._write({"type": "jana.voice_llm.chunk", "request_id": request_id, "data": data})
                status = "ok"
            elif code == 200 and isinstance(reply, dict):
                # A non-streamed request (the sidecar's warm-up): its one completion as a single chunk.
                content = reply["choices"][0]["message"]["content"]
                chunk = {"id": reply["id"], "object": "chat.completion.chunk", "created": reply["created"],
                         "model": reply["model"], "choices": [{"index": 0, "delta": {"content": content},
                                                               "finish_reason": "stop"}]}  # fmt: skip
                await self._write({"type": "jana.voice_llm.chunk", "request_id": request_id,
                                   "data": json.dumps(chunk, separators=(",", ":"))})  # fmt: skip
                status = "ok"
        except asyncio.CancelledError:
            raise
        except Exception:
            status = "error"
        with contextlib.suppress(VoiceTransportError):
            await self._write({"type": "jana.voice_llm.done", "request_id": request_id, "status": status})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = list(self._tunnel.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self._socket.close()
        finally:
            self._ended()


def _data(line: str) -> list[str]:
    """The `data:` payloads of the SSE text the adapter yields (keep-alive comments and `[DONE]` dropped: the
    connection's own heartbeats keep it alive, and `jana.voice_llm.done` ends the reply)."""
    out = []
    for row in line.splitlines():
        if row.startswith("data:"):
            payload = row[5:].strip()
            if payload and payload != "[DONE]":
                out.append(payload)
    return out


class VoiceRoute:
    """Which endpoint a voice start uses (VOC-27, VOC-28): cloud voice when it is ready for the open profile, else the
    local sidecar. A cloud connection that cannot open falls back to the sidecar when a speech pack is installed
    (VOC-29); without one the failure stands and the voice bar says why (VOC-14)."""

    def __init__(self, cloud: CloudVoice, local: Callable[[], Awaitable[Any]], local_available: Callable[[], bool]):
        self.cloud, self.local, self.local_available = cloud, local, local_available

    async def __call__(self, binding: Any) -> Any:
        if self.cloud.ready():
            try:
                return await self.cloud.open(binding.session_id)
            except VoiceTransportError:
                if not self.local_available():
                    raise
        return await self.local()

    async def fallback(self) -> Any:
        """The local sidecar for a session whose online endpoint was lost (VOC-29); without a speech pack, voice turns
        off and text continues (VOC-14)."""
        if not self.local_available():
            raise VoiceTransportError("voice_pack_unavailable")
        return await self.local()
