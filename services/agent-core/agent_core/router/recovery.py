"""What the router does for each error reason (TABLE MDL-07a), and retry backoff (MDL-08).

The table is data: for each reason, how many times the same model is retried and what is done first (backoff,
compaction, shrinking, a parameter fix, stripping a grammar keyword, a runtime restart, a lower context rung), and
what happens when retries are exhausted (fall back, fall back to a local model, or fail). The loop acts on these
actions, never on raw provider messages."""

from __future__ import annotations

import random
from dataclasses import dataclass

from agent_core.config import Settings


@dataclass(frozen=True)
class Recovery:
    retries: int | str  # a count, or "config" for the configured same-model retries
    before_retry: str  # "backoff", "none", "compact", "shrink", "param_fix", "strip_keyword", "restart_runtime",
    #                    "lower_context"
    on_exhausted: str  # "fallback", "fallback_local", "fallback_another_local", "fallback_smaller_local", "fail"
    first: str | None = None  # an action taken once, before anything else ("mark_cloud_offline", ...)


RECOVERY: dict[str, Recovery] = {
    "rate_limit": Recovery("config", "backoff", "fallback"),
    "overloaded": Recovery("config", "backoff", "fallback"),
    "server_error": Recovery(2, "backoff", "fallback"),
    "timeout": Recovery(1, "none", "fallback"),
    "network_unavailable": Recovery(0, "none", "fallback_local", first="mark_cloud_offline"),
    "auth": Recovery(0, "none", "fallback", first="disable_candidate"),
    "quota_or_billing": Recovery(0, "none", "fallback", first="pause_cloud"),
    "context_overflow": Recovery(1, "compact", "fail"),
    "payload_too_large": Recovery(1, "shrink", "fail"),
    "model_not_found": Recovery(0, "none", "fallback"),
    "content_filter": Recovery(0, "none", "fallback_local"),
    "format_error": Recovery(1, "param_fix", "fallback"),
    "grammar_rejected": Recovery(1, "strip_keyword", "fail"),
    "runtime_down": Recovery(1, "restart_runtime", "fallback_another_local"),
    "out_of_memory": Recovery(1, "lower_context", "fallback_smaller_local"),
    "bad_response": Recovery(0, "none", "fallback"),
    "unknown": Recovery(1, "backoff", "fallback"),
}

# Bad-response subtypes as the router configuration's fallback triggers name them (MDL-09).
BAD_RESPONSE_TRIGGERS = ("schema_invalid_twice", "empty_twice", "unparsed_tool_call_twice")


def retries_for(reason: str, settings: Settings) -> int:
    count = RECOVERY[reason].retries
    return settings.router_retry_same_model if count == "config" else int(count)


def actions(reason: str, settings: Settings) -> list[str]:
    """The ordered actions for a failure that repeats on every attempt (the fixture view of the table)."""
    rule = RECOVERY[reason]
    steps = [rule.first] if rule.first else []
    for _ in range(retries_for(reason, settings)):
        steps += ([rule.before_retry] if rule.before_retry != "none" else []) + ["retry"]
    return [*steps, rule.on_exhausted]


@dataclass
class Backoff:
    """Exponential backoff with jitter: initial * factor^n, at most max, ±jitter; Retry-After honoured up to max."""

    settings: Settings
    rng: random.Random

    def delay(self, retry_index: int, retry_after_s: float | None = None) -> float:
        s = self.settings
        if retry_after_s is not None:
            return min(retry_after_s, s.router_backoff_max_s)
        base = min(s.router_backoff_initial_s * s.router_backoff_factor**retry_index, s.router_backoff_max_s)
        jittered = base * (1 + self.rng.uniform(-s.router_backoff_jitter, s.router_backoff_jitter))
        return min(jittered, s.router_backoff_max_s)

    def bounds(self, retry_index: int) -> tuple[float, float]:
        s = self.settings
        base = min(s.router_backoff_initial_s * s.router_backoff_factor**retry_index, s.router_backoff_max_s)
        return base * (1 - s.router_backoff_jitter), min(base * (1 + s.router_backoff_jitter), s.router_backoff_max_s)
