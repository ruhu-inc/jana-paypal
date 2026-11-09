"""The lesson flow on a session (AGT-22, TOL-22): a session opened with `origin.kind: lesson` plays the lesson's pack
blocks in order through the engine.

"Next" (`next_step`) opens the lesson at its saved block, or moves to the next one; the block index is saved at every
move (MEM-02), so a lesson resumes where it stopped. Each move rewrites the session plan with `lesson_plan_write`,
called by engine code only (TOL-22), and shows it as the `plan` card. The Coach phrases and paces a text block (the
idea, the worked example, the common mistakes, a visual's caption, the summary); a check block (prior knowledge, a
guided step, a quick check, the exit check, and a step-shape lesson's step and check, D110) opens its item as the
open problem, so the deterministic marker marks the answer (the Checker's part, ASM-02) and the leak gate guards its
key (AGT-09). A question the student asks mid-lesson is answered as a side question (AGT-18); a new problem is kept
to try after the lesson.

Everything a block says is the pack's reviewed text: its numbers are the block's own, so they trace to it like a
question's numbers do (AGT-10). No model chooses a block, its order or when the lesson moves on (AGT-03)."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from agent_core.engine import figures, turn_scope
from agent_core.engine.events import new_id
from agent_core.engine.input import normalise_input
from agent_core.engine.state import Problem
from agent_core.engine.tutor_flows import LessonFlow
from agent_core.engine.ui import emit_part

if TYPE_CHECKING:
    from agent_core.engine.turn import Engine, TurnIds, TurnResult

# A step-shape lesson's step and check (TABLE CUR-11b, D110) ask an item, as the script's check blocks do.
CHECK_BLOCKS = ("prior_knowledge", "guided_step", "quick_check", "exit_check", "step", "check")
BLOCK_TEXT_MAX = 1000  # characters of a block's text in the Coach's note (tier 8 has its own budget, PRM-09)


class LessonDesk:
    """The lesson player's reads for the engine, each on the learner database as it is open now (`AppRuntime.learn`):
    the lesson view, the saved position and a check block's pack item."""

    def __init__(self, learn: Callable[[], contextlib.AbstractContextManager[Any]]) -> None:
        self.learn = learn

    def lesson(self, lesson_id: str) -> dict[str, Any] | None:
        with self.learn() as desk:
            return desk.lesson(lesson_id) if desk is not None else None

    def save_position(self, lesson_id: str, block_index: int, version: str, now_ms: int) -> dict[str, Any] | None:
        with self.learn() as desk:
            return desk.save_position(lesson_id, block_index, version, now_ms) if desk is not None else None

    def item(self, item_id: str) -> dict[str, Any] | None:
        with self.learn() as desk:
            record = desk.content.record("items", item_id) if desk is not None else None
        return dict(record) if record is not None else None


def block_text(block: dict[str, Any]) -> str:
    """What a text block says, from the pack: the Coach's material and the provenance of its numbers."""
    kind = block["type"]
    if kind == "worked_example":
        example = block["worked_example"]
        shown = set(example.get("faded_steps") or [])  # a faded step is the student's to fill (LRN-17)
        steps = [f"{s['latex']} ({s['reason']})" for s in example["steps"] if s["n"] not in shown]
        return "\n".join([example["problem"], *steps])
    if kind == "common_mistakes":
        mistakes = [f"{m['name']}: {m['fix']}" for m in block.get("mistakes") or []]
        return "\n".join([block.get("text") or "", *mistakes]).strip()
    if kind == "visual":
        return str(block.get("caption") or "")
    if kind == "step":  # the lead-in and the question; the why stays with the check reply (D110)
        stems = [str(i.get("stem") or "") for i in block.get("items") or []]
        return "\n".join(t for t in [str(block.get("text") or ""), *stems] if t)
    return str(block.get("text") or "")


