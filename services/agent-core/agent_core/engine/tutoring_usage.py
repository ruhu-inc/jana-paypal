"""The read-only tutoring allowance shown in chat, separate from lesson content."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal


def asks_usage(text: str) -> bool:
    words = " ".join(text.casefold().replace("\u2019", "'").strip().rstrip("?.!").split())
    return re.fullmatch(
        r"(?:what(?:'s| is) my (?:current |online tutoring |tutoring )?usage|"
        r"(?:check|show)(?: me)?(?: my)? (?:current )?usage|"
        r"how much (?:online )?(?:tutoring|usage|allowance) (?:do i have left|is left|have i used))",
        words,
    ) is not None


@dataclass(frozen=True)
class TutoringUsage:
    scope: Literal["unavailable", "uncovered", "device", "subscription"]
    remaining: float | None = None
    until_ms: int | None = None
    period_ends: bool = False
    reserved: bool = False
    device_remaining: float | None = None

    def answer(self) -> str:
        if self.scope == "uncovered":
            return "There is no active included online tutoring allowance available to this profile."
        if self.remaining is None or self.until_ms is None:
            return "I can't read your current online tutoring usage right now."
        pct = round(self.remaining * 100, 10)
        rounded = int(pct + 0.5)
        left = f"{rounded}%"
        used_text = f"{100 - rounded}%"
        if 0 < pct < 1:
            left, used_text = "less than 1%", "over 99%"
        elif 99 < pct < 100:
            left, used_text = "over 99%", "less than 1%"
        balance = f"You have {left} of your included online tutoring left."
        if self.scope == "device":
            balance = f"On this device, you have {left} of this profile's tutoring allowance left."
        action = "used or reserved" if self.reserved else "used"
        date = datetime.fromtimestamp(self.until_ms / 1000, UTC).strftime("%d %B at %H:%M UTC").lstrip("0")
        end = "Your current paid period ends" if self.period_ends else "Your allowance resets"
        answer = f"{balance} You've {action} {used_text} this allowance month. {end} on {date}."
        if self.device_remaining is not None:
            device_pct = self.device_remaining * 100
            device_left = "less than 1%" if 0 < device_pct < 1 else f"{int(device_pct + 0.5)}%"
            answer += f" This device has {device_left} of its own tutoring allowance left."
        return answer
