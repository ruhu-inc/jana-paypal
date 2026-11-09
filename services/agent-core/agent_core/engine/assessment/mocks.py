"""Persisted active-time mock sittings and reports (ASM-15 to ASM-20, ASM-24)."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, get_args

from agent_core.contracts.api.commands import Settings as WireSettings
from agent_core.engine.assessment.bands import predict_band
from agent_core.engine.assessment.criteria import decide_method, drop_restatements, score_criteria
from agent_core.engine.assessment.errors import Refused
from agent_core.engine.assessment.items import pin_item, present, public_answer, ready, validate_answer
from agent_core.engine.marking import Verdict
from agent_core.store.assessment import dump
from agent_core.store.sqlite import transaction

LOCKED = ("in_progress", "paused", "interrupted")
DAY_MS = 86_400_000


def _iso(ms: int) -> str:
    from agent_core.engine.assessment.service import iso

    return iso(ms)


def _millis(value: str) -> int:
    from agent_core.engine.assessment.service import millis

    return millis(value)


def show_that_target(part: Mapping[str, Any]) -> str | None:
    """The given result of a "show that" part (ASM-25): its expression, its value or its one solution's value (a line's
    last side is compared with it)."""
    answer = part.get("answer") or {}
    if answer.get("kind") == "algebraic":
        return str(answer["expression"])
    if answer.get("kind") == "numeric":
        return str(answer["value"])
    if answer.get("kind") == "equation_solutions" and len(answer.get("values") or []) == 1:
        return str(answer["values"][0])
    return None


class MockCommands:
    def _exam_row(self: Any, identity: str) -> Any:
        row = self.learner.conn.execute(
            "SELECT status,duration_ms,extra_time_percent,elapsed_ms,pauses,interruptions,"
            " started_at,closes_at,submitted_at FROM exams WHERE exam_id=?",
            (identity,),
        ).fetchone()
        if row is None:
            raise Refused("not_found")
        return row

    def _extra_time(self: Any) -> int:
        row = self.learner.conn.execute("SELECT value FROM settings WHERE key='extra_time_percent'").fetchone()
        value = json.loads(row[0]) if row else self.settings.extra_time_percent
        if isinstance(value, bool) or value not in get_args(WireSettings.model_fields["extra_time_percent"].annotation):
            raise Refused("invalid_extra_time")
        return int(value)

    def create_mock(self: Any, paper_id: str) -> dict[str, Any]:
        if paper_id.startswith("ppr_"):  # an imported past paper (ASM-27)
            return dict(self.create_past_paper_sitting(paper_id))
        self.guard_tutor()
        paper = self.content.record("mock_papers", paper_id)
        if paper is None:
            raise Refused("not_found")
        subject = {"mth": "mathematics", "phy": "physics", "chm": "chemistry", "bio": "biology", "acc": "accounting"}[
            paper["subject"]
        ]
        state = self._new(
            "mock",
            subject,
            paper=copy.deepcopy(paper),
            items={},
            report=None,
            signals=[],
            section_ms={},
            pause_started=None,
            active_section=paper["sections"][0]["section_id"],
        )
        for section in paper["sections"]:
            choose = section.get("choose")
            marks = [q["marks"] for q in section["questions"]]
            if choose is not None and (choose < 1 or choose > len(marks) or len(set(marks)) != 1):
                raise Refused("blueprint_choice_invalid")
            expected = sum(marks) if choose is None else choose * marks[0]
            if expected != section["marks"]:
                raise Refused("blueprint_marks_mismatch")
            for question in section["questions"]:
                item = self.content.record("items", question["item_id"])
                if item is None or item["id"] not in self._reserved() or item["type"] not in section["item_types"]:
                    raise Refused("invalid_paper_item")
                if not ready(item, self.content, self.settings):
                    raise Refused("paper_item_unavailable")
                if sum(p["marks"] for p in item["parts"]) != question["marks"]:
                    raise Refused("blueprint_marks_mismatch")
                state["items"][question["question_id"]] = pin_item(item, self.content)
        if paper["total_marks"] != sum(s["marks"] for s in paper["sections"]):
            raise Refused("blueprint_marks_mismatch")
        weights = paper["blueprint"]["topic_weights"]
        if abs(sum(weights.values()) - 1) > self.settings.mock_blueprint_weight_tolerance:
            raise Refused("blueprint_weights_invalid")
        topic_marks: dict[str, float] = {}
        largest = 0
        for section in paper["sections"]:
            for question in section["questions"]:
                topics = {self._topic_of(kc) for kc in question["kc_ids"]}
                largest = max(largest, question["marks"])
                for topic in topics:
                    topic_marks[topic] = topic_marks.get(topic, 0) + question["marks"] / len(topics)
        if any(abs(topic_marks.get(t, 0) - weight * paper["total_marks"]) > largest for t, weight in weights.items()):
            raise Refused("blueprint_topic_mismatch")
        if set(topic_marks) - set(weights):
            raise Refused("blueprint_topic_mismatch")
        duration = int(paper["duration_minutes"] * 60_000)
        extra = self._extra_time()

        def insert() -> None:
            self.learner.conn.execute(
                "INSERT INTO exams (exam_id,paper_ref,status,duration_ms,extra_time_percent) VALUES (?,?,'ready',?,?)",
                (state["id"], paper_id, duration, extra),
            )

        state["state"] = "ready"
        self._commit(state, extra=insert)
        return dict(self.mock("exm_" + state["id"]))

    def _tick(self: Any, state: dict[str, Any]) -> Any:
        row = self._exam_row(state["id"])
        status, duration, extra, elapsed = row[:4]
        allowed = duration * (100 + extra) // 100
        now = self.clock_ms()
        events = []
        pauses = json.loads(row[4])
        if status == "in_progress":
            mono = self.monotonic_ms()
            last = self.running.setdefault(state["id"], mono)
            spent = max(0, mono - last)
            section = state.get("active_section")
            if section:
                state["section_ms"][section] = state["section_ms"].get(section, 0) + min(
                    spent, max(0, allowed - elapsed)
                )
            elapsed = min(allowed, elapsed + spent)
            self.running[state["id"]] = max(last, mono)
            status = "submitted" if elapsed >= allowed else status
        elif status == "paused" and state.get("pause_started") is not None:
            pauses = json.loads(row[4])
            previous = sum(p["length_ms"] for p in pauses[:-1])
            budget = max(0, self.settings.mock_pause_minutes * 60_000 - previous)
            mono = self.monotonic_ms()
            last = self.paused.setdefault(state["id"], mono)
            paused = pauses[-1]["length_ms"] + max(0, mono - last)
            self.paused[state["id"]] = max(last, mono)
            pauses[-1]["length_ms"] = min(paused, budget)
            if paused >= budget:
                elapsed = min(allowed, elapsed + max(0, paused - budget))
                status = "submitted" if elapsed >= allowed else "in_progress"
                self.running[state["id"]] = self.monotonic_ms()
                state["pause_started"] = None
                self.paused.pop(state["id"], None)
        elif status == "interrupted":
            interruptions = json.loads(row[5])
            if interruptions and now - _millis(interruptions[-1]["at"]) >= self.settings.mock_abandon_days * DAY_MS:
                status = "abandoned"
        if status in ("submitted", "abandoned") and row[0] not in ("submitted", "abandoned"):
            events = [self._submission_event(state, "time_up" if status == "submitted" else "abandoned", elapsed)]
            self.running.pop(state["id"], None)
            self.paused.pop(state["id"], None)
        state["state"] = status

        def save() -> None:
            self.learner.conn.execute(
                "UPDATE exams SET status=?,elapsed_ms=?,closes_at=?,submitted_at=coalesce(submitted_at,?),pauses=?"
                " WHERE exam_id=?",
                (status, elapsed, now, now if status == "submitted" else None, dump(pauses), state["id"]),
            )

        self._commit(state, events, save)
        return self._exam_row(state["id"])

    def mock(self: Any, identity: str) -> dict[str, Any]:
        state = self._get(identity, "mock")
        row = self._tick(state)
        status, base, extra, elapsed = row[:4]
        duration = base * (100 + extra) // 100
        remaining = max(0, duration - elapsed)
        answers = {
            qid: {
                "answer": public_answer(state["items"][qid], json.loads(answer)) if answer is not None else None,
                "flagged": bool(flagged),
            }
            for qid, answer, flagged in self.learner.conn.execute(
                "SELECT item_ref,answer,flagged FROM exam_answers WHERE exam_id=?", (state["id"],)
            )
        }
        return {
            "exam_id": "exm_" + state["id"],
            "paper_id": state["paper"]["id"],
            "state": status,
            "questions": [
                present(state["items"][qid], self.content, self.settings) for qid in self._question_order(state)
            ],
            "answers": answers,
            "duration_ms": duration,
            "remaining_ms": remaining,
            "pauses": json.loads(row[4]),
            "interruptions": json.loads(row[5]),
            "running_deadline_ts": self.clock_ms() + remaining if status == "in_progress" else None,
            "extra_time_percent": extra,
            "calculator": bool(state["paper"].get("calculator", False)),
            "question_ids": self._question_order(state),
        }

    def list_mocks(self: Any) -> dict[str, Any]:
        """The papers the installed packs hold and this profile's sittings, newest first (UXS-25, `MockList`,
        D93): the Mock exam place offers a paper, a sitting left open is found again after a restart, and an abandoned
        one is shown on Home. An open sitting is brought up to date first, as reading it does (its clock, its time-up
        or its abandonment)."""
        names = {"mth": "mathematics", "phy": "physics", "chm": "chemistry", "bio": "biology", "acc": "accounting"}
        papers = [
            {
                "paper_id": paper["id"],
                "subject": names[paper["subject"]],
                "level": str(paper["level"]).upper(),
                "title": paper["title"],
                "paper_kind": paper["paper_kind"],
                "duration_minutes": paper["duration_minutes"],
                "total_marks": paper["total_marks"],
                "questions": sum(len(s["questions"]) for s in paper["sections"]),
                "calculator": bool(paper.get("calculator", False)),
            }
            for paper in sorted(self.content.table("mock_papers"), key=lambda p: str(p["id"]))
            if all(
                (item := self.content.record("items", q["item_id"])) is not None
                and ready(item, self.content, self.settings)
                for section in paper["sections"]
                for q in section["questions"]
            )
        ]
        sittings = []
        for state in sorted(
            (s for s in self.store.all() if s["kind"] == "mock"), key=lambda s: (s["started"], s["id"]), reverse=True
        ):
            row = self._exam_row(state["id"])
            if row[0] in LOCKED:
                row = self._tick(state)
            status, base, extra, elapsed = row[:4]
            report = self._get(state["id"], "mock").get("report")
            sittings.append(
                {
                    "exam_id": "exm_" + state["id"],
                    "paper_id": state["paper"]["id"],
                    "state": status,
                    "remaining_ms": max(0, base * (100 + extra) // 100 - elapsed),
                    "score": report["score"] if report else None,
                    "max_score": report["max_score"] if report else None,
                }
            )
        return {"papers": papers, "sittings": sittings[: self.settings.mock_list_sittings_max]}

    def transition(self: Any, identity: str, action: str) -> dict[str, Any]:
        self.mock(identity)
        state = self._get(identity, "mock")
        row = self._exam_row(state["id"])
        status = row[0]
        extra = row[2]
        now = self.clock_ms()
        pauses, interruptions = json.loads(row[4]), json.loads(row[5])
        if action == "start" and status == "ready":
            self.guard_tutor()
            if self.busy:
                raise Refused("request_conflict")
            if self.mock_active() or any(
                s["kind"] == "diagnostic" and s["state"] == "in_progress" for s in self.store.all()
            ):
                raise Refused("tutor_locked")
            extra = self._extra_time()
            status = "in_progress"
            # An imported paper's questions are not pack items: no exposure, as no mastery (ASM-27).
            events = (
                []
                if state["paper"].get("imported")
                else [self._exposure(i, "mock", state["id"]) for i in state["items"].values()]
            )
        elif action == "pause" and status == "in_progress":
            if (
                len(pauses) >= self.settings.mock_pause_count
                or sum(p["length_ms"] for p in pauses) >= self.settings.mock_pause_minutes * 60_000
            ):
                raise Refused("pause_budget_exhausted")
            status = "paused"
            pauses.append({"at": _iso(now), "length_ms": 0})
            state["pause_started"] = now
            events = []
        elif action == "resume" and status in ("paused", "interrupted"):
            # mock() above settled the paused interval against its monotonic anchor.
            status, state["pause_started"] = "in_progress", None
            events = []
        elif action == "interrupt" and status in ("in_progress", "paused"):
            status = "interrupted"
            state["pause_started"] = None
            interruptions.append({"at": _iso(now), "remaining_ms": row[1] * (100 + row[2]) // 100 - row[3]})
            events = []
        else:
            raise Refused("invalid_transition")
        state["state"] = status

        def save() -> None:
            self.learner.conn.execute(
                "UPDATE exams SET status=?,started_at=coalesce(started_at,?),pauses=?,interruptions=?,"
                " closes_at=?,extra_time_percent=? WHERE exam_id=?",
                (status, now, dump(pauses), dump(interruptions), now, extra, state["id"]),
            )

        self._commit(state, events, save)
        if status == "in_progress":
            self.running[state["id"]] = self.monotonic_ms()
            self.paused.pop(state["id"], None)
        elif status == "paused":
            self.running.pop(state["id"], None)
            self.paused.setdefault(state["id"], self.monotonic_ms())
        else:
            self.running.pop(state["id"], None)
            self.paused.pop(state["id"], None)
        return dict(self.mock(identity))

    def recover(self: Any) -> None:
        for state in self.store.all():
            if state["kind"] == "mock" and self._exam_row(state["id"])[0] in ("in_progress", "paused"):
                row = self._exam_row(state["id"])
                pauses = json.loads(row[4])
                # Pause lengths were already checkpointed using monotonic time. Wall timestamps are audit data,
                # not elapsed time; neither a clock jump nor a new boot may change that durable allowance.
                self.running.pop(state["id"], None)
                self.paused.pop(state["id"], None)
                interruptions = json.loads(row[5])
                interruptions.append(
                    {"at": _iso(row[7] or self.clock_ms()), "remaining_ms": row[1] * (100 + row[2]) // 100 - row[3]}
                )
                state.update(state="interrupted", pause_started=None)

                def save(s: Any = state, p: Any = pauses, i: Any = interruptions) -> None:
                    self.learner.conn.execute(
                        "UPDATE exams SET status='interrupted',pauses=?,interruptions=? WHERE exam_id=?",
                        (dump(p), dump(i), s["id"]),
                    )

                self._commit(state, extra=save)
            elif state["kind"] == "diagnostic" and state["state"] == "in_progress":
                state.update(state="stopped", last_active=self.clock_ms())
                self._commit(state)

    def checkpoint(self: Any) -> None:
        self.schedule_ready_checks()
        for assessment in self.store.all():
            if assessment["kind"] == "diagnostic" and assessment["state"] == "in_progress":
                self.assessment(assessment["id"])
        for state in self.store.all():
            if state["kind"] == "mock" and self._exam_row(state["id"])[0] in ("in_progress", "paused", "interrupted"):
                self.mock(state["id"])

    def interrupt_all(self: Any) -> None:
        for state in self.store.all():
            if state["kind"] == "mock" and self._exam_row(state["id"])[0] in ("in_progress", "paused"):
                self.transition(state["id"], "interrupt")

    def save_mock_answer(self: Any, identity: str, question_id: str, answer: Any, flagged: bool) -> dict[str, Any]:
        state = self._get(identity, "mock")
        if self.mock(identity)["state"] != "in_progress":
            raise Refused("answers_locked")
        item = state["items"].get(question_id)
        if item is None or not isinstance(flagged, bool):
            raise Refused("invalid_question")
        if answer is not None:
            section = next(
                section
                for section in state["paper"]["sections"]
                if any(q["question_id"] == question_id for q in section["questions"])
            )
            if section.get("choose") is not None:
                ids = {q["question_id"] for q in section["questions"] if q["question_id"] != question_id}
                answered = {
                    q
                    for (q,) in self.learner.conn.execute(
                        "SELECT item_ref FROM exam_answers WHERE exam_id=? AND answer IS NOT NULL", (state["id"],)
                    )
                }
                if len(ids & answered) >= section["choose"]:
                    raise Refused("section_choice_limit")
            try:
                answer = validate_answer(item, answer)
            except ValueError as error:
                raise Refused(str(error)) from None
        with transaction(self.learner.conn):
            self.learner.conn.execute(
                "INSERT INTO exam_answers (exam_id,item_ref,answer,flagged,updated_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(exam_id,item_ref) DO UPDATE SET answer=excluded.answer,flagged=excluded.flagged,"
                " updated_at=excluded.updated_at",
                (state["id"], question_id, dump(answer) if answer is not None else None, int(flagged), self.clock_ms()),
            )
        return {"saved_ts": self.clock_ms()}

    def _submission_event(self: Any, state: Mapping[str, Any], reason: str, elapsed: int) -> Any:
        rows = self.learner.conn.execute(
            "SELECT answer,flagged FROM exam_answers WHERE exam_id=?", (state["id"],)
        ).fetchall()
        exam = self._exam_row(state["id"])
        return self._event(
            "mock_submitted",
            {
                "sitting_id": state["id"],
                "paper_id": state["paper"]["id"],
                "reason": reason,
                "answered": sum(a is not None for a, _ in rows),
                "flagged": sum(f for _, f in rows),
                "active_ms": elapsed,
                "interruptions": len(json.loads(exam[5])),
                "pauses": len(json.loads(exam[4])),
            },
            state["id"],
        )

    def integrity(self: Any, identity: str, kind: str, amount: int) -> bool:
        state = self._get(identity, "mock")
        if self.mock(identity)["state"] not in LOCKED:
            raise Refused("sitting_not_open")
        state = self._get(identity, "mock")
        if kind not in ("focus_loss", "large_paste") or isinstance(amount, bool) or amount < 0:
            raise Refused("invalid_integrity_signal")
        threshold = self.settings.mock_focus_loss_ms if kind == "focus_loss" else self.settings.mock_large_paste_chars
        if amount <= threshold:
            return False
        payload = {
            "kind": kind,
            "sitting_id": state["id"],
            "duration_ms" if kind == "focus_loss" else "size_chars": amount,
        }
        # Counts only in reports; never store pasted text or a surveillance feed.
        state["signals"].append(kind)
        self._commit(state, [self._event("integrity_signal", payload, state["id"])])
        return True

    async def _method_equivalent(
        self: Any, left: str, right: str, variables: Sequence[str] = ()
    ) -> tuple[bool | None, str | None]:
        if self.run_tool is None:
            return None, None
        args: dict[str, Any] = {"a": left, "b": right}
        if variables:
            args["variables"] = list(variables)
        value, check_id = await self.run_tool("math_check_equivalence", args)
        verdict = (value or {}).get("verdict")
        result = (
            True
            if verdict in ("equivalent", "equivalent_on_domain")
            else False
            if verdict == "not_equivalent"
            else None
        )
        return result, check_id

    async def _same_written_form(self: Any, left: str, right: str, variables: Sequence[str]) -> bool | None:
        """Whether two expressions are written the same, up to term order (ASM-25): the worker's as-written parse of
        each, never their values; None when either cannot be read."""
        if self.run_tool is None:
            return None
        forms: list[str] = []
        for text in (left, right):
            args: dict[str, Any] = {"text": text}
            if variables:
                args["variables"] = list(variables)
            value, _ = await self.run_tool("math_parse", args)
            written = (value or {}).get("as_written_latex")
            if not written:
                return None
            forms.append(str(written))
        return forms[0] == forms[1]

    async def _score_question(
        self: Any, question: Mapping[str, Any], item: Mapping[str, Any], answer: Any
    ) -> tuple[int, int, list[dict[str, Any]], dict[str, Any]]:
        verdicts = await self._mark(item, answer)
        results = []
        for part in item["parts"]:
            scheme_part: dict[str, Any] = next(
                (p for p in question.get("parts") or [] if p["part_id"] == part["id"]), {}
            )
            criteria = scheme_part.get("criteria") or question.get("criteria") or []
            if not criteria:
                previous_method = None
                for point in part.get("mark_points") or []:
                    needs = (
                        [point["depends_on"]]
                        if point.get("depends_on")
                        else [previous_method]
                        if point["type"] == "A" and previous_method
                        else []
                    )
                    criteria.append(
                        {
                            "criterion_id": point["id"],
                            "mark_type": point["type"],
                            "marks": point["marks"],
                            "pass_if": point["criterion"],
                            "requires": needs,
                            "judge_allowed": bool(point.get("judge_allowed")),
                            "check": {
                                "check_id": point.get("check_id"),
                                "method_lines": list(point.get("method_lines") or [])
                                + list(point.get("alternatives") or []),
                            },
                        }
                    )
                    if point["type"] == "M":
                        previous_method = point["id"]
            verdict = verdicts[part["id"]]
            if not criteria:
                results.append(
                    {
                        "criterion_id": part["id"],
                        "mark_type": "B",
                        "awarded": int(verdict.marks_awarded),
                        "available": 0 if verdict.outcome == "unverifiable" else int(verdict.marks_available),
                        "resolved": verdict.outcome != "unverifiable",
                        "scheme": part["prompt"],
                        "notice_key": "assessment.not_marked_automatically"
                        if verdict.outcome == "unverifiable"
                        else None,
                    }
                )
                continue
            decisions: dict[str, bool | None] = {}
            for criterion in criteria:
                cid = criterion["criterion_id"]
                carried = next(
                    (
                        c
                        for c in verdict.criteria
                        if c["criterion_id"] == cid and c.get("follow_through") and c["method"] == "tool"
                    ),
                    None,
                )
                if answer is None:
                    decisions[cid] = False
                elif carried is not None:
                    decisions[cid] = {"pass": True, "fail": False}.get(carried["result"])
                elif criterion["mark_type"] != "M":
                    matched = next((c for c in verdict.criteria if c["criterion_id"] == cid), None)
                    if matched is not None:
                        decisions[cid] = (
                            {"pass": True, "fail": False}.get(matched["result"])
                            if matched["method"] in ("key", "tool")
                            else None
                        )
                    else:
                        decisions[cid] = None if verdict.outcome == "unverifiable" else verdict.outcome == "correct"
                else:
                    check = criterion.get("check") or {}
                    methods = check.get("method_lines") or []
                    lines = [line["latex"] for line in answer.get("working", []) if line["part_id"] == part["id"]]
                    lines += [
                        a.get("latex", a.get("text", ""))
                        for a in answer["parts"]
                        if a["part_id"] == part["id"] and a["kind"] in ("algebraic", "short_text")
                    ]
                    if not methods:
                        methods = [
                            step["latex"]
                            for step in (item.get("solution") or {}).get("steps", [])
                            if step.get("check_id") and step["check_id"] == check.get("check_id")
                        ]
                    variables = set(part["answer"].get("variables") or []) | set(item.get("quantities") or {})
                    if part["answer"].get("variable"):
                        variables.add(part["answer"]["variable"])

                    async def equivalent(
                        left: str, right: str, scope: tuple[str, ...] = tuple(sorted(variables))
                    ) -> tuple[bool | None, str | None]:
                        result: tuple[bool | None, str | None] = await self._method_equivalent(left, right, scope)
                        return result

                    target = show_that_target(part) if scheme_part.get("command_word") == "show_that" else None
                    if target is not None:

                        async def same_form(left: str, right: str, scope: tuple[str, ...] = tuple(sorted(variables))
                                            ) -> bool | None:  # fmt: skip
                            result: bool | None = await self._same_written_form(left, right, scope)
                            return result

                        lines = await drop_restatements(lines, target, same_form)
                    judge = getattr(self, "local_method_judge", None)
                    if self.judge_method is not None:

                        async def criterion_judge(
                            working: Sequence[str],
                            alternatives: Sequence[str],
                            rule: Mapping[str, Any] = {**criterion, "problem": item["stem"], "prompt": part["prompt"]},
                        ) -> bool | None:
                            decision: bool | None = await self.judge_method(rule, working)
                            return decision

                        judge = criterion_judge
                    decisions[cid] = await decide_method(
                        lines,
                        methods,
                        equivalent,
                        judge_allowed=bool(criterion.get("judge_allowed")),
                        judge=judge,
                    )
            part_results = score_criteria(criteria, decisions)
            results.extend(part_results)
            own_score = sum(r["awarded"] for r in part_results)
            own_available = sum(r["available"] for r in part_results)
            verdicts[part["id"]] = replace(
                verdict,
                marks_awarded=own_score,
                marks_available=own_available,
                outcome="unverifiable"
                if own_available == 0
                else "correct"
                if own_score == own_available
                else "partial"
                if own_score
                else "incorrect",
            )
        return sum(r["awarded"] for r in results), sum(r["available"] for r in results), results, verdicts

    def _refresh_mock_plan(self: Any) -> None:
        """Publish a fresh Home proposal after marking, even when the optional practice set stays unopened.

        The stored report is the durable source. Repeat submissions/recovery reuse an already-matching proposal.
        Earlier plans remain in history; no completed work or student decision is overwritten.
        """
        from agent_core.engine.plans import PlanDesk
        from agent_core.learner import planner

        if self.learner.conn.execute("SELECT 1 FROM profile").fetchone() is None:
            return
        desk = PlanDesk(self.learner, self.content, self.settings, clock_ms=self.clock_ms)
        state = desk.state(self.clock_ms())
        weak = planner.follow_up(state)
        current = desk.current()
        old = next((b for b in (current or {}).get("blocks", []) if b["kind"] == "main"), None)
        if weak:
            if old and old.get("reason_code") == "mock_follow_up" and tuple(old["kc_ids"]) == weak:
                return
            desk.propose()
        elif old and old.get("reason_code") == "mock_follow_up":
            desk.propose()

    async def submit_mock(self: Any, identity: str) -> dict[str, Any]:
        state = self._get(identity, "mock")
        view = self.mock(identity)
        state = self._get(identity, "mock")
        if view["state"] in LOCKED:
            state["state"] = "submitted"

            def submit() -> None:
                self.learner.conn.execute(
                    "UPDATE exams SET status='submitted',submitted_at=? WHERE exam_id=?", (self.clock_ms(), state["id"])
                )

            self._commit(state, [self._submission_event(state, "student", self._exam_row(state["id"])[3])], submit)
            self.running.pop(state["id"], None)
        elif view["state"] not in ("submitted", "marked", "reviewed"):
            raise Refused("invalid_transition")
        state = self._get(identity, "mock")
        if state["report"] is not None:
            self._refresh_mock_plan()
            return dict(state["report"])
        if state["id"] in self.busy:
            raise Refused("request_conflict")
        self.busy.add(state["id"])
        try:
            topics: dict[str, dict[str, Any]] = {}
            review, criterion_rows, events, transcripts = [], [], [], []
            total = maximum = 0
            selected = self._selected_questions(state, view)
            mark_rows = []
            imported = bool(state["paper"].get("imported"))
            past_marking: list[dict[str, Any]] = []
            for section in state["paper"]["sections"]:
                for q in section["questions"]:
                    qid, item = q["question_id"], state["items"][q["question_id"]]
                    if qid not in selected:
                        continue
                    answer = view["answers"].get(qid, {}).get("answer")
                    if imported:  # marked honestly, and never mastery evidence (ASM-27)
                        score, available, criteria, marking = await self._score_imported(state, qid, answer)
                        past_marking.extend(marking)
                        verdicts = {}
                    else:
                        score, available, criteria, verdicts = await self._score_question(q, item, answer)
                    mark_rows.append((score, available, state["id"], qid))
                    total += score
                    maximum += available
                    question_topics = sorted({self._topic_of(kc) for kc in item["kc_ids"]})
                    for index, topic in enumerate(question_topics):
                        bucket = topics.setdefault(topic, {"topic_id": topic, "score": 0, "max_score": 0})
                        bucket["score"] += score // len(question_topics) + (index < score % len(question_topics))
                        bucket["max_score"] += available // len(question_topics) + (
                            index < available % len(question_topics)
                        )
                    review.append(
                        {
                            "question_id": qid,
                            "correct": score == q["marks"] and available == q["marks"],
                            "show_me_why_ref": state["id"] + ":" + qid,
                        }
                    )
                    criterion_rows.append({"question_id": qid, "criteria": criteria})
                    if answer is not None and not imported:
                        outcome = (
                            "unverifiable"
                            if available == 0
                            else "correct"
                            if score == available
                            else "partial"
                            if score
                            else "incorrect"
                        )
                        scored = Verdict(
                            outcome,
                            score,
                            available,
                            "mock-criteria-v1",
                            tuple(cid for v in verdicts.values() for cid in v.check_ids),
                        )
                        _, new_events, transcript = self._attempt_events(
                            state,
                            item,
                            verdicts
                            if item["type"] == "structured" and all(p.get("kc_ids") for p in item["parts"])
                            else {item["parts"][0]["id"]: scored},
                            answer,
                            0,
                        )
                        events.extend(new_events)
                        transcripts.append(transcript)
            report = {
                "exam_id": "exm_" + state["id"],
                "score": total,
                "max_score": maximum,
                "by_topic": list(topics.values()),
                "review_items": review,
                "criteria": criterion_rows,
                "flagged": [q for q, a in view["answers"].items() if a["flagged"]],
                "unanswered": [
                    q
                    for q in view["question_ids"]
                    if q in selected and view["answers"].get(q, {}).get("answer") is None
                ],
                "optional_omitted": [q for q in view["question_ids"] if q not in selected],
                "integrity_counts": {k: state["signals"].count(k) for k in ("focus_loss", "large_paste")},
                "extra_time_percent": view["extra_time_percent"],
                "taken_with_extra_time": bool(view["extra_time_percent"]),
                "pauses": view["pauses"],
                "interruptions": view["interruptions"],
                "time_by_section": state["section_ms"],
                "marks_lost": {
                    kind: sum(
                        c["available"] - c["awarded"]
                        for row in criterion_rows
                        for c in row["criteria"]
                        if c["mark_type"] == kind
                    )
                    for kind in ("M", "A", "B")
                },
                "practice_kc_ids": [],
                "practice_set_ref": None,
                "grade_band": None,
            }
            if imported:
                report["past_paper"] = {"paper_id": state["paper"]["id"], "mastery_changed": False,
                                        "marking": past_marking[:1000]}  # fmt: skip
            state.update(state="marked", report=report)

            def save() -> None:
                for transcript in transcripts:
                    transcript()
                self.learner.conn.executemany(
                    "UPDATE exam_answers SET marks_awarded=?,marks_available=? WHERE exam_id=? AND item_ref=?",
                    mark_rows,
                )
                self.learner.conn.execute(
                    "UPDATE exams SET status='marked',score_by_topic=? WHERE exam_id=?",
                    (dump(report["by_topic"]), state["id"]),
                )
                report["practice_kc_ids"] = self._weakest_kcs(state)
                if state["paper"].get("grade_config"):  # null: not enough evidence yet for a band
                    report["grade_band"] = self.grade_band(state)
                else:  # the paper carries no board letters: no band at all, not even "not enough evidence" (D27)
                    report.pop("grade_band", None)
                report["practice_set_ref"] = self.prepare_review_practice(state["subject"], report["practice_kc_ids"])

            self._commit(state, events, save)
            self._refresh_mock_plan()
            return report
        finally:
            self.busy.discard(state["id"])

    def _weakest_kcs(self: Any, state: Mapping[str, Any]) -> list[str]:
        """The mock's weak KCs (ASM-20, UXS-26, LRN-28; D102): only the KCs of questions that lost marks, the least
        mastered first, at most `mock_weakest_kcs`. A question answered for full marks adds none; a paper with no
        mark lost has no follow-up."""
        missed = {q["question_id"] for q in state["report"]["review_items"] if not q["correct"]}
        tagged = {q["question_id"]: q.get("kc_ids") for s in state["paper"]["sections"] for q in s["questions"]}
        kcs = {k for qid, i in state["items"].items() if qid in missed for k in (tagged.get(qid) or i["kc_ids"])}
        probabilities = {k: p for k, p in self.learner.conn.execute("SELECT kc_id,p_mastery FROM kc_mastery")}
        return sorted(kcs, key=lambda k: (probabilities.get(k, 0), k))[: self.settings.mock_weakest_kcs]

    def review(self: Any, reference: str) -> dict[str, Any]:
        self.guard_tutor()
        identity, separator, question = reference.partition(":")
        if not separator:
            raise Refused("invalid_review_reference")
        state = self._get(identity, "mock")
        if self._exam_row(identity)[0] not in ("marked", "reviewed") or question not in state["items"]:
            raise Refused("review_not_available")
        row = self.learner.conn.execute(
            "SELECT answer FROM exam_answers WHERE exam_id=? AND item_ref=?", (identity, question)
        ).fetchone()
        state["state"] = "reviewed"

        def save() -> None:
            self.learner.conn.execute("UPDATE exams SET status='reviewed' WHERE exam_id=?", (identity,))

        self._commit(state, extra=save)
        return {
            "item": copy.deepcopy(state["items"][question]),
            "answer": json.loads(row[0]) if row and row[0] else None,
            "mode": "guided",
            "origin": {"kind": "exam_review", "ref": reference},
            "subject": state["subject"],
        }

    def navigate(self: Any, identity: str, question_id: str) -> dict[str, Any]:
        self.mock(identity)
        state = self._get(identity, "mock")
        if self._exam_row(state["id"])[0] != "in_progress" or question_id not in state["items"]:
            raise Refused("invalid_navigation")
        state["active_section"] = next(
            s["section_id"]
            for s in state["paper"]["sections"]
            if any(q["question_id"] == question_id for q in s["questions"])
        )
        self._commit(state)
        return {"saved_ts": self.clock_ms()}

    def grade_band(self: Any, state: Mapping[str, Any]) -> dict[str, Any] | None:
        mastery = {k: (p, n) for k, p, n in self.learner.conn.execute("SELECT kc_id,p_mastery,n_eff FROM kc_mastery")}
        weights: dict[str, float] = {}
        covered = 0
        total = state["paper"]["total_marks"]
        for section in state["paper"]["sections"]:
            for question in section["questions"]:
                for kc in question["kc_ids"]:
                    weights[kc] = weights.get(kc, 0) + question["marks"] / len(question["kc_ids"])
                if all(mastery.get(kc, (0, 0))[1] > 0 for kc in question["kc_ids"]):
                    covered += question["marks"]
        checked = 0
        for item_id, outcome in self.learner.conn.execute("SELECT item_ref,outcome FROM attempts"):
            if item_id and item_id.split(".")[1] == state["paper"]["subject"] and outcome != "unverifiable":
                checked += 1
        recent = []
        for status, submitted, topics in self.learner.conn.execute(
            "SELECT status,submitted_at,score_by_topic FROM exams WHERE paper_ref=?", (state["paper"]["id"],)
        ):
            if (
                status in ("marked", "reviewed")
                and submitted
                and submitted >= self.clock_ms() - self.settings.grade_recent_days * DAY_MS
                and topics
            ):
                rows = json.loads(topics)
                maximum = sum(r["max_score"] for r in rows)
                if maximum:
                    recent.append(sum(r["score"] for r in rows) / maximum)
        config = state["paper"].get("grade_config") or {}
        return predict_band(
            mastery,
            weights,
            checked,
            covered / total if total else 0,
            recent,
            self.settings,
            seed=int(state["started"]),
            boundaries=config.get("boundaries"),
            labels=config.get("labels"),
        )

    def _topic_of(self: Any, kc: str) -> str:
        node_id = self.content.kcs[kc].node_id
        seen = set()
        while node_id not in seen:
            seen.add(node_id)
            node = self.content.nodes.get(node_id)
            if node is None or node.get("kind") == "topic" or not node.get("parent_id"):
                return str(node_id)
            node_id = node["parent_id"]
        raise Refused("invalid_topic_graph")

    @staticmethod
    def _question_order(state: Mapping[str, Any]) -> list[str]:
        return [q["question_id"] for section in state["paper"]["sections"] for q in section["questions"]]

    def _selected_questions(self: Any, state: Mapping[str, Any], view: Mapping[str, Any]) -> set[str]:
        selected = set()
        for section in state["paper"]["sections"]:
            ids = [q["question_id"] for q in section["questions"]]
            if section.get("choose") is None:
                selected.update(ids)
            else:
                answered = [q for q in ids if view["answers"].get(q, {}).get("answer") is not None]
                saved = [q for q in ids if q in view["answers"] and q not in answered]
                rest = [q for q in ids if q not in answered and q not in saved]
                selected.update((answered + saved + rest)[: section["choose"]])
        return selected
