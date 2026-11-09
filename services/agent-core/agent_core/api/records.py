"""Saved questions, problem reports and the student's notes over the local API (API-28, API-23, PRV-10, LRN-37).

All of them are scoped to the profile the request acts for and live only in its learner database. A saved question's
snapshot is built here from the named session: the problem statement and the board lines, never chat or photos and
never anything the client sends. Saved questions are local only: there is no send endpoint. Retention (DAT-08) and
profile deletion (DAT-09) remove them.

The notes: the list says who each note came from (the student's own request, or the Reflector), and a forget is
immediate, erases the text and tells every open session of the profile (MEM-11, MEM-14).

The knows page (G90): the learner view, answering a review item for exactly
the versions shown (MEM-09), "This isn't right" on a note or a misconception, and editing a preference (LRN-37). Each
is served only in the open profile with its token (D87, MEM-14, PRV-10), and answers with the view afterwards."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Literal

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CURRENT_PROFILE, CoreService
from agent_core.api.errors import ApiError, invalid
from agent_core.api.profiles import Mutating
from agent_core.engine.offers import HINT_OFFER_CODE
from agent_core.engine.tutor_flows import saved_question_board
from agent_core.store.consents import states as consent_states
from agent_core.store.events import iso_ms
from agent_core.store.ids import new_ulid
from agent_core.store.profiles import ProfileDirectory, ProfileError
from agent_core.store.sqlite import transaction

RECORDS = "learning/student-records#/$defs/"
SUBJECTS = {"mathematics", "physics", "chemistry", "biology", "accounting"}
Viewer = Literal["student"]
# The knows page's sharing flags and the consent scope each follows (LRN-36).
SHARING = {"backup": "cloud_backup", "devices": "multi_device", "parent_view": "parent_remote_view",
           "cloud_tutor": "cloud_tier", "tutor_memory": "tutor_memory_online"}  # fmt: skip
# The preference keys of the knows page (G90, LRN-37; `preference_key`); the values are checked by the memory layer.
PREFERENCE_KEYS = frozenset({"start_style", "representation", "pace", "unprompted_help", "self_explanation_prompts"})
_VERSION = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def _resolution(body: Any) -> dict[str, Any]:
    """A `ReviewResolution` body (G90, MEM-09), checked here: the answer and the two versions it was given for."""
    if not isinstance(body, dict) or set(body) != {"accept", "pending_version", "target_version"}:
        raise ApiError("validation_failed", "the body is {accept, pending_version, target_version}")
    accept, pending, target = body["accept"], body["pending_version"], body["target_version"]
    if not isinstance(accept, bool) or not isinstance(pending, str) or not _VERSION.match(pending):
        raise ApiError("validation_failed", "accept is a boolean and pending_version a version id")
    if target is not None and (not isinstance(target, str) or not _VERSION.match(target)):
        raise ApiError("validation_failed", "target_version is a version id or null")
    return {"accept": accept, "pending_version": pending, "target_version": target}


def _valid(definition: str, body: Any) -> dict[str, Any]:
    refused = invalid(RECORDS + definition, body)  # value-free: no message ever quotes the body
    if refused is not None:
        raise refused
    assert isinstance(body, dict)
    return body


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    def conn() -> Any:
        """The learner database of the profile the request acts for (the core keeps exactly that one open)."""
        store = core.store
        if not hasattr(store, "conn"):
            raise ApiError("not_found", "student records need the learner database")
        return store.conn

    def days(key: str) -> int:
        return int(getattr(core.settings, key) * 86_400_000)

    def entry_of(session_id: str) -> Any:
        return core.entry(session_id)  # another profile's session answers 404 (API-04)

    # --- saved questions ------------------------------------------------------------------------------------------

    def saved(row: Any) -> dict[str, Any]:
        content = json.loads(row[2])
        return {"question_id": row[0], "session_id": f"ses_{row[1]}", "snapshot": content, "status": row[3],
                "expires_at": iso_ms(row[5])}  # fmt: skip

    @app.post("/v1/saved-questions")
    async def save_question(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = _valid("CreateSavedQuestion", body)
            entry = entry_of(asked["session_id"])
            turns = {m.get("turn_id") for m in entry.state["messages"]}
            if asked["turn_id"] not in turns:
                raise ApiError("not_found", "no such turn in that session")
            parts = entry.state.get("parts", {})

            def offer_only(message: dict[str, Any]) -> bool:
                """An engine-written turn holding only the quiet hint offer (D112): it is not a reply, so the reply
                above it can still be saved."""
                return not message.get("part_ids") and any(
                    p.get("kind") == "notice" and p.get("turn_id") == message.get("turn_id")
                    and (p.get("notice") or {}).get("code") == HINT_OFFER_CODE for p in parts.values()
                )  # fmt: skip

            latest = next((m for m in reversed(entry.state["messages"])
                           if m.get("role") == "tutor" and not offer_only(m)), None)  # fmt: skip
            if (
                entry.active_turn is not None
                or latest is None
                or latest.get("turn_id") != asked["turn_id"]
                or latest.get("status") != "complete"
            ):
                raise ApiError("request_conflict", "only the latest completed question can be saved")
            if entry.engine_state.problem is None:
                raise ApiError("validation_failed", "there is no learning problem to save")
            now = core.clock_ms()
            question_id = new_ulid(now)
            board = saved_question_board(entry.engine_state)
            subject = entry.meta.get("subject")
            problem = entry.engine_state.problem.statement if entry.engine_state.problem else ""
            snapshot = {"kind": "saved_question", "question_id": question_id,
                        "subject": subject if subject in SUBJECTS else "physics",
                        "topic_id": entry.meta.get("topic_id"), "problem": problem[:8000], "board": board,
                        "reason": "student_requested", "created_at": iso_ms(now)}  # fmt: skip
            expires = now + days("retention_saved_content_days")
            c = conn()
            with transaction(c):
                c.execute("INSERT INTO saved_questions (question_id, session_id, content, status, created_at,"
                          " expires_at) VALUES (?, ?, ?, 'kept', ?, ?)",
                          (question_id, asked["session_id"][4:], json.dumps(snapshot), now, expires))  # fmt: skip
            row = c.execute("SELECT * FROM saved_questions WHERE question_id = ?", (question_id,)).fetchone()
            return 200, saved(row)

        return await mutating(request, run)

    @app.get("/v1/saved-questions")
    async def list_saved() -> list[dict[str, Any]]:
        rows = conn().execute("SELECT * FROM saved_questions ORDER BY created_at").fetchall()
        return [saved(r) for r in rows]

    @app.patch("/v1/saved-questions/{record_id}")
    async def sort_saved(record_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            status = _valid("UpdateSavedQuestion", body)["status"]
            c = conn()
            with transaction(c):
                changed = c.execute("UPDATE saved_questions SET status = ? WHERE question_id = ?",
                                    (status, record_id)).rowcount  # fmt: skip
            if not changed:
                raise ApiError("not_found", "no such saved question")
            return 200, saved(c.execute("SELECT * FROM saved_questions WHERE question_id = ?", (record_id,)).fetchone())

        return await mutating(request, run)

    @app.delete("/v1/saved-questions/{record_id}")
    async def remove_saved(record_id: str, request: Request) -> Response:
        return await mutating(request, delete("saved_questions", "question_id", record_id))

    # --- problem reports --------------------------------------------------------------------------------------------

    def report(row: Any) -> dict[str, Any]:
        return {**json.loads(row[2]), "report_id": row[0], "created_at": iso_ms(row[3]), "expires_at": iso_ms(row[4])}

    @app.post("/v1/problem-reports")
    async def create_report(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = _valid("CreateProblemReport", body)
            entry = entry_of(asked["session_id"])
            turns = {m.get("turn_id") for m in entry.state["messages"]}
            if set(asked["turn_ids"]) - turns:
                raise ApiError("not_found", "no such turn in that session")
            now = core.clock_ms()
            report_id = new_ulid(now)
            content = {"session_id": asked["session_id"], "turn_ids": asked["turn_ids"], "category": asked["category"],
                       "note": asked["note"], "allow_support_export": asked["allow_support_export"]}  # fmt: skip
            c = conn()
            with transaction(c):
                c.execute("INSERT INTO problem_reports (report_id, session_id, content, created_at, expires_at)"
                          " VALUES (?, ?, ?, ?, ?)", (report_id, asked["session_id"][4:], json.dumps(content), now,
                                                     now + days("retention_saved_content_days")))  # fmt: skip
            return 200, report(c.execute("SELECT * FROM problem_reports WHERE report_id = ?", (report_id,)).fetchone())

        return await mutating(request, run)

    @app.get("/v1/problem-reports")
    async def list_reports() -> list[dict[str, Any]]:
        return [report(r) for r in conn().execute("SELECT * FROM problem_reports ORDER BY created_at").fetchall()]

    @app.delete("/v1/problem-reports/{record_id}")
    async def remove_report(record_id: str, request: Request) -> Response:
        return await mutating(request, delete("problem_reports", "report_id", record_id))

    # --- the student's notes (PRV-10 correction and deletion; LRN-37) ---------------------------------------------

    @app.get("/v1/profiles/{profile_id}/memory")
    async def memory(profile_id: str) -> dict[str, Any]:
        if CURRENT_PROFILE.get() != profile_id:
            raise ApiError("not_found", "no such profile")
        conn()  # the learner database must be open
        records = core.records
        return {"notes": records.memory_notes() if records is not None else []}

    @app.delete("/v1/memory/{note_id}")
    async def forget_note(note_id: str, request: Request) -> Response:
        async def run(_: Any) -> tuple[int, Any]:
            if core.records is None or core.records.learner is None:
                raise ApiError("not_found", "no such note")
            # A deletion is an event plus erasure of the note's text everywhere it was stored (LRN-37, MEM-14).
            if not core.records.forget_note(note_id.removeprefix("mem_"), "student"):
                raise ApiError("not_found", "no such note")
            # Open sessions drop it from their frozen snapshot and their streams say so (MEM-11, MEM-14, UXS-28).
            core.memory_forgotten(note_id.removeprefix("mem_"))
            return 204, None

        return await mutating(request, run)

    # --- the knows page: the learner view, review items and corrections (G90; MEM-09, MEM-14, LRN-36, LRN-37) -------

    def viewer(profile_id: str, request: Request) -> Viewer:
        """Only the open profile's token can reach its learner view and corrections (D87, API-04)."""
        if CURRENT_PROFILE.get() == profile_id and profile_id is not None:
            return "student"
        raise ApiError("not_found", "no such profile")

    def sharing(profile_id: str) -> dict[str, bool] | None:
        """The page's sharing sentence inputs: each consent in effect (granted, and its online feature on)."""
        directory = core.directory
        if not isinstance(directory, ProfileDirectory):
            return None
        on = {r[0] for r in directory.device.execute("SELECT feature FROM online_features WHERE enabled = 1")}
        try:
            with directory.open_learner(profile_id) as learner:
                effective = {s["scope"] for s in consent_states(learner, on) if s["effective"]}
        except ProfileError:
            return None
        return {flag: scope in effective for flag, scope in SHARING.items()}

    def knows(profile_id: str, request: Request, act: Callable[[Any, Viewer], None] | None = None) -> dict[str, Any]:
        """Run one correction (if any) on the profile's desk, then answer with the view as it is now."""
        who = viewer(profile_id, request)
        if core.knows is None:
            raise ApiError("not_found", "no such profile")
        with core.knows(profile_id) as desk:
            if desk is None:
                raise ApiError("not_found", "no such profile")
            if act is not None:
                act(desk, who)
            view = desk.view()
        return {"profile_id": profile_id, **view, "sharing": sharing(profile_id)}

    def forgotten(profile_id: str, note_ids: list[str]) -> None:
        """Open sessions of the open profile drop deleted notes and say so (MEM-11, MEM-14); a locked profile has
        none open."""
        if profile_id == core.records_profile:
            for note_id in note_ids:
                core.memory_forgotten(note_id)
            core.memory_changed()

    @app.get("/v1/profiles/{profile_id}/knows")
    async def knows_view(profile_id: str, request: Request) -> dict[str, Any]:
        return knows(profile_id, request)

    @app.post("/v1/profiles/{profile_id}/memory/reviews/{review_id}")
    async def resolve_review(profile_id: str, review_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            viewer(profile_id, request)  # another profile's page is 404 before anything else is checked
            answer = _resolution(body)
            gone: list[str] = []

            def act(desk: Any, who: Viewer) -> None:
                outcome = desk.resolve(review_id, actor=who, **answer)
                if outcome is None:
                    raise ApiError("not_found", "no such review item")
                if not isinstance(outcome, list):  # the item moved on since it was shown: shown again (MEM-09)
                    raise ApiError("review_changed", "the review item changed since it was shown",
                                   details={"review": outcome.current})  # fmt: skip
                gone.extend(outcome)

            view = knows(profile_id, request, act)
            forgotten(profile_id, gone)
            return 200, view

        return await mutating(request, run)

    @app.post("/v1/profiles/{profile_id}/memory/{note_id}/dispute")
    async def dispute_note(profile_id: str, note_id: str, request: Request) -> Response:
        async def run(_: Any) -> tuple[int, Any]:
            gone: list[str] = []

            def act(desk: Any, who: Viewer) -> None:
                deleted = desk.dispute_note(note_id, who)
                if deleted is None:
                    raise ApiError("not_found", "no such note")
                gone.append(deleted)

            view = knows(profile_id, request, act)
            forgotten(profile_id, gone)
            return 200, view

        return await mutating(request, run)

    @app.post("/v1/profiles/{profile_id}/misconceptions/{misconception_id}/correction")
    async def correct_misconception(profile_id: str, misconception_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            viewer(profile_id, request)
            if not isinstance(body, dict) or set(body) != {"action"} or body["action"] not in ("dispute", "undispute"):
                raise ApiError("validation_failed", "action must be dispute or undispute")

            def act(desk: Any, who: Viewer) -> None:
                if not desk.correct_misconception(misconception_id, body["action"], who):
                    raise ApiError("not_found", "no such misconception to correct")

            return 200, knows(profile_id, request, act)

        return await mutating(request, run)

    @app.patch("/v1/profiles/{profile_id}/preferences/{key}")
    async def correct_preference(profile_id: str, key: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            viewer(profile_id, request)
            if not isinstance(body, dict) or set(body) != {"value"}:
                raise ApiError("validation_failed", "the body is {value}")
            if key not in PREFERENCE_KEYS:
                raise ApiError("not_found", "no such preference")

            def act(desk: Any, who: Viewer) -> None:
                if not desk.correct_preference(key, body["value"], who):
                    raise ApiError("validation_failed", f"not a value {key} can take")

            view = knows(profile_id, request, act)
            if profile_id == core.records_profile:
                core.memory_changed()
            return 200, view

        return await mutating(request, run)

    def delete(table: str, key: str, record_id: str) -> Callable[[Any], Any]:
        async def run(_: Any) -> tuple[int, Any]:
            c = conn()
            with transaction(c):
                gone = c.execute(f"DELETE FROM {table} WHERE {key} = ?", (record_id,)).rowcount  # noqa: S608
            if not gone:
                raise ApiError("not_found", "no such record")
            return 204, None

        return run
