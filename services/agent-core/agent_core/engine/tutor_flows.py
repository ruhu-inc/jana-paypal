"""Tutor flows, modes, the hint ladder's progression and the answer policy, as engine code (AGT-19 to AGT-23, AGT-25,
PED-01, PED-02, PED-04, PED-06, PED-07, PED-09, PED-22, PED-24, PED-25, TOL-22).

Code decides the flow (AGT-03); everything here is deterministic and runs offline with no model:

- Modes (PED-01): each mode is a flow with the roles it may call (so the tools it exposes), the rungs it allows, what
  happens to the final answer and how much its attempts count toward mastery (TABLE LRN-06b).
- Mode changes (PED-02, AGT-25): the answer policy (PED-09) and the mock lock (AGT-24) are checked in code before any
  change; a forbidden change answers with the policy template and makes no model call. A change keeps the board, the
  plan and the transcript.
- The ladder (PED-03 to PED-07): the rung rises by at most one per request with a minimum gap between rungs on a step
  (requests inside it are the gaming signal of LRN-19); "Show this step" jumps to rung 5 after an attempt; rung 5 is a
  bottom-out with its consequences. Rung content comes from authored ladders, never the model (PED-04).
- The guided flow (AGT-19) and the misconception flow (PED-22) as state machines; saved questions for human help
  (PED-24); the lesson flow with its checkpointed block index and the session plan tool (AGT-22, TOL-22)."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

from agent_core.config import Settings
from agent_core.engine.state import SessionState, numbers_in

if TYPE_CHECKING:
    from agent_core.assets.skills import Skill
    from agent_core.engine.turn import Engine, TurnIds, TurnResult

MODES = ("guided", "solution", "check_working", "learn", "practice", "mock", "revision")
POLICY_VALUES = ("on_request",)  # Guided by default, a full solution on request (D87).
ALL_RUNGS = (1, 2, 3, 4, 5)


# --- modes (PED-01) ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModeContract:
    mode: str
    roles: tuple[str, ...]  # the specialists the flow may call (TABLE AGT-15a); the Coach has no compute or UI tools
    rungs: tuple[int, ...]  # the hint rungs the mode allows
    final_answer: str  # when the final answer may reach the student
    leak_gate: bool  # the no-answer-leak gate runs (off only where the answer is the point: solution mode)
    verified_gate: bool = True  # every number traces to a check, in every mode (H1)


MODE_CONTRACTS: dict[str, ModeContract] = {
    "guided": ModeContract(
        "guided", ("diagnoser", "solver", "checker", "coach"), ALL_RUNGS, "when_derived_or_rung5", True
    ),
    "solution": ModeContract("solution", ("diagnoser", "solver", "checker", "coach"), (), "shown", False),
    "check_working": ModeContract(
        "check_working", ("solver", "checker", "diagnoser", "coach"), ALL_RUNGS, "only_if_students_is_right", True
    ),
    "learn": ModeContract("learn", ("checker", "coach"), ALL_RUNGS, "quick_checks_only", True),
    "practice": ModeContract("practice", ("diagnoser", "checker", "coach"), ALL_RUNGS, "after_item_closes", True),
    "mock": ModeContract("mock", (), (), "after_submit", True),
    "revision": ModeContract("revision", ("checker", "coach"), (1, 2), "after_attempt", True),
}


def contract(mode: str, settings: Settings | None = None) -> ModeContract:
    found = MODE_CONTRACTS[mode]
    if mode == "revision" and settings is not None:  # rungs 1 to the configured cap only (PED-25)
        found = ModeContract(found.mode, found.roles, tuple(r for r in ALL_RUNGS if r <= settings.revision_max_rung),
                             found.final_answer, found.leak_gate)  # fmt: skip
    return found


def mode_tools(mode: str, tools_of: Callable[[str], Iterable[str]]) -> frozenset[str]:
    """The tools a mode exposes: the union of its roles' tools (`tools_of(role)`, the role grants of TOL-16)."""
    return frozenset(t for role in MODE_CONTRACTS[mode].roles for t in tools_of(role))


