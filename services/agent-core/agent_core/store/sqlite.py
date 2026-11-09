"""Opening and migrating the SQLite stores (DAT-03, DAT-12).

Every connection gets the contract's pragmas. A database's schema version is `PRAGMA user_version`: it lives in the
file header, so it changes in the same transaction as the migration that sets it. Migrations are forward-only and
numbered; each runs as one transaction, statement by statement (a script runner that commits between statements
would leave half a migration behind). Before migrating an existing file the runner writes a copy beside it and keeps
it until the next successful open. A file newer than this app is refused and left untouched. Encryption at rest
(SQLCipher, DAT-02) arrives in M10; until then the copies are plain like the databases they copy.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_core.store.migration_v2 import SQL as V2_SQL
from agent_core.store.migration_v3 import LEARNER as V3_LEARNER
from agent_core.store.migration_v4 import LEARNER as V4_LEARNER
from agent_core.store.migration_v5 import LEARNER as V5_LEARNER
from agent_core.store.migration_v6 import LEARNER as V6_LEARNER
from agent_core.store.migration_v7 import LEARNER as V7_LEARNER
from agent_core.store.migration_v8 import LEARNER as V8_LEARNER
from agent_core.store.migration_v9 import LEARNER as V9_LEARNER
from agent_core.store.migration_v10 import LEARNER as V10_LEARNER
from agent_core.store.migration_v11 import LEARNER as V11_LEARNER
from agent_core.store.migration_v12 import LEARNER as V12_LEARNER
from agent_core.store.migration_v13 import LEARNER as V13_LEARNER
from agent_core.store.migration_v14 import LEARNER as V14_LEARNER
from agent_core.store.migration_v15 import LEARNER as V15_LEARNER
from agent_core.store.migration_v16 import LEARNER as V16_LEARNER
from agent_core.store.migration_v17 import LEARNER as V17_LEARNER
from agent_core.store.migration_v18 import LEARNER as V18_LEARNER
from agent_core.store.migration_v19 import LEARNER as V19_LEARNER
from agent_core.store.migration_v20 import LEARNER as V20_LEARNER
from agent_core.store.migration_v21 import DEVICE as V3_DEVICE
from agent_core.store.migration_v21 import LEARNER as V21_LEARNER
from agent_core.store.migration_v22 import LEARNER as V22_LEARNER
from agent_core.store.migration_v23 import LEARNER as V23_LEARNER
from agent_core.store.schema_v1 import DATABASES as V1_DATABASES

# The contract's pragmas (DAT-03). Recursive triggers make a REPLACE conflict fire the delete triggers, so
# `INSERT OR REPLACE` cannot rewrite an event past the append-only guard.
PRAGMAS = ("journal_mode=WAL", "synchronous=FULL", "foreign_keys=ON", "secure_delete=ON", "recursive_triggers=ON")
NEWER_MESSAGE_KEY = "profile.newer_app"


@dataclass(frozen=True)
class Migration:
    version: int
    sql: str
    # The migration edits a stored definition through `writable_schema` (a CHECK that only widens): the runner bumps
    # the schema version in the same transaction so every connection reads the new definition.
    edits_schema: bool = False


class NewerDatabaseError(RuntimeError):
    """The file was written by a newer app. Nothing is opened or changed (DAT-12)."""

    message_key = NEWER_MESSAGE_KEY


class MigrationError(RuntimeError):
    """A migration failed; the database is still at its previous version and usable."""


def statements(sql: str) -> list[str]:
    """Split a script into complete statements (triggers keep their BEGIN … END bodies)."""
    out: list[str] = []
    current = ""
    for line in sql.splitlines(keepends=True):
        current += line
        if sqlite3.complete_statement(current):
            if current.strip():
                out.append(current.strip())
            current = ""
    if current.strip():
        raise ValueError("incomplete statement at the end of the script")
    return out


MIGRATIONS: dict[str, tuple[Migration, ...]] = {
    kind: (Migration(1, sql), Migration(2, V2_SQL[kind])) for kind, sql in V1_DATABASES.items()
}
MIGRATIONS["learner"] = (
    *MIGRATIONS["learner"],
    Migration(3, V3_LEARNER),
    Migration(4, V4_LEARNER),
    Migration(5, V5_LEARNER),
    Migration(6, V6_LEARNER),
    Migration(7, V7_LEARNER),
    Migration(8, V8_LEARNER),
    Migration(9, V9_LEARNER),
    Migration(10, V10_LEARNER),
    Migration(11, V11_LEARNER),
    Migration(12, V12_LEARNER),
    Migration(13, V13_LEARNER),
    Migration(14, V14_LEARNER),
    Migration(15, V15_LEARNER),
    Migration(16, V16_LEARNER),
    Migration(17, V17_LEARNER),
    Migration(18, V18_LEARNER),
    Migration(19, V19_LEARNER, edits_schema=True),
    Migration(20, V20_LEARNER),
    Migration(21, V21_LEARNER, edits_schema=True),
    Migration(22, V22_LEARNER),
    Migration(23, V23_LEARNER),
)
MIGRATIONS["device"] = (*MIGRATIONS["device"], Migration(3, V3_DEVICE, edits_schema=True))


def connect(path: Path | str) -> sqlite3.Connection:
    """A connection with the contract's pragmas, in autocommit mode (transactions are explicit)."""
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    for pragma in PRAGMAS:
        conn.execute(f"PRAGMA {pragma}")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def recorded_version(path: Path) -> int:
    """The highest version the file claims, read without changing it (no pragmas, read-only).

    Without a write-ahead log the file is read as immutable, which creates no lock or index files beside it."""
    wal = path.with_name(f"{path.name}-wal")
    mode = "mode=ro" if wal.exists() else "immutable=1"
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?{mode}", uri=True)
    try:
        version = schema_version(conn)
        for table in ("meta", "device"):
            try:
                row = conn.execute(f"SELECT schema_version FROM {table}").fetchone()  # noqa: S608 - fixed names
            except sqlite3.OperationalError:
                continue
            if row is not None:
                version = max(version, int(row[0]))
        return version
    finally:
        conn.close()


