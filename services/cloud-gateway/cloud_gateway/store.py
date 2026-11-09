"""The gateway's only storage: device credentials, short-lived auth state and per-request metadata (SEC-21, OBS-22).

There is no column for a prompt, a completion, an IP address, a session pseudonym or a provider key, and no code
path writes one. Access tokens and nonces are stored as SHA-256 digests. Request metadata carries a device hash that
rotates every day (an HMAC of the subscription the credential belongs to under that day's random key; the key of
every earlier day is deleted at rollover, so requests cannot be linked across days) and is purged after the retention
window. Credentials are bound to a subscription, its paid period and its seats (BIZ-07, D70), so the gateway can hold
the subscription to its monthly allowance (D33)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    credential_id TEXT PRIMARY KEY,
    public_key BLOB NOT NULL,
    licence_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    revoked_at INTEGER,
    subscription_id TEXT,
    expires_at REAL,
    period_start REAL,
    seats INTEGER
);
CREATE TABLE IF NOT EXISTS challenges (
    nonce_sha256 BLOB PRIMARY KEY,
    credential_id TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token_sha256 BLOB PRIMARY KEY,
    credential_id TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS day_keys (
    day TEXT PRIMARY KEY,
    key BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    at INTEGER NOT NULL,
    day TEXT NOT NULL,
    device_hash TEXT NOT NULL,
    licence_id TEXT NOT NULL,
    feature TEXT NOT NULL,
    model TEXT NOT NULL,
    tokens_in INTEGER NOT NULL,
    tokens_out INTEGER NOT NULL,
    tokens_cached INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    status INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    cost_usd REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS allowance_spend (
    account_sha256 BLOB NOT NULL,
    month_start INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    PRIMARY KEY (account_sha256, month_start)
);
CREATE INDEX IF NOT EXISTS requests_day ON requests (day, licence_id, device_hash);
CREATE INDEX IF NOT EXISTS credentials_subscription ON credentials (subscription_id);
CREATE INDEX IF NOT EXISTS requests_at ON requests (at);
"""
DB_NAME = "gateway.sqlite3"
MONTH_MAX_DAYS = 31  # the longest calendar month, for purging finished allowance months