def mastery_weight(mode: str, settings: Settings) -> float:
    """How much a mode's attempts count toward mastery (TABLE LRN-06b; solution mode adds no observation)."""
    if mode == "solution":
        return 0.0
    return {"check_working": settings.obs_mode_check_working, "learn": settings.obs_mode_learn}.get(mode, 1.0)


# --- the answer policy (PED-09) ------------------------------------------------------------------------------------


class PolicyRefused(ValueError):
    """A write of a value outside TABLE PED-09a."""


def validate_policy(value: Any) -> str:
    if value not in POLICY_VALUES:
        raise PolicyRefused(f"solution_policy must be one of {', '.join(POLICY_VALUES)}")
    return str(value)


def attempted(state: SessionState) -> bool:
    """At least one checked attempt on the problem; a submitted mock answer counts (TABLE PED-09a)."""
    return any(n > 0 for n in state.attempts.values()) or state.mock_answer_submitted


def solution_template(state: SessionState) -> str | None:
    """A requested solution is available outside an active mock, including for old checkpoints (D87)."""
    return "tpl.mock_running" if state.mock_running else None


# --- mode changes (PED-02, AGT-25) ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModeDecision:
    allowed: bool
    template: str | None  # the reviewed message when refused (none when the refusal has no student-facing words)
    reason: str  # the `mode_change` reason, or why it was refused


def decide_mode_change(state: SessionState, to_mode: str, *, queue_empty: bool = True,
                       reason: str = "student_request") -> ModeDecision:  # fmt: skip
    """Checked in code before any change or call: the mock lock first, then the answer policy (AGT-25, PED-02)."""
    if to_mode not in MODES:
        raise ValueError(f"unknown mode {to_mode!r}")
    if state.mock_running and to_mode != "mock":
        return ModeDecision(False, "tpl.mock_running", "mock_running")  # until the paper is submitted or abandoned
    if to_mode == "mock":
        return ModeDecision(queue_empty, None, "mock_start" if queue_empty else "tutor_queue_busy")
    if to_mode == "solution":
        template = solution_template(state)
        if template is not None:
            return ModeDecision(False, template, "policy_block")
    return ModeDecision(True, None, reason)


def mode_change_event(state: SessionState, from_mode: str, to_mode: str, reason: str) -> dict[str, Any]:
    return {"session_id": state.session_id.removeprefix("ses_"), "from_mode": from_mode, "to_mode": to_mode,
            "reason": reason}  # fmt: skip


# --- the hint ladder (PED-03 to PED-07, LRN-19) --------------------------------------------------------------------


@dataclass(frozen=True)
class HintDecision:
    kind: Literal["entry_question", "hint", "paused"]
    rung: int  # the rung shown (0 when none)
    gaming: bool = False  # a request inside the gap: the help-seeking line, not a higher rung (LRN-19)
    bottom_out: bool = False


def hint_request(state: SessionState, now_ms: int, settings: Settings, *, start_rung: int = 1,
                 show_step: bool = False, repeat: bool = False) -> HintDecision:  # fmt: skip
    """The engine's answer to "I'm stuck" (or "Show this step") on the current step. Writes engine state only."""
    if state.hint_step != state.step:  # the rung resets when the step changes (PED-06)
        if state.hint_step:
            state.rung = 0
        state.hint_step, state.hint_at_ms, state.gaming = state.step, None, False
    tried = state.attempts.get(state.step, 0) > 0
    if show_step and tried:  # UXS-17: straight to rung 5, with the bottom-out consequences
        state.rung, state.hint_at_ms = 5, now_ms
        return HintDecision("hint", 5, bottom_out=True)
    if not tried and state.step not in state.deflected:  # PED-05: one easier entry question first
        state.deflected.add(state.step)
        return HintDecision("entry_question", 0)
    if repeat:  # a barge-in re-ask of the same request is not a second request (VOC-36)
        return HintDecision("hint", state.rung)
    gap_ms = settings.hint_min_gap_s * 1000
    if state.gaming:  # paused until the student submits an attempt (`attempt_arrived`)
        return HintDecision("paused", state.rung, gaming=True)
    if state.hint_at_ms is not None and now_ms - state.hint_at_ms < gap_ms:
        state.gaming = True  # hints pause until an attempt arrives (LRN-19)
        return HintDecision("paused", state.rung, gaming=True)
    allowed = contract(state.mode if state.mode in MODE_CONTRACTS else "guided", settings).rungs or ALL_RUNGS
    top = max(allowed)
    rung = max(start_rung, 1) if state.rung == 0 else state.rung + 1
    rung = min(rung, top)
    state.rung, state.hint_at_ms = rung, now_ms
    return HintDecision("hint", rung, bottom_out=rung == 5)


