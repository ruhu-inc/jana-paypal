"""The offline outbox (ARC-09, DAT-11).

Queued online work is written to the `outbox` table before the action reports done, with its feature, idempotency
key, creation time, size and expiry. While connectivity is usable the outbox drains in order per feature, retries
with jittered backoff, caps its total size and drops entries past expiry with a ledger note. Delivery is exactly once
from the destination's view: each item is sent with its idempotency key, marked delivered only after a 2xx (or a
"seen already" 409), and resent with the same key after a restart that interrupted it. Turning a feature off purges
its payloads at once, except tombstones and consent withdrawals, which are still delivered (DAT-11)."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_core.net.client import EgressClient, NetError
from agent_core.net.policy import EgressRefused
from agent_core.store import egress as store

LATEST_ONLY = frozenset({"backup_snapshot", "parent_digest"})  # TABLE ARC-07a: the latest replaces older pending ones
EXPIRED_NOTE = "expired_unsent"
PAYLOAD_CLASS = {"telemetry": "telemetry_aggregate"}  # outbox kind -> payload class where the names differ


@dataclass
class Outbox:
    """`client` sends; `host_for(feature)` names the feature's destination host. Backoff and caps come from the
    configuration (`outbox_backoff_min_s`, `outbox_backoff_max_s`, `outbox_cap_bytes`)."""

    device: Any
    client: EgressClient
    host_for: Callable[[str], str]
    backoff_min_s: float = 30.0
    backoff_max_s: float = 6 * 3600.0
    cap_bytes: int = 50 * 1024 * 1024
    clock_ms: Callable[[], int] = field(default=lambda: int(time.time() * 1000))
    rng: random.Random = field(default_factory=random.Random)

    def put(self, kind: str, feature: str, payload: bytes, *, expires_ms: int, idempotency_key: str | None = None,
            profile_id: str | None = None) -> str:  # fmt: skip
        """Queue work durably (ARC-09). A latest-only kind replaces its older pending items; the total stays under the
        cap by dropping the oldest purgeable items of the same feature."""
        now = self.clock_ms()
        if feature == "telemetry" and kind == "telemetry":
            import json

            from agent_core.config.telemetry_schema import problems

            if problems(json.loads(payload)):
                raise ValueError("PRV-09: telemetry payload refused")
        if kind in LATEST_ONLY:
            for item in store.pending(self.device, feature):
                if item.kind == kind and item.profile_id == profile_id:
                    store.drop(self.device, item.outbox_id)
        over = store.total_bytes(self.device) + len(payload) - self.cap_bytes
        for item in store.pending(self.device, feature):
            if over <= 0:
                break
            if item.kind not in store.KEPT_KINDS:
                store.drop(self.device, item.outbox_id)
                over -= item.size
        return store.enqueue(self.device, kind=kind, feature=feature, destination=self.host_for(feature),
                             payload=payload, now_ms=now, expires_ms=expires_ms, idempotency_key=idempotency_key,
                             profile_id=profile_id)  # fmt: skip

    def purge(self, feature: str) -> int:
        """The feature was switched off: delete its queued payloads before the switch returns (ARC-09, DAT-11)."""
        return store.purge_feature(self.device, feature)

    def counts(self) -> dict[str, int]:
        return store.counts(self.device)

    def _backoff_ms(self, attempts: int) -> int:
        base = min(self.backoff_max_s, self.backoff_min_s * (2**attempts))
        return int(base * (0.5 + self.rng.random()) * 1000)  # jitter: half to one and a half times

    async def drain(self, *, force: bool = False) -> int:
        """Send what is due, in order per feature. A feature whose send fails waits for its backoff; the others go
        on. `force` ignores the backoff ("Send now", UXS-08). Returns the number delivered."""
        now = self.clock_ms()
        delivered = 0
        blocked: set[str] = set()
        for item in store.pending(self.device):
            feature = item.feature or item.destination
            if feature in blocked:
                continue
            if item.expires_at <= now:
                store.drop(self.device, item.outbox_id)
                store.ledger_record(self.device, now, feature, item.destination, 0, EXPIRED_NOTE, item.profile_id)
                continue
            if not force and item.next_attempt_at is not None and item.next_attempt_at > now:
                blocked.add(feature)
                continue
            try:
                host = self.host_for(feature)  # the destination as the policy names it now (never a stored address)
            except KeyError:
                blocked.add(feature)
                continue
            if feature == "telemetry" and item.kind == "telemetry":
                import json

                from agent_core.config.telemetry_schema import problems

                try:
                    invalid = problems(json.loads(item.payload))
                except (TypeError, ValueError):
                    invalid = ["invalid JSON"]
                if invalid:
                    store.drop(self.device, item.outbox_id)
                    continue
            try:
                reply = await self.client.send(
                    feature, "POST", f"https://{host}/v1/outbox/{item.kind}", PAYLOAD_CLASS.get(item.kind, item.kind),
                    content=item.payload, headers={"idempotency-key": item.idempotency_key,
                                                   "content-type": "application/octet-stream"},
                    profile_id=item.profile_id)  # fmt: skip
            except EgressRefused as refused:
                if refused.reason == "offline":
                    return delivered  # nothing more can go now
                blocked.add(feature)
                continue
            except NetError:
                store.failed(self.device, item.outbox_id, now + self._backoff_ms(item.attempts))
                blocked.add(feature)
                continue
            if 200 <= reply.status < 300 or reply.status == 409:
                store.delivered(self.device, item.outbox_id, self.clock_ms())
                delivered += 1
            else:
                store.failed(self.device, item.outbox_id, now + self._backoff_ms(item.attempts))
                blocked.add(feature)
        return delivered
