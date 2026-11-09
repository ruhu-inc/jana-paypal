"""Exam dates, the countdown and device-local reminders (LRN-44, D119).

A learner sets an exam date per subject or per paper (a name such as "Physics paper 2", a date and optionally a
time). The nearest one still to come is the countdown Home and the plan show ("12 days to Physics paper 2"), and in
the `exam_frontload_days` before it the plan's main block comes from that subject's weakest KCs (`PlanDesk`).
Reminders are computed here, on the device, when the app is open: `exam_reminder_days` days before (proposal: 14, 7,
3 and 1) and the morning of the exam, each shown until the learner puts it away; a learner can switch an exam's
reminders off. Dates and reminders live only in the `exam_dates` table: never in an event, so never synced, and never
in telemetry or a cloud request.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from agent_core.learner.mastery import DAY_MS
from agent_core.learner.model import exam_day_ms
from agent_core.store.ids import new_ulid
from agent_core.store.sqlite import transaction

SUBJECTS = ("mathematics", "physics", "chemistry", "biology", "accounting")
TIME = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
KINDS = {14: "d14", 7: "d7", 3: "d3", 1: "d1"}


class ExamDateRefused(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ExamDesk:
    def __init__(self, conn: Any, settings: Any, *, clock_ms: Callable[[], int], day_offset_minutes: int = 0) -> None:
        self.conn, self.settings, self.clock_ms = conn, settings, clock_ms
        self.offset_ms = day_offset_minutes * 60_000

    # --- reading -----------------------------------------------------------------------------------------------------

    def _rows(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT exam_id,subject,name,date,time,reminders,dismissed FROM exam_dates ORDER BY date,time,exam_id"
        ).fetchall()
        return [
            {"exam_id": r[0], "subject": r[1], "name": r[2], "date": r[3], "time": r[4], "reminders": bool(r[5]),
             "_dismissed": json.loads(r[6])}
            for r in rows
        ]  # fmt: skip

    def today(self, now: int | None = None) -> int:
        return ((self.clock_ms() if now is None else now) + self.offset_ms) // DAY_MS

    def days_to(self, row: Mapping[str, Any], now: int | None = None) -> int:
        return int(exam_day_ms(str(row["date"])) or 0) // DAY_MS - self.today(now)

    def upcoming(self, now: int | None = None) -> list[dict[str, Any]]:
        """The exams still to come (today's included), soonest first."""
        return [r for r in self._rows() if self.days_to(r, now) >= 0]

    def subject_days(self, now: int | None = None) -> dict[str, int]:
        """Each subject's nearest exam still to come, as the UTC midnight of its date (the planner's exam day)."""
        out: dict[str, int] = {}
        for row in self.upcoming(now):
            day = exam_day_ms(row["date"])
            if day is not None and (row["subject"] not in out or day < out[row["subject"]]):
                out[row["subject"]] = day
        return out

    def countdown(self, now: int | None = None) -> dict[str, Any] | None:
        """The nearest exam still to come, for Home and the plan."""
        rows = self.upcoming(now)
        if not rows:
            return None
        row = rows[0]
        return {**{k: row[k] for k in ("exam_id", "subject", "name", "date", "time")}, "days": self.days_to(row, now)}

    def reminders_due(self, now: int | None = None) -> list[dict[str, Any]]:
        """Reminders due now, on this device only: on the reminder days before an exam, and from the morning hour on
        the day itself; never one the learner put away or for an exam whose reminders are off."""
        at = self.clock_ms() if now is None else now
        hour = ((at + self.offset_ms) % DAY_MS) // 3_600_000
        due = []
        for row in self.upcoming(at):
            if not row["reminders"]:
                continue
            days = self.days_to(row, at)
            if days == 0:
                kind = "day" if hour >= self.settings.exam_reminder_morning_hour else None
            else:
                kind = KINDS.get(days) if days in self.settings.exam_reminder_days else None
            if kind is None or kind in row["_dismissed"]:
                continue
            due.append({"exam_id": row["exam_id"], "kind_id": kind, "subject": row["subject"], "name": row["name"],
                        "days": days})  # fmt: skip
        return due

    @staticmethod
    def public(row: Mapping[str, Any]) -> dict[str, Any]:
        return {k: row[k] for k in ("exam_id", "subject", "name", "date", "time", "reminders")}

    def list(self) -> dict[str, Any]:
        """`ExamDateList`: every date, soonest first (past ones after), the countdown and the reminders due."""
        now = self.clock_ms()
        rows = self._rows()
        coming = [r for r in rows if self.days_to(r, now) >= 0]
        past = [r for r in rows if self.days_to(r, now) < 0]
        return {
            "exams": [self.public(r) for r in coming + past],
            "countdown": self.countdown(now),
            "reminders_due": self.reminders_due(now),
        }

    def get(self, exam_id: str) -> dict[str, Any]:
        row = next((r for r in self._rows() if r["exam_id"] == exam_id), None)
        if row is None:
            raise ExamDateRefused("not_found")
        return row

    # --- changes -----------------------------------------------------------------------------------------------------

    @staticmethod
    def _valid(subject: Any, name: Any, day: Any, time: Any) -> None:
        if subject not in SUBJECTS:
            raise ExamDateRefused("invalid_subject")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ExamDateRefused("invalid_name")
        try:
            date.fromisoformat(day)
        except (TypeError, ValueError):
            raise ExamDateRefused("invalid_date") from None
        if time is not None and (not isinstance(time, str) or not TIME.match(time)):
            raise ExamDateRefused("invalid_time")

    def create(self, body: Mapping[str, Any]) -> dict[str, Any]:
        self._valid(body.get("subject"), body.get("name"), body.get("date"), body.get("time"))
        count = self.conn.execute("SELECT count(*) FROM exam_dates").fetchone()[0]
        if count >= self.settings.exam_dates_max:
            raise ExamDateRefused("too_many_exam_dates")
        now = self.clock_ms()
        exam_id = "exd_" + new_ulid(now)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO exam_dates (exam_id,subject,name,date,time,reminders,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (exam_id, body["subject"], body["name"].strip(), body["date"], body.get("time"),
                 int(body.get("reminders", True) is not False), now, now),
            )  # fmt: skip
        return self.public(self.get(exam_id))

    def patch(self, exam_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        row = self.get(exam_id)
        if not body or set(body) - {"name", "date", "time", "reminders", "dismiss"}:
            raise ExamDateRefused("invalid_change")
        name = body.get("name", row["name"])
        day = body.get("date", row["date"])
        time = body["time"] if "time" in body else row["time"]
        self._valid(row["subject"], name, day, time)
        reminders = body.get("reminders", row["reminders"])
        if not isinstance(reminders, bool):
            raise ExamDateRefused("invalid_change")
        dismissed = list(row["_dismissed"])
        if "dismiss" in body:
            if body["dismiss"] not in ("d14", "d7", "d3", "d1", "day"):
                raise ExamDateRefused("invalid_change")
            dismissed = sorted({*dismissed, body["dismiss"]})
        if "date" in body and day != row["date"]:
            dismissed = []  # a moved exam reminds again
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE exam_dates SET name=?,date=?,time=?,reminders=?,dismissed=?,updated_at=? WHERE exam_id=?",
                (name.strip(), day, time, int(reminders), json.dumps(dismissed), self.clock_ms(), exam_id),
            )
        return self.public(self.get(exam_id))

    def delete(self, exam_id: str) -> dict[str, Any]:
        self.get(exam_id)
        with transaction(self.conn):
            self.conn.execute("DELETE FROM exam_dates WHERE exam_id=?", (exam_id,))
        return self.list()
