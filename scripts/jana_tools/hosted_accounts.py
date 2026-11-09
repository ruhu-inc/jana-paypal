"""Sign-in for the hosted demo: accounts, password hashes and sessions, in one SQLite file beside the learners' data.

Each account owns one member slot, so one account's learners never meet another's. Only digests are stored: a password
is kept as a salted scrypt hash and a session as the SHA-256 of its token. There is no email delivery, so an address is
not proven by a password sign-up: the invite code is what keeps strangers out.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s.]{1,63}(\.[^@\s.]{1,63})+$")
MIN_PASSWORD = 10
MAX_PASSWORD = 200
SESSION_SECONDS = 14 * 86400
_N, _R, _P = 2**14, 8, 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  account_id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT,
  google_sub TEXT UNIQUE,
  slot TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS sessions (
  token_digest TEXT PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
  expires_at INTEGER NOT NULL
) STRICT;
"""


class AccountError(ValueError):
    """A refusal with a code the page words: email_taken, no_space, invalid_email, weak_password."""


@dataclass(frozen=True)
class Account:
    account_id: int
    email: str
    slot: str
    has_password: bool
    google: bool


def normalise_email(email: str) -> str:
    cleaned = email.strip().lower()
    if not EMAIL.fullmatch(cleaned) or len(cleaned) > 254:
        raise AccountError("invalid_email")
    return cleaned


def hash_password(password: str) -> str:
    if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise AccountError("weak_password")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(derived).decode()}"


# Checked when an address is unknown, so a wrong address takes as long as a wrong password.
_DUMMY = hash_password("not-a-real-password-for-timing")


def verify_password(password: str, stored: str | None) -> bool:
    known = stored is not None
    _, n, r, p, salt, expected = (stored or _DUMMY).split("$")
    derived = hashlib.scrypt(
        password[:MAX_PASSWORD].encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32
    )
    return hmac.compare_digest(derived, base64.b64decode(expected)) and known


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Accounts:
    def __init__(self, path: Path, slots: Iterable[str]) -> None:
        self.path, self.slots = path, list(slots)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._open() as conn:
            conn.executescript(SCHEMA)
        path.chmod(0o600)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def create(self, email: str, password_hash: str | None = None, google_sub: str | None = None) -> Account:
        address = normalise_email(email)
        with self._open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("SELECT 1 FROM accounts WHERE email=?", (address,)).fetchone():
                    raise AccountError("email_taken")
                taken = {row[0] for row in conn.execute("SELECT slot FROM accounts")}
                free = next((slot for slot in self.slots if slot not in taken), None)
                if free is None:
                    raise AccountError("no_space")
                conn.execute(
                    "INSERT INTO accounts (email,password_hash,google_sub,slot,created_at) VALUES (?,?,?,?,?)",
                    (address, password_hash, google_sub, free, int(time.time())),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        found = self.by_email(address)
        assert found is not None
        return found

    @staticmethod
    def _account(row: sqlite3.Row) -> Account:
        return Account(row["account_id"], row["email"], row["slot"], row["password_hash"] is not None,
                       row["google_sub"] is not None)  # fmt: skip

    def by_email(self, email: str) -> Account | None:
        with self._open() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE email=?", (email.strip().lower(),)).fetchone()
        return self._account(row) if row else None

    def by_google(self, sub: str) -> Account | None:
        with self._open() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE google_sub=?", (sub,)).fetchone()
        return self._account(row) if row else None

    def link_google(self, account_id: int, sub: str) -> None:
        with self._open() as conn:
            conn.execute("UPDATE accounts SET google_sub=? WHERE account_id=?", (sub, account_id))

    def password_hash(self, email: str) -> str | None:
        with self._open() as conn:
            row = conn.execute("SELECT password_hash FROM accounts WHERE email=?", (email.strip().lower(),)).fetchone()
        return row[0] if row else None

    def set_password(self, email: str, password_hash: str) -> bool:
        """Sets a new password and ends every session of that account."""
        with self._open() as conn:
            row = conn.execute("SELECT account_id FROM accounts WHERE email=?", (email.strip().lower(),)).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE accounts SET password_hash=? WHERE account_id=?", (password_hash, row[0]))
            conn.execute("DELETE FROM sessions WHERE account_id=?", (row[0],))
        return True

    def start_session(self, account_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self._open() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at<?", (now,))
            conn.execute("INSERT INTO sessions VALUES (?,?,?)", (_digest(token), account_id, now + SESSION_SECONDS))
        return token

    def from_session(self, token: str | None) -> Account | None:
        if not token:
            return None
        with self._open() as conn:
            row = conn.execute(
                "SELECT a.* FROM sessions s JOIN accounts a USING (account_id) "
                "WHERE s.token_digest=? AND s.expires_at>=?",
                (_digest(token), int(time.time())),
            ).fetchone()
        return self._account(row) if row else None

    def end_session(self, token: str | None) -> None:
        if token:
            with self._open() as conn:
                conn.execute("DELETE FROM sessions WHERE token_digest=?", (_digest(token),))

    def listed(self) -> list[Account]:
        with self._open() as conn:
            return [self._account(row) for row in conn.execute("SELECT * FROM accounts ORDER BY account_id")]


class Attempts:
    """Slows guessing: at most `limit` tries per key in `window` seconds. Kept in memory, so a restart clears it."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit, self.window = limit, window
        self.seen: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        times = self.seen[key]
        while times and moment - times[0] > self.window:
            times.popleft()
        if len(times) >= self.limit:
            return False
        times.append(moment)
        return True
