"""Per-profile settings and their effective values (DAT-15, D87).

The canonical set is the student preferences of the API `Settings` contract. Every change appends
a `setting_change` event and updates the `settings` row, which caches the value, its source and the event's stamp.
Unset keys take their configured default. Locks (an active mock sitting, the restrict-only safety configuration)
override the stored value in the effective result and refuse a student write to the locked key."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from agent_core.config import Settings
from agent_core.store.events import EventLog
from agent_core.store.migration_v8 import CHARACTERS, DEFAULT_CHARACTER
from agent_core.store.sqlite import transaction

RETIRED_POLICY_KEY = "solution_policy"
PREFERENCES = ("low_power", "text_scale", "locale", "memory_saving", "notifications", "reduced_motion", "read_aloud",
               "easy_read", "extra_time_percent", "speech_rate", "speech_voice", "theme",
               "character_shortcuts", "tutor_name", "tutor_avatar", "faster_tutor", "wake_word",
               "checkin_notifications")  # fmt: skip
KEYS = PREFERENCES
# Keys that exist nowhere (TCH-13, D53, D54): a write of any of them is refused by the API contract and the table.
REMOVED = ("daily_minutes", "allowed_modes", "study_hours", "homework_hours", "locked_keys", "retention_days",
           "backup", "cloud_model_tier", "language")  # fmt: skip


def tutor_avatar(value: Any) -> str:
    """The tutor character a stored or imported value names: itself when it is one of the six of TABLE DSN-26a,
    otherwise Jana; an id from before the characters (avatar.default, avatar.disc_N; D103, G119) reads, and is
    written back, as Jana."""
    return value if isinstance(value, str) and value in CHARACTERS else DEFAULT_CHARACTER


class SettingsError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def tutor_name(value: Any) -> str:
    """A tutor name the student chose, as stored: the rules of a profile's display name (PRV-03, PRM-03): 1 to 40
    characters once trimmed, printable, on one line."""
    name = value.strip() if isinstance(value, str) else ""
    if not 1 <= len(name) <= 40 or not name.isprintable():
        raise SettingsError("validation_failed", "a tutor name is 1 to 40 printable characters on one line")
    return name


class ProfileSettings:
    def __init__(
        self,
        conn: sqlite3.Connection,
        events: EventLog,
        settings: Settings,
        clock_ms: Callable[[], int],
        locks: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.conn = conn
        self.events = events
        self.settings = settings
        self.clock_ms = clock_ms
        self.locks = locks or (lambda: {})

    def stored(self) -> dict[str, tuple[Any, str]]:
        rows = self.conn.execute("SELECT key, value, source FROM settings").fetchall()
        return {str(k): (json.loads(v), str(s)) for k, v, s in rows}

    def effective(self) -> dict[str, Any]:
        values = {k: getattr(self.settings, k) for k in KEYS}
        values.update({k: v for k, (v, _) in self.stored().items() if k in KEYS})
        values.update(self.locks())
        values["tutor_avatar"] = tutor_avatar(values["tutor_avatar"])
        return values

    def patch(self, body: Mapping[str, Any], actor: str) -> dict[str, Any]:
        """Apply the open profile's validated preferences; a refused key leaves the whole patch unchanged."""
        if RETIRED_POLICY_KEY in body:
            raise SettingsError("validation_failed", "the answer policy is fixed at on_request")
        if "tutor_name" in body:
            body = {**body, "tutor_name": tutor_name(body["tutor_name"])}
        if "tutor_avatar" in body:
            body = {**body, "tutor_avatar": tutor_avatar(body["tutor_avatar"])}
        locked = self.locks()
        refused = [k for k in body if k in locked and body[k] != locked[k]]
        if refused:
            raise SettingsError("tutor_locked", f"locked while it is in force: {', '.join(sorted(refused))}")
        current = self.effective()
        now = self.clock_ms()
        with transaction(self.conn):
            for name in [k for k in KEYS if k in body]:
                value = body[name]
                if value == current[name]:
                    continue
                source = "student"
                event = self.events.append(
                    "setting_change",
                    {"setting": name, "old_value": current[name], "new_value": value,
                     "actor_role": "student"},
                    source="student",
                )  # fmt: skip
                self.conn.execute(
                    "INSERT INTO settings (key, value, source, set_by_event_id, hlc, updated_at) VALUES (?, ?, ?, ?,"
                    " ?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value, source = excluded.source,"
                    " set_by_event_id = excluded.set_by_event_id, hlc = excluded.hlc, updated_at = excluded.updated_at",
                    (name, json.dumps(value), source, event.event_id, event.hlc, now),
                )
        return self.effective()
