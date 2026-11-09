"""Scripted and stub providers: deterministic, network-free (MDL-01).

`ScriptedProvider` replays a script of OpenAI-shaped replies, HTTP errors and transport failures through the same
request building, reply parsing and error classification as a real OpenAI-compatible provider, and keeps the
request bodies it would have sent. With `kind="cloud"` it is the fake cloud provider of the conformance suite;
recorded cassettes of real small-model replies replay through it. `CloudProviderStub` is the S1 cloud provider:
disabled, it fails every call as `network_unavailable` without any I/O."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

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
from agent_core.providers.errors import classify_transport, provider_error
from agent_core.providers.openai_compat import assemble_stream, parse_reply, request_body

Step = dict[str, Any]  # {"reply": {...}} | {"error": {"status", "body"?, "headers"?}} | {"transport": kind}


def default_capabilities(kind: str, context: int = 8192) -> ModelCapabilities:
    return ModelCapabilities(
        tools=True,
        parallel_tools=True,
        vision=False,
        json_schema=True,
        json_object=True,
        grammar=kind == "local",
        reasoning="toggle",
        reasoning_default="on",
        context_window=context,
        context_for_agents=context,
        max_output=4096,
        family="scripted",
        locality="local" if kind == "local" else "cloud",
    )


@dataclass
class ScriptedProvider:
    # A list of steps in order, a dict of step lists per role (the router passes the role), or a callable.
    script: list[Step] | dict[str, list[Step]] | Callable[[str, list[dict[str, Any]], dict[str, Any]], Step]
    kind: str = "local"
    caps: dict[str, ModelCapabilities] = field(default_factory=dict)
    installed_models: list[str] = field(default_factory=list)
    price_per_mtok: tuple[float, float] = (0.0, 0.0)  # input, output (USD per million tokens)
    requests: list[dict[str, Any]] = field(default_factory=list)
    chunk_chars: int = 3

    def _next(self, body: dict[str, Any], role: str | None = None) -> Step:
        self.requests.append({**body, "_role": role} if isinstance(self.script, dict) else body)
        if callable(self.script):
            return self.script(body["model"], body["messages"], body)
        steps = self.script.get(role or "", []) if isinstance(self.script, dict) else self.script
        if not steps:
            raise LookupError(f"the provider script is exhausted for role {role!r} (an unexpected extra call)")
        step = steps.pop(0)
        return step(body) if callable(step) else step  # a callable step sees the request (for ids made at run time)

    def _cost(self, usage: TokenUsage) -> float:
        return (usage.input * self.price_per_mtok[0] + usage.output * self.price_per_mtok[1]) / 1_000_000

    def _raise_for(self, step: Step) -> None:
        if "transport" in step:
            raise ProviderError(classify_transport(step["transport"], self.kind), step["transport"])
        if "error" in step:
            error = step["error"]
            raise provider_error(error["status"], error.get("body"), "", error.get("headers", {}))

    async def installed(self) -> list[str]:
        return list(self.installed_models)

    async def capabilities(self, model: str) -> ModelCapabilities:
        return self.caps.get(model) or default_capabilities(self.kind)

    async def complete(
        self, model: str, messages: list[Message], tools: list[ToolSchema], **settings: Any
    ) -> AssistantTurn:
        started = time.monotonic()
        step = self._next(request_body(model, messages, tools, self.kind, settings, stream=False), settings.get("role"))
        if "stall_s" in step:  # a slow model, for cancellation tests
            await asyncio.sleep(step["stall_s"])
        self._raise_for(step)
        effort = _effort(settings)
        return parse_reply(step["reply"], model, effort, self._cost, int((time.monotonic() - started) * 1000))

    async def stream(
        self, model: str, messages: list[Message], tools: list[ToolSchema], **settings: Any
    ) -> AsyncIterator[StreamChunk]:
        started = time.monotonic()
        step = self._next(request_body(model, messages, tools, self.kind, settings, stream=True), settings.get("role"))
        if "stall_s" in step:
            await asyncio.sleep(step["stall_s"])
        self._raise_for(step)
        effort = _effort(settings)
        async for chunk in assemble_stream(_as_events(step, self.chunk_chars), model, effort, self._cost, started):
            yield chunk


def _effort(settings: dict[str, Any]) -> EffortRecord:
    """The effort the router asked for and the effective level it mapped to this model (MDL-12)."""
    effective = settings.get("effort", "none")
    return EffortRecord(settings.get("effort_requested", effective), effective)


async def _as_events(step: Step, size: int) -> AsyncIterator[dict[str, Any]]:
    """A recorded stream as recorded, or a whole reply cut into small deltas."""
    if "events" in step:
        for event in step["events"]:
            if "transport" in event:
                raise ProviderError(classify_transport(event["transport"], "local"), event["transport"])
            yield event
        return
    reply = step["reply"]
    choice = (reply.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    model = reply.get("model")
    for key in ("reasoning_content", "content"):
        text = message.get(key) or ""
        for i in range(0, len(text), size):
            yield {"model": model, "choices": [{"delta": {key: text[i : i + size]}}]}
    for index, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        arguments = function.get("arguments") or ""
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": call.get("id"),
                                "function": {"name": function.get("name"), "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        }
        for i in range(0, len(arguments), size):
            yield {
                "choices": [
                    {"delta": {"tool_calls": [{"index": index, "function": {"arguments": arguments[i : i + size]}}]}}
                ]
            }
    yield {
        "model": model,
        "choices": [{"delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}],
        "usage": reply.get("usage"),
        "timings": reply.get("timings"),
    }


class CloudProviderStub:
    """The cloud tier in S1: off and network-free. Every call fails as `network_unavailable`."""

    kind = "cloud"

    async def installed(self) -> list[str]:
        return []

    async def capabilities(self, model: str) -> ModelCapabilities:
        raise ProviderError("network_unavailable", "the cloud tier is not part of this build")

    async def complete(
        self, model: str, messages: list[Message], tools: list[ToolSchema], **settings: Any
    ) -> AssistantTurn:
        raise ProviderError("network_unavailable", "the cloud tier is not part of this build")

    async def stream(
        self, model: str, messages: list[Message], tools: list[ToolSchema], **settings: Any
    ) -> AsyncIterator[StreamChunk]:
        raise ProviderError("network_unavailable", "the cloud tier is not part of this build")
        yield  # pragma: no cover - makes this an async generator
