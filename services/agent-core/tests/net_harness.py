"""Test doubles for M19: a local HTTPS server standing in for our online services (the cloud gateway, downloads,
updates, telemetry...), certificates from a throwaway CA, and an `OnlineLayer` pointed at the server.

Nothing here reaches the internet: our service hostnames (`*.dev.jana.invalid`) are dialled at 127.0.0.1, and the
server speaks just enough HTTP/1.1 for the connectivity layer's client. Replies are synthetic."""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import json
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_core.config import Settings
from agent_core.net.client import spki_pin
from agent_core.net.layer import OnlineLayer
from agent_core.net.policy import load_policy
from agent_core.store import subscription as subscriptions
from agent_core.store.keystore import MemoryKeyStore
from agent_core.store.local import open_local
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

HOSTS = load_policy().services["dev"]
# The stand-in gateway's replies, valid against the gateway contract (D39).
CREDENTIAL_ID = "dcr_test_credential"
ACCESS_TOKEN = "tok_test_access_token"  # noqa: S105 (a synthetic token)


@dataclass
class Pki:
    """A CA and a server certificate for every service host. `ca_pin` is the CA's SPKI pin."""

    ca_pem: bytes
    ca_der: bytes
    cert_pem: bytes
    key_pem: bytes

    @property
    def ca_pin(self) -> str:
        return spki_pin(self.ca_der)

    def client_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context(cadata=self.ca_pem.decode())
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def server_context(self, tmp: Path) -> ssl.SSLContext:
        cert, key = tmp / "server.pem", tmp / "server.key"
        cert.write_bytes(self.cert_pem + self.ca_pem)
        key.write_bytes(self.key_pem)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        return context


def make_pki(names: list[str], common_name: str = "Test CA", ips: tuple[str, ...] = ()) -> Pki:
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
          .not_valid_after(now + datetime.timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))  # fmt: skip
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([*(x509.DNSName(n) for n in names),
                                                        *(x509.IPAddress(ipaddress.ip_address(i)) for i in ips)]),
                           critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))  # fmt: skip
    pem = serialization.Encoding.PEM
    return Pki(ca.public_bytes(pem), ca.public_bytes(serialization.Encoding.DER), cert.public_bytes(pem),
               key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))  # fmt: skip


@dataclass
class Seen:
    method: str
    path: str
    host: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


Handler = Callable[[Seen], Any]  # returns (status, body) | ("sse", [events], cut_after) | None for the default


@dataclass
class FakeServices:
    """Our online services on one local TLS port. `handlers` maps a path prefix to a handler; the default answers
    the gateway's protocol (devices, auth, chat) and the outbox (deduplicated by idempotency key)."""

    tmp: Path
    pki: Pki = field(default_factory=lambda: make_pki(sorted(set(HOSTS.values()))))
    requests: list[Seen] = field(default_factory=list)
    handlers: dict[str, Handler] = field(default_factory=dict)
    delivered: dict[str, bytes] = field(default_factory=dict)  # idempotency key -> payload, applied once
    raw_bytes: int = 0  # every byte any client sent, before TLS was even checked
    revoked: bool = False
    chat: list[Any] = field(default_factory=list)  # scripted chat replies: a dict, ("sse", texts, cut) or "drop"
    port: int = 0
    _server: asyncio.base_events.Server | None = None
    _writers: set[asyncio.StreamWriter] = field(default_factory=set)

    async def __aenter__(self) -> FakeServices:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0,
                                                  ssl=self.pki.server_context(self.tmp))  # fmt: skip
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_: Any) -> None:
        assert self._server is not None
        self._server.close()
        for writer in list(self._writers):
            writer.close()
        await asyncio.gather(*(writer.wait_closed() for writer in list(self._writers)), return_exceptions=True)
        await self._server.wait_closed()

    def connect(self) -> dict[str, tuple[str, int]]:
        return {host: ("127.0.0.1", self.port) for host in HOSTS.values()}

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                self.raw_bytes += len(head)
                lines = head.decode().split("\r\n")
                method, path, _ = lines[0].split(" ", 2)
                headers = {k.lower(): v.strip() for k, v in (line.split(":", 1) for line in lines[1:] if ":" in line)}
                body = await reader.readexactly(int(headers.get("content-length", "0")))
                self.raw_bytes += len(body)
                seen = Seen(method, path, headers.get("host", ""), headers, body)
                self.requests.append(seen)
                if not await self._answer(seen, writer):
                    break
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    async def _answer(self, seen: Seen, writer: asyncio.StreamWriter) -> bool:
        handler = next((h for prefix, h in self.handlers.items() if seen.path.startswith(prefix)), None)
        result = handler(seen) if handler is not None else None
        if result is None:
            result = self._default(seen)
        if result == "drop":
            return False
        if isinstance(result, tuple) and result[0] == "sse":
            _, events, cut = result
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nconnection: close\r\n\r\n")
            for index, event in enumerate(events):
                if cut is not None and index >= cut:
                    await writer.drain()
                    return False  # the network is cut mid-stream
                writer.write(f"data: {json.dumps(event)}\n\n".encode())
                await writer.drain()
                await asyncio.sleep(0)
            writer.write(b"data: [DONE]\n\n")
            await writer.drain()
            return False
        status, payload = result
        data = b"" if payload is None else json.dumps(payload).encode()
        reason = {200: "OK", 201: "Created", 204: "No Content", 401: "Unauthorized", 409: "Conflict"}.get(status, "X")
        writer.write(f"HTTP/1.1 {status} {reason}\r\ncontent-type: application/json\r\ncontent-length: {len(data)}"
                     f"\r\n\r\n".encode() + data)  # fmt: skip
        await writer.drain()
        return True

    def _default(self, seen: Seen) -> Any:
        if seen.path == "/v1/ping":
            return 204, None
        if seen.path == "/v1/devices":
            return 201, {"credential_id": CREDENTIAL_ID}
        if seen.path == "/v1/auth/challenge":
            return (401, {"error": {"code": "auth", "message": "revoked"}}) if self.revoked else (
                200, {"nonce": "bm9uY2Vub25jZW5vbmNlbm9uY2Vub25jZW5vbmNlMTI=", "expires_in": 60})  # fmt: skip
        if seen.path == "/v1/auth/token":
            return (401, {"error": {"code": "auth", "message": "revoked"}}) if self.revoked else (
                200, {"access_token": ACCESS_TOKEN, "token_type": "Bearer", "expires_in": 900})  # fmt: skip
        if seen.path.startswith("/v1/outbox/"):
            key = seen.headers.get("idempotency-key", "")
            if key in self.delivered:
                return 409, {"error": {"code": "duplicate", "message": "seen already"}}
            self.delivered[key] = seen.body
            return 200, {"ok": True}
        if seen.path == "/v1/chat/completions":
            step = self.chat.pop(0) if self.chat else {"choices": [{"message": {"content": "ok"}}]}
            if isinstance(step, dict):
                return 200, step
            return step
        return 404, {"error": {"code": "not_found", "message": seen.path}}


