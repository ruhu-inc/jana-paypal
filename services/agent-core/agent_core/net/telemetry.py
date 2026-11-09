"""OBS-20, PRV-09: durable bounded aggregates; preparation never opens a socket."""

from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from typing import Any

from agent_core.config.telemetry_schema import problems, report
from agent_core.store import egress
from agent_core.store.sqlite import transaction

log = logging.getLogger(__name__)

KEY = "m23.telemetry"
DAY_MS = 86_400_000


def _merge(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for metrics in rows:
        for name, value in metrics.items():
            cell = result.setdefault(name, {"count": 0, "sum": 0, "buckets": [0] * len(value["buckets"])})
            cell["count"] += value["count"]
            cell["sum"] += value["sum"]
            cell["buckets"] = [a + b for a, b in zip(cell["buckets"], value["buckets"], strict=True)]
    return result


class TelemetryQueue:
    def __init__(self, layer: Any) -> None:
        self.layer = layer

    def _read(self) -> dict[str, Any]:
        row = self.layer.device.execute("SELECT value FROM app_settings WHERE key = ?", (KEY,)).fetchone()
        return json.loads(row[0]) if row else {"days": {}, "last_queued": None}

    def _write(self, state: dict[str, Any]) -> None:
        with nullcontext() if self.layer.device.in_transaction else transaction(self.layer.device):
            self.layer.device.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (KEY, json.dumps(state, allow_nan=False), self.layer.clock_ms()),
            )

    def _today(self) -> int:
        return int(self.layer.clock_ms()) // DAY_MS

    def _prune(self, state: dict[str, Any]) -> None:
        cutoff = self._today() - self.layer.settings.retention_telemetry_days + 1
        state["days"] = {day: row for day, row in state["days"].items() if cutoff <= int(day) <= self._today()}
        if "queued_snapshot" in state:
            state["queued_snapshot"] = {
                day: row for day, row in state["queued_snapshot"].items() if cutoff <= int(day) <= self._today()
            }

    def days(self) -> list[str]:
        state = self._read()
        self._prune(state)
        self._write(state)
        return sorted(state["days"])

    def add(self, metrics: dict[str, Any], **metadata: Any) -> None:
        if "telemetry" not in self.layer.features_on():
            self.clear()
            return
        report(metrics, days=1, **metadata)  # reject arbitrary strings before they can reach durable storage
        state = self._read()
        day = str(self._today())
        groups = state["days"].setdefault(day, {})
        key = json.dumps(metadata, sort_keys=True)
        previous = groups.get(key, {}).get("metrics", {})
        groups[key] = {"metadata": metadata, "metrics": _merge([previous, metrics])}
        self._prune(state)
        self._write(state)

    def clear(self) -> None:
        state = self._read()
        if state.get("last_queued") is not None:
            self._write({"days": {}, "last_queued": state["last_queued"]})
        else:
            self.layer.device.execute("DELETE FROM app_settings WHERE key = ?", (KEY,))
        for row in egress.pending(self.layer.device, "telemetry"):
            if row.kind == "telemetry":
                egress.drop(self.layer.device, row.outbox_id)

    def prepare(self) -> str | None:
        if "telemetry" not in self.layer.features_on():
            self.clear()
            return None
        state = self._read()
        self._prune(state)
        if not self.layer.usable() or state["last_queued"] == self._today() or not state["days"]:
            self._write(state)
            return None
        # A pending report that did not get acknowledged stays retryable with its original idempotency key.
        pending = [r for r in egress.pending(self.layer.device, "telemetry") if r.kind == "telemetry"]
        if pending:
            first = pending[0]
            if first.created_at // DAY_MS == self._today():
                return None
            for row in pending:
                egress.drop(self.layer.device, row.outbox_id)
        merged: dict[str, Any] = {}
        for groups in state["days"].values():
            for key, row in groups.items():
                old = merged.get(key, {}).get("metrics", {})
                merged[key] = {**row, "metrics": _merge([old, row["metrics"]])}
        values = list(merged.values())
        value = report(
            _merge([r["metrics"] for r in values if not r["metadata"].get("labels")]),
            days=len(state["days"]),
            series=[{**r["metadata"], "metrics": r["metrics"]} for r in values],
            **{k: v for k, v in values[-1]["metadata"].items() if k != "labels"},
        )
        found = problems(value)
        if found:
            # PRV-09, OBS-20: a report its published schema refuses never leaves the device. This is a real check, not
            # an assert, so it holds under `python -O` too; the days stay for a fixed build, and only the count of
            # problems is logged, never a value.
            log.warning("telemetry report refused by its schema (%d problems)", len(found))
            self._write(state)
            return None
        with transaction(self.layer.device):
            outbox_id = self.layer.outbox.put(
                "telemetry",
                "telemetry",
                json.dumps(value).encode(),
                expires_ms=(self._today() + 1) * DAY_MS,
                idempotency_key=value["report_id"],
            )
            state["last_queued"] = self._today()
            state["queued_snapshot"] = json.loads(json.dumps(state["days"]))
            self._write(state)
        return str(outbox_id)

    def acknowledged(self) -> None:
        if any(r.kind == "telemetry" for r in egress.pending(self.layer.device, "telemetry")):
            return
        state = self._read()
        for day, groups in state.pop("queued_snapshot", {}).items():
            current = state["days"].get(day, {})
            for key, row in groups.items():
                if key not in current:
                    continue
                metrics = current[key]["metrics"]
                for name, sent in row["metrics"].items():
                    cell = metrics.get(name)
                    if cell is not None:
                        cell["count"] -= sent["count"]
                        cell["sum"] -= sent["sum"]
                        cell["buckets"] = [a - b for a, b in zip(cell["buckets"], sent["buckets"], strict=True)]
                        if cell["count"] == 0:
                            metrics.pop(name)
                if not metrics:
                    current.pop(key)
            if not current:
                state["days"].pop(day, None)
        self._write(state)
