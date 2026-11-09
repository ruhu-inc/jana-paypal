"""The egress ledger, "Send now" and the cloud tier subscription (ARC-10, UXS-08, BIZ-07, D70).

These routes are in the command contract (ARC-10, UXS-06, UXS-08, BIZ-07): `GET /v1/connectivity/ledger`
(Settings → "What leaves this device"), `POST /v1/connectivity/send-now` (the indicator popover) and
`GET|POST|DELETE /v1/subscription`. The connectivity layer itself is reached through `core.online`, which the app
attaches; the API never imports a network client (ARC-18)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CURRENT_PROFILE, CoreService, require
from agent_core.api.errors import ApiError
from agent_core.api.profiles import ATTESTED_BY, Mutating
from agent_core.config.environments import Environment
from agent_core.store import consents as consent_store
from agent_core.store import egress as egress_store
from agent_core.store import subscription as subscriptions
from agent_core.store.events import iso_ms
from agent_core.store.profiles import ProfileDirectory
from agent_core.store.sqlite import transaction

DAY_MS = 86_400_000


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    def directory() -> ProfileDirectory:
        if not isinstance(core.directory, ProfileDirectory):
            raise ApiError("not_found", "profiles are not available")
        return core.directory

    @app.get("/v1/connectivity/ledger")
    async def ledger(request: Request) -> dict[str, Any]:
        """Every egress request of the retention window, newest first, with no content (ARC-10): the device's rows,
        plus the open profile's own rows when a profile token is given; never another profile's (DAT-14)."""
        from agent_core.store.profiles import internal_id

        d = directory()
        days = core.settings.retention_egress_days
        since = core.clock_ms() - days * DAY_MS
        profile = CURRENT_PROFILE.get()
        mine: tuple[str, ...] = (profile, internal_id(profile)) if profile is not None else ()
        rows = egress_store.ledger_rows(d.device, since, mine)
        return {"retention_days": days, "entries": [{**r, "at": iso_ms(r["at"])} for r in rows]}

    @app.post("/v1/connectivity/send-now")
    async def send_now(request: Request) -> Response:
        async def run(_: Any) -> tuple[int, Any]:
            online = getattr(core, "online", None)
            if online is None or not online.usable():
                raise ApiError("network_unavailable", "not connected; queued work waits for a connection")
            await online.drain_once(force=True)
            return 200, online.state_view(extended=True)

        return await mutating(request, run)

    # --- the subscription (D70) ---------------------------------------------------------------------------------

    def minors() -> set[str]:
        d = directory()
        return {p["profile_id"] for p in d.summaries()["profiles"] if not d.self_consents(p["profile_id"])}

    def current_view() -> dict[str, Any]:
        """`CloudSubscription`, with the open profile's allowance for this month used up or not (BIZ-07, D33)."""
        d = directory()
        stored = subscriptions.load(d.device)
        now = core.clock_ms()
        profile = CURRENT_PROFILE.get()
        used_up = False
        if profile is not None and profile in stored.get("profiles", []) and stored.get("allowance_tokens") is not None:
            metered = egress_store.DeviceSpend(d.device, profile, subscriptions.allowance_period(stored, now),
                                               core.clock_ms)  # fmt: skip
            used_up = metered.period_tokens() >= int(stored["allowance_tokens"])
        return subscriptions.view(stored, now, allowance_used_up=used_up)

    def switch_cloud(
        on: bool, profiles: list[str], role_for: dict[str, str], notices: dict[str, str] | None = None
    ) -> None:
        """The cloud tier switch and each profile's consent, in one action (ARC-11, D70)."""
        d = directory()
        now = core.clock_ms()
        with transaction(d.device):
            d.device.execute(
                "INSERT INTO online_features (feature, enabled, enabled_by_role, enabled_at, disabled_at)"
                " VALUES ('cloud_tier', ?, 'subscription', ?, ?) ON CONFLICT (feature) DO UPDATE SET"
                " enabled = excluded.enabled,"
                " enabled_at = coalesce(excluded.enabled_at, enabled_at),"
                " disabled_at = coalesce(excluded.disabled_at, disabled_at)",
                (int(on), now if on else None, None if on else now),
            )
            d.audit(f"online_feature.{'enable' if on else 'disable'}", None, {"feature": "cloud_tier"})
        versions = core.notices.get("cloud_tier", [])
        for profile_id in profiles:
            role = role_for.get(profile_id, "guardian")
            with d.open_learner(profile_id) as conn, transaction(conn):
                if on:
                    consent_store.grant(conn, "cloud_tier", role, ATTESTED_BY if role == "guardian" else "Student",
                                        (notices or {}).get(profile_id, versions[-1] if versions else "unversioned"),
                                        core.settings.consent_dpia_ref,
                                        now)  # fmt: skip
                else:
                    consent_store.withdraw(conn, "cloud_tier", role, now)

    @app.get("/v1/subscription")
    async def subscription(request: Request) -> dict[str, Any]:
        return current_view()

    def _self_paying(request: Request) -> bool:
        profile = CURRENT_PROFILE.get()
        return profile is not None and directory().self_consents(profile)

    @app.post("/v1/subscription")
    async def subscribe(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = require("SubscriptionApply", body)
            profile = CURRENT_PROFILE.get()
            if not core.settings.consent_dpia_ref:
                raise ApiError("feature_disabled", "the cloud tier waits for its DPIA (D13, PRV-08)")
            licence = getattr(core, "licence", None)
            if licence is not None and licence.state.install_kind == "school_lab":
                raise ApiError("feature_disabled", "no guardian can subscribe on a school lab install (BIZ-06, JRN-11)")
            known = {p["profile_id"] for p in directory().summaries()["profiles"]}
            named = [c["profile_id"] for c in asked["consents"]]
            if any(p not in known for p in named):
                raise ApiError("not_found", "no such profile")
            for c in asked["consents"]:
                if c["notice_version"] not in core.notices.get("cloud_tier", []):
                    raise ApiError("validation_failed", "the consent names a notice this version does not ship")
            young = minors()
            d = directory()
            now = core.clock_ms()
            study_consents: list[dict[str, Any]] = []
            notices = {c["profile_id"]: c["notice_version"] for c in asked["consents"]}
            if asked["entitlement"] == {}:  # adults may grant only their own pending agreement
                if any(p in young for p in named):
                    raise ApiError("guardian_consent_required", "school-age consent needs signed checkout evidence")
                stored = subscriptions.load(d.device)
                if subscriptions.state(stored, now) != "pending_consent":
                    raise ApiError("validation_failed", "no subscription is waiting for a guardian's consent")
                stored = subscriptions.consent_given(stored, named)
            else:
                environment = Environment.PRODUCTION if getattr(core, "release", False) else Environment.DEV
                try:
                    entitlement = subscriptions.verify(asked["entitlement"], environment,
                                                       getattr(core, "entitlement_trust", None))  # fmt: skip
                    covered = list(entitlement.profile_ids) or named
                    if any(p not in known for p in covered):
                        raise ApiError("not_found", "no such profile")
                    # Read scope evidence only AFTER verifying the whole signed checkout document.
                    study_consents = asked["entitlement"].get("study_material_consents", [])
                    if study_consents and not core.settings.study_workspace_enabled:
                        raise ApiError("feature_disabled", "Study materials are not enabled in this build")
                    seen_study: set[str] = set()
                    for evidence in study_consents:
                        target = evidence["profile_id"]
                        if target in seen_study or target not in covered:
                            raise ApiError("validation_failed", "Study consent must name a covered profile once")
                        seen_study.add(target)
                        if evidence["notice_version"] not in core.notices.get("study_materials", []):
                            raise ApiError("validation_failed", "Study consent names an unavailable notice")
                        expected = "guardian" if target in young else "student_18_plus"
                        if evidence["actor_role"] != expected or (
                            target not in young and (not _self_paying(request) or target != profile)
                        ):
                            raise ApiError("guardian_consent_required", "Study consent has the wrong actor")
                    # A device request cannot attest for a guardian. These scope-specific records are trusted
                    # only because the issuer signed the complete checkout document above.
                    guardian_agreed: set[str] = set()
                    for evidence in asked["entitlement"].get("cloud_tier_consents", []):
                        target = evidence["profile_id"]
                        if (
                            target in guardian_agreed
                            or target not in covered
                            or target not in young
                            or evidence["actor_role"] != "guardian"
                            or evidence["notice_version"] not in core.notices.get("cloud_tier", [])
                        ):
                            raise ApiError("validation_failed", "invalid signed cloud consent evidence")
                        guardian_agreed.add(target)
                        notices[target] = evidence["notice_version"]
                    if any(p in young and p not in guardian_agreed for p in named):
                        raise ApiError("guardian_consent_required", "school-age consent needs signed checkout evidence")
                    # The checkout records guardian agreement for each covered minor (D70, D87).
                    stored = subscriptions.record(
                        entitlement,
                        covered,
                        young,
                        guardian_agreed,
                        "student_18_plus" if _self_paying(request) else "guardian",
                        asked["entitlement"],
                    )
                except subscriptions.EntitlementRefused as refused:
                    raise ApiError("validation_failed", f"the entitlement was refused: {refused}") from None
            subscriptions.save(d.device, stored, now)
            if subscriptions.state(stored, now) == "active":
                # Consent per profile: the guardian's for a minor; an 18+ student's own only when they are the payer
                # (otherwise they grant it themselves, D43, PRV-06).
                covered = [
                    p for p in stored.get("profiles", []) if p in young or (_self_paying(request) and p == profile)
                ]
                switch_cloud(
                    True, covered, {p: ("guardian" if p in young else "student_18_plus") for p in covered}, notices
                )
                for evidence in study_consents:
                    with d.open_learner(evidence["profile_id"]) as conn, transaction(conn):
                        role = evidence["actor_role"]
                        consent_store.grant(
                            conn,
                            "study_materials",
                            role,
                            ATTESTED_BY if role == "guardian" else "Student",
                            evidence["notice_version"],
                            core.settings.consent_dpia_ref,
                            now,
                            include_study=True,
                        )
                    online = getattr(core, "online", None)
                    if online is not None:
                        online.record_notice(evidence["profile_id"], "study_materials", True)
                await _switched(True)
            return 200, current_view()

        return await mutating(request, run)

    @app.delete("/v1/subscription")
    async def unsubscribe(request: Request) -> Response:
        async def run(_: Any) -> tuple[int, Any]:
            d = directory()
            stored = subscriptions.load(d.device)
            if stored:
                stored = {**stored, "ended_at": core.clock_ms()}
                subscriptions.save(d.device, stored, core.clock_ms())
                young = minors()
                roles = {p: ("guardian" if p in young else "student_18_plus") for p in stored.get("profiles", [])}
                switch_cloud(False, list(stored.get("profiles", [])), roles)
                await _switched(False)
            return 200, current_view()

        return await mutating(request, run)

    async def _switched(on: bool) -> None:
        if not on:
            core.study.cancel_cloud()
        online = getattr(core, "online", None)
        if online is not None:
            await online.switched({"cloud_tier": on})
