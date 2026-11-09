"""Explicit operator deployment projection, never a model qualification or a signed approval."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_core.contracts import errors

SERVICES = ("tutor", "study", "ask", "research", "segmentation", "voice", "narration")


def load(path: str | None, host: str, voice_host: str) -> dict[str, Any]:
    if not path:
        return {}
    try:
        with Path(path).open("rb") as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            return {}
        value = json.loads(raw)
        if errors("cloud/catalogue", value) or value["host"] != host:
            return {}
        if value["voice_host"] not in (None, voice_host):
            return {}
        if value["voice_host"] is None and host != voice_host:
            value["services"].pop("voice", None)
        return dict(value)
    except (OSError, ValueError, TypeError):
        return {}


def allowed(catalogue: dict[str, Any], service: str, now_ms: int) -> bool:
    permission = catalogue.get("services", {}).get(service)
    valid = permission is not None and now_ms < permission["expires_at_ms"]
    if service == "tutor":
        return valid and any(
            {"text", "structured_output", "tool_calls", "streaming"} <= set(model["capabilities"])
            for aliases in models(catalogue, now_ms).values()
            for model in aliases.values()
        )
    return valid


def models(catalogue: dict[str, Any], now_ms: int) -> dict[str, dict[str, Any]]:
    return {
        slot: {alias: model for alias, model in aliases.items() if now_ms < model["expires_at_ms"]}
        for slot, aliases in catalogue.get("slots", {}).items()
    }
