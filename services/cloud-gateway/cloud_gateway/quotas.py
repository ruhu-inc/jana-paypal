"""Rate limits, the device's daily spend cap, the subscription's monthly allowance, cost from the price table, and
spend-anomaly alerts (SEC-20, MDL-29, BIZ-07, OPS-19).

Rate limits are token buckets held in memory per subscription and per licence. Spend comes from the metadata store:
a device's spend today is keyed by its daily device hash, a licence's by its licence id, and a subscription's spend
in its current allowance month is one running total (`GatewayStore.allowance_spend`). A subscription may spend its
students' monthly allowance (`seats` times `cloud_gateway.tier.monthly_allowance_usd`) in each allowance month of its
paid period (D33; one allowance for every plan and band, D121); past it the gateway refuses until the next month, and
the device continues on the local tutor (BIZ-07). There is no per-school cap: school seats are set aside (D100). The
anomaly check compares today's spend with trailing daily means computed from the same store (a device against its
licence's mean spend per device-day, a licence against its own daily mean), so it never needs to link a device across
days."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cloud_gateway.config import GatewayConfig, Price
from cloud_gateway.providers.base import Usage
from cloud_gateway.store import GatewayStore, day_of
from cloud_gateway.tier import allowance_window, monthly_allowance_usd

AlertSink = Callable[[str, dict[str, object]], None]
log = logging.getLogger("cloud_gateway")


def log_alert(kind: str, detail: dict[str, object]) -> None:
    log.warning("alert %s %s", kind, " ".join(f"{k}={v}" for k, v in sorted(detail.items())))


def cost_usd(price: Price | None, usage: Usage) -> float:
    if price is None:
        return 0.0
    cached = min(usage.cached_tokens, usage.input_tokens)
    return (
        (usage.input_tokens - cached) * price.input + cached * price.cached_input + usage.output_tokens * price.output
    ) / 1_000_000


@dataclass
class _Bucket:
    tokens: float
    at: float


class TokenBuckets:
    def __init__(self, per_minute: float, burst: float) -> None:
        self.rate = per_minute / 60.0
        self.burst = burst
        self._buckets: dict[str, _Bucket] = {}

    def take(self, key: str, now: float) -> float | None:
        """None if a request may go now, else the seconds until it may."""
        bucket = self._buckets.setdefault(key, _Bucket(self.burst, now))
        bucket.tokens = min(self.burst, bucket.tokens + (now - bucket.at) * self.rate)
        bucket.at = now
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return None
        return (1 - bucket.tokens) / self.rate if self.rate > 0 else 3600.0


class Quotas:
    def __init__(self, config: GatewayConfig, store: GatewayStore, alerts: AlertSink = log_alert) -> None:
        self.config = config
        self.q = config.quotas
        self.store = store
        self.alerts = alerts
        self.devices = TokenBuckets(self.q.device_requests_per_minute, self.q.device_burst)
        self.licences = TokenBuckets(self.q.licence_requests_per_minute, self.q.licence_burst)
        self._alerted: set[tuple[str, str, str]] = set()

    def refusal(self, account: str, device_hash: str, licence_id: str, now: float, *,
                period_start: float | None = None, seats: int | None = None) -> tuple[str, float] | None:  # fmt: skip
        """(why, retry-after seconds) if the request is over a quota, else None. `period_start` and `seats` come from
        the subscription's entitlement; without them (a developer registration) there is no monthly allowance."""
        day = day_of(now)
        tomorrow = datetime.fromtimestamp(now, UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        until_tomorrow = (tomorrow + timedelta(days=1)).timestamp() - now
        if self.store.spend(day, device_hash=device_hash) >= self.q.device_daily_spend_usd:
            return "the device's daily spend cap", until_tomorrow
        if period_start is not None and seats is not None:
            start, end = allowance_window(period_start, now)
            if self.store.allowance_spend(account, start) >= seats * monthly_allowance_usd(self.config):
                return "the subscription's monthly allowance", end - now
        wait = self.devices.take(account, now)
        if wait is not None:
            return "the device's request rate limit", wait
        wait = self.licences.take(licence_id, now)
        if wait is not None:
            return "the licence's request rate limit", wait
        return None

    def spent(self, account: str, period_start: float | None, now: float, cost: float) -> None:
        """Count a call's cost against its subscription's allowance month (none without an entitlement)."""
        if period_start is not None and cost > 0:
            self.store.add_allowance_spend(account, allowance_window(period_start, now)[0], cost)

    def _alert(self, kind: str, subject: str, day: str, detail: dict[str, object]) -> None:
        if (kind, subject, day) in self._alerted:
            return
        self._alerted.add((kind, subject, day))
        self.alerts(kind, detail)

    def check_anomaly(self, device_hash: str, licence_id: str, now: float) -> None:
        day = day_of(now)
        start = datetime.fromtimestamp(now, UTC)
        days = [day_of((start - timedelta(days=n)).timestamp()) for n in range(1, self.q.anomaly_trailing_days + 1)]
        licence_mean, device_day_mean = self.store.trailing_daily_spend(licence_id, days)
        device_today = self.store.spend(day, device_hash=device_hash)
        licence_today = self.store.spend(day, licence_id=licence_id)
        floor, multiple = self.q.anomaly_min_usd, self.q.anomaly_multiple
        if device_today >= floor and device_day_mean > 0 and device_today > multiple * device_day_mean:
            self._alert("device_spend_anomaly", device_hash, day, {
                "licence_id": licence_id, "device_hash": device_hash, "spend_usd": round(device_today, 4),
                "trailing_mean_usd": round(device_day_mean, 4),
            })  # fmt: skip
        if licence_today >= floor and licence_mean > 0 and licence_today > multiple * licence_mean:
            self._alert("licence_spend_anomaly", licence_id, day, {
                "licence_id": licence_id, "spend_usd": round(licence_today, 4),
                "trailing_mean_usd": round(licence_mean, 4),
            })  # fmt: skip
