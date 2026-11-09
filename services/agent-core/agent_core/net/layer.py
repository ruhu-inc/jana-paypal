"""The connectivity layer as the core runs it (ARC-06 to ARC-11, API-15, DAT-11, SEC-21, D70).

One `OnlineLayer` per device: it resolves the policy from the adult switches (`online_features`), the release's DPIA
list and the connectivity state; owns the egress client, the connectivity state machine, the outbox and the service
credential; and answers the questions the rest of the core asks: is the cloud tier in effect for this profile, what
does `connectivity.status` say, what may the shell's `net` module do. Nothing here starts network work while every
feature is off: no probe, no drain, no lookup (ARC-06 invariant 2)."""

from __future__ import annotations

import asyncio
import sqlite3
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from agent_core.config import Settings
from agent_core.config.environments import Environment
from agent_core.net.client import USABLE, EgressClient, NetError
from agent_core.net.credentials import CredentialRefused, ServiceCredential
from agent_core.net.dpia import visible_features
from agent_core.net.outbox import Outbox
from agent_core.net.policy import EgressRefused, LanServer, Policy, ResolvedPolicy, load_policy
from agent_core.net.state import Connectivity, ProbeOutcome
from agent_core.net.study import StudyCloud
from agent_core.store import consents as consent_store
from agent_core.store import egress as egress_store
from agent_core.store import subscription as subscriptions
from agent_core.store.events import iso_ms
from agent_core.store.keystore import KeyStore

AppEvent = Callable[[dict[str, Any]], None]


