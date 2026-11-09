"""Model-free assessment HTTP commands (API-27, UXS-24 to UXS-26 API side)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CURRENT_PROFILE, CoreService, require
from agent_core.api.errors import ApiError, invalid
from agent_core.api.profiles import Mutating
from agent_core.engine.assessment.errors import Refused


def desk(core: CoreService) -> Any:
    if core.directory is not None and core.directory.exists() and CURRENT_PROFILE.get() != core.records_profile:
        raise ApiError("not_found", "this profile is not open")
    opened = core.assessments() if core.assessments is not None else None
    if opened is None:
        raise ApiError("not_found", "assessment needs an open learner database")
    return opened


def translate(error: Refused) -> ApiError:
    code = (
        error.reason
        if error.reason in ("tutor_locked", "not_found", "request_conflict", "worker_unavailable")
        else "validation_failed"
    )
    return ApiError(code, error.reason, details={"reason": error.reason})


def checked(definition: str, body: Any) -> dict[str, Any]:
    refused = invalid("learning/student-records#/$defs/" + definition, body)  # value-free (API-32)
    if refused is not None:
        raise refused
    return dict(body)


def require_answer(definition: str, body: Any) -> dict[str, Any]:
    """An answer with its typed working lines (`Answer.working`, ASM-09, ASM-17; D92)."""
    return dict(require(definition, body))


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    @app.get("/v1/lessons/{lesson_id}/probe")
    async def lesson_probe(lesson_id: str) -> Any:
        try:
            return desk(core).lesson_probe(lesson_id)
        except Refused as error:
            raise translate(error) from None

    @app.post("/v1/probes/{identity}/answers")
    async def probe_answer(identity: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            require("ProbeAnswer", body)
            try:
                loaded = desk(core).answer_probe(identity, body["option_id"])
                return 200, core.create_session(
                    {
                        "client_session_id": "cli_" + identity,
                        "mode": "guided",
                        "subject": loaded["subject"],
                        "origin": loaded["origin"],
                    }
                )
            except Refused as error:
                raise translate(error) from None

        return await mutating(request, run)

    @app.post("/v1/assessments")
    async def start(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = checked("AssessmentStart", body)
            if core._turn_running():
                raise ApiError("turn_active", "finish the tutor turn before starting an assessment")
            try:
                return 200, desk(core).start_assessment(asked["kind"], asked["subject"], asked.get("kc_id"))
            except Refused as error:
                raise translate(error) from None

        return await mutating(request, run)

    @app.get("/v1/assessments")
    async def all_assessments() -> list[Any]:
        return list(desk(core).list_assessments())

    @app.get("/v1/assessments/{identity}")
    async def assessment(identity: str) -> dict[str, Any]:
        try:
            return dict(desk(core).assessment(identity))
        except Refused as error:
            raise translate(error) from None

    @app.post("/v1/assessments/{identity}/answers")
    async def assessment_answer(identity: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = checked("AssessmentAnswer", body)
            try:
                return 200, await desk(core).assessment_answer(
                    identity,
                    asked["client_answer_id"],
                    asked["item_id"],
                    asked.get("answer"),
                    not_learned=asked["action"] == "not_learned",
                )
            except Refused as error:
                raise translate(error) from None

        return await mutating(request, run)

    @app.post("/v1/assessments/{identity}/stop")
    async def stop(identity: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            try:
                return 200, desk(core).stop_assessment(identity)
            except Refused as error:
                raise translate(error) from None

        return await mutating(request, run)

    @app.post("/v1/assessments/{identity}/explain")
    async def explain(identity: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = checked("ExplainBack", body)
            try:
                return 200, await desk(core).explain_back(
                    identity, asked["client_explain_id"], asked["text"], asked["modality"]
                )
            except Refused as error:
                raise translate(error) from None

        return await mutating(request, run)

    @app.post("/v1/assessments/{identity}/resume")
    async def resume(identity: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            try:
                return 200, desk(core).resume_assessment(identity)
            except Refused as error:
                raise translate(error) from None

        return await mutating(request, run)

    @app.post("/v1/practice/sets")
    async def practice(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = require("CreatePracticeSet", body)
            try:
                return 200, desk(core).create_practice(asked["subject"], asked.get("kc_ids") or [], asked["size"])
            except Refused as error:
                raise translate(error) from None

        return await mutating(request, run)

    @app.get("/v1/practice/sets/{identity}")
    async def practice_set(identity: str) -> dict[str, Any]:
        try:
            return dict(desk(core).practice(identity))
        except Refused as error:
            raise translate(error) from None

    @app.post("/v1/practice/sets/{identity}/answers")
    async def practice_answer(identity: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = require_answer("PracticeAnswer", body)
            try:
                return 200, await desk(core).practice_answer(
                    identity,
                    asked["client_attempt_id"],
                    asked["item_id"],
                    asked["answer"],
                    asked["time_ms"],
                    asked["hints_used"],
                )
            except Refused as error:
                raise translate(error) from None

        return await mutating(request, run)

    @app.post("/v1/practice/sets/{identity}/reveal")
    async def reveal_item(identity: str, request: Request) -> Response:
        """ "Don't know" (ASM-28): the question's worked answer and the next question. Proposed in
        contract-additions-m45d.json (D72); the body is checked here until the command contract adopts it."""

        async def run(body: Any) -> tuple[int, Any]:
            if (
                not isinstance(body, dict)
                or set(body) != {"item_id"}
                or not isinstance(body["item_id"], str)
                or not 0 < len(body["item_id"]) <= 96
            ):
                raise ApiError("validation_failed", "send the item_id of the question shown")
            try:
                return 200, desk(core).practice_reveal(identity, body["item_id"])
            except Refused as error:
                raise translate(error) from None

        return await mutating(request, run)

    @app.post("/v1/practice/sets/{identity}/help")
    async def help_item(identity: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = require("PracticeHelp", body)
            try:
                loaded = desk(core).practice_help(identity, asked["item_id"], asked.get("reason") or "help")
                return 200, core.create_session(
                    {
                        "client_session_id": "cli_" + identity.removeprefix("set_"),
                        "mode": loaded["mode"],
                        "subject": loaded["subject"],
                        "origin": loaded["origin"],
                    }
                )
            except Refused as error:
                raise translate(error) from None

        return await mutating(request, run)

    @app.post("/v1/mocks")
    async def create_mock(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = require("StartMock", body)
            if core._turn_running():
                raise ApiError("turn_active", "finish the tutor queue before starting a mock")
            try:
                return 200, desk(core).create_mock(asked["paper_id"])
            except Refused as error:
                raise translate(error) from None

        return await mutating(request, run)

    @app.get("/v1/mocks")
    async def mocks() -> dict[str, Any]:
        """The papers on this computer and the profile's sittings (UXS-25, `MockList`, D93)."""
        return dict(desk(core).list_mocks())

    @app.get("/v1/mocks/{identity}")
    async def mock(identity: str) -> dict[str, Any]:
        try:
            return dict(desk(core).mock(identity))
        except Refused as error:
            raise translate(error) from None

    @app.post("/v1/mocks/{identity}/{action}")
    async def mock_action(identity: str, action: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            try:
                opened = desk(core)
                if action == "submit":
                    return 200, await opened.submit_mock(identity)
                if action in ("start", "pause", "resume"):
                    if action == "start" and core._turn_running():
                        raise ApiError("turn_active", "finish the tutor queue before starting a mock")
                    reply = opened.transition(identity, action)
                    if action == "start":
                        core.mock_started()
                    return 200, reply
                if action == "navigate":
                    asked = require("MockNavigate", body)
                    return 200, opened.navigate(identity, asked["question_id"])
                if action == "integrity":
                    asked = require("MockIntegrity", body)
                    return 200, {"recorded": opened.integrity(identity, asked["kind"], asked["amount"])}
                raise ApiError("not_found", "unknown mock command")
            except Refused as error:
                raise translate(error) from None

        return await mutating(request, run)

    @app.put("/v1/mocks/{identity}/answers/{question_id}")
    async def save(identity: str, question_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = require_answer("MockAnswer", body)
            try:
                return 200, desk(core).save_mock_answer(identity, question_id, asked["answer"], asked["flagged"])
            except Refused as error:
                raise translate(error) from None

        return await mutating(request, run)

    @app.get("/v1/search/items")
    async def search(q: str = "") -> list[Any]:
        return list(desk(core).search(q))

    @app.get("/v1/items/{item_id}/solution")
    async def solution(item_id: str) -> Any:
        """UXS-51, PED-09: the full worked solution of an item on request (a `WorkedExampleView`; adopted in D101),
        behind the same filter as search, so reserved and protected items expose nothing."""
        try:
            return desk(core).solution(item_id)
        except Refused as error:
            raise translate(error) from None


def add_past_paper_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    """Imported past papers (ASM-27, D119): import from the study area, list, read and remove. A paper is sat with
    `POST /v1/mocks` and its `ppr_` id, timed and locked like any mock."""
    from agent_core.engine.study import StudyError

    def sources(request: Request, selection: dict[str, Any]) -> list[dict[str, Any]]:
        """The source's passages, after the study area's own grant check for a private source."""
        if not core.settings.study_workspace_enabled or getattr(core, "study", None) is None:
            raise ApiError("feature_disabled", "importing a paper reads it in the study area")
        try:
            core.study.authorise(selection["source_id"], request.headers.get("x-study-open", ""),
                                 selection["revision"])  # fmt: skip
            return list(core.study.desk().selected([selection]))
        except StudyError as error:
            if error.reason == "not_found":
                raise ApiError("not_found", "no such source") from None
            raise ApiError("validation_failed", error.reason, details={"reason": error.reason}) from None

    @app.get("/v1/mocks/{identity}/figures/{question_id}")
    async def figure(identity: str, question_id: str) -> Response:
        """A question's diagram page while its paper is sat: read from the study area without its sitting lock, never
        for a private source (ASM-27)."""
        try:
            found = desk(core).figure(identity, question_id)
        except Refused as error:
            raise translate(error) from None
        if getattr(core, "study", None) is None:
            raise ApiError("not_found", "no study area")
        try:
            study = core.study.desk()
            if study.source(found["source_id"]).get("private"):
                raise ApiError("not_found", "a private source's pages are shown only in the study area")
            png = await study.page_raster(found["source_id"], int(found["revision"]), int(found["page"]))
        except StudyError:
            raise ApiError("not_found", "the page could not be read") from None
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/v1/past-papers")
    async def past_papers() -> dict[str, Any]:
        return dict(desk(core).list_past_papers())

    @app.post("/v1/past-papers")
    async def import_paper(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = require("PastPaperImport", body)
            opened = desk(core)
            paper = sources(request, asked["paper"])
            scheme = sources(request, asked["mark_scheme"]) if asked.get("mark_scheme") else None
            row = opened.learner.conn.execute("SELECT level FROM profile").fetchone()
            try:
                return 200, opened.import_past_paper(asked, paper, scheme, str(row[0] if row else "O"))
            except Refused as error:
                raise translate(error) from None

        return await mutating(request, run)

    @app.get("/v1/past-papers/{paper_id}")
    async def past_paper(paper_id: str) -> dict[str, Any]:
        try:
            return dict(desk(core).past_paper(paper_id))
        except Refused as error:
            raise translate(error) from None

    @app.delete("/v1/past-papers/{paper_id}")
    async def remove_paper(paper_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            try:
                return 200, desk(core).delete_past_paper(paper_id)
            except Refused as error:
                raise translate(error) from None

        return await mutating(request, run)
