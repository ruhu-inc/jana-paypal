"""Cloud cost budgets and the cloud tier's allowance (MDL-29, BIZ-07).

Spend is metered on the device, from each cloud call's usage and the configured price table, so budgets hold
offline. Three limits apply to a session's cloud calls: the session budget, the student's daily budget and the
student's allowance for the month (in tokens, fixed by the subscription's entitlement per student per month, BIZ-07,
D33; no adult cap setting exists, D53). Each limit sends the engine one reminder at each configured fraction (50% and
85%); at 100% the cloud is off for that period and calls fall back to local silently. A call already running when a
limit is crossed finishes; the next call is served locally. Students never see costs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


class SpendStore(Protocol):
    """Where a profile's metered spend is kept (the device database, `store.egress`)."""

    def day_usd(self, day: str) -> float: ...
    def period_tokens(self) -> int: ...
    def add(self, day: str, usd: float, tokens: int) -> None: ...


@dataclass
class MemorySpend:
    days: dict[str, float] = field(default_factory=dict)
    tokens: int = 0

    def day_usd(self, day: str) -> float:
        return self.days.get(day, 0.0)

    def period_tokens(self) -> int:
        return self.tokens

    def add(self, day: str, usd: float, tokens: int) -> None:
        self.days[day] = self.days.get(day, 0.0) + usd
        self.tokens += tokens


@dataclass
class CloudBudget:
    session_usd: float
    day_usd: float
    reminders: tuple[float, ...]
    spend: SpendStore
    today: Callable[[], str]
    allowance_tokens: int | None = None  # the month's allowance; None when no entitlement sets one
    on_reminder: Callable[[str, float], None] | None = None  # (limit, fraction): "session", "day" or "month"
    session_spent: float = 0.0
    _sent: set[tuple[str, float]] = field(default_factory=set)

    def used(self) -> dict[str, float]:
        """The share of each limit used, 0 to 1 and beyond."""
        shares = {
            "session": self.session_spent / self.session_usd if self.session_usd > 0 else 1.0,
            "day": self.spend.day_usd(self.today()) / self.day_usd if self.day_usd > 0 else 1.0,
        }
        if self.allowance_tokens is not None:
            shares["month"] = self.spend.period_tokens() / self.allowance_tokens if self.allowance_tokens > 0 else 1.0
        return shares

    def allows(self) -> bool:
        return all(share < 1.0 for share in self.used().values())

    def exhausted(self) -> list[str]:
        return [limit for limit, share in self.used().items() if share >= 1.0]

    def record(self, usd: float, tokens: int) -> None:
        self.session_spent += usd
        self.spend.add(self.today(), usd, tokens)
        for limit, share in self.used().items():
            for fraction in self.reminders:
                if share >= fraction and (limit, fraction) not in self._sent:
                    self._sent.add((limit, fraction))  # one-shot (MDL-29)
                    if self.on_reminder is not None:
                        self.on_reminder(limit, fraction)
