"""The router's trace of a model call (MDL-30), as the model-call record of the turn trace (OBS-04).

Each attempt (a retry or a fallback) is one `model_calls` entry, carrying the candidate list after filtering, the
prompt mode and, for an attempt that ended in a retry or fallback, its TABLE MDL-07a `reason`. No student text is
recorded."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_core.providers.base import AssistantTurn
from agent_core.trace import new_ulid

CONTRACT = "trace/turn-trace#/properties/model_calls/items"
# TABLE MDL-07a reasons onto the trace contract's coarser error classes.
ERROR_CLASS = {
    "rate_limit": "rate_limit",
    "overloaded": "overloaded",
    "timeout": "timeout",
    "context_overflow": "context_overflow",
    "model_not_found": "model_not_found",
    "content_filter": "content_policy",
    "format_error": "format",
    "grammar_rejected": "format",
    "network_unavailable": "network",
    "auth": "auth",
    "empty_twice": "empty",
    "schema_invalid_twice": "schema_invalid",
    "schema_invalid": "schema_invalid",
    "empty": "empty",
}
PROVIDER_KIND = {"local": "local", "cloud": "cloud", "school_server": "school_server"}
# The router's error kinds as TABLE MDL-07a reasons (the trace's `reason` enum).
MDL07_REASONS = frozenset({
    "rate_limit", "overloaded", "server_error", "timeout", "network_unavailable", "auth", "quota_or_billing",
    "context_overflow", "payload_too_large", "model_not_found", "content_filter", "format_error", "grammar_rejected",
    "runtime_down", "out_of_memory", "bad_response", "unknown",
})  # fmt: skip
REASON = {"empty": "bad_response", "empty_twice": "bad_response", "schema_invalid": "format_error",
          "schema_invalid_twice": "format_error"}  # fmt: skip
PROMPT_MODES = ("full", "compact", "minimal")
# The trace's `slot` for a call: "main" for a team role, the slot name for an auxiliary slot it names (MDL-06).
MAIN_ROLES = ("coach", "diagnoser", "solver", "checker", "reflector")
TRACE_SLOTS = ("title", "compaction", "judge", "reflection", "vision")
SLOT_ROLE = {"reflection": "reflector"}  # a slot whose role has another name in the trace


def mdl07_reason(error: str) -> str:
    return error if error in MDL07_REASONS else REASON.get(error, "unknown")


def attempt_record(
    *,
    role: str,
    tier: str,
    requested: str,
    provider_kind: str,
    attempt: int,
    total_ms: int,
    turn: AssistantTurn | None = None,
    error: str | None = None,
    fallback_from: str | None = None,
    effort_requested: str = "none",
    effort_effective: str | None = None,
    structured_mode: str = "none",
    schema_valid: bool | None = None,
    correction_retry: bool = False,
    queue_ms: int | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "request_id": new_ulid(),
        "role": SLOT_ROLE.get(role, role),
        "tier": tier,
        "model_requested": requested[:128],
        "model_served": (turn.served_by if turn else requested)[:128],
        "provider_kind": PROVIDER_KIND[provider_kind],
        "effort_requested": effort_requested,
        "effort_effective": effort_effective or (turn.effort.effective if turn else effort_requested),
        "attempt": attempt,
        "tokens": {"input": turn.usage.input if turn else 0, "output": turn.usage.output if turn else 0},
        "latency": {"total_ms": max(0, total_ms)},
        "cost_usd": turn.cost_usd if turn else 0.0,
        "finish_reason": turn.finish_reason if turn and error is None else "error",
        "structured_mode": structured_mode,
        "correction_retry": correction_retry,
    }
    if turn:
        usage = turn.usage
        for name in ("cache_read", "cache_write", "reasoning"):
            if getattr(usage, name):
                record["tokens"][name] = getattr(usage, name)
        timings = turn.timings
        for name in ("ttft_ms", "prompt_eval_ms", "generation_ms"):
            if getattr(timings, name) is not None:
                record["latency"][name] = getattr(timings, name)
        if timings.generation_ms and usage.output:
            record["tokens_per_s"] = round(usage.output / (timings.generation_ms / 1000), 2)
    if role in MAIN_ROLES or role in TRACE_SLOTS:
        record["slot"] = "main" if role in MAIN_ROLES else role
    if queue_ms is not None:
        record["latency"]["queue_ms"] = queue_ms
    if error:
        record["error_class"] = ERROR_CLASS.get(error, "other")
        record["reason"] = mdl07_reason(error)  # the retry or fallback that follows this attempt (TABLE MDL-07a)
    if fallback_from:
        record["fallback_from"] = fallback_from[:128]
    if schema_valid is not None:
        record["schema_valid"] = schema_valid
    return record


@dataclass
class CallRecord:
    """Everything the router decided for one model call."""

    role: str
    tier: str
    candidates: list[str]
    dropped: dict[str, str]  # candidate -> why it was filtered out
    prompt_mode: str | None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)  # retries and fallbacks with reasons, in order
    # Each provider error's diagnostic code (§19 W8 fix): what refused the call, never text; not a trace field.
    refusals: list[dict[str, Any]] = field(default_factory=list)
    efforts: list[dict[str, Any]] = field(default_factory=list)  # per candidate tried: the effort mapping (MDL-12)

    def model_calls(self) -> list[dict[str, Any]]:
        """The attempts as `model_calls` entries of the turn trace (MDL-30), with the candidate list after filtering
        and the prompt mode (each errored attempt already carries its reason)."""
        out = []
        for attempt in self.attempts:
            entry = {**attempt, "candidates": [c[:128] for c in self.candidates[:16]]}
            if self.prompt_mode in PROMPT_MODES:
                entry["prompt_mode"] = self.prompt_mode
            out.append(entry)
        return out