@dataclass
class OnlineLayer:
    settings: Settings
    device: sqlite3.Connection
    keys: KeyStore
    environment: Environment = Environment.DEV
    release: bool = False
    policy: Policy = field(default_factory=load_policy)
    hosts: Mapping[str, str] | None = None  # service -> host override (operations, tests)
    pins: Mapping[str, tuple[str, ...]] | None = None  # host -> pins override
    connect: Mapping[str, tuple[str, int]] | None = None  # host -> dialled address (tests only)
    context: ssl.SSLContext | None = None
    lan: LanServer | None = None
    # The licence and signed entitlement the device registers its credential with (SEC-21, D70); by default the
    # applied subscription's, read at each registration.
    licence_id: Callable[[], str | None] | None = None
    entitlement: Callable[[], Mapping[str, Any] | None] | None = None
    clock_ms: Callable[[], int] = field(default=lambda: int(time.time() * 1000))
    probe: Callable[[str], Any] | None = None  # replaces the HTTPS probe (tests)
    publish: list[AppEvent] = field(default_factory=list)  # app-channel events (API-15)
    visible: list[str] | None = None  # set from the DPIA list at start

    def __post_init__(self) -> None:
        # M42 and cloud voice (VOC-28) are explicit capabilities; other S3/S4 features keep their stage boundary.
        early = [name for name, on in (("study_materials", self.settings.study_workspace_enabled),
                                       ("cloud_voice", self.settings.cloud_voice_enabled)) if on]  # fmt: skip
        if early:
            self.policy = replace(
                self.policy,
                features={
                    **self.policy.features,
                    **{name: replace(self.policy.features[name], capability_enabled=True) for name in early},
                },
            )
        if self.visible is None:
            self.visible = visible_features(self.policy, self.release, self.environment)
        self.client = EgressClient(self.resolved, self.device, self.clock_ms, self.settings.net_connect_timeout_s,
                                   self.context, self.connect, on_egress=self._egress,
                                   notice=self._notice)  # fmt: skip
        self.connectivity = Connectivity(self.features_on, self._probe_host, self._probe,
                                         interval_s=self.settings.net_probe_interval_s,
                                         backoff_max_s=self.settings.net_probe_backoff_max_s)  # fmt: skip
        self.connectivity.listeners.append(lambda _: self.announce())
        self.outbox = Outbox(self.device, self.client, self.host_for, self.settings.outbox_backoff_min_s,
                             self.settings.outbox_backoff_max_s, int(self.settings.outbox_cap_mb * 1024 * 1024),
                             self.clock_ms)  # fmt: skip
        self.credential = ServiceCredential(self.keys, self.client, lambda: self.host_for("cloud_tier"),
                                            self.licence_id or self._subscription_licence, self.device,
                                            entitlement=self.entitlement or self._subscription_entitlement)  # fmt: skip
        from agent_core.net.telemetry import TelemetryQueue

        self.telemetry = TelemetryQueue(self)
        self.safety_forced_off: set[str] = set()  # set only by DevicePolicy.apply_online (OPS-10, OPS-11)
        self.active: dict[str, int] = {}
        self._tasks: list[asyncio.Task[Any]] = []

    def _subscription_licence(self) -> str | None:
        found = subscriptions.load(self.device).get("licence_id")
        return str(found) if found else None

    def _subscription_entitlement(self) -> Mapping[str, Any] | None:
        found = subscriptions.load(self.device).get("entitlement")
        return found if isinstance(found, Mapping) else None

    # --- the resolved policy (ARC-06, ARC-07, PRV-08) -------------------------------------------------------------

    def shown(self) -> list[str]:
        """The features this build shows in Settings: shipped and, in a release, on the signed DPIA list."""
        shipped = self.policy.shipped()
        return shipped if self.visible is None else [f for f in shipped if f in self.visible]

    def features_on(self) -> set[str]:
        rows = self.device.execute("SELECT feature FROM online_features WHERE enabled = 1").fetchall()
        return ({str(r[0]) for r in rows} & set(self.shown())) - getattr(self, "safety_forced_off", set())

    def resolved(self) -> ResolvedPolicy:
        return self.policy.resolve(self.environment.value, self.features_on(), release=self.release,
                                   network=self.connectivity.state if hasattr(self, "connectivity") else "unknown",
                                   visible=self.shown(), hosts=self.hosts, pins=self.pins,
                                   lan=self.lan)  # fmt: skip

    def host_for(self, feature: str) -> str:
        service = self.policy.features[feature].service
        return (self.hosts or {}).get(service) or self.policy.services[self.environment.value][service]

    # --- connectivity (ARC-08) ---------------------------------------------------------------------------------

    def _probe_host(self) -> str | None:
        on = sorted(self.features_on())
        return self.host_for(on[0]) if on else None

    async def _probe(self, host: str) -> ProbeOutcome:
        if self.probe is not None:
            outcome: ProbeOutcome = await self.probe(host)
            return outcome
        feature = next((f for f in sorted(self.features_on()) if self.host_for(f) == host), None)
        if feature is None:
            return "failed"
        try:
            reply = await self.client.send(feature, "GET", f"https://{host}/v1/ping", "probe", announce=False)
        except EgressRefused as refused:
            return "tls_failure" if refused.reason in ("pin_mismatch", "no_pin") else "failed"
        except NetError as error:
            return "tls_failure" if error.kind == "tls" else "failed"
        return "ok" if reply.status < 400 else "captive" if reply.status in (301, 302, 307, 511) else "failed"

    def network_failure(self, state: str = "offline") -> None:
        """A request saw the network unusable (MDL-24); the layer re-probes on its own schedule."""
        self.connectivity.report_failure()

    def usable(self) -> bool:
        return self.connectivity.state in USABLE

    # --- switches (ARC-11, ARC-09, PRV-02) --------------------------------------------------------------------

    async def switched(self, changes: Mapping[str, bool]) -> None:
        """After the adult's switch: a feature turned off loses its queued payloads and its requests in flight before
        this returns; the state machine is told (`off` at once when everything is off)."""
        for feature, on in changes.items():
            if not on:
                self.outbox.purge(feature)
                if feature == "telemetry":
                    self.telemetry.clear()
                await self.client.cut(feature)
        self.connectivity.features_changed()
        self.announce()

    # --- what the indicator and Settings show (API-15, UXS-06, UXS-08) -------------------------------------------

    def _egress(self, feature: str, state: str, label: str, data_classes: tuple[str, ...]) -> None:
        step = {"started": 1, "ended": -1}.get(state, 0)
        self.active[feature] = max(0, self.active.get(feature, 0) + step)
        for listener in list(self.publish):
            listener({"type": "egress.notice", "feature": feature, "state": state, "destination_label": label,
                      "data_classes": list(data_classes)})  # fmt: skip

    def feature_state(self, feature: str, enabled: bool, queued: int) -> str:
        if self.active.get(feature, 0) > 0:
            return "active"
        if queued:
            return "queued"
        if enabled and self.connectivity.state in ("offline", "limited"):
            return "unavailable"
        return "idle"

    def status(self) -> dict[str, Any]:
        """`connectivity.status`, listing only the features this build shows."""
        on = self.features_on()
        counts = egress_store.counts(self.device)
        return {"type": "connectivity.status", "network": self.connectivity.state,
                "checked_ts": self.connectivity.checked_ms,
                "features": [{"feature": f, "enabled": f in on,
                              "state": self.feature_state(f, f in on, counts.get(f, 0)),
                              "queued_items": counts.get(f, 0)} for f in self.shown()]}  # fmt: skip

    def state_view(self, extended: bool) -> dict[str, Any]:
        """`ConnectivityState`; `extended` adds the proposed `queued_items` and `last_sent_at`."""
        on = self.features_on()
        counts = egress_store.counts(self.device)
        sent = egress_store.last_sent(self.device) if extended else {}
        features: dict[str, Any] = {}
        for f in self.shown():
            entry: dict[str, Any] = {"enabled": f in on, "state": self.feature_state(f, f in on, counts.get(f, 0))}
            if extended:
                entry["queued_items"] = counts.get(f, 0)
                entry["last_sent_at"] = iso_ms(sent[f]) if f in sent else None
            features[f] = entry
        return {"network": self.connectivity.state, "features": features}

    def announce(self) -> None:
        event = self.status()
        for listener in list(self.publish):
            listener(event)

    # --- crash reports, asked each time (OBS-17, D53, D85): the local API's view of `net.crash_reports` ----------

    def crash_report(self, record: Mapping[str, Any], hardware_class: str | None) -> dict[str, Any]:
        from agent_core.net import crash_reports

        return crash_reports.build_report(record, hardware_class)

    def crash_report_problems(self, report: Mapping[str, Any], identifying_terms: list[str]) -> list[str]:
        from agent_core.net import crash_reports

        return crash_reports.report_problems(report, identifying_terms)

    def send_crash_report(self, report: Mapping[str, Any], identifying_terms: list[str], expires_ms: int) -> str:
        """Queue this exact report ("Send"), only while `crash_reports` is on (`CrashReportRefused` otherwise)."""
        from agent_core.net import crash_reports

        return crash_reports.approve(self, report, identifying_terms, self.settings.crash_reports_queued_max,
                                     expires_ms)  # fmt: skip

    # --- the cloud tier for one profile (ARC-11, D70, PRV-08) ---------------------------------------------------

    def ask_client(self, profile: Callable[[], str | None], connection: Callable[[], Any]) -> Any:
        """Ask anything's transport (STU-20), on the cloud tier's egress scope and gates."""
        from agent_core.net.ask import AskCloud

        return AskCloud(self, profile, connection)

    def study_client(self, profile: Callable[[], str | None], connection: Callable[[], Any]) -> StudyCloud:
        return StudyCloud(self, profile, connection)

    def subscription(self) -> dict[str, Any]:
        return subscriptions.load(self.device)

    async def tutoring_balance(self, profile: str, current: Callable[[], bool]) -> dict[str, Any] | None:
        """Read this credential's allowance without sending chat content or starting a model call."""
        if not current() or not self.usable() or self.notice_answer(profile) != "accepted":
            return None
        try:
            async with asyncio.timeout(5):
                token = await self.credential.token()
                if not current():
                    return None
                reply = await self.client.send(
                    "cloud_tier", "GET", f"https://{self.host_for('cloud_tier')}/v1/tutoring/usage",
                    "service_auth", profile_id=profile, headers={"authorization": "Bearer " + token}, announce=False,
                )
        except (CredentialRefused, EgressRefused, NetError, TimeoutError):
            return None
        return reply.body if current() and reply.status == 200 and isinstance(reply.body, dict) else None

    def deployment(self) -> dict[str, Any]:
        from agent_core.net.catalogue import load

        return load(self.settings.cloud_catalogue_file, self.host_for("cloud_tier"), self.host_for("cloud_voice"))

    def service_allowed(self, learner: sqlite3.Connection | None, service: str = "tutor") -> bool:
        from agent_core.net.catalogue import allowed

        if learner is None:
            return False
        try:
            band = learner.execute("SELECT age_band FROM profile LIMIT 1").fetchone()
        except sqlite3.Error:
            return False
        return bool(
            band and band[0] in ("13_15", "16_17", "18_plus") and allowed(self.deployment(), service, self.clock_ms())
        )

    def service_availability(self, learner: sqlite3.Connection | None) -> dict[str, bool]:
        """The API-facing projection; network deployment details stay inside this layer (ARC-18)."""
        from agent_core.net.catalogue import SERVICES

        return {service: self.service_allowed(learner, service) for service in SERVICES}

    def consent_active(self, learner: sqlite3.Connection, scope: str) -> bool:
        consent = consent_store.active(learner, scope)
        if consent is None or consent[5] is not None:
            return False
        band = learner.execute("SELECT age_band FROM profile LIMIT 1").fetchone()
        if not band or (band[0] != "18_plus" and (consent[1], consent[2]) != ("guardian", "checkout_guardian")):
            return False
        expiry = learner.execute("SELECT expires_at FROM consents WHERE consent_id=?", (consent[0],)).fetchone()
        return not (expiry and expiry[0] is not None and expiry[0] <= self.clock_ms())

    def cloud_effective(
        self, profile_id: str | None, learner: sqlite3.Connection | None, service: str = "tutor"
    ) -> bool:
        """The switch is on (subscribing turns it on, D70), the subscription is active and names the profile, and the
        profile holds an active `cloud_tier` consent (a guardian's, or their own at 18+)."""
        if profile_id is None or learner is None or not self.service_allowed(learner, service):
            return False
        if "cloud_tier" not in self.features_on():
            return False
        if not subscriptions.covers(self.subscription(), profile_id, self.clock_ms()):
            return False
        return self.consent_active(learner, "cloud_tier")

    def study_effective(self, profile_id: str | None, learner: sqlite3.Connection | None) -> bool:
        """Online Study materials (STU-01, PRV-05): its switch is on, the cloud tier is effective for the profile and
        the profile holds an active, unpaused `study_materials` consent. Only then may the learner's excerpts, search
        hits or transcript lines reach the cloud."""
        if "study_materials" not in self.features_on() or not self.cloud_effective(profile_id, learner, "study"):
            return False
        assert learner is not None
        return self.consent_active(learner, "study_materials")

    def memory_online_effective(self, profile_id: str | None, learner: sqlite3.Connection | None) -> bool:
        """Personal tutor memory online (SPEC §19.6, JM-D21): its switch is on, the cloud tier is effective for the
        profile (the consent requires it) and the profile holds an active, unpaused `tutor_memory_online` consent.
        Only then may screened notes and the Reflector's evidence lines reach the cloud tutor."""
        if "tutor_memory_online" not in self.features_on() or not self.cloud_effective(profile_id, learner):
            return False
        assert learner is not None
        return self.consent_active(learner, "tutor_memory_online")

    def voice_effective(self, profile_id: str | None, learner: sqlite3.Connection | None) -> bool:
        """Cloud voice is switched on, the subscription names the profile and its `cloud_voice` consent is active
        (VOC-28, PRV-05, D70); the first-use notice is asked separately."""
        if profile_id is None or learner is None or not self.service_allowed(learner, "voice"):
            return False
        if "cloud_voice" not in self.features_on():
            return False
        if not subscriptions.covers(self.subscription(), profile_id, self.clock_ms()):
            return False
        return self.consent_active(learner, "cloud_voice")

    def notice_answer(self, profile_id: str | None, feature: str = "cloud_tier") -> str | None:
        return egress_store.first_use(self.device, profile_id).get(feature)

    def _notice(self, feature: str, profile_id: str | None) -> str | None:
        """The one-time notice gate at the layer (ARC-10): a profile's data for a profile feature goes only after
        that profile accepted the feature's notice. Device features were named to the adult at the switch (UXS-07)."""
        row = self.policy.features.get(feature)
        if row is None or row.consent != "profile" or profile_id is None:
            return "accepted"
        return self.notice_answer(profile_id, feature)

    def record_notice(self, profile_id: str | None, feature: str, accepted: bool) -> None:
        egress_store.record_first_use(self.device, profile_id, feature, accepted, self.clock_ms())
        self.announce()  # the answer changes what is offered (cloud voice is offered only after it, VOC-28)

    # --- background work ----------------------------------------------------------------------------------

    async def drain_once(self, force: bool = False) -> int:
        if not self.usable():
            return 0
        self.telemetry.prepare()
        delivered = await self.outbox.drain(force=force)
        if delivered:
            self.telemetry.acknowledged()
        if delivered:
            self.announce()
        return delivered

    def start(self) -> None:
        """The probe loop, and a drain whenever the state turns usable. Both idle while every feature is off."""
        loop = asyncio.get_running_loop()
        self._tasks.append(loop.create_task(self.connectivity.run()))

        async def telemetry_flush() -> None:
            while True:
                await asyncio.sleep(self.settings.telemetry_flush_interval_s)
                if "telemetry" in self.features_on():
                    await self.drain_once()

        self._tasks.append(loop.create_task(telemetry_flush()))

        def on_state(state: str) -> None:
            if state in USABLE:
                self._tasks.append(loop.create_task(self.drain_once()))

        self.connectivity.listeners.append(on_state)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.client.close()
