"""The provider interface every model backend implements (MDL-01, MDL-02).

The agent core is asynchronous, so `complete` and `stream` are coroutines with the specified names, arguments and
types. Providers never loop, call tools or retry (MDL-02): the turn engine owns the loop and the router owns retry
and fallback. Every failure is raised as `ProviderError` with exactly one reason from TABLE MDL-07a."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal["stop", "tool_calls", "length", "content_filter"]
Effort = Literal["none", "low", "medium", "high"]
ReasoningKind = Literal["none", "toggle", "effort", "budget"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    # Opaque provider-private replay data; never a tool argument or displayed reasoning.
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    # Display only: never replayed to any model (MDL-02).
    reasoning: str | None = None
    # Provider-private sidecars, keyed by provider kind; stripped before a request goes to another provider.
    extras: dict[str, Any] = field(default_factory=dict)
    # Photos for a vision call (MDL-23), as JPEG data URLs the engine made safe first (SEC-05); empty otherwise.
    images: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolSchema:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class TokenUsage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0


@dataclass(frozen=True)
class EffortRecord:
    requested: Effort = "none"
    effective: Effort = "none"


@dataclass(frozen=True)
class Timings:
    ttft_ms: int | None = None
    prompt_eval_ms: int | None = None
    generation_ms: int | None = None
    total_ms: int = 0


@dataclass(frozen=True)
class AssistantTurn:
    text: str
    tool_calls: list[ToolCall]
    finish_reason: FinishReason
    reasoning: str | None
    structured: dict[str, Any] | None
    usage: TokenUsage
    cost_usd: float
    served_by: str
    effort: EffortRecord
    extras: dict[str, Any] = field(default_factory=dict)
    timings: Timings = field(default_factory=Timings)


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True)
class ToolCallDelta:
    index: int
    id: str | None
    name: str | None
    arguments: str  # a fragment of the JSON arguments


@dataclass(frozen=True)
class FinalTurn:
    turn: AssistantTurn


StreamChunk = TextDelta | ReasoningDelta | ToolCallDelta | FinalTurn


@dataclass(frozen=True)
class ModelCapabilities:
    tools: bool
    parallel_tools: bool
    vision: bool
    json_schema: bool
    json_object: bool
    grammar: bool
    reasoning: ReasoningKind
    reasoning_default: str
    context_window: int
    context_for_agents: int
    max_output: int
    family: str
    locality: Literal["local", "cloud"]


class ProviderError(Exception):
    """A classified provider failure (MDL-07). `detail` is for logs only; the loop acts on `reason`. A `format_error`
    may name the rejected request parameter and the endpoint's suggested replacement (MDL-11), read once at the
    provider boundary."""

    def __init__(
        self,
        reason: str,
        detail: str = "",
        status: int | None = None,
        retry_after_s: float | None = None,
        param: str | None = None,
        replacement: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        # A diagnostic code from a fixed vocabulary (`egress_notice_pending`, `redaction:term`, `http_429:plan_limit`):
        # what refused the call, safe for traces and API error details; never request or reply text.
        self.code = code
        self.status = status
        self.retry_after_s = retry_after_s
        self.param = param
        self.replacement = replacement


class ProviderClient(Protocol):
    kind: str  # "local", "cloud" or "school_server": the provider family, for stripping extras

    async def complete(
        self, model: str, messages: list[Message], tools: list[ToolSchema], **settings: Any
    ) -> AssistantTurn: ...

    def stream(
        self, model: str, messages: list[Message], tools: list[ToolSchema], **settings: Any
    ) -> AsyncIterator[StreamChunk]: ...

    async def capabilities(self, model: str) -> ModelCapabilities: ...
