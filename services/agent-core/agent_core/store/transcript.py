"""Sessions, turns, parts and board steps in the learner database (DAT-06).

The session row is written when the session starts, the student's turn and the tutor's turn when the turn starts,
a part when it ends (the whole-part snapshot the stream converged on, never its deltas), and one board-step row per
step, updated as its status changes. A turn ends with its streamed status; an interrupted turn keeps the text the
student saw. Session ids are stored as bare ULIDs; turn, message, part, board and step ids keep their prefixes
(API-07). Personal-data redaction of student text arrives in S2 (PRV-20).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agent_core.store.sqlite import transaction


def bare(session_id: str) -> str:
    return session_id.removeprefix("ses_")


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class TranscriptStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # --- writes -------------------------------------------------------------------------------------------------------

    def start_session(self, meta: dict[str, Any], *, hardware_class: str | None = None) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, mode, subject, topic_id, title, hardware_class,"
                " started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (bare(meta["session_id"]), meta["mode"], meta.get("subject"), meta.get("topic_id"),
                 meta.get("title") or None, hardware_class, meta["created_ts"]),
            )  # fmt: skip

    def update_session(self, session_id: str, changed: dict[str, Any]) -> None:
        columns = {k: v for k, v in changed.items() if k in ("title", "mode", "subject", "topic_id")}
        if not columns:
            return
        assignments = ", ".join(f"{name} = ?" for name in columns)
        with transaction(self.conn):
            self.conn.execute(
                f"UPDATE sessions SET {assignments} WHERE session_id = ?",  # noqa: S608 - fixed column names
                (*[v if v != "" else None for v in columns.values()], bare(session_id)),
            )

    def _next_turn_index(self, session_id: str) -> int:
        row = self.conn.execute(
            "SELECT coalesce(max(idx), -1) + 1 FROM turns WHERE session_id = ?", (bare(session_id),)
        ).fetchone()
        return int(row[0])

    def start_turn(
        self,
        session_id: str,
        *,
        turn_id: str,
        message_id: str | None,
        student_text: str | None,
        input_kind: str,
        ts: int,
    ) -> None:
        """The student's turn (its message) and the tutor's turn, which stays `open` until `turn.end`."""
        with transaction(self.conn):
            idx = self._next_turn_index(session_id)
            if message_id is not None:
                self.conn.execute(
                    "INSERT OR IGNORE INTO turns (turn_id, session_id, idx, speaker, status, student_text, input_kind,"
                    " started_at, ended_at) VALUES (?, ?, ?, 'student', 'complete', ?, ?, ?, ?)",
                    (message_id, bare(session_id), idx, student_text, input_kind, ts, ts),
                )
                idx += 1
            self.conn.execute(
                "INSERT OR IGNORE INTO turns (turn_id, session_id, idx, speaker, status, started_at)"
                " VALUES (?, ?, ?, 'tutor', 'open', ?)",
                (turn_id, bare(session_id), idx, ts),
            )

    def put_part(self, turn_id: str, part: dict[str, Any], ts: int, *, idx: int | None = None) -> None:
        """The whole part as it ended (or as it changed afterwards, such as an answered card)."""
        card_type = part.get("card", {}).get("card", {}).get("card_type") if part["kind"] == "card" else None
        status = part["status"] if part["status"] in ("complete", "error", "interrupted", "retracted") else "complete"
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO parts (part_id, turn_id, idx, kind, status, card_type, content, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (part_id) DO UPDATE SET status = excluded.status,"
                " card_type = excluded.card_type, content = excluded.content, updated_at = excluded.updated_at",
                (part["part_id"], turn_id, part["index"] if idx is None else idx, part["kind"], status, card_type,
                 _dumps(part), ts, ts),
            )  # fmt: skip

    def remove_part(self, session_id: str, part_id: str) -> None:
        """Remove only a part owned by this session, including its history copy."""
        with transaction(self.conn):
            self.conn.execute(
                "DELETE FROM parts WHERE part_id = ? AND turn_id IN (SELECT turn_id FROM turns WHERE session_id = ?)",
                (part_id, bare(session_id)),
            )

    def end_session(self, session_id: str, *, reason: str, ts: int) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE session_id = ?", (ts, reason, bare(session_id))
            )

    def turn_starts(self, session_id: str) -> list[int]:
        rows = self.conn.execute(
            "SELECT started_at FROM turns WHERE session_id = ? ORDER BY started_at", (bare(session_id),)
        )
        return [int(r[0]) for r in rows]

    def end_turn(self, turn_id: str, *, status: str, usage: dict[str, Any] | None, ts: int) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE turns SET status = ?, usage = ?, ended_at = ? WHERE turn_id = ?",
                (status, _dumps(usage) if usage is not None else None, ts, turn_id),
            )

    def voice_delivery(self, turn_id: str, delivery: dict[str, Any], ts: int) -> None:
        """Keep heard/spoken reconciliation beside the final text part, without changing the wire part contract."""
        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT part_id, content FROM parts WHERE turn_id = ? AND kind = 'text' ORDER BY idx DESC LIMIT 1",
                (turn_id,),
            ).fetchone()
            if row is not None:
                content = json.loads(row[1])
                content["voice_delivery"] = delivery
                self.conn.execute(
                    "UPDATE parts SET content = ?, updated_at = ? WHERE part_id = ?", (_dumps(content), ts, row[0])
                )

    def interrupt_open(self, session_id: str, ts: int) -> None:
        """After a crash: a turn still `open` ended `interrupted` (its parts were written as they ended)."""
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE turns SET status = 'interrupted', ended_at = ? WHERE session_id = ? AND status = 'open'",
                (ts, bare(session_id)),
            )

    def put_step(self, session_id: str, board_id: str, part_id: str | None, step: dict[str, Any], ts: int) -> None:
        with transaction(self.conn):
            known_part = (
                part_id is not None
                and self.conn.execute("SELECT 1 FROM parts WHERE part_id = ?", (part_id,)).fetchone()
            )
            self.conn.execute(
                "INSERT INTO board_steps (step_id, session_id, board_id, part_id, idx, latex, reason, owner, status,"
                " error_category, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (step_id) DO UPDATE SET latex = excluded.latex, reason = excluded.reason,"
                " status = excluded.status, error_category = excluded.error_category, idx = excluded.idx",
                (step["step_id"], bare(session_id), board_id, part_id if known_part else None, step["index"],
                 step["latex"], step.get("reason"), step["owner"], step["status"],
                 (step.get("error") or {}).get("category"), ts),
            )  # fmt: skip

    def reset_board(self, board_id: str, ts: int) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE board_steps SET superseded_at = ? WHERE board_id = ? AND superseded_at IS NULL", (ts, board_id)
            )

    # --- reads --------------------------------------------------------------------------------------------------------

    def session(self, session_id: str) -> dict[str, Any] | None:
        self.conn.row_factory = sqlite3.Row
        try:
            row = self.conn.execute("SELECT * FROM sessions WHERE session_id = ?", (bare(session_id),)).fetchone()
            return dict(row) if row else None
        finally:
            self.conn.row_factory = None

    def turns(self, session_id: str) -> list[dict[str, Any]]:
        self.conn.row_factory = sqlite3.Row
        try:
            rows = self.conn.execute("SELECT * FROM turns WHERE session_id = ? ORDER BY idx", (bare(session_id),))
            return [dict(row) for row in rows]
        finally:
            self.conn.row_factory = None

    def parts(self, turn_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT part_id, idx, kind, status, card_type, content FROM parts WHERE turn_id = ? ORDER BY idx",
            (turn_id,),
        )
        return [
            {"part_id": r[0], "idx": r[1], "kind": r[2], "status": r[3], "card_type": r[4], "content": json.loads(r[5])}
            for r in rows
        ]

    def steps(self, session_id: str) -> list[dict[str, Any]]:
        self.conn.row_factory = sqlite3.Row
        try:
            rows = self.conn.execute(
                "SELECT * FROM board_steps WHERE session_id = ? ORDER BY board_id, idx, step_id", (bare(session_id),)
            )
            return [dict(row) for row in rows]
        finally:
            self.conn.row_factory = None
