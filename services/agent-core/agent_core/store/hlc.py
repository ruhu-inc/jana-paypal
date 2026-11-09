"""Hybrid logical clock stamps (DAT-16).

A stamp is fixed-width text, 15 digits of physical milliseconds, a hyphen and a 5-digit counter, so plain string
comparison is clock order. Local appends and received stamps follow the usual hybrid logical clock rules. A counter
that would pass its width moves the logical physical part on by one millisecond instead of waiting for the wall
clock. Every check that can refuse an input runs before the clock changes.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

PATTERN = re.compile(r"^[0-9]{15}-[0-9]{5}$")
MAX_PHYSICAL = 10**15 - 1
MAX_COUNTER = 99_999


class ClockRangeError(ValueError):
    """The stamp would not fit the encoding."""


class ClockSkewError(ValueError):
    """A received stamp is further ahead of this device's wall clock than allowed (`clock_skew`)."""

    reason = "clock_skew"


@dataclass(frozen=True, order=True)
class Stamp:
    physical: int
    counter: int

    def __post_init__(self) -> None:
        if not (0 <= self.physical <= MAX_PHYSICAL and 0 <= self.counter <= MAX_COUNTER):
            raise ClockRangeError(f"stamp ({self.physical}, {self.counter}) is outside the encoding")

    def encode(self) -> str:
        return f"{self.physical:015d}-{self.counter:05d}"

    @staticmethod
    def parse(text: str) -> Stamp:
        if not PATTERN.match(text):
            raise ClockRangeError(f"not a clock stamp: {text!r}")
        physical, counter = text.split("-")
        return Stamp(int(physical), int(counter))


ZERO = Stamp(0, 0)


def _successor(physical: int, counter: int) -> Stamp:
    """The stamp (physical, counter + 1), rolling into the next millisecond past the counter's width."""
    if counter < MAX_COUNTER:
        return Stamp(physical, counter + 1)
    if physical >= MAX_PHYSICAL:
        raise ClockRangeError("the clock reached the end of its encoding")
    return Stamp(physical + 1, 0)


class HybridClock:
    """One device's clock. `last` is the highest stamp this device has issued or seen (persisted with each event)."""

    def __init__(self, wall_ms: Callable[[], int], max_skew_ms: int, last: Stamp = ZERO) -> None:
        self.wall_ms = wall_ms
        self.max_skew_ms = max_skew_ms
        self.last = last

    def now(self) -> Stamp:
        """A stamp for a local append: strictly above every stamp issued or seen."""
        wall = self.wall_ms()
        if wall > MAX_PHYSICAL or wall < 0:
            raise ClockRangeError(f"wall clock {wall} is outside the encoding")
        stamp = Stamp(wall, 0) if wall > self.last.physical else _successor(self.last.physical, self.last.counter)
        self.last = stamp
        return stamp

    def admit(self, remote: Stamp) -> None:
        """Refuse a received stamp too far ahead of the wall clock; changes nothing either way."""
        if remote.physical - self.wall_ms() > self.max_skew_ms:
            raise ClockSkewError(f"stamp {remote.encode()} is more than {self.max_skew_ms} ms ahead")

    def receive(self, remote: Stamp) -> Stamp:
        """Merge a received stamp (after `admit`): the result is above both the local and the remote stamp."""
        self.admit(remote)
        wall = self.wall_ms()
        physical = max(self.last.physical, remote.physical, wall)
        if physical == self.last.physical == remote.physical:
            stamp = _successor(physical, max(self.last.counter, remote.counter))
        elif physical == self.last.physical:
            stamp = _successor(physical, self.last.counter)
        elif physical == remote.physical:
            stamp = _successor(physical, remote.counter)
        else:
            stamp = Stamp(physical, 0)
        self.last = stamp
        return stamp
