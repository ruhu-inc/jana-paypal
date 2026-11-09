"""Adapter for any OpenAI-compatible chat-completions endpoint (a candidate fallback provider, D7 open).

The neutral request is already OpenAI-shaped, so this adapter only sets the concrete model, asks for usage on
streams, drops other providers' sidecars and, when configured, puts the session pseudonym in the provider's
end-user field. The key goes in the `Authorization` header only."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import httpx

from cloud_gateway.config import ProviderSpec
from cloud_gateway.custody import Secret
from cloud_gateway.providers.base import ProviderReply, ProviderRequest, StreamPart, UpstreamError, Usage
from cloud_gateway.providers.http import post_json, sse_events


def usage_of(raw: dict[str, Any] | None) -> Usage:
    raw = raw or {}
    prompt = raw.get("prompt_tokens_details") or {}
    completion = raw.get("completion_tokens_details") or {}
    return Usage(
        input_tokens=int(raw.get("prompt_tokens", 0) or 0),
        output_tokens=int(raw.get("completion_tokens", 0) or 0),
        cached_tokens=int(prompt.get("cached_tokens", 0) or 0),
        reasoning_tokens=int(completion.get("reasoning_tokens", 0) or 0),
    )


def _message(message: dict[str, Any]) -> dict[str, Any]:
    out = dict(message)
    if out.get("tool_calls"):
        out["tool_calls"] = [{k: v for k, v in c.items() if k != "extra_content"} for c in out["tool_calls"]]
    return out


class OpenAICompatAdapter:
    def __init__(self, spec: ProviderSpec, key: Secret | None, client: httpx.AsyncClient) -> None:
        self.spec = spec
        self._key = key
        self.client = client
        self.timeout = httpx.Timeout(spec.timeout_s, connect=spec.connect_timeout_s)

    def __repr__(self) -> str:
        return f"OpenAICompatAdapter(base_url={self.spec.base_url!r})"

    def _headers(self) -> dict[str, str]:
        if self._key is None:
            raise UpstreamError("upstream_auth", "no provider key in the secret source", extra={"unbilled": True})
        return {"authorization": f"Bearer {self._key.reveal()}", "content-type": "application/json"}

    def _body(self, request: ProviderRequest, stream: bool) -> dict[str, Any]:
        body = dict(request.body)
        body["messages"] = [_message(m) for m in body.get("messages") or []]
        body["model"] = request.model.id
        body["stream"] = stream
        if stream:
            body["stream_options"] = {"include_usage": True}
        if self.spec.pseudonym_field:
            body[self.spec.pseudonym_field] = request.pseudonym
        return body

    @property
    def _url(self) -> str:
        return f"{self.spec.base_url.rstrip('/')}/chat/completions"

    async def complete(self, request: ProviderRequest) -> ProviderReply:
        raw = await post_json(self.client, self._url, self._headers(), self._body(request, False), self.timeout)
        if not isinstance(raw, dict) or not raw.get("choices"):
            raise UpstreamError("bad_response", "reply has no choices")
        raw["model"] = raw.get("model") or request.model.id
        return ProviderReply(raw, usage_of(raw.get("usage")))

    async def stream(self, request: ProviderRequest) -> AsyncGenerator[StreamPart]:
        usage = Usage()
        events = sse_events(self.client, self._url, self._headers(), self._body(request, True), self.timeout)
        async for event in events:
            if event.get("usage"):
                usage = usage_of(event.pop("usage"))
            if event.get("choices"):
                event["model"] = event.get("model") or request.model.id
                yield StreamPart(event)
        yield StreamPart(None, usage)

    async def aclose(self) -> None:
        await self.client.aclose()
