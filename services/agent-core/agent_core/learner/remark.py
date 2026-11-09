"""Re-marking a withdrawn item's attempts against its corrected key (CUR-36, ASM-23, OPS-27), on this device only.

`marked_attempts` lists every verdict given on the item's attempts as it reads now (its latest re-mark, or the
original); the marker, which lives in the engine, marks each stored response against the corrected item, and
`remark_payload` turns that into a `verdict` event superseding the one it replaces. `Learner.supersede` appends them
and rebuilds the projections once. Nothing original is edited or removed: `original_evidence_sha256` hashes the
item's attempt events and original verdicts, so the same hash before and after shows they were retained, and
`projections_match_replay` shows the tables equal a rebuild from the log (DAT-05).

When a re-mark changed results, the student is told once (ASM-23): `notice_writer` records how many in the learner
database's audit log, in the re-mark's own transaction, and `take_notice` reads what is waiting and marks it told, so
the count survives a restart and is shown once.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from agent_core.learner.projections import Supersession
from agent_core.learner.service import Learner
from agent_core.store.events import COLUMNS, MERGE_ORDER, StoredEvent
from agent_core.store.ids import new_ulid
from agent_core.store.sqlite import transaction

# What a marker decides; the rest of a verdict payload (its id, attempt, opportunity, supersession) is bookkeeping.
MARKING_FIELDS = (
    "outcome", "marks_awarded", "marks_available", "error_category", "misconception_id", "first_error_step",
    "first_error_kc", "check_ids", "criteria", "marker_version",
)  # fmt: skip


@dataclass(frozen=True)
class Marked:
    """One verdict given on an attempt at the item, as the projections read it now."""

    attempt: dict[str, Any]  # the attempt payload (its stored response, part and KCs)
    session_id: str | None
    verdict: dict[str, Any]  # the latest re-mark of the original verdict, or the original itself
    original_id: str  # the original verdict's id


def _attempt_and_verdicts(learner: Learner) -> Iterator[StoredEvent]:
    rows = learner.conn.execute(
        f"SELECT {COLUMNS} FROM events WHERE type IN ('attempt', 'verdict') AND erased_at IS NULL"  # noqa: S608
        f" ORDER BY {MERGE_ORDER}"
    )
    return map(StoredEvent.from_row, rows)


def _on_item(attempt: Mapping[str, Any], item_id: str) -> bool:
    return attempt.get("item_id") == item_id or (not attempt.get("item_id") and attempt.get("problem_ref") == item_id)


def _evidence(learner: Learner, item_id: str) -> tuple[list[StoredEvent], list[StoredEvent], Supersession]:
    """The item's attempt events and every verdict given on them, in merge order, with the log's re-marks."""
    events = list(_attempt_and_verdicts(learner))
    attempts = [e for e in events if e.type == "attempt" and _on_item(e.payload, item_id)]
    ids = {e.payload["attempt_id"] for e in attempts}
    verdicts = [e for e in events if e.type == "verdict" and e.payload["attempt_id"] in ids]
    return attempts, verdicts, Supersession.of(e for e in events if e.type == "verdict")


def marked_attempts(learner: Learner, item_id: str) -> list[Marked]:
    """Every original verdict on the item's attempts, each as it reads now, in log order."""
    attempts, verdicts, remarks = _evidence(learner, item_id)
    by_id = {e.payload["attempt_id"]: e.payload for e in attempts}
    out = []
    for event in verdicts:
        verdict_id = event.payload["verdict_id"]
        if verdict_id in remarks.replacing:
            continue
        latest = remarks.replaced_by.get(verdict_id)
        current = latest.payload if latest is not None else event.payload
        out.append(Marked(by_id[event.payload["attempt_id"]], event.session_id, current, verdict_id))
    return out


def marking_of(verdict: Mapping[str, Any]) -> dict[str, Any]:
    return {k: verdict[k] for k in MARKING_FIELDS if verdict.get(k) is not None}


