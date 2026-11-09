"""Service status and objectives (OBS-22).

`/v1/status` reports ok, degraded or outage from the upstream outcomes of a recent window, so the app can say the
online tutor is having problems and keep the student on the local tutor. The availability and latency objectives
are evaluated over the metadata store and raise alerts when missed. Only upstream faults count against the
service: a device over its quota or a refused request is the gateway working, not failing."""

from __future__ import annotations

import math
from collections import deque
from typing import Literal

from cloud_gateway.config import Objectives
from cloud_gateway.quotas import AlertSink
from cloud_gateway.store import GatewayStore

Status = Literal["ok", "degraded", "outage"]
UPSTREAM_FAULTS = frozenset(
    {"server_error", "overloaded", "timeout", "network_unavailable", "upstream_auth", "bad_response"}
)


class UpstreamHealth:
    def __init__(self, objectives: Objectives) -> None:
        self.o = objectives
        self._outcomes: deque[tuple[float, bool]] = deque()

    def record(self, now: float, outcome: str) -> None:
        self._outcomes.append((now, outcome not in UPSTREAM_FAULTS))

    def status(self, now: float) -> Status:
        while self._outcomes and self._outcomes[0][0] < now - self.o.status_window_s:
            self._outcomes.popleft()
        if len(self._outcomes) < self.o.status_min_samples:
            return "ok"
        failed = sum(1 for _, ok in self._outcomes if not ok) / len(self._outcomes)
        if failed >= self.o.outage_failure_ratio:
            return "outage"
        if failed >= self.o.degraded_failure_ratio:
            return "degraded"
        return "ok"


def evaluate_objectives(store: GatewayStore, objectives: Objectives, now: float, alerts: AlertSink) -> dict[str, float]:
    """Availability and p95 latency over the objective window; an alert for each objective missed."""
    rows = store.outcomes_since(now - objectives.availability_window_days * 86400)
    if not rows:
        return {}
    good = sum(1 for _, outcome, _ in rows if outcome not in UPSTREAM_FAULTS)
    availability = good / len(rows)
    latencies = sorted(lat for _, outcome, lat in rows if outcome == "ok")
    p95 = float(latencies[max(0, math.ceil(0.95 * len(latencies)) - 1)]) if latencies else 0.0
    if availability < objectives.availability:
        alerts("availability_objective_missed", {"availability": round(availability, 5),
                                                 "objective": objectives.availability})  # fmt: skip
    if p95 > objectives.latency_p95_ms:
        alerts("latency_objective_missed", {"p95_ms": p95, "objective_ms": objectives.latency_p95_ms})
    return {"availability": availability, "latency_p95_ms": p95, "requests": float(len(rows))}
