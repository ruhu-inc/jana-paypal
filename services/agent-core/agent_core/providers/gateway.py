"""The cloud tier's provider: our cloud gateway, reached through the connectivity layer (ARC-12, MDL-26).

The device never talks to a model provider: every cloud candidate (`cloud:primary/<model>`, `cloud:fallback/<model>`)
is served by `services/cloud-gateway`, which holds the provider keys (SEC-20) and picks the provider the gateway's
configuration names for the slot. The request is an OpenAI-style chat completion (the gateway translates it for the
provider), sent as feature `cloud_tier` with payload class `cloud_prompt`. Before it leaves, the redaction check
blocks any body carrying an identifying term (MDL-25) and the router serves the call locally. The session is a
random per-session pseudonym (PRV-25). Capabilities come from configuration, so choosing candidates never touches
the network. Network failures are reported to the connectivity layer (MDL-24) and classified for the router."""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

from agent_core.net.client import EgressClient, NetError, Response
from agent_core.net.credentials import CredentialRefused
from agent_core.net.policy import EgressRefused
from agent_core.net.redaction import Redactor
from agent_core.providers.base import (
    AssistantTurn,
    EffortRecord,
    Message,
    ModelCapabilities,
    ProviderError,
    StreamChunk,
    TokenUsage,
    ToolSchema,
)
from agent_core.providers.errors import provider_error
from agent_core.providers.openai_compat import assemble_stream, parse_reply, request_body
from agent_core.trace import current_scope

FEATURE = "cloud_tier"
PAYLOAD = "cloud_prompt"
CHAT = "/v1/chat/completions"
SEGMENT = "/v1/vision/segment"  # SAM 3.1 masks for a screened photo (JM-D10)
log = logging.getLogger(__name__)


def cloud_capabilities(context: int, max_output: int, vision: bool = True) -> ModelCapabilities:
    return ModelCapabilities(tools=True, parallel_tools=True, vision=vision, json_schema=True, json_object=True,
                             grammar=False, reasoning="effort", reasoning_default="low", context_window=context,
                             context_for_agents=context, max_output=max_output, family="gateway",
                             locality="cloud")  # fmt: skip