def attempt_arrived(state: SessionState) -> None:
    """An attempt clears the gaming pause (LRN-19)."""
    state.gaming = False


# --- rung content provenance (PED-04) ------------------------------------------------------------------------------

LADDER_LINE = re.compile(r"^\s*([1-5])\.\s+(.+)$", re.M)


def skill_ladder(skill: Skill | None) -> dict[int, str]:
    """A teaching skill's authored `## Hint ladder` lines by rung (SKL-05): instructions to the Coach, never shown."""
    if skill is None or "## Hint ladder" not in skill.body:
        return {}
    section = skill.body.split("## Hint ladder", 1)[1].split("\n## ", 1)[0]
    return {int(n): text.strip() for n, text in LADDER_LINE.findall(section)}


@dataclass(frozen=True)
class RungSource:
    source: Literal["item_ladder", "item_solution", "skill_ladder", "generic_template"]
    ref: str  # where the rung's content comes from
    text: str  # the authored content the Coach rephrases (or the generic template)

    @property
    def event_source(self) -> str:
        """The `hint` event's `source`: authored content of any kind is the skill ladder's family (LRN-01)."""
        return "generic_template" if self.source == "generic_template" else "skill_ladder"


def rung_source(rung: int, *, item: Mapping[str, Any] | None = None, part_id: str | None = None,
                step_index: int = 1, skill: Skill | None = None,
                generic: Mapping[int, str] | None = None) -> RungSource:  # fmt: skip
    """Where rung content comes from, in order: the item's own ladder (per part: rungs 1-2 for the part, 3-4 for the
    current step of its verified solution, 5 that step completed), then the teaching skill's ladder, then the generic
    template (PED-04)."""
    if item is not None and part_id is not None:
        part = next((p for p in item.get("parts", []) if p.get("id") == part_id), None)
        if part is not None:
            hints = sorted(part.get("hints") or [], key=lambda h: int(h.get("rung", 0)))
            if rung <= 2 and hints:
                hint = next((h for h in hints if int(h.get("rung", 0)) == rung), hints[min(rung, len(hints)) - 1])
                text = (hint.get("text") or {}).get("en") if isinstance(hint.get("text"), dict) else hint.get("text")
                if text:
                    return RungSource("item_ladder", f"{item['id']}#{part_id}.hint{hint.get('rung', rung)}", str(text))
            steps = (item.get("solution") or {}).get("steps") or []
            step = next((s for s in steps if int(s.get("n", 0)) == step_index), steps[-1] if steps else None)
            if rung >= 3 and step is not None:
                return RungSource("item_solution", f"{item['id']}#step{step.get('n')}", str(step.get("latex", "")))
    ladder = skill_ladder(skill)
    if rung in ladder and skill is not None:
        return RungSource("skill_ladder", f"{skill.name}#hint-ladder.{rung}", ladder[rung])
    return RungSource("generic_template", f"hints.{rung}", str((generic or {}).get(rung, "")))


# --- bottom-out consequences (PED-07) ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Scheduled:
    """An isomorphic item to come back to (PED-07): the planner (LRN-28) picks the item from these KCs in the window."""

    problem_ref: str
    kc_ids: tuple[str, ...]
    earliest: str  # ISO dates
    latest: str
    reason: str = "bottom_out"


