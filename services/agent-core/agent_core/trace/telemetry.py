"""What leaves the trace store: telemetry aggregates and the default debug bundle (TOL-15, OBS-19, SEC-30).

Telemetry carries counts and codes only: tool names, statuses, error codes and durations, never argument or result
values, prompts or student text (TABLE OBS-19a). The default debug bundle carries trace metadata and logs; the
conversation is added only when an adult includes it."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

TELEMETRY_FIELDS = ("name", "status", "error_code", "executor", "toolset")


def telemetry_export(traces: list[dict[str, Any]]) -> dict[str, Any]:
    tools: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    durations: list[int] = []
    for trace in traces:
        for call in trace.get("tool_calls", []):
            tools[f"{call['name']}:{call['status']}"] += 1
            if call.get("error_code"):
                errors[call["error_code"]] += 1
            durations.append(int(call.get("duration_ms", 0)))
    turns = Counter(t.get("status", "unknown") for t in traces)
    return {"turns": dict(turns), "tool_calls": dict(tools), "tool_errors": dict(errors),
            "tool_duration_ms_p50": sorted(durations)[len(durations) // 2] if durations else 0}  # fmt: skip


def debug_bundle(traces: list[dict[str, Any]], log_dir: Path, conversation: list[dict[str, Any]] | None = None,
                 include_conversation: bool = False) -> dict[str, Any]:  # fmt: skip
    """The default bundle has trace metadata and the (already redacted) logs, never the conversation."""
    logs = {p.name: p.read_text() for p in sorted(log_dir.glob("*.jsonl"))} if log_dir.is_dir() else {}
    bundle: dict[str, Any] = {"traces": traces, "logs": logs}
    if include_conversation and conversation is not None:
        bundle["conversation"] = conversation
    return bundle


def as_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
