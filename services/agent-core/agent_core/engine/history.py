"""Private session history (UXS-48): the open profile's retained sessions and, read-only, what each one showed.

The list holds every session the learner database still has a row for (DAT-06), newest first, by date, subject, topic
and duration. A session's conversation, board and visuals come from its stored turns, parts and board steps; once the
retention job has purged its transcript (TABLE DAT-08a, `retention_transcript_days`) the session is listed as purged
and opens with no content, while every event, and so all progress, stays. Nothing here shares, exports or sends: a
readable archive of chosen sessions is the separate confirmation of API-30."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agent_core.store.pack_index import PackIndex
from agent_core.store.transcript import bare

COLUMNS = "session_id, mode, subject, topic_id, title, started_at, ended_at, transcript_purged_at"
STUDY_MODES = "'study', 'study_companion'"


class HistoryRefused(ValueError):
    pass


def _entry(row: tuple[Any, ...], content: PackIndex | None) -> dict[str, Any]:
    session_id, mode, subject, topic, title, started, ended, purged = row
    return {
        "session_id": "ses_" + str(session_id),
        "mode": mode,
        "subject": subject,
        "topic_id": topic,
        "topic_title": content.topic_title(topic) if content is not None and topic else None,
        "title": title or "",
        "started_at": int(started),
        "duration_ms": int(ended) - int(started) if ended is not None and ended >= started else None,
        "transcript": "purged" if purged is not None else "kept",
    }


class HistoryDesk:
    def __init__(self, conn: sqlite3.Connection, content: PackIndex | None, retention_days: int) -> None:
        self.conn, self.content, self.retention_days = conn, content, retention_days

    def list(self, limit: int = 50, before: str | None = None) -> dict[str, Any]:
        """`HistoryList`: a page of sessions, newest first, and the fixed retention period the screen explains."""
        size = max(1, min(limit, 100))
        # A Study session (SPEC §19.3.2) has no conversation to read back: its activity shows on Progress instead.
        query = f"SELECT {COLUMNS} FROM sessions WHERE mode NOT IN ({STUDY_MODES})"  # noqa: S608 - fixed columns
        args: tuple[Any, ...] = ()
        if before is not None:
            query += " AND session_id < ?"
            args = (bare(before),)
        rows = self.conn.execute(query + " ORDER BY session_id DESC LIMIT ?", (*args, size + 1)).fetchall()
        entries = [_entry(row, self.content) for row in rows[:size]]
        more = len(rows) > size
        return {"sessions": entries, "next_before": entries[-1]["session_id"] if more else None,
                "retention_days": self.retention_days}  # fmt: skip

    def session(self, session_id: str) -> dict[str, Any]:
        """`HistorySession`: the session read-only, as the snapshot shapes it (messages, parts by id, boards), or no
        content when its transcript was purged."""
        row = self.conn.execute(f"SELECT {COLUMNS} FROM sessions WHERE session_id = ?",  # noqa: S608
                                (bare(session_id),)).fetchone()  # fmt: skip
        if row is None or not session_id.startswith("ses_"):
            raise HistoryRefused("not_found")
        entry = _entry(row, self.content)
        if entry["transcript"] == "purged":
            return {"session": entry, "messages": [], "parts": {}, "boards": []}
        messages: list[dict[str, Any]] = []
        parts: dict[str, Any] = {}
        for turn_id, speaker, text in self.conn.execute(
            "SELECT turn_id, speaker, student_text FROM turns WHERE session_id = ? ORDER BY idx", (bare(session_id),)
        ):
            if speaker == "student":
                messages.append({"role": "student", "turn_id": turn_id, "text": text or ""})
                continue
            if speaker != "tutor":
                continue
            ids = []
            for part_id, content in self.conn.execute(
                "SELECT part_id, content FROM parts WHERE turn_id = ? AND status != 'retracted' ORDER BY idx",
                (turn_id,),
            ):
                parts[part_id] = json.loads(content)
                ids.append(part_id)
            messages.append({"role": "tutor", "turn_id": turn_id, "part_ids": ids})
        boards: dict[str, dict[str, Any]] = {}
        for board_id, part_id, step_id, idx, latex, reason, owner, status in self.conn.execute(
            "SELECT board_id, part_id, step_id, idx, latex, reason, owner, status FROM board_steps"
            " WHERE session_id = ? AND superseded_at IS NULL ORDER BY board_id, idx, step_id",
            (bare(session_id),),
        ):
            board = boards.setdefault(board_id, {"board_id": board_id, "part_id": part_id, "steps": []})
            board["steps"].append({"step_id": step_id, "index": idx, "latex": latex, "reason": reason,
                                   "owner": owner, "status": status})  # fmt: skip
        return {"session": entry, "messages": messages, "parts": parts, "boards": list(boards.values())}
