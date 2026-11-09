"""The llama.cpp `llama-server` adapter (MDL-01, MDL-20 subset).

The runtime is launched and supervised by the shell (D19) in router mode, on a loopback port with a per-launch key;
this adapter is given the address and key. Requests name the model id the server lists under `/v1/models`. The
runtime counts as ready only after a real short generation through the tool template (MDL-20): a health reply is
not readiness."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from agent_core.net.loopback import LoopbackClient, LoopbackError, Response
from agent_core.providers.base import (
    AssistantTurn,
    EffortRecord,
    Message,
    ModelCapabilities,
    ProviderError,
    StreamChunk,
    ToolSchema,
)
from agent_core.providers.errors import classify_transport, provider_error
from agent_core.providers.openai_compat import assemble_stream, grammar_friendly, parse_reply, request_body

CHAT = "/v1/chat/completions"
READY_TOOL = ToolSchema("noop", "Does nothing.", {"type": "object", "properties": {}})


def _free(_: object) -> float:
    return 0.0  # local inference has no per-token cost


# The roles with the longest, most repeated prompts each keep a slot of their own; the short auxiliary roles share the
# last one. With fewer slots than this needs, nothing is pinned and the runtime picks by prompt similarity.
PINNED_ROLES = ("coach", "solver", "diagnoser")


# A model that always reasons and takes its amount as `reasoning_strength` (Muse Glimmer, JM-D11): the router's effort
# maps onto it, and "none" is its lowest level, since its reasoning cannot be turned off (SPEC §7).
STRENGTH = {"none": "low", "low": "low", "medium": "medium", "high": "high"}


def role_slot(role: str, slots: int) -> int | None:
    """The slot a role's calls go to (D123), or None to let llama-server choose."""
    if slots < len(PINNED_ROLES) + 1:
        return None
    return PINNED_ROLES.index(role) if role in PINNED_ROLES else len(PINNED_ROLES)