def bottom_out(state: SessionState, problem_ref: str, kc_ids: Sequence[str], now_ms: int,
               settings: Settings) -> Scheduled:  # fmt: skip
    """Rung 5 was shown: the opportunity counts as not mastered (its `hint` event at rung 5 selects the full-failure
    row of TABLE LRN-06a), an isomorphic item is scheduled in the configured window, and the follow-up asks the student
    to explain the step back."""
    earliest_days, latest_days = settings.bottom_out_review_days
    day = 86_400_000
    scheduled = Scheduled(problem_ref, tuple(kc_ids), _iso_day(now_ms + earliest_days * day),
                          _iso_day(now_ms + latest_days * day))  # fmt: skip
    state.bottom_outs.append(state.step)
    state.scheduled.append(scheduled.__dict__.copy())
    return scheduled


def _iso_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).date().isoformat()


# --- check my working: what a correction would reveal (AGT-21) -----------------------------------------------------

_PRODUCT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*([*^/\u00d7])\s*(\d+(?:\.\d+)?)(?![\w.])")


def correction_values(previous_line: str) -> list[Fraction]:
    """The values one operation away from the student's last valid line (each product, quotient or power of two
    written numbers): what a correction of the next line would show. They join the withheld set until the student
    writes them, so the Coach says where to look and never writes the corrected line (AGT-21)."""
    out: list[Fraction] = []
    for a, op, b in _PRODUCT.findall(previous_line.replace("\u2212", "-")):
        x, y = Fraction(a), Fraction(b)
        if op in ("*", "\u00d7"):
            out.append(x * y)
        elif op == "/" and y != 0:
            out.append(x / y)
        elif op == "^" and y.denominator == 1 and 0 <= y <= 6:
            out.append(x ** int(y))
    return out


# --- the guided flow (AGT-19) --------------------------------------------------------------------------------------

GUIDED_NODES = ("question", "input_guard", "diagnoser", "clarify", "concept", "solver", "checker_plan",
                "honest_blocker", "coach_rung", "student_reply", "checker_diagnoser", "raise_rung_or_target", "wrap_up",
                "side_fork", "template")  # fmt: skip
GUIDED_EDGES = frozenset({
    ("question", "input_guard"), ("input_guard", "diagnoser"), ("input_guard", "template"),
    ("diagnoser", "clarify"), ("diagnoser", "concept"), ("concept", "coach_rung"), ("diagnoser", "solver"),
    ("solver", "checker_plan"),
    ("checker_plan", "honest_blocker"), ("checker_plan", "coach_rung"), ("checker_plan", "solver"),
    ("coach_rung", "student_reply"), ("student_reply", "checker_diagnoser"), ("student_reply", "raise_rung_or_target"),
    ("student_reply", "side_fork"), ("side_fork", "coach_rung"), ("checker_diagnoser", "coach_rung"),
    ("checker_diagnoser", "wrap_up"), ("checker_diagnoser", "raise_rung_or_target"),
    ("raise_rung_or_target", "coach_rung"),
})  # fmt: skip


@dataclass
class GuidedFlow:
    """The guided mode flow of AGT-19 as a state machine: code picks every next node from the event; models only fill
    the nodes' content. `path` is the node sequence the turns produced."""

    path: list[str] = field(default_factory=lambda: ["question"])
    plan_failures: int = 0

    @property
    def node(self) -> str:
        return self.path[-1]

    def _go(self, node: str) -> str:
        if (self.node, node) not in GUIDED_EDGES:
            raise ValueError(f"no edge {self.node} -> {node}")
        self.path.append(node)
        return node

    def guard(self, in_scope: bool) -> str:
        self._go("input_guard")
        return self._go("diagnoser" if in_scope else "template")

    def diagnosed(self, needs_clarification: bool, conceptual: bool = False) -> str:
        """Clarify first; a definition or explanation with nothing to calculate skips the Solver (D122); else plan."""
        if needs_clarification:
            return self._go("clarify")
        if conceptual:
            self._go("concept")
            return self._go("coach_rung")
        return self._go("solver")

    def plan_checked(self, passed: bool) -> str:
        if self.node == "solver":
            self._go("checker_plan")
        if passed:
            return self._go("coach_rung")
        self.plan_failures += 1
        if self.plan_failures >= 2:  # fails twice: the honest blocker, method without numbers
            return self._go("honest_blocker")
        self._go("solver")
        return self._go("checker_plan")

    def reply(self, kind: Literal["answer", "stuck", "side_question"]) -> str:
        self._go("student_reply")
        if kind == "stuck":
            self._go("raise_rung_or_target")
            return self._go("coach_rung")
        if kind == "side_question":
            self._go("side_fork")
            return self._go("coach_rung")
        return self._go("checker_diagnoser")

    def checked(self, correct: bool, last_step: bool) -> str:
        if not correct:
            self._go("raise_rung_or_target")
            return self._go("coach_rung")
        return self._go("wrap_up" if last_step else "coach_rung")


