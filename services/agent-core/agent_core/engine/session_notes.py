"""Extractive, referenced session notes (STU-14). The local model selects and groups accepted evidence; it cannot
supply new prose. Only exact, screened excerpts are materialised. Missing/invalid references are counted, never shown.
This projection is independent of memory saving and never writes learning events or changes mastery.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
from collections.abc import Callable
from typing import Any

from jsonschema import Draft202012Validator

from agent_core.engine.events import new_id
from agent_core.engine.safety import redact_personal
from agent_core.memory.capture import CaptureFilter
from agent_core.store.sqlite import transaction

log = logging.getLogger(__name__)
SECTIONS = ("worked_on", "method", "corrections", "revise")
SELECTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["statements"],
    "properties": {
        "statements": {
            "type": "array",
            "maxItems": 24,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["evidence", "section"],
                "properties": {"evidence": {"type": "integer", "minimum": 0}, "section": {"enum": list(SECTIONS)}},
            },
        }
    },
}
SELECTION_VALIDATOR = Draft202012Validator(SELECTION_SCHEMA)
COLUMNS = (
    "note_id,session_id,title,subject,kc_ids,statements,state,private,source_revision,withheld,created_at,updated_at"
)


def evidence(conn: sqlite3.Connection, session_id: str, capture: CaptureFilter | None) -> list[dict[str, Any]]:
    """Only terminal accepted tutor parts, with source turn and trace ids. A rejected/speculative candidate is never a
    transcript part. Interrupted/error/retracted content and safety sessions never enter the model's task."""
    if (
        capture is None
        or conn.execute(
            "SELECT 1 FROM events WHERE session_id=? AND type='safety_flagged' LIMIT 1", (session_id,)
        ).fetchone()
    ):
        return []
    rows = conn.execute(
        "SELECT t.turn_id,t.trace_id,p.kind,p.content FROM parts p JOIN turns t ON t.turn_id=p.turn_id"
        " WHERE t.session_id=? AND t.speaker='tutor' AND t.status='complete' AND p.status='complete'"
        " ORDER BY t.idx,p.idx",
        (session_id,),
    ).fetchall()
    found = []
    for turn_id, trace_id, kind, body in rows:
        part = json.loads(body)
        excerpts: list[tuple[str | None, str]] = []
        if kind == "text":
            excerpts = [(None, str(part.get("text", "")))]
        elif kind == "card":
            card = part.get("card", {}).get("card", {})
            if card.get("card_type") == "reflection":
                excerpts = [
                    ("method", str(card.get("did", ""))),
                    ("corrections", str(card.get("right", ""))),
                    ("revise", str(card.get("next_goal", ""))),
                ]
        for section, text in excerpts:
            for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
                sentence = redact_personal(sentence.strip())[0]
                if (
                    not sentence
                    or len(sentence) > 400
                    or re.search(r"\b(mastered|secure|mastery|ability)\b", sentence, re.I)
                ):
                    continue
                if capture.check(sentence, instructions=False).blocked:
                    continue
                found.append(
                    {
                        "text": sentence,
                        "section": section,
                        "reference": {"session_id": f"ses_{session_id}", "turn_id": turn_id, "trace_id": trace_id},
                    }
                )
    return found[-48:]