def day_of(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")


def digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


@dataclass(frozen=True)
class Credential:
    credential_id: str
    public_key: bytes
    licence_id: str
    revoked: bool
    # The subscription the credential was registered under, and when that subscription's period ends (BIZ-07, D70):
    # a credential works only while the entitlement it was registered with is current.
    subscription_id: str | None = None
    expires_at: float | None = None
    # The paid period's start and the students it covers: the allowance months and their cap (BIZ-07, D33). None for
    # a developer environment's open registration, which has no allowance.
    period_start: float | None = None
    seats: int | None = None

    def lapsed(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at


@dataclass(frozen=True)
class RequestRecord:
    """Everything the gateway keeps about a request (OBS-22)."""

    at: float
    device_hash: str
    licence_id: str
    feature: str
    model: str
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    latency_ms: int
    status: int
    outcome: str  # "ok" or the failure reason
    cost_usd: float


class GatewayStore:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / DB_NAME
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA secure_delete=ON")  # purged rows are overwritten, not left in free pages
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(credentials)")}
        if columns and "subscription_id" not in columns:  # a store made before credentials were bound (D70)
            self._db.execute("ALTER TABLE credentials ADD COLUMN subscription_id TEXT")
            self._db.execute("ALTER TABLE credentials ADD COLUMN expires_at REAL")
        if columns and "period_start" not in columns:  # a store made before the monthly allowance (D33)
            self._db.execute("ALTER TABLE credentials ADD COLUMN period_start REAL")
            self._db.execute("ALTER TABLE credentials ADD COLUMN seats INTEGER")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
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

    # --- credentials (SEC-21) ---

    def bind_credential(self, credential_id: str, public_key: bytes, licence_id: str, subscription_id: str,
                        expires_at: float | None, now: float, limit: int, *, period_start: float | None = None,
                        seats: int | None = None) -> tuple[str, str]:  # fmt: skip
        """Register `public_key` under a subscription, in one transaction: ("revoked", "") if the key was ever
        revoked; ("renewed", id) when the key already holds a credential for the subscription (a new period moves
        its expiry); ("full", "") when the subscription already has `limit` live credentials; else ("added", id)."""
        with self._tx() as db:
            if db.execute("SELECT 1 FROM credentials WHERE public_key = ? AND revoked_at IS NOT NULL",
                          (public_key,)).fetchone():  # fmt: skip
                return "revoked", ""
            row = db.execute(
                "SELECT credential_id FROM credentials WHERE public_key = ? AND subscription_id = ?",
                (public_key, subscription_id),
            ).fetchone()
            if row is not None:
                db.execute(
                    "UPDATE credentials SET expires_at = ?, licence_id = ?, period_start = ?, seats = ?"
                    " WHERE credential_id = ?",
                    (expires_at, licence_id, period_start, seats, row[0]),
                )
                return "renewed", str(row[0])
            live = db.execute(
                "SELECT count(*) FROM credentials WHERE subscription_id = ? AND revoked_at IS NULL"
                " AND (expires_at IS NULL OR expires_at > ?)",
                (subscription_id, now),
            ).fetchone()[0]
            if live >= limit:
                return "full", ""
            db.execute(
                "INSERT INTO credentials (credential_id, public_key, licence_id, created_at, revoked_at,"
                " subscription_id, expires_at, period_start, seats) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (credential_id, public_key, licence_id, int(now), subscription_id, expires_at, period_start, seats),
            )
        return "added", credential_id

    def credential(self, credential_id: str) -> Credential | None:
        with self._lock:
            row = self._db.execute(
                "SELECT public_key, licence_id, revoked_at, subscription_id, expires_at, period_start, seats"
                " FROM credentials WHERE credential_id = ?",
                (credential_id,),
            ).fetchone()
        if row is None:
            return None
        return Credential(credential_id, row[0], row[1], row[2] is not None, row[3],
                          None if row[4] is None else float(row[4]), None if row[5] is None else float(row[5]),
                          None if row[6] is None else int(row[6]))  # fmt: skip

    def revoke(self, credential_id: str, now: float) -> bool:
        """Revoke a credential and drop its outstanding challenges and access tokens at once."""
        with self._tx() as db:
            found = db.execute(
                "UPDATE credentials SET revoked_at = COALESCE(revoked_at, ?) WHERE credential_id = ?",
                (int(now), credential_id),
            ).rowcount
            db.execute("DELETE FROM tokens WHERE credential_id = ?", (credential_id,))
            db.execute("DELETE FROM challenges WHERE credential_id = ?", (credential_id,))
        return found > 0

    def add_challenge(self, nonce: str, credential_id: str, expires_at: float) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO challenges VALUES (?, ?, ?)", (digest(nonce), credential_id, expires_at))

    def take_challenge(self, nonce: str) -> tuple[str, float] | None:
        """The challenge's credential and expiry, deleted in the same transaction: a nonce is single-use."""
        with self._tx() as db:
            row = db.execute(
                "SELECT credential_id, expires_at FROM challenges WHERE nonce_sha256 = ?", (digest(nonce),)
            ).fetchone()
            db.execute("DELETE FROM challenges WHERE nonce_sha256 = ?", (digest(nonce),))
        return (row[0], row[1]) if row else None

    def add_token(self, token: str, credential_id: str, expires_at: float) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO tokens VALUES (?, ?, ?)", (digest(token), credential_id, expires_at))

    def token(self, token: str) -> tuple[str, float] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT credential_id, expires_at FROM tokens WHERE token_sha256 = ?", (digest(token),)
            ).fetchone()
        return (row[0], row[1]) if row else None

    # --- the daily device hash (OBS-22) ---

    def device_hash(self, credential_id: str, now: float) -> str:
        day = day_of(now)
        with self._tx() as db:
            row = db.execute("SELECT key FROM day_keys WHERE day = ?", (day,)).fetchone()
            if row is None:
                db.execute("DELETE FROM day_keys WHERE day <> ?", (day,))
                key = secrets.token_bytes(32)
                db.execute("INSERT INTO day_keys VALUES (?, ?)", (day, key))
            else:
                key = row[0]
        return hmac.new(key, credential_id.encode(), hashlib.sha256).hexdigest()[:32]

    # --- request metadata (OBS-22) ---

    def record(self, r: RequestRecord) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO requests (at, day, device_hash, licence_id, feature, model, tokens_in, tokens_out, "
                "tokens_cached, latency_ms, status, outcome, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (int(r.at), day_of(r.at), r.device_hash, r.licence_id, r.feature, r.model, r.tokens_in, r.tokens_out,
                 r.tokens_cached, r.latency_ms, r.status, r.outcome, r.cost_usd),
            )  # fmt: skip

    def spend(self, day: str, *, device_hash: str | None = None, licence_id: str | None = None) -> float:
        sql = "SELECT COALESCE(SUM(cost_usd), 0) FROM requests WHERE day = ?"
        args: list[str] = [day]
        if device_hash is not None:
            sql += " AND device_hash = ?"
            args.append(device_hash)
        if licence_id is not None:
            sql += " AND licence_id = ?"
            args.append(licence_id)
        with self._lock:
            return float(self._db.execute(sql, args).fetchone()[0])

    # --- the subscription's spend in each allowance month (BIZ-07, D33) ---
    # One running total per subscription and month, keyed by a digest of the subscription: it links no request to
    # another and is deleted once the month is over.

    def add_allowance_spend(self, account: str, month_start: float, cost_usd: float) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO allowance_spend VALUES (?, ?, ?) ON CONFLICT (account_sha256, month_start)"
                " DO UPDATE SET cost_usd = cost_usd + excluded.cost_usd",
                (digest(account), int(month_start), cost_usd),
            )

    def allowance_spend(self, account: str, month_start: float) -> float:
        with self._lock:
            row = self._db.execute(
                "SELECT cost_usd FROM allowance_spend WHERE account_sha256 = ? AND month_start = ?",
                (digest(account), int(month_start)),
            ).fetchone()
        return float(row[0]) if row else 0.0

    def allowance_committed(self, account: str, month_start: float) -> tuple[float, float]:
        """Read settled spend and in-flight Study reservations in one SQLite snapshot."""
        key = digest(account), int(month_start)
        with self._lock:
            spent, held = self._db.execute(
                "SELECT coalesce((SELECT cost_usd FROM allowance_spend WHERE account_sha256=? AND month_start=?),0),"
                " coalesce((SELECT sum(reserved_usd) FROM study_receipts"
                " WHERE account=? AND month_start=? AND state='reserved'),0)", (*key, *key)
            ).fetchone()
        return float(spent), float(held)

    def trailing_daily_spend(self, licence_id: str, days: list[str]) -> tuple[float, float]:
        """For a licence over the given days: mean daily spend, and mean spend per device-day."""
        if not days:
            return 0.0, 0.0
        marks = ",".join("?" * len(days))
        with self._lock:
            total, device_days = self._db.execute(
                f"SELECT COALESCE(SUM(cost_usd), 0), COUNT(DISTINCT day || device_hash) FROM requests "  # noqa: S608
                f"WHERE licence_id = ? AND day IN ({marks})",
                (licence_id, *days),
            ).fetchone()
        return float(total) / len(days), (float(total) / device_days if device_days else 0.0)

    def outcomes_since(self, since: float) -> list[tuple[int, str, int]]:
        """(status, outcome, latency_ms) of every request since a time, for the objectives."""
        with self._lock:
            return [
                (int(s), str(o), int(lat))
                for s, o, lat in self._db.execute(
                    "SELECT status, outcome, latency_ms FROM requests WHERE at >= ?", (int(since),)
                )
            ]

    def purge(self, now: float, retention_days: int) -> int:
        """Delete metadata past retention, and expired challenges and tokens. The allowance months' running totals have
        their own horizon: they outlive the request rows (an allowance month can outlast the retention window) and go
        once their month is over, so the allowance never depends on request metadata (D33, OBS-22)."""
        with self._tx() as db:
            gone = db.execute("DELETE FROM requests WHERE at < ?", (int(now - retention_days * 86400),)).rowcount
            db.execute("DELETE FROM challenges WHERE expires_at < ?", (now,))
            # An allowance month lasts at most 31 days: older totals belong to months that are over.
            db.execute("DELETE FROM allowance_spend WHERE month_start < ?", (int(now - MONTH_MAX_DAYS * 86400),))
            db.execute("DELETE FROM tokens WHERE expires_at < ?", (now,))
        return gone