# --- misconceptions (PED-22) ---------------------------------------------------------------------------------------


@dataclass
class MisconceptionFlow:
    """The misconception handling flow of PED-22. Statuses are learner-model events: the engine emits one
    `misconception_evidence` per match (the projection derives suspected or active from them) and the check item's
    verdict; `status` here mirrors what the projection will show, for the turn."""

    misconception_id: str
    kc_id: str
    active_evidence: int  # Settings.misconception_active_evidence
    node: str = "matched"
    status: str | None = None
    fixes: int = 0
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def matched(self, evidence_count: int, source: Literal["signature", "probe", "check_working"] = "signature"
                ) -> str:  # fmt: skip
        """A signature (or probe) matched; `evidence_count` counts this match."""
        self.events.append(("misconception_evidence", {"misconception_id": self.misconception_id,
                                                       "kc_id": self.kc_id, "evidence_source": source,
                                                       "weight": 1.0}))  # fmt: skip
        if evidence_count >= self.active_evidence or source == "probe":
            return self._name_it()
        self.node = "probe"  # ask one probing question
        return self.node

    def probe_answered(self, matches_again: bool) -> str:
        if self.node != "probe":
            raise ValueError("no probe is open")
        if matches_again:
            self.events.append(("misconception_evidence", {"misconception_id": self.misconception_id,
                                                           "kc_id": self.kc_id, "evidence_source": "probe",
                                                           "weight": 1.0}))  # fmt: skip
            return self._name_it()
        self.node, self.status = "continue", "suspected"
        return self.node

    def _name_it(self) -> str:
        self.status, self.node = "active", "name_and_fix"  # name it gently; the skill's targeted fix
        self.fixes += 1
        return self.node

    def check_item(self) -> str:
        if self.node != "name_and_fix":
            raise ValueError("the check item follows the fix")
        self.node = "discriminating_check"
        return self.node

    def check_answered(self, correct: bool) -> str:
        if self.node != "discriminating_check":
            raise ValueError("no check item is open")
        if correct:  # two more correct on later days resolve it (the projection, LRN-37)
            self.status, self.node = "resolving", "resolving"
        else:
            self.status, self.node = "active", "switch_representation"
        return self.node

    def survived(self, limit: int) -> bool:
        """The misconception survived `limit` fixes: the question is saved for human help (PED-24)."""
        return self.fixes >= limit and self.status == "active"


# --- saved questions for human help (PED-24) -----------------------------------------------------------------------

SaveReason = Literal["stuck", "repeated_error", "verification_disagreement", "outside_content", "student_requested"]
SAVE_REASONS = frozenset(get_args(SaveReason))
SUBJECTS = frozenset({"mathematics", "physics", "chemistry", "biology", "accounting"})


def saved_question_board(state: SessionState) -> list[dict[str, str]]:
    """Only checked lines from this problem's board; an older board can remain until the next board operation."""
    if state.problem is None:
        return []
    digest = hashlib.sha256(state.problem.statement.encode()).hexdigest()[:16]
    if state.board.problem_key != f"{state.problem.problem_id}:{digest}":
        return []
    return [{"latex": s.latex[:300], "reason": (s.reason or "")[:120]} for s in state.board.steps
            if s.status in ("final", "correct", "equivalent_unsimplified") and s.check_id][:100]  # fmt: skip


