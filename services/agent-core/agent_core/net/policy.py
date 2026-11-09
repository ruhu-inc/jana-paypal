"""The network policy of the connectivity layer (ARC-06, ARC-07, DAT-18, PRV-08, SEC-31).

`policy.json` is TABLE ARC-07a as data. `Policy.resolve` combines it with what this device has decided: the adult
switches, the build's stage, the release's approved DPIA list, the environment's hosts and pins, the connectivity
state and an adult-configured LAN compute server. The result is the one resolved policy both enforcement points
read: `agent_core.net` checks every request against it, and the shell's `net` module receives `document()`.

A request names its feature; it is refused when the feature is off or not shipped, when the host is not the feature's
allow-listed host, when the payload class is not one the feature may carry, or when it is not HTTPS (SEC-31)."""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

POLICY_FILE = Path(__file__).with_name("policy.json")
STAGES = ("S1", "S2", "S3", "S4", "S5")
SHIPPED_STAGE = "S2"  # the stage this build ships (ARC-07: a feature of a later stage is refused)
NetworkState = Literal["off", "offline", "limited", "metered", "online", "unknown"]
LAN_COMPUTE = "lan_compute"


class EgressRefused(Exception):
    """A request the policy does not allow; nothing was sent. `reason` is one of the listed codes."""

    REASONS = ("feature_off", "stage_not_shipped", "unknown_feature", "host_not_allowed", "payload_class_not_allowed",
               "plain_http", "redirect_outside_allow_list", "offline", "notice_pending", "notice_declined", "redacted",
               "pin_mismatch", "no_pin", "credential_on_anonymous_feature", "not_private_address")  # fmt: skip

    def __init__(self, reason: str, feature: str, detail: str = "") -> None:
        super().__init__(f"{feature}: {reason}" + (f" ({detail})" if detail else ""))
        self.reason = reason
        self.feature = feature
        self.detail = detail


@dataclass(frozen=True)
class Feature:
    """One row of TABLE ARC-07a."""

    id: str
    stage: str
    service: str
    payload_classes: tuple[str, ...]
    data_classes: tuple[str, ...]
    queued: str
    consent: str  # "profile", "device" or "none"
    authenticated: bool
    dpia_gate: str
    destination_label: str

    capability_enabled: bool = False

    @property
    def shipped(self) -> bool:
        return self.capability_enabled or STAGES.index(self.stage) <= STAGES.index(SHIPPED_STAGE)


@dataclass(frozen=True)
class LanServer:
    """The adult-configured student compute server (ARC-13): a pinned private address."""

    address: str
    port: int
    pins: tuple[str, ...]


@dataclass(frozen=True)
class Policy:
    features: Mapping[str, Feature]
    services: Mapping[str, Mapping[str, str]]  # environment -> service -> host
    pins: Mapping[str, tuple[str, ...]]  # environment -> pins for our services
    probe_payload_class: str
    lan: Mapping[str, Any]

    def shipped(self) -> list[str]:
        return [f for f, row in self.features.items() if row.shipped]

    def host(self, feature: str, environment: str) -> str:
        return self.services[environment][self.features[feature].service]

    def resolve(
        self,
        environment: str,
        enabled: Iterable[str],
        *,
        release: bool = False,
        network: NetworkState = "unknown",
        visible: Iterable[str] | None = None,
        hosts: Mapping[str, str] | None = None,
        pins: Mapping[str, Iterable[str]] | None = None,
        lan: LanServer | None = None,
    ) -> ResolvedPolicy:
        """`visible` is the features the release's DPIA list approves (None: every shipped feature, a dev build);
        `hosts` and `pins` override the environment's (service -> host, host -> pins) for tests and operations."""
        on = set(enabled)
        shown = set(self.shipped()) if visible is None else set(visible) & set(self.shipped())
        service_hosts = {**self.services[environment], **(hosts or {})}
        resolved: dict[str, ResolvedFeature] = {}
        for feature_id in self.shipped():
            if feature_id not in shown:
                continue  # hidden: its DPIA is not on the release's signed list (PRV-08)
            row = self.features[feature_id]
            resolved[feature_id] = ResolvedFeature(
                enabled=feature_id in on,
                hosts=(service_hosts[row.service],),
                payload_classes=(*row.payload_classes, self.probe_payload_class),
                authenticated=row.authenticated,
                destination_label=row.destination_label,
                data_classes=row.data_classes,
            )
        ours = tuple(self.pins.get(environment, ()))
        pinned = {h: ours for f in resolved.values() for h in f.hosts}
        pinned.update({h: tuple(p) for h, p in (pins or {}).items()})
        return ResolvedPolicy(environment, release, network, resolved, pinned, lan, tuple(self.lan["payload_classes"]))


