"""The personal learning agent's routes (SPEC §19, W8).

- `GET /v1/companion/opener` (phase 1): the Last time card on Home and in Study: facts by code from the open
  profile's learner database, worded by the engine's templates, no model call (§19.5.2).
- `POST /v1/companion` (phase 2): today's episode of the thread, opened or resumed (`CompanionOpened`); the opener is
  in this reply only, never on the stream. Turns go through `POST /v1/sessions/{session_id}/turns`.
- `GET /v1/companion/summary` (phase 2): the thread's rolling summary, folded by code (§19.5.3).
- Check-ins (phase 3, §19.3.4, §19.5.5): `GET /v1/checkins/due` (at most one, chosen in one transaction),
  `POST /v1/checkins/{commitment_id}/dismiss`, and `POST /v1/checkins/{commitment_id}/open` (the thread's episode,
  opened with the check-in's question; its first accepted turn answers it)."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CURRENT_PROFILE, CoreService, require
from agent_core.api.errors import ApiError
from agent_core.api.profiles import Mutating
from agent_core.engine.companion_opener import OpenerDesk
from agent_core.engine.profile_scope import ProfileChanged


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    @app.get("/v1/companion/opener")
    async def opener() -> Any:
        if core.directory is not None and core.directory.exists() and CURRENT_PROFILE.get() != core.records_profile:
            raise ApiError("not_found", "this profile is not open")
        records = core.records
        if records is None or records.learner is None or core.records_profile is None:
            raise ApiError("not_found", "the card needs an open learner database")
        companion = getattr(core, "companion", None)

        def study_cards(now: int) -> int:
            try:
                return int(companion.study_cards_due(now)) if companion is not None else 0
            except (sqlite3.Error, AttributeError, ValueError):
                return 0

        return OpenerDesk(records.learner, core.settings, core.clock_ms, study_cards=study_cards).opener()

    def opened(body: Any) -> str | None:
        profile_open()
        require("CompanionOpen", body or {})
        online, profile = getattr(core, "online", None), core.records_profile
        if online is None or profile is None:
            return None
        answer = (body or {}).get("cloud_notice")
        if answer in ("accepted", "declined"):
            online.record_notice(profile, "cloud_tier", answer == "accepted")
        assert core.records is not None and core.records.learner is not None
        if not online.cloud_effective(profile, core.records.learner.conn):
            return None
        given = online.notice_answer(profile, "cloud_tier")
        if given is None:
            return "notice_required"
        if given == "declined" and core.tutor_unavailable is not None and core.tutor_unavailable():
            return "notice_declined"
        return None

    def availability(reply: dict[str, Any], notice: str | None) -> dict[str, Any]:
        return {
            **reply,
            "notice": notice,
            "can_reply": notice is None and not (core.tutor_unavailable is not None and core.tutor_unavailable()),
        }

    def moment(body: Any) -> None:
        """Ask about this moment (SPEC §20.3): the open's `context` becomes the episode's pending turn context once
        the material is this profile's; a material that is not is `not_found`."""
        context = (body or {}).get("context")
        if not context:
            return
        try:
            core.study.desk().source(context["source_id"])
        except Exception:
            raise ApiError("not_found", "no such material") from None
        core.companion.set_context({"source_id": context["source_id"], "at_ms": int(context["at_ms"])})

    @app.post("/v1/companion")
    async def open_thread(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            notice = opened(body)
            moment(body)
            try:
                reply = await core.open_companion()
            except LookupError:
                raise ApiError("not_found", "the companion needs an open learner database") from None
            return 200, require("CompanionOpened", availability(reply, notice))

        return await mutating(request, run)

    @app.get("/v1/companion/summary")
    async def summary() -> Any:
        if core.directory is not None and core.directory.exists() and CURRENT_PROFILE.get() != core.records_profile:
            raise ApiError("not_found", "this profile is not open")
        try:
            return require("CompanionSummary", core.companion.fold())
        except LookupError:
            raise ApiError("not_found", "the summary needs an open learner database") from None

    def profile_open() -> None:
        if core.directory is not None and core.directory.exists() and CURRENT_PROFILE.get() != core.records_profile:
            raise ApiError("not_found", "this profile is not open")
        if core.records is None or core.records.learner is None or core.records_profile is None:
            raise ApiError("not_found", "check-ins need an open learner database")

    @app.get("/v1/companion/history")
    async def history(limit: int = 50, before: str | None = None) -> Any:
        from agent_core.engine.companion_history import public_page

        profile_open()
        if not 1 <= limit <= 100:
            raise ApiError("validation_failed", "history limit must be between 1 and 100")
        assert core.records is not None and core.records.learner is not None
        try:
            return require("CompanionHistory", public_page(core.records.learner.conn, limit=limit, before=before))
        except ValueError:
            raise ApiError("validation_failed", "invalid history cursor") from None

    @app.get("/v1/checkins/due")
    async def checkins_due() -> Any:
        profile_open()
        return require("CheckinsDue", {"items": core.checkins.due()})

    @app.post("/v1/checkins/{commitment_id}/dismiss")
    async def checkin_dismiss(commitment_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            profile_open()
            try:
                return 200, require("Commitment", core.checkins.dismiss(commitment_id))
            except LookupError:
                raise ApiError("not_found", "no such check-in") from None

        return await mutating(request, run)

    @app.post("/v1/checkins/{commitment_id}/open")
    async def checkin_open(commitment_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            notice = opened(body)
            profile_open()
            try:
                checkin = core.checkins.opening(commitment_id)
            except LookupError:
                raise ApiError("not_found", "no such check-in") from None
            try:
                reply = await core.open_companion(checkin=checkin)
            except ProfileChanged:
                raise ApiError("not_found", "the check-in's profile is no longer open") from None
            return 200, require("CompanionOpened", availability(reply, notice))

        return await mutating(request, run)
