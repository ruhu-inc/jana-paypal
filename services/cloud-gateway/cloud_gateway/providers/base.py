"""The provider-adapter interface (MDL-26): every cloud provider is reached through the same two calls over the
neutral OpenAI chat-completions shape the device speaks, so the router and the gateway stay provider-agnostic and
all provider-specific behaviour lives in its adapter.

An adapter receives a `ProviderRequest` whose body has already passed the gateway's minimisation and field
allow-list, and returns an OpenAI-shaped reply (or chunks). Failures are raised as `UpstreamError` with one reason
from the device's classification (MDL-07); the gateway maps the reason to the status the device classifies."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, Protocol

from cloud_gateway.config import ModelSpec


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0  # reasoning tokens included: they are billed as output
    cached_tokens: int = 0
    reasoning_tokens: int = 0

    def openai(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.input_tokens,
            "completion_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "prompt_tokens_details": {"cached_tokens": self.cached_tokens},
            "completion_tokens_details": {"reasoning_tokens": self.reasoning_tokens},
        }


@dataclass(frozen=True)
class ProviderRequest:
    model: ModelSpec  # the concrete model the slot and alias resolve to
    body: dict[str, Any]  # the allow-listed OpenAI chat-completions body (model and stream fields removed)
    pseudonym: str  # the per-session random pseudonym (PRV-25); the only per-user value a provider may receive
    role: str = "unspecified"  # the engine role or gateway feature, for the budget ledger only; never sent upstream


@dataclass(frozen=True)
class ProviderReply:
    body: dict[str, Any]  # an OpenAI chat.completion object
    usage: Usage


@dataclass(frozen=True)
class StreamPart:
    """One OpenAI chat.completion.chunk, or the final usage (then `chunk` is None)."""

    chunk: dict[str, Any] | None
    usage: Usage | None = None


@dataclass
class UpstreamError(Exception):
    reason: str  # a reason of TABLE MDL-07a (rate_limit, quota_or_billing, overloaded, server_error, ...)
    message: str = ""
    upstream_status: int | None = None
    retry_after_s: float | None = None
    param: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.reason}: {self.message}" if self.message else self.reason


class ProviderAdapter(Protocol):
    async def complete(self, request: ProviderRequest) -> ProviderReply: ...

    def stream(self, request: ProviderRequest) -> AsyncGenerator[StreamPart]: ...

    async def aclose(self) -> None: ...