@dataclass
class SavedQuestions:
    """Local saved questions (PED-24, UXS-42): written to the learner database only. There is no send path, no
    teacher queue and no network; the notebook can be printed or shown to someone the student trusts."""

    conn: sqlite3.Connection
    retention_ms: int
    clock: Callable[[], int]
    saved: set[tuple[str, str]] = field(default_factory=set)  # (problem, reason) already kept this session

    def save(self, state: SessionState, reason: str, topic_id: str | None = None) -> str | None:
        """One list item per trigger and problem; None when this trigger already saved this problem."""
        if reason not in SAVE_REASONS:
            raise ValueError(f"unknown save trigger {reason!r}")
        from agent_core.store.events import iso_ms
        from agent_core.store.ids import new_ulid

        if state.problem is None:
            return None
        problem = state.problem.statement if state.problem else ""
        key = (problem, reason)
        if key in self.saved:
            return None
        now = self.clock()
        question_id = new_ulid(now)
        board = saved_question_board(state)
        snapshot = {"kind": "saved_question", "question_id": question_id,
                    "subject": state.subject if state.subject in SUBJECTS else "physics", "topic_id": topic_id,
                    "problem": problem[:8000], "board": board, "reason": reason, "created_at": iso_ms(now)}  # fmt: skip
        self.conn.execute("INSERT INTO saved_questions (question_id, session_id, content, status, created_at,"
                          " expires_at) VALUES (?, ?, ?, 'kept', ?, ?)",
                          (question_id, state.session_id.removeprefix("ses_"), json.dumps(snapshot), now,
                           now + self.retention_ms))  # fmt: skip
        self.saved.add(key)
        return question_id


def record_wellbeing_shown(conn: sqlite3.Connection, category: str, now: int) -> str:
    """The local parent view's `wellbeing_message_shown` entry (TABLE TCH-14a): the category only, never the student's
    words, in this profile's learner database. Nothing sends it anywhere (PRV-14, TCH-14; D102)."""
    from agent_core.store.ids import new_ulid

    alert_id = new_ulid(now)
    conn.execute("INSERT INTO alerts (alert_id, kind, severity, audience, payload, created_at) VALUES"
                 " (?, 'wellbeing_message_shown', 'urgent', 'parent', ?, ?)",
                 (alert_id, json.dumps({"category": category}), now))  # fmt: skip
    return alert_id


# --- the session plan (TOL-22)-------------------------------------------------------------------------------------

TODO_STATUSES = ("pending", "in_progress", "done", "skipped")


class PlanRefused(ValueError):
    pass


