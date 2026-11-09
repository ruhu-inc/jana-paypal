"""The parent views on the device (TCH-12, TCH-14, UXS-33, JRN-13): the weekly digest and the alerts, both read from
projections and events only, in the open profile's learner database.

- The digest is due each week on Sunday at `parent_digest_hour` local time (D53: fixed, not a setting) and covers that
  Monday to Sunday: time studied by day, the KCs that reached `mastered` (a passed mastery check that week), the KCs to
  watch (`learning` for over `parent_watch_learning_days`, or `moved_on`), mock results, full solutions viewed and
  bottom-out hints with the week's steps, the streak and the days to the nearest exam. It holds numbers, ids and the
  packs' own topic titles and KC statements; the words around them are the client's templates. It never reads a
  transcript, a note, a saved question or a safety flag. Once due it is written to `digests`; it is rebuilt on each read
  until its week is over, then kept as written. The Reflector's optional paragraph is not written.
- Alerts are written by code into `alerts` when they are read, per TABLE TCH-14a; `wellbeing_message_shown` is written
  by the engine as the calm screen shows (`tutor_flows.record_wellbeing_shown`, category only, never for an abuse
  disclosure). Each trigger is written once (its `key` in the payload); nothing here sends anything anywhere.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from agent_core.config import Settings
from agent_core.engine.exam_dates import ExamDesk
from agent_core.learner import model
from agent_core.learner.mastery import DAY_MS, MasteryParams, subject_of
from agent_core.learner.projections import local_date, week_start
from agent_core.store.ids import new_ulid
from agent_core.store.pack_index import PackIndex
from agent_core.store.sqlite import transaction

HOUR_MS = 3_600_000
SUNDAY = 6  # days after the Monday a week starts on
ALERT_SEVERITY = {"inactive": "info", "bottom_out_heavy": "attention", "mastery_drop": "info", "mock_result": "info",
                  "wellbeing_message_shown": "urgent"}  # fmt: skip
MARKED = ("marked", "reviewed")
# An alert due: its kind, the key that writes it once, its payload beyond the key and when it became due.
Trigger = tuple[str, str, dict[str, Any], int]


class ParentDesk:
    """The digest and alerts of the open profile, on its learner database and the installed packs."""

    def __init__(self, conn: sqlite3.Connection, params: MasteryParams, content: PackIndex, settings: Settings, *,
                 clock_ms: Callable[[], int]) -> None:  # fmt: skip
        self.conn, self.params, self.content, self.settings, self.clock_ms = conn, params, content, settings, clock_ms
        self.offset_ms = params.learner_day_offset_minutes * 60_000

    # --- time ----------------------------------------------------------------------------------------------------

    def day_start(self, day: int) -> int:
        """The first millisecond of a local day."""
        return day * DAY_MS - self.offset_ms

    def due_week(self, now: int) -> int:
        """The Monday of the latest week whose digest is due: this week from Sunday at the digest hour, else last."""
        today = self.params.local_day(now)
        monday = week_start(today)
        hour = ((now + self.offset_ms) % DAY_MS) // HOUR_MS
        return monday if today - monday == SUNDAY and hour >= self.settings.parent_digest_hour else monday - 7

    # --- the digest (TCH-12) -------------------------------------------------------------------------------------

    def latest_digest(self) -> dict[str, Any]:
        """The latest due week's digest: written when first due, rebuilt on each read until its week is over."""
        now = self.clock_ms()
        monday = self.due_week(now)
        week = local_date(monday)
        row = self.conn.execute("SELECT digest_id, content, viewed_at FROM digests WHERE week_start = ?",
                                (week,)).fetchone()  # fmt: skip
        if row is not None and json.loads(row[1]).get("final"):
            content: dict[str, Any] = json.loads(row[1])
            if row[2] is None:
                self.conn.execute("UPDATE digests SET viewed_at = ? WHERE digest_id = ?", (now, row[0]))
            return content
        content = self.build(monday, now)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO digests (digest_id, week_start, content, generated_at, viewed_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (week_start) DO UPDATE SET content = excluded.content,"
                " generated_at = excluded.generated_at, viewed_at = coalesce(digests.viewed_at, excluded.viewed_at)",
                (new_ulid(now), week, json.dumps(content, sort_keys=True), now, now),
            )
        return content

    def build(self, monday: int, now: int) -> dict[str, Any]:
        """One week's digest from the projections (and the passed checks and mock sittings it names): numbers, ids and
        pack titles only."""
        start, end = self.day_start(monday), self.day_start(monday + 7)
        days = [local_date(monday + i) for i in range(7)]
        daily = {day: (minutes, sessions, solutions) for day, minutes, sessions, solutions in self.conn.execute(
            "SELECT day, minutes_active, sessions, solutions_viewed FROM engagement_daily WHERE day >= ? AND day <= ?",
            (days[0], days[-1]))}  # fmt: skip
        day_minutes = [int(daily.get(day, (0, 0, 0))[0]) for day in days]
        by_day = [{"day": day, "minutes": m} for day, m in zip(days, day_minutes, strict=True)]
        minutes = sum(day_minutes)
        sessions = sum(int(v[1]) for v in daily.values())
        steps, bottom_outs, hinted = self.steps(start, end)
        activity = "none" if sessions == 0 and minutes == 0 and steps == 0 else (
            "low" if minutes < self.settings.parent_digest_min_minutes else "enough")  # fmt: skip
        # The streak as the week left it: the length on its last day that counted (the engagement projection).
        streak = self.conn.execute(
            "SELECT streak_length FROM engagement_daily WHERE day >= ? AND day <= ? AND streak_length > 0"
            " ORDER BY day DESC LIMIT 1",
            (days[0], days[-1]),
        ).fetchone()
        return {
            "week_start": days[0],
            "week_end": days[-1],
            "generated_at": now,
            "final": now >= end,
            "activity": activity,
            "time_by_day": by_day,
            "minutes_total": minutes,
            "days_studied": sum(1 for m in day_minutes if m > 0),
            "sessions": sessions,
            "mastered": [self.kc(kc) for kc in self.mastered(start, end)],
            "to_watch": self.to_watch(end),
            "mocks": self.mocks(start, end),
            "solutions_viewed": sum(int(v[2]) for v in daily.values()),
            "bottom_out_hints": bottom_outs,
            "steps": steps,
            "steps_with_hints": hinted,
            "streak_days": int(streak[0]) if streak else 0,
            "exam": self.exam(min(now, end - 1)),
        }

    def steps(self, start: int, end: int) -> tuple[int, int, int]:
        """The week's closed steps (attempts projection, mocks aside), those that ended at the bottom-out hint (rung 5)
        and those with any hint."""
        row = self.conn.execute(
            "SELECT count(*), coalesce(sum(rung >= 5), 0), coalesce(sum(hints > 0), 0) FROM (SELECT max(max_rung)"
            " AS rung, max(hints_used) AS hints FROM attempts WHERE occurred_at >= ? AND occurred_at < ?"
            " AND mode != 'mock' GROUP BY session_id, item_ref, step_id)",
            (start, end),
        ).fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    def mastered(self, start: int, end: int) -> list[str]:
        """KCs whose mastery check passed in the window (the moment a KC reaches `mastered`, LRN-09)."""
        return [str(kc) for (kc,) in self.conn.execute(
            "SELECT DISTINCT json_extract(payload, '$.kc_id') FROM events WHERE type = 'mastery_check'"
            " AND erased_at IS NULL AND json_extract(payload, '$.check_state') = 'passed' AND occurred_at >= ?"
            " AND occurred_at < ? ORDER BY 1", (start, end))]  # fmt: skip

    def to_watch(self, end: int) -> list[dict[str, Any]]:
        """KCs `moved_on`, or `learning` since a first attempt over the watch period before the week's end."""
        first = {str(kc): int(at) for kc, at in self.conn.execute(
            "SELECT json_extract(value, '$.kc_id'), min(occurred_at) FROM attempts, json_each(attempts.kc_ids)"
            " GROUP BY 1")}  # fmt: skip
        long_ago = end - self.settings.parent_watch_learning_days * DAY_MS
        out = []
        for kc, view in sorted(model.kc_views(self.conn, self.params, end - 1).items()):
            if view.state == "moved_on":
                out.append({**self.kc(kc), "reason": "moved_on"})
            elif view.state == "learning" and first.get(kc, end) < long_ago:
                out.append({**self.kc(kc), "reason": "learning_long"})
        return out

    def kc(self, kc_id: str) -> dict[str, Any]:
        topic = self.content.topic_of(kc_id)
        return {"kc_id": kc_id, "subject": subject_of(kc_id), "topic_id": topic,
                "topic_title": self.content.topic_title(topic) if topic else None,
                "statement": self.content.kc_statement(kc_id)}  # fmt: skip

    def mocks(self, start: int, end: int) -> list[dict[str, Any]]:
        """Sittings submitted in the window, with their marks once marked; a pack paper's title, never an imported
        paper's (the student named it)."""
        out = []
        for exam_id, paper, status, submitted in self.conn.execute(
            "SELECT exam_id, paper_ref, status, submitted_at FROM exams WHERE submitted_at >= ? AND submitted_at < ?"
            " ORDER BY submitted_at, exam_id",
            (start, end),
        ):
            awarded, available = self.conn.execute(
                "SELECT sum(marks_awarded), sum(marks_available) FROM exam_answers WHERE exam_id = ?"
                " AND marks_awarded IS NOT NULL",
                (exam_id,),
            ).fetchone()
            record = self.content.record("mock_papers", str(paper))
            marked = status in MARKED and available is not None
            out.append({"exam_id": "exm_" + str(exam_id), "paper_id": str(paper), "submitted_at": int(submitted),
                        "title": str(record["title"]) if record is not None else None,
                        "marks_awarded": float(awarded) if marked else None,
                        "marks_available": float(available) if marked else None})  # fmt: skip
        return out

    def exam(self, at: int) -> dict[str, Any] | None:
        """The days to the nearest exam still to come: the learner's own dates (LRN-44), else a profile exam date."""
        desk = ExamDesk(self.conn, self.settings, clock_ms=self.clock_ms,
                        day_offset_minutes=self.params.learner_day_offset_minutes)  # fmt: skip
        found = desk.countdown(at)
        if found is not None:
            return {"subject": found["subject"], "days": int(found["days"])}
        row = self.conn.execute("SELECT subjects, exam_date FROM profile").fetchone()
        today = self.params.local_day(at)
        dates = []
        for entry in json.loads((row[0] if row else None) or "[]"):
            subject, day = (entry, row[1]) if isinstance(entry, str) else (entry.get("subject"), entry.get("exam_date")
                                                                            or row[1])  # fmt: skip
            exam_ms = model.exam_day_ms(day) if isinstance(day, str) else None
            if isinstance(subject, str) and exam_ms is not None and exam_ms // DAY_MS >= today:
                dates.append((exam_ms // DAY_MS - today, subject))
        if not dates:
            return None
        days, subject = min(dates)
        return {"subject": subject, "days": days}

    # --- alerts (TCH-14) -----------------------------------------------------------------------------------------

    def alerts(self) -> list[dict[str, Any]]:
        """The open profile's alerts not dismissed, newest first, after writing any newly due (TABLE TCH-14a). Kind,
        severity and date only, with the KC of a mastery drop; never a payload's other contents."""
        self.raise_due()
        rows = self.conn.execute(
            "SELECT alert_id, kind, severity, payload, created_at FROM alerts WHERE dismissed_at IS NULL"
            " AND audience = 'parent' ORDER BY created_at DESC, alert_id DESC"
        ).fetchall()
        out = []
        for alert_id, kind, severity, payload, created in rows:
            kc = json.loads(payload).get("kc_id") if kind == "mastery_drop" else None
            out.append({"alert_id": "alr_" + str(alert_id), "kind": kind, "severity": severity,
                        "created_at": int(created), "kc_id": kc if isinstance(kc, str) else None})  # fmt: skip
        return out

    def dismiss(self, alert_id: str) -> bool:
        """Put an alert away; it then goes with the retention period of TABLE DAT-08a."""
        cursor = self.conn.execute(
            "UPDATE alerts SET dismissed_at = ? WHERE alert_id = ? AND dismissed_at IS NULL",
            (self.clock_ms(), alert_id.removeprefix("alr_")),
        )
        if cursor.rowcount > 0:
            return True
        found = self.conn.execute("SELECT 1 FROM alerts WHERE alert_id = ?", (alert_id.removeprefix("alr_"),))
        return found.fetchone() is not None

    def raise_due(self) -> int:
        """Write every alert now due that is not written yet; a trigger before the look-back is left alone."""
        now = self.clock_ms()
        since = now - self.settings.alert_lookback_days * DAY_MS
        found: list[Trigger] = []
        found += self._inactive(now)
        found += self._bottom_out(now)
        found += self._mastery_drop(now)
        found += self._mock_result(since)
        written = 0
        with transaction(self.conn):
            for kind, key, payload, created in found:
                if created < since or self._written(kind, key):
                    continue
                self.conn.execute(
                    "INSERT INTO alerts (alert_id, kind, severity, audience, payload, created_at)"
                    " VALUES (?, ?, ?, 'parent', ?, ?)",
                    (new_ulid(created), kind, ALERT_SEVERITY[kind], json.dumps({"key": key, **payload}), created),
                )
                written += 1
        return written

    def _written(self, kind: str, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM alerts WHERE kind = ? AND json_extract(payload, '$.key') = ?",
                                 (kind, key)).fetchone() is not None  # fmt: skip

    def _inactive(self, now: int) -> list[Trigger]:
        """No session for `alert_inactive_days` since the last one started (a profile never used raises none)."""
        row = self.conn.execute("SELECT event_id, occurred_at FROM events WHERE type = 'session_started'"
                                " ORDER BY occurred_at DESC, seq DESC LIMIT 1").fetchone()  # fmt: skip
        if row is None:
            return []
        due = int(row[1]) + self.settings.alert_inactive_days * DAY_MS
        return [("inactive", "inactive:" + str(row[0]), {}, due)] if now >= due else []

    def _bottom_out(self, now: int) -> list[Trigger]:
        """This week or last: over the share of the week's steps ended at the bottom-out hint."""
        out: list[Trigger] = []
        this_week = week_start(self.params.local_day(now))
        for monday in (this_week - 7, this_week):
            steps, bottom_outs, _ = self.steps(self.day_start(monday), self.day_start(monday + 7))
            heavy = steps >= self.settings.alert_bottom_out_min_steps
            if heavy and bottom_outs / steps > self.settings.alert_bottom_out_share:
                created = min(now, self.day_start(monday + 7) - 1)
                out.append(("bottom_out_heavy", "bottom_out_heavy:" + local_date(monday), {}, created))
        return out

    def _mastery_drop(self, now: int) -> list[Trigger]:
        """A KC confirmed by a passed check that now reads `needs_review`, once per passed check."""
        out: list[Trigger] = []
        for kc, view in sorted(model.kc_views(self.conn, self.params, now).items()):
            if view.state != "needs_review":
                continue
            passed = self.conn.execute(
                "SELECT event_id FROM events WHERE type = 'mastery_check' AND erased_at IS NULL"
                " AND json_extract(payload, '$.kc_id') = ? AND json_extract(payload, '$.check_state') = 'passed'"
                " ORDER BY occurred_at DESC, seq DESC LIMIT 1",
                (kc,),
            ).fetchone()
            if passed is not None:
                out.append(("mastery_drop", f"mastery_drop:{kc}:{passed[0]}", {"kc_id": kc}, now))
        return out

    def _mock_result(self, since: int) -> list[Trigger]:
        """Each mock sitting submitted (TABLE TCH-14a `mock_result`), at the time it was submitted."""
        rows = self.conn.execute("SELECT exam_id, submitted_at FROM exams WHERE submitted_at >= ?", (since,))
        return [("mock_result", "mock_result:" + str(exam_id), {}, int(submitted)) for exam_id, submitted in rows]
