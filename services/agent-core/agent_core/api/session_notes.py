"""The student's local session-note projections (STU-14, UXS-52); no remote or memory-write path."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CURRENT_PROFILE, CoreService
from agent_core.api.errors import ApiError
from agent_core.api.profiles import Mutating
from agent_core.engine.session_notes import COLUMNS, note_of
from agent_core.store.sqlite import transaction


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    @app.post("/v1/sessions/{session_id}/end")
    async def end_session(session_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            if body != {}:
                raise ApiError("validation_failed")
            core.assessment_guard()
            entry = core.entry(session_id)
            if (entry.task is not None and not entry.task.done()) or session_id in core.voice_engines:
                raise ApiError("turn_active")
            if core.records is None:
                raise ApiError("not_found")
            core.records.end(session_id, None, core.clock_ms())
            core.session_notes.scan()
            return 200, {"session_id": session_id, "state": "ended"}

        return await mutating(request, run)

    def conn() -> Any:
        core._guard()
        if CURRENT_PROFILE.get() != core.records_profile or core.records_profile is None:
            raise ApiError("profile_token_expired")
        if core.mock_active is not None and core.mock_active():
            raise ApiError("tutor_locked", "notes wait until the mock is submitted")
        value = core.session_notes.connection()
        if value is None:
            raise ApiError("not_found", "session notes need an open profile")
        return value

    def get(identity: str, opened: bool = False) -> dict[str, Any]:
        row = (
            conn()
            .execute(f"SELECT {COLUMNS} FROM session_notes WHERE note_id=? AND state<>'deleted'", (identity,))  # noqa: S608 - fixed columns
            .fetchone()
        )
        if row is None:
            raise ApiError("not_found")
        return note_of(row, opened=opened)

    @app.get("/v1/session-notes")
    async def list_notes(q: str = "") -> list[dict[str, Any]]:
        if len(q) > 200:
            raise ApiError("validation_failed")
        db = conn()
        core.session_notes.scan()
        notes = [
            note_of(row)
            for row in db.execute(
                f"SELECT {COLUMNS} FROM session_notes WHERE state<>'deleted' ORDER BY updated_at DESC,note_id DESC"  # noqa: S608 - fixed columns
            )
        ]
        query = q.casefold()
        return [
            n
            for n in notes
            if not query
            or (
                not n["private"]
                and query
                in " ".join([n["title"] or "", *n["kc_ids"], *(s["text"] for s in n["statements"])]).casefold()
            )
        ]

    @app.post("/v1/session-notes/{note_id}/open")
    async def open_note(note_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            if not isinstance(body, dict) or set(body) != {"confirm"} or type(body["confirm"]) is not bool:
                raise ApiError("validation_failed")
            if get(note_id)["private"] and body["confirm"] is not True:
                raise ApiError("validation_failed", "confirm before opening this private note")
            return 200, get(note_id, opened=True)

        return await mutating(request, run)

    @app.patch("/v1/session-notes/{note_id}")
    async def mark_note(note_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            if not isinstance(body, dict) or set(body) != {"private"} or type(body["private"]) is not bool:
                raise ApiError("validation_failed")
            get(note_id)
            conn().execute("UPDATE session_notes SET private=? WHERE note_id=?", (int(body["private"]), note_id))
            return 200, get(note_id)

        return await mutating(request, run)

    @app.post("/v1/session-notes/{note_id}/retry")
    async def retry(note_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            if body != {}:
                raise ApiError("validation_failed")
            get(note_id)
            conn().execute(
                "UPDATE session_notes SET state='queued' WHERE note_id=? AND state IN ('failed','no_model')", (note_id,)
            )
            return 200, get(note_id)

        return await mutating(request, run)

    @app.delete("/v1/session-notes/{note_id}")
    async def remove(note_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            get(note_id)
            with transaction(conn()):
                conn().execute(
                    "UPDATE session_notes SET state='deleted',title='',subject=NULL,kc_ids='[]',"
                    "statements='[]',private=0,withheld=0 WHERE note_id=?",
                    (note_id,),
                )
            return 204, None

        return await mutating(request, run)