@dataclass
class LessonPlan:
    """`lesson_plan_write({todos})`: replaces the whole session plan. Called by engine code only (the lesson flow and
    the learning system's plan), never by a model (AGT-03)."""

    todos: list[dict[str, Any]] = field(default_factory=list)

    def write(self, todos: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        new = [dict(t) for t in todos]
        for todo in new:
            if set(todo) != {"id", "title", "kc_ids", "status"} or todo["status"] not in TODO_STATUSES:
                raise PlanRefused("each todo is {id, title, kc_ids, status} with a known status")
        ids = [t["id"] for t in new]
        if len(set(ids)) != len(ids):
            raise PlanRefused("todo ids are unique")
        if sum(t["status"] == "in_progress" for t in new) != 1:
            raise PlanRefused("exactly one todo is in progress")
        dropped = {t["id"] for t in self.todos} - set(ids)
        if dropped:
            raise PlanRefused(f"todos {sorted(dropped)} were dropped without being marked skipped")
        self.todos = new
        return self.todos

    def card(self, title: str) -> dict[str, Any]:
        """The `plan` card's items (skipped todos are not shown)."""
        return {"card_type": "plan", "title": title[:200], "actions": [],
                "items": [{"id": str(t["id"])[:32], "text": str(t["title"])[:300], "status": t["status"]}
                          for t in self.todos if t["status"] != "skipped"][:12]}  # fmt: skip

    async def tool(self, args: dict[str, Any], context: Any) -> Any:
        """The host executor for the engine's grant (TOL-22)."""
        from agent_core.tools.spec import InvalidArguments

        try:
            return {"todos": self.write(args.get("todos") or [])}
        except PlanRefused as error:
            raise InvalidArguments(str(error)) from None


# --- the lesson flow (AGT-22) --------------------------------------------------------------------------------------


@dataclass
class LessonFlow:
    """A lesson plays its pack blocks in order (CUR-11); the block index is saved at every move so the lesson resumes
    where it stopped (MEM-02). `desk` is the lesson player's reads (`LearnDesk`: `lesson`, `save_position`)."""

    desk: Any
    lesson_id: str
    clock: Callable[[], int]
    plan: LessonPlan = field(default_factory=LessonPlan)
    view: dict[str, Any] = field(default_factory=dict)
    index: int = 0

    def open(self) -> dict[str, Any]:
        view = self.desk.lesson(self.lesson_id)
        if view is None:
            raise LookupError("that lesson is not installed")
        self.view = view
        position = view.get("position")
        self.index = int(position["block_index"]) if position else 0
        self._write_plan()
        return self.block()

    def block(self) -> dict[str, Any]:
        return dict(self.view["blocks"][self.index])

    @property
    def finished(self) -> bool:
        return self.index >= len(self.view["blocks"]) - 1

    def advance(self) -> dict[str, Any] | None:
        if self.finished:
            return None
        self.index += 1
        self.desk.save_position(self.lesson_id, self.index, self.view["version"], self.clock())
        self._write_plan()
        return self.block()

    def _write_plan(self) -> None:
        todos = []
        for n, block in enumerate(self.view["blocks"]):
            status = "done" if n < self.index else ("in_progress" if n == self.index else "pending")
            todos.append({"id": f"blk{n}", "title": str(block.get("type", "block")), "kc_ids":
                          list(self.view.get("kc_ids", [])), "status": status})  # fmt: skip
        self.plan.write(todos)

    @staticmethod
    def route_question(text: str) -> Literal["side_question", "after_lesson"]:
        """A student question mid-lesson: a new problem (numbers to work with) is offered as "Try this after the
        lesson"; anything else is answered as a side question (AGT-18)."""
        return "after_lesson" if len(numbers_in(text)) >= 2 else "side_question"


# --- the engine's mode entry point (AGT-20, AGT-25) ----------------------------------------------------------------


class TutorFlows:
    """Mode changes and solution mode on a running engine. A refused change ends its turn with the reviewed template
    and makes no model call; an allowed one keeps the board and plan and emits `mode_change`."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def switch_mode(self, to_mode: str, ids: TurnIds | None = None, *, queue_empty: bool = True,
                          stepped: bool = True) -> TurnResult:  # fmt: skip
        from agent_core.engine import turn_scope

        turn = self.engine._begin("ask", ids)
        with turn_scope(turn.turn_id):
            try:
                return await self._switch(turn, to_mode, queue_empty, stepped)
            except Exception:
                self.engine._finish_crashed(turn)
                raise

    async def _switch(self, turn: Any, to_mode: str, queue_empty: bool, stepped: bool) -> TurnResult:
        engine = self.engine
        decision = decide_mode_change(engine.state, to_mode, queue_empty=queue_empty)
        if not decision.allowed:
            if decision.template is None:
                return engine._end(turn, None)
            return engine._finish_template(turn, decision.template, "completed")
        if to_mode == "solution" and engine.state.problem is not None:
            return await engine._solution(turn, asked=False, stepped=stepped)  # the one engine switch (PED-09)
        engine._set_mode(turn, to_mode, decision.reason)
        return engine._end(turn, None)


def load_seeded_errors(path: Path) -> list[dict[str, Any]]:
    """The seeded-error set of AGT-21 (synthetic working with one known first wrong line)."""
    import yaml

    data = yaml.safe_load(path.read_text())
    return list(data["items"])
