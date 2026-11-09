"""Engine state for a guided session (AGT-03, PED-03, PED-05, PED-08, AGT-10).

Everything the flow decides from lives here and is written only by engine code: the problem, the verified plan, the
current step, the hint rung, attempts, the board and the values each number may come from. Model output never
writes this state; it is read when building prompts and checking drafts."""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from agent_core.engine.events import new_id

NUMBER = re.compile(r"(?<![A-Za-z_])[-\u2212]?\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?(?:[eE][-+]?\d+)?")
# The largest power of ten a written number may carry. Beyond it a value is no quantity any lesson has, and exact
# arithmetic on it (10**10000000) would hold the event loop for minutes: such a token is not read as a number.
MAX_EXPONENT = 400
_EXPONENT = re.compile(r"[eE]([-+]?\d+)$")


def expression_key(value: str) -> str:
    """Conservative spelling equivalence, never algebra or removal of grouping."""
    value = " ".join(value.replace("\u2212", "-").replace("**", "^").split())
    value = re.sub(r"\^\{(-?\d+)\}", r"^\1", value)
    return re.sub(r"\s*([+*/^=()-])\s*", r"\1", value)


def exponent_ok(token: str) -> bool:
    """A written number's power of ten is within MAX_EXPONENT (a number without one always is)."""
    found = _EXPONENT.search(token)
    if found is None:
        return True
    digits = found.group(1).lstrip("+-").lstrip("0")
    return len(digits) <= 3 and int(digits or "0") <= MAX_EXPONENT


def numbers_in(text: str) -> list[Fraction]:
    """Every number written in the text, exactly (fractions as written, decimals exactly). A text is parsed once:
    student lines are read on every gate check of every turn."""
    return list(_numbers_in(text))


_STATED = re.compile(r"[-\u2212]?\d+(?:\.\d+)?(?:\s*/\s*\d+)?\s*([A-Za-z/^0-9\u00b0]*)")


def stated_value(text: str) -> Fraction | None:
    """The value a student states: the number after the last `=` when nothing but a unit follows it, or a bare number.
    A working line ("0 = 14^2 - 2(9.8)s") states no value."""
    tail = text.rsplit("=", 1)[-1].strip().rstrip(".?!")
    if _STATED.fullmatch(tail):
        found = numbers_in(tail)
        return found[0] if found else None
    return None


def stated_unit(text: str) -> str | None:
    """The unit written after a stated value ("s = 10 km" is km), if there is one."""
    found = _STATED.fullmatch(text.rsplit("=", 1)[-1].strip().rstrip(".?!"))
    return (found.group(1) or None) if found else None


@functools.lru_cache(maxsize=4096)
def _numbers_in(text: str) -> tuple[Fraction, ...]:
    found = []
    for match in NUMBER.finditer(text.replace("\u2212", "-")):
        token = match.group(0).replace(" ", "")
        if not exponent_ok(token):
            continue
        try:
            if "/" in token:
                top, bottom = token.split("/")
                found.append(Fraction(top) / Fraction(bottom))
            else:
                found.append(Fraction(token))
        except (ValueError, ZeroDivisionError):
            continue
    return tuple(found)


@dataclass
class BoardStep:
    step_id: str
    index: int
    latex: str
    reason: str | None
    owner: str  # tutor or student
    status: str
    check_id: str | None = None
    # The first error a check named on an incorrect line: its TABLE VER-06a category and copy key (API-12, UXS-16).
    error: dict[str, str] | None = None


@dataclass
class Board:
    board_id: str = field(default_factory=lambda: new_id("brd"))
    steps: list[BoardStep] = field(default_factory=list)
    knowns: list[dict[str, Any]] = field(default_factory=list)
    unknowns: list[dict[str, Any]] = field(default_factory=list)
    problem_key: str | None = None  # the problem this board was opened for with `board.reset` (API-41)


@dataclass
class PlanStep:
    index: int
    latex: str
    value: str | None
    unit: str | None
    check_id: str | None


@dataclass
class Plan:
    """The Solver's plan after the verified gate: every computed step has a passing check (AGT-10)."""

    steps: list[PlanStep]
    final_value: str
    final_unit: str | None
    final_check_id: str
    method: str = ""
    final_latex: str = ""  # the final answer as written ("s = 10 m"): names the unknown


@dataclass
class Problem:
    problem_id: str
    statement: str
    statement_ref: str


