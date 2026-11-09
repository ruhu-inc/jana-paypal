"""Connectivity detection (ARC-08, TABLE ARC-08a).

States: `off` (every feature disabled: no probe, no egress), `offline` (the OS reports no route, or a probe fails),
`limited` (a captive portal or a TLS failure on the probe: treated as offline), `metered` (the OS metered flag: large
downloads pause) and `online` (a probe to an enabled feature's host succeeds), plus `unknown` before the first check.
Probes go only to the host of an enabled feature and never run while every feature is off. OS hints (a network
change, the metered flag, the shell's `control.net_state`) trigger a probe; the periodic probe runs no more often
than the configured interval and backs off while offline. A caller that saw a network failure reports it, and the
layer re-probes on its own schedule (MDL-24). Every change is published to the listeners (the core's
`connectivity.status`, the router's view)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from agent_core.net.policy import NetworkState

ProbeOutcome = Literal["ok", "failed", "tls_failure", "captive"]


@dataclass
class Connectivity:
    """`features_on()` lists the enabled features; `probe_host()` names a host of one of them (None when all are
    off); `probe(host)` performs the probe. Intervals are the configuration's `net_probe_interval_s` and
    `net_probe_backoff_max_s`."""

    features_on: Callable[[], set[str]]
    probe_host: Callable[[], str | None]
    probe: Callable[[str], Awaitable[ProbeOutcome]]
    interval_s: float = 60.0
    backoff_max_s: float = 900.0
    clock: Callable[[], float] = time.monotonic
    state: NetworkState = "unknown"
    checked_ms: int = 0
    os_online: bool | None = None
    metered: bool = False
    probes: int = 0
    listeners: list[Callable[[NetworkState], None]] = field(default_factory=list)
    _delay_s: float = 0.0
    _due: float = 0.0
    _wake: asyncio.Event | None = None

    def __post_init__(self) -> None:
        if not self.features_on():
            self.state = "off"
        self._delay_s = self.interval_s

    @property
    def usable(self) -> bool:
        return self.state in ("online", "metered")

    def _set(self, state: NetworkState) -> None:
        self.checked_ms = int(time.time() * 1000)
        if state != self.state:
            self.state = state
            for listener in list(self.listeners):
                listener(state)

    def _poke(self) -> None:
        self._due = self.clock()
        if self._wake is not None:
            self._wake.set()

    # --- inputs ------------------------------------------------------------------------------------------------

    def features_changed(self) -> None:
        """An adult switched a feature: all off is `off` at once; otherwise the state is checked again."""
        if not self.features_on():
            self._set("off")
            return
        if self.state == "off":
            self._set("unknown")
        self._delay_s = self.interval_s
        self._poke()

    def os_hint(self, online: bool | None = None, metered: bool | None = None) -> None:
        """A network change or the metered flag from the OS (through the shell). No route is offline at once."""
        if metered is not None:
            self.metered = metered
        if online is not None:
            self.os_online = online
        if self.state == "off":
            return
        if online is False:
            self._set("offline")
            return
        if self.usable and metered is not None:
            self._set("metered" if self.metered else "online")
        self._poke()

    def report_failure(self) -> None:
        """A request found the network unusable (MDL-24): treat it as offline until the next probe."""
        if self.state == "off":
            return
        self._set("offline")
        self._delay_s = self.interval_s
        self._due = self.clock() + self._delay_s

    def recheck_soon(self, min_age_s: float) -> None:
        """Something would use the network now (a turn the cloud would have served): while offline or limited, probe
        again as soon as the last check is `min_age_s` old, rather than waiting out the backoff (W3: the cloud tutor
        returns on the next turn after Wi-Fi comes back)."""
        if self.state not in ("offline", "limited"):
            return
        if time.time() * 1000 - self.checked_ms >= min_age_s * 1000:
            self._poke()

    # --- probing ------------------------------------------------------------------------------------------------

    async def check(self) -> NetworkState:
        """One probe now, unless every feature is off or the OS says there is no route."""
        if not self.features_on():
            self._set("off")
            return self.state
        if self.os_online is False:
            self._set("offline")
            return self.state
        host = self.probe_host()
        if host is None:
            self._set("off")
            return self.state
        self.probes += 1
        outcome = await self.probe(host)
        if outcome == "ok":
            self._set("metered" if self.metered else "online")
            self._delay_s = self.interval_s
        elif outcome in ("tls_failure", "captive"):
            self._set("limited")
            self._delay_s = min(self.backoff_max_s, max(self.interval_s, self._delay_s * 2))
        else:
            self._set("offline")
            self._delay_s = min(self.backoff_max_s, max(self.interval_s, self._delay_s * 2))
        self._due = self.clock() + self._delay_s
        return self.state

    def next_delay_s(self) -> float:
        return self._delay_s

    async def run(self, sleep: Callable[[float], Awaitable[None]] | None = None) -> None:
        """The periodic probe: at most once per interval, backing off while offline; idle while all off."""
        self._wake = asyncio.Event()
        while True:
            wait = max(0.0, self._due - self.clock())
            if wait > 0 or not self.features_on():
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=wait if self.features_on() else None)
                except TimeoutError:
                    pass
                if not self.features_on():
                    self._set("off")
                    continue
            await self.check()
