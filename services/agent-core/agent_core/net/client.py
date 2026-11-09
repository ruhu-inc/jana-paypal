"""The connectivity layer's HTTP client: the core's only path to a non-loopback host (ARC-06, ARC-18).

Every request names its feature and payload class. Before any lookup or socket the client checks the resolved
policy (feature on and shipped, allow-listed host, allowed payload class, HTTPS only) and the connectivity state (no
attempt while off, offline or limited), and refuses with `EgressRefused` otherwise, logging the refusal without
content. An allowed request is written to the egress ledger (ARC-10) and announced to the indicator while it runs.

Transport security (SEC-31): TLS 1.2 or later with certificate validation; connections to our services must also
match a pinned public key of our intermediate CAs (a backup pin is listed beside the current one). The pin is
checked right after the handshake, before a byte of the request, so an interception proxy with a locally trusted
root receives neither payload nor credential. No redirect is followed outside the feature's allow-list, and proxy
and certificate environment variables are ignored.

Failures are `NetError` with a kind the caller classifies: `connect`, `timeout`, `reset` or `tls` (a TLS failure,
such as a captive portal, makes the connectivity state `limited`, ARC-08)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import socket
import ssl
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import certifi
import httpcore
import httpx
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agent_core.net.policy import LAN_COMPUTE, EgressRefused, ResolvedPolicy
from agent_core.store import egress as ledger_store

USABLE = ("online", "metered")
KEPT_PAYLOADS = ("tombstone", "consent_withdrawal")
MAX_REDIRECTS = 3


class NetError(Exception):
    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


class _PinRefused(Exception):
    def __init__(self, reason: str, host: str) -> None:
        super().__init__(f"{reason}: {host}")
        self.reason = reason
        self.host = host


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: Any  # parsed JSON, or the text when the body is not JSON


def tls_context(cafile: str | None = None) -> ssl.SSLContext:
    """Certificate validation against the bundled CA set (not the environment's), TLS 1.2 at least."""
    context = ssl.create_default_context(cafile=cafile or certifi.where())
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def spki_pin(der_certificate: bytes) -> str:
    """base64(SHA-256(SubjectPublicKeyInfo)) of a DER certificate: the pin format of SEC-31."""
    key = x509.load_der_x509_certificate(der_certificate).public_key()
    return base64.b64encode(hashlib.sha256(key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).digest()
                            ).decode()  # fmt: skip


def _chain(ssl_object: Any) -> list[bytes]:
    """The verified chain as DER certificates, leaf first. Python 3.12 has the call on the private object only."""
    inner = getattr(ssl_object, "_sslobj", ssl_object)
    getter = getattr(inner, "get_verified_chain", None)
    if getter is None:
        peer = ssl_object.getpeercert(binary_form=True)
        return [peer] if peer else []
    return [cert.public_bytes(ssl._ssl.ENCODING_DER) for cert in getter()]  # type: ignore[attr-defined]


async def _abort(stream: httpcore.AsyncNetworkStream) -> None:
    """Drop a connection that failed its pin check without waiting for the peer: no byte of the request is sent."""
    raw = stream.get_extra_info("socket")
    if raw is not None:
        with contextlib.suppress(OSError):
            raw.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(Exception):
        await stream.aclose()


class _PinnedStream(httpcore.AsyncNetworkStream):
    def __init__(self, stream: httpcore.AsyncNetworkStream, backend: _PinnedBackend) -> None:
        self._stream = stream
        self._backend = backend

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._stream.read(max_bytes, timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await self._stream.write(buffer, timeout)

    async def aclose(self) -> None:
        await self._stream.aclose()

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)

    async def start_tls(self, ssl_context: ssl.SSLContext, server_hostname: str | None = None,
                        timeout: float | None = None) -> httpcore.AsyncNetworkStream:  # fmt: skip
        tls = await self._stream.start_tls(ssl_context, server_hostname, timeout)
        host = server_hostname or ""
        pins = self._backend.pins_for(host)
        if not pins:
            if self._backend.release:
                await _abort(tls)
                raise _PinRefused("no_pin", host)
            return tls
        offered = {spki_pin(der) for der in _chain(tls.get_extra_info("ssl_object"))}
        if not offered & set(pins):
            await _abort(tls)
            raise _PinRefused("pin_mismatch", host)
        return tls


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Connections whose TLS handshake is checked against the policy's pins. `connect` maps a host to the address
    actually dialled (tests point our hostnames at a local server; production leaves it empty)."""

    def __init__(self, pins_for: Callable[[str], tuple[str, ...]], release: bool,
                 connect: Mapping[str, tuple[str, int]] | None = None) -> None:  # fmt: skip
        self.pins_for = pins_for
        self.release = release
        self._connect = dict(connect or {})
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                          socket_options: Any = None) -> httpcore.AsyncNetworkStream:  # fmt: skip
        dial_host, dial_port = self._connect.get(host, (host, port))
        stream = await self._inner.connect_tcp(dial_host, dial_port, timeout, local_address, socket_options)
        return _PinnedStream(stream, self)

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: Any = None) -> httpcore.AsyncNetworkStream:  # fmt: skip
        raise NetError("connect", "unix sockets are not an egress path")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _Transport(httpx.AsyncHTTPTransport):
    def __init__(self, context: ssl.SSLContext, backend: _PinnedBackend) -> None:
        super().__init__(verify=context, trust_env=False, retries=0)
        self._pool = httpcore.AsyncConnectionPool(ssl_context=context, network_backend=backend, retries=0)


@dataclass
class EgressClient:
    """`policy` returns the current resolved policy (it changes as the adult switches features and as connectivity
    changes). `device` is the device database holding the ledger. `on_egress(feature, state, label, data_classes)`
    announces non-session egress to the indicator (API-15); a session's own notices come from its engine.
    `notice(feature, profile_id)` answers the one-time notice of ARC-10 ("accepted", "declined" or None for not yet
    shown; None for the callable itself means no notice is required here)."""

    policy: Callable[[], ResolvedPolicy]
    device: Any = None
    clock_ms: Callable[[], int] = field(default=lambda: int(time.time() * 1000))
    connect_timeout_s: float = 5.0
    context: ssl.SSLContext | None = None
    connect: Mapping[str, tuple[str, int]] | None = None
    on_egress: Callable[[str, str, str, tuple[str, ...]], None] | None = None
    notice: Callable[[str, str | None], str | None] | None = None
    on_notice_needed: Callable[[str, str | None], None] | None = None
    refusals: list[dict[str, Any]] = field(default_factory=list)
    _client: httpx.AsyncClient | None = None
    _sends: dict[str, set[asyncio.Task[Any]]] = field(default_factory=dict)
    _streams: dict[str, set[httpx.Response]] = field(default_factory=dict)
    _cut: set[asyncio.Task[Any]] = field(default_factory=set)
    _cut_streams: set[int] = field(default_factory=set)

    def in_flight(self, feature: str) -> int:
        return len(self._sends.get(feature, ())) + len(self._streams.get(feature, ()))

    async def cut(self, feature: str) -> int:
        """The feature was switched off: stop its requests in flight now (PRV-02). Each ends as refused."""
        stopped = 0
        for task in list(self._sends.get(feature, ())):
            self._cut.add(task)
            task.cancel()
            stopped += 1
        for response in list(self._streams.get(feature, ())):
            self._cut_streams.add(id(response))
            await response.aclose()
            stopped += 1
        return stopped

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            current = self.policy()
            backend = _PinnedBackend(lambda host: self.policy().pins_for(host), current.release, self.connect)
            self._client = httpx.AsyncClient(
                transport=_Transport(self.context or tls_context(), backend),
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(None, connect=self.connect_timeout_s),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- the checks (ARC-06) -------------------------------------------------------------------------------------

    def _refuse(self, refused: EgressRefused) -> EgressRefused:
        # Logged with the feature and the reason only: never the address's path, query or any payload (OBS-19).
        self.refusals.append({"at": self.clock_ms(), "feature": refused.feature, "reason": refused.reason})
        return refused

    def admit(self, feature: str, url: str, payload_class: str, *, credential: bool = False,
              profile_id: str | None = None, notify: bool = True) -> tuple[ResolvedPolicy, str]:  # fmt: skip
        """The checks before any lookup or socket. Returns the policy used and the host."""
        current = self.policy()
        try:
            row = current.features.get(feature)
            kept = payload_class in KEPT_PAYLOADS and row is not None and not row.enabled
            if kept:  # owed deletions and withdrawals still reach destinations that hold data (DAT-11)
                current = _with_enabled(current, feature)
            host = current.check(feature, url, payload_class, credential=credential)
            internet = feature != LAN_COMPUTE  # the LAN compute server needs no internet (ARC-13)
            if internet and current.network not in USABLE and payload_class != "probe":  # probes learn the state
                raise EgressRefused("offline", feature, current.network)
            if self.notice is not None and payload_class not in (*KEPT_PAYLOADS, "probe"):
                answer = self.notice(feature, profile_id)
                if answer == "declined":
                    raise EgressRefused("notice_declined", feature)
                if answer is None:
                    if notify and self.on_notice_needed is not None:
                        self.on_notice_needed(feature, profile_id)
                    raise EgressRefused("notice_pending", feature)
        except EgressRefused as refused:
            raise self._refuse(refused) from None
        return current, host

    def _announce(self, current: ResolvedPolicy, feature: str, state: str) -> None:
        row = current.features.get(feature)
        if self.on_egress is not None and row is not None:
            self.on_egress(feature, state, row.destination_label, row.data_classes)

    def _ledger(self, feature: str, host: str, size: int, payload_class: str, profile_id: str | None) -> None:
        if self.device is not None:
            ledger_store.ledger_record(self.device, self.clock_ms(), feature, host, size, payload_class, profile_id)

    # --- requests --------------------------------------------------------------------------------------------------

    async def send(self, feature: str, method: str, url: str, payload_class: str, *, body: Any = None,
                   content: bytes | None = None, headers: Mapping[str, str] | None = None,
                   profile_id: str | None = None, timeout_s: float | None = None,
                   announce: bool = True) -> Response:  # fmt: skip
        data = content if content is not None else (json.dumps(body).encode() if body is not None else b"")
        sent_headers = {**({"content-type": "application/json"} if body is not None else {}), **(headers or {})}
        credential = any(k.lower() == "authorization" for k in sent_headers)
        current, host = self.admit(feature, url, payload_class, credential=credential, profile_id=profile_id)
        if feature == "telemetry" and payload_class == "telemetry_aggregate":
            from agent_core.config.telemetry_schema import problems

            try:
                invalid = problems(json.loads(data))
            except (ValueError, TypeError):
                invalid = ["invalid JSON"]
            if invalid:
                raise self._refuse(EgressRefused("redacted", feature))
        self._ledger(feature, host, len(data) + len(method) + len(url), payload_class, profile_id)
        if announce:
            self._announce(current, feature, "started")
        try:
            for _ in range(MAX_REDIRECTS + 1):
                task = asyncio.ensure_future(self._request(feature, method, url, data, sent_headers, timeout_s))
                self._sends.setdefault(feature, set()).add(task)
                try:
                    reply = await task
                except asyncio.CancelledError:
                    if task in self._cut:
                        raise self._refuse(EgressRefused("feature_off", feature, "switched off in flight")) from None
                    raise
                finally:
                    self._sends[feature].discard(task)
                    self._cut.discard(task)
                location = reply.headers.get("location")
                if reply.status_code in (301, 302, 303, 307, 308) and location:
                    url = str(httpx.URL(url).join(location))
                    try:
                        host = current.check_redirect(feature, url, payload_class)
                    except EgressRefused as refused:
                        raise self._refuse(refused) from None
                    self._ledger(feature, host, len(data), payload_class, profile_id)
                    continue
                return Response(reply.status_code, dict(reply.headers), _parse(reply.text))
            raise NetError("reset", "too many redirects")
        finally:
            if announce:
                self._announce(current, feature, "ended")

    async def _request(self, feature: str, method: str, url: str, data: bytes, headers: Mapping[str, str],
                       timeout_s: float | None) -> httpx.Response:  # fmt: skip
        timeout = httpx.Timeout(timeout_s, connect=self.connect_timeout_s) if timeout_s else None
        try:
            if timeout is None:
                return await self._http().request(method, url, content=data or None, headers=dict(headers))
            return await self._http().request(method, url, content=data or None, headers=dict(headers),
                                              timeout=timeout)  # fmt: skip
        except _PinRefused as refused:
            raise self._refuse(EgressRefused(refused.reason, feature, refused.host)) from None
        except httpx.HTTPError as error:
            raise _translate(error) from None
        except ssl.SSLError as error:
            raise NetError("tls", type(error).__name__) from None

    async def stream_events(self, feature: str, url: str, payload_class: str, body: dict[str, Any], *,
                            headers: Mapping[str, str] | None = None, profile_id: str | None = None,
                            first_byte_timeout_s: float | None = None, idle_timeout_s: float = 120,
                            on_open: Callable[[], None] | None = None,
                            ) -> AsyncIterator[dict[str, Any] | Response]:  # fmt: skip
        """Server-sent events of a streamed reply (`data:` lines as JSON). A non-2xx status yields one `Response`
        and stops; `[DONE]` ends the stream, and a stream that stops without it (or with an error event) was cut:
        `NetError("reset")`. The caller announces session egress itself."""
        data = json.dumps(body).encode()
        sent = {"content-type": "application/json", **(headers or {})}
        credential = any(k.lower() == "authorization" for k in sent)
        _, host = self.admit(feature, url, payload_class, credential=credential, profile_id=profile_id)
        self._ledger(feature, host, len(data), payload_class, profile_id)
        timeout = httpx.Timeout(
            idle_timeout_s,
            connect=self.connect_timeout_s,
            read=first_byte_timeout_s if first_byte_timeout_s is not None else idle_timeout_s,
        )
        try:
            async with self._http().stream("POST", url, content=data, headers=sent, timeout=timeout) as reply:
                self._streams.setdefault(feature, set()).add(reply)
                try:
                    if on_open is not None:
                        on_open()
                    if reply.status_code >= 300:
                        text = (await reply.aread()).decode("utf-8", "replace")
                        yield Response(reply.status_code, dict(reply.headers), _parse(text))
                        return
                    async for line in reply.aiter_lines():
                        if id(reply) in self._cut_streams:
                            break
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            return
                        parsed = _parse(payload)
                        if isinstance(parsed, dict):
                            if "error" in parsed and "choices" not in parsed:
                                raise NetError("reset", "the service failed mid-stream")
                            yield parsed
                    if id(reply) not in self._cut_streams:
                        raise NetError("reset", "the stream ended before it finished")  # no [DONE]: cut
                except (httpx.HTTPError, httpx.StreamClosed):
                    if id(reply) not in self._cut_streams:
                        raise
                finally:
                    self._streams[feature].discard(reply)
                if id(reply) in self._cut_streams:
                    self._cut_streams.discard(id(reply))
                    raise self._refuse(EgressRefused("feature_off", feature, "switched off in flight"))
        except _PinRefused as refused:
            raise self._refuse(EgressRefused(refused.reason, feature, refused.host)) from None
        except httpx.HTTPError as error:
            raise _translate(error) from None
        except ssl.SSLError as error:
            raise NetError("tls", type(error).__name__) from None


def _with_enabled(current: ResolvedPolicy, feature: str) -> ResolvedPolicy:
    from dataclasses import replace

    features = dict(current.features)
    features[feature] = replace(features[feature], enabled=True)
    return replace(current, features=features)


def _parse(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _translate(error: httpx.HTTPError) -> NetError:
    cause = error.__cause__ or error.__context__
    if isinstance(cause, ssl.SSLError) or "SSL" in type(cause).__name__ or "certificate" in str(error).lower():
        return NetError("tls", type(error).__name__)
    if isinstance(error, httpx.ConnectError):
        return NetError("connect", "the host is not reachable")
    if isinstance(error, httpx.TimeoutException):
        return NetError("timeout", type(error).__name__)
    return NetError("reset", type(error).__name__)