async def next_block(engine: Engine, ids: TurnIds | None = None) -> TurnResult:
    """`next_step` in a lesson session: open the lesson at its saved block, or move on one block."""
    turn = engine._begin("ask", ids)
    with turn_scope(turn.turn_id):
        try:
            flow = engine.lesson
            if flow is None:
                if engine.lesson_desk is None or engine.state.lesson_id is None:
                    return engine._finish_text(turn, engine.receipt_texts.cards["lesson_missing"], None, "completed")
                flow = LessonFlow(engine.lesson_desk, engine.state.lesson_id, engine.events.clock_ms)
                try:
                    block: dict[str, Any] | None = flow.open()  # at the saved block (MEM-02)
                except LookupError:
                    return engine._finish_text(turn, engine.receipt_texts.cards["lesson_missing"], None, "completed")
                engine.lesson = flow
            else:
                block = flow.advance()
            if block is None:  # past the last block: the lesson is done
                return await _wrap_up(engine, turn, flow)
            _plan_card(engine, turn, flow)
            return await _present(engine, turn, flow, block)
        except Exception:
            engine._finish_crashed(turn)
            raise


def _plan_card(engine: Engine, turn: Any, flow: LessonFlow) -> None:
    """The session plan as `lesson_plan_write` left it, shown as the `plan` card (TOL-22, API-41)."""
    body = flow.plan.card(str(flow.view.get("title") or ""))
    part = emit_part(engine.events, "card", [{"type": "card.open", "request_id": new_id("req"), "part_id": "",
                                              "call_id": None, "card": body}])  # fmt: skip
    engine._persist(turn, part, "card", body)


async def _present(engine: Engine, turn: Any, flow: LessonFlow, block: dict[str, Any]) -> TurnResult:
    kind, index, total = block["type"], flow.index, len(flow.view["blocks"])
    where = f"lesson block {index + 1} of {total} ({kind.replace('_', ' ')})"
    if kind in CHECK_BLOCKS:
        item = next((engine.lesson_desk.item(i["item_id"]) for i in block.get("items") or []), None)
        if item is not None and item.get("parts"):
            note = f"{where}: ask this check question as it is and wait for the answer; never give its answer"
            figure = block if isinstance(block.get("visual"), dict) and block["visual"].get("resolved") else None
            return await engine._start_practice(turn, item, str(item["parts"][0]["id"]), diagnose=False, note=note,
                                                figure=figure)  # fmt: skip
    text = block_text(block)
    state = engine.state
    engine._new_problem(turn)
    state.practice, state.plan, state.solved, state.step, state.rung = None, None, False, 1, 0
    # The block's own words are the material: its numbers trace to it, as a question's do (AGT-10).
    state.problem = Problem(f"lesson:{flow.lesson_id}"[:64], text, f"{flow.lesson_id}#block{index}")
    note = (f"{where}: present this in your own words, briefly, then ask one short question to check the student "
            f"is following: {text[:BLOCK_TEXT_MAX]}")  # fmt: skip
    shown = figures.show(engine, turn, block)  # an idea's or a visual block's checked figure (a sim asks first)
    if shown:
        note = f"{note}; {shown}"
    return await engine._coach_turn(turn, intent_note=note)


async def _wrap_up(engine: Engine, turn: Any, flow: LessonFlow) -> TurnResult:
    """The lesson is finished: the Coach closes it, and offers the problems kept for after it."""
    later = engine.state.after_lesson
    note = "the lesson is finished: say what the student can now do in one sentence"
    if later:
        note += "; offer to try the question they asked during the lesson now"
    engine.state.problem, engine.state.plan, engine.state.practice = None, None, None
    return await engine._coach_turn(turn, intent_note=note)


async def question(engine: Engine, text: str, ids: TurnIds | None = None) -> TurnResult:
    """A question mid-lesson (AGT-22): a new problem is kept to try after the lesson, with the engine's line; anything
    else is answered as a side question, and the lesson stays where it was (AGT-18)."""
    if LessonFlow.route_question(text) == "side_question":
        return await engine.side_question(text, ids)
    turn = engine._begin("ask", ids, fork=True)
    with turn_scope(turn.turn_id):
        try:
            stopped, text = engine._screen(turn, normalise_input(text), model=await engine._guard_model(text))
            if stopped is not None:
                return stopped
            engine.state.after_lesson.append(text[:2000])
            return engine._finish_text(turn, engine.receipt_texts.cards["lesson_after"], None, "completed")
        except Exception:
            engine._finish_crashed(turn)
            raise
