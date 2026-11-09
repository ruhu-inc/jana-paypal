"""Side questions (AGT-18, API-26): "what does gradient mean again?" without leaving the problem.

A side question runs as a fork of the Coach: the same prompt the main lesson would build, with the question added at
the end, no tools offered, at most `side_question_max_calls` calls, and the same stop gates (the leak gate included,
so a side question cannot pull out a withheld value). Its answer is one text part in a turn marked `fork: true`,
which the client shows in the side panel. Nothing of the main lesson changes: no transcript or memory write, no
board operation, card or visual, no hint rung, step, attempt or mastery change. The main problem's next prompt is
exactly what it would have been."""

from __future__ import annotations

import asyncio
from typing import Any

from agent_core.engine.roster import speaking
from agent_core.prompts.builder import Message, PromptSession
from agent_core.router import RouterError

SIDE_NOTE = ("side question: answer only this question, briefly, in the side panel; do not move the problem on, "
             "give no hint for the current step and change nothing on the board")  # fmt: skip


async def answer(engine: Any, turn: Any, text: str) -> Any:
    """Run one side question as the forked turn `turn` (already begun with `fork: true`)."""
    from agent_core.engine.turn import _escape

    settings = engine.deps.settings
    with speaking("coach"):
        context = engine._tutor_context(SIDE_NOTE)
        transcript = tuple(engine._sent_transcript(engine.state.transcript))
        block = engine._learner_block()
        session = PromptSession(**engine._prompt_base(), tutor_context=context, transcript=transcript,
                                learner_block=block.text if block else "",
                                learner_dropped=block.dropped if block else ())  # fmt: skip
        build = engine._build("coach", session, [], "agents/tutor-turn")
        question = Message("user", f"<student-message>\n{_escape(text)}\n</student-message>")
        for iteration in range(settings.side_question_max_calls):
            if engine.cancelled.is_set():
                return finish(engine, turn, None, interrupted=True)
            reminders = tuple(Message("user", engine.app_events.wrap(n)) for n in turn.nudges)  # PRM-19
            turn.nudges = []
            offset = turn.trace.offset_ms()
            try:
                result = await engine._call("coach", [*build.messages, question, *reminders], [],
                                            "agents/tutor-turn", turn, cancellable=True)  # fmt: skip
            except asyncio.CancelledError:
                if not engine.cancelled.is_set():
                    raise
                return finish(engine, turn, None, interrupted=True)
            except RouterError:
                turn.status, turn.stop_reason = "error", "provider_error"
                break
            turn.coach_calls += 1
            turn.trace.phase("call", iteration, offset, "continue")
            offset = turn.trace.offset_ms()
            verdict, candidate = engine._gate(turn, result.structured, result.schema_valid, result.errors)
            turn.trace.phase("stop_gates", iteration, offset, verdict)
            if verdict == "continue":
                return finish(engine, turn, candidate)
            if verdict == "break":
                break
        return finish(engine, turn, turn.gates.pending)


def finish(engine: Any, turn: Any, accepted: dict[str, Any] | None, interrupted: bool = False) -> Any:
    """The fork's only output: the accepted answer as one text part, or the withholding-safe canned line. Its board
    operations, card and visual are dropped and nothing is written to the lesson. Before anything is shown the answer
    passes the output critic's safety pass like any tutor turn (PRV-18 layer 2): a risky one becomes its template."""
    if interrupted or engine.cancelled.is_set():
        engine.events.end_open_parts("interrupted")
        turn.status, turn.stop_reason = "interrupted", "student_stop"
        return engine._end(turn, None)
    if accepted is None:
        turn.degraded = True
        turn.degraded_reason = turn.degraded_reason or "side_question_unanswered"
        accepted = engine._canned()
    accepted = {**accepted, "board": [], "card": None, "visual": []}  # what the fork never shows is not checked
    category, accepted = engine._critic(turn, accepted)
    if category is not None:
        return engine._finish_guard(turn, category)
    text = accepted["say"]
    with speaking("coach"):
        part = engine.events.start_part("text")
        engine.events.text(part, text)
        engine.events.end_part(part)
    engine._persist(turn, part, "text", text)  # the part is kept as streamed; the lesson's transcript is not touched
    return engine._end(turn, accepted)
