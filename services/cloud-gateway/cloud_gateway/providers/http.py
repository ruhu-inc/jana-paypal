"""Shared HTTP plumbing for adapters: one JSON POST, one server-sent-events stream, transport failures classified.

Nothing here logs a request or a reply; headers (which carry the provider key) never leave this module except on
the wire."""

from __future__ import annotations

import email.utils
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from cloud_gateway.errors import classify_upstream
from cloud_gateway.providers.base import UpstreamError

ErrorClassifier = Callable[[int, Any], str]


def retry_after(headers: httpx.Headers) -> float | None:
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, IndexError):
            return None


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw or b"null")
    except ValueError:
        return raw[:500].decode(errors="replace")


def _param(body: Any) -> str | None:
    error = body.get("error") if isinstance(body, dict) else None
    param = error.get("param") if isinstance(error, dict) else None
    return param if isinstance(param, str) and param else None


# Statuses a provider returns before it processes the request (bad key, no access, rate limit): nothing is billed.
UNBILLED_STATUSES = frozenset({401, 403, 429})


def upstream_error(status: int, raw: bytes, headers: httpx.Headers, classify: ErrorClassifier) -> UpstreamError:
    body = _json(raw)
    reason = classify(status, body)
    extra = {"unbilled": True} if status in UNBILLED_STATUSES else {}
    return UpstreamError(reason, f"upstream status {status}", status, retry_after(headers), _param(body), extra)


def _transport(exc: httpx.HTTPError) -> UpstreamError:
    if isinstance(exc, httpx.TimeoutException):
        return UpstreamError("timeout", type(exc).__name__)
    return UpstreamError("network_unavailable", type(exc).__name__)


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: httpx.Timeout,
    classify: ErrorClassifier = classify_upstream,
) -> Any:
    try:
        response = await client.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.HTTPError as exc:
        raise _transport(exc) from None
    if response.status_code >= 400:
        raise upstream_error(response.status_code, response.content, response.headers, classify)
    try:
        return response.json()
    except ValueError:
        raise UpstreamError("bad_response", "reply is not JSON", response.status_code) from None


async def sse_events(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: httpx.Timeout,
    classify: ErrorClassifier = classify_upstream,
) -> AsyncIterator[Any]:
    """The JSON payload of every `data:` event; `[DONE]` ends the stream. An error status before the first event,
    an `error` event, or a transport failure raises `UpstreamError`."""
    try:
        async with client.stream("POST", url, headers=headers, json=body, timeout=timeout) as response:
            if response.status_code >= 400:
                raise upstream_error(response.status_code, await response.aread(), response.headers, classify)
            data: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data.append(line[5:].removeprefix(" "))
                    continue
                if line.strip() or not data:
                    continue
                payload, data = "\n".join(data), []
                if payload.strip() == "[DONE]":
                    return
                event = _json(payload.encode())
                if isinstance(event, dict) and "error" in event:
                    status = event["error"].get("code") if isinstance(event["error"], dict) else None
                    reason = classify(status if isinstance(status, int) else 500, event)
                    raise UpstreamError(reason, "error event in stream")
                if not isinstance(event, dict):
                    raise UpstreamError("bad_response", "unreadable stream event")
                yield event
            if data and "\n".join(data).strip() != "[DONE]":
                event = _json("\n".join(data).encode())
                if isinstance(event, dict):
                    yield event
    except httpx.HTTPError as exc:
        raise _transport(exc) from None
