"""Run the preserved browser app against an explicitly configured, budgeted local gateway.

Development host only. The original consent, first-use, egress ledger, profile,
assessment and Study paths remain in use. This adapter never reads a provider key.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from functools import partial
from pathlib import Path
from typing import Any

import httpx
from agent_core.api.__main__ import main
from agent_core.api.core import CoreService
from agent_core.config import Settings
from agent_core.engine.assembly import AppRuntime
from agent_core.net.layer import OnlineLayer
from agent_core.net.state import ProbeOutcome
from agent_core.net.voice import VoiceTransportError
from agent_core.net.voice_cloud import FEATURE as VOICE_FEATURE
from agent_core.net.voice_cloud import PATH as VOICE_PATH
from agent_core.net.voice_cloud import PAYLOAD as VOICE_PAYLOAD
from agent_core.net.voice_cloud import CloudRealtimeConnection, CloudVoice
from agent_core.router.hardware import TutorPath
from agent_core.store.keystore import FileKeyStore, KeyStore, MemoryKeyStore
from cloud_gateway.app import VERSION as GATEWAY_VERSION
from cloud_gateway.catalogue import export as export_catalogue
from cloud_gateway.config import GatewayConfig, load_deployment

HOST = "localhost"
log = logging.getLogger(__name__)


class GatewayHop(httpx.AsyncBaseTransport):
    """A virtual HTTPS service name mapped only to a literal loopback gateway.

    Used behind the existing egress admission checks, never for external services.
    The actual provider connection remains HTTPS in the private gateway.
    """

    def __init__(self, port: int, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not 1 <= port <= 65535:
            raise ValueError("invalid local gateway port")
        self.port = port
        self.inner = transport or httpx.AsyncHTTPTransport(trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (
            request.url.scheme != "https"
            or request.url.host != HOST
            or request.url.port not in (None, 443)
            or request.url.username
            or request.url.password
            or not request.url.path.startswith("/v1/")
        ):
            raise httpx.ConnectError("Refused a destination outside the local gateway", request=request)
        url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
        headers = dict(request.headers)
        headers["host"] = f"127.0.0.1:{self.port}"
        # The device's request goes as it is: the gateway checks it against the device contract and its
        # configured adapter applies the provider's rules, so nothing provider-specific is added here.
        forwarded = httpx.Request(
            request.method, url, headers=headers, stream=request.stream, extensions=request.extensions
        )
        response = await self.inner.handle_async_request(forwarded)
        if response.status_code >= 400:
            # The gateway's fixed error codes never contain prompts or credentials.
            await response.aread()
            try:
                error = response.json().get("error", {})
                log.warning(
                    f"local_gateway status={response.status_code} code={error.get('code')} param={error.get('param')}"
                )
            except (ValueError, AttributeError):
                log.warning(f"local_gateway status={response.status_code}")
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


class LoopbackCloudVoice(CloudVoice):
    """Cloud voice to the local gateway's `/v1/voice/realtime`, as `GatewayHop` is for its HTTP routes: the same
    admission, credential, first-use notice, egress announcement and ledger as `CloudVoice.open`, then a plain
    WebSocket to the literal loopback port (the gateway serves HTTP locally; its Voice Transcribe connection is TLS).
    Development host only."""

    def __init__(self, *args: Any, port: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not 1 <= port <= 65535:
            raise ValueError("invalid local gateway port")
        self.port = port

    async def open(self, session_id: str) -> CloudRealtimeConnection:
        from agent_core.net.policy import EgressRefused
        from websockets.asyncio.client import connect as websocket_connect

        profile = self.profile()
        if not self.effective() or self.host() != HOST:
            raise VoiceTransportError("cloud_voice_refused")
        try:
            current, host = self.layer.client.admit(VOICE_FEATURE, f"https://{HOST}{VOICE_PATH}", VOICE_PAYLOAD,
                                                    credential=True, profile_id=profile)  # fmt: skip
            token = await self.layer.credential.token(VOICE_FEATURE, host)
        except EgressRefused:
            raise VoiceTransportError("cloud_voice_refused") from None
        except Exception:
            raise VoiceTransportError("voice_endpoint_unavailable") from None
        row = current.features[VOICE_FEATURE]
        notice = {"type": "egress.notice", "feature": VOICE_FEATURE, "destination_label": row.destination_label,
                  "data_classes": list(row.data_classes)}  # fmt: skip
        self.announce(session_id, {**notice, "state": "started"})
        self.layer.client._ledger(VOICE_FEATURE, host, len(VOICE_PATH), VOICE_PAYLOAD, profile)
        try:
            socket = await websocket_connect(
                self.url(HOST, session_id).replace(f"wss://{HOST}", f"ws://127.0.0.1:{self.port}", 1),
                additional_headers={"Authorization": "Bearer " + token},
                proxy=None, compression=None, max_size=1_000_000, max_queue=64, close_timeout=2,
                open_timeout=self.layer.settings.net_connect_timeout_s, ping_interval=1.0,
                ping_timeout=3.0)  # fmt: skip
        except Exception:
            self.announce(session_id, {**notice, "state": "ended"})
            self.layer.network_failure()
            raise VoiceTransportError("voice_endpoint_unavailable") from None
        return CloudRealtimeConnection(
            socket, self.respond, lambda: self.announce(session_id, {**notice, "state": "ended"})
        )


def gateway_config() -> GatewayConfig:
    path = os.environ.get("JANA_GATEWAY_CONFIG")
    if not path:
        raise ValueError("Set JANA_GATEWAY_CONFIG to the reviewed gateway configuration")
    if os.environ.get("JANA_ADULT_WORKSHOP") == "1" and os.environ.get("JANA_SPONSORED_ANCHORS"):
        from jana_tools.adult_workshop import adult_gateway

        config = adult_gateway(Path(path))
    else:
        config = load_deployment(Path(path))
    if (
        config.environment != "dev"
        or config.audience_host != HOST
        or config.quotas.deployment_total_spend_usd is None
        or config.quotas.deployment_daily_spend_usd is None
        or config.quotas.deployment_budget_expires_at is None
    ):
        raise ValueError("The browser host requires a local gateway with daily, total and expiry limits")
    return config


class SponsoredRuntime(AppRuntime):
    def key_store(self) -> KeyStore:
        if self.release:
            raise ValueError("The browser key store is development-only")
        if self.keys is None or isinstance(self.keys, MemoryKeyStore):
            self.keys = FileKeyStore(self.data_root.parent / f".{self.data_root.name}-keys", self.data_root)
        return self.keys

    def attach_online(self) -> OnlineLayer:
        layer = super().attach_online()
        if self.release:
            raise ValueError("The browser adapter is not a release deployment")
        self.gateway = gateway_config()
        self.gateway_port = int(os.environ.get("JANA_GATEWAY_PORT", "8081"))
        layer.hosts = {service: HOST for service in ("gateway", "voice")}
        # This development-only projection reflects operator configuration, without inventing permission.
        target = self.data_root / ".service-catalogue.dev.json"
        with tempfile.NamedTemporaryFile("w", dir=self.data_root, prefix=".catalogue-", delete=False) as out:
            staged = Path(out.name)
            json.dump(export_catalogue(self.gateway, time.time()), out, allow_nan=False)
        try:
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)
        self.settings = self.settings.model_copy(update={"cloud_catalogue_file": str(target)})
        layer.settings = self.settings
        # Downloads/updates/voice keep their original implementations, but no such
        # external service is connected by this local inference adapter.
        layer.client._client = httpx.AsyncClient(
            transport=GatewayHop(self.gateway_port), trust_env=False, follow_redirects=False, timeout=180
        )
        layer.probe = partial(upstream_probe, self.gateway_port)
        return layer

    def prepare_cloud_voice(self, announce: Any, respond: Any) -> CloudVoice | None:
        voice = super().prepare_cloud_voice(announce, respond)
        if voice is None or self.online is None:
            return voice
        return LoopbackCloudVoice(self.online, self.public_profile,
                                  lambda: self.stores.learner if self.stores is not None else None, announce, respond,
                                  port=self.gateway_port)  # fmt: skip

    def tutor_path(self) -> TutorPath:
        # Before first-use acceptance the original engine must be able to show its
        # notice. No inference is admitted until cloud_tier_on() becomes true.
        if self.online and self.stores and self.online.cloud_effective(self.public_profile(), self.stores.learner):
            return "online"
        return super().tutor_path()

    def tutor_unavailable(self) -> bool:
        if self.tutor_path() == "online":
            return False
        if "local" not in self.providers and not self.school_server_reachable():
            return True
        return super().tutor_unavailable()


def settings_for_browser(settings: Settings) -> Settings:
    adult = os.environ.get("JANA_ADULT_WORKSHOP") == "1" and bool(os.environ.get("JANA_SPONSORED_ANCHORS"))
    return settings.model_copy(
        update={
            # Expose the imported visual tutoring, local parent views and history
            # in the development app. This is not a release-acceptance claim.
            "build_stage": "S3",
            "study_workspace_enabled": True,
            "study_offline_enabled": False,
            "study_neural_speech_enabled": True,
            "study_batch_char_limit": 16000,
            "consent_dpia_ref": "m42-owner-approval-2026-10-18" if adult else "local-synthetic-hackathon-notice",
            "cloud_voice_enabled": not adult,
            "offline_lean_classes": ("low", "mid", "high"),
        }
    )


async def upstream_probe(port: int, _host: str) -> ProbeOutcome:
    """Probe the deployed upstream through the loopback gateway."""
    try:
        async with httpx.AsyncClient(trust_env=False) as probe:
            reply = await probe.get(f"http://127.0.0.1:{port}/v1/ping", params={"upstream": "1"}, timeout=4)
    except httpx.HTTPError:
        return "failed"
    return "ok" if reply.status_code == 204 else "failed"


async def check_gateway(port: int, transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Fail closed unless the already-running gateway on `port` is this repository's (its `jana-gateway-version`
    header equals `cloud_gateway.app.VERSION`) and advertises its persistent cap. A gateway from another project on
    the shared port answers the ping but knows none of this repository's kinds, so every job failed with
    `processing_failed` (live-run fix 2026-11-01); the message names the port and the version found."""
    async with httpx.AsyncClient(trust_env=False, transport=transport) as probe:
        try:
            reply = await probe.get(f"http://127.0.0.1:{port}/v1/ping", timeout=3)
        except httpx.HTTPError as error:
            raise ValueError(
                f"No gateway answered on port {port}: start this repository's gateway (version {GATEWAY_VERSION})"
                " before the browser host"
            ) from error
        version = reply.headers.get("jana-gateway-version")
        if reply.status_code != 204 or version != GATEWAY_VERSION:
            raise ValueError(
                f"The gateway on port {port} is not this repository's: it serves version {version or 'none'}"
                f" (status {reply.status_code}) and this repository's gateway serves {GATEWAY_VERSION}."
                f" Stop it and start this repository's gateway on port {port} before the browser host"
            )
        if reply.headers.get("x-jana-inference-budget") != "enabled":
            raise ValueError(f"Start the approved budgeted gateway on port {port} before the browser host")


