"""INP-33: an allowlisted capture observation, never a copy of an image/transcription record."""

from __future__ import annotations

import math
import re
from typing import Any

from agent_core.trace.record import TraceBuilder

IDENTIFIER = re.compile(r"[A-Za-z0-9_./:@+\-]{1,128}\Z")


def capture_trace(trace: TraceBuilder, capture: dict[str, Any], preprocess_ms: int) -> dict[str, Any]:
    reader = capture["reader"]

    def identity(value: Any, limit: int = 128) -> str | None:
        return value if isinstance(value, str) and len(value) <= limit and IDENTIFIER.fullmatch(value) else None

    outcome = capture["status"]
    trace.status = (
        "complete"
        if outcome == "confirmed"
        else "error"
        if outcome in ("refused", "unreadable")
        else ("interrupted" if outcome == "cancelled" else "running")
    )
    record = trace.record()
    record.update(
        job_name="capture",
        perception={
            "source": capture["source"],
            "outcome": outcome,
            "reader": {
                "path": reader["path"],
                "model_id": identity(reader["model_id"]),
                "model_version": identity(reader.get("model_version"), 64),
                "hardware_class": reader["hardware_class"],
                "duration_ms": reader["duration_ms"],
            },
            "quality_checks": [
                {k: check[k] for k in ("check", "value", "outcome")} for check in capture["quality"]["checks"]
            ],
            "region_confidences": [region["confidence"] for region in capture["regions"]],
            "durations_ms": {"preprocess": preprocess_ms, "read": reader["duration_ms"], "total": trace.offset_ms()},
            "confirm_method": capture["confirmation"]["method"],
            "confirm_edit_distance": capture["confirmation"]["edit_distance"],
        },
    )

    def finite(value: Any) -> bool:
        if isinstance(value, dict):
            return all(finite(item) for item in value.values())
        if isinstance(value, list):
            return all(finite(item) for item in value)
        return not isinstance(value, float) or math.isfinite(value)

    if not finite(record):
        raise ValueError("non-finite capture metric")
    return record
