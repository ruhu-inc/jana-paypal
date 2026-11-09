"""Student-authored local study notes and visible pack-content speech (STU-15, VOC-18).

Private notes (D102, the shared lab of JRN-11): the student may mark a note private. Profiles open with a tap (D86), so
this is a plain courtesy on a shared computer, not a lock: the list shows a private note without its text, a search
never matches inside it, and its text comes back only through `POST /v1/study/notes/{note_id}/open` with
`confirm: true`, the client's confirm step. `PATCH /v1/study/notes/{note_id}` marks or unmarks it. The mark is the
note's `study_notes.private` column (API 1.10, D104); the side file a core before it kept beside the learner database
(`study-notes-private.json`, note ids only) is moved into the column the first time the notes are read, then removed."""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CoreService, require
from agent_core.api.errors import ApiError
from agent_core.api.profiles import Mutating
from agent_core.contracts import errors
from agent_core.engine.events import new_id
from agent_core.engine.input import normalise_input
from agent_core.engine.safety import redact_personal
from agent_core.store.checkpoints import CheckpointStore
from agent_core.store.sqlite import transaction

if TYPE_CHECKING:
    from agent_core.api.voice_activities import VoiceActivities

LEGACY_PRIVATE_FILE = "study-notes-private.json"  # before API 1.10: the private notes' ids beside the database (D102)
COLUMNS = ("note_id", "text", "source", "created_at", "updated_at", "private")


class StudyNotes:
    def __init__(self, core: CoreService, activities: VoiceActivities) -> None:
        self.core, self.activities = core, activities
        self._moved: set[str] = set()  # the databases whose legacy marks file has been folded in this run

    def conn(self) -> sqlite3.Connection:
        self.core._guard()
        if not isinstance(self.core.store, CheckpointStore):
            raise ApiError("feature_disabled", "study notes require profile storage")
        conn = self.core.store.conn
        self._move_legacy_marks(conn)
        return conn

    def _move_legacy_marks(self, conn: sqlite3.Connection) -> None:
        """DAT-12, D104: the marks file of a core before API 1.10 becomes the `private` column, once, then goes. A note
        it names that no longer exists is skipped; an unreadable file marks nothing and is still removed, since it held
        ids only and a mark is the student's to set again."""
        found = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
        if not found or found in self._moved:
            return
        path = Path(found).parent / LEGACY_PRIVATE_FILE
        if path.is_file():
            try:
                ids = [str(i) for i in json.loads(path.read_text()).get("note_ids", [])]
            except (OSError, ValueError, AttributeError):
                ids = []
            with transaction(conn):
                conn.executemany("UPDATE study_notes SET private = 1 WHERE note_id = ?", [(i,) for i in ids])
            path.unlink(missing_ok=True)
        self._moved.add(found)

    def get(self, identity: str) -> dict[str, Any]:
        row = (
            self.conn()
            .execute(
                "SELECT note_id,text,source,created_at,updated_at,private FROM study_notes WHERE note_id=?", (identity,)
            )
            .fetchone()
        )
        if row is None:
            raise ApiError("not_found")
        note = dict(zip(COLUMNS, row, strict=True))
        # The study_note contract: `private` is present, as true, only on a note the student marked (D104).
        return {**note, "private": True} if note.pop("private") else note

    # --- private notes (D102, D104): marked by the student, opened through a confirm step ---------------------------

    @staticmethod
    def shown(note: dict[str, Any]) -> dict[str, Any]:
        """A note as the list shows it (StudyNoteListed): a private note without its text; any other note exactly as
        the `study_note` contract has it."""
        return {**note, "text": None} if note.get("private") else note

    def set_private(self, identity: str, private: bool) -> dict[str, Any]:
        self.get(identity)
        conn = self.conn()
        with transaction(conn):
            conn.execute("UPDATE study_notes SET private = ? WHERE note_id = ?", (int(private), identity))
        return self.shown(self.get(identity))

    def open(self, identity: str, confirmed: bool) -> dict[str, Any]:
        note = self.get(identity)
        if note.get("private") and not confirmed:
            raise ApiError("validation_failed", "a private note opens after the confirm step",
                           details={"reason": "confirm_needed"})  # fmt: skip
        return note

    def save(self, session_id: str, body: Any) -> dict[str, Any]:
        self.core.entry(session_id)
        if not isinstance(body, dict) or body.get("type") != "save_note" or errors("voice/activities", body):
            raise ApiError("validation_failed")
        text = redact_personal(normalise_input(body["text"]).strip())[0]
        if not text:
            raise ApiError("validation_failed")
        conn = self.conn()
        identity = body.get("capture_id")
        previous = None
        if identity:
            if identity not in self.activities.note_drafts:
                raise ApiError("not_found")
            owner, draft = self.activities.drafts[identity]
            previous = owner, copy.deepcopy(draft)
            self.activities.confirm(session_id, identity, text)
        now = self.core.clock_ms()
        note = {
            "note_id": new_id("nt"),
            "text": text,
            "source": "voice" if identity else "typed",
            "created_at": now,
            "updated_at": now,
        }
        try:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO study_notes (note_id, text, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    tuple(note.values()),
                )
        except sqlite3.Error:
            if identity and previous is not None:
                self.activities.drafts[identity] = previous
            raise
        if identity:
            self.activities.drafts.pop(identity, None)
            self.activities.note_drafts.discard(identity)
        return note

    def read_source(self, source: dict[str, Any], session_id: str) -> str:
        if source["kind"] == "session_card":
            entry = self.core.entry(session_id)
            part = entry.state["parts"].get(source["part_id"])
            message = next(
                (
                    m
                    for m in entry.state["messages"]
                    if m["role"] == "tutor" and source["part_id"] in m.get("part_ids", [])
                ),
                None,
            )
            if (
                part is None
                or part.get("kind") != "card"
                or part.get("status") != "complete"
                or message is None
                or message.get("status") == "streaming"
            ):
                raise ApiError("request_conflict", "only an accepted displayed card can be read aloud")
            card = part.get("card", {}).get("card", {})
            if card.get("card_type") == "question":
                return "\n".join(
                    [
                        card["prompt"],
                        *[
                            option["label"] + (r" \(" + option["latex"] + r"\)" if option.get("latex") else "")
                            for option in card["options"]
                        ],
                    ]
                )
            if card.get("card_type") == "hint":
                return str(card["body"])
            raise ApiError("not_found")
        if source["kind"] == "note":
            return str(self.get(source["note_id"])["text"])
        if self.core.learn is None:
            raise ApiError("not_found")
        with self.core.learn() as desk:
            lesson = desk.lesson(source["lesson_id"]) if desk is not None else None
        if lesson is None:
            raise ApiError("not_found")
        if lesson["version"] != source["version"]:
            raise ApiError("request_conflict", "reload this lesson before reading")
        index = source["block_index"]
        if index >= len(lesson["blocks"]):
            raise ApiError("not_found")
        block = lesson["blocks"][index]
        if source["kind"] == "lesson_question":
            item = next((item for item in block.get("items", []) if item["item_id"] == source["item_id"]), None)
            if item is None:
                raise ApiError("not_found")
            return question_text(item)
        return block_text(block)

    def add_routes(self, app: FastAPI, mutating: Mutating) -> None:
        @app.get("/v1/study/notes")
        async def listing(q: str = "") -> dict[str, Any]:
            if len(q) > 200:
                raise ApiError("validation_failed")
            # Bounded substring search: query is data, never SQL/FTS syntax; deletion has no secondary index to erase.
            # A search never matches inside a private note; the plain list still shows that it is there (D102).
            rows = (
                self.conn()
                .execute(
                    "SELECT note_id FROM study_notes WHERE instr(lower(text), lower(?)) > 0 AND (? = '' OR private = 0)"
                    " ORDER BY updated_at DESC,note_id DESC LIMIT 200",
                    (q, q),
                )
                .fetchall()
            )
            return {"notes": [self.shown(self.get(row[0])) for row in rows]}

        @app.patch("/v1/study/notes/{note_id}")
        async def privacy(note_id: str, request: Request) -> Response:
            self.conn()

            async def run(body: Any) -> tuple[int, Any]:
                return 200, self.set_private(note_id, require("StudyNoteMark", body)["private"])

            return await mutating(request, run)

        @app.post("/v1/study/notes/{note_id}/open")
        async def open_note(note_id: str, request: Request) -> dict[str, Any]:
            try:
                body = await request.json() if await request.body() else {}
            except ValueError:
                raise ApiError("validation_failed") from None
            return self.open(note_id, require("StudyNoteOpen", body).get("confirm") is True)

        @app.post("/v1/sessions/{session_id}/study/notes")
        async def save(session_id: str, request: Request) -> Response:
            self.core.entry(session_id)

            async def run(body: Any) -> tuple[int, Any]:
                return 200, self.save(session_id, body)

            return await mutating(request, run)

        @app.delete("/v1/study/notes/{note_id}")
        async def delete(note_id: str, request: Request) -> Response:
            self.conn()  # profile scope and guard precede the profile-namespaced idempotency lookup

            async def run(_: Any) -> tuple[int, Any]:
                self.get(note_id)
                self.conn().execute("DELETE FROM study_notes WHERE note_id=?", (note_id,))  # its mark goes with it
                return 204, None

            return await mutating(request, run)


