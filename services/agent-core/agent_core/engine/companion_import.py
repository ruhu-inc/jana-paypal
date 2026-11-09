"""An imported profile's companion state (API-30, D87, SPEC §19): the two steps of a profile archive import that
touch the memory layer, which the api may not import (ARC-18). `store.companion_archive.restore` writes the rows."""

from __future__ import annotations

import sqlite3
from typing import Any

from agent_core.memory.visibility import advance
from agent_core.memory.write import fold_companion_summary


def exclude_removed_memory(conn: sqlite3.Connection, now_ms: int) -> None:
    """Old archives can contain paraphrases of since-forgotten memory in dialogue and check-ins. Keep authorized
    history visible, exclude it from model recall, and do not revive reminders derived from removed memory. Runs in
    the import's transaction."""
    advance(conn, now_ms)
    conn.execute("DELETE FROM commitments")


def refold_summaries(conn: sqlite3.Connection, rows: dict[str, Any], content: Any, *, now_ms: int) -> None:
    """Replay each restored thread's rolling summary from the imported events by code (§19.5.3): the restore kept the
    thread and dropped the archived body, whose event cursor was a position in another database."""
    for summary in rows["summaries"]:
        fold_companion_summary(conn, summary["thread_id"], content, now_ms=now_ms)
