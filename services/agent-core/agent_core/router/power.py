"""Low-power mode (PLT-22).

On battery below `low_power_battery_pct`, or when the student turns low-power mode on (a student preference, D53),
the router uses the next smaller installed model, asks for shorter outputs and no thinking, and the client shows the
low-battery banner (the `power` health condition). The battery is read from the operating system without elevated
rights; a machine with no battery is never in battery-saving.
"""

from __future__ import annotations

import ctypes
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agent_core.config import Settings

Reason = Literal["preference", "battery"]


@dataclass(frozen=True)
class Battery:
    percent: float
    discharging: bool


@dataclass(frozen=True)
class PowerPlan:
    """What saving power changes for a call: the smaller model to use (None keeps the model), the output cap, and
    thinking off."""

    reason: Reason
    model: str | None
    max_output_tokens: int


def saving(settings: Settings, preference: bool, battery: Battery | None) -> Reason | None:
    """Why the app saves power now, or None."""
    if preference:
        return "preference"
    if battery is not None and battery.discharging and battery.percent < settings.low_power_battery_pct:
        return "battery"
    return None


def plan(settings: Settings, preference: bool, battery: Battery | None, models: list[str],
         serving: str | None) -> PowerPlan | None:  # fmt: skip
    """The power plan for the next call. `models` are the plan's models, largest first (MDL-19): the next smaller one
    after the model serving now is used when there is one; on the smallest (the minimum laptop's 8B-class model, D58)
    the model stays and only the outputs and thinking change."""
    reason = saving(settings, preference, battery)
    if reason is None:
        return None
    smaller = None
    if serving in models and models.index(serving) + 1 < len(models):
        smaller = models[models.index(serving) + 1]
    return PowerPlan(reason, smaller, settings.low_power_max_output_tokens)


def _run(*args: str) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def read_battery() -> Battery | None:
    """This machine's battery, or None when it has none or it cannot be read."""
    system = platform.system()
    if system == "Darwin":
        out = _run("pmset", "-g", "batt")
        found = re.search(r"(\d+)%;\s*(\w+)", out)
        if not found:
            return None
        return Battery(float(found.group(1)), "Battery Power" in out or found.group(2) == "discharging")
    if system == "Linux":
        for supply in sorted(Path("/sys/class/power_supply").glob("BAT*")):
            try:
                percent = float((supply / "capacity").read_text().strip())
                status = (supply / "status").read_text().strip().lower()
            except (OSError, ValueError):
                continue
            return Battery(percent, status == "discharging")
        return None
    return _windows_battery()  # pragma: no cover - Windows only


def _windows_battery() -> Battery | None:  # pragma: no cover - Windows only
    class Status(ctypes.Structure):
        _fields_ = [("ac", ctypes.c_byte), ("flag", ctypes.c_byte), ("percent", ctypes.c_byte),
                    ("saver", ctypes.c_byte), ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]  # fmt: skip

    status = Status()
    try:
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):  # type: ignore[attr-defined]
            return None
    except (AttributeError, OSError):
        return None
    if status.flag & 128 or status.percent < 0 or status.percent > 100:  # no system battery, or unknown
        return None
    return Battery(float(status.percent), status.ac == 0)
