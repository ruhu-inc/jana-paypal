"""The first-session interview over the local API (UXS-10 step 2, JRN-03 step 3; `engine.interview`).

- `GET /v1/interview`: the open profile's interview: the current question as a `card_question` (one per card), how
  many were answered, and whether it is done.
- `POST /v1/interview/{question_id}/answer`: the student's answer, a `CardAnswer` body (option ids, their own words
  for the goals question, or `skipped`); the reply is the interview after it.

Both are the commands contract's (API 1.10, D104: `InterviewView`, `CardAnswer`); the answer route is screened like a
note (STU-11, PRV-13, PRV-14) before anything is stored, and needs the profile's token like every learning route."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CURRENT_PROFILE, CoreService, require
from agent_core.api.errors import ApiError
from agent_core.api.profiles import Mutating
from agent_core.engine.interview import Interview, InterviewRefused
from agent_core.engine.receipts import default_receipts


def desk(core: CoreService) -> Interview:
    if core.directory is not None and core.directory.exists() and CURRENT_PROFILE.get() != core.records_profile:
        raise ApiError("not_found", "this profile is not open")
    opened = core.planner() if core.planner is not None else None  # the open learner database's desk
    if opened is None:
        raise ApiError("not_found", "the interview needs an open learner database")
    return Interview(opened.learner, default_receipts(), core.clock_ms)


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    @app.get("/v1/interview")
    async def interview() -> dict[str, Any]:
        return desk(core).view()

    @app.post("/v1/interview/{question_id}/answer")
    async def answer(question_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = require("CardAnswer", body)
            try:
                return 200, desk(core).answer(question_id, asked["answer"])
            except InterviewRefused as error:
                raise ApiError("validation_failed", error.reason, details={"reason": error.reason}) from None

        return await mutating(request, run)
