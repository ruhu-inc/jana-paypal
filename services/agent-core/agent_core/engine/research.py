"""EVL-22, MEM-26: allow-listed research exports at the API's engine boundary."""

from __future__ import annotations

import sqlite3
from typing import Any

from agent_core.store.exports import research_rows
from agent_core.trace.research import project


def export_rows(conn: sqlite3.Connection, approval: dict[str, Any] | None = None) -> dict[str, Any]:
    """The sealed research export's body. It names its kind and the consent it was made under (the `research_use`
    scope, the student's assent and the registered protocol, never the school), so the authoring system's analytics
    intake can refuse anything else (AUT-37, D51)."""
    consent = {"scope": "research_use", "student_assent": bool((approval or {}).get("student_assent")),
               "protocol_ref": str((approval or {}).get("protocol_ref", ""))}  # fmt: skip
    return {"kind": "research_export", "consent": consent, **project(research_rows(conn))}
