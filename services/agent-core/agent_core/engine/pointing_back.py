"""Pointing back at a term or a part (UXS-58, D103).

The student selects a term or term range of a board line (`stp_….t2-t4`, the step's `terms` split, API-12) or a part
of a visual on screen (`<visual_id>:<part_id>`, VSL-19). "Why this?" names it as the `why` turn's ref, and while
something is selected "this" in a message refers to it. The engine resolves the ref to the target's id and content
for the Coach, never coordinates; a ref that names nothing on the board or on screen is refused."""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent_core.engine.annotations import split_terms
from agent_core.engine.state import SessionState

_STEP = re.compile(r"^(stp_[0-9A-HJKMNP-TV-Z]{26})(?:\.(t[0-9]{1,2}(?:-t[0-9]{1,2})?))?$")
_PART = re.compile(r"^(vis_[0-9A-HJKMNP-TV-Z]{26}):([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)$")
# "this", "this one", "that one", "here": the student points with the selection.
DEICTIC = re.compile(r"\b(this|that)( one)?\b|\bhere\b", re.IGNORECASE)


@dataclass(frozen=True)
class Target:
    ref: str
    step_id: str | None = None
    number: int | None = None  # the board line's number, 1-based
    terms: str | None = None
    content: str | None = None  # the selected terms' LaTeX, or the whole line
    visual_id: str | None = None
    part_id: str | None = None

    def describe(self) -> str:
        """The target for the Coach, by id and content (never coordinates)."""
        if self.part_id is not None:
            return f"part {self.part_id} of the visual on screen ({self.ref})"
        where = f"board {self.number}"
        if self.terms is not None:
            return f"the terms {self.content!r} ({self.terms}) of {where} ({self.ref})"
        return f"{where}, {self.content!r} ({self.ref})"


def resolve_ref(state: SessionState, ref: str) -> Target | None:
    """The line, term range or visual part a ref names, or None when it names nothing on the board or on screen."""
    step = _STEP.match(ref)
    if step is not None:
        found = next(((n, s) for n, s in enumerate(state.board.steps, 1) if s.step_id == step[1]), None)
        if found is None or not found[1].latex.strip():
            return None
        number, line = found
        if step[2] is None:
            return Target(ref, line.step_id, number, None, line.latex)
        spans = {t["term_id"]: t for t in split_terms(line.latex)}
        first, _, last = step[2].partition("-")
        last = last or first
        if first not in spans or last not in spans or spans[first]["start"] > spans[last]["start"]:
            return None
        return Target(ref, line.step_id, number, step[2], line.latex[spans[first]["start"] : spans[last]["end"]])
    part = _PART.match(ref)
    if part is not None:
        shown = next((v for v in state.shown_visuals if v["visual_id"] == part[1]), None)
        if shown is None or part[2] not in shown.get("parts", []):
            return None
        return Target(ref, visual_id=part[1], part_id=part[2])
    return None
