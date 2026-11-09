"""Board annotations anchored to ids (VSL-40, D103) and the term split they address (API-12).

A board line's terms are its top-level terms: the LaTeX split at `+`, `-` and `=` (and the relations) outside any
group, a sign belonging to the term it starts; term ids run t1, t2, ... across the whole line. Annotations, pointing
back and term carry address the same ids (VSL-19, UXS-58, UXS-61).

`resolve` turns the Coach's `annotate` op into a `board.annotate` event or a reason to drop it. Targets are ids only:
a board step with optionally a term or term range of it, or a part of a visual on screen. An unknown or off-screen
target, an arrow without an end, a strike on the student's writing, a "look here" on a tutor line, or a margin note
over 40 characters or with an unverified number is dropped and logged, never placed by position."""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Any

from agent_core.engine.events import new_id
from agent_core.engine.gates import untraced_numbers
from agent_core.engine.state import SessionState

MAX_TERMS = 40
NOTE_LIMIT = 40
_RELATIONS = ("=", "<", ">", "\\le", "\\ge", "\\leq", "\\geq", "\\neq", "\\approx")
_RANGE = re.compile(r"^t([0-9]{1,2})(?:-t([0-9]{1,2}))?$")


def split_terms(latex: str) -> list[dict[str, Any]]:
    """The top-level terms of a line as {term_id, start, end} character ranges (end exclusive), at most 40."""
    spans: list[tuple[int, int]] = []
    depth, start, i = 0, None, 0
    previous = ""  # the last significant character outside groups, to tell a binary sign from a leading one

    def close(at: int) -> None:
        nonlocal start
        if start is not None:
            end = at
            while end > start and latex[end - 1] == " ":
                end -= 1
            if end > start:
                spans.append((start, end))
        start = None

    while i < len(latex):
        ch = latex[i]
        if ch in "{([":
            depth += 1
        elif ch in "})]":
            depth = max(0, depth - 1)
        relation = next((r for r in _RELATIONS if depth == 0 and latex.startswith(r, i)
                         and (r[0] != "\\" or not latex[i + len(r):i + len(r) + 1].isalpha())), None)  # fmt: skip
        if relation is not None:
            close(i)
            i += len(relation)
            previous = "="
            continue
        if depth == 0 and ch in "+-" and previous not in ("", "=", "+", "-", "^", "_"):
            close(i)
            start = i if ch == "-" else None
            previous = ch
            i += 1
            continue
        if ch != " ":
            if start is None:
                start = i
            if depth == 0 or ch in "})]":
                previous = ch
        i += 1
    close(len(latex))
    return [{"term_id": f"t{n}", "start": a, "end": b} for n, (a, b) in enumerate(spans[:MAX_TERMS], 1)]


def _terms_valid(terms: str, count: int) -> bool:
    match = _RANGE.match(terms)
    if not match:
        return False
    first = int(match[1])
    last = int(match[2]) if match[2] else first
    return 1 <= first <= last <= count


def _part(state: SessionState, part_id: str) -> str | None:
    """The visual on screen holding the part, or None when no visual or more than one holds it (never guessed)."""
    holders = [v["visual_id"] for v in state.shown_visuals if part_id in v.get("parts", [])]
    return holders[0] if len(holders) == 1 else None


def resolve(op: dict[str, Any], state: SessionState, context_numbers: set[Fraction]) -> tuple[dict[str, Any] | None,
                                                                                               str | None]:  # fmt: skip
    """The `board.annotate` event for an `annotate` op, or (None, reason) when it must be dropped."""
    board, mark = state.board, op["mark"]
    terms = (op.get("terms") or [None])[0]
    target: dict[str, Any] = {"step_id": None, "terms": None, "visual_id": None, "part_id": None}
    step = None
    if op.get("part"):
        visual_id = _part(state, op["part"])
        if visual_id is None:
            return None, "part_not_on_screen"
        target.update(visual_id=visual_id, part_id=op["part"])
    elif op.get("step") is not None:
        if not 1 <= op["step"] <= len(board.steps):
            return None, "step_unknown"
        step = board.steps[op["step"] - 1]
        target["step_id"] = step.step_id
        if terms:
            if not _terms_valid(terms, len(split_terms(step.latex))):
                return None, "terms_unknown"
            target["terms"] = terms
    else:
        return None, "no_target"
    to = None
    if mark == "arrow":
        end = op.get("to")
        if not end:
            return None, "arrow_without_end"
        if _RANGE.match(end) and step is not None:
            if not _terms_valid(end, len(split_terms(step.latex))):
                return None, "terms_unknown"
            to = {"step_id": step.step_id, "terms": end, "visual_id": None, "part_id": None}
        else:
            visual_id = _part(state, end)
            if visual_id is None:
                return None, "part_not_on_screen"
            to = {"step_id": None, "terms": None, "visual_id": visual_id, "part_id": end}
    elif op.get("to"):
        return None, "end_only_for_arrow"
    if mark == "strike" and (step is None or step.owner != "tutor"):
        return None, "strike_not_on_tutor_line"  # never through the student's writing
    if mark == "look_here" and (step is None or step.owner != "student"):
        return None, "look_here_not_on_student_line"
    if mark == "margin_note" and (step is None or terms):
        return None, "margin_note_needs_a_line"
    note = None
    if mark == "margin_note":
        note = (op.get("reason") or "").strip()
        if not note or len(note) > NOTE_LIMIT:
            return None, "note_length"
        if untraced_numbers({"note": note}, state, context_numbers):
            return None, "note_number_unverified"  # the number rule (VSL-12)
    event = {"type": "board.annotate", "board_id": board.board_id, "annotation_id": new_id("ann"), "mark": mark,
             "target": target, "to": to, "note": note}  # fmt: skip
    return event, None


def term_moves(prev_step_id: str, prev_latex: str, latex: str, found: dict[str, Any] | None) -> list[dict[str, Any]]:
    """`term_moves` for a new line (UXS-61): the step checker's moves between the line it was checked against and
    this one, as term ids. Empty unless the checker's term split agrees with the board's for both lines, so a move is
    never placed on a term it did not compare; the client never infers one."""
    if not found:
        return []
    counts = [len(split_terms(prev_latex)), len(split_terms(latex))]
    if counts != list(found.get("terms") or []):
        return []
    return [{"from": f"{prev_step_id}.t{m['from']}", "to": f"t{m['to']}" if m["to"] else None, "kind": m["kind"]}
            for m in found["moves"][:8]]  # fmt: skip
