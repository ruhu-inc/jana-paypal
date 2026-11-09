"""The engine's handlers for the tutor turn's fields: `update_board`, `show_hint`, `ask_student` (TOL-18 to TOL-20,
AGT-12). They are never offered to a model; Finalise calls them with the accepted tutor turn.

Board operations apply atomically: one invalid operation applies none. Board step statuses that report a check
(`correct`, `incorrect`, `equivalent_unsimplified`) are set by the engine from check results only."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from agent_core.contracts import errors as contract_errors
from agent_core.engine.annotations import resolve as resolve_annotation
from agent_core.engine.annotations import split_terms
from agent_core.engine.events import EventStream, new_id
from agent_core.engine.gates import CHECK_STATUSES, untraced_numbers
from agent_core.engine.state import Board, BoardStep, SessionState, numbers_in

OP_CONTRACT = "agents/tutor-turn#/properties/board/items"
ANNOTATE_ONLY: dict[str, Any] = {"mark": None, "part": None, "to": None}  # null on every op but annotate (VSL-40)
# `annotate` (VSL-40, S3, D103) marks a step, a term range of it or a visual part by id; an unresolvable one is dropped.
OPS = ("add_step", "revise_step", "mark_step", "highlight", "set_knowns", "await_student", "annotate")
ENGINE_STATUSES = ("correct", "incorrect", "equivalent_unsimplified", "awaiting", "checking", "unchecked")
MAX_OPTIONS = 4
SOMETHING_ELSE = "Something else…"


class HandlerError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class BoardResult:
    applied: bool
    step_ids: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)  # annotations dropped, with the reason (VSL-40)


def validate_ops(ops: list[dict[str, Any]], state: SessionState, context_numbers: set[Fraction]) -> None:
    """Every op valid against the tutor-turn contract, step refs exist, no engine-only status from the model, and
    no untraced number in any line (TOL-18, AGT-10). Raises `HandlerError` and applies nothing."""
    for n, op in enumerate(ops):
        problems = contract_errors(OP_CONTRACT, op)
        if problems:
            raise HandlerError("invalid_args", f"ops/{n}: {problems[0]}")
        if op["op"] in ("revise_step", "mark_step", "highlight") and not (1 <= op["step"] <= len(state.board.steps)):
            raise HandlerError("invalid_args", f"ops/{n}: step {op['step']} is not on the board")
        if op["op"] == "add_step" and op.get("status") in ENGINE_STATUSES:
            raise HandlerError("invalid_args", f"ops/{n}: status {op['status']} is set by the engine from a check")
        if op["op"] == "mark_step" and op.get("status") in CHECK_STATUSES:
            # API-41: a check status only with the step's own check id; a checked `incorrect` is never changed
            # (AGT-14, the stop gates refuse such a draft first).
            step = state.board.steps[op["step"] - 1]
            if step.check_id is None or op.get("check_id") != step.check_id:
                raise HandlerError("invalid_args", f"ops/{n}: {op['status']} needs the check id of step {op['step']}")
        if op["op"] in ("mark_step", "revise_step") and state.board.steps[op["step"] - 1].status == "incorrect" \
                and not (op["op"] == "mark_step" and op.get("status") == "incorrect"):  # fmt: skip
            raise HandlerError("invalid_args", f"ops/{n}: step {op['step']} was checked incorrect")
        if op.get("latex"):
            missing = untraced_numbers({f"ops/{n}": op["latex"]}, state, context_numbers)
            if missing:
                raise HandlerError("invalid_args", f"untraced number in {missing[0]}")


def update_board(ops: list[dict[str, Any]], state: SessionState, context_numbers: set[Fraction]) -> BoardResult:
    """Apply the ops to the board model and return the board events to emit (all or nothing). An op the engine wrote
    itself may leave out the annotate-only fields (`mark`, `part`, `to`; VSL-40, D103), which are null for it."""
    ops = [{**ANNOTATE_ONLY, **op} for op in ops]
    validate_ops(ops, state, context_numbers)
    board = state.board
    events: list[dict[str, Any]] = []
    step_ids: list[str] = []
    dropped: list[dict[str, Any]] = []
    for op in ops:
        kind = op["op"]
        if kind == "add_step":
            owner = op["owner"] or "tutor"
            status, check_id = op.get("status") or "final", op.get("check_id")
            if owner == "tutor":
                status, check_id = tutor_step_status(op["latex"], check_id, state)
            step = BoardStep(new_id("stp"), len(board.steps), op["latex"], op.get("reason"), owner, status, check_id)
            board.steps.append(step)
            step_ids.append(step.step_id)
            events.append(_step_event(board.board_id, step))
        elif kind in ("revise_step", "mark_step"):
            step = board.steps[op["step"] - 1]
            if kind == "revise_step":
                step.latex, step.reason = op["latex"] or step.latex, op.get("reason") or step.reason
            else:
                step.status = op["status"]
            step_ids.append(step.step_id)
            events.append(_step_event(board.board_id, step))
        elif kind == "highlight":
            step = board.steps[op["step"] - 1]
            events.append(
                {"type": "board.highlight", "board_id": board.board_id, "step_ids": [step.step_id], "ttl_ms": None}
            )
        elif kind == "set_knowns":
            events.append(
                {
                    "type": "board.knowns",
                    "board_id": board.board_id,
                    "knowns": board.knowns,
                    "unknowns": board.unknowns,
                    "conventions": [],
                }
            )
        elif kind == "await_student":
            step = BoardStep(new_id("stp"), len(board.steps), "", None, "student", "awaiting")
            board.steps.append(step)
            events.append(_step_event(board.board_id, step))
            events.append(
                {
                    "type": "board.await",
                    "board_id": board.board_id,
                    "step_id": step.step_id,
                    "prompt": op["prompt"][:300],
                    "input_kind": "latex",
                }
            )
        elif kind == "annotate":
            event, reason = resolve_annotation(op, state, context_numbers)
            if event is None:
                dropped.append({"mark": op["mark"], "step": op.get("step"), "part": op.get("part"), "reason": reason})
            else:
                events.append(event)
    return BoardResult(True, step_ids, events, dropped)


def tutor_step_status(latex: str, check_id: str | None, state: SessionState) -> tuple[str, str | None]:
    """A tutor step is `final` or `unchecked`, never `draft` (API-41): `final` when it states no number or when its
    check is known (the one cited, if it holds one of the step's numbers, else the check that produced one); a step
    with a number and no check is `unchecked` (API-12, AGT-10)."""
    if (
        state.practice
        and state.practice.get("completed_mastery")
        and state.plan
        and check_id
        and any(step.latex == latex and step.check_id == check_id for step in state.plan.steps)
    ):
        # The signed item's build check verifies this exact expression even when it shows no result yet.
        return "final", check_id
    numbers = numbers_in(latex)
    if not numbers:
        return "final", None
    givens = set(numbers_in(state.problem.statement)) if state.problem else set()
    computed = [n for n in numbers if n not in givens]
    if check_id is not None and any(v in state.check_values.get(check_id, ()) for v in numbers):
        return "final", check_id
    found = next((c for c, values in state.check_values.items() if any(v in values for v in computed)), None)
    return ("final", found) if found is not None else ("unchecked", None)


def open_board(state: SessionState) -> dict[str, Any] | None:
    """The board for the open problem: the first board operation for a problem opens a fresh board and returns its
    `board.reset` (reason `new_problem`; the part id is filled when the part opens), else None (API-41)."""
    problem = state.problem
    if problem is None:
        return None
    key = f"{problem.problem_id}:{hashlib.sha256(problem.statement.encode()).hexdigest()[:16]}"
    if state.board.problem_key == key:
        return None
    if state.board.problem_key is not None or state.board.steps:
        state.board = Board()
    state.board.problem_key = key
    return {"type": "board.reset", "board_id": state.board.board_id, "part_id": "",
            "problem": {"text": problem.statement[:4000], "latex": None}, "reason": "new_problem"}  # fmt: skip


def plan_card(prompt: str, options: list[dict[str, Any]]) -> dict[str, Any]:
    """A plan request (API-41): the prompt as its title and each option as a pending item."""
    items = [{"id": f"itm_{n}", "text": str(o["label"])[:300], "status": "pending"} for n, o in enumerate(options)]
    return {"card_type": "plan", "title": prompt[:200], "items": items[:12], "actions": ["start", "change"]}


def reflection_card(prompt: str, marks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A reflection request; its marks come only from Checker verdicts the engine holds (ASM-20), never the model."""
    return {"card_type": "reflection", "did": prompt[:600], "right": "", "next_goal": "", "marks": list(marks or [])}


def subject_card(title: str, body: str, subject: str) -> dict[str, Any]:
    """The engine's offer of a new question in another subject (AGT-15, D94): a confirm card naming the subject."""
    return {"card_type": "confirm", "purpose": "other", "title": title[:200], "body": body[:1000],
            "confirm_label_key": "card.new_subject.start", "cancel_label_key": "card.new_subject.stay",
            "subject": subject}  # fmt: skip


def confirm_card(prompt: str, purpose: str = "other") -> dict[str, Any]:
    """A confirm request; its purpose comes from the flow node, never the model."""
    return {"card_type": "confirm", "purpose": purpose, "title": prompt[:200], "body": "",
            "confirm_label_key": "card.confirm", "cancel_label_key": "card.cancel"}  # fmt: skip


def step_event(board_id: str, step: BoardStep) -> dict[str, Any]:
    return _step_event(board_id, step)


def _step_event(board_id: str, step: BoardStep) -> dict[str, Any]:
    return {
        "type": "board.step",
        "board_id": board_id,
        "step_id": step.step_id,
        "index": step.index,
        "latex": step.latex,
        "reason": step.reason,
        "owner": step.owner,
        "status": step.status,
        "check_id": step.check_id,
        "error": step_error(step),
        **({"terms": terms} if (terms := split_terms(step.latex)) else {}),
    }


# The first-error categories of TABLE VER-06a, as `step_error_category` of the events contract names them.
ERROR_CATEGORIES = ("sign", "arithmetic", "dropped_term", "illegal_operation", "units", "copy", "other")


def error_of(category: str | None) -> dict[str, str] | None:
    """The `error` of an incorrect line (API-12, UXS-16): the first error's TABLE VER-06a category and the copy key the
    board shows for it; a category outside the table is `other`, and no category is no error."""
    if category is None:
        return None
    named = category if category in ERROR_CATEGORIES else "other"
    return {"category": named, "message_key": f"board.error.{named}"}


def step_error(step: BoardStep) -> dict[str, str] | None:
    """A line's `error` as the board event carries it: only an incorrect line names one."""
    return dict(step.error) if step.error is not None and step.status == "incorrect" else None


def hint_card(rung: int, allowed: int, body: str) -> dict[str, Any]:
    """`show_hint`: accepted only at the rung the engine allows now (TOL-19, PED-03)."""
    if rung != allowed:
        raise HandlerError("policy_denied", f"the next hint allowed is rung {allowed}")
    actions = ["try_it", "next_hint"] if rung < 5 else ["try_it"]
    return {"card_type": "hint", "rung": rung, "of": 5, "body": body[:2000], "actions": actions}


def question_card(prompt: str, options: list[dict[str, Any]], allow_free_text: bool, multi: bool) -> dict[str, Any]:
    """`ask_student`: at most 4 options, the recommended one first (the client labels it), free text when allowed."""
    if len(options) > MAX_OPTIONS:
        raise HandlerError("invalid_args", f"at most {MAX_OPTIONS} options")
    ordered = sorted(options, key=lambda o: not o.get("recommended", False))
    rendered = []
    for n, option in enumerate(ordered):
        # The client labels the recommended option with its own keyed copy (LOC-01); the engine sends the flag.
        entry: dict[str, Any] = {"id": f"opt_{n}", "label": option["label"][:200],
                                 "recommended": bool(option.get("recommended"))}  # fmt: skip
        if option.get("latex"):
            entry["latex"] = option["latex"]
        rendered.append(entry)
    card = {
        "card_type": "question",
        "prompt": prompt[:1000],
        "options": rendered,
        "allow_free_text": allow_free_text,
        "multi": multi,
    }
    if allow_free_text:
        card["placeholder_key"] = "card.something_else"
    return card


def emit_part(events: EventStream, kind: str, payloads: list[dict[str, Any]]) -> str:
    """One part of `kind` carrying the given events (board or card)."""
    part_id = events.start_part(kind)
    for payload in payloads:
        if payload["type"] in ("card.open", "board.reset"):
            payload = {**payload, "part_id": part_id}
        events.emit(payload)
    events.end_part(part_id)
    return part_id


def card_answered(request_id: str, choice: str | None) -> dict[str, Any]:
    """The outcome of a question card: `answered`, or `student_declined` with the action taken (TOL-20)."""
    if choice is None:
        return {"status": "error", "error": {"code": "student_declined", "action_taken": True}}
    return {"status": "ok", "answered": {"choice": choice}, "request_id": request_id}
