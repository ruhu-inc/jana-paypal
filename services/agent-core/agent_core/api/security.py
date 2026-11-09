"""Local API hardening (API-02, API-03, SEC-10), as plain ASGI middleware in front of every route.

- `Host` must name the loopback address and the bound port (`127.0.0.1`, `[::1]` or `localhost`), which blocks DNS
  rebinding; an `Origin`, when present, must be the app's webview origin. Either failure is 403
  `forbidden_origin`, checked before the token, so a valid token never helps a foreign page.
- Every request carries `Authorization: Bearer <launch token>`, compared in constant time; otherwise 401.
- The webview's origin differs from the core's, so the app's own origin (and only it) gets CORS: its preflight is
  answered and its responses name that origin (never `*`). A preflight or request from any other origin is refused
  with no `Access-Control-*` header, so a browser page on the same machine can neither send nor read.

The token lives in memory only: it arrives over the control channel and is never logged (the access log is off)."""

from __future__ import annotations

import hmac
import json
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import Any

from agent_core.api.errors import envelope

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# The Tauri webview's origins: macOS and Linux use the custom scheme, Windows (WebView2) the http form.
APP_ORIGINS = frozenset({"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"})
LOOPBACK_HOSTS = ("127.0.0.1", "[::1]", "localhost")
ALLOWED_HEADERS = b"authorization, content-type, idempotency-key, last-event-id, x-profile-token, x-study-open"


def websocket_credential(scope: Scope, prefix: str) -> str | None:
    """Exactly one nonempty credential subprotocol; never reflect it in the accepted protocol."""
    values = [value[len(prefix) :] for value in scope.get("subprotocols", []) if value.startswith(prefix)]
    return values[0] if len(values) == 1 and values[0] else None


def _cors(origin: str) -> list[tuple[bytes, bytes]]:
    return [(b"access-control-allow-origin", origin.encode("latin-1")), (b"vary", b"Origin")]


class LaunchSecret:
    """The per-launch bearer token, held in memory and compared in constant time. Its repr never shows it."""

    __slots__ = ("_value",)

    def __init__(self, value: str | None = None) -> None:
        self._value = value.encode() if value else None

    def set(self, value: str) -> None:
        self._value = value.encode()

    @property
    def ready(self) -> bool:
        return self._value is not None

    def matches(self, presented: str | None) -> bool:
        if self._value is None or presented is None:
            return False
        return hmac.compare_digest(self._value, presented.encode())

    def __repr__(self) -> str:
        return "LaunchSecret(<redacted>)"


class LoopbackGuard:
    def __init__(self, app: ASGIApp, secret: LaunchSecret, port: Callable[[], int | None],
                 origins: Iterable[str] = APP_ORIGINS, *,
                 adapter_secret: LaunchSecret | None = None) -> None:  # fmt: skip
        self.app = app
        self.secret = secret
        self.port = port
        self.origins = frozenset(origins)
        self.adapter_secret = adapter_secret

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        origin = headers.get("origin")
        if scope["type"] == "websocket":
            # Browsers cannot set Authorization on a WebSocket. Credentials stay in the offered subprotocols;
            # only jana.voice.1 is ever echoed. Refusals must use ASGI WebSocket messages, not HTTP messages.
            bearer = websocket_credential(scope, "jana.bearer.")
            headers["authorization"] = f"Bearer {bearer}" if bearer else ""
            failure = self.check("GET", headers)
            if failure is not None or origin not in self.origins or "jana.voice.1" not in scope.get("subprotocols", []):
                close_code = 4401 if failure is not None and failure[0] == 401 else 4403
                await send({"type": "websocket.close", "code": close_code})
                return
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "OPTIONS" and origin in self.origins and self._host_ok(headers):
            await self._preflight(origin, send)
            return
        failure = self.check(scope.get("method", "GET"), headers, str(scope.get("path", "")))
        if failure is None:
            if origin is None:
                await self.app(scope, receive, send)
                return

            async def with_cors(message: Message) -> None:
                if message["type"] == "http.response.start":
                    message["headers"] = [*message.get("headers", []), *_cors(origin)]
                await send(message)

            await self.app(scope, receive, with_cors)
            return
        status, code = failure
        body = json.dumps(envelope(code, "request refused by the local API guard")).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"cache-control", b"no-store")]})  # fmt: skip
        await send({"type": "http.response.body", "body": body})

    def _host_ok(self, headers: dict[str, str]) -> bool:
        port = self.port()
        return port is not None and headers.get("host", "") in {f"{h}:{port}" for h in LOOPBACK_HOSTS}

    async def _preflight(self, origin: str, send: Send) -> None:
        await send({"type": "http.response.start", "status": 204,
                    "headers": [*_cors(origin), (b"access-control-allow-methods", b"GET, POST, PUT, PATCH, DELETE"),
                                (b"access-control-allow-headers", ALLOWED_HEADERS),
                                (b"access-control-max-age", b"600")]})  # fmt: skip
        await send({"type": "http.response.body", "body": b""})

    def check(self, method: str, headers: dict[str, str], path: str = "") -> tuple[int, str] | None:
        if not self._host_ok(headers):
            return 403, "forbidden_origin"
        origin = headers.get("origin")
        if origin is not None and origin not in self.origins:
            return 403, "forbidden_origin"
        if method == "OPTIONS":
            return 403, "forbidden_origin"  # no CORS preflight is ever answered
        auth = headers.get("authorization", "")
        token = auth[7:] if auth[:7].lower() == "bearer " else None
        secret = (
            self.adapter_secret
            if method == "POST" and path == "/v1/voice/llm/chat/completions" and self.adapter_secret is not None
            else self.secret
        )
        if not secret.matches(token):
            return 401, "unauthorized"
        return None
