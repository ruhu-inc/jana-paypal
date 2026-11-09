"""The durable memory-context marker: model recall is invalidated after a learner edits or forgets memory.

History remains available to the learner. Prior dialogue is excluded from model recall conservatively, so an old
paraphrase cannot restore a removed fact. The marker is an `audit_log` row that commits with the memory edit and
contains no learner text. The memory layer advances it through `memory.visibility`; the store reads and restores it
in a profile archive (`store.companion_archive`), which is why it lives here (ARC-18)."""

from __future__ import annotations

import sqlite3
from typing import Literal

from agent_core.store.ids import new_ulid

ACTION = "memory_context_changed"


def advance(conn: sqlite3.Connection, now_ms: int, *, actor: Literal["student", "system"] = "student") -> None:
    if not conn.in_transaction:
        raise RuntimeError("memory visibility must change in the memory write's transaction")
    # Retention keeps the latest marker only. A clock rollback must not reopen an older recall window, including
    # a first forget after dialogue was stored with later timestamps. The logical cutoff covers all existing text.
    latest_turn = int(conn.execute("SELECT coalesce(max(started_at),-1) FROM turns").fetchone()[0])
    latest_board = int(conn.execute("SELECT coalesce(max(created_at),-1) FROM board_steps").fetchone()[0])
    cutoff_at = max(now_ms, cutoff(conn), latest_turn, latest_board)
    conn.execute(
        "INSERT INTO audit_log (audit_id,actor_role,action,at) VALUES (?,?,?,?)",
        (new_ulid(now_ms), actor, ACTION, cutoff_at),
    )


def revision(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT audit_id FROM audit_log WHERE action = ? ORDER BY rowid DESC LIMIT 1",
        (ACTION,),
    ).fetchone()
    return str(row[0]) if row is not None else ""


def cutoff(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT coalesce(max(at),-1) FROM audit_log WHERE action = ?", (ACTION,)).fetchone()[0])
