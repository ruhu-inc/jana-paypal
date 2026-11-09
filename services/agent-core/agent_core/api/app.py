"""The local HTTP API (API-01, API-23): S1 session, turn and cancel commands, the snapshot and the session event stream.

Every route sits behind `LoopbackGuard` (token, Host, Origin). Mutating commands carry `Idempotency-Key` (API-31);
every non-2xx body is the error envelope (API-32). Endpoints of later stages answer 404 `not_found` until their
milestone. No interactive docs or schema routes are served."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent_core.api import ask as ask_routes
from agent_core.api import assessment as assessment_routes
from agent_core.api import captures as capture_routes
from agent_core.api import companion as companion_routes
from agent_core.api import connectivity as connectivity_routes
from agent_core.api import drafts as draft_routes
from agent_core.api import exports as export_routes
from agent_core.api import interview as interview_routes
from agent_core.api import journal as journal_routes
from agent_core.api import learn as learn_routes
from agent_core.api import learning_journey as journey_routes
from agent_core.api import licence as licence_routes
from agent_core.api import operations as operations_routes
from agent_core.api import packs as pack_routes
from agent_core.api import parents as parent_routes
from agent_core.api import plans as plan_routes
from agent_core.api import profiles as profile_routes
from agent_core.api import records as record_routes
from agent_core.api import screening, voice_activities, voice_adapter
from agent_core.api import session_notes as session_note_routes
from agent_core.api import setup as setup_routes
from agent_core.api import study as study_routes
from agent_core.api import support as support_routes
from agent_core.api import updates as update_routes
from agent_core.api import voice as voice_routes
from agent_core.api.core import CURRENT_PROFILE, CoreService
from agent_core.api.errors import ApiError, envelope
from agent_core.api.security import APP_ORIGINS, LaunchSecret, LoopbackGuard

MAX_BODY_BYTES = 1024 * 1024


def route_key(request: Request) -> str:
    """`METHOD /path/{param}` of the route that matched, as `screening.ROUTES` names it."""
    route = request.scope.get("route")
    return f"{request.method} {getattr(route, 'path', request.url.path)}"


def create_app(core: CoreService, secret: LaunchSecret, port: Callable[[], int | None],
               origins: frozenset[str] = APP_ORIGINS, *, voice: voice_routes.VoiceRuntime | None = None,
               ) -> LoopbackGuard:  # fmt: skip
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, error: ApiError) -> JSONResponse:
        headers = {"retry-after": str(max(1, error.retry_after_ms // 1000))} if error.retry_after_ms else None
        return JSONResponse(error.envelope(), status_code=error.status, headers=headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, error: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if error.status_code in (404, 405) else "invalid_request"
        return JSONResponse(envelope(code, code.replace("_", " ")), status_code=404 if code == "not_found" else 400)

    @app.exception_handler(Exception)
    async def _crash(_: Request, error: Exception) -> JSONResponse:
        return JSONResponse(envelope("internal_error", "internal error"), status_code=500)

    async def body_of(request: Request) -> Any:
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise ApiError("payload_too_large", "request body too large")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError("invalid_request", "body is not JSON") from None

    async def mutating(request: Request, run: Callable[[Any], Awaitable[tuple[int, Any]]]) -> Response:
        """Run a mutating command once per `Idempotency-Key`; a repeat returns the stored response (API-31)."""
        key = request.headers.get("idempotency-key")
        if not key or not key.startswith("cli_") or len(key) > 64:
            raise ApiError("invalid_request", "Idempotency-Key (a cli_ id) is required")
        body = await body_of(request)
        core.limit_command(request.url.path)
        digest = core.idempotency.digest(request.method, request.url.path, body)
        key = f"{CURRENT_PROFILE.get()}:{key}"
        cached = core.idempotency.lookup(key, digest)
        if cached is None:
            core._guard(allow_recovery=request.url.path == "/v1/updates/rollback")
            # The input guard on free text outside a tutor turn, before the command touches anything (PRV-13).
            screening.screen(core, route_key(request), request.path_params, body)
            status, payload = await run(body)
            core.idempotency.store(key, digest, status, payload)
        else:
            status, payload = cached
        if payload is None:
            return Response(status_code=status)
        return JSONResponse(payload, status_code=status)

    @app.get("/v1/meta")
    async def meta() -> dict[str, Any]:
        return core.meta()

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        return core.health()

    @app.post("/v1/sessions")
    async def create_session(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            return 200, core.create_session(body)

        return await mutating(request, run)

    @app.get("/v1/sessions")
    async def list_sessions(limit: int = 20, before: str | None = None) -> dict[str, Any]:
        return core.list_sessions(limit, before)

    @app.get("/v1/sessions/{session_id}/tutoring-usage")
    async def tutoring_usage(session_id: str) -> Response:
        usage = await core.tutoring_usage(session_id)
        period = f"{CURRENT_PROFILE.get()}:{usage.until_ms}"
        return JSONResponse({
            "scope": usage.scope, "remaining": usage.remaining, "until_ms": usage.until_ms,
            "period_ends": usage.period_ends, "device_remaining": usage.device_remaining,
            "warning_window": hashlib.sha256(period.encode()).hexdigest() if usage.until_ms is not None else None,
        }, headers={"Cache-Control": "no-store"})

    @app.get("/v1/sessions/{session_id}/learning-state")
    async def learning_state(session_id: str) -> Response:
        return JSONResponse(core.learning_state(session_id), headers={"Cache-Control": "no-store"})

    @app.get("/v1/sessions/{session_id}/snapshot")
    async def snapshot(session_id: str) -> dict[str, Any]:
        return core.snapshot(session_id)

    @app.patch("/v1/sessions/{session_id}")
    async def update_session(session_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            return 200, core.update_session(session_id, body)

        return await mutating(request, run)

    @app.post("/v1/sessions/{session_id}/archive")
    async def archive(session_id: str, request: Request) -> Response:
        async def run(_: Any) -> tuple[int, Any]:
            core.archive(session_id)
            return 204, None

        return await mutating(request, run)

    @app.post("/v1/sessions/{session_id}/turns")
    async def start_turn(session_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            core.limit_turn_start()
            await core.preflight_input(session_id, body)
            return 202, core.start_turn(session_id, body)

        return await mutating(request, run)

    @app.post("/v1/turns/{turn_id}/cancel")
    async def cancel(turn_id: str, request: Request) -> Response:
        async def run(_: Any) -> tuple[int, Any]:
            return 200, await core.cancel(turn_id)

        return await mutating(request, run)

    @app.post("/v1/turns/{turn_id}/steer")
    async def steer(turn_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            return 200, core.steer(turn_id, body)

        return await mutating(request, run)

    @app.post("/v1/sessions/{session_id}/side-questions")
    async def side_question(session_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            core.limit_turn_start()
            return 202, core.side_question(session_id, body)

        return await mutating(request, run)

    @app.post("/v1/sessions/{session_id}/idle")
    async def step_idle(session_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            core.step_idle(session_id, body)
            return 204, None  # LRN-20, D104: an offer, if any, arrives on the session's stream

        return await mutating(request, run)

    @app.post("/v1/sessions/{session_id}/visuals/{visual_id}/prediction")
    async def sim_prediction(session_id: str, visual_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            core.sim_prediction(session_id, visual_id, body)
            return 204, None  # VSL-47: the tutor's turn arrives on the session's stream

        return await mutating(request, run)

    @app.post("/v1/cards/{request_id}/answer")
    async def answer_card(request_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            return 200, core.answer_card(request_id, body)

        return await mutating(request, run)

    @app.delete("/v1/sketches/{sketch_id}")
    async def delete_sketch(sketch_id: str, request: Request) -> Response:
        async def run(_: Any) -> tuple[int, Any]:
            core.delete_sketch(sketch_id)  # VSL-43: gone from the board, the history and replay
            return 204, None

        return await mutating(request, run)

    @app.get("/v1/boards/{board_id}/replay")
    async def board_replay(board_id: str) -> dict[str, Any]:
        return core.board_replay(board_id)  # UXS-59: replayed from recorded events, no model and no audio

    @app.put("/v1/sessions/{session_id}/selection")
    async def set_selection(session_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            core.set_selection(session_id, body)
            return 204, None  # "this" now refers to the selection (UXS-58)

        return await mutating(request, run)

    @app.post("/v1/visuals/{visual_id}/practice")
    async def start_manipulation(visual_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            return 200, core.start_manipulation(visual_id, body)

        return await mutating(request, run)

    @app.post("/v1/visuals/{visual_id}/answer")
    async def answer_manipulation(visual_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            return 200, await core.answer_manipulation(visual_id, body)

        return await mutating(request, run)

    @app.get("/v1/packs")
    async def packs() -> dict[str, Any]:
        """Installed packs, each verified when listed: a damaged one shows `signature_ok` false (API-29 subset)."""
        return {"packs": core.packs() if core.packs is not None else []}

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        """The verified models, the one the runtime serves and the hardware class with why, with the installed and
        downloadable model packs and their licences once the pack desk is attached (API-29, PLT-10, UXS-11)."""
        catalogue = core.model_catalogue()
        return await asyncio.to_thread(core.pack_desk.catalogue, catalogue) if core.pack_desk is not None else catalogue

    operations_routes.add_routes(app, core, mutating)
    profile_routes.add_routes(app, core, mutating)
    pack_routes.add_routes(app, core, mutating)
    update_routes.add_routes(app, core, mutating)
    export_routes.add_routes(app, core, mutating)
    profile_routes.add_consent_routes(app, core, mutating)
    connectivity_routes.add_routes(app, core, mutating)
    record_routes.add_routes(app, core, mutating)
    session_note_routes.add_routes(app, core, mutating)
    study_routes.add_routes(app, core)
    ask_routes.add_routes(app, core, mutating)  # the one Home input and Ask anything (UXS-67, STU-20)
    learn_routes.add_routes(app, core, mutating)
    assessment_routes.add_routes(app, core, mutating)
    journey_routes.add_routes(app, core, mutating)
    assessment_routes.add_past_paper_routes(app, core, mutating)  # imported past papers (ASM-27)
    plan_routes.add_routes(app, core, mutating)  # plans and progress (LRN-28 to LRN-33, UXS-12, UXS-27)
    companion_routes.add_routes(app, core, mutating)  # the Last time card (SPEC §19.5.2, W8 phase 1)
    parent_routes.add_routes(app, core, mutating)  # parent summary, alerts, session history (TCH-12, TCH-14, UXS-48)
    draft_routes.add_routes(app, core, mutating)
    journal_routes.add_routes(app, core, mutating)
    licence_routes.add_routes(app, core, mutating)  # the school licence file (PLT-19)
    setup_routes.add_routes(app, core, mutating)  # where first run stands (PLT-17, UXS-10)
    interview_routes.add_routes(app, core, mutating)  # the hand-off's first-session interview (UXS-10, JRN-03)
    support_routes.add_routes(app, core, mutating)  # crash reports and the support share (OBS-17, OBS-18, D85)
    core.capture_desk = capture_routes.add_routes(app, core, mutating)
    voice_activities.add_routes(app, core, voice, mutating)
    if voice is not None:
        voice_routes.add_routes(app, voice)
        if voice.adapter is not None:
            voice_adapter.add_routes(app, voice.adapter)

    @app.get("/v1/sessions/{session_id}/events")
    async def events(session_id: str, request: Request, cursor: str | None = None) -> StreamingResponse:
        connection, replayed, resumed = core.open_stream(session_id, request.headers.get("last-event-id") or cursor)
        return StreamingResponse(
            core.stream(session_id, connection, replayed, resumed),
            media_type="text/event-stream",
            headers={"cache-control": "no-store", "x-accel-buffering": "no"},
        )

    adapter_secret = voice.adapter.secret if voice is not None and voice.adapter is not None else None
    return LoopbackGuard(profile_routes.ProfileScope(app, core), secret, port, origins, adapter_secret=adapter_secret)