def _semantic_marking(value: Any) -> Any:
    """A repeated tool call issues fresh check ids; those identify provenance, not a changed marking decision."""
    if isinstance(value, dict):
        return {k: _semantic_marking(v) for k, v in value.items() if k not in ("check_id", "check_ids")}
    if isinstance(value, list):
        return [_semantic_marking(v) for v in value]
    return value


def remark_payload(marked: Marked, marking: Mapping[str, Any]) -> dict[str, Any] | None:
    """The verdict that supersedes `marked` with the corrected marking, or None when it already reads as a re-mark
    giving exactly that marking (re-marking twice appends nothing)."""
    fields = marking_of(marking)
    if marked.verdict.get("supersedes") and _semantic_marking(marking_of(marked.verdict)) == _semantic_marking(fields):
        return None
    return {
        **fields,
        "verdict_id": new_ulid(),
        "attempt_id": marked.attempt["attempt_id"],
        "closes_opportunity": bool(marked.verdict["closes_opportunity"]),  # the opportunity keeps its shape
        "supersedes": marked.verdict["verdict_id"],
    }


def original_evidence_sha256(learner: Learner, item_id: str) -> str:
    """A hash of the item's attempt events and original verdict events (re-marks left out), byte for byte as
    stored: equal before and after a re-mark when nothing original changed."""
    attempts, verdicts, remarks = _evidence(learner, item_id)
    digest = hashlib.sha256()
    originals = [e for e in verdicts if e.payload["verdict_id"] not in remarks.replacing]
    for event in sorted([*attempts, *originals], key=lambda e: e.order):
        row = [event.event_id, event.device_id, event.origin_seq, event.hlc, event.type, event.session_id,
               event.occurred_at, event.payload]  # fmt: skip
        digest.update(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def projections_match_replay(learner: Learner) -> tuple[bool, str]:
    """Whether the learner tables equal a full replay of the log (DAT-05), and their checksum after it."""
    before = learner.projector.checksum()
    learner.projector.rebuild()
    after = learner.projector.checksum()
    return before == after, after


# The learner database's audit log keeps the notice (no learner table holds pending notices): one row per re-mark that
# changed results, and one row per row told, naming it.
NOTICE_ACTION = "remark.notice"
NOTICE_SHOWN = "remark.notice_shown"


def notice_writer(job_id: str, changed: int, now_ms: int) -> Callable[[sqlite3.Connection, Any], None]:
    """The re-mark's `extra` step: records `changed` results waiting to be told, when there are any."""

    def write(conn: sqlite3.Connection, _stored: Any) -> None:
        if changed > 0:
            conn.execute(
                "INSERT INTO audit_log (audit_id, actor_role, action, target, detail, at) VALUES (?, 'system', ?, ?,"
                " ?, ?)",
                (new_ulid(now_ms), NOTICE_ACTION, job_id, json.dumps({"changed_results": changed}), now_ms),
            )

    return write


def _waiting(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT audit_id, CAST(json_extract(detail, '$.changed_results') AS INTEGER) FROM audit_log a"
        " WHERE action = ? AND NOT EXISTS (SELECT 1 FROM audit_log s WHERE s.action = ? AND s.target = a.audit_id)"
        " ORDER BY audit_id",
        (NOTICE_ACTION, NOTICE_SHOWN),
    ).fetchall()
    return [(str(audit_id), int(n or 0)) for audit_id, n in rows]


def pending_notice(conn: sqlite3.Connection) -> int:
    """How many re-marked results the student has not yet been told about."""
    return sum(n for _, n in _waiting(conn))


def take_notice(conn: sqlite3.Connection, now_ms: int) -> int:
    """The re-marked results waiting to be told, marked told in the same transaction (0 when none wait, or when a
    write is open on the connection: the notice then waits for the next turn)."""
    if conn.in_transaction:
        return 0
    with transaction(conn):
        waiting = _waiting(conn)
        for audit_id, _ in waiting:
            conn.execute(
                "INSERT INTO audit_log (audit_id, actor_role, action, target, detail, at) VALUES (?, 'system', ?, ?,"
                " NULL, ?)",
                (new_ulid(now_ms), NOTICE_SHOWN, audit_id, now_ms),
            )
    return sum(n for _, n in waiting)
