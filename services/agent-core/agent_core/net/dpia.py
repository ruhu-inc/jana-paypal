"""The DPIA gate per online feature (PRV-08).

A release build carries a signed list of approved DPIA identifiers, each naming the gate of TABLE PRV-08a it covers
(signed with the content key, artefact type `dpia_list`, SEC-12). An entry naming `features` approves only those; one
naming none approves its whole gate. A feature with no approved DPIA on the
list is hidden: it is left out of the resolved policy, so Settings does not show it and nothing can send for it. A
missing or unverifiable list hides every feature. Development builds without a list show every shipped feature."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_core.config.environments import Environment
from agent_core.config.trust import SignatureRefused, TrustStore, bundled_trust
from agent_core.net.policy import Policy

LIST_FILE = Path(__file__).with_name("dpia_list.json")  # bundled by the release pipeline; absent in development


@dataclass(frozen=True)
class DpiaList:
    approved: Mapping[str, str]  # gate -> DPIA identifier, for an entry that names no features (the whole gate)
    problem: str | None = None  # why the list was not accepted, if it was not
    # feature -> DPIA identifier: an entry naming its features approves only those (PRV-08: each feature's own DPIA)
    features: Mapping[str, str] = field(default_factory=dict)

    def visible(self, policy: Policy) -> list[str]:
        return [f for f in policy.shipped() if self.ref(policy, f) is not None]

    def ref(self, policy: Policy, feature: str) -> str | None:
        """The DPIA identifier a consent for this feature stores (`consents.dpia_ref`)."""
        row = policy.features.get(feature)
        if row is None:
            return None
        return self.features.get(feature) or self.approved.get(row.dpia_gate)


def verify(document: Mapping[str, Any], environment: Environment, trust: TrustStore | None = None) -> DpiaList:
    """The approved gates of a signed list, or an empty list saying why it was refused."""
    try:
        (trust or bundled_trust()).verify("dpia_list", document, environment, str(document.get("created_at") or ""))
    except SignatureRefused as refused:
        return DpiaList({}, str(refused))
    approved: dict[str, str] = {}
    features: dict[str, str] = {}
    for entry in document.get("approved") or []:
        if not (isinstance(entry, Mapping) and entry.get("dpia_id") and entry.get("gate")):
            continue
        named = entry.get("features")
        if isinstance(named, list) and named:
            for feature in named:
                features.setdefault(str(feature), str(entry["dpia_id"]))
        elif named is None:
            approved.setdefault(str(entry["gate"]), str(entry["dpia_id"]))
    return DpiaList(approved, features=features)


def release_list(environment: Environment, path: Path = LIST_FILE, trust: TrustStore | None = None) -> DpiaList:
    if not path.is_file():
        return DpiaList({}, "this build carries no DPIA list")
    try:
        document = json.loads(path.read_text())
    except ValueError:
        return DpiaList({}, "the DPIA list is not JSON")
    return verify(document, environment, trust)


def visible_features(policy: Policy, release: bool, environment: Environment, path: Path = LIST_FILE,
                     trust: TrustStore | None = None) -> list[str] | None:  # fmt: skip
    """None (every shipped feature) for a development build without a list; otherwise the approved features."""
    if not release and not path.is_file():
        return None
    return release_list(environment, path, trust).visible(policy)
