"""Session checkpoints in the profile's learner database (DAT-07, ARC-25, MEM-02).

The record the API restores a session from (its projection and engine state) is written to `checkpoints` after every
completed board step, card answer, mode change and turn end, in one transaction; only the latest three per session are
kept. On start after a crash or power loss the newest checkpoint of each session is read back, so a student resumes at
the last completed step. This replaces the S1 JSON files of `store.sessions`, which shared one folder across profiles
(TABLE DAT-01a); the same interface is kept, so the API does not know which it holds."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from agent_core.store.ids import new_ulid
from agent_core.store.sqlite import transaction

_ID = re.compile(r"^ses_[0-9A-HJKMNP-TV-Z]{26}$")
STATE_VERSION = 1


def _bare(session_id: str) -> str:
    if not _ID.match(session_id):
        raise ValueError("not a session id")
    return session_id[4:]


class CheckpointStore:
    def __init__(self, conn: sqlite3.Connection, keep: int = 3, clock_ms: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.keep = keep
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def save(self, session_id: str, record: dict[str, Any]) -> None:
        bare = _bare(session_id)
        projection = record.get("projection") or {}
        meta = projection.get("session") or {}
        now = self.clock_ms()
        with transaction(self.conn):
            # The transcript store usually made the row at session start; a core without learning records has not.
            self.conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, mode, subject, started_at) VALUES (?, ?, ?, ?)",
                (bare, meta.get("mode", "guided"), _subject(meta.get("subject")), meta.get("created_ts", now)),
            )
            self.conn.execute(
                "INSERT INTO checkpoints (checkpoint_id, session_id, turn_id, last_event_seq, phase, state,"
                " state_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (new_ulid(now), bare, None, int((projection.get("cursor") or {}).get("seq", 0)), "persisted",
                 json.dumps(record, ensure_ascii=False, separators=(",", ":")), STATE_VERSION, now),
            )  # fmt: skip
            self.conn.execute(
                "DELETE FROM checkpoints WHERE session_id = ? AND checkpoint_id NOT IN (SELECT checkpoint_id FROM"
                " checkpoints WHERE session_id = ? ORDER BY created_at DESC, checkpoint_id DESC LIMIT ?)",
                (bare, bare, self.keep),
            )

    def save_assessment(self, body: dict[str, Any], now: int) -> None:
        """M16 assessment state, joining its learner evidence transaction (ASM-16, API-27, MEM-01)."""
        if not self.conn.in_transaction:
            raise ValueError("an assessment checkpoint must join the learner write")
        identity = _bare("ses_" + body["id"])
        self.conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, mode, subject, started_at) VALUES (?, ?, ?, ?)",
            (identity, body["kind"], body["subject"], now),
        )
        checkpoint = new_ulid()
        self.conn.execute(
            "INSERT INTO checkpoints (checkpoint_id, session_id, last_event_seq, phase, state,"
            " state_version, created_at) VALUES (?, ?, 0, 'assessment', ?, ?, ?)",
            (
                checkpoint,
                identity,
                json.dumps({"assessment_runtime": body}, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                STATE_VERSION,
                now,
            ),
        )
        self.conn.execute(
            "DELETE FROM checkpoints WHERE session_id = ? AND phase = 'assessment' AND checkpoint_id != ?",
            (identity, checkpoint),
        )

    def load(self, session_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT state FROM checkpoints WHERE session_id = ? ORDER BY created_at DESC, checkpoint_id DESC LIMIT 1",
            (_bare(session_id),),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def load_all(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT session_id, state FROM checkpoints c WHERE checkpoint_id = (SELECT checkpoint_id FROM checkpoints"
            " WHERE session_id = c.session_id ORDER BY created_at DESC, checkpoint_id DESC LIMIT 1)"
            " ORDER BY session_id"
        ).fetchall()
        return {f"ses_{sid}": json.loads(state) for sid, state in rows}

    def delete(self, session_id: str) -> None:
        with transaction(self.conn):
            self.conn.execute("DELETE FROM checkpoints WHERE session_id = ?", (_bare(session_id),))


def _subject(subject: Any) -> str | None:
    subjects = {"physics", "mathematics", "chemistry", "biology", "accounting"}
    return str(subject) if subject in subjects else None
