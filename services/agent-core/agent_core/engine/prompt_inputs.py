"""What the engine puts in front of the Coach besides the stable tiers (PRM-15 to PRM-20, SEC-03, PRV-16):

- intake: each untrusted input is scanned once when it arrives; a hit is recorded by pattern id and source in the
  turn trace (`scan_hits`, OBS-06) and as an `injection_flagged` learner event, and the content is still treated as
  the question (a student message is kept whole; OCR text and file text lose the matched spans);
- the per-turn extras of the `<tutor-context>` block: the matched misconception with its fix, and the capability-off
  notices, with any line that would carry a withheld value left out;
- the transcript as sent: every untrusted message escaped, wrapped in its typed tag and cut to the input cap, and the
  session's one-shot reminders placed where they were sent, inside the trusted wrapper.

Everything here is built from engine state, never from model text."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_core.config import Settings
from agent_core.engine.gates import leak_findings
from agent_core.engine.input import normalise_input
from agent_core.engine.state import SessionState, numbers_in
from agent_core.prompts.builder import Message
from agent_core.prompts.reminders import ReminderLedger
from agent_core.prompts.untrusted import PHOTO, STUDENT, UPLOAD, AppEvents, input_cap, screened, truncate, wrap

EVENT_SOURCE = {"student_text": "text", "ocr_text": "photo", "upload": "text", "pack_text": "pack"}
WRAPPER_FOR = {"ocr_text": PHOTO, "upload": UPLOAD}
MAX_SCAN_HITS = 32  # the trace contract's limit


def intake(trace: Any, learn: Any, text: str, source: str = "student_text") -> str:
    """Scan one untrusted input on arrival (PRM-18). Returns the text to keep. `trace` is the turn's TraceBuilder,
    `learn` takes a list of (type, payload, source) learner events."""
    kept, hits = screened(normalise_input(text), source)
    if not hits:
        return kept
    recorded = getattr(trace, "scan_hits", None)
    patterns = list(dict.fromkeys(h.pattern_id for h in hits))
    if isinstance(recorded, list):
        for pattern in patterns:
            if len(recorded) < MAX_SCAN_HITS:
                recorded.append({"pattern_id": pattern, "source": source})
    learn([("injection_flagged", {"source": EVENT_SOURCE.get(source, "text"), "pattern_id": p}, "engine")
           for p in patterns])  # fmt: skip
    return kept


def input_chars(settings: Settings, context_tokens: int) -> int:
    return input_cap(context_tokens, settings.untrusted_input_context_share, settings.untrusted_input_max_chars)


def sent_message(stored: dict[str, Any], settings: Settings, cap: int) -> Message:
    """A stored transcript entry as sent (PRM-19, PRM-20): student, photo and file text escaped, wrapped in its typed
    tag and cut to the cap; the full text stays in the stored transcript for `session_recall` (MEM-18)."""
    if stored["role"] != "user" or stored.get("kind") == "compaction":
        return Message(stored["role"], stored["content"])
    text, _ = truncate(str(stored["content"]), cap, settings.untrusted_head_share, settings.untrusted_tail_share)
    kind = WRAPPER_FOR.get(str(stored.get("source", "")), STUDENT)
    return Message("user", wrap(text, kind, stored.get("name")))


def with_reminders(
    messages: list[Message], ledger: ReminderLedger, events: AppEvents, offset: int = 0
) -> list[Message]:
    """The transcript with each sent reminder at the place it was sent, in the trusted wrapper (PRM-16). `offset` is
    where `messages` starts in the session transcript, when the window begins later (the current problem, D131)."""
    placed = [(position - offset, text) for position, text in ledger.placed(len(messages) + offset)
              if position >= offset]  # fmt: skip
    if not placed:
        return messages
    out: list[Message] = []
    for index in range(len(messages) + 1):
        out += [Message("user", events.wrap(text)) for position, text in placed if position == index]
        if index < len(messages):
            out.append(messages[index])
    return out


def offer_state_reminders(ledger: ReminderLedger, state: SessionState, position: int, notices: list[str]) -> None:
    """The state keys the engine watches each turn: mode, rung and the capability notices in effect."""
    ledger.offer("mode.changed", state.mode, position)
    ledger.offer("rung.changed", str(state.rung), position)
    ledger.offer("capability.changed", "; ".join(notices) or "all on", position)


def misconception_lines(state: SessionState, fix_of: Callable[[str], str] | None = None) -> list[str]:
    """PRM-15: on a practice item, the option the student chose (never the key: the Coach does not see the other
    options) and the misconception the marker matched on that answer, with its fix as the item's author wrote it: the
    chosen option's feedback, or for a match no option carries (a numeric or structural signature, CUR-18) the
    misconception's own fix from the pack (`fix_of`)."""
    practice = state.practice
    if not practice or not practice.get("verdicts"):
        return []
    item = practice["item"]
    part = next((p for p in item.get("parts", []) if p.get("id") == practice.get("part")), None)
    options = ((part or {}).get("answer") or {}).get("options") or []
    lines = []
    said = state.student_lines[-1].strip() if state.student_lines else ""
    chosen = next((o for o in options if str(o.get("id")) == said and not o.get("correct")), None)
    if chosen is not None:
        lines.append(f"last answer: option {chosen['id']}, \"{str(chosen.get('text', ''))[:120]}\", "
                     f"marked {practice['verdicts'][-1]}")  # fmt: skip
    misconception = practice.get("misconception_id")
    if misconception:
        fix = next((str(o.get("feedback") or "")[:240] for o in options if o.get("misconception_id") == misconception),
                   "")  # fmt: skip
        if not fix and fix_of is not None:
            fix = fix_of(str(misconception))[:240]
        lines.append(f"matched misconception [1] {misconception}" + (f"; its fix: {fix}" if fix else ""))
    return lines


def without_withheld(lines: list[str], state: SessionState, settings: Settings) -> list[str]:
    """PRM-15: no line of the block carries a withheld value (a fix text or a board line that would)."""
    withheld = state.withheld()
    if not withheld:
        return lines
    givens = set(numbers_in(state.problem.statement)) if state.problem else set()
    for line in state.student_lines:
        givens.update(numbers_in(line))
    return [ln for ln in lines if not leak_findings({"line": ln}, withheld, settings, None, givens)]
