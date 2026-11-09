"""The local trace store, `traces/traces.db` (OBS-08, DAT-01).

Turn records go to a queue and a writer thread stores them, so a turn never waits on the disk. A failing writer is
counted and logged by the caller's choice; it never raises into the turn. Retention is by age and by size, oldest
first: after each write, records older than the age limit go, then the oldest until the stored records fit the size
cap, and freed pages are returned to the file system. Profile deletion removes that profile's records, overwriting the
freed pages and emptying the write-ahead log (DAT-09). The store is not encrypted at rest (D68).
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
  trace_id     TEXT    PRIMARY KEY,
  profile_ref  TEXT    NOT NULL,
  session_ref  TEXT    NOT NULL,
  recorded_at  INTEGER NOT NULL,
  bytes        INTEGER NOT NULL CHECK (bytes >= 0),
  record       TEXT    NOT NULL CHECK (json_valid(record))
) STRICT;
CREATE INDEX IF NOT EXISTS traces_recorded ON traces(recorded_at);
CREATE INDEX IF NOT EXISTS traces_profile ON traces(profile_ref);
CREATE INDEX IF NOT EXISTS traces_job ON traces(json_extract(record, '$.job_name'));
"""
DAY_MS = 86_400_000
_STOP = object()


class TraceStore:
    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int,
        retention_days: int,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        queue_size: int = 1000,
    ) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.retention_days = retention_days
        self.clock_ms = clock_ms
        self.queue: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
        self.failures = 0
        self.changes = 0  # bumped by every write, prune and deletion: readers know when to read again
        self.dropped = 0
        self.last_error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="trace-writer", daemon=True)
        self._thread.start()

    # --- the turn's side: never blocks, never raises ----------------------------------------------------------------

    def submit(self, record: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1

    def flush(self, timeout: float = 5.0) -> None:
        """Wait until every submitted record has been handled (tests and shutdown)."""
        done = threading.Event()
        try:
            self.queue.put(done, timeout=timeout)
        except queue.Full:
            return
        done.wait(timeout)

    def close(self, timeout: float = 5.0) -> None:
        try:
            self.queue.put(_STOP, timeout=timeout)
        except queue.Full:
            return
        self._thread.join(timeout)

    # --- the writer thread ------------------------------------------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")  # takes effect on a new file; lets freed pages go back
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA secure_delete=ON")
        conn.executescript(SCHEMA)
        return conn

    def _run(self) -> None:
        conn: sqlite3.Connection | None = None
        while True:
            item = self.queue.get()
            if item is _STOP:
                break
            if isinstance(item, threading.Event):
                item.set()
                continue
            try:
                conn = conn or self._open()
                self._write(conn, item)
                self.changes += 1
            except Exception as error:
                self.failures += 1
                self.last_error = error
                if conn is not None:
                    conn.close()
                conn = None
        if conn is not None:
            conn.close()

    def _write(self, conn: sqlite3.Connection, record: dict[str, Any]) -> None:
        text = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        now = self.clock_ms()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT OR REPLACE INTO traces (trace_id, profile_ref, session_ref, recorded_at, bytes, record)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (record["trace_id"], record.get("profile_ref", ""), record.get("session_ref", ""), now,
                 len(text.encode()), text),
            )  # fmt: skip
            self._retain(conn, now)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("PRAGMA incremental_vacuum")

    def _retain(self, conn: sqlite3.Connection, now: int) -> None:
        conn.execute("DELETE FROM traces WHERE recorded_at <= ?", (now - self.retention_days * DAY_MS,))
        total = conn.execute("SELECT coalesce(sum(bytes), 0) FROM traces").fetchone()[0]
        if total <= self.max_bytes:
            return
        excess = total - self.max_bytes
        # Oldest first: drop the shortest prefix (by time, then id) whose bytes cover the excess.
        conn.execute(
            "DELETE FROM traces WHERE trace_id IN (SELECT trace_id FROM (SELECT trace_id, bytes, sum(bytes) OVER"
            " (ORDER BY recorded_at, trace_id) AS running FROM traces) WHERE running - bytes < ?)",
            (excess,),
        )

    # --- reads and profile deletion (on their own connection) -------------------------------------------------------

    def _reader(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._reader() as conn:
            return [json.loads(r[0]) for r in conn.execute("SELECT record FROM traces ORDER BY recorded_at, trace_id")]

    def timings_since(self, after_ms: int) -> list[dict[str, Any]]:
        """The timing fields of the turns recorded after `after_ms`, read in SQL so the health panel never loads
        whole records (OBS-15): kind, mode, status, duration, the first model call phase's start offset and the
        first Coach call's time to first token."""
        if not self.path.exists():
            return []
        query = (
            "SELECT json_extract(record, '$.kind'), json_extract(record, '$.mode'), json_extract(record, '$.status'),"
            " json_extract(record, '$.duration_ms'),"
            " (SELECT min(json_extract(p.value, '$.start_offset_ms')) FROM json_each(record, '$.phases') AS p"
            "  WHERE json_extract(p.value, '$.phase') = 'call'),"
            " (SELECT json_extract(c.value, '$.latency.ttft_ms') FROM json_each(record, '$.model_calls') AS c"
            "  WHERE json_extract(c.value, '$.role') = 'coach' ORDER BY c.key LIMIT 1)"
            " FROM traces WHERE recorded_at > ?"
            " AND coalesce(json_extract(record, '$.job_name'), '') != 'capture' ORDER BY recorded_at"
        )
        names = ("kind", "mode", "status", "duration_ms", "first_call_offset_ms", "coach_ttft_ms")
        with self._reader() as conn:
            return [dict(zip(names, row, strict=True)) for row in conn.execute(query, (after_ms,))]

    def perception_summary(self) -> list[dict[str, Any]]:
        """The local health panel reads aggregate metadata in SQL, never whole turn/capture records."""
        if not self.path.exists():
            return []
        query = (
            "SELECT json_extract(record, '$.perception.reader.hardware_class'), count(*),"
            " sum(json_extract(record, '$.perception.outcome') = 'confirmed'),"
            " sum(json_extract(record, '$.perception.confirm_method') = 'edited'),"
            " sum(json_extract(record, '$.perception.outcome') = 'unreadable')"
            " FROM traces WHERE json_extract(record, '$.job_name') = 'capture'"
            " AND json_extract(record, '$.perception.reader.hardware_class') IN"
            " ('content_only', 'low', 'mid', 'high', 'server') GROUP BY 1 ORDER BY 1"
        )
        names = ("hardware_class", "captures", "confirmed", "edited", "unreadable")
        with self._reader() as conn:
            return [dict(zip(names, row, strict=True)) for row in conn.execute(query)]

    def stored_bytes(self) -> int:
        if not self.path.exists():
            return 0
        with self._reader() as conn:
            return int(conn.execute("SELECT coalesce(sum(bytes), 0) FROM traces").fetchone()[0])

    def prune(self, now_ms: int) -> None:
        """Apply the age and size limits without a new record: the retention job's pass over the store (DAT-08)."""
        if not self.path.exists():
            return
        conn = self._reader()
        try:
            conn.execute("PRAGMA secure_delete=ON")
            with conn:
                self._retain(conn, now_ms)
            self.changes += 1
            conn.execute("PRAGMA incremental_vacuum")
        finally:
            conn.close()

    def delete_profile(self, profile_ref: str) -> None:
        self.flush()  # profile closing quiesces producers; drain queued observations before erasing their rows
        if not self.path.exists():
            return
        conn = self._reader()
        try:
            conn.execute("PRAGMA secure_delete=ON")
            with conn:
                conn.execute("DELETE FROM traces WHERE profile_ref = ?", (profile_ref,))
            self.changes += 1
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