def backup_path(path: Path, version: int) -> Path:
    return path.with_name(f"{path.name}.pre-v{version}.bak")


@dataclass(frozen=True)
class Applied:
    version: int
    backup_file: str | None


def open_database(
    path: Path,
    kind: str,
    migrations: Sequence[Migration] | None = None,
) -> tuple[sqlite3.Connection, list[Applied]]:
    """Open `path` as a `kind` database, migrating it to the latest known version.

    Returns the connection and the migrations applied, for the caller to record in the device's `migrations`."""
    steps = tuple(sorted(migrations if migrations is not None else MIGRATIONS[kind], key=lambda m: m.version))
    latest = steps[-1].version
    existed = path.exists() and path.stat().st_size > 0
    current = recorded_version(path) if existed else 0
    if current > latest:
        raise NewerDatabaseError(f"{path.name} has schema version {current}; this app knows {latest}")
    conn = connect(path)
    pending = [m for m in steps if m.version > current]
    backup: str | None = None
    if pending and current > 0:
        target = backup_path(path, current)
        copy = sqlite3.connect(str(target))
        with copy:
            conn.backup(copy)
        copy.close()
        backup = target.name
    applied: list[Applied] = []
    for migration in pending:
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in statements(migration.sql):
                conn.execute(statement)
            if migration.edits_schema:
                bumped = int(conn.execute("PRAGMA schema_version").fetchone()[0]) + 1
                conn.execute(f"PRAGMA schema_version = {bumped}")
            for table in ("meta", "device"):
                if _has_table(conn, table):
                    conn.execute(f"UPDATE {table} SET schema_version = ?", (migration.version,))  # noqa: S608
            conn.execute(f"PRAGMA user_version = {int(migration.version)}")
            conn.execute("COMMIT")
        except sqlite3.Error as error:
            conn.execute("ROLLBACK")
            conn.close()
            raise MigrationError(f"migration {migration.version} of {path.name} failed: {error}") from error
        applied.append(Applied(migration.version, backup))
    if not pending:
        for stale in path.parent.glob(f"{path.name}.pre-v*.bak"):
            stale.unlink()  # the copy is kept until the next successful open
    return conn, applied


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (name,)).fetchone()
    return row is not None


def record_migrations(
    device: sqlite3.Connection, kind: str, db_file: str, applied: Sequence[Applied], app_version: str, now_ms: int
) -> None:
    """The device database's ledger of every migration on this installation (DAT-12)."""
    with transaction(device):
        for step in applied:
            device.execute(
                "INSERT INTO migrations (db_kind, db_file, version, applied_at, app_version, backup_file)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (kind, db_file, step.version, now_ms, app_version, step.backup_file),
            )


class transaction:  # used as a statement: `with transaction(conn):`
    """An explicit write transaction on an autocommit connection; rolls back on any exception."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, kind: object, *_: object) -> None:
        self.conn.execute("COMMIT" if kind is None else "ROLLBACK")