@dataclass(frozen=True)
class ResolvedFeature:
    enabled: bool
    hosts: tuple[str, ...]
    payload_classes: tuple[str, ...]
    authenticated: bool
    destination_label: str
    data_classes: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedPolicy:
    environment: str
    release: bool
    network: NetworkState
    features: Mapping[str, ResolvedFeature]
    pins: Mapping[str, tuple[str, ...]]
    lan: LanServer | None = None
    lan_payload_classes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def all_off(self) -> bool:
        return not any(f.enabled for f in self.features.values())

    def enabled(self) -> list[str]:
        return [f for f, row in self.features.items() if row.enabled]

    def check(self, feature: str, url: str, payload_class: str, *, credential: bool = False) -> str:
        """The host this request may go to, or `EgressRefused`. Connectivity is checked by the client."""
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise EgressRefused("plain_http", feature, parts.scheme)
        host = (parts.hostname or "").lower()
        if parts.username or parts.password:
            raise EgressRefused("host_not_allowed", feature, "credentials in the address")
        if feature == LAN_COMPUTE:
            return self._check_lan(host, parts.port or 443, payload_class)
        row = self.features.get(feature)
        if row is None:
            known = feature in load_policy().features
            raise EgressRefused("stage_not_shipped" if known else "unknown_feature", feature)
        if not row.enabled:
            raise EgressRefused("feature_off", feature)
        if host not in row.hosts:
            raise EgressRefused("host_not_allowed", feature, host)
        if payload_class not in row.payload_classes:
            raise EgressRefused("payload_class_not_allowed", feature, payload_class)
        if credential and not row.authenticated:
            raise EgressRefused("credential_on_anonymous_feature", feature)
        return host

    def check_redirect(self, feature: str, location: str, payload_class: str) -> str:
        try:
            return self.check(feature, location, payload_class)
        except EgressRefused as refused:
            raise EgressRefused("redirect_outside_allow_list", feature, refused.reason) from None

    def _check_lan(self, host: str, port: int, payload_class: str) -> str:
        lan = self.lan
        if lan is None:
            raise EgressRefused("feature_off", LAN_COMPUTE, "no LAN compute server is configured")
        if host != lan.address or port != lan.port:
            raise EgressRefused("host_not_allowed", LAN_COMPUTE, host)
        if not _private(host):
            raise EgressRefused("not_private_address", LAN_COMPUTE, host)
        if payload_class not in self.lan_payload_classes:
            raise EgressRefused("payload_class_not_allowed", LAN_COMPUTE, payload_class)
        return host

    def pins_for(self, host: str) -> tuple[str, ...]:
        if self.lan is not None and host == self.lan.address:
            return self.lan.pins
        return self.pins.get(host, ())

    def document(self) -> dict[str, Any]:
        """The resolved policy as the shell's `net` module reads it."""
        return {
            "version": 1,
            "environment": self.environment,
            "release": self.release,
            "network": self.network,
            "features": {f: {"enabled": r.enabled, "hosts": list(r.hosts), "payload_classes": list(r.payload_classes),
                             "authenticated": r.authenticated, "destination_label": r.destination_label,
                             "data_classes": list(r.data_classes)} for f, r in self.features.items()},
            "pins": {h: list(p) for h, p in self.pins.items()},
        }  # fmt: skip


def _private(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private and not address.is_loopback and not address.is_link_local


@cache
def load_policy(path: Path = POLICY_FILE) -> Policy:
    data = json.loads(path.read_text())
    features = {
        fid: Feature(fid, row["stage"], row["service"], tuple(row["payload_classes"]), tuple(row["data_classes"]),
                     row["queued"], row["consent"], bool(row["authenticated"]), row["dpia_gate"],
                     row["destination_label"])
        for fid, row in data["features"].items()
    }  # fmt: skip
    pins = {env: tuple(values) for env, values in data["pins"].items() if not env.startswith("$")}
    return Policy(features, data["services"], pins, data["probe_payload_class"], data["lan_compute"])
