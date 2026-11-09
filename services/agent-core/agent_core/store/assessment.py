"""Assessment checkpoints in learner.db (ASM-10, ASM-16, API-27).

Uses the existing sessions/checkpoints tables. Assessment checkpoints have their own phase and are never restored
as tutor sessions. Mock answers live only in exam_answers, so old checkpoints cannot resurrect a cleared response.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agent_core.store.checkpoints import CheckpointStore


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class AssessmentStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, identity: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT state FROM checkpoints WHERE session_id = ? AND phase = 'assessment'"
            " ORDER BY created_at DESC, checkpoint_id DESC LIMIT 1",
            (identity,),
        ).fetchone()
        return json.loads(row[0])["assessment_runtime"] if row else None

    def all(self) -> list[dict[str, Any]]:
        ids = self.conn.execute("SELECT DISTINCT session_id FROM checkpoints WHERE phase = 'assessment'").fetchall()
        return [state for (identity,) in ids if (state := self.get(identity)) is not None]

    def put(self, body: dict[str, Any], now: int) -> None:
        CheckpointStore(self.conn).save_assessment(body, now)
