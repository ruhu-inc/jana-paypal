"""Local profile tokens isolate learner data; family settings use the launch token (D87, API-04)."""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Awaitable, Callable, MutableMapping
from functools import partial
from typing import Any

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CURRENT_PROFILE, CoreService, require
from agent_core.api.errors import ApiError, envelope
from agent_core.api.security import websocket_credential
from agent_core.contracts.api.endpoints import ENDPOINTS_BY_KEY
from agent_core.store.events import EventLog
from agent_core.store.profiles import ProfileDirectory, ProfileError, internal_id
from agent_core.store.settings import ProfileSettings, SettingsError
from agent_core.store.sqlite import NEWER_MESSAGE_KEY

Mutating = Callable[[Request, Callable[[Any], Awaitable[tuple[int, Any]]]], Awaitable[Response]]
PROFILE_ROUTES = tuple(
    (e["method"], re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", e["path"]) + "$"))
    for e in ENDPOINTS_BY_KEY.values()
    if "profile" in e["auth"]
)
ASGIApp = Callable[[MutableMapping[str, Any], Any, Any], Awaitable[None]]


def _header(scope: MutableMapping[str, Any], name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return str(value.decode("latin-1"))
    return None


class ProfileScope:
    """Sets the request's profile (API-04, DAT-14) before the app runs, in the same task, so streams keep it."""

    def __init__(self, app: ASGIApp, core: CoreService) -> None:
        self.app = app
        self.core = core

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        directory = self.core.directory
        if scope["type"] not in ("http", "websocket") or directory is None or not directory.exists():
            await self.app(scope, receive, send)
            return
        credential = (
            websocket_credential(scope, "jana.profile.")
            if scope["type"] == "websocket"
            else _header(scope, b"x-profile-token")
        )
        profile = directory.profile_of(credential)
        lapsed = directory.tokens.lapsed_profiles()
        for idle in lapsed:
            # An idle lock unloads the profile at once, like a lock, but lets its study jobs finish (SEC-19, DAT-14).
            self.core.forget_profile(idle, idle=True)
        if lapsed:
            await self.core.clear_caches()
        scoped = (
            bool(
                re.fullmatch(
                    r"/v1/(?:search/items|practice/sets/[^/]+(?:/reveal)?|lessons/[^/]+/probe|mocks|mocks/[^/]+/(?:integrity|navigate)|probes(?:/.*)?"
                    r"|plans(?:/.*)?|progress|interview(?:/.*)?|session-notes(?:/.*)?"
                    r"|sessions/[^/]+/(?:end|tutoring-usage|learning-state)|ask(?:/route)?"
                    r"|digests/latest|alerts(?:/[^/]+/dismiss)?|history(?:/[^/]+)?)",
                    str(scope.get("path", "")),
                )
            )
            or (
                re.fullmatch(
                    r"/v1/sessions/[^/]+/voice"
                    if scope["type"] == "websocket"
                    else r"/v1/sessions/[^/]+/voice/(?:utterances|dictations)(?:/[^/]+(?:/playback|/confirm)?)?",
                    str(scope.get("path", "")),
                )
                is not None
            )
            or any(
                method == scope.get("method") and route.fullmatch(str(scope.get("path", "")))
                for method, route in PROFILE_ROUTES
            )
        )
        if profile is None and scoped:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4401})
            else:
                await _send_error(send, "profile_token_expired", "unlock a profile first")
            return
        token = CURRENT_PROFILE.set(profile)
        try:
            await self.app(scope, receive, send)
        finally:
            CURRENT_PROFILE.reset(token)


async def _send_error(send: Any, code: str, message: str) -> None:
    import json

    body = json.dumps(envelope(code, message)).encode()
    await send(
        {
            "type": "http.response.start",
            "status": ApiError(code).status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})  # fmt: skip


def _loaded_profiles(core: CoreService) -> set[str]:
    """Every profile with anything in the core's memory: its learner database, its sessions or a live token. Taken
    from ownership, not the token book, so a token that idled out still leaves its profile to be unloaded (SEC-19)."""
    loaded = {e.profile_id for e in core.sessions.values()} | {core.records_profile}
    if core.directory is not None:
        loaded.add(core.directory.tokens.active_profile())
    return {p for p in loaded if p is not None}