def sse_reply(text: str, size: int = 4, model: str = "gateway-model") -> list[dict[str, Any]]:
    """A streamed chat completion of `text`, as OpenAI-style chunks, ending with usage."""
    chunks = [
        {"model": model, "choices": [{"delta": {"content": text[i : i + size]}}]} for i in range(0, len(text), size)
    ]
    return [*chunks, {"model": model, "choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"usage": {"prompt_tokens": 1000, "completion_tokens": 100}}]  # fmt: skip


def device_db(tmp: Path) -> Any:
    return open_local(tmp / "data", app_version="0.0.0", now_ms=1_760_000_000_000).device


def layer(tmp: Path, services: FakeServices | None = None, settings: Settings | None = None,
          **kwargs: Any) -> OnlineLayer:  # fmt: skip
    """An OnlineLayer on a fresh device.db, pointed at the fake services when given (pins = the test CA)."""
    tmp.mkdir(parents=True, exist_ok=True)
    device = kwargs.pop("device", None) or device_db(tmp)
    extra: dict[str, Any] = {}
    if services is not None:
        extra = {"connect": services.connect(), "context": services.pki.client_context(),
                 "pins": {h: (services.pki.ca_pin,) for h in HOSTS.values()}}  # fmt: skip
    if services is not None and "entitlement" not in kwargs:
        kwargs["entitlement"] = registration_entitlement(device)
    configured = settings or Settings()
    if not configured.cloud_catalogue_file:
        from catalogue_fixture import synthetic_catalogue

        catalogue_path = tmp / "synthetic-deployment.json"
        catalogue_path.write_text(json.dumps(synthetic_catalogue()))
        configured = configured.model_copy(update={"cloud_catalogue_file": str(catalogue_path)})
    online = OnlineLayer(configured, device, kwargs.pop("keys", None) or MemoryKeyStore(), **{**extra, **kwargs})
    if settings is None or not settings.cloud_catalogue_file:
        synthetic = synthetic_catalogue()
        synthetic.update(host=online.host_for("cloud_tier"), voice_host=online.host_for("cloud_voice"))
        catalogue_path.write_text(json.dumps(synthetic))
    return online


def registration_entitlement(device: Any) -> Callable[[], Mapping[str, Any] | None]:
    """What the device registers its credential with: the applied subscription's entitlement, as the app does, or,
    when the test applied none, a synthetic one signed by the test licence key (the gateway contract requires one)."""
    from signing_service.hierarchy import dev_signer
    from signing_service.keys import Purpose

    document = {"kind": "cloud_tier_subscription", "subscription_id": "sub_harness", "licence_id": "lic_harness",
                "period_start": "2026-01-01T00:00:00.000Z", "period_end": "2099-01-01T00:00:00.000Z", "seats": 1,
                "allowance_tokens": 1_000_000,
                "plan": "jana_online", "band": "nigeria", "billing": "month", "features": ["cloud_tier"], "limits": {},
                "created_at": "2026-01-01T00:00:00.000Z"}  # fmt: skip
    synthetic = dev_signer(Purpose.LICENCE).sign("licence_file", document, now="2026-01-01T00:00:00.000Z")

    def found() -> Mapping[str, Any] | None:
        stored = subscriptions.load(device).get("entitlement")
        return stored if isinstance(stored, Mapping) else synthetic

    return found


def enable(online: OnlineLayer, *features: str, on: bool = True) -> None:
    for feature in features:
        online.device.execute(
            "INSERT INTO online_features (feature, enabled, enabled_by_role, enabled_at) VALUES (?, ?, 'user', 0)"
            " ON CONFLICT (feature) DO UPDATE SET enabled = excluded.enabled",
            (feature, int(on)),
        )


def telemetry_payload(n: int = 0, series_count: int = 0) -> bytes:
    """Synthetic OBS-20 payload for transport tests; no arbitrary test text leaves as telemetry."""
    from agent_core.config.telemetry_schema import report
    from agent_core.trace.metrics import observe

    metrics: dict[str, Any] = {}
    observe(metrics, "model_calls", n)
    metadata = {
        "app_version": "0.0.0",
        "channel": "dev",
        "hardware_class": "low",
        "os_family": "linux",
        "router_tier": "local",
    }
    series = [{**metadata, "metrics": metrics}] * series_count if series_count else None
    return json.dumps(report(metrics, days=1, series=series, **metadata)).encode()
