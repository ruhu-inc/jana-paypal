"""Classifying provider failures into the reasons of TABLE MDL-07a, once, at the provider boundary (MDL-07)."""

from __future__ import annotations

import email.utils
import re
import time
from typing import Any

from agent_core.providers.base import ProviderError

REASONS = (
    "rate_limit",
    "overloaded",
    "server_error",
    "timeout",
    "network_unavailable",
    "auth",
    "quota_or_billing",
    "context_overflow",
    "payload_too_large",
    "model_not_found",
    "content_filter",
    "format_error",
    "grammar_rejected",
    "runtime_down",
    "out_of_memory",
    "bad_response",
    "unknown",
)

_CONTEXT = (
    "context length",
    "context window",
    "exceeds the available context",
    "prompt is too long",
    "n_ctx",
    "maximum context",
    "too many tokens",
)
_GRAMMAR = ("grammar", "json_schema", "json-schema", "json schema", "unsupported schema", "failed to parse schema",
            "unable to generate parser")  # fmt: skip
_MEMORY = ("out of memory", "failed to allocate", "cudamalloc", "unable to allocate", "kv cache")
_QUOTA = ("insufficient_quota", "quota", "billing", "credit")
_FILTER = ("content_filter", "content_policy", "safety system", "flagged")
# A parameter an endpoint names as unsupported, and the replacement it suggests (MDL-11).
_PARAM = re.compile(r"(?:unsupported|unknown|unrecognized|invalid) (?:parameter|argument|field)s?:?\s+"
                    r"['\"`]?([A-Za-z_]\w*)", re.I)  # fmt: skip
_INSTEAD = re.compile(r"\buse ['\"`]?([A-Za-z_]\w*)['\"`]? instead", re.I)


def _text(body: Any) -> str:
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            return " ".join(str(error.get(k, "")) for k in ("type", "code", "message")).lower()
        return str(error).lower()
    return str(body or "").lower()


def classify_http(status: int, body: Any = None) -> str:
    """A reason for an HTTP error reply (status ≥ 400)."""
    text = _text(body)
    if status == 429:
        return "quota_or_billing" if any(k in text for k in _QUOTA) else "rate_limit"
    if status in (503, 529):
        return "overloaded"
    if status in (401, 403):
        return "auth"
    if status == 402:
        return "quota_or_billing"
    if status == 404:
        return "model_not_found"
    if status == 413:
        return "payload_too_large"
    if status in (408, 504):
        return "timeout"
    if status in (400, 422):
        if any(k in text for k in _CONTEXT):
            return "context_overflow"
        if any(k in text for k in _FILTER):
            return "content_filter"
        if any(k in text for k in _GRAMMAR):
            return "grammar_rejected"
        return "format_error"
    if status >= 500:
        if any(k in text for k in _MEMORY):
            return "out_of_memory"
        if any(k in text for k in _GRAMMAR):
            return "grammar_rejected"  # llama-server reports some grammar failures as 500
        if any(k in text for k in _CONTEXT):
            return "context_overflow"
        return "server_error"
    return "unknown"


def rejected_parameter(body: Any) -> tuple[str | None, str | None]:
    """The top-level request parameter a 400 reply names, and the replacement it suggests, if any (MDL-11). The
    structured `param` field wins over the message."""
    error = body.get("error", body) if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None, None
    message = str(error.get("message") or "")
    named = error.get("param")
    found = _PARAM.search(message)
    param = named.split(".")[0].split("[")[0] if isinstance(named, str) and named else found.group(1) if found else None
    instead = _INSTEAD.search(message)
    replacement = instead.group(1) if param and instead and instead.group(1) != param else None
    return param, replacement


def provider_error(status: int, body: Any, detail: str = "", headers: dict[str, str] | None = None) -> ProviderError:
    """The one classified error for an HTTP error reply, with the rejected parameter of a `format_error`."""
    reason = classify_http(status, body)
    param, replacement = rejected_parameter(body) if reason == "format_error" else (None, None)
    return ProviderError(reason, detail, status, retry_after_seconds(headers or {}), param, replacement)


def classify_transport(kind: str, locality: str) -> str:
    """A reason for a transport failure: `connect`, `timeout` or `reset` (agent_core.net)."""
    if kind == "timeout":
        return "timeout"
    if locality == "local":
        return "runtime_down"  # nothing listening, or the server dropped the connection mid-reply
    return "network_unavailable"


def retry_after_seconds(headers: dict[str, str], now: float | None = None) -> float | None:
    """`Retry-After` as seconds (delta-seconds or an HTTP date)."""
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        return max(0.0, parsed.timestamp() - (now if now is not None else time.time()))
