"""Capability-off notices (PRM-17) and one-shot transcript reminders (PRM-16).

Notices are switches decided per turn from engine state and rendered into the `<tutor-context>` block, never into
tiers 1 to 7, so the model never narrates an action it cannot take ("I'll remember that" with saving off).

A reminder is a short line keyed by a reminder key. The ledger sends a key's line once for each new value: the same
key and value again sends nothing; a changed value is sent once more. The first value the engine observes for a state
key (the session's opening mode, say) is its baseline and is not announced. Sent reminders stay in the session's
transcript tier at the place they were sent, so later prompts still carry them until compaction."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

REMINDER_KEYS = (
    "checker.first_wrong_step",
    "mode.changed",
    "rung.changed",
    "budget.threshold",
    "gate.nudge",
    "capability.changed",
    "compaction.done",
)
NOTICE_IDS = ("memory_off", "vision_off", "cloud_off", "visuals_off", "checks_off")
# State keys whose first value is a baseline, not news.
BASELINE_KEYS = frozenset({"mode.changed", "rung.changed", "capability.changed"})


class ReminderError(ValueError):
    pass


@dataclass(frozen=True)
class NoticeText:
    notices: dict[str, str]
    reminders: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> NoticeText:
        data = yaml.safe_load(path.read_text()) if path.is_file() else {}
        notices = dict((data or {}).get("notices") or {})
        reminders = dict((data or {}).get("reminders") or {})
        unknown = sorted(set(notices) - set(NOTICE_IDS)) + sorted(set(reminders) - set(REMINDER_KEYS))
        if unknown:
            raise ReminderError(f"unknown notice or reminder ids: {unknown}")
        return cls(notices, reminders)

    @classmethod
    def empty(cls) -> NoticeText:
        return cls({}, {})


def capability_notices(text: NoticeText, *, memory_saving: bool | None = None, vision: bool | None = None,
                       cloud: bool | None = None, visuals: bool | None = None,
                       checks: bool | None = None) -> list[str]:  # fmt: skip
    """One line per capability that is off this turn (None: not known here, so nothing is said)."""
    state = {"memory_off": memory_saving, "vision_off": vision, "cloud_off": cloud, "visuals_off": visuals,
             "checks_off": checks}  # fmt: skip
    return [text.notices[n] for n in NOTICE_IDS if state[n] is False and n in text.notices]


@dataclass
class Reminder:
    key: str
    value: str
    text: str
    position: int  # the transcript length when it was sent: it sits after that many messages


@dataclass
class ReminderLedger:
    """One session's reminders: the last value sent per key, and every reminder sent, in order."""

    text: NoticeText
    max_chars: int = 300
    last: dict[str, str] = field(default_factory=dict)
    sent: list[Reminder] = field(default_factory=list)

    def offer(self, key: str, value: str, position: int) -> Reminder | None:
        """Send `key`'s line for `value` unless that value was the last one sent (or is the key's baseline)."""
        if key not in REMINDER_KEYS:
            raise ReminderError(f"unknown reminder key {key}")
        if self.last.get(key) == value:
            return None
        baseline = key in BASELINE_KEYS and key not in self.last
        self.last[key] = value
        if baseline:
            return None
        line = self.text.reminders.get(key, "{value}").format(value=value)
        if len(line) > self.max_chars:
            line = line[: self.max_chars - 1].rstrip() + "…"
        reminder = Reminder(key, value, line, position)
        # A newer value supersedes the older line of the same key: only the latest per key stays in the transcript,
        # so a long session carries at most one line per key.
        self.sent = [r for r in self.sent if r.key != key] + [reminder]
        return reminder

    def placed(self, transcript_length: int) -> list[tuple[int, str]]:
        """(position, line) of the reminders in the transcript, for interleaving into the transcript tier."""
        return [(min(r.position, transcript_length), r.text) for r in self.sent]
