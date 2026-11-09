"""Local companion archive state. Raw dialogue and cached openers require transcript authorization."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agent_core.contracts import errors
from agent_core.store.companion_summary import summary_body
from agent_core.store.exports import ExportError
from agent_core.store.memory_context import ACTION, cutoff, revision

TABLES = {
    "summaries": ("companion_summary", ("thread_id", "version", "cursor_seq", "updated_at", "body_json")),
    "episodes": (
        "companion_episodes",
        ("session_id", "thread_id", "profile_day", "opener_json", "opened_at", "ended_at", "checkin_id"),
    ),
    "turn_context": ("companion_turn_context", ("turn_id", "memory_revision", "kind", "egress_json")),
    "commitments": (
        "commitments",
        (
            "commitment_id",
            "kind",
            "subject",
            "kc_id",
            "text",
            "due_from",
            "due_until",
            "dedupe_key",
            "confidence",
            "state",
            "source",
            "source_session_id",
            "created_at",
            "shown_at",
            "resolved_at",
        ),
    ),
    "recalls": ("note_recalls", ("note_id", "session_id", "day")),
}


def snapshot(conn: sqlite3.Connection, session_ids: list[str]) -> dict[str, Any]:
    rows: dict[str, Any] = {
        key: [dict(zip(columns, row, strict=True)) for row in conn.execute(f"SELECT {','.join(columns)} FROM {table}")]  # noqa: S608 - fixed identifiers
        for key, (table, columns) in TABLES.items()
    }
    allowed = set(session_ids)
    rows["episodes"] = [r for r in rows["episodes"] if r["session_id"] in allowed]
    for row in rows["episodes"]:
        cached = json.loads(row["opener_json"])
        row["opener_json"] = json.dumps(
            {
                "opener_id": row["profile_day"],
                "text": cached.get("text", "Ready to study?"),
                "facts": cached.get("facts", {}),
            }
        )
    turns = {r[0] for r in conn.execute("SELECT turn_id,session_id FROM turns") if r[1] in allowed}
    rows["turn_context"] = [r for r in rows["turn_context"] if r["turn_id"] in turns]
    rows["revision"] = {"id": revision(conn), "at": cutoff(conn)}
    return rows


def validate(rows: Any, sessions: dict[str, Any]) -> None:
    """Validate types, real table constraints and ownership before creating an imported profile."""
    from agent_core.store.migration_v20 import LEARNER as v20
    from agent_core.store.migration_v21 import LEARNER as v21
    from agent_core.store.migration_v22 import LEARNER as v22

    def reject() -> None:
        raise ExportError("archive_invalid", "invalid companion state")

    if not isinstance(rows, dict) or set(rows) != {*TABLES, "revision"}:
        reject()
    marker = rows["revision"]
    if (
        not isinstance(marker, dict)
        or set(marker) != {"id", "at"}
        or not isinstance(marker["id"], str)
        or len(marker["id"]) not in (0, 26)
        or type(marker["at"]) is not int
        or marker["at"] < -1
        or bool(marker["id"]) != (marker["at"] >= 0)
    ):
        reject()
    check = sqlite3.connect(":memory:")
    try:
        check.executescript(
            "PRAGMA foreign_keys=ON; CREATE TABLE sessions(session_id TEXT PRIMARY KEY);"
            "CREATE TABLE turns(turn_id TEXT PRIMARY KEY);"
        )
        check.executescript(v20)
        check.execute("ALTER TABLE companion_episodes ADD COLUMN checkin_id TEXT")
        check.executescript(v21[v21.index("CREATE TABLE commitments") :])
        check.executescript(v22)
        for sid, session in sessions.items():
            check.execute("INSERT INTO sessions VALUES (?)", (sid,))
            for row in session["turns"]:
                if row["session_id"] != sid:
                    reject()
                check.execute("INSERT INTO turns VALUES (?)", (row["turn_id"],))
        for key, (table, columns) in TABLES.items():
            if not isinstance(rows[key], list):
                reject()
            for row in rows[key]:
                if not isinstance(row, dict) or set(row) != set(columns):
                    reject()
                if key == "turn_context":
                    policy = json.loads(row["egress_json"])
                    if not isinstance(policy, dict) or any(type(v) is not bool for v in policy.values()):
                        reject()
                if key in ("summaries", "episodes"):
                    field, contract = (
                        ("body_json", "CompanionSummary") if key == "summaries" else ("opener_json", "Opener")
                    )
                    if errors("api/commands#/$defs/" + contract, json.loads(row[field])):
                        reject()
                check.execute(
                    f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",  # noqa: S608
                    tuple(row[c] for c in columns),
                )
        threads = {r["thread_id"] for r in rows["summaries"]}
        if any(r["thread_id"] not in threads for r in rows["episodes"]):
            reject()
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        reject()
    finally:
        check.close()


def restore(conn: sqlite3.Connection, rows: dict[str, Any]) -> None:
    for key, (table, columns) in TABLES.items():
        for original in rows[key]:
            row = dict(original)
            if key == "summaries":
                # Event sequences are local SQLite positions, not portable event identities. Import may omit
                # erased events; replay the summary from imported evidence instead of skipping future events.
                row.update(version=0, cursor_seq=0, body_json=json.dumps(summary_body()))
            if key == "turn_context":
                row["egress_json"] = '{"local_only":true}'
            if (
                key == "recalls"
                and not conn.execute(
                    "SELECT 1 FROM notes WHERE note_id=? AND status<>'deleted'", (row["note_id"],)
                ).fetchone()
            ):
                continue
            conn.execute(
                f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",  # noqa: S608
                tuple(row[c] for c in columns),
            )
    marker = rows["revision"]
    if marker["id"]:
        conn.execute(
            "INSERT INTO audit_log (audit_id,actor_role,action,at) VALUES (?,'student',?,?)",
            (marker["id"], ACTION, marker["at"]),
        )
