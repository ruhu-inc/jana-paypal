"""Plans and progress over the local API (LRN-28 to LRN-33, UXS-12, UXS-27): model-free commands on the open profile.

- `GET /v1/plans/current`: the plan Home shows (the latest of today's proposals not replaced by a change), or null.
- `POST /v1/plans`: a new proposal, at any hour and as often as asked (Keep learning after a finished plan). Retries
  carry the same `Idempotency-Key` and get the same plan, so Keep learning starts exactly one plan per request.
- `POST /v1/plans/{plan_id}/change`: the student's change to that proposal only (an alternative main block, a topic,
  an order, or shortening); the changed plan is a new proposal.
- `GET /v1/progress`: the Progress screen's states, bands, history, time studied, streak and the gated grade band.
- `GET/POST /v1/exam-dates`, `PATCH/DELETE /v1/exam-dates/{exam_id}`: the learner's exam dates, the countdown and
  the reminders due (LRN-44); device-local, never events.

The Home card is answered through the card route: `POST /v1/cards/req_<plan_id>/answer` (API-14). Shapes are the
commands contract's `PlanCurrent`, `PlanView`, `PlanRequest`, `PlanChange` and `ProgressView` (D99).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CURRENT_PROFILE, CoreService, require
from agent_core.api.errors import ApiError
from agent_core.api.profiles import Mutating
from agent_core.engine.exam_dates import ExamDateRefused
from agent_core.engine.plans import PlanRefused

ALL_REVIEWS = "all_reviews"


def desk(core: CoreService) -> Any:
    if core.directory is not None and core.directory.exists() and CURRENT_PROFILE.get() != core.records_profile:
        raise ApiError("not_found", "this profile is not open")
    opened = core.planner() if core.planner is not None else None
    if opened is None:
        raise ApiError("not_found", "plans need an open learner database")
    return opened


def translate(error: PlanRefused) -> ApiError:
    code = "not_found" if error.reason == "not_found" else "validation_failed"
    return ApiError(code, error.reason, details={"reason": error.reason})


def _object(body: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(body, dict) or set(body) - allowed:
        raise ApiError("validation_failed", "unexpected fields")
    return body


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    @app.get("/v1/plans/current")
    async def current() -> Any:
        return desk(core).current()

    @app.post("/v1/plans")
    async def propose(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = _object(body or {}, {"client_request_id", ALL_REVIEWS, "kc_id"})
            if not isinstance(asked.get(ALL_REVIEWS, False), bool) or not isinstance(asked.get("kc_id", ""), str):
                raise ApiError("validation_failed", "all_reviews is a boolean and kc_id a KC id")
            try:
                return 200, desk(core).propose(all_reviews=bool(asked.get(ALL_REVIEWS)), kc_id=asked.get("kc_id"),
                                               request_key=asked.get("client_request_id"))  # fmt: skip
            except PlanRefused as error:
                raise translate(error) from None

        return await mutating(request, run)

    @app.post("/v1/plans/{plan_id}/change")
    async def change(plan_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = _object(body, {"alternative_index", "kc_id", "order", "shorten"})
            if len(asked) != 1:
                raise ApiError("validation_failed", "name exactly one change")
            order = asked.get("order")
            if order is not None and not (isinstance(order, list) and all(isinstance(i, int) for i in order)):
                raise ApiError("validation_failed", "order lists block indexes")
            alternative = asked.get("alternative_index")
            if alternative is not None and (isinstance(alternative, bool) or not isinstance(alternative, int)):
                raise ApiError("validation_failed", "alternative_index is an index")
            try:
                return 200, desk(core).change(plan_id, alternative=alternative, shorten=asked.get("shorten") is True,
                                              order=order, kc_id=asked.get("kc_id"))  # fmt: skip
            except PlanRefused as error:
                raise translate(error) from None

        return await mutating(request, run)

    @app.get("/v1/progress")
    async def progress(subject: str | None = None) -> Any:
        view = desk(core).progress(subject)
        assessments = core.assessments() if core.assessments is not None else None
        view["studied_checkable"] = [
            kc for kc in view["studied_today"] if assessments is not None and assessments.check_possible(kc)
        ]
        return view

    def refused(error: ExamDateRefused) -> ApiError:
        code = "not_found" if error.reason == "not_found" else "validation_failed"
        return ApiError(code, error.reason, details={"reason": error.reason})

    @app.get("/v1/exam-dates")
    async def exam_dates() -> Any:
        return desk(core).exams().list()

    @app.post("/v1/exam-dates")
    async def add_exam_date(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = require("ExamDateCreate", body)
            try:
                return 200, desk(core).exams().create(asked)
            except ExamDateRefused as error:
                raise refused(error) from None

        return await mutating(request, run)

    @app.patch("/v1/exam-dates/{exam_id}")
    async def change_exam_date(exam_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = require("ExamDatePatch", body)
            try:
                return 200, desk(core).exams().patch(exam_id, asked)
            except ExamDateRefused as error:
                raise refused(error) from None

        return await mutating(request, run)

    @app.delete("/v1/exam-dates/{exam_id}")
    async def remove_exam_date(exam_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            try:
                return 200, desk(core).exams().delete(exam_id)
            except ExamDateRefused as error:
                raise refused(error) from None

        return await mutating(request, run)
