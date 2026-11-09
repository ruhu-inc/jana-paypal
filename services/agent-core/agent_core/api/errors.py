"""The error envelope of the local API (API-32): every non-2xx response is `{error: {...}}` with a code from the
status table below. Messages are for logs; clients show localised copy keyed by `code`."""

from __future__ import annotations

from typing import Any

from agent_core.contracts import validator
from agent_core.engine.events import new_id

# HTTP status per error code (TABLE API-32a). The voice adapter's `tools_not_supported` uses its own error body.
STATUS: dict[str, int] = {
    **dict.fromkeys(("invalid_request", "invalid_cursor"), 400),
    **dict.fromkeys(("unauthorized", "profile_token_expired"), 401),
    **dict.fromkeys(
        (
            "forbidden_origin",
            "guardian_consent_required",
            "feature_disabled",
            "lan_server_unpinned",
            "replica_disabled",
        ),
        403,
    ),
    "not_found": 404,
    **dict.fromkeys(
        (
            "turn_active",
            "cursor_expired",
            "epoch_mismatch",
            "request_conflict",
            "already_answered",
            "capture_not_ready",
            "lan_fingerprint_mismatch",
            "review_changed",
        ),
        409,
    ),
    "request_expired": 410,
    **dict.fromkeys(("payload_too_large", "image_too_large"), 413),
    "unsupported_format": 415,
    "validation_failed": 422,
    **dict.fromkeys(("tutor_locked", "capture_locked"), 423),
    **dict.fromkeys(("upgrade_required", "lan_protocol_unsupported"), 426),
    "rate_limited": 429,
    "internal_error": 500,
    **dict.fromkeys(
        (
            "core_starting",
            "model_unavailable",
            "worker_unavailable",
            "network_unavailable",
            "photo_reading_unavailable",
            "lan_server_unreachable",
        ),
        503,
    ),
    "storage_full": 507,
}
# Codes the command contract names that TABLE API-32a has no row for yet (gap G81); kept apart so STATUS stays the
# table exactly.
PENDING_STATUS: dict[str, int] = {
    "scope_not_profile": 422,
    "archive_invalid": 422,
    "archive_unsupported_version": 422,
}
RETRYABLE = frozenset({"rate_limited", "core_starting", "model_unavailable", "network_unavailable",
                       "photo_reading_unavailable", "worker_unavailable", "lan_server_unreachable",
                       "internal_error"})  # fmt: skip


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        action_taken: bool = False,
        details: dict[str, Any] | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.status = STATUS[code] if code in STATUS else PENDING_STATUS[code]
        self.message = message or code.replace("_", " ")
        self.action_taken = action_taken
        self.details = details
        self.retry_after_ms = retry_after_ms

    def envelope(self, request_id: str | None = None) -> dict[str, Any]:
        return envelope(self.code, self.message, self.action_taken, self.details, self.retry_after_ms, request_id)


def envelope(
    code: str,
    message: str,
    action_taken: bool = False,
    details: dict[str, Any] | None = None,
    retry_after_ms: int | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "message": message[:2000],
        "retryable": code in RETRYABLE,
        "action_taken": action_taken,
        "request_id": request_id or new_id("req"),
    }
    if retry_after_ms is not None:
        error["retry_after_ms"] = retry_after_ms
    if details is not None:
        error["details"] = details
    return {"error": error}


def violations(name: str, instance: Any) -> list[str]:
    """Every contract violation of a request body as '<json path>: <rule>', in a stable order, with the offending
    value left out. An error's message and details are for logs (API-32), and a value may be what the student wrote,
    so none is ever echoed (PRV-09, OBS-19)."""
    return sorted({f"{e.json_path}: fails {e.validator}" for e in validator(name).iter_errors(instance)})


def invalid(name: str, instance: Any) -> ApiError | None:
    """`validation_failed` for a body that breaks its contract, value-free; None when it keeps it."""
    problems = violations(name, instance)
    return ApiError("validation_failed", problems[0], details={"errors": problems[:20]}) if problems else None


def cloud_refusal(error: ValueError) -> str:
    """The reason a Study cloud call was refused. The api layer may not import `agent_core.net` (ARC-18), so a refusal
    is known by its shape, as the engine knows it: a ValueError carrying a `reason`. Any other ValueError re-raises."""
    reason = getattr(error, "reason", None)
    if not isinstance(reason, str):
        raise error
    return reason
