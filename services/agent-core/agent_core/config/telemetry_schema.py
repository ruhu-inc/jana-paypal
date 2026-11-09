"""OBS-13, OBS-20, PRV-09: closed technical metrics; no learner fields."""

from __future__ import annotations

import copy
import math
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker

from agent_core.config.keys import Settings
from agent_core.contracts import document

METRICS = {
    "photo_captures": "perception",
    "photo_confirmed": "perception",
    "photo_edited": "perception",
    "photo_unreadable": "perception",
    "photo_duration_ms": "perception",
    "core_ready_ms": "startup",
    "ready_ms": "startup",
    "model_load_ms": "startup",
    "ttft_ms": "latency",
    "prompt_tokens_per_s": "latency",
    "generation_tokens_per_s": "latency",
    "guided_step_ms": "latency",
    "turn_duration_ms": "latency",
    "model_calls": "model",
    "model_fallbacks": "model",
    "model_invalid": "model",
    "model_empty": "model",
    "correction_turns": "model",
    "model_cache_hits": "model",
    "cloud_cost_usd": "model",
    "tool_calls": "tools",
    "tool_failures": "tools",
    "tool_repairs": "tools",
    "tool_duration_ms": "tools",
    "worker_restarts": "tools",
    "gate_fired": "guards",
    "gate_exhausted": "guards",
    "guard_triggered": "guards",
    "turn_complete": "turns",
    "turn_error": "turns",
    "turn_interrupted": "turns",
    "turn_truncated": "turns",
    "compactions": "turns",
    "sessions": "reliability",
    "crashed_sessions": "reliability",
    "process_restarts": "reliability",
    "process_giveups": "reliability",
    "peak_memory_mb": "resources",
    "cpu_share": "resources",
    "battery_drain_per_hour": "resources",
    "connectivity_seconds": "connectivity",
    "outbox_depth": "connectivity",
    "outbox_age_s": "connectivity",
    "egress_bytes": "connectivity",
    "download_failures": "connectivity",
}


def schema() -> dict[str, Any]:
    """The published report schema: the bundled `trace/telemetry` contract (OBS-20, D92)."""
    return document("trace/telemetry")


def problems(value: Any) -> list[str]:
    errors = [e.message for e in Draft202012Validator(schema(), format_checker=FormatChecker()).iter_errors(value)]

    def finite(node: Any) -> bool:
        if isinstance(node, dict):
            return all(finite(v) for v in node.values())
        if isinstance(node, list):
            return all(finite(v) for v in node)
        return not isinstance(node, float) or math.isfinite(node)

    if not finite(value):
        errors.append("non-finite measurement")
    if errors:
        return errors
    if value["days"] > Settings().retention_telemetry_days:
        errors.append("telemetry retention exceeded")
    metric_sets = [value["metrics"], *[row["metrics"] for row in value.get("series", [])]]
    for metrics in metric_sets:
        for cell in metrics.values():
            if sum(cell["buckets"]) != cell["count"]:
                errors.append("histogram count mismatch")

    return errors


def report(
    metrics: dict[str, Any],
    *,
    app_version: str,
    channel: str,
    hardware_class: str,
    os_family: str,
    router_tier: str,
    days: int,
    series: list[dict[str, Any]] | None = None,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    value = {
        "report_id": str(uuid4()),
        "app_version": app_version,
        "channel": channel,
        "hardware_class": hardware_class,
        "os_family": os_family,
        "router_tier": router_tier,
        "days": days,
        "metrics": copy.deepcopy(metrics),
    }
    if labels is not None:
        value["labels"] = copy.deepcopy(labels)
    if series is not None:
        value["series"] = copy.deepcopy(series)
    errors = problems(value)
    if errors:
        raise ValueError(errors[0])
    if days > Settings().retention_telemetry_days:
        raise ValueError("telemetry retention exceeded")
    return value
