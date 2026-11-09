"""The plan desk: optional daily plans, the student's choices, the support level, reminders and the progress view
(LRN-27 to LRN-35, UXS-12, UXS-27).

Every proposal is computed by code (`agent_core.learner.planner`) from the learner model at this moment and
appended as a `plan_generated` event with a new `plan_id`; the student's Start, Change and Not today are
`plan_decision` events, and a change also generates the changed plan as a new proposal (LRN-31). The `study_plans`
projection keeps each plan with its decision and its blocks' progress, so stopping keeps finished work and the next
read resumes at the first unfinished block. Nothing here limits learning: a plan can be asked for at any hour, as
often as wanted, with no PIN and no daily cap (D54, D86), and it all works offline.

The Home plan card is `card_plan` (events contract); its request id is `req_` + the plan id, answered through the
existing card-answer route (`POST /v1/cards/{request_id}/answer`): action `start`, action `change` with option
`alt_0`..`alt_2` or `shorten`, or `skipped: true` for Not today.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from agent_core.config import Settings
from agent_core.engine.assessment.bands import pack_scale, predict_band
from agent_core.engine.exam_dates import ExamDesk
from agent_core.engine.learn import SUBJECT_CODES
from agent_core.learner import model, planner, policies
from agent_core.learner.mastery import BANDS, DAY_MS, KcEvidence, MasteryParams, kc_probability, subject_of
from agent_core.learner.profile import merge_profile
from agent_core.learner.projections import local_date, week_start
from agent_core.learner.service import Learner, NewEvent
from agent_core.memory.queries import studied_kcs, study_activity_counts
from agent_core.store.assessment import AssessmentStore
from agent_core.store.ids import new_ulid
from agent_core.store.pack_index import PackIndex

REQUEST_PREFIX = "req_"
KIND_TEXT = {"warmup": "Warm-up", "mixed": "Mixed review", "reflect": "Look back at today", "mock": "Timed paper"}


class PlanRefused(ValueError):
    """A plan command that cannot apply: an unknown plan, or a choice on a plan already decided."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PlanDesk:
    def __init__(
        self,
        learner: Learner,
        content: PackIndex,
        settings: Settings,
        *,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.learner, self.content, self.settings, self.clock_ms = learner, content, settings, clock_ms
        self.decisions: list[policies.Decision] = []  # this desk's policy decisions, for the trace (LRN-15)
        self._proposed: dict[str, str] = {}  # client request key -> plan id: Keep learning starts one plan per request

    @property
    def conn(self) -> Any:
        return self.learner.conn

    @property
    def params(self) -> MasteryParams:
        return self.learner.projector.ctx.params

    # --- what the planner reads ----------------------------------------------------------------------------------

    def profile(self) -> dict[str, Any]:
        """Level, subjects with exam dates and support level from the profile; an empty profile plans nothing."""
        row = self.conn.execute("SELECT level, subjects, exam_date, support_level FROM profile").fetchone()
        if row is None:
            return {"level": "o", "subjects": [], "support_level": "standard"}
        subjects: list[tuple[str, str | None]] = []
        for entry in json.loads(row[1] or "[]"):
            if isinstance(entry, str):
                subjects.append((entry, row[2]))
            elif isinstance(entry, dict) and isinstance(entry.get("subject"), str):
                subjects.append((entry["subject"], entry.get("exam_date") or row[2]))
        return {"level": str(row[0] or "O").lower(), "subjects": subjects, "support_level": row[3] or "standard"}

    def _kcs(self, subjects: Sequence[str], level: str) -> list[str]:
        prefixes = tuple(f"kc.{SUBJECT_CODES[s]}.{level}." for s in subjects if s in SUBJECT_CODES)
        return sorted(k for k in self.content.kcs if k.startswith(prefixes))

    def exams(self) -> ExamDesk:
        """The learner's own exam dates (LRN-44): device-local, never events."""
        return ExamDesk(self.conn, self.settings, clock_ms=self.clock_ms,
                        day_offset_minutes=self.params.learner_day_offset_minutes)  # fmt: skip

    def exam_days(self) -> dict[str, int]:
        """Each subject's exam day: the profile's exam date, or the nearest exam date the learner set for it that is
        still to come when that is sooner (LRN-44)."""
        out = {}
        for subject, exam in self.profile()["subjects"]:
            day = model.exam_day_ms(exam)
            if day is not None:
                out[subject] = day
        today_ms = self.params.local_day(self.clock_ms()) * DAY_MS
        for subject, day in self.exams().subject_days().items():
            if subject not in out or out[subject] < today_ms or day < out[subject]:
                out[subject] = day
        return out

    def state(self, now: int, chosen_kc: str | None = None) -> planner.PlannerState:
        p, s = self.params, self.settings
        profile = self.profile()
        today = p.local_day(now)
        views = model.kc_views(self.conn, p, now)
        theta = {r[0]: r[1] for r in self.conn.execute("SELECT subject, theta FROM subject_ability")}
        recent = now - s.final_recent_error_days * DAY_MS
        failed = {
            kc
            for (kc,) in self.conn.execute(
                "SELECT DISTINCT json_extract(value, '$.kc_id') FROM attempts, json_each(attempts.kc_ids)"
                " WHERE outcome = 'incorrect' AND occurred_at >= ?",
                (recent,),
            )
        }
        subjects = [name for name, _ in profile["subjects"]]
        countdown = self.exams().countdown(now)
        if countdown is not None and countdown["subject"] not in subjects and profile["subjects"]:
            subjects.append(countdown["subject"])  # an exam date set for a subject outside the profile's list
        infos: dict[str, planner.KcPlanInfo] = {}
        for kc in self._kcs(subjects, profile["level"]):
            view = views.get(kc)
            p_k = view.p_mastery if view else kc_probability(theta.get(subject_of(kc), 0.0), KcEvidence(), p,
                                                             self.content.reference_difficulty(kc), kc)  # fmt: skip
            infos[kc] = planner.KcPlanInfo(
                kc, subject_of(kc), view.state if view else "not_started", p_k, self.content.exam_weight(kc),
                self.content.kcs[kc].prerequisites, self.content.confusable_with(kc), kc in failed,
                view is not None and view.revisit_at is not None and view.revisit_at <= now,
            )  # fmt: skip
        exams = self.exam_days()
        due = model.due_cards(self.conn, p, now, exams, s.fsrs_exam_window_days)
        moved = tuple(
            k for k, v in sorted(views.items(), key=lambda kv: -(kv[1].last_seen_at or 0))
            if v.last_seen_at is not None and p.local_day(v.last_seen_at) == today
        )  # fmt: skip
        info = tuple(
            planner.SubjectInfo(name, (exams[name] // DAY_MS - today) if name in exams else None) for name in subjects
        )
        taken = self.conn.execute(
            "SELECT count(*) FROM study_plans WHERE day = ? AND status IN ('started', 'done', 'not_today')",
            (local_date(today),),
        ).fetchone()[0]
        frontload = (countdown["subject"] if countdown is not None
                     and countdown["days"] <= s.exam_frontload_days else None)  # fmt: skip
        return planner.PlannerState(info, infos, tuple((d.kc_id, d.retrievability) for d in due), moved,
                                    profile["support_level"] == "extra", taken, self._days_since_mock(now),
                                    chosen_kc, self._mock_follow_up(), frontload, self._study_due(now))  # fmt: skip

    def _study_due(self, now: int) -> tuple[tuple[str, str], ...]:
        """Study's own due cards (SPEC §19.4) as (deck_id, card_id): due by the end of the profile's local today,
        soonest first, at most `planner.STUDY_CARDS_MAX`, from decks whose source revision still exists and never a
        suspended card (a stale source's cards are suspended)."""
        p = self.params
        end = (p.local_day(now) + 1) * DAY_MS - p.learner_day_offset_minutes * 60_000
        rows = self.conn.execute(
            "SELECT f.deck_id, f.card_id FROM study_flashcards f JOIN study_decks d ON d.deck_id = f.deck_id"
            " JOIN study_revisions r ON r.source_id = d.source_id AND r.revision = d.revision"
            " WHERE f.due_at < ? AND json_extract(f.body, '$.suspended') = 0 ORDER BY f.due_at, f.card_id LIMIT ?",
            (end, planner.STUDY_CARDS_MAX),
        )
        return tuple((str(deck), str(card)) for deck, card in rows)

    def _mock_follow_up(self) -> tuple[str, ...]:
        """The weak KCs of the latest marked mock (its report's practice KCs: the questions that lost marks), which the
        planner adds to the plan until they are secure (ASM-20, LRN-28; D102)."""
        row = self.conn.execute(
            "SELECT exam_id FROM exams WHERE status IN ('marked', 'reviewed') ORDER BY submitted_at DESC, exam_id DESC"
            " LIMIT 1"
        ).fetchone()
        sitting = AssessmentStore(self.conn).get(row[0]) if row else None
        report = (sitting or {}).get("report") or {}
        return tuple(str(k) for k in report.get("practice_kc_ids") or ())

    def _days_since_mock(self, now: int) -> int | None:
        last = None
        for (hlc,) in self.conn.execute("SELECT hlc FROM events WHERE type = 'mock_submitted' AND erased_at IS NULL"):
            last = max(last or 0, int(hlc[:15]))
        for (items,) in self.conn.execute("SELECT items FROM study_plans WHERE status IN ('started', 'done')"):
            for block in json.loads(items)["blocks"]:
                if block["kind"] == "mock" and block.get("completed_at"):
                    last = max(last or 0, int(block["completed_at"]))
        return None if last is None else self.params.local_day(now) - self.params.local_day(last)

    # --- proposals --------------------------------------------------------------------------------------------------

    def propose(self, *, all_reviews: bool = False, kc_id: str | None = None, request_key: str | None = None
                ) -> dict[str, Any]:  # fmt: skip
        """A new proposal (Home's plan card, or Keep learning after one is done): a new plan id every time; the same
        `request_key` returns the plan it already started rather than a second one."""
        if request_key is not None and request_key in self._proposed:
            return self.view(self._proposed[request_key])
        self.refresh_support_level()
        now = self.clock_ms()
        if kc_id is not None and kc_id not in self.content.kcs:
            raise PlanRefused("unknown_kc")
        proposal = planner.plan(self.state(now, kc_id), self.settings, all_reviews=all_reviews)
        plan_id = self._generate(proposal, now)
        if request_key is not None:
            self._proposed[request_key] = plan_id
        return self.view(plan_id, alternatives=proposal.alternatives)

    def _generate(self, proposal: planner.Plan, now: int, decision: NewEvent | None = None) -> str:
        plan_id = new_ulid(now)
        events = [] if decision is None else [decision]
        events.append(NewEvent("plan_generated", proposal.payload(plan_id, local_date(self.params.local_day(now))),
                               source="engine", occurred_at=now))  # fmt: skip
        self.learner.record_batch(events)
        return plan_id

    def _row(self, plan_id: str) -> tuple[dict[str, Any], str, str, int]:
        row = self.conn.execute("SELECT items, status, day, updated_at FROM study_plans WHERE plan_id = ?",
                                (plan_id,)).fetchone()  # fmt: skip
        if row is None:
            raise PlanRefused("not_found")
        return json.loads(row[0]), row[1], row[2], row[3]

    def _stored(self, plan_id: str) -> planner.Plan:
        items, _, _, _ = self._row(plan_id)
        return planner.from_payload(items)

    def alternatives(self, plan_id: str) -> tuple[planner.Block, ...]:
        """The ranked alternatives to a proposal's main block, as the learner model ranks them now."""
        stored = self._stored(plan_id)
        fresh = planner.plan(self.state(self.clock_ms()), self.settings)
        main = {b.kc_ids for b in stored.blocks if b.kind in ("main", "mock")}
        options = [b for b in (*fresh.blocks, *fresh.alternatives) if b.kind == "main" and b.kc_ids not in main]
        return tuple(options[: self.settings.planner_alternatives])

    def current(self) -> dict[str, Any] | None:
        """The plan Home shows: the latest proposal of today that was not replaced by a change, else none."""
        today = local_date(self.params.local_day(self.clock_ms()))
        row = self.conn.execute(
            "SELECT p.plan_id FROM study_plans p JOIN events e ON e.type='plan_generated'"
            " AND json_extract(e.payload,'$.plan_id')=p.plan_id WHERE p.day=? AND p.status!='changed'"
            " ORDER BY e.hlc DESC,e.device_id DESC,e.origin_seq DESC LIMIT 1",
            (today,),
        ).fetchone()
        return self.view(row[0]) if row else None

    def view(self, plan_id: str, alternatives: Sequence[planner.Block] | None = None) -> dict[str, Any]:
        """The plan as Home shows it (PlanView): the next activity first, each block with an estimate and its progress,
        the card, and while it is proposed the alternatives Change offers. The deferred backlog is never shown."""
        items, status, day, updated = self._row(plan_id)
        blocks = []
        next_block = None
        for index, block in enumerate(items["blocks"]):
            done = block.get("completed_at") is not None
            started = status in ("started", "done")
            state = "done" if done else "in_progress" if started and next_block is None else "pending"
            if not done and next_block is None and status in ("proposed", "started"):
                next_block = index
            # A reflection block stored before the planner capped it can name more KCs than PlanBlock allows.
            kc_ids = list(block.get("kc_ids", []))
            if block["kind"] == "reflect":
                kc_ids = kc_ids[: planner.BLOCK_KCS_MAX]
            blocks.append({"index": index, "kind": block["kind"], "kc_ids": kc_ids,
                           "minutes": block["minutes"], "status": state, "done": block.get("done", 0),
                           "target": block.get("target", 0), **({"reason_code": block["reason_code"]}
                                                                if block.get("reason_code") else {}),
                           **({"study_cards": [{"deck_id": c["deck_id"], "card_id": c["card_id"]}
                                               for c in block["study_cards"][: planner.STUDY_CARDS_MAX]]}
                              if block.get("study_cards") else {})})  # fmt: skip
        if status == "proposed" and alternatives is None:
            alternatives = self.alternatives(plan_id)
        options = [b.payload() for b in (alternatives or ())] if status == "proposed" else []
        earlier_done = self.conn.execute(
            "SELECT count(*) FROM study_plans WHERE day = ? AND status = 'done' AND plan_id != ?", (day, plan_id)
        ).fetchone()[0]
        view = {
            "plan_id": plan_id,
            "request_id": REQUEST_PREFIX + plan_id,
            "date": day,
            "phase": items["phase"],
            "status": status,
            "minutes": items["minutes"],
            "blocks": blocks,
            "next_block": next_block,
            "alternatives": options,
            "keep_learning": bool(earlier_done),
            "decided_ts": updated if status != "proposed" else None,
            "countdown": self.exams().countdown(),
        }
        view["card"] = self.card(view)
        return view

    def card(self, view: Mapping[str, Any]) -> dict[str, Any]:
        """The `card_plan` body: each item names the activity and an approximate duration, never a deadline."""
        items = []
        for block in view["blocks"]:
            items.append({"id": f"b{block['index']}", "text": self.block_text(block)[:300],
                          "status": block["status"]})  # fmt: skip
        title = "Keep learning" if view["keep_learning"] else "Next up"
        return {"card_type": "plan", "title": title, "items": items[:12], "actions": ["start", "change"]}

    def block_text(self, block: Mapping[str, Any]) -> str:
        minutes = f"about {block['minutes']} min"
        if block["kind"] == "warmup" and block.get("study_cards"):
            count = len(block["study_cards"])
            return f"Warm-up: {count} Study card{'s' if count != 1 else ''} ({minutes})"
        if block["kind"] == "warmup":
            count = len(block["kc_ids"])
            return f"Warm-up: {count} review{'s' if count != 1 else ''} ({minutes})"
        if block["kind"] == "main" and len(block["kc_ids"]) > 1:  # the mock's weak topics (UXS-26)
            return f"Topics from your mock: practice ({minutes})"
        if block["kind"] == "main" and block["kc_ids"]:
            kc = block["kc_ids"][0]
            topic = self.content.topic_of(kc)
            name = (self.content.topic_title(topic) if topic else None) or self.content.kc_statement(kc) or kc
            started = self.conn.execute("SELECT attempts FROM kc_mastery WHERE kc_id = ?", (kc,)).fetchone()
            what = (
                "practice topics from your mock"
                if block.get("reason_code") == "mock_follow_up"
                else "practice"
                if started and started[0]
                else "lesson then practice"
            )
            return f"{name}: {what} ({minutes})"
        return f"{KIND_TEXT.get(block['kind'], block['kind'].replace('_', ' ').capitalize())} ({minutes})"

    # --- decisions (LRN-31, LRN-32) ---------------------------------------------------------------------------------

    def _decision(self, plan_id: str, decision: str, now: int, **extra: Any) -> NewEvent:
        return NewEvent("plan_decision", {"plan_id": plan_id, "decision": decision, **extra}, source="student",
                        occurred_at=now)  # fmt: skip

    def decide(self, plan_id: str, decision: str) -> dict[str, Any]:
        """Start or Not today: one `plan_decision`; Not today asks nothing further and keeps the streak rules."""
        if decision not in ("start", "not_today"):
            raise PlanRefused("invalid_decision")
        _, status, _, _ = self._row(plan_id)
        if status != "proposed":
            raise PlanRefused("already_decided")
        self.learner.record_batch([self._decision(plan_id, decision, self.clock_ms())])
        return self.view(plan_id)

    def change(
        self,
        plan_id: str,
        *,
        alternative: int | None = None,
        shorten: bool = False,
        order: Sequence[int] | None = None,
        kc_id: str | None = None,
    ) -> dict[str, Any]:
        """A change to this proposal only: swap the main block for a ranked alternative, choose a topic, reorder the
        blocks or shorten to the next activity. The decision names the alternative only when one was chosen; the
        changed plan is a new proposal. Nothing persists beyond this plan (no duration preference, D54)."""
        _, status, _, _ = self._row(plan_id)
        if status != "proposed":
            raise PlanRefused("already_decided")
        now = self.clock_ms()
        stored = self._stored(plan_id)
        extra: dict[str, Any] = {}
        if alternative is not None:
            options = self.alternatives(plan_id)
            if not 0 <= alternative < len(options):
                raise PlanRefused("no_such_alternative")
            changed = planner.swap_main(replace(stored, alternatives=options), alternative)
            extra["alternative_index"] = alternative
        elif kc_id is not None:
            if kc_id not in self.content.kcs:
                raise PlanRefused("unknown_kc")
            chosen = planner.main_block(self.state(now, kc_id), kc_id, "weak_high_value", self.settings)
            changed = planner.swap_main(replace(stored, alternatives=(chosen,)), 0)
        elif order is not None:
            try:
                changed = planner.reorder(stored, order)
            except ValueError:
                raise PlanRefused("invalid_order") from None
        elif shorten:
            changed = planner.shorten(stored)
            extra["new_minutes"] = max(1, changed.minutes)
        else:
            raise PlanRefused("invalid_change")
        new_id = self._generate(replace(changed, alternatives=()), now, self._decision(plan_id, "change", now, **extra))
        return self.view(new_id)

    def owns(self, request_id: str) -> bool:
        if not request_id.startswith(REQUEST_PREFIX):
            return False
        plan_id = request_id.removeprefix(REQUEST_PREFIX)
        return self.conn.execute("SELECT 1 FROM study_plans WHERE plan_id = ?", (plan_id,)).fetchone() is not None

    def answer_card(self, request_id: str, answer: Mapping[str, Any]) -> dict[str, Any]:
        """The plan card answered (API-14 semantics): accepted once; a decided plan answers `already_answered`."""
        plan_id = request_id.removeprefix(REQUEST_PREFIX)
        _, status, _, updated = self._row(plan_id)
        if status != "proposed":
            return {"status": "already_answered", "answered_ts": updated}
        now = self.clock_ms()
        if answer.get("skipped"):
            self.decide(plan_id, "not_today")
        elif answer.get("action") == "start":
            self.decide(plan_id, "start")
        elif answer.get("action") == "change":
            options = list(answer.get("option_ids") or [])
            choice = options[0] if options else ""
            if choice == "shorten":
                self.change(plan_id, shorten=True)
            elif choice.startswith("alt_") and choice[4:].isdigit():
                self.change(plan_id, alternative=int(choice[4:]))
            else:
                raise PlanRefused("invalid_change")
        else:
            raise PlanRefused("invalid_decision")
        return {"status": "accepted", "answered_ts": now}

    # --- the support level (LRN-27) ---------------------------------------------------------------------------------

    def refresh_support_level(self) -> policies.Decision:
        """Recompute `support_level` from the entry diagnostic and attendance; a change is written to the profile with
        a `profile_updated` event naming the field (values never go into events)."""
        p, s = self.params, self.settings
        now = self.clock_ms()
        profile = self.profile()
        weeks = model.active_days_by_week(self.conn, p, now, max(s.support_weeks * 2, 4))
        first = model.first_active_day(self.conn)
        known = 0 if first is None else max(0, (week_start(p.local_day(now)) - week_start(first)) // 7)
        weeks = weeks[-known:] if known else []
        recovered = 0
        for days in reversed(weeks):
            if days < s.support_min_days_per_week:
                break
            recovered += 1
        decision = policies.support_level(current=profile["support_level"], diagnostic_mean=self.diagnostic_mean(),
                                          active_days_by_week=weeks, recovered_weeks=recovered, s=s)  # fmt: skip
        self.decisions.append(decision)
        level = decision.output["support_level"]
        if level != profile["support_level"] and self.conn.execute("SELECT 1 FROM profile").fetchone():
            merge_profile(self.learner, {"support_level": level}, now, source="engine")
        return decision

    def diagnostic_mean(self) -> float | None:
        """The mean P_k of the KCs the entry diagnostic assessed (ASM-10), or None before any diagnostic."""
        kcs = {
            kc for (kc,) in self.conn.execute(
                "SELECT DISTINCT json_extract(payload, '$.kc_id') FROM events WHERE type = 'diagnostic_item'"
                " AND erased_at IS NULL")
        }  # fmt: skip
        if not kcs:
            return None
        views = model.kc_views(self.conn, self.params, self.clock_ms())
        values = [views[k].p_mastery for k in kcs if k in views]
        return sum(values) / len(values) if values else None

    # --- reminders (LRN-35) -----------------------------------------------------------------------------------------

    def next_reminder(self, local_now_minutes: int, sent_today: int, *, preferred_minute: int) -> int | None:
        """When today's reminder may be shown, in local minutes after midnight: never while reminders are off, never in
        the quiet hours, at most the daily number; None when none is due today."""
        enabled = self.conn.execute("SELECT value FROM settings WHERE key = 'notifications'").fetchone()
        on = bool(json.loads(enabled[0])) if enabled else self.settings.notifications
        return reminder_slot(on, local_now_minutes, sent_today, preferred_minute, self.settings)

    # --- progress (UXS-27, LRN-34) ----------------------------------------------------------------------------------

    def progress(self, subject: str | None = None) -> dict[str, Any]:
        """The Progress screen's data (ProgressView): per subject the KC states and bands by topic, the bands over
        time, the grade band once enough answers are checked (ASM-19, hidden below the threshold), and the time
        studied and streak. States and bands only, never a probability."""
        p, s = self.params, self.settings
        now = self.clock_ms()
        profile = self.profile()
        subjects = [name for name, _ in profile["subjects"] if subject in (None, name)]
        views = model.kc_views(self.conn, p, now)
        out = []
        for name in subjects:
            kcs = self._kcs([name], profile["level"])
            topics: dict[str, list[dict[str, Any]]] = {}
            counts = {"not_started": 0, "learning": 0, "secure": 0}
            for kc in kcs:
                state = views[kc].state if kc in views else "not_started"
                counts[BANDS[state]] += 1
                topic = self.content.topic_of(kc) or self.content.kcs[kc].node_id
                topics.setdefault(topic, []).append({"kc_id": kc, "state": state, "band": BANDS[state]})
            out.append({
                "subject": name,
                "bands": counts,
                "topics": [{"topic_id": t, "title": self.content.topic_title(t) or t,
                            "bands": {b: sum(k["band"] == b for k in rows) for b in counts}, "kcs": rows}
                           for t, rows in sorted(topics.items())],
                "history": self._history(kcs, now),
                "grade": self._grade(name, kcs, views, now),
            })  # fmt: skip
        week = local_date(p.local_day(now) - 6)
        minutes_7d, sessions_7d = self.conn.execute(
            "SELECT coalesce(sum(minutes_active), 0), coalesce(sum(sessions), 0) FROM engagement_daily WHERE day >= ?",
            (week,),
        ).fetchone()
        total = self.conn.execute("SELECT coalesce(sum(minutes_active), 0) FROM engagement_daily").fetchone()[0]
        weekly = s.support_weekly_freezes if profile["support_level"] == "extra" else s.streak_weekly_freezes
        streak = model.streak(self.conn, p, now, weekly)
        moved = sorted(k for k, v in views.items() if v.last_seen_at is not None
                       and p.local_day(v.last_seen_at) == p.local_day(now))  # fmt: skip
        return {
            "subjects": out,
            "time_studied": {
                "minutes_7d": int(minutes_7d),
                "sessions_7d": int(sessions_7d),
                "minutes_total": int(total),
            },
            "streak": {"days": streak.days, "freezes_left": streak.freezes_left, "counted_today": streak.counted_today},
            "moved_today": moved,
            # Study practice today, for "Check this idea": the way practice becomes mastery (activity, never mastery).
            "studied_today": [
                k
                for k in studied_kcs(self.conn, p.local_day(now) * DAY_MS - p.learner_day_offset_minutes * 60_000)
                if k in self.content.kcs and (k not in views or views[k].state != "mastered")
            ],
            # SPEC §19.1: the week's Study activity, one quiet line (activity, never mastery; JM-D26).
            "study_activity": study_activity_counts(
                self.conn, (p.local_day(now) - 6) * DAY_MS - p.learner_day_offset_minutes * 60_000
            ),
        }

    def _history(self, kcs: Sequence[str], now: int) -> list[dict[str, Any]]:
        """Weekly counts of KCs started (first direct observation) and secure (a passed mastery check) up to each week,
        for the last weeks: mastery by topic over time, from the attempts and check events."""
        p = self.params
        first: dict[str, int] = {}
        for kc, at in self.conn.execute(
            "SELECT json_extract(value, '$.kc_id'), min(occurred_at) FROM attempts, json_each(attempts.kc_ids)"
            " GROUP BY 1"
        ):
            first[kc] = at
        secure: dict[str, int] = {}
        for kc, at in self.conn.execute(
            "SELECT json_extract(payload, '$.kc_id'), min(occurred_at) FROM events WHERE type = 'mastery_check'"
            " AND erased_at IS NULL AND json_extract(payload, '$.check_state') = 'passed' GROUP BY 1"
        ):
            secure[kc] = at
        this_week = week_start(p.local_day(now))
        out = []
        for back in range(self.settings.progress_history_weeks - 1, -1, -1):
            start = this_week - 7 * back
            end_ms = (start + 7) * DAY_MS
            out.append({"week_start": local_date(start),
                        "learning": sum(1 for k in kcs if first.get(k, end_ms) < end_ms),
                        "secure": sum(1 for k in kcs if secure.get(k, end_ms) < end_ms)})  # fmt: skip
        return out

    def _grade(self, subject: str, kcs: Sequence[str], views: Mapping[str, model.KcView], now: int) -> dict[str, Any]:
        """The predicted grade band (ASM-19) only after the configured checked answers in the subject; before that,
        how many so far (UXS-27). A subject whose pack sets no board grade letters shows no band at all (`no_scale`,
        D27; adopted in D101)."""
        code = SUBJECT_CODES[subject]
        scale = pack_scale(self.content.table("mock_papers"), code)
        checked = sum(
            1
            for (kcs_json, outcome) in self.conn.execute("SELECT kc_ids, outcome FROM attempts")
            if outcome != "unverifiable" and any(k["kc_id"].split(".")[1] == code for k in json.loads(kcs_json))
        )
        needed = self.settings.grade_min_checked
        if scale is None:
            return {"status": "no_scale", "checked": checked, "needed": needed}
        if checked < needed:
            return {"status": "hidden", "checked": checked, "needed": needed}
        weights = {k: self.content.exam_weight(k) or 1.0 for k in kcs}
        mastery = {k: (min(max(views[k].p_mastery, 1e-6), 1 - 1e-6), views[k].n_eff) for k in kcs if k in views}
        covered = sum(w for k, w in weights.items() if k in mastery) / (sum(weights.values()) or 1)
        evidenced = {k: w for k, w in weights.items() if k in mastery}
        band = predict_band(mastery, evidenced, checked, covered, [], self.settings, seed=self.params.local_day(now),
                            boundaries=scale.get("boundaries"), labels=scale.get("labels"))  # fmt: skip
        if band is None:
            return {"status": "uncertain", "checked": checked, "needed": needed}
        return {"status": "shown", "checked": checked, "needed": needed, "band": band["band"],
                "range": band["range"], "confidence": band["confidence"]}  # fmt: skip


def reminder_slot(
    enabled: bool, local_now_minutes: int, sent_today: int, preferred_minute: int, s: Settings
) -> int | None:
    """LRN-35: the minute today a reminder may be shown, or None. Quiet hours run from the first configured hour to the
    second (overnight), the daily number is fixed, and nothing is scheduled while the preference is off."""
    if not enabled or sent_today >= s.notify_max_per_day:
        return None
    start, end = (h * 60 for h in s.notify_quiet_hours)
    minute = max(preferred_minute, local_now_minutes)

    def quiet(m: int) -> bool:
        return m >= start or m < end if start > end else start <= m < end

    if quiet(minute):
        if minute < end:
            minute = end
        else:
            return None  # the evening's quiet hours run into tomorrow
    return minute if minute < 24 * 60 and not quiet(minute) else None