class GatewayProvider:
    kind = "cloud"

    def __init__(
        self,
        client: EgressClient,
        host: Callable[[], str],
        slot: str,
        *,
        token: Callable[[], Awaitable[str]] | None = None,
        caps: Mapping[str, ModelCapabilities] | None = None,
        prices: Mapping[str, tuple[float, float]] | None = None,
        redactor: Redactor | None = None,
        profile_id: str | None = None,
        on_network_failure: Callable[[str], None] | None = None,
        request_timeout_s: float = 120.0,
        allowed: Callable[[str], bool] | None = None,
        strict_models: bool = False,
        model_allowed: Callable[[str], bool] | None = None,
    ) -> None:
        self.allowed, self.strict_models, self.model_allowed = allowed, strict_models, model_allowed
        self.client = client
        self.host = host
        self.slot = slot
        self.token = token
        self.caps = dict(caps or {})
        self.prices = dict(prices or {})  # model -> (input, output) USD per million tokens
        self.redactor = redactor or Redactor()
        self.profile_id = profile_id  # for the ledger only; never sent
        self.pseudonym = secrets.token_hex(16)  # one per session: replaces every profile and session id (PRV-25)
        self.on_network_failure = on_network_failure
        self.request_timeout_s = request_timeout_s
        self.requests: list[dict[str, Any]] = []  # the bodies sent, for audits in tests (no copy is kept elsewhere)

    def _permission(self, service: str) -> None:
        if self.allowed is not None and not self.allowed(service):
            raise ProviderError("model_not_found", "deployment service permission unavailable")

    async def installed(self) -> list[str]:
        return []

    async def capabilities(self, model: str) -> ModelCapabilities:
        if self.strict_models and (model not in self.caps or (self.model_allowed and not self.model_allowed(model))):
            raise ProviderError("model_not_found", "model is absent from the deployment catalogue")
        return self.caps.get(model) or cloud_capabilities(32768, 4096)

    def _cost(self, model: str) -> Callable[[TokenUsage], float]:
        price_in, price_out = self.prices.get(model, (0.0, 0.0))

        def cost(usage: TokenUsage) -> float:
            return (usage.input * price_in + usage.output * price_out) / 1_000_000

        return cost

    async def _prepare(self, model: str, messages: list[Message], tools: list[ToolSchema], settings: dict[str, Any],
                       stream: bool) -> tuple[dict[str, Any], dict[str, str], EffortRecord]:  # fmt: skip
        self._permission("tutor")
        if self.strict_models:
            await self.capabilities(model)
        body = request_body(f"{self.slot}/{model}", messages, tools, self.kind, settings, stream)
        findings = self.redactor.findings(body)
        if findings:
            kinds = sorted({finding.kind for finding in findings})
            fields = [str(key) for key, value in body.items() if self.redactor.blocks(value)]
            log.warning(f"gateway_redaction_blocked role={settings.get('role')} kinds={kinds} fields={fields}")
            raise ProviderError("content_filter", "the redaction check blocked the request before it left (MDL-25)",
                                code="redaction:" + ",".join(kinds))  # fmt: skip
        headers = {"x-session-pseudonym": self.pseudonym}
        role = settings.get("role")
        if role in {
            "coach",
            "solver",
            "checker",
            "router",
            "reflection",
            "vision",
            "title",
            "compaction",
            "input_guard",
            "rule_matcher",
            "research_chat",
            "companion",  # SPEC §19.4 (W8): the gateway counts the thread's turns and holds their bound
            "reflector",  # and the Reflector's jobs under tutor_memory_online, each under its own feature
        }:
            headers["x-jana-role"] = str(role)
        scope = current_scope()
        if role in ("companion", "reflector") and scope is not None:
            # A turn/job has one operation across tools, repairs, retries and provider objects. Send neither its
            # local ID nor a learner ID; rotation of this token never changes the authenticated account's cap.
            day = time.strftime("%Y-%m-%d", time.gmtime())
            headers["x-jana-operation"] = hashlib.sha256(f"{day}:{scope.kind}:{scope.id}".encode()).hexdigest()
        if self.token is not None:
            try:
                headers["authorization"] = f"Bearer {await self.token()}"
            except CredentialRefused as refused:
                raise ProviderError("auth", str(refused), code="credential_refused") from None
            except EgressRefused as refused:
                raise self._refused(refused) from None
            except NetError as error:
                raise self._network(error) from None
        self._permission("tutor")
        effort = settings.get("effort", "none")
        self.requests.append(body)
        return body, headers, EffortRecord(settings.get("effort_requested", effort), effort)

    def _failure(self, reply: Response) -> ProviderError:
        error = reply.body.get("error") if isinstance(reply.body, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if reply.status == 503 and code == "provider_unavailable":
            return ProviderError("network_unavailable", "OPS-12: cloud path disabled; use local fallback",
                                 code="http_503:provider_unavailable")  # fmt: skip
        if reply.status == 502 and code == "upstream_unreachable":
            # The gateway answered but could not reach the model provider: on the laptop's own gateway that is the
            # power cut itself (W3). The call is served locally at once, and the connectivity layer re-probes before
            # the cloud is tried again, instead of each call retrying a dead route with backoff.
            self._report("offline")
            return ProviderError("network_unavailable", "the gateway cannot reach the model provider",
                                 code="http_502:upstream_unreachable")  # fmt: skip
        if reply.status == 429 and code == "plan_limit":
            # SPEC §19.7 (W8): the companion's or the Reflector's daily cap. Only that feature stops for the day: no
            # retry, no pause of the cloud tier (a quota error would pause it), and the call is served locally.
            return ProviderError("content_filter", "plan_limit: the feature's daily cap at the gateway",
                                 code="http_429:plan_limit")  # fmt: skip
        failed = provider_error(reply.status, reply.body, _detail(reply.body), reply.headers)
        failed.code = f"http_{reply.status}" + (f":{code}" if isinstance(code, str) and _CODE.fullmatch(code) else "")
        return failed

    def _refused(self, refused: EgressRefused) -> ProviderError:
        # Off, offline, a declined notice: the cloud is unavailable and the router serves the call locally.
        if refused.reason in ("offline",):
            self._report("offline")
        return ProviderError("network_unavailable", f"egress refused: {refused.reason}",
                             code=f"egress_{refused.reason}")  # fmt: skip

    def _network(self, error: NetError) -> ProviderError:
        self._report("limited" if error.kind == "tls" else "offline")
        if error.kind == "timeout":
            return ProviderError("timeout", error.detail, code="net_timeout")
        return ProviderError("network_unavailable", error.kind, code=f"net_{error.kind}" if _CODE.fullmatch(error.kind)
                             else "net_error")  # fmt: skip

    def _report(self, state: str) -> None:
        if self.on_network_failure is not None:
            self.on_network_failure(state)

    async def complete(
        self, model: str, messages: list[Message], tools: list[ToolSchema], **settings: Any
    ) -> AssistantTurn:
        body, headers, effort = await self._prepare(model, messages, tools, settings, stream=False)
        started = time.monotonic()
        try:
            reply: Response = await self.client.send(FEATURE, "POST", f"https://{self.host()}{CHAT}", PAYLOAD,
                                                     body=body, headers=headers, profile_id=self.profile_id,
                                                     timeout_s=settings.get("timeout_s", self.request_timeout_s),
                                                     announce=False)  # fmt: skip
        except EgressRefused as refused:
            raise self._refused(refused) from None
        except NetError as error:
            raise self._network(error) from None
        if reply.status >= 400:
            raise self._failure(reply)
        if not isinstance(reply.body, dict) or "choices" not in reply.body:
            raise ProviderError("bad_response", "the reply is not a chat completion")
        return parse_reply(
            reply.body, model, effort, self._cost(model), int((time.monotonic() - started) * 1000), kind=self.kind
        )

    async def segment(self, body: dict[str, Any], timeout_s: float = 20.0) -> dict[str, Any]:
        """SAM 3.1 masks for a photo the local privacy pass already screened (JM-D10): the gateway's
        `/v1/vision/segment`, with the same feature, payload class, pseudonym and credential as a cloud read. `body`
        is the route's request (the image, its size, and object prompts or one concept); the reply is its objects."""
        self._permission("segmentation")
        if self.redactor.findings({k: v for k, v in body.items() if k != "image"}):
            raise ProviderError("content_filter", "the redaction check blocked the request before it left (MDL-25)")
        headers = {"x-session-pseudonym": self.pseudonym, "x-jana-role": "vision"}
        if self.token is not None:
            try:
                headers["authorization"] = f"Bearer {await self.token()}"
            except CredentialRefused as refused:
                raise ProviderError("auth", str(refused), code="credential_refused") from None
            except EgressRefused as refused:
                raise self._refused(refused) from None
            except NetError as error:
                raise self._network(error) from None
        try:
            self._permission("segmentation")
            reply: Response = await self.client.send(FEATURE, "POST", f"https://{self.host()}{SEGMENT}", PAYLOAD,
                                                     body=body, headers=headers, profile_id=self.profile_id,
                                                     timeout_s=timeout_s, announce=False)  # fmt: skip
        except EgressRefused as refused:
            raise self._refused(refused) from None
        except NetError as error:
            raise self._network(error) from None
        if reply.status >= 400:
            raise self._failure(reply)
        if not isinstance(reply.body, dict) or not isinstance(reply.body.get("objects"), list):
            raise ProviderError("bad_response", "the reply is not a segmentation")
        return reply.body

    async def stream(
        self, model: str, messages: list[Message], tools: list[ToolSchema], **settings: Any
    ) -> AsyncIterator[StreamChunk]:
        body, headers, effort = await self._prepare(model, messages, tools, settings, stream=True)
        started = time.monotonic()

        async def events() -> AsyncIterator[dict[str, Any]]:
            try:
                async for event in self.client.stream_events(
                    FEATURE, f"https://{self.host()}{CHAT}", PAYLOAD, body, headers=headers,
                    profile_id=self.profile_id, first_byte_timeout_s=settings.get("first_token_timeout_s"),
                ):  # fmt: skip
                    if isinstance(event, Response):
                        raise self._failure(event)
                    yield event
            except EgressRefused as refused:
                raise self._refused(refused) from None
            except NetError as error:
                raise self._network(error) from None

        async for chunk in assemble_stream(events(), model, effort, self._cost(model), started, kind=self.kind):
            yield chunk


_CODE = re.compile(r"[a-z][a-z0-9_]{0,40}")  # an error code, never free text


def _detail(body: Any) -> str:
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            return str(error.get("message", ""))[:200]
    return str(body)[:200]
