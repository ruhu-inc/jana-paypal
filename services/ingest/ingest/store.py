"""The ingest's storage: received reports, support tickets and bundles, and the access log (OBS-17, OBS-20, OPS-24).

What is never here: a client address, a credential, a device identifier with a report, the time of day a report
arrived (reports keep their UTC day only, so arrivals cannot be lined up with anything), or a bundle's plaintext
(bundle files are AES-GCM encrypted under the storage key, with the bundle id as associated data; their ticket
metadata is encrypted the same way). Idempotency keys are kept as SHA-256 digests beside the report they delivered,
so a resend after an interrupted reply answers "seen already" (ARC-09) for as long as the report is kept."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('crash_report', 'telemetry')),
    key_sha256 BLOB NOT NULL,
    day TEXT NOT NULL,
    body TEXT NOT NULL CHECK (json_valid(body)),
    UNIQUE (kind, key_sha256)
);
CREATE INDEX IF NOT EXISTS reports_day ON reports (kind, day);
CREATE TABLE IF NOT EXISTS tickets (
    ticket_ref TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    closed_at REAL,
    incident TEXT
);
CREATE TABLE IF NOT EXISTS assignees (
    ticket_ref TEXT NOT NULL REFERENCES tickets (ticket_ref),
    actor TEXT NOT NULL,
    PRIMARY KEY (ticket_ref, actor)
);
CREATE TABLE IF NOT EXISTS bundles (
    bundle_id TEXT PRIMARY KEY,
    ticket_ref TEXT NOT NULL REFERENCES tickets (ticket_ref),
    received_at REAL NOT NULL,
    bytes INTEGER NOT NULL,
    sealed INTEGER NOT NULL CHECK (sealed IN (0, 1)),
    sha256 TEXT NOT NULL,
    metadata BLOB,
    deleted_at REAL,
    deleted_reason TEXT
);
CREATE INDEX IF NOT EXISTS bundles_ticket ON bundles (ticket_ref);
CREATE TABLE IF NOT EXISTS access_log (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
    reason TEXT
);
CREATE INDEX IF NOT EXISTS access_log_target ON access_log (target);
CREATE INDEX IF NOT EXISTS access_log_at ON access_log (at);
"""
DB_NAME = "ingest.sqlite3"