async def connect(runtime: AppRuntime, core: CoreService) -> None:
    if not isinstance(runtime, SponsoredRuntime) or runtime.online is None or runtime.directory is None:
        raise ValueError("The browser host requires its own local runtime")
    online = runtime.online
    core.settings = runtime.settings
    await check_gateway(runtime.gateway_port)
    expires = runtime.gateway.quotas.deployment_budget_expires_at
    assert expires is not None
    end_ms = int(expires.timestamp() * 1000)
    if end_ms <= core.clock_ms():
        raise ValueError("The approved inference period has expired")
    if anchor_file := os.environ.get("JANA_SPONSORED_ANCHORS"):
        from agent_core.config.trust import TrustStore

        if online.device.execute("SELECT 1 FROM app_settings WHERE key='hackathon_sponsor'").fetchone():
            raise ValueError("Use a personal workshop slot without the automatic demo subscription")
        core.entitlement_trust = TrustStore.from_document(json.loads(Path(anchor_file).read_text()))
    # A hosted core uses normal signed subscriptions. Only a funded workshop can issue its month.
    runtime.providers.pop("local", None)
    runtime.link = None
    core.readiness = "ready"
    core.components["runtime"] = "ok"


if __name__ == "__main__":
    raise SystemExit(
        asyncio.run(
            main(runtime_factory=SponsoredRuntime, settings_transform=settings_for_browser, before_ready=connect)
        )
    )