@dataclass
class SessionState:
    session_id: str
    subject: str = "physics"
    level: str = "O"
    mode: str = "guided"
    hardware_class: str = "mid"
    # Solution mode was opened by asking for this problem's solution (PED-09, JRN-09): the next problem starts guided
    # again. A session put in solution mode from the mode menu stays in it.
    asked_for_solution: bool = False
    solution_shown: bool = False  # this problem's verified solution is on the board (AGT-20)
    reading_level: str = "standard"  # support, standard or advanced: the student's own choice (LOC-22, PED-21)
    assessment_origin: str | None = None
    # Where the session was opened from (`CreateSession.origin.kind`) and its topic: a session started by asking follows
    # its first question's subject, and a diagnosed topic is the session's (AGT-15, API-41, D94).
    origin: str = "ask"
    topic_id: str | None = None
    mock_running: bool = False
    problem: Problem | None = None
    plan: Plan | None = None
    step: int = 1  # 1-based index into plan.steps
    rung: int = 0  # current hint rung on this step (0: no hint given yet)
    attempts: dict[int, int] = field(default_factory=dict)  # step -> attempts
    deflected: set[int] = field(default_factory=set)  # steps that had their one entry question (PED-05)
    board: Board = field(default_factory=Board)
    check_values: dict[str, list[Fraction]] = field(default_factory=dict)  # check_id -> values in its result
    # Whole expressions proved by the worker; their digits are never independent numeric evidence.
    check_expressions: dict[str, list[str]] = field(default_factory=dict)
    # check_id -> the worker's `implausible` warnings (VER-26): outside the teaching skill's plausibility band, a
    # probable input error. Such a value traces to no check until the Checker has reviewed it (`reviewed`).
    implausible: dict[str, list[str]] = field(default_factory=dict)
    reviewed: list[str] = field(default_factory=list)
    student_lines: list[str] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)  # persisted messages (never tutor-context)
    solved: bool = False
    practice: dict[str, Any] | None = None  # the practice item and part (AGT-23), when in practice
    # M14.d/f engine state (PED-06, PED-07, PED-09, PED-19, PRV-20, AGT-21): the hint pacing on the current step, the
    # gaming pause (LRN-19), rung-5 steps and the isomorphic items they scheduled, a submitted mock answer (counts as
    # an attempt), values a correction would reveal, questions queued from a bulk paste, the one personal-details
    # notice per session.
    hint_step: int = 0
    hint_at_ms: int | None = None
    gaming: bool = False
    bottom_outs: list[int] = field(default_factory=list)
    scheduled: list[dict[str, Any]] = field(default_factory=list)
    mock_answer_submitted: bool = False
    withheld_extra: list[str] = field(default_factory=list)
    question_queue: list[str] = field(default_factory=list)
    personal_notice_sent: bool = False
    # A lesson session (AGT-22, `origin.kind: lesson`): the lesson it plays, and new problems the student asked
    # during it, kept to try after the lesson.
    lesson_id: str | None = None
    after_lesson: list[str] = field(default_factory=list)
    # The engine's own offers (LRN-20, LRN-23): wrong answers in a row on the open problem's step, the steps already
    # offered a hint and those whose offer the student has since taken up with a hint (D112), each open offer card by
    # request id, the KCs whose detour the student declined this session, the
    # detour under way (its prerequisite, items and place) and the positions detours return to, innermost last.
    wrong_streak: int = 0
    offered_steps: list[int] = field(default_factory=list)
    offer_pulled: list[int] = field(default_factory=list)
    offers: dict[str, dict[str, Any]] = field(default_factory=dict)
    detour_declined: list[str] = field(default_factory=list)
    detour: dict[str, Any] | None = None
    detour_stack: list[dict[str, Any]] = field(default_factory=list)
    # The visuals shown for the current problem, as ids only ({visual_id, parts}): what a spoken cue may point at
    # (VOC-10, VSL-21, VSL-45). Cleared with a new problem.
    shown_visuals: list[dict[str, Any]] = field(default_factory=list)
    # The manipulation the student's turn is (VSL-42): visual id, kind, awaiting step, adjustable parts' keys.
    manipulation: dict[str, Any] | None = None
    # What the student has selected or hovers (UXS-58): `stp_….t2-t4` or `<visual_id>:<part_id>`, so "this" means it.
    selection: str | None = None
    # Board lines read from the student's own photo (JM-D10): step id -> `cap_<id>:rgn_<n>`, the region SAM outlined,
    # so pointing at the line also shows it on the photo. Kept only for steps still on the board.
    photo_lines: dict[str, str] = field(default_factory=dict)
    # Parts of the student's photographed sketch the engine compared (JM-D10): `cap_<id>:rgn_<n>`, pointable too.
    photo_marks: list[str] = field(default_factory=list)

    # --- provenance (AGT-10, H1) -------------------------------------------------------------------------------

    def unreviewed_implausible(self) -> set[str]:
        """Checks flagged `implausible` that the Checker has not reviewed (VER-26)."""
        return set(self.implausible) - set(self.reviewed)

    def provenance(self) -> set[Fraction]:
        values: set[Fraction] = set()
        flagged = self.unreviewed_implausible()
        for check_id, found in self.check_values.items():
            if check_id not in flagged:  # an implausible result is not a checked value until reviewed (VER-26)
                values.update(found)
        if self.problem:
            values.update(numbers_in(self.problem.statement))
        if self.practice and self.practice.get("completed_mastery"):
            values.update(Fraction(v) for v in self.practice.get("convention_values", {}).values())
            for checked_line in self.practice.get("authored_numbers", {}).values():
                values.update(Fraction(v) for v in checked_line)
        for line in self.student_lines:
            values.update(numbers_in(line))
        return values

    # --- withheld set (PED-08, AGT-09) -------------------------------------------------------------------------

    def withheld(self) -> list[tuple[Fraction, str | None]]:
        """The final answer and the results of steps the student has not reached; rung 4 and 5 may show the
        current step only. Values the student derived (or rung 5 showed) are no longer withheld."""
        if self.plan is None or not self.leak_gated() or self.solved:
            return []
        shown_through = self.step if self.rung >= 4 else self.step - 1
        if (
            self.practice
            and self.practice.get("completed_mastery")
            and self.rung == 5
            and len(self.practice["item"]["parts"]) == 1
        ):
            # This authored ladder describes the whole single part, whose full-step hint gives its answer.
            shown_through = self.plan.steps[-1].index if self.plan.steps else 0
        values: list[tuple[Fraction, str | None]] = []
        for step in self.plan.steps:
            if step.index > shown_through and step.value is not None:
                parsed = numbers_in(step.value)
                if parsed:
                    values.append((parsed[0], step.unit))
        final = numbers_in(self.plan.final_value)
        last = self.plan.steps[-1].index if self.plan.steps else 0
        if final and not (shown_through >= last):
            values.append((final[0], self.plan.final_unit))
        values += [(Fraction(v), None) for v in self.withheld_extra]  # what a correction would show (AGT-21)
        derived = self._derived()
        return [(v, u) for v, u in values if v not in derived]

    def _derived(self) -> set[Fraction]:
        """The values the student has reached on this problem. The statement and earlier problems are not their work,
        and a line that quotes a number from the statement ("3x = 9") has reached only the value it states."""
        statement = self.problem.statement if self.problem else None
        lines = self.student_lines
        if statement in lines:
            lines = lines[len(lines) - lines[::-1].index(statement) :]
        givens = set(numbers_in(statement)) if statement else set()
        derived: set[Fraction] = set()
        for line in lines:
            stated = stated_value(line)
            derived.update(v for v in numbers_in(line) if v not in givens or v == stated)
        return derived

    def leak_gated(self) -> bool:
        """The no-answer-leak gate runs in every mode but solution mode; in revision the answer shows after the
        attempt (PED-01, PED-25)."""
        if self.mode == "solution":
            return False
        return not (self.mode == "revision" and self.attempts.get(self.step, 0) > 0)

    def current_step(self) -> PlanStep | None:
        """The first step at or after the pointer that has a value to reach (statement steps have none)."""
        if self.plan is None or not self.plan.steps:
            return None
        for step in self.plan.steps:
            if step.index >= self.step and step.value is not None:
                return step
        return None


