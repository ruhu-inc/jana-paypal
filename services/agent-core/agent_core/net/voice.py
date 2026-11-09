"""The core's bounded, authenticated loopback Realtime connection (VOC-25/27).

Only the net layer imports the WebSocket client. Proxy discovery and compression are disabled, endpoints are
literal loopback addresses, and diagnostics never contain the URL's credentials, headers, frames or transcripts.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from websockets.asyncio.client import ClientConnection, connect

from agent_core.net.local_address import is_literal_loopback


class VoiceTransportError(ConnectionError):
    pass


class LocalVoiceEndpoint:
    def __init__(self, url: str, credential: str) -> None:
        parsed = urlsplit(url)
        try:
            valid = (
                is_literal_loopback(parsed.hostname or "")
                and parsed.port is not None
                and parsed.scheme == "ws"
                and parsed.path == "/v1/realtime"
                and not parsed.query
                and not parsed.fragment
                and parsed.username is None
                and parsed.password is None
            )
        except ValueError:
            valid = False
        if (
            not valid
            or len(credential) < 32
            or len(credential) > 256
            or not credential.isascii()
            or any(character.isspace() or ord(character) < 33 for character in credential)
        ):
            raise VoiceTransportError("invalid_local_voice_endpoint")
        self.url = url
        self._credential = credential

    def __repr__(self) -> str:
        return "LocalVoiceEndpoint(<loopback>, <redacted>)"

    async def open(self) -> RealtimeSocket:
        try:
            socket = await connect(
                self.url,
                additional_headers={"Authorization": "Bearer " + self._credential},
                proxy=None,
                compression=None,
                max_size=512_000,
                max_queue=8,
                open_timeout=5,
                close_timeout=2,
                ping_interval=20,
            )
        except Exception:
            raise VoiceTransportError("voice_endpoint_unavailable") from None
        return RealtimeSocket(socket)


class RealtimeSocket:
    def __init__(self, socket: ClientConnection) -> None:
        self._socket = socket

    async def send(self, event: dict[str, Any]) -> None:
        try:
            await self._socket.send(json.dumps(event, separators=(",", ":")))
        except Exception:
            raise VoiceTransportError("voice_send_failed") from None

    async def receive(self) -> dict[str, Any]:
        try:
            raw = await self._socket.recv()
            if not isinstance(raw, str):
                raise ValueError
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError
            return event
        except Exception:
            raise VoiceTransportError("voice_receive_failed") from None

    async def close(self) -> None:
        await self._socket.close()
