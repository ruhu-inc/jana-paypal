"""Audited cloud switches and synthetic-probe routing (OPS-12, OPS-20).

Thresholds are deployment input, exported by the release CLI from agent_core.config.keys.
"""

from __future__ import annotations

import math
import sqlite3
import threading
from collections import defaultdict, deque
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OperationsPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    probe_interval_s: int = Field(gt=0)
    window_s: int = Field(gt=0)
    failure_share: float = Field(ge=0, le=1)
    latency_multiple: float = Field(gt=0)
    latency_objective_s: float = Field(gt=0)
    healthy_s: int = Field(gt=0)
    qualified_providers: frozenset[str]
    cost_history_days: int = Field(gt=0)
    cost_multiple: float = Field(gt=0)
    metadata_retention_days: int = Field(gt=0)


class GatewayOperations:
    def __init__(
        self,
        folder: Path,
        policy: OperationsPolicy | None = None,
        *,
        alerts: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / "operations.sqlite"
        self.policy = policy
        self.alerts = alerts
        self.lock = threading.RLock()
        self.samples: dict[str, deque[tuple[float, bool, float]]] = defaultdict(deque)
        self.degraded: set[str] = set()
        self.healthy_since: dict[str, float] = {}
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS switches (kind TEXT, target TEXT, disabled INTEGER NOT NULL,
                    PRIMARY KEY(kind,target));
                CREATE TABLE IF NOT EXISTS audit (at REAL, kind TEXT, target TEXT, disabled INTEGER,
                    actor TEXT, reason TEXT, incident_id TEXT);
                CREATE TABLE IF NOT EXISTS cost (day TEXT, provider TEXT, model TEXT, role TEXT, licence_id TEXT,
                    tokens_in INTEGER, tokens_cached INTEGER, tokens_out INTEGER, cost_usd REAL,
                    PRIMARY KEY(day,provider,model,role,licence_id));
                CREATE TABLE IF NOT EXISTS alerted (day TEXT, licence_id TEXT, PRIMARY KEY(day,licence_id));
            """)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def switch(
        self, kind: str, target: str, disabled: bool, actor: str, reason: str, incident_id: str, now: float
    ) -> None:
        if (
            kind not in {"global", "provider", "model", "licence"}
            or not all(s.strip() for s in (target, actor, reason, incident_id))
            or type(disabled) is not bool
            or not math.isfinite(now)
            or (kind == "global" and target != "*")
        ):
            raise ValueError("OPS-12: invalid switch or audit fields")
        with self.lock, closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO audit VALUES (?,?,?,?,?,?,?)", (now, kind, target, disabled, actor, reason, incident_id)
            )
            db.execute("INSERT OR REPLACE INTO switches VALUES (?,?,?)", (kind, target, disabled))

    def audit(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db, db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM audit ORDER BY rowid")]

    def blocked(self, provider: str, model: str, licence: str) -> bool:
        targets = {("global", "*"), ("provider", provider), ("model", model), ("licence", licence)}
        try:
            with closing(self._connect()) as db, db:
                return any(
                    (k, t) in targets for k, t in db.execute("SELECT kind,target FROM switches WHERE disabled=1")
                )
        except sqlite3.Error:
            return True

    def probe(self, provider: str, ok: bool, latency_s: float, now: float) -> None:
        p = self.policy
        if p is None or provider not in p.qualified_providers:
            return
        if not math.isfinite(now) or not math.isfinite(latency_s) or latency_s < 0:
            raise ValueError("OPS-20: invalid synthetic probe")
        with self.lock:
            samples = self.samples[provider]
            if samples and now < samples[-1][0]:
                raise ValueError("OPS-20: probe clock moved backwards")
            samples.append((now, ok, latency_s))
            while samples and samples[0][0] < now - p.window_s:
                samples.popleft()
            failures = sum(not s[1] for s in samples) / len(samples)
            latencies = sorted(s[2] for s in samples if s[1])
            slow = bool(latencies) and latencies[-1] > p.latency_objective_s * p.latency_multiple
            if failures > p.failure_share or slow:
                self.degraded.add(provider)
                self.healthy_since.pop(provider, None)
            else:
                self.healthy_since.setdefault(provider, now)
                if now - self.healthy_since[provider] >= p.healthy_s:
                    self.degraded.discard(provider)

    def _healthy(self, provider: str, now: float) -> bool:
        if self.policy is None:
            return True
        if provider not in self.policy.qualified_providers or provider in self.degraded:
            return False
        samples = self.samples[provider]
        return not samples or now - samples[-1][0] <= self.policy.window_s

    def choose(self, primary: str, fallback: str | None, model: str, licence: str, now: float) -> str | None:
        with self.lock:
            if self._healthy(primary, now) and not self.blocked(primary, model, licence):
                return primary
            if (
                self.policy is not None
                and fallback
                and self._healthy(fallback, now)
                and not self.blocked(fallback, model, licence)
            ):
                return fallback
            return None

    def record_cost(
        self,
        now: float,
        provider: str,
        model: str,
        role: str,
        licence: str,
        tokens_in: int,
        tokens_cached: int,
        tokens_out: int,
        cost_usd: float,
    ) -> None:
        p = self.policy
        if p is None:
            return
        if (
            any(type(v) is not int or v < 0 for v in (tokens_in, tokens_cached, tokens_out))
            or tokens_cached > tokens_in
            or not math.isfinite(cost_usd)
            or cost_usd < 0
        ):
            raise ValueError("OPS-19: invalid token metadata")
        day = datetime.fromtimestamp(now, UTC).date()
        cutoff = day - timedelta(days=p.metadata_retention_days)
        alert = False
        with self.lock, closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO cost VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(day,provider,model,role,licence_id) "
                "DO UPDATE SET tokens_in=tokens_in+excluded.tokens_in, "
                "tokens_cached=tokens_cached+excluded.tokens_cached, tokens_out=tokens_out+excluded.tokens_out, "
                "cost_usd=cost_usd+excluded.cost_usd",
                (day.isoformat(), provider, model, role, licence, tokens_in, tokens_cached, tokens_out, cost_usd),
            )
            history = {
                d: total
                for d, total in db.execute(
                    "SELECT day,SUM(cost_usd) FROM cost WHERE licence_id=? AND day>=? AND day<? GROUP BY day",
                    (licence, (day - timedelta(days=p.cost_history_days)).isoformat(), day.isoformat()),
                )
            }
            today = db.execute(
                "SELECT SUM(cost_usd) FROM cost WHERE licence_id=? AND day=?", (licence, day.isoformat())
            ).fetchone()[0]
            if (
                len(history) == p.cost_history_days
                and today >= median(history.values()) * p.cost_multiple
                and today > 0
            ):
                alert = (
                    db.execute("INSERT OR IGNORE INTO alerted VALUES (?,?)", (day.isoformat(), licence)).rowcount > 0
                )
            db.execute("DELETE FROM cost WHERE day<?", (cutoff.isoformat(),))
            db.execute("DELETE FROM alerted WHERE day<?", (cutoff.isoformat(),))
        if alert and self.alerts is not None:
            self.alerts("ops19_daily_spike", {"licence_id": licence, "day": day.isoformat(), "cost_usd": today})

    def cost_groups(self, now: float) -> list[dict[str, Any]]:
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in db.execute("SELECT * FROM cost WHERE day=? ORDER BY provider,model,role,licence_id", (day,))
            ]