# --- persistence codec (API-17, DAT-06 provider built early): plain JSON, Fractions as exact strings ---------------


def state_to_json(state: SessionState) -> dict[str, Any]:
    def plain(value: Any) -> Any:
        if isinstance(value, Fraction):
            return str(value)
        if isinstance(value, set):
            return sorted(plain(v) for v in value)
        if isinstance(value, dict):
            return {str(k): plain(v) for k, v in value.items()}
        if isinstance(value, list):
            return [plain(v) for v in value]
        if hasattr(value, "__dataclass_fields__"):
            return {name: plain(getattr(value, name)) for name in value.__dataclass_fields__}
        return value

    data = plain(state)
    assert isinstance(data, dict)
    return data


def state_from_json(data: dict[str, Any]) -> SessionState:
    board = data["board"]
    plan = data["plan"]
    problem = data["problem"]
    return SessionState(
        **{
            k: v
            for k, v in data.items()
            # A field an earlier build stored and this one dropped is not read back.
            if k in SessionState.__dataclass_fields__
            and k not in ("problem", "plan", "attempts", "deflected", "board", "check_values")
        },
        problem=Problem(**problem) if problem else None,
        plan=Plan(**{**plan, "steps": [PlanStep(**s) for s in plan["steps"]]}) if plan else None,
        attempts={int(k): v for k, v in data["attempts"].items()},
        deflected=set(data["deflected"]),
        board=Board(
            board["board_id"],
            [BoardStep(**s) for s in board["steps"]],
            board["knowns"],
            board["unknowns"],
            board.get("problem_key"),
        ),
        check_values={k: [Fraction(v) for v in values] for k, values in data["check_values"].items()},
    )
