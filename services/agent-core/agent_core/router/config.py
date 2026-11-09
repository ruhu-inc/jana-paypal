"""The router configuration (MDL-05, MDL-08, MDL-09), validated against the router-config contract at load.

A configuration in which a tier has no local candidate is rejected (D4): the contract requires it and the loader
reports it by tier. Candidates are `local:<model-id>` or `cloud:<provider>/<model-id>`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agent_core.contracts import errors as contract_errors

CONTRACT = "agents/router-config"
TIERS = ("fast", "strong", "judge")
SLOTS = (
    "rule_matcher",
    "input_guard",
    "title",
    "compaction",
    "judge",
    "reflection",
    "vision",
    "research_chat",
    "checkin",  # SPEC §19.5.5 (W8): the check-in extractor's one structured call
)  # MDL-06


class RouterConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Candidate:
    locality: str  # "local" or "cloud"
    provider: str  # "local" for local candidates, else the cloud provider name
    model: str

    @classmethod
    def parse(cls, text: str) -> Candidate:
        locality, _, rest = text.partition(":")
        if locality == "local":
            return cls("local", "local", rest)
        provider, _, model = rest.partition("/")
        return cls("cloud", provider, model)

    def __str__(self) -> str:
        return f"local:{self.model}" if self.locality == "local" else f"cloud:{self.provider}/{self.model}"


@dataclass(frozen=True)
class RouterConfig:
    data: dict[str, Any]

    @property
    def tiers(self) -> dict[str, list[Candidate]]:
        return {tier: [Candidate.parse(c) for c in self.data["tiers"][tier]] for tier in TIERS}

    def role(self, name: str) -> dict[str, Any]:
        if name in self.data["roles"]:
            return dict(self.data["roles"][name])
        slot = self.data["auxiliary"].get(name)
        if slot is None and name not in SLOTS:
            raise KeyError(f"no router entry for role {name!r}")
        return dict(slot or {})  # an unset slot inherits the main local model (MDL-06)

    @property
    def retry(self) -> dict[str, Any]:
        return dict(self.data["retry"])

    @property
    def fallback(self) -> dict[str, Any]:
        return dict(self.data["fallback"])

    @property
    def cloud(self) -> dict[str, Any]:
        return dict(self.data["cloud"])

    @property
    def local(self) -> dict[str, Any]:
        return dict(self.data["local"])


def load(source: Path | dict[str, Any]) -> RouterConfig:
    data = yaml.safe_load(source.read_text()) if isinstance(source, Path) else source
    problems = contract_errors(CONTRACT, data)
    if problems:
        missing_local = [
            t for t in TIERS if not any(str(c).startswith("local:") for c in data.get("tiers", {}).get(t, []))
        ]
        if missing_local:
            raise RouterConfigError(f"tier {missing_local[0]} has no local candidate (every tier needs one)")
        raise RouterConfigError(f"router configuration is invalid: {problems[0]}")
    return RouterConfig(data)
