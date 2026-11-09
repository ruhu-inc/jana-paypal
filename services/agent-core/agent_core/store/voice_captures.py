"""Promote an explicitly confirmed voice draft into the shared M21 capture repository."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def promote(conn: sqlite3.Connection, record: dict[str, Any], expires_at: int) -> None:
    if record["source"] != "voice" or record["status"] != "confirmed" or not record["confirmation"]["confirmed"]:
        raise ValueError("voice_capture_not_confirmed")
    conn.execute(
        "INSERT INTO captures (capture_id, session_id, record, expires_at) VALUES (?, ?, ?, ?)",
        (record["capture_id"], record["session_id"][4:], json.dumps(record, ensure_ascii=False), expires_at),
    )