def _api(error: ProfileError | SettingsError) -> ApiError:
    return ApiError(error.code, error.message, retry_after_ms=getattr(error, "retry_after_ms", None))


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    def directory() -> ProfileDirectory:
        if not isinstance(core.directory, ProfileDirectory):
            raise ApiError("not_found", "profiles are not available")
        return core.directory

    def owner(profile_id: str) -> None:
        """The request's profile token must name this profile; any other id answers 404 (API-04)."""
        if CURRENT_PROFILE.get() != profile_id:
            raise ApiError("not_found", "no such profile")

    def guarded(work: Callable[[], Any]) -> Any:
        try:
            return work()
        except (ProfileError, SettingsError) as error:
            raise _api(error) from None

    def erase_directory(profile_id: str) -> Any:
        """The `profile_directory` eraser (DAT-09): the profile's folder, its key and its device rows, last."""
        return guarded(lambda: directory().delete(profile_id)) if isinstance(core.directory, ProfileDirectory) else None

    core.erasers.register("profile_directory", erase_directory)

    def announce(profile_id: str, effective: dict[str, Any] | None = None) -> None:
        """Tell the core's preference hooks the open profile's effective preferences (VOC-23, D96)."""
        if not core.on_preferences:
            return
        if effective is None:
            try:
                opened, settings = settings_of(profile_id)
                with opened:
                    effective = settings.effective()
            except (ProfileError, SettingsError, sqlite3.Error, OSError):
                return  # the hooks are best effort: the profile opens all the same
        for hook in core.on_preferences:
            hook(profile_id, effective)

    def settings_of(profile_id: str) -> tuple[Any, ProfileSettings]:
        d = directory()
        opened = d.open_learner(profile_id)
        conn = opened.conn
        events = EventLog.open(conn, internal_id(profile_id), d.device_id, lambda: int(time.time() * 1000),
                               core.settings.sync_clock_skew_max_ms)  # fmt: skip
        return opened, ProfileSettings(conn, events, core.settings, core.clock_ms, locks=lambda: core.locks(profile_id))

    @app.get("/v1/profiles")
    async def list_profiles() -> dict[str, Any]:
        return guarded(lambda: directory().summaries())  # type: ignore[no-any-return]

    @app.post("/v1/profiles")
    async def create_profile(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            # PRD-22: a pilot build (the pilot environment, or a device whose school licence puts it on the pilot
            # channel) creates no profile until its signed, verified country record is complete.
            licence = getattr(core, "licence", None)
            pilot_channel = licence is not None and licence.state.view(False).get("channel") == "pilot"
            if core.settings.environment == "pilot" or pilot_channel:
                from agent_core.config.environments import Environment
                from agent_core.config.pilot import require_country

                try:
                    trust = core.pack_desk._trust() if core.pack_desk is not None else None
                    require_country(core.settings.pilot_country_file, Environment.PILOT, trust)
                except (ValueError, OSError) as error:
                    raise ApiError("validation_failed", str(error)) from None
            created = guarded(lambda: directory().create(require("CreateProfile", body)))
            for hook in core.on_profile_created:  # a minor created while an online feature is on (ARC-11)
                hook(created["profile_id"])
            return 200, created

        return await mutating(request, run)

    @app.post("/v1/profiles/{profile_id}/open")
    async def unlock(profile_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            if body not in ({}, None):
                raise ApiError("validation_failed", "opening a profile takes no credentials")
            if guarded(lambda: directory().newer_app(profile_id)):
                # Refused before a token is issued, with nothing opened or changed (DAT-12, G51).
                raise ApiError("upgrade_required", "this profile was saved by a newer version of the app",
                               details={"message_key": NEWER_MESSAGE_KEY})  # fmt: skip
            previous = _loaded_profiles(core) - {profile_id}
            token = guarded(lambda: directory().unlock(profile_id))
            for other in sorted(previous):
                core.forget_profile(other)  # switching ends the previous profile and clears its state (SEC-19)
            if previous:
                await core.clear_caches()
            core.load_profile(profile_id)
            announce(profile_id)
            return 200, token

        return await mutating(request, run)

    @app.post("/v1/profiles/{profile_id}/close")
    async def lock(profile_id: str, request: Request) -> Response:
        async def run(_: Any) -> tuple[int, Any]:
            owner(profile_id)
            directory().lock(profile_id)
            with directory().open_learner(profile_id) as conn:
                conn.execute("DELETE FROM archive_authorizations")  # a lock ends every authorization (API-30)
            core.forget_profile(profile_id)
            await core.clear_caches()
            return 204, None

        return await mutating(request, run)

    @app.get("/v1/profiles/{profile_id}")
    async def read_profile(profile_id: str) -> dict[str, Any]:
        """The open profile's own record with its age band, so the study screen asks the guardian only on a minor
        profile (PRV-06, API-28, D93). Another profile's id answers 404 (API-04)."""
        owner(profile_id)
        return guarded(lambda: directory().profile(profile_id))  # type: ignore[no-any-return]

    @app.patch("/v1/profiles/{profile_id}")
    async def update_profile(profile_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            owner(profile_id)
            asked = require("UpdateProfile", body)
            updated, withdrawn = guarded(lambda: directory().edit(profile_id, asked))
            online = getattr(core, "online", None)
            if online is not None:
                from agent_core.store.consents import PROFILE_SCOPES

                features = {PROFILE_SCOPES[scope][1] for scope in withdrawn if scope in PROFILE_SCOPES}
                if "age_band" in asked and updated["age_band"] != "18_plus":
                    # Existing guardian grants also lose inference permission. End in-flight work even when
                    # no self-granted consent was withdrawn; subsequent requests already fail the age gate.
                    features.update({"cloud_tier", "study_materials", "tutor_memory_online", "cloud_voice"})
                    if core.records_profile == profile_id:
                        core.study.cancel_cloud()
                        if core.voice_close is not None:
                            await core.voice_close()
                for feature in features - {None}:
                    await online.client.cut(feature)
            return 200, updated

        return await mutating(request, run)

    @app.delete("/v1/profiles/{profile_id}")
    async def delete_profile(profile_id: str, request: Request) -> Response:
        async def run(_: Any) -> tuple[int, Any]:
            owner(profile_id)
            directory()  # profiles exist on this computer, or not_found before anything is erased
            erased = await core.erase_profile(profile_id)  # every store of the registry, in order (DAT-09)
            return 200, erased["profile_directory"]

        return await mutating(request, run)

    @app.get("/v1/profiles/{profile_id}/settings")
    async def get_settings(profile_id: str) -> dict[str, Any]:
        owner(profile_id)
        opened, settings = settings_of(profile_id)
        with opened:
            return settings.effective()

    @app.patch("/v1/profiles/{profile_id}/settings")
    async def patch_settings(profile_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            owner(profile_id)
            patch = require("SettingsPatch", body)
            opened, settings = settings_of(profile_id)
            with opened:
                effective = guarded(lambda: settings.patch(patch, "student"))
            announce(profile_id, effective)
            return 200, effective

        return await mutating(request, run)


# --- consent and online features (PRV-05, PRV-06, ARC-11, D53) ------------------------------------------------------

ATTESTED_BY = "Device adult"


def add_consent_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    from agent_core.store import consents as store
    from agent_core.store.sqlite import transaction

    def directory() -> ProfileDirectory:
        if not isinstance(core.directory, ProfileDirectory):
            raise ApiError("not_found", "profiles are not available")
        return core.directory

    def features_on() -> set[str]:
        online = getattr(core, "online", None)
        if online is not None:
            return set(online.features_on())  # hidden features (no DPIA on the release's list) are never on (PRV-08)
        return {r[0] for r in directory().device.execute("SELECT feature FROM online_features WHERE enabled = 1")}

    def profile_features(profile_id: str) -> set[str]:
        features = features_on()
        online = getattr(core, "online", None)
        with directory().open_learner(profile_id) as learner:
            for feature, service in (
                ("cloud_tier", "tutor"),
                ("study_materials", "study"),
                ("tutor_memory_online", "tutor"),
                ("cloud_voice", "voice"),
            ):
                if online is None or not online.service_allowed(learner, service):
                    features.discard(feature)
        return features

    def shown_features() -> list[str]:
        online = getattr(core, "online", None)
        return list(online.shown()) if online is not None else store.shipped_features()

    def notice(scope: str, version: str | None = None) -> str:
        versions = core.notices.get(scope, [])
        if not versions or (version is not None and version not in versions):
            raise ApiError("validation_failed", f"no shipped notice {version or ''} for {scope}".replace("  ", " "))
        return version or versions[-1]

    def guarded(work: Callable[[], Any]) -> Any:
        try:
            return work()
        except (store.ConsentError, ProfileError) as error:
            raise ApiError(error.code, error.message) from None

    @app.get("/v1/profiles/{profile_id}/consents")
    async def consents(profile_id: str) -> Any:
        if CURRENT_PROFILE.get() != profile_id:
            raise ApiError("not_found", "no such profile")
        with directory().open_learner(profile_id) as conn:
            return store.states(
                conn, profile_features(profile_id), core.settings.study_workspace_enabled, core.early_scopes()
            )

    @app.put("/v1/profiles/{profile_id}/consents/{scope}")
    async def switch(profile_id: str, scope: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            if CURRENT_PROFILE.get() != profile_id:
                raise ApiError("not_found", "no such profile")
            if scope in store.DEVICE_SCOPES:
                raise ApiError("scope_not_profile", "telemetry and crash reports are switched for the whole device")
            if scope not in store.shipped_scopes(core.settings.study_workspace_enabled, core.early_scopes()):
                raise ApiError("validation_failed", f"{scope} is not a consent scope of this version")
            asked = require("ConsentSwitch", body)
            d = directory()
            action = asked["action"]
            with d.open_learner(profile_id) as conn, transaction(conn):
                row = store.active(conn, scope)
                role = "student_18_plus" if d.self_consents(profile_id) else "student"
                if action == "on":
                    lab = getattr(core, "licence", None)
                    if store.PROFILE_SCOPES[scope][1] is not None and lab is not None \
                            and lab.state.install_kind == "school_lab":  # fmt: skip
                        # No online scope on a school lab install, whoever asks (PRV-06, JRN-11, BIZ-06a); a research
                        # scope is recorded there as on any install (D13, D100).
                        raise ApiError("feature_disabled", "no online feature on a school lab install")
                    if row is None:
                        if not core.settings.consent_dpia_ref:  # no grant before its DPIA (D13, PRV-08)
                            raise ApiError("feature_disabled", "consent waits for its DPIA reference (D13)")
                        if role == "student" and scope not in ("research_use", "model_training_research"):
                            raise ApiError(  # API-28, TABLE API-32a: the guardian grants it at checkout (D70)
                                "guardian_consent_required", "the first online consent comes from subscription checkout"
                            )
                        if role == "student" and not asked.get("guardian_agreed"):
                            raise ApiError("validation_failed", "a parent or guardian must agree to this study")
                        guarded(
                            lambda: store.grant(
                                conn,
                                scope,
                                "guardian" if role == "student" else role,
                                "Parent or guardian" if role == "student" else "Student",
                                notice(scope, asked.get("notice_version")),
                                core.settings.consent_dpia_ref,
                                core.clock_ms(),
                                include_study=core.settings.study_workspace_enabled,
                                early=core.early_scopes(),
                            )
                        )
                    store.resume(conn, scope)
                    if scope == "study_materials":
                        # The Study agreement names the material and destination.
                        # Record that explicit notice with the validated profile
                        # consent; Study has no separate tutor-session notice card.
                        online = getattr(core, "online", None)
                        if online is not None:
                            online.record_notice(profile_id, scope, True)
                elif action == "pause":
                    store.pause(conn, scope, core.clock_ms())
                elif store.withdraw(conn, scope, role, core.clock_ms()):
                    with transaction(d.device):
                        store.queue_withdrawal(
                            d.device,
                            internal_id(profile_id),
                            scope,
                            core.clock_ms(),
                            core.clock_ms() + int(core.settings.retention_tombstone_days * 86_400_000),
                        )
            feature = store.PROFILE_SCOPES[scope][1]
            if action != "on" and scope in ("study_materials", "cloud_tier") and profile_id == core.records_profile:
                core.study.cancel_cloud()
            online = getattr(core, "online", None)
            if feature is not None and online is not None and action != "on":
                await online.client.cut(feature)
            if feature is not None and online is not None and action == "withdraw":
                d.device.execute(
                    "DELETE FROM outbox WHERE feature = ? AND profile_id IN (?, ?)"
                    " AND kind NOT IN ('consent_withdrawal','tombstone')",
                    (feature, profile_id, internal_id(profile_id)),
                )
            with d.open_learner(profile_id) as conn:
                return 200, next(
                    s
                    for s in store.states(
                        conn, profile_features(profile_id), core.settings.study_workspace_enabled, core.early_scopes()
                    )
                    if s["scope"] == scope
                )

        return await mutating(request, run)

    def connectivity_state() -> dict[str, Any]:
        online = getattr(core, "online", None)
        if online is not None:
            state: dict[str, Any] = online.state_view(extended=_declares_queue())
            return state
        on = features_on()
        return {
            "network": core.network_state,
            "features": {f: {"enabled": f in on, "state": "idle"} for f in store.shipped_features()},
        }

    @app.get("/v1/connectivity")
    async def connectivity() -> dict[str, Any]:
        return connectivity_state()

    @app.patch("/v1/connectivity")
    async def patch_connectivity(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = require("ConnectivityPatch", body)["features"]
            unknown = [f for f in asked if f not in shown_features()]
            if unknown:
                raise ApiError("validation_failed", f"not an online feature of this version: {', '.join(unknown)}")
            needs_consent = [f for f, on in asked.items() if on and f not in ("downloads", "update_checks")]
            if needs_consent and not core.settings.consent_dpia_ref:
                raise ApiError("feature_disabled", "online features that use learner data wait for the DPIA (D13)")
            d = directory()
            now = core.clock_ms()
            for feature, on in asked.items():
                with transaction(d.device):
                    d.device.execute(
                        "INSERT INTO online_features (feature, enabled, enabled_by_role, enabled_at, disabled_at)"
                        " VALUES (?, ?, 'user', ?, ?) ON CONFLICT (feature) DO UPDATE SET enabled = excluded.enabled,"
                        " enabled_at = coalesce(excluded.enabled_at, enabled_at),"
                        " disabled_at = coalesce(excluded.disabled_at, disabled_at)",
                        (feature, int(on), now if on else None, None if on else now),
                    )
                    d.audit(f"online_feature.{'enable' if on else 'disable'}", None, {"feature": feature})
                    if feature in store.DEVICE_SCOPES:
                        if on:
                            guarded(partial(store.device_grant, d.device, feature, notice(feature),
                                            core.settings.consent_dpia_ref, now))  # fmt: skip
                        else:
                            store.device_withdraw(d.device, feature, now)
            online = getattr(core, "online", None)
            if online is not None:  # queued payloads and requests in flight of a feature switched off stop now
                if any(asked.get(scope) is False for scope in ("study_materials", "cloud_tier")):
                    core.study.cancel_cloud()
                await online.switched(asked)
            return 200, connectivity_state()

        return await mutating(request, run)


def _declares_queue() -> bool:
    """Whether the bundled contract declares `queued_items` and `last_sent_at` (UXS-06, ARC-10)."""
    from agent_core.contracts import document

    entry = document("api/commands")["$defs"]["ConnectivityState"]["properties"]["features"]["additionalProperties"]
    return "queued_items" in entry.get("properties", {})
