"""Crash reports and the support share over the local API (OBS-17, OBS-18, D85, D86).

`GET /v1/crash-reports` lists the crash reports waiting for a decision, each exactly as it would be sent (the allowed
fields of `net.crash_reports.build_report`). Nothing is listed while `crash_reports` is off: the app never prompts
anyone to go online (PLT-18). `POST /v1/crash-reports/{crash_id}/send` is "Send" on "Send this report?": the report
is queued for upload, and only while the feature is on (`feature_disabled` otherwise); `.../dismiss` is "Don't send".
Each report is decided once, and every report asks each time: there is no automatic option (D53). A report the
redaction scanner would refuse is never listed. Both use the launch token only (D86: anyone at the laptop decides).

`POST /v1/sessions/{session_id}/support-share` is the student's own "Let support see this session" for one session
of the open profile (OBS-18): recorded in that profile's `support_shares` table, so only a request made with the
student's profile token can make it, and another profile's session is a missing resource. Nothing is sent: the
support report is a local file the app never sends itself."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CURRENT_PROFILE, CoreService
from agent_core.api.errors import ApiError
from agent_core.api.profiles import Mutating
from agent_core.store.profiles import ProfileDirectory
from agent_core.store.sqlite import transaction

DECISIONS = "crash_report_decisions"  # device `app_settings` key: {crash_id: "sent" | "dismissed"}
ACTIONS = ("send", "dismiss")
FEATURE = "crash_reports"  # the online feature (TABLE ARC-07a) a report needs to leave


def confirm_share(directory: ProfileDirectory, profile_id: str, session_id: str, now_ms: int) -> None:
    """The student's own confirmation for one session (OBS-18); confirming again keeps the first time."""
    with directory.open_learner(profile_id) as conn, transaction(conn):
        conn.execute("INSERT INTO support_shares (session_id, confirmed_at, included_at) VALUES (?, ?, NULL)"
                     " ON CONFLICT (session_id) DO NOTHING", (session_id.removeprefix("ses_"), now_ms))  # fmt: skip


def shared_sessions(directory: ProfileDirectory, profile_id: str) -> list[str]:
    with directory.open_learner(profile_id) as conn:
        return [f"ses_{row[0]}" for row in conn.execute("SELECT session_id FROM support_shares ORDER BY confirmed_at")]


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    def directory() -> ProfileDirectory:
        if not isinstance(core.directory, ProfileDirectory):
            raise ApiError("not_found", "profiles are not available")
        return core.directory

    def online() -> Any:
        """The connectivity layer (ARC-06), which owns the crash report rules (`net.crash_reports`)."""
        layer = getattr(core, "online", None)
        if layer is None:
            raise ApiError("feature_disabled", "crash reports are off")
        return layer

    def features_on() -> set[str]:
        layer = getattr(core, "online", None)
        return set(layer.features_on()) if layer is not None else set()

    def decisions() -> dict[str, str]:
        row = directory().device.execute("SELECT value FROM app_settings WHERE key = ?", (DECISIONS,)).fetchone()
        return dict(json.loads(row[0])) if row else {}

    def decide(crash_id: str, decision: str) -> None:
        d = directory()
        kept = {r["crash_id"] for r in records()}
        current = {k: v for k, v in decisions().items() if k in kept}  # forget decisions on pruned records
        current[crash_id] = decision
        now = core.clock_ms()
        with transaction(d.device):
            d.device.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET"
                " value = excluded.value, updated_at = excluded.updated_at",
                (DECISIONS, json.dumps(current, sort_keys=True), now),
            )

    def records() -> list[dict[str, Any]]:
        store = getattr(core, "crashes", None)
        return list(store.records()) if store is not None else []

    def terms() -> list[str]:
        """The device's identifying terms the redaction scanner looks for: every profile's display name."""
        return [str(p["display_name"]) for p in directory().summaries()["profiles"]]

    def hardware_class() -> str | None:
        row = directory().device.execute("SELECT hardware_class FROM device").fetchone()
        return str(row[0]) if row and row[0] else None

    def waiting() -> list[dict[str, Any]]:
        if FEATURE not in features_on():
            return []
        decided, hardware, identifying = decisions(), hardware_class(), terms()
        layer = online()
        built = (layer.crash_report(r, hardware) for r in records() if r.get("crash_id") not in decided)
        listed = [report for report in built if not layer.crash_report_problems(report, identifying)]
        return listed[: core.settings.crash_reports_queued_max]

    @app.get("/v1/telemetry/schema")
    async def telemetry_schema() -> dict[str, Any]:
        from agent_core.config.telemetry_schema import schema

        return schema()

    @app.get("/v1/crash-reports")
    async def crash_list() -> dict[str, Any]:
        return {"reports": waiting()}

    @app.post("/v1/crash-reports/{crash_id}/{action}")
    async def crash_decision(crash_id: str, action: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            if action not in ACTIONS:
                raise ApiError("not_found", "no such action")
            if action == "send" and FEATURE not in features_on():
                raise ApiError("feature_disabled", "crash reports are off")
            report = next((r for r in waiting() if r["crash_id"] == crash_id), None)
            if report is None:
                raise ApiError("not_found", "no such crash report waiting")
            if action == "send":
                expires = core.clock_ms() + core.settings.crash_record_retention_days * 86_400_000
                try:
                    online().send_crash_report(report, terms(), expires)
                except ValueError as refused:  # net.crash_reports.CrashReportRefused
                    switched_off = getattr(refused, "code", "") == "feature_disabled"
                    raise ApiError("feature_disabled" if switched_off else "not_found", str(refused)) from None
            decide(crash_id, "sent" if action == "send" else "dismissed")
            return 204, None

        return await mutating(request, run)

    @app.post("/v1/sessions/{session_id}/support-share")
    async def support_share(session_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            profile = CURRENT_PROFILE.get()
            if profile is None:
                raise ApiError("not_found", "no such session")
            core.entry(session_id)  # a session of the open profile, or not_found (API-04)
            confirm_share(directory(), profile, session_id, core.clock_ms())
            return 204, None

        return await mutating(request, run)
