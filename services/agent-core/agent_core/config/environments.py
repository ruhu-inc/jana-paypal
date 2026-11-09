"""Environments (TABLE OPS-35a): dev (local work and CI), staging, pilot and production, isolated by credentials,
DNS names and signing roots. No learner data from pilot or production reaches staging or dev.

Records carry their provenance: a synthetic record says `"synthetic": true`, any other record names the
environment it originated in as `origin_env` (working assumption; the specification names no provenance field).
A record without provenance counts as learner data, so scans fail closed."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class Environment(StrEnum):
    DEV = "dev"
    STAGING = "staging"
    PILOT = "pilot"
    PRODUCTION = "production"


@dataclass(frozen=True)
class EnvironmentPolicy:
    environment: Environment
    signing_roots: frozenset[str]  # artefacts must be signed under one of these roots
    learner_data: bool  # whether learner data may exist here at all
    accepts_origins: frozenset[Environment]  # environments whose non-synthetic records may be stored here


_POLICIES = {
    Environment.DEV: EnvironmentPolicy(Environment.DEV, frozenset({"test"}), False, frozenset({Environment.DEV})),
    Environment.STAGING: EnvironmentPolicy(
        Environment.STAGING, frozenset({"staging", "release"}), False, frozenset({Environment.DEV, Environment.STAGING})
    ),
    Environment.PILOT: EnvironmentPolicy(
        Environment.PILOT, frozenset({"release"}), True, frozenset({Environment.PILOT})
    ),
    Environment.PRODUCTION: EnvironmentPolicy(
        Environment.PRODUCTION, frozenset({"release"}), True, frozenset({Environment.PRODUCTION})
    ),
}
# Release builds (pilot and production) embed only the release root.
RELEASE_ENVIRONMENTS = frozenset({Environment.PILOT, Environment.PRODUCTION})


def environment_policy(environment: Environment) -> EnvironmentPolicy:
    return _POLICIES[environment]


def signing_root_allowed(environment: Environment, root: str) -> bool:
    return root in _POLICIES[environment].signing_roots


def _records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text()
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    return data if isinstance(data, list) else [data]


def store_violations(store: Path, environment: Environment) -> list[str]:
    """Records in a JSON or JSONL store that may not exist in `environment`."""
    policy = _POLICIES[environment]
    violations = []
    for path in sorted(store.rglob("*")):
        if path.suffix not in (".json", ".jsonl") or not path.is_file():
            continue
        for i, record in enumerate(_records(path)):
            if record.get("synthetic") is True:
                continue
            origin = record.get("origin_env")
            if origin not in {e.value for e in policy.accepts_origins}:
                where = f"{path.relative_to(store)}#{i}"
                violations.append(f"{where}: record from {origin or 'an unknown origin'} in {environment.value}")
    return violations
