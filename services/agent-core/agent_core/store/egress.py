"""The egress ledger and the outbox as stored in `device.db` (ARC-09, ARC-10, DAT-11).

The ledger keeps one row per request that left the device: time, feature, host, bytes and payload class, never the
payload. Retention is the store's (DAT-08). The outbox keeps queued online work with its idempotency key, size and
expiry; turning a feature off purges its payloads except tombstones and consent withdrawals, which are still owed to
destinations that hold data (DAT-11 over ARC-09). The decisions on the one-time egress notices of ARC-10 live in
`app_settings`, per profile (device features under `device`)."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from agent_core.store.events import iso_ms
from agent_core.store.ids import new_ulid
from agent_core.store.sqlite import transaction

KEPT_KINDS = ("tombstone", "consent_withdrawal")  # never purged by a switch, delivered to every holder (DAT-11)
FIRST_USE_KEY = "egress_first_use"
DEVICE = "device"


@contextlib.contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[None]:
    """One write transaction, or the caller's when one is open (the connections are autocommit)."""
    if conn.in_transaction:
        yield
        return
    with transaction(conn):
        yield


# --- the egress ledger (ARC-10) ------------------------------------------------------------------------------------


def ledger_record(device: sqlite3.Connection, at_ms: int, feature: str, destination: str, size: int,
                  payload_class: str, profile_id: str | None = None) -> str:  # fmt: skip
    ledger_id = new_ulid(at_ms)
    with _atomic(device):
        device.execute(
            "INSERT INTO egress_ledger (ledger_id, at, feature, destination, bytes, payload_class, profile_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ledger_id, at_ms, feature, destination, max(0, int(size)), payload_class, profile_id),
        )
    return ledger_id


def ledger_rows(device: sqlite3.Connection, since_ms: int, profile_ids: tuple[str, ...] | None = None,
                limit: int = 5000) -> list[dict[str, Any]]:  # fmt: skip
    """Newest first. `profile_ids` keeps device rows (no profile) and those profiles' rows; None keeps every row."""
    sql = "SELECT at, feature, destination, bytes, payload_class FROM egress_ledger WHERE at >= ?"
    args: list[Any] = [since_ms]
    if profile_ids is not None:
        marks = ",".join("?" * len(profile_ids))
        sql += f" AND (profile_id IS NULL{f' OR profile_id IN ({marks})' if profile_ids else ''})"
        args += list(profile_ids)
    rows = device.execute(sql + " ORDER BY at DESC, ledger_id DESC LIMIT ?", (*args, limit)).fetchall()
    return [{"at": r[0], "feature": r[1], "destination": r[2], "bytes": r[3], "payload_class": r[4]} for r in rows]


def last_sent(device: sqlite3.Connection) -> dict[str, int]:
    """The time each feature last sent anything (UXS-06 popover, PRV-28)."""
    rows = device.execute("SELECT feature, max(at) FROM egress_ledger WHERE bytes > 0 GROUP BY feature").fetchall()
    return {str(f): int(at) for f, at in rows}


# --- the outbox (ARC-09, DAT-11) ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class OutboxItem:
    outbox_id: str
    idempotency_key: str
    kind: str
    feature: str | None
    profile_id: str | None
    destination: str
    payload: bytes
    size: int
    created_at: int
    attempts: int
    next_attempt_at: int | None
    expires_at: int


_COLUMNS = ("outbox_id, idempotency_key, kind, feature, profile_id, destination, payload, bytes, created_at, attempts,"
            " next_attempt_at, expires_at")  # fmt: skip


def _item(row: tuple[Any, ...]) -> OutboxItem:
    return OutboxItem(str(row[0]), str(row[1]), str(row[2]), row[3], row[4], str(row[5]), bytes(row[6]), int(row[7]),
                      int(row[8]), int(row[9]), row[10], int(row[11]))  # fmt: skip