class LlamaServerProvider:
    kind = "local"

    def __init__(self, base_url: str, api_key: str, request_timeout_s: float = 300.0,
                 *, model_families: Mapping[str, str] | None = None) -> None:
        self.client = LoopbackClient(base_url, api_key)
        self.request_timeout_s = request_timeout_s
        self._caps: dict[str, ModelCapabilities] = {}
        self._model_families = dict(model_families or {})
        self.used: set[str] = set()  # models this core has prompted, whose slots may hold a profile's tokens
        self._slots: dict[str, int] = {}  # a model's slot count, from /props (MDL-22)
        self._strength: set[str] = set()  # models whose template takes `reasoning_strength` (Muse Glimmer)
        self._real = 0  # real calls in flight; the warm-up waits for none (D128)
        self._idle = asyncio.Event()
        self._idle.set()
        self._warm_call: asyncio.Task[Any] | None = None
        self.warming: asyncio.Task[int] | None = None

    async def close(self) -> None:
        self.stop_warming()
        await self.client.close()

    # --- prefix warm-up (D128) ----------------------------------------------------------------------------

    def start_warming(self, model: str, requests: Sequence[tuple[str, list[Message], list[ToolSchema]]]) -> None:
        """Read each role's static prompt prefix (its system prompt and tools) into the role's pinned slot in the
        background, so the first real call of each role reads only its own task (ARC-22: Home is usable meanwhile;
        MDL-22, D123). A real call always goes first: the warm-up waits while one runs and abandons the request it
        has in flight when one starts, reading it again afterwards. A new warm-up replaces a running one."""
        self.stop_warming()
        self.warming = asyncio.get_running_loop().create_task(self._warm(model, list(requests)))

    def stop_warming(self) -> None:
        if self.warming is not None and not self.warming.done():
            self.warming.cancel()
        if self._warm_call is not None and not self._warm_call.done():
            self._warm_call.cancel()
        self.warming = self._warm_call = None

    async def _warm(self, model: str, requests: list[tuple[str, list[Message], list[ToolSchema]]]) -> int:
        await self.capabilities(model)  # the slot count, for pinning
        warmed = 0
        for role, messages, tools in requests:
            for _ in range(3):  # a request abandoned for a real call is read again, a few times at most
                await self._idle.wait()
                body, _ = self._body(model, [*messages, Message("user", ".")], tools,
                                     {"role": role, "max_tokens": 1, "temperature": 0, "effort": "none"},
                                     stream=False)  # fmt: skip
                self._warm_call = asyncio.ensure_future(self.client.post(CHAT, body, self.request_timeout_s))
                try:
                    reply = await self._warm_call
                except asyncio.CancelledError:
                    if self.warming is None or self.warming.cancelling():
                        raise
                    continue  # a real call started: wait for it, then read this prefix again
                except LoopbackError:
                    return warmed  # the runtime went away; the next launch warms again
                warmed += reply.status < 400
                break
        return warmed

    def _begin_real(self) -> None:
        self._real += 1
        self._idle.clear()
        if self._warm_call is not None and not self._warm_call.done():
            self._warm_call.cancel()  # closing the request makes llama-server stop reading it

    def _end_real(self) -> None:
        self._real -= 1
        if self._real == 0:
            self._idle.set()

    async def clear_cache(self) -> int:
        """Erase every slot's KV cache of the models this core has used, so no token of one profile's prompts
        survives a switch to another (DAT-14). Returns the slots erased; an unreachable runtime is left alone."""
        erased = 0
        for model in sorted(self.used):
            try:
                listed = await self.client.get(f"/slots?model={model}", 10)
                for slot in listed.body if isinstance(listed.body, list) else []:
                    done = await self.client.post(f"/slots/{int(slot['id'])}?action=erase&model={model}", {}, 10)
                    erased += done.status < 400
            except LoopbackError:
                continue
        return erased

    # --- models ---------------------------------------------------------------------------------------

    async def installed(self) -> list[str]:
        """Model ids the server can load (router mode lists every model in its models directory), with each model's
        aliases: a server started with `--alias a,b` lists one of them as the id and answers to both."""
        reply = await self._call(self.client.get("/v1/models", 10))
        names: set[str] = set()
        for m in (reply.body or {}).get("data", []):
            if isinstance(m, dict) and "id" in m:
                names.add(str(m["id"]))
                names.update(str(a) for a in m.get("aliases") or [] if isinstance(a, str))
        return sorted(names)

    async def capabilities(self, model: str) -> ModelCapabilities:
        if model in self._caps:
            return self._caps[model]
        props = (await self._call(self.client.get(f"/props?model={model}", 30))).body or {}
        caps = props.get("chat_template_caps") or {}
        settings = props.get("default_generation_settings") or {}
        context = int(settings.get("n_ctx") or props.get("n_ctx") or 0)
        template = str(props.get("chat_template") or "")
        strength = "reasoning_strength" in template
        thinking = "enable_thinking" in template or "<think>" in template
        meta = props.get("model_meta")
        architecture = meta.get("general.architecture") if isinstance(meta, dict) else None
        result = ModelCapabilities(
            tools=bool(caps.get("supports_tools", caps.get("supports_tool_calls", False))),
            parallel_tools=bool(caps.get("supports_parallel_tool_calls", False)),
            vision=bool((props.get("modalities") or {}).get("vision", False)),
            json_schema=True,  # llama-server converts JSON Schema to a grammar at decode time
            json_object=True,
            grammar=True,
            reasoning="effort" if strength else "toggle" if thinking else "none",
            reasoning_default="low" if strength else "on" if thinking else "none",
            context_window=context,
            context_for_agents=context,
            max_output=context,
            # Aliases are arbitrary labels. Prefer the core's verified GGUF header; otherwise use reported
            # architecture metadata or the neutral common prompt, never infer a vendor from the alias.
            family=self._model_families.get(model) or (architecture if isinstance(architecture, str) else ""),
            locality="local",
        )
        self._caps[model] = result
        self._slots[model] = int(props.get("total_slots") or 0)
        if strength:
            self._strength.add(model)
        return result

    async def ready(self, model: str) -> bool:
        """A real 8-token generation through the tool template succeeds (MDL-20)."""
        body = request_body(
            model,
            [Message("user", "Reply with OK.")],
            [READY_TOOL],
            self.kind,
            {"max_tokens": 8, "temperature": 0},
            stream=False,
        )
        body["chat_template_kwargs"] = {"enable_thinking": False}
        try:
            reply = await self.client.post(CHAT, body, 120)
        except LoopbackError:
            return False
        choices = reply.body.get("choices") if isinstance(reply.body, dict) else None
        return (
            reply.status == 200
            and isinstance(choices, list)
            and bool(choices)
            and isinstance(choices[0].get("message"), dict)
        )

    async def alive(self, timeout_s: float = 2.0) -> bool:
        """The process still listens (any HTTP status, or a slow reply): liveness for the core's view of the runtime the
        shell supervises, never readiness. Only nothing listening or a dropped connection counts as gone."""
        try:
            await self.client.get("/health", timeout_s)
        except LoopbackError as error:
            return error.kind == "timeout"
        return True

    async def benchmark(self, model: str, prompt: str, generate: int, timeout_s: float) -> dict[str, float]:
        """One short generation without the prompt cache, timed by the runtime itself (PLT-08): prompt and
        generation speeds in tokens per second."""
        body = {"model": model, "prompt": prompt, "n_predict": generate, "cache_prompt": False, "temperature": 0}
        reply = await self._call(self.client.post("/completion", body, timeout_s))
        timings = (reply.body or {}).get("timings") or {}
        return {
            "prompt_tokens_s": float(timings.get("prompt_per_second") or 0.0),
            "generation_tokens_s": float(timings.get("predicted_per_second") or 0.0),
            "seconds": (float(timings.get("prompt_ms") or 0) + float(timings.get("predicted_ms") or 0)) / 1000,
        }

    # --- calls ----------------------------------------------------------------------------------------

    def _body(
        self, model: str, messages: list[Message], tools: list[ToolSchema], settings: dict[str, Any], stream: bool
    ) -> tuple[dict[str, Any], EffortRecord]:
        # llama-server compiles tool schemas into a lazy grammar: send them grammar-friendly (the host validates).
        friendly = [ToolSchema(t.name, t.description, grammar_friendly(t.parameters)) for t in tools]
        self.used.add(model)
        body = request_body(model, messages, friendly, self.kind, settings, stream)
        # The router maps the role's effort to this model's control (MDL-12): a toggle here. The class's thinking
        # budget is fixed at launch with the context and slots (MDL-18); none of them is re-negotiated per request.
        effort = settings.get("effort", "none")
        control = (settings.get("reasoning") or {}).get("control")
        if model in self._strength:
            body["chat_template_kwargs"] = {"reasoning_strength": STRENGTH.get(str(effort), "low")}
        else:
            body["chat_template_kwargs"] = {"enable_thinking": control != "off" if control else effort != "none"}
        body["cache_prompt"] = True
        slot = role_slot(str(settings.get("role") or ""), self._slots.get(model, 0))
        if slot is not None:
            body["id_slot"] = slot  # the role's own slot keeps its prompt prefix cached between calls (MDL-22, D123)
        return body, EffortRecord(settings.get("effort_requested", effort), effort)

    async def complete(
        self, model: str, messages: list[Message], tools: list[ToolSchema], **settings: Any
    ) -> AssistantTurn:
        body, effort = self._body(model, messages, tools, settings, stream=False)
        started = time.monotonic()
        self._begin_real()
        try:
            reply = await self._call(self.client.post(CHAT, body, settings.get("timeout_s", self.request_timeout_s)))
        finally:
            self._end_real()
        if not isinstance(reply.body, dict) or "choices" not in reply.body:
            raise ProviderError("bad_response", "the reply is not a chat completion")
        return parse_reply(reply.body, model, effort, _free, int((time.monotonic() - started) * 1000))

    async def stream(
        self, model: str, messages: list[Message], tools: list[ToolSchema], **settings: Any
    ) -> AsyncIterator[StreamChunk]:
        body, effort = self._body(model, messages, tools, settings, stream=True)
        started = time.monotonic()

        async def events() -> AsyncIterator[dict[str, Any]]:
            try:
                async for event in self.client.stream_events(CHAT, body, settings.get("first_token_timeout_s")):
                    if isinstance(event, Response):
                        raise provider_error(event.status, event.body, _detail(event.body), event.headers)
                    yield event
            except LoopbackError as error:
                raise ProviderError(classify_transport(error.kind, self.kind), error.detail) from None

        self._begin_real()
        try:
            async for chunk in assemble_stream(events(), model, effort, _free, started):
                yield chunk
        finally:
            self._end_real()

    async def _call(self, pending: Any) -> Response:
        try:
            reply: Response = await pending
        except LoopbackError as error:
            raise ProviderError(classify_transport(error.kind, self.kind), error.detail) from None
        if reply.status >= 400:
            raise provider_error(reply.status, reply.body, _detail(reply.body), reply.headers)
        return reply


def _detail(body: Any) -> str:
    """A short, non-student detail for logs (the loop never acts on it)."""
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            return str(error.get("message", ""))[:200]
    return str(body)[:200]
