"""Learned feature rejections (MDL-04) and the parameter fixes they carry (MDL-11).

When an endpoint rejects a request parameter once (`response_format`, `tool_choice`, `parallel_tool_calls`, a
renamed `max_tokens`, ...), the (candidate, parameter) pair is remembered for the install: later requests to that
candidate send the parameter renamed or not at all, and structured output starts from the next mode (MDL-15). The
router may not reach the stores, so persistence is the owner's: `on_change` receives every new state and the owner
passes the saved state back in at start."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

Fixes = dict[str, dict[str, str | None]]  # candidate -> parameter -> replacement (None: dropped)


@dataclass
class FeatureRejections:
    fixes: Fixes = field(default_factory=dict)
    on_change: Callable[[Fixes], None] | None = None

    def remember(self, candidate: str, param: str, replacement: str | None) -> None:
        held = self.fixes.setdefault(candidate, {})
        if held.get(param, "") == replacement:
            return
        held[param] = replacement
        if self.on_change is not None:
            self.on_change({c: dict(p) for c, p in self.fixes.items()})

    def for_candidate(self, candidate: str) -> dict[str, str | None]:
        return dict(self.fixes.get(candidate, {}))

    def dropped(self, candidate: str) -> frozenset[str]:
        """Parameters this candidate's endpoint does not accept at all."""
        return frozenset(p for p, r in self.fixes.get(candidate, {}).items() if r is None)