def enqueue(device: sqlite3.Connection, *, kind: str, feature: str, destination: str, payload: bytes, now_ms: int,
            expires_ms: int, idempotency_key: str | None = None, profile_id: str | None = None) -> str:  # fmt: skip
    """Write the work durably before the action reports done (ARC-09). A repeated idempotency key keeps the first."""
    outbox_id = new_ulid(now_ms)
    key = idempotency_key or f"{kind}:{outbox_id}"
    with _atomic(device):
        device.execute(
            "INSERT INTO outbox (outbox_id, idempotency_key, kind, feature, profile_id, destination, payload, bytes,"
            " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (idempotency_key) DO NOTHING",
            (outbox_id, key, kind, feature, profile_id, destination, payload, len(payload), now_ms, expires_ms),
        )
    return outbox_id


def pending(device: sqlite3.Connection, feature: str | None = None) -> list[OutboxItem]:
    """Undelivered items in order (per feature: oldest first)."""
    sql = f"SELECT {_COLUMNS} FROM outbox WHERE delivered_at IS NULL"  # noqa: S608 (fixed column list)
    args: tuple[Any, ...] = ()
    if feature is not None:
        sql, args = sql + " AND feature = ?", (feature,)
    return [_item(r) for r in device.execute(sql + " ORDER BY created_at, outbox_id", args).fetchall()]


def counts(device: sqlite3.Connection) -> dict[str, int]:
    rows = device.execute("SELECT feature, count(*) FROM outbox WHERE delivered_at IS NULL AND feature IS NOT NULL"
                          " GROUP BY feature").fetchall()  # fmt: skip
    return {str(f): int(n) for f, n in rows}


def total_bytes(device: sqlite3.Connection) -> int:
    row = device.execute("SELECT coalesce(sum(bytes), 0) FROM outbox WHERE delivered_at IS NULL").fetchone()
    return int(row[0])


def delivered(device: sqlite3.Connection, outbox_id: str, now_ms: int) -> None:
    with _atomic(device):
        device.execute("UPDATE outbox SET delivered_at = ?, payload = x'' WHERE outbox_id = ?", (now_ms, outbox_id))


def failed(device: sqlite3.Connection, outbox_id: str, next_attempt_ms: int) -> None:
    with _atomic(device):
        device.execute("UPDATE outbox SET attempts = attempts + 1, next_attempt_at = ? WHERE outbox_id = ?",
                       (next_attempt_ms, outbox_id))  # fmt: skip


def drop(device: sqlite3.Connection, outbox_id: str) -> None:
    with _atomic(device):
        device.execute("DELETE FROM outbox WHERE outbox_id = ?", (outbox_id,))


def purge_feature(device: sqlite3.Connection, feature: str) -> int:
    """A feature switched off: its queued payloads go, its tombstones and consent withdrawals stay (DAT-11)."""
    marks = ",".join("?" * len(KEPT_KINDS))
    with _atomic(device):
        return int(device.execute(
            f"DELETE FROM outbox WHERE feature = ? AND delivered_at IS NULL AND kind NOT IN ({marks})",  # noqa: S608
            (feature, *KEPT_KINDS),
        ).rowcount)  # fmt: skip


# --- the one-time egress notices (ARC-10) --------------------------------------------------------------------------


def first_use(device: sqlite3.Connection, profile_id: str | None) -> dict[str, str]:
    """feature -> "accepted" | "declined" for one profile (or the device's features)."""
    row = device.execute("SELECT value FROM app_settings WHERE key = ?", (FIRST_USE_KEY,)).fetchone()
    data = json.loads(row[0]) if row else {}
    found = data.get(profile_id or DEVICE, {})
    return {str(k): str(v) for k, v in found.items()} if isinstance(found, dict) else {}


def record_first_use(device: sqlite3.Connection, profile_id: str | None, feature: str, accepted: bool,
                     now_ms: int) -> None:  # fmt: skip
    with _atomic(device):
        row = device.execute("SELECT value FROM app_settings WHERE key = ?", (FIRST_USE_KEY,)).fetchone()
        data = json.loads(row[0]) if row else {}
        data.setdefault(profile_id or DEVICE, {})[feature] = "accepted" if accepted else "declined"
        device.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET"
            " value = excluded.value, updated_at = excluded.updated_at",
            (FIRST_USE_KEY, json.dumps(data, sort_keys=True), now_ms),
        )