def day_of(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")


def days_before(now: float, days: int) -> str:
    return (datetime.fromtimestamp(now, UTC) - timedelta(days=days)).strftime("%Y-%m-%d")


def digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


@dataclass(frozen=True)
class Ticket:
    ticket_ref: str
    created_at: float
    closed_at: float | None
    incident: str | None
    assignees: frozenset[str]


@dataclass(frozen=True)
class Bundle:
    bundle_id: str
    ticket_ref: str
    received_at: float
    size: int
    sealed: bool
    sha256: str
    metadata: bytes | None
    deleted_at: float | None


class IngestStore:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "bundles").mkdir(exist_ok=True)
        self.directory = directory
        self._lock = threading.Lock()
        self._db = sqlite3.connect(directory / DB_NAME, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def bundle_path(self, bundle_id: str) -> Path:
        return self.directory / "bundles" / f"{bundle_id}.enc"

    # --- reports (OBS-17, OBS-20) ----------------------------------------------------------------------------------

    def add_report(self, kind: str, key: str, now: float, body: dict[str, Any]) -> bool:
        """Keep a validated report; False when this key delivered one already (a resend)."""
        text = json.dumps(body, sort_keys=True, separators=(",", ":"))
        with self._tx() as db:
            cursor = db.execute(
                "INSERT INTO reports (kind, key_sha256, day, body) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (kind, digest(key), day_of(now), text),
            )
            return cursor.rowcount == 1

    def reports(self, kind: str, first_day: str, last_day: str, limit: int) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT day, body FROM reports WHERE kind = ? AND day BETWEEN ? AND ? ORDER BY id LIMIT ?",
            (kind, first_day, last_day, limit),
        ).fetchall()
        return [{"day": day, "report": json.loads(body)} for day, body in rows]

    def erase_report(self, kind: str, key: str) -> bool:
        with self._tx() as db:
            return db.execute("DELETE FROM reports WHERE kind = ? AND key_sha256 = ?", (kind, digest(key))).rowcount > 0

    # --- tickets and bundles (OPS-24) -------------------------------------------------------------------------------

    def add_ticket(self, ticket_ref: str, assignees: frozenset[str], now: float) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO tickets (ticket_ref, created_at) VALUES (?, ?)", (ticket_ref, now))
            db.executemany("INSERT INTO assignees (ticket_ref, actor) VALUES (?, ?)",
                           [(ticket_ref, actor) for actor in sorted(assignees)])  # fmt: skip

    def ticket(self, ticket_ref: str) -> Ticket | None:
        row = self._db.execute("SELECT ticket_ref, created_at, closed_at, incident FROM tickets WHERE ticket_ref = ?",
                               (ticket_ref,)).fetchone()  # fmt: skip
        if row is None:
            return None
        actors = self._db.execute("SELECT actor FROM assignees WHERE ticket_ref = ?", (ticket_ref,)).fetchall()
        return Ticket(row[0], row[1], row[2], row[3], frozenset(a for (a,) in actors))

    def close_ticket(self, ticket_ref: str, now: float) -> None:
        with self._tx() as db:
            db.execute("UPDATE tickets SET closed_at = ? WHERE ticket_ref = ? AND closed_at IS NULL", (now, ticket_ref))

    def hold_ticket(self, ticket_ref: str, incident: str | None) -> None:
        with self._tx() as db:
            db.execute("UPDATE tickets SET incident = ? WHERE ticket_ref = ?", (incident, ticket_ref))

    def tickets_held_by(self, incident: str) -> list[str]:
        return [r for (r,) in self._db.execute("SELECT ticket_ref FROM tickets WHERE incident = ?", (incident,))]

    def add_bundle(self, bundle: Bundle) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO bundles (bundle_id, ticket_ref, received_at, bytes, sealed, sha256, metadata)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (bundle.bundle_id, bundle.ticket_ref, bundle.received_at, bundle.size, int(bundle.sealed),
                 bundle.sha256, bundle.metadata),
            )  # fmt: skip

    def _bundle(self, row: tuple[Any, ...]) -> Bundle:
        return Bundle(row[0], row[1], row[2], row[3], bool(row[4]), row[5], row[6], row[7])

    _COLUMNS = "bundle_id, ticket_ref, received_at, bytes, sealed, sha256, metadata, deleted_at"

    def bundle(self, bundle_id: str) -> Bundle | None:
        row = self._db.execute(f"SELECT {self._COLUMNS} FROM bundles WHERE bundle_id = ?", (bundle_id,)).fetchone()  # noqa: S608
        return None if row is None else self._bundle(row)

    def bundles(self, ticket_ref: str, *, live_only: bool = False) -> list[Bundle]:
        rows = self._db.execute(f"SELECT {self._COLUMNS} FROM bundles WHERE ticket_ref = ? ORDER BY received_at",  # noqa: S608
                                (ticket_ref,)).fetchall()  # fmt: skip
        found = [self._bundle(row) for row in rows]
        return [b for b in found if b.deleted_at is None] if live_only else found

    def mark_erased(self, bundle_id: str, now: float, reason: str) -> None:
        """The file is already gone; the row keeps only the fact, time and reason (its metadata is dropped)."""
        with self._tx() as db:
            db.execute("UPDATE bundles SET deleted_at = ?, deleted_reason = ?, metadata = NULL WHERE bundle_id = ?",
                       (now, reason, bundle_id))  # fmt: skip

    def expired_bundles(self, now: float, retention_days: int) -> list[str]:
        """Live bundles whose ticket is closed or which are older than the retention period, unless an incident
        holds their ticket (OPS-24, OPS-31)."""
        cutoff = now - retention_days * 86400
        rows = self._db.execute(
            "SELECT b.bundle_id FROM bundles b JOIN tickets t ON t.ticket_ref = b.ticket_ref"
            " WHERE b.deleted_at IS NULL AND t.incident IS NULL AND (t.closed_at IS NOT NULL OR b.received_at <= ?)",
            (cutoff,),
        ).fetchall()
        return [r for (r,) in rows]

    # --- the access log (OPS-24) ------------------------------------------------------------------------------------

    def log(self, now: float, actor: str, action: str, target: str, allowed: bool, reason: str | None = None) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO access_log (at, actor, action, target, allowed, reason) VALUES (?, ?, ?, ?, ?, ?)",
                       (now, actor, action, target, int(allowed), reason))  # fmt: skip

    def access_log(self, target: str) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT at, actor, action, allowed, reason FROM access_log WHERE target = ?"
                                " ORDER BY id", (target,)).fetchall()  # fmt: skip
        return [{"at": at, "actor": actor, "action": action, "allowed": bool(allowed), "reason": reason}
                for at, actor, action, allowed, reason in rows]  # fmt: skip

    # --- retention ----------------------------------------------------------------------------------------------------

    def purge_reports(self, now: float, crash_days: int, telemetry_days: int) -> int:
        with self._tx() as db:
            removed = db.execute("DELETE FROM reports WHERE kind = 'crash_report' AND day < ?",
                                 (days_before(now, crash_days),)).rowcount  # fmt: skip
            removed += db.execute("DELETE FROM reports WHERE kind = 'telemetry' AND day < ?",
                                  (days_before(now, telemetry_days),)).rowcount  # fmt: skip
        return int(removed)

    def purge_access_log(self, now: float, days: int) -> int:
        with self._tx() as db:
            return int(db.execute("DELETE FROM access_log WHERE at < ?", (now - days * 86400,)).rowcount)
