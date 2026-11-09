"""The one Home input's routes (UXS-67) and Ask anything (STU-20, D117).

- `POST /v1/ask/route` {text}: where the box's words go (`tutor`, `study` or `ask`) and whether Ask anything can run
  now. Read-only and local: nothing is stored or sent.
- `POST /v1/ask` {question, mode?, reach?}: the question is routed again here, so a question in an installed subject
  is handed to the tutor with no answer whatever the app asked; any other question is answered by the online feature
  `ask_anything` under the profile's policy. The reply is always 200 with a `state`: `answered`, `unavailable`
  (offline, no online plan, consent or notice missing, allowance used up) or `failed`.

The input guard runs on the question in the API's mutating wrapper before anything else (`screening.ROUTES`)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from agent_core.api.core import CURRENT_PROFILE, CoreService
from agent_core.api.errors import ApiError
from agent_core.api.profiles import Mutating
from agent_core.engine.ask_anything import (
    QUESTION_MAX,
    AskError,
    compose,
    handoff,
    own_evidence,
    policy_for,
    request_body,
    source_config,
)
from agent_core.engine.ask_route import route_input
from agent_core.engine.events import new_id

TEXT_MAX = 20000
WAITING = (
    "offline",
    "needs_online_plan",
    "consent_required",
    "notice_required",
    "allowance_exhausted",
)


def _index(core: CoreService) -> Any:
    knowledge = getattr(core, "knowledge", None)
    try:
        return knowledge() if knowledge is not None else None
    except Exception:  # no index: every question is general, never a crash
        return None


def route_of(core: CoreService, text: str) -> dict[str, Any]:
    """Where the Home box's words go (UXS-67): `tutor`, `study` or `ask`."""
    return route_input(text, _index(core), material_min_chars=core.settings.ask_material_min_chars).view()


def unavailable(core: CoreService) -> str | None:
    cloud = getattr(core, "ask_cloud", None)
    return "needs_online_plan" if cloud is None else cloud.unavailable()


async def answer_question(core: CoreService, question: str, mode: str | None = None,
                          reach: str | None = None) -> dict[str, Any]:  # fmt: skip
    """The reply of `POST /v1/ask` for a question the input guard has passed, typed or spoken (VOC-53): routed again
    here, so homework is handed to the tutor with no answer; otherwise Ask anything under the profile's policy."""
    conn = getattr(core.store, "conn", None)
    found = route_of(core, question)
    if found["route"] == "tutor":
        return handoff(found)  # homework goes to the guided tutor; no answer is made here (PED-01)
    if found["route"] == "study":
        return {"route": "study", "reason": found["reason"], "answer": None}
    reason = unavailable(core)
    if reason is not None:
        return {"route": "ask", "state": "unavailable", "reason": reason, "answer": None}
    row = conn.execute("SELECT age_band, display_name, profile_id FROM profile").fetchone() if conn else None
    policy = policy_for(row[0] if row else None, mode, reach)
    config = source_config()
    own = own_evidence(conn, question)
    # The learner's excerpts go to the cloud only when the profile may send study material (STU-01, PRV-05).
    layer = core.online
    share = layer is not None and layer.study_effective(CURRENT_PROFILE.get(), conn)
    ask_id = new_id("ask")
    payload = request_body(ask_id, question, policy, own, share, config)
    try:
        reply = await core.ask_cloud.ask(payload, tuple(str(v) for v in (row[1:] if row else ()) if v))
        answer = compose(ask_id, question, policy, own, reply, config)
    except (AskError, ValueError) as error:
        # The transport (`net.ask.AskCloudError`, reached through `core.ask_cloud`) and compose both say why.
        reason = getattr(error, "reason", None)
        if not isinstance(reason, str):
            raise
        state = "unavailable" if reason in WAITING else "failed"
        return {"route": "ask", "state": state, "reason": reason, "answer": None}
    return {"route": "ask", "state": "answered", "answer": answer}


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    def learner() -> sqlite3.Connection | None:
        core._guard()
        if CURRENT_PROFILE.get() is None or CURRENT_PROFILE.get() != core.records_profile:
            raise ApiError("profile_token_expired")
        return getattr(core.store, "conn", None)

    @app.post("/v1/ask/route")
    async def route(request: Request) -> Response:
        learner()
        raw = await request.body()
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise ApiError("invalid_request", "body is not JSON") from None
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or len(text) > TEXT_MAX or set(body) != {"text"}:
            raise ApiError("validation_failed")
        found = route_of(core, text)
        reason = unavailable(core) if found["route"] == "ask" else None
        found["ask"] = {"available": reason is None, **({"reason": reason} if reason else {})}
        return JSONResponse(found)

    @app.post("/v1/ask")
    async def ask(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            if (
                not isinstance(body, dict)
                or not set(body) <= {"question", "mode", "reach"}
                or not isinstance(body.get("question"), str)
                or not body["question"].strip()
                or len(body["question"]) > QUESTION_MAX
                or body.get("mode", "teach") not in ("teach", "explain")
                or body.get("reach", "allow_list") not in ("allow_list", "wider_web")
            ):
                raise ApiError("validation_failed")
            learner()
            return 200, await answer_question(core, body["question"].strip(), body.get("mode"), body.get("reach"))

        return await mutating(request, run)
