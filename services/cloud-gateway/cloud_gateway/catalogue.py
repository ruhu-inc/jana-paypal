"""Export a secret-free device configuration projection. No network calls or approval creation."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from cloud_gateway.config import GatewayConfig, ServicePermission, load_deployment


def export(config: GatewayConfig, now: float) -> dict[str, Any]:
    def permit(permission: ServicePermission | None) -> dict[str, Any] | None:
        if permission is None or not permission.permits(now):
            return None
        return {
            "expires_at_ms": int(permission.expires_at.timestamp() * 1000),
            "permission_sha256": hashlib.sha256(permission.reference.encode()).hexdigest(),
        }

    slots: dict[str, Any] = {}
    for slot, name in config.slots.items():
        if slot not in ("primary", "fallback"):
            continue
        provider = config.providers[name]
        permission = permit(provider.permission)
        if permission is None:
            continue
        aliases = {}
        for alias, model in provider.models.items():
            price = config.prices.get(model.id)
            if model.context is None or model.max_output is None or price is None or model.max_output > model.context:
                continue
            aliases[alias] = {
                **permission,
                "context": model.context,
                "max_output": model.max_output,
                "capabilities": sorted(model.capabilities),
                "price_in_per_mtok": price.input,
                "price_out_per_mtok": price.output,
            }
        slots[slot] = aliases
    services: dict[str, Any] = {}
    for aliases in slots.values():
        for model in aliases.values():
            if {"text", "structured_output", "tool_calls", "streaming"} <= set(model["capabilities"]):
                permission = {k: model[k] for k in ("expires_at_ms", "permission_sha256")}
                if permission["expires_at_ms"] > services.get("tutor", {}).get("expires_at_ms", 0):
                    services["tutor"] = permission
    slot, alias = config.study_model.split("/", 1)
    study = slots.get(slot, {}).get(alias)
    if study and {"text", "structured_output"} <= set(study["capabilities"]):
        permission = {k: study[k] for k in ("expires_at_ms", "permission_sha256")}
        services["study"] = permission
    ask_slot, ask_alias = config.ask_model.split("/", 1)
    ask = slots.get(ask_slot, {}).get(ask_alias)
    if ask and {"text", "structured_output"} <= set(ask["capabilities"]):
        services["ask"] = {k: ask[k] for k in ("expires_at_ms", "permission_sha256")}
    research = config.research
    if research.enabled:
        if research.adapter == "json_search" and research.search:
            permission = permit(research.search.permission)
        else:
            research_provider = config.slots.get(slot)
            permission = permit(config.providers[research_provider].permission) if research_provider else None
        if permission:
            services["research"] = permission
    segmentation = config.segmentation if config.segmentation.enabled else config.sam
    if segmentation.enabled and (permission := permit(segmentation.permission)):
        services["segmentation"] = permission
    speech = config.speech
    synthesis = permit(speech.synthesis_permission) if speech.synthesizer != "none" else None
    recognition = permit(speech.recognition_permission) if speech.recognizer != "none" else None
    if config.voice_enabled and recognition and synthesis:
        services["voice"] = min((recognition, synthesis), key=lambda p: p["expires_at_ms"])
    if config.podcast_speech.enabled and synthesis:
        services["narration"] = synthesis
    return {
        "version": 1,
        "host": config.audience_host,
        "voice_host": config.voice_audience_host,
        "services": services,
        "slots": slots,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Exclusive creation prevents silently replacing a deployed projection.
    catalogue = export(load_deployment(args.config), time.time())
    with args.output.open("x") as output:
        json.dump(catalogue, output, indent=2, allow_nan=False)
        output.write("\n")


if __name__ == "__main__":
    main()
