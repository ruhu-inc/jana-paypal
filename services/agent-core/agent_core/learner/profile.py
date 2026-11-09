"""Profile merges (LRN-04): the profile changes only by a schema merge of the fields that changed.

`merge_profile` compares each offered field with the stored one, writes only those that differ and appends one
`profile_updated` event naming them (names only, never values; learning-event contract), in one transaction. A field
not offered, or offered with its current value, is left exactly as it was. Model output never reaches here: the
callers are the student's own edits and engine policies (the support level of LRN-27).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from agent_core.learner.service import Learner, NewEvent

# The profile fields a merge may write (TABLE LRN-03a; the answer policy is fixed and never stored, D86).
FIELDS = ("level", "subjects", "exam_date", "goals", "reading_level", "support_level", "locale", "student_settings")
JSON_FIELDS = ("subjects", "goals", "student_settings")


def stored_profile(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(f"SELECT {', '.join(FIELDS)} FROM profile").fetchone()  # noqa: S608 - fixed column names
    if row is None:
        return {}
    out = dict(zip(FIELDS, row, strict=True))
    for name in JSON_FIELDS:
        out[name] = json.loads(out[name]) if out[name] is not None else None
    return out


def merge_profile(learner: Learner, changes: Mapping[str, Any], now: int, source: str = "student") -> list[str]:
    """Write the offered fields that differ from the stored profile; returns the changed field names."""
    unknown = set(changes) - set(FIELDS)
    if unknown:
        raise ValueError(f"not profile fields: {sorted(unknown)}")
    current = stored_profile(learner.conn)
    if not current:
        raise ValueError("no profile to merge into")
    changed = [name for name in FIELDS if name in changes and changes[name] != current[name]]
    if not changed:
        return []

    def write(conn: sqlite3.Connection, _: Any) -> None:
        for name in changed:
            value = json.dumps(changes[name]) if name in JSON_FIELDS and changes[name] is not None else changes[name]
            conn.execute(f"UPDATE profile SET {name} = ?, updated_at = ?", (value, now))  # noqa: S608 - a fixed name

    learner.record_batch([NewEvent("profile_updated", {"changed_fields": changed}, source=source, occurred_at=now)],
                         extra=write)  # fmt: skip
    return changed
