"""Profile-local paginated thread history and bounded context, sharing transcript retention and provenance."""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from typing import Any

from agent_core.memory.visibility import revision
from agent_core.store.sqlite import transaction


def record_context(conn: sqlite3.Connection, turn_id: str, kind: str, policy: dict[str, bool]) -> None:
    """Stamp the completed tutor turn and its preceding learner message. No duplicate copy of their text."""
    row = conn.execute("SELECT session_id,idx FROM turns WHERE turn_id = ?", (turn_id,)).fetchone()
    if row is None:
        return  # an engine without a transcript sink (unit harness)
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO companion_turn_context SELECT turn_id,?,?,? FROM turns"
            " WHERE session_id = ? AND idx IN (?,?)",
            (revision(conn), kind, json.dumps(policy), row[0], row[1], row[1] - 1),
        )


def _cursor(value: str) -> tuple[int, str, int]:
    try:
        if len(value) > 256:
            raise ValueError
        found = json.loads(base64.urlsafe_b64decode(value.encode()).decode())
        if (
            not isinstance(found, list)
            or len(found) != 3
            or type(found[0]) is not int
            or not isinstance(found[1], str)
            or type(found[2]) is not int
        ):
            raise ValueError
        return found[0], found[1], found[2]
    except (ValueError, UnicodeError, binascii.Error):
        raise ValueError("invalid history cursor") from None


def page(conn: sqlite3.Connection, *, limit: int = 50, before: str | None = None) -> dict[str, Any]:
    """Newest page, items in chronological order; an opaque keyset cursor avoids overlap as new turns arrive."""
    limit = max(1, min(limit, 100))
    boundary = _cursor(before) if before else (2**63 - 1, "~", 2**63 - 1)
    rows = conn.execute(
        "SELECT t.turn_id,t.session_id,t.speaker,t.student_text,t.started_at,e.opened_at,t.idx,"
        " c.memory_revision,c.kind,c.egress_json FROM turns t"
        " JOIN companion_episodes e ON e.session_id = t.session_id"
        " JOIN sessions s ON s.session_id = t.session_id"
        " LEFT JOIN companion_turn_context c ON c.turn_id = t.turn_id"
        " WHERE s.transcript_purged_at IS NULL AND t.status <> 'open' AND t.speaker IN ('student','tutor')"
        " AND (e.opened_at,t.session_id,t.idx) < (?,?,?)"
        " ORDER BY e.opened_at DESC,t.session_id DESC,t.idx DESC LIMIT ?",
        (*boundary, limit + 1),
    ).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for tid, sid, speaker, student, started, _, _, epoch, kind, egress in reversed(rows):
        if speaker == "student":
            text = student or ""
        else:
            parts = conn.execute(
                "SELECT content FROM parts WHERE turn_id = ? AND kind = 'text' AND status <> 'retracted' ORDER BY idx",
                (tid,),
            ).fetchall()
            text = "\n\n".join(str(json.loads(p[0]).get("text", "")) for p in parts)
        items.append(
            {
                "id": tid,
                "session_id": "ses_" + sid,
                "role": "user" if speaker == "student" else "assistant",
                "text": text[:8000],
                "created_at": started,
                "_revision": epoch,
                "_kind": kind,
                "_egress": json.loads(egress) if egress else {"local_only": True},
            }
        )
    cursor = None
    if more and rows:
        last = rows[-1]
        cursor = base64.urlsafe_b64encode(json.dumps([last[5], last[1], last[6]]).encode()).decode()
    return {"items": items, "next_cursor": cursor}


def public_page(conn: sqlite3.Connection, *, limit: int = 50, before: str | None = None) -> dict[str, Any]:
    found = page(conn, limit=limit, before=before)
    found["items"] = [{k: v for k, v in item.items() if not k.startswith("_")} for item in found["items"]]
    return found


def context_rows(conn: sqlite3.Connection, session_id: str, *, allowance_chars: int = 12000) -> list[dict[str, Any]]:
    """A bounded recent window across episodes. Past raw dialogue is always local, regardless of old consent."""
    from agent_core.engine.companion_memory import parse

    current = revision(conn)
    chosen, used = [], 0
    # Bound both the scan and prompt size; deeper recall remains available through the local session_recall tool.
    for item in reversed(page(conn, limit=100)["items"]):
        if item["_kind"] != "companion" or (item["_revision"] or "") != current:
            continue
        if item["role"] == "user" and parse(item["text"]) is not None:
            continue
        if not item["text"]:
            continue
        if used + len(item["text"]) > allowance_chars:
            break
        policy = item["_egress"]
        if item["session_id"] != session_id:
            policy = {"local_only": True}
        chosen.append({"role": item["role"], "content": item["text"], "kind": "free", "companion_egress": policy})
        used += len(item["text"])
    return list(reversed(chosen))