def materialise(output: Any, sources: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """References are engine-owned, and text must come from one of those references. Unknown prose is withheld."""
    shown: list[dict[str, Any]] = []
    withheld = 0
    raw = output.get("statements", []) if isinstance(output, dict) else []
    if not isinstance(raw, list):
        return [], 1
    seen = set()
    for statement in raw[:48]:
        if not isinstance(statement, dict) or set(statement) != {"evidence", "section"}:
            withheld += 1
            continue
        index, section = statement["evidence"], statement["section"]
        if type(index) is not int or not 0 <= index < len(sources) or section not in SECTIONS:
            withheld += 1
            continue
        source = sources[index]
        if not source.get("reference", {}).get("turn_id") or source.get("section") not in (None, section):
            withheld += 1
            continue
        if index in seen:
            continue
        seen.add(index)
        shown.append({"section": section, "text": source["text"], "reference": source["reference"]})
    return shown[:24], withheld


def note_of(row: Any, *, opened: bool = False) -> dict[str, Any]:
    note = dict(zip(COLUMNS.split(","), row, strict=True))
    note["kind"] = "session"
    note["private"] = bool(note["private"])
    note["session_id"] = "ses_" + note["session_id"]
    note["kc_ids"] = json.loads(note["kc_ids"])
    note["statements"] = json.loads(note["statements"])
    # Defense at the display boundary too: damaged/imported records cannot expose unreferenced statements.
    original = len(note["statements"])
    note["statements"] = [
        s
        for s in note["statements"]
        if isinstance(s, dict)
        and s.get("section") in SECTIONS
        and isinstance(s.get("text"), str)
        and isinstance(s.get("reference"), dict)
        and re.fullmatch(r"trn_[0-9A-HJKMNP-TV-Z]{26}", str(s["reference"].get("turn_id")))
        and s["reference"].get("session_id") == note["session_id"]
    ]
    if original > len(note["statements"]):
        log.info("session note statements withheld at display: %d", original - len(note["statements"]))
    if note["private"] and not opened:
        note.update(title=None, kc_ids=[], statements=[])
    return note


class SessionNotesQueue:
    def __init__(
        self,
        connection: Callable[[], sqlite3.Connection | None],
        model: Callable[[], Any],
        capture: Callable[[], CaptureFilter | None],
        busy: Callable[[], bool],
        clock: Callable[[], int],
    ):
        self.connection, self.model, self.capture, self.busy, self.clock = connection, model, capture, busy, clock
        self.task: asyncio.Task[Any] | None = None
        self.owner: sqlite3.Connection | None = None

    def scan(self) -> None:
        conn = self.connection()
        if conn is None:
            return
        # Ended sessions are durable; this also recovers work interrupted by shutdown or a power cut.
        rows = conn.execute(
            "SELECT s.session_id,s.title,s.subject,max(t.ended_at)*1000+count(*) FROM sessions s JOIN turns t"
            " ON t.session_id=s.session_id WHERE s.ended_at IS NOT NULL AND t.speaker='tutor'"
            " AND t.status='complete' AND NOT EXISTS (SELECT 1 FROM events e WHERE e.session_id=s.session_id"
            " AND e.type='safety_flagged') GROUP BY s.session_id ORDER BY s.ended_at DESC LIMIT 100"
        ).fetchall()
        with transaction(conn):
            for sid, title, subject, revision in rows:
                now = self.clock()
                conn.execute(
                    "INSERT INTO session_notes (note_id,session_id,title,subject,state,source_revision,"
                    "created_at,updated_at)"
                    " VALUES (?,?,?,?,'queued',?,?,?) ON CONFLICT(session_id) DO UPDATE SET"
                    " state='queued',source_revision=excluded.source_revision,updated_at=excluded.updated_at"
                    " WHERE session_notes.state<>'deleted' AND session_notes.source_revision<excluded.source_revision",
                    (
                        new_id("snt"),
                        sid,
                        redact_personal(title or subject or "")[0],
                        subject,
                        int(revision or 0),
                        now,
                        now,
                    ),
                )

    def poll(self) -> None:
        conn = self.connection()
        if self.task is not None and not self.task.done():
            if self.owner is not conn or self.busy():
                self.pause()
            return
        self.scan()
        if conn is None or self.busy():
            return
        row = conn.execute(
            "SELECT note_id,session_id FROM session_notes WHERE state IN"
            " ('queued','writing','no_model') ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            self.owner = conn
            self.task = asyncio.create_task(self.generate(conn, str(row[0]), str(row[1])))

    def pause(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()

    async def close(self) -> None:
        self.pause()
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)

    async def generate(self, conn: sqlite3.Connection, note_id: str, session_id: str) -> None:
        revision = None
        try:
            model = self.model()
            available = model is not None and await model.local_ready()
            if self.connection() is not conn:
                return
            row = conn.execute("SELECT source_revision,state FROM session_notes WHERE note_id=?", (note_id,)).fetchone()
            if row is None or row[1] == "deleted":
                return
            revision = row[0]
            if not available:
                conn.execute(
                    "UPDATE session_notes SET state='no_model' WHERE note_id=? AND state<>'deleted'", (note_id,)
                )
                return
            sources = evidence(conn, session_id, self.capture())
            turn_ids = {str(source["reference"]["turn_id"]) for source in sources}
            kcs = set()
            for turn_id, payload in conn.execute(
                "SELECT turn_id,payload FROM events WHERE session_id=?", (session_id,)
            ):
                if turn_id not in turn_ids and f"trn_{turn_id}" not in turn_ids:
                    continue
                for kc in json.loads(payload).get("kc_ids", []):
                    identity = kc.get("kc_id") if isinstance(kc, dict) else kc
                    if isinstance(identity, str) and identity.startswith("kc."):
                        kcs.add(identity)
            conn.execute("UPDATE session_notes SET state='writing' WHERE note_id=? AND state<>'deleted'", (note_id,))
            if sources:
                selections, local = await model.session_note(sources)
                if not local:
                    raise ValueError("nonlocal_note_refused")
                statements, withheld = materialise(selections, sources)
            else:
                statements, withheld = [], 0
            if self.connection() is not conn:
                return
            # Neither a learner deletion/private mark nor a newer generation is overwritten.
            with transaction(conn):
                conn.execute(
                    "UPDATE session_notes SET statements=?,kc_ids=?,withheld=?,state='ready',updated_at=?"
                    " WHERE note_id=? AND state<>'deleted' AND source_revision=?",
                    (json.dumps(statements), json.dumps(sorted(kcs)), withheld, self.clock(), note_id, revision),
                )
            if withheld:
                log.info("session note statements withheld: %d", withheld)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self.connection() is conn:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute(
                        "UPDATE session_notes SET state='failed' WHERE note_id=? AND state<>'deleted'"
                        " AND (? IS NULL OR source_revision=?)",
                        (note_id, revision, revision),
                    )
            log.warning("session note generation failed (%s)", type(error).__name__)


def archive_note(row: Any) -> tuple[Any, ...]:
    """A profile archive can carry these projections without carrying the source conversation."""
    if not isinstance(row, dict) or set(row) != set(COLUMNS.split(",")):
        raise ValueError("invalid_session_note")
    if not re.fullmatch(r"snt_[0-9A-HJKMNP-TV-Z]{26}", str(row["note_id"])) or not re.fullmatch(
        r"[0-9A-HJKMNP-TV-Z]{26}", str(row["session_id"])
    ):
        raise ValueError("invalid_session_note")
    if row["state"] not in ("queued", "writing", "ready", "failed", "no_model", "deleted") or row["private"] not in (
        0,
        1,
    ):
        raise ValueError("invalid_session_note")
    if not isinstance(row["title"], str) or len(row["title"]) > 400:
        raise ValueError("invalid_session_note")
    for key in ("source_revision", "withheld", "created_at", "updated_at"):
        if type(row[key]) is not int or row[key] < 0:
            raise ValueError("invalid_session_note")
    ids, statements = json.loads(row["kc_ids"]), json.loads(row["statements"])
    if row["state"] == "deleted" and (row["title"] or row["subject"] or ids or statements or row["private"]):
        raise ValueError("invalid_session_note_tombstone")
    if not isinstance(ids, list) or not all(isinstance(k, str) and k.startswith("kc.") for k in ids):
        raise ValueError("invalid_session_note")
    if not isinstance(statements, list) or len(statements) > 24:
        raise ValueError("invalid_session_note")
    for statement in statements:
        if not isinstance(statement, dict) or set(statement) != {"text", "section", "reference"}:
            raise ValueError("invalid_session_note")
        ref = statement["reference"]
        if (
            statement["section"] not in SECTIONS
            or not isinstance(statement["text"], str)
            or len(statement["text"]) > 400
        ):
            raise ValueError("invalid_session_note")
        if (
            not isinstance(ref, dict)
            or ref.get("session_id") != "ses_" + row["session_id"]
            or not re.fullmatch(r"trn_[0-9A-HJKMNP-TV-Z]{26}", str(ref.get("turn_id")))
        ):
            raise ValueError("invalid_session_note")
    return tuple(row[k] for k in COLUMNS.split(","))
