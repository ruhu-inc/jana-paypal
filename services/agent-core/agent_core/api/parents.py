"""The parent summary, the alerts and private session history over the local API (TCH-12, TCH-14, UXS-33, UXS-46a,
UXS-48): model-free reads of the open profile only, offline, with no sharing route.

- `GET /v1/digests/latest`: the latest due week's digest (`ParentDigest`), Settings → "Parent summary".
- `GET /v1/alerts`: the alerts not put away, newest first (`AlertList`), Settings → "Alerts";
  `POST /v1/alerts/{alert_id}/dismiss` puts one away (ids only; 204).
- `GET /v1/history[?before=&limit=]` and `GET /v1/history/{session_id}`: Progress → "Session history"
  (`HistoryList`, `HistorySession`); a session does not open while a mock is sat, like the notes (ASM-18).

None of these is in the command contract yet: the shapes are proposed in contract-additions-m33a.json (D72), and the
dismiss body is checked here until the contract adopts them. A request for another profile's data finds nothing: every
route reads the open profile's learner database (DAT-14)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, Response

from agent_core.api import plans as plan_routes
from agent_core.api.core import CoreService
from agent_core.api.errors import ApiError
from agent_core.api.profiles import Mutating
from agent_core.engine.history import HistoryDesk, HistoryRefused
from agent_core.engine.parents import ParentDesk


def parent_desk(core: CoreService) -> ParentDesk:
    planner = plan_routes.desk(core)  # the open profile's learner database, or not_found
    return ParentDesk(planner.conn, planner.params, planner.content, core.settings, clock_ms=core.clock_ms)


def history_desk(core: CoreService, opening: bool = False) -> HistoryDesk:
    planner = plan_routes.desk(core)
    if opening and core.mock_active is not None and core.mock_active():
        raise ApiError("tutor_locked", "a session opens again once the mock is submitted")
    return HistoryDesk(planner.conn, planner.content, core.settings.retention_transcript_days)


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    @app.get("/v1/digests/latest")
    async def latest_digest() -> dict[str, Any]:
        return parent_desk(core).latest_digest()

    @app.get("/v1/alerts")
    async def alerts() -> dict[str, Any]:
        return {"alerts": parent_desk(core).alerts()}

    @app.post("/v1/alerts/{alert_id}/dismiss")
    async def dismiss(alert_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            if body != {}:
                raise ApiError("validation_failed", "dismissing an alert takes no body")
            if not parent_desk(core).dismiss(alert_id):
                raise ApiError("not_found", "no such alert")
            return 204, None

        return await mutating(request, run)

    @app.get("/v1/history")
    async def history(limit: int = 50, before: str | None = None) -> dict[str, Any]:
        return history_desk(core).list(limit, before)

    @app.get("/v1/history/{session_id}")
    async def history_session(session_id: str) -> dict[str, Any]:
        try:
            return history_desk(core, opening=True).session(session_id)
        except HistoryRefused:
            raise ApiError("not_found", "no such session") from None
