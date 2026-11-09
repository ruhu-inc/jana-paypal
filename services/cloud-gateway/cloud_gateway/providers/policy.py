"""Whole-product service permission and declared model capabilities, before any upstream call."""

from collections.abc import AsyncGenerator, Callable
from typing import Any

from cloud_gateway.config import ProviderSpec
from cloud_gateway.providers.base import ProviderAdapter, ProviderReply, ProviderRequest, StreamPart, UpstreamError


def required_capabilities(body: dict[str, Any], *, stream: bool = False) -> frozenset[str]:
    required = {"text"}
    if stream or body.get("stream"):
        required.add("streaming")
    if body.get("tools"):
        required.add("tool_calls")
    if (body.get("response_format") or {}).get("type") in ("json_object", "json_schema"):
        required.add("structured_output")
    for message in body.get("messages") or []:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                required.add("vision")
            if isinstance(part, dict) and part.get("type") == "input_audio":
                required.add("audio")
    return frozenset(required)


class PermittedAdapter:
    """Also checks direct callers and startup probes, which do not all use slot resolution."""

    def __init__(
        self,
        adapter: ProviderAdapter,
        spec: ProviderSpec,
        clock: Callable[[], float],
        *,
        priced_models: frozenset[str] | None = None,
    ) -> None:
        self.adapter, self.spec, self.clock = adapter, spec, clock
        self.priced_models = priced_models

    def check(self, request: ProviderRequest, *, stream: bool = False) -> None:
        if self.priced_models is not None and request.model.id not in self.priced_models:
            raise UpstreamError("overloaded", "explicit model price unavailable", extra={"unbilled": True})
        permission = self.spec.permission
        if permission is None or not permission.permits(self.clock()):
            raise UpstreamError("overloaded", "whole-product service permission unavailable", extra={"unbilled": True})
        if not required_capabilities(request.body, stream=stream) <= request.model.capabilities:
            raise UpstreamError("format_error", "required model capability unavailable", extra={"unbilled": True})

    async def complete(self, request: ProviderRequest) -> ProviderReply:
        self.check(request)
        return await self.adapter.complete(request)

    async def stream(self, request: ProviderRequest) -> AsyncGenerator[StreamPart]:
        self.check(request, stream=True)
        parts = self.adapter.stream(request)
        try:
            async for part in parts:
                yield part
        finally:
            await parts.aclose()

    async def aclose(self) -> None:
        await self.adapter.aclose()
