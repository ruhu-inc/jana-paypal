"""Capabilities and settings in layers (MDL-03).

Order, later layers filling only what earlier ones leave unset: (1) adult or developer override; (2) the curated
recommendations file (MDL-28; a provisional subset until the model pack ships it); (3) what the provider reports.
For local models the machine's memory then caps `context_for_agents` (MDL-18)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from agent_core.providers.base import ModelCapabilities

RECOMMENDATIONS = Path(__file__).with_name("recommended_models.json")
CONTRACT = "agents/model-recommendations"  # MDL-28


def recommendations(path: Path = RECOMMENDATIONS) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text())
    return {entry["model"]: entry for entry in data["models"]}


def recommendation_errors(document: Any) -> list[str]:
    """MDL-28: the file against its schema, every entry complete. A build whose contracts lack the schema cannot
    validate the file, and says so rather than passing it."""
    from agent_core.contracts import errors as contract_errors

    try:
        return contract_errors(CONTRACT, document)
    except KeyError:
        return [f"the {CONTRACT} contract is not in this build, so the recommendations file cannot be validated"]


def entries_for(models: list[str], document: dict[str, Any]) -> list[str]:
    """MDL-28: every model a pack holds needs its own entry in the recommendations it ships with."""
    listed = {e.get("model") for e in document.get("models", []) if isinstance(e, dict)}
    return [f"{m}: no recommendations entry" for m in models if m not in listed]


def layered(
    reported: ModelCapabilities,
    recommended: dict[str, Any] | None = None,
    override: dict[str, Any] | None = None,
    memory_context_cap: int | None = None,
) -> ModelCapabilities:
    names = {f.name for f in fields(ModelCapabilities)}
    merged: dict[str, Any] = {}
    for layer in (override or {}, (recommended or {}).get("capabilities", {})):
        for name, value in layer.items():
            if name in names and name not in merged and value is not None:
                merged[name] = value
    result = replace(reported, **merged)
    if memory_context_cap is not None and result.locality == "local":
        result = replace(result, context_for_agents=min(result.context_for_agents, memory_context_cap))
    return result


def pack_recommendations(manifest: Mapping[str, Any], bundled: Path = RECOMMENDATIONS) -> dict[str, Any]:
    """The recommendations file in force (MDL-28): the copy inside the verified, signed model manifest, which updates
    only with the pack; without one, the copy bundled with the app (offline installs)."""
    carried = manifest.get("recommendations")
    if isinstance(carried, dict):
        return dict(carried)
    loaded: dict[str, Any] = json.loads(bundled.read_text())
    return loaded


def pack_recommendation_problems(manifest: Mapping[str, Any], bundled: Path = RECOMMENDATIONS) -> list[str]:
    """MDL-28 for one model pack: its recommendations valid against their schema, and an entry for every model it
    holds."""
    document = pack_recommendations(manifest, bundled)
    models = [str(e.get("model")) for e in manifest.get("models", [])]
    return recommendation_errors(document) + entries_for(models, document)
