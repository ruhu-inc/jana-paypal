"""Profile-local learning handoffs. No provider calls or private memory export."""

from typing import Any

from fastapi import FastAPI, Request, Response

from agent_core.api.assessment import desk, translate
from agent_core.api.core import CoreService
from agent_core.api.errors import invalid
from agent_core.api.profiles import Mutating
from agent_core.engine.assessment.errors import Refused
from agent_core.engine.learning_journey import PREFIX, LearningJourney
from agent_core.engine.study import StudyError


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    @app.get("/v1/learning-journey")
    async def current() -> Any:
        try:
            return LearningJourney(desk(core)).reply()
        except Refused as error:
            raise translate(error) from None

    @app.post("/v1/learning-journey")
    async def start(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            problem = invalid(PREFIX + "Start", body)
            if problem:
                raise problem
            try:
                study = None
                if source := body.get("source"):
                    core.study.authorise(
                        source["source_id"], request.headers.get("x-study-open", ""), source["revision"]
                    )
                    study = core.study.desk()
                return 200, LearningJourney(desk(core)).start(body, study)
            except (Refused, StudyError) as error:
                raise translate(Refused(error.reason)) from None

        return await mutating(request, run)

    @app.post("/v1/learning-journey/step")
    async def step(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            problem = invalid(PREFIX + "Step", body)
            if problem:
                raise problem
            try:
                return 200, LearningJourney(desk(core)).step(body["action"])
            except Refused as error:
                raise translate(error) from None

        return await mutating(request, run)
