"""Integrity at open, recovery of a damaged learner database, and the disk-low rule (DAT-13).

Every database is checked with `PRAGMA quick_check` when it opens. A damaged learner database is never deleted: it is
renamed beside the original (`learner.db.damaged-<ms>`, with its WAL and SHM), then the newest migration backup is
restored, or, failing that, a fresh database receives every event and profile row that can still be read, and the
caller rebuilds the projections from those events. The outcome is kept so the adult can be told what was lost."""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from agent_core.store.sqlite import Applied, NewerDatabaseError, open_database

KEPT_TABLES = ("meta", "profile", "settings", "consents", "events")  # copied from a damaged file when readable


class CorruptDatabaseError(RuntimeError):
    """`PRAGMA quick_check` failed, or the file is not a database."""


@dataclass(frozen=True)
class Recovery:
    method: str  # "backup" or "events" (a learner database), "reindex" (the device database, UXS-47)
    damaged_file: str
    events_kept: int
    rows_lost: bool  # some table or row could not be read
    needs_rebuild: bool  # the caller rebuilds projections from the kept events


def quick_check(path: Path) -> bool:
    """False when the file is damaged or is not a database. A file another process holds locked is not called damaged:
    a lock says nothing about the pages, and a healthy file must never be set aside (UXS-47)."""
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as error:
        return "locked" in str(error) or "busy" in str(error)
    except sqlite3.DatabaseError:
        return False
    return rows == [("ok",)]


def full_check(conn: sqlite3.Connection) -> bool:
    """The weekly background `integrity_check` (DAT-13)."""
    try:
        return conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    except sqlite3.DatabaseError:
        return False


def open_checked(path: Path, kind: str) -> tuple[sqlite3.Connection, list[Applied], Recovery | None]:
    """Open with the integrity check; a damaged learner database is recovered first (never deleted)."""
    recovery = None
    if path.exists() and path.stat().st_size > 0 and not quick_check(path):
        if kind != "learner":
            raise CorruptDatabaseError(f"{path.name} failed its integrity check")
        recovery = recover(path)
    conn, applied = open_database(path, kind)
    return conn, list(applied), recovery


def set_aside(path: Path) -> Path:
    """Rename a damaged database beside the original (`<name>.damaged-<ms>`, with its WAL and SHM); never delete it."""
    damaged = path.with_name(f"{path.name}.damaged-{_now_ms()}")
    for suffix in ("", "-wal", "-shm"):
        source = path.with_name(path.name + suffix)
        if source.exists():
            source.rename(damaged.with_name(damaged.name + suffix))
    return damaged


def recover(path: Path) -> Recovery:
    damaged = set_aside(path)
    backups = sorted(path.parent.glob(f"{path.name}.pre-v*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)
    for backup in backups:
        if quick_check(backup):
            shutil.copyfile(backup, path)
            return Recovery("backup", damaged.name, _count(path), rows_lost=True, needs_rebuild=False)
    conn, _ = open_database(path, "learner")
    kept = 0
    try:
        old = sqlite3.connect(f"{damaged.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        conn.close()
        return Recovery("events", damaged.name, 0, rows_lost=True, needs_rebuild=True)
    try:
        for table in KEPT_TABLES:
            try:
                cursor = old.execute(f"SELECT * FROM {table}")  # noqa: S608 - fixed table names
                names = [d[0] for d in cursor.description]
            except sqlite3.DatabaseError:
                continue
            marks = ", ".join("?" for _ in names)
            while True:
                try:
                    row = cursor.fetchone()
                except sqlite3.DatabaseError:
                    break  # the rest of this table sits on damaged pages
                if row is None:
                    break
                try:
                    conn.execute(f"INSERT OR IGNORE INTO {table} ({', '.join(names)}) VALUES ({marks})", row)  # noqa: S608
                    kept += table == "events"
                except sqlite3.DatabaseError:
                    continue
    finally:
        old.close()
        conn.close()
    # Transcripts, notes and checkpoints are not carried over from a damaged file, so something is always lost.
    return Recovery("events", damaged.name, kept, rows_lost=True, needs_rebuild=True)


def _count(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return int(conn.execute("SELECT count(*) FROM events").fetchone()[0])
    except sqlite3.DatabaseError:
        return 0
    finally:
        conn.close()


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def disk_low(path: Path, free_floor_mb: float) -> bool:
    """Free space under the floor (DAT-13): attachments and traces stop first; events and checkpoints go on."""
    return shutil.disk_usage(path).free < free_floor_mb * 1024 * 1024


__all__ = ["CorruptDatabaseError", "NewerDatabaseError", "Recovery", "disk_low", "full_check", "open_checked",
           "quick_check", "recover", "set_aside"]  # fmt: skip
