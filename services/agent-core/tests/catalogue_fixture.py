"""Synthetic device deployment for tests only; no actual service approval or vendor choice."""

from typing import Any


def synthetic_catalogue() -> dict[str, Any]:
    from agent_core.net.catalogue import SERVICES

    permission = {"expires_at_ms": 4070908800000, "permission_sha256": "a" * 64}
    model = {
        **permission,
        "context": 131072,
        "max_output": 16384,
        "capabilities": ["text", "structured_output", "tool_calls", "streaming", "vision"],
        "price_in_per_mtok": 1.25,
        "price_out_per_mtok": 4.25,
    }
    return {
        "version": 1,
        "host": "gateway.jana.invalid",
        "voice_host": "voice.jana.invalid",
        "services": {service: dict(permission) for service in SERVICES},
        "slots": {
            slot: {alias: dict(model) for alias in ("fast-model", "strong-model")} for slot in ("primary", "fallback")
        },
    }