def question_text(item: dict[str, Any]) -> str:
    pieces = [item["stem"]]
    for part in item.get("parts", []):
        pieces.append(part["prompt"])
        shown = part.get("input", {})
        pieces.extend(option["label"] for option in shown.get("options", shown.get("elements", [])))
        if shown.get("unit_prompt"):
            pieces.append(shown["unit_prompt"])
    return "\n".join(str(p) for p in pieces if p)


def block_text(block: dict[str, Any]) -> str:
    """Only fields rendered by LessonPlayer, never recursive extraction of arbitrary pack metadata or answer keys."""
    kind = block["type"]
    if "items" in block:
        return "\n".join(question_text(item) for item in block["items"])
    if kind in ("idea", "summary"):
        return str(block["text"])
    if kind == "visual":
        return str(block.get("caption") or "")
    if kind == "common_mistakes":
        return "\n".join([block.get("text") or "", *[m["name"] + ". " + m["fix"] for m in block["mistakes"]]])
    if kind == "worked_example":
        example = block["worked_example"]
        pieces = [example["problem"]]
        for step in example["steps"]:
            if step["n"] not in example["faded_steps"]:
                pieces.extend([r"\(" + step["latex"] + r"\)", step["reason"]])
        # The board shows the final answer even with faded steps; retain its displayed maths, no hidden steps.
        pieces.append(r"\(" + example["final_answer"]["latex"] + r"\)")
        return "\n".join(pieces)
    raise ApiError("not_found")
