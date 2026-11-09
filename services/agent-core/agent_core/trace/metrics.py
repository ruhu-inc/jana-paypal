"""OBS-13: projection of local technical observations, with closed metric names."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from agent_core.config import Settings
from agent_core.config.telemetry_schema import METRICS, schema


def observe(metrics: dict[str, Any], name: str, value: Any, settings: Settings | None = None) -> None:
    if name not in METRICS or type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return
    bounds = (settings or Settings()).telemetry_histogram_bounds
    cell = metrics.setdefault(name, {"count": 0, "sum": 0, "buckets": [0] * (len(bounds) + 1)})
    cell["count"] += 1
    cell["sum"] += value
    bucket = next((i for i, boundary in enumerate(bounds) if value <= boundary), len(bounds))
    cell["buckets"][bucket] += 1


def merge(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for metrics in rows:
        for name, value in metrics.items():
            if name not in METRICS:
                raise ValueError("unknown technical metric")
            cell = result.setdefault(name, {"count": 0, "sum": 0, "buckets": [0] * len(value["buckets"])})
            cell["count"] += value["count"]
            cell["sum"] += value["sum"]
            cell["buckets"] = [a + b for a, b in zip(cell["buckets"], value["buckets"], strict=True)]
    return result


def from_traces(traces: Iterable[Mapping[str, Any]], settings: Settings | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def add(name: str, value: Any) -> None:
        observe(result, name, value, settings)

    for trace in traces:
        if trace.get("job_name") == "capture":
            photo = trace.get("perception", {})
            add("photo_captures", 1)
            add("photo_duration_ms", photo.get("durations_ms", {}).get("total"))
            if photo.get("outcome") == "confirmed":
                add("photo_confirmed", 1)
                if photo.get("confirm_method") == "edited":
                    add("photo_edited", 1)
            if photo.get("outcome") == "unreadable":
                add("photo_unreadable", 1)
            continue
        add("turn_duration_ms", trace.get("duration_ms"))
        status = trace.get("status")
        if status in ("complete", "error", "interrupted", "truncated"):
            add(f"turn_{status}", 1)
        if trace.get("compaction"):
            add("compactions", 1)
        if trace.get("correction"):
            add("correction_turns", 1)
        for call in trace.get("model_calls", []):
            add("model_calls", 1)
            for key, metric in (
                ("fallback", "model_fallbacks"),
                ("cache_hit", "model_cache_hits"),
                ("invalid", "model_invalid"),
                ("empty", "model_empty"),
            ):
                enabled = bool(call.get(key))
                if key == "fallback":
                    enabled |= bool(call.get("fallback_from"))
                elif key == "cache_hit":
                    enabled |= call.get("tokens", {}).get("cache_read", 0) > 0
                elif key == "invalid":
                    enabled |= call.get("schema_valid") is False
                elif key == "empty":
                    enabled |= call.get("error_class") == "empty"
                if enabled:
                    add(metric, 1)
            for key, metric in (
                ("ttft_ms", "ttft_ms"),
                ("prompt_tokens_per_s", "prompt_tokens_per_s"),
                ("generation_tokens_per_s", "generation_tokens_per_s"),
            ):
                add(metric, call.get("latency", {}).get(key))
            tokens, latency = call.get("tokens", {}), call.get("latency", {})
            for count, timing, metric in (
                ("input", "prompt_eval_ms", "prompt_tokens_per_s"),
                ("output", "generation_ms", "generation_tokens_per_s"),
            ):
                duration, amount = latency.get(timing), tokens.get(count)
                if type(duration) in (int, float) and duration > 0 and type(amount) in (int, float):
                    add(metric, amount * 1000 / duration)
            add("cloud_cost_usd", call.get("cost_usd"))
        for call in trace.get("tool_calls", []):
            add("tool_calls", 1)
            add("tool_duration_ms", call.get("duration_ms"))
            if call.get("error_code") or call.get("status") in ("error", "failed"):
                add("tool_failures", 1)
            add("tool_repairs", len(call.get("repairs", [])))
        for gate in trace.get("gates", []):
            if gate.get("fired"):
                add("gate_fired", 1)
            if gate.get("exhausted") or str(gate.get("outcome", "")).startswith("exhausted_"):
                add("gate_exhausted", 1)
        for guard in trace.get("guards", []):
            add("guard_triggered", guard.get("count", 1))
    return result


def catalogue(
    traces: list[dict[str, Any]], logs: list[dict[str, Any]], settings: Settings | None = None
) -> dict[str, Any]:
    """OBS-13: local dimensions, technical rates and exact per-tool quantiles from the recorded window."""
    import json
    from collections import defaultdict

    settings = settings or Settings()
    groups: dict[str, Any] = {}
    durations: dict[str, list[float]] = defaultdict(list)
    label_schema = schema()["properties"]["labels"]["properties"]

    def safe_labels(raw: dict[str, Any]) -> dict[str, str]:
        return {
            key: str(value) if value in label_schema[key]["enum"] else "other"
            for key, value in raw.items()
            if key in label_schema and (value in label_schema[key]["enum"] or "other" in label_schema[key]["enum"])
        }

    def put(build: dict[str, Any], tier: str, labels: dict[str, Any], metrics: dict[str, Any]) -> None:
        if not metrics:
            return
        metadata: dict[str, Any] = {
            key: build.get(key) for key in ("app_version", "channel", "hardware_class", "os_family")
        }
        metadata["router_tier"] = tier
        labels = safe_labels(labels)
        from agent_core.config.telemetry_schema import report

        # Validate dimensions before allowing them to become dictionary keys or durable data.
        report(metrics, days=1, labels=labels, **metadata)
        key = json.dumps({**metadata, "labels": labels}, sort_keys=True)
        old = groups.get(key, {}).get("metrics", {})
        groups[key] = {**metadata, "labels": labels, "metrics": merge([old, metrics])}

    for trace in traces:
        build = trace.get("build", {})
        calls = trace.get("model_calls", [])
        tier = calls[0].get("tier", "local") if calls else "local"
        tier = "lan" if tier == "school_server" else tier
        put(build, tier, {}, from_traces([trace], settings))
        if trace.get("kind", "turn") == "turn" and trace.get("mode") == "guided":
            values: dict[str, Any] = {}
            observe(values, "guided_step_ms", trace.get("duration_ms"), settings)
            put(build, tier, {}, values)
        for call in calls:
            call_tier = call.get("tier", tier)
            call_tier = "lan" if call_tier == "school_server" else call_tier
            put(build, call_tier, {"role": call.get("role", "other")}, from_traces([{"model_calls": [call]}], settings))
        for call in trace.get("tool_calls", []):
            name = safe_labels({"tool": call.get("name", "other")}).get("tool", "other")
            value = call.get("duration_ms")
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                durations[name].append(value)
            labels = {"tool": name}
            if call.get("error_code"):
                labels["error_code"] = call["error_code"]
            put(build, tier, labels, from_traces([{"tool_calls": [call]}], settings))
            for repair in call.get("repairs", []):
                values = {}
                observe(values, "tool_repairs", 1, settings)
                put(build, tier, {"tool": name, "repair_step": repair}, values)
        for gate in trace.get("gates", []):
            put(build, tier, {"gate": gate.get("gate", "other")}, from_traces([{"gates": [gate]}], settings))
    for row in logs:
        values = {}
        metric = row.get("metric")
        if metric in METRICS:
            observe(values, metric, row.get("value"), settings)
            labels = {key: row[key] for key in label_schema if key in row}
            put(row.get("build", row), row.get("router_tier", "local"), labels, values)
    tools = {}
    for name, samples in durations.items():
        ordered = sorted(samples)
        tools[name] = {
            "count": len(samples),
            "p50_ms": ordered[max(0, math.ceil(len(samples) * 0.5) - 1)],
            "p95_ms": ordered[max(0, math.ceil(len(samples) * 0.95) - 1)],
        }
    totals = merge([row["metrics"] for row in groups.values() if not row["labels"]])

    def ratio(numerator: str, denominator: str) -> float | None:
        den = totals.get(denominator, {}).get("sum", 0)
        return totals.get(numerator, {}).get("sum", 0) / den if den else None

    crashed = ratio("crashed_sessions", "sessions")
    return {
        "series": list(groups.values()),
        "tools": tools,
        "rates": {
            "model_fallback": ratio("model_fallbacks", "model_calls"),
            "invalid_output": ratio("model_invalid", "model_calls"),
            "tool_failure": ratio("tool_failures", "tool_calls"),
            "cache_hit": ratio("model_cache_hits", "model_calls"),
            "crash_free": None if crashed is None else 1 - crashed,
        },
    }