def forget_first_use(device: sqlite3.Connection, profile_id: str, now_ms: int) -> None:
    """A deleted profile leaves no decision behind."""
    with _atomic(device):
        row = device.execute("SELECT value FROM app_settings WHERE key = ?", (FIRST_USE_KEY,)).fetchone()
        if row is None:
            return
        data = json.loads(row[0])
        if data.pop(profile_id, None) is not None:
            device.execute("UPDATE app_settings SET value = ?, updated_at = ? WHERE key = ?",
                           (json.dumps(data, sort_keys=True), now_ms, FIRST_USE_KEY))  # fmt: skip


# --- metered cloud spend (MDL-29) ----------------------------------------------------------------------------------

SPEND_KEY = "cloud_spend"


@dataclass
class DeviceSpend:
    """A profile's cloud spend, kept on the device so budgets hold offline: USD per day and tokens per billing period,
    the allowance month the period's name gives (`subscription.allowance_period`, D33): a new month starts at zero.
    Only the most recent `keep_days` days are kept."""

    device: sqlite3.Connection
    profile_id: str
    period: str | Callable[[], str]
    now_ms: Callable[[], int]
    keep_days: int = 31

    def _all(self) -> dict[str, Any]:
        row = self.device.execute("SELECT value FROM app_settings WHERE key = ?", (SPEND_KEY,)).fetchone()
        data = json.loads(row[0]) if row else {}
        return data if isinstance(data, dict) else {}

    def _mine(self) -> dict[str, Any]:
        mine = self._all().get(self.profile_id)
        return mine if isinstance(mine, dict) else {}

    def day_usd(self, day: str) -> float:
        return float(self._mine().get("days", {}).get(day, 0.0))

    def period_tokens(self) -> int:
        mine = self._mine()
        period = self.period() if callable(self.period) else self.period
        return int(mine.get("tokens", 0)) if mine.get("period") == period else 0

    def add(self, day: str, usd: float, tokens: int) -> None:
        with _atomic(self.device):
            data = self._all()
            found = data.get(self.profile_id)
            mine: dict[str, Any] = found if isinstance(found, dict) else {}
            days = dict(sorted(mine.get("days", {}).items())[-self.keep_days :])
            days[day] = float(days.get(day, 0.0)) + usd
            period = self.period() if callable(self.period) else self.period
            earlier = int(mine.get("tokens", 0)) if mine.get("period") == period else 0
            data[self.profile_id] = {**mine, "days": days, "period": period, "tokens": earlier + int(tokens)}
            self.device.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET"
                " value = excluded.value, updated_at = excluded.updated_at",
                (SPEND_KEY, json.dumps(data, sort_keys=True), self.now_ms()),
            )

    def add_study(self, receipt: str, usd: float, tokens: int) -> None:
        """Content-free replay receipts and spend update atomically in the existing profile-scoped spend record."""
        with _atomic(self.device):
            data = self._all()
            mine = data.get(self.profile_id, {})
            seen = {key: at for key, at in mine.get("study_receipts", {}).items() if at > self.now_ms() - 86400000}
            if receipt in seen:
                return
            # The gateway's replay window is 15 minutes; keep a full day and bound admission rather than
            # dropping a live receipt that could be billed twice.
            if len(seen) >= 10000:
                raise ValueError("study usage receipt capacity reached")
            seen[receipt] = self.now_ms()
            self.add(iso_ms(self.now_ms())[:10], usd, tokens)
            data = self._all()
            data[self.profile_id]["study_receipts"] = seen
            self.device.execute(
                "UPDATE app_settings SET value=?,updated_at=? WHERE key=?",
                (json.dumps(data, sort_keys=True), self.now_ms(), SPEND_KEY),
            )
