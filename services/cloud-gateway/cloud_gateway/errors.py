"""Upstream failures in, device-facing errors out.

Adapters classify a provider's error reply into one reason of TABLE MDL-07a (`classify_upstream`). The gateway turns
the reason into the status and body the device's own classifier (agent_core.providers.errors.classify_http) reads
back as the same reason (`device_error`), with two deliberate exceptions: the provider rejecting *our* key is a
gateway fault (502), never the device's credential (401 is only for the device's own token); and every quota the
gateway enforces says "quota", so the device pauses the cloud tier and continues locally (MDL-24, BIZ-07).

Messages are fixed per reason and never echo request or reply text. Every error body is built through the
`GatewayError` contract model (D39)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cloud_gateway.contracts.cloud.gateway import GatewayError, GatewayErrorError

_CONTEXT = ("context length", "context window", "maximum context", "too many tokens", "prompt is too long",
            "exceeds the maximum number of tokens", "input token count")  # fmt: skip
_QUOTA = ("insufficient_quota", "quota", "billing", "credit")
_FILTER = ("content_filter", "content_policy", "safety", "blocked", "prohibited")
_SCHEMA = ("json_schema", "json schema", "response_schema", "responsejsonschema", "unsupported schema")


def _text(body: Any) -> str:
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            return " ".join(str(error.get(k, "")) for k in ("type", "code", "status", "message")).lower()
        return str(error).lower()
    if isinstance(body, list) and body:
        return _text(body[0])
    return str(body or "").lower()


def classify_upstream(status: int, body: Any) -> str:
    text = _text(body)
    if status == 429:
        return "quota_or_billing" if any(k in text for k in _QUOTA) else "rate_limit"
    if status in (401, 403):
        return "quota_or_billing" if "billing" in text else "upstream_auth"
    if status == 402:
        return "quota_or_billing"
    if status == 404:
        return "model_not_found"
    if status == 413:
        return "payload_too_large"
    if status in (408, 504):
        return "timeout"
    if status in (503, 529):
        return "overloaded"
    if status in (400, 422):
        if "api key" in text or "api_key" in text:
            return "upstream_auth"  # some providers answer a bad key with 400
        if any(k in text for k in _CONTEXT):
            return "context_overflow"
        if "billing" in text:
            return "quota_or_billing"
        if any(k in text for k in _SCHEMA):
            return "grammar_rejected"
        if any(k in text for k in _FILTER):
            return "content_filter"
        return "format_error"
    if status >= 500:
        return "server_error"
    return "bad_response"


@dataclass(frozen=True)
class DeviceError:
    status: int
    code: str
    message: str
    retry_after_s: float | None = None
    param: str | None = None

    def body(self) -> dict[str, Any]:
        return error_body(self.code, self.message, self.param)

    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(max(1, round(self.retry_after_s)))} if self.retry_after_s is not None else {}


def error_body(code: str, message: str, param: str | None = None) -> dict[str, Any]:
    """A `GatewayError` body: the code doubles as the type; the message and parameter are cut to the contract's
    bounds, never refused (an error reply must always be sendable)."""
    error = GatewayErrorError(code=code, type=code, message=message[:300], param=param[:80] if param else None)
    return GatewayError(error=error).model_dump(exclude_none=True)


# reason -> (status, code, message). The message carries the words the device's classifier reads.
_DEVICE: dict[str, tuple[int, str, str]] = {
    "rate_limit": (429, "rate_limit", "The model provider is rate limiting requests; retry later."),
    "quota_or_billing": (429, "insufficient_quota", "Cloud quota exhausted at the model provider."),
    "overloaded": (503, "overloaded", "The model provider is overloaded."),
    "server_error": (502, "upstream_error", "The model provider failed."),
    "upstream_auth": (502, "upstream_error", "The gateway could not authenticate with the model provider."),
    "timeout": (504, "timeout", "The model provider timed out."),
    "network_unavailable": (502, "upstream_unreachable", "The model provider is unreachable."),
    "context_overflow": (400, "context_length_exceeded", "The request exceeds the model's context window."),
    "payload_too_large": (413, "payload_too_large", "The request is too large."),
    "model_not_found": (404, "model_not_found", "No model is configured for this slot and alias."),
    "content_filter": (400, "content_filter", "The model provider's safety system blocked the request."),
    "grammar_rejected": (400, "json_schema_rejected", "The model provider rejected the json schema."),
    "format_error": (400, "invalid_request", "The model provider rejected the request."),
    "bad_response": (502, "bad_response", "The model provider sent an unreadable reply."),
}


def device_error(reason: str, retry_after_s: float | None = None, param: str | None = None) -> DeviceError:
    status, code, message = _DEVICE.get(reason, _DEVICE["server_error"])
    if param and reason == "format_error":
        message = f"The model provider rejected the request: unsupported parameter '{param}'."
    return DeviceError(status, code, message, retry_after_s, param if reason == "format_error" else None)


def auth_error(message: str) -> DeviceError:
    return DeviceError(401, "auth", message)


def gateway_quota(message: str, retry_after_s: float | None = None) -> DeviceError:
    """A gateway-enforced rate limit or spend cap: always reads as quota on the device."""
    return DeviceError(429, "quota", f"Cloud quota reached: {message}", retry_after_s)
