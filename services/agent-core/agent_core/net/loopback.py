"""HTTP to services on this device only: the local model runtime (MDL-01, ARC-18).

The client refuses any host that is not the literal loopback address `127.0.0.1` or `::1`: never a name such as
`localhost`, which a hosts file or resolver could point elsewhere, and never another 127/8 address. It ignores proxy
and certificate environment variables and carries the runtime's per-launch key. Failures are reported as
`LoopbackError` with a kind the provider classifies (MDL-07): `connect` (nothing listening), `timeout`, `reset` (the
connection dropped mid-reply)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from agent_core.net.local_address import is_literal_loopback


class LoopbackError(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: Any  # parsed JSON, or the text when the body is not JSON


def is_loopback(base_url: str) -> bool:
    try:
        host = urlsplit(base_url).hostname or ""
    except ValueError:
        return False
    return is_literal_loopback(host)


class LoopbackClient:
    """One client per runtime launch. `api_key` is the launch's key (sent as a bearer token)."""

    def __init__(self, base_url: str, api_key: str | None = None, connect_timeout_s: float = 2.0) -> None:
        if not is_loopback(base_url):
            raise ValueError(f"not a loopback address: {urlsplit(base_url).hostname!r}")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            trust_env=False,  # no proxies, no netrc: the request never leaves the device
            timeout=httpx.Timeout(None, connect=connect_timeout_s),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, timeout_s: float | None = None) -> Response:
        return await self._send("GET", path, None, timeout_s)

    async def post(self, path: str, body: dict[str, Any], timeout_s: float | None = None) -> Response:
        return await self._send("POST", path, body, timeout_s)

    async def _send(self, method: str, path: str, body: dict[str, Any] | None, timeout_s: float | None) -> Response:
        try:
            reply = await self._client.request(method, path, json=body, timeout=_timeout(timeout_s))
        except httpx.HTTPError as error:
            raise _translate(error) from None
        return Response(reply.status_code, dict(reply.headers), _parse(reply.text))

    async def stream_events(
        self, path: str, body: dict[str, Any], first_byte_timeout_s: float | None = None, idle_timeout_s: float = 120
    ) -> AsyncIterator[dict[str, Any] | Response]:
        """Server-sent events of a streamed reply, each `data:` payload parsed as JSON. A non-2xx status yields one
        `Response` and stops; `[DONE]` ends the stream."""
        timeout = httpx.Timeout(idle_timeout_s, connect=self._client.timeout.connect)
        if first_byte_timeout_s is not None:
            timeout = httpx.Timeout(idle_timeout_s, connect=self._client.timeout.connect, read=first_byte_timeout_s)
        try:
            async with self._client.stream("POST", path, json=body, timeout=timeout) as reply:
                if reply.status_code >= 300:
                    text = (await reply.aread()).decode("utf-8", "replace")
                    yield Response(reply.status_code, dict(reply.headers), _parse(text))
                    return
                async for line in reply.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        return
                    parsed = _parse(payload)
                    if isinstance(parsed, dict):
                        yield parsed
        except httpx.HTTPError as error:
            raise _translate(error) from None


def _timeout(seconds: float | None) -> httpx.Timeout | None:
    return None if seconds is None else httpx.Timeout(seconds, connect=min(seconds, 2.0))


def _parse(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _translate(error: httpx.HTTPError) -> LoopbackError:
    if isinstance(error, httpx.ConnectError):
        return LoopbackError("connect", "the local runtime is not listening")
    if isinstance(error, httpx.TimeoutException):
        return LoopbackError("timeout", type(error).__name__)
    return LoopbackError("reset", type(error).__name__)
