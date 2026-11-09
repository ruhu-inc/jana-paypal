"""Reasoning effort mapping (MDL-12).

A role names an effort (`none`, `low`, `medium`, `high`); each model has one control: none, a toggle, an effort level
or a token budget (MDL-01 `reasoning`). The router maps the requested effort to that control, maps a level the
model does not support to the nearest one it does, and records both the requested and the effective effort. On a
local model the hardware class bounds thinking first: `router_thinking_budget` per class, where 0 is off."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_core.config import Settings
from agent_core.providers.base import Effort, ModelCapabilities

LEVELS: tuple[Effort, ...] = ("none", "low", "medium", "high")
CLASS_INDEX = {"low": 0, "mid": 1, "high": 2}
# The levels each control can express. A toggle is off or on; an effort-level endpoint has no "off".
SUPPORTED: dict[str, tuple[Effort, ...]] = {
    "none": ("none",),
    "toggle": LEVELS,
    "effort": ("low", "medium", "high"),
    "budget": LEVELS,
}


@dataclass(frozen=True)
class EffortPlan:
    requested: Effort
    effective: Effort
    control: str  # "off", "on" (toggle), "low" | "medium" | "high" (effort level) or "budget"
    budget_tokens: int | None  # the thinking budget, None when the model's own default applies

    def settings(self) -> dict[str, Any]:
        """The call settings a provider maps onto its request fields."""
        return {
            "effort": self.effective,
            "effort_requested": self.requested,
            "reasoning": {"control": self.control, "budget_tokens": self.budget_tokens},
        }

    def record(self) -> dict[str, Any]:
        return {"requested": self.requested, "effective": self.effective, "control": self.control,
                "budget_tokens": self.budget_tokens}  # fmt: skip


def as_level(value: object) -> Effort:
    """An effort named by the router configuration or a caller; anything else counts as `none`."""
    return next((level for level in LEVELS if level == value), "none")


def nearest(level: Effort, supported: tuple[Effort, ...]) -> Effort:
    """The supported level closest to `level`; a tie goes to the cheaper one."""
    index = LEVELS.index(level)
    return min(supported, key=lambda s: (abs(LEVELS.index(s) - index), LEVELS.index(s)))


def class_budget(caps: ModelCapabilities, hardware_class: str | None, settings: Settings) -> int | None:
    """The hardware class's thinking budget for a local model; None for a cloud model or an unknown class."""
    if caps.locality != "local" or hardware_class not in CLASS_INDEX:
        return None
    return int(settings.router_thinking_budget[CLASS_INDEX[hardware_class]])


def plan(requested: Effort, caps: ModelCapabilities, hardware_class: str | None, settings: Settings) -> EffortPlan:
    cap = class_budget(caps, hardware_class, settings)
    if cap == 0 or caps.reasoning == "none":
        return EffortPlan(requested, "none", "off", 0 if cap == 0 else None)
    effective = nearest(requested, SUPPORTED[caps.reasoning])
    if effective == "none":
        return EffortPlan(requested, "none", "off", 0)
    if caps.reasoning == "toggle":
        return EffortPlan(requested, effective, "on", cap)
    if caps.reasoning == "effort":
        return EffortPlan(requested, effective, effective, cap)
    budget = int(settings.router_effort_budget[LEVELS.index(effective)])
    return EffortPlan(requested, effective, "budget", budget if cap is None else min(budget, cap))
