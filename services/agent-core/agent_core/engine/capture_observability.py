"""Engine-owned capture observations. The HTTP boundary passes metadata; trace construction stays here (ARC-18)."""

from __future__ import annotations

from typing import Any

from agent_core.trace.perception import capture_trace
from agent_core.trace.record import TraceBuilder, default_build, sha256_of


def new_capture_trace(
    session_id: str, profile_id: str, mode: str, hardware: str, capture_id: str, settings: dict[str, Any]
) -> TraceBuilder:
    return TraceBuilder(
        session_id,
        profile_id,
        kind="job",
        mode=mode,
        trace_id=capture_id.removeprefix("cap_"),
        build=default_build(hardware if hardware in ("low", "mid", "high") else "mid"),
        assets={
            "config_sha256": sha256_of(settings),
            "agent_bundle_id": "none",
            "soul_sha256": sha256_of(""),
            "role_manifests": [],
            "content_packs": [],
        },
    )


def capture_observation(builder: Any, record: dict[str, Any], preprocess_ms: int) -> dict[str, Any]:
    return capture_trace(builder, record, preprocess_ms)
