"""What a session leaves in the learner database: its transcript rows (DAT-06) and its learning events (LRN-01).

The API's projection folds each stream event first; this then writes the rows that event completes, from the folded
state (a part's final content, a step's latest status). A failing write is counted and logged and never breaks the
stream: the session's checkpoint still lets it resume (MEM-02).
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from agent_core.engine.events import new_id
from agent_core.learner.service import Learner, NewEvent
from agent_core.memory.queries import stored_notes
from agent_core.memory.write import Actor, compact_storage, forget
from agent_core.store.transcript import TranscriptStore, bare

log = logging.getLogger(__name__)


def block_text(block: dict[str, Any]) -> str:
    kind = block["type"]
    if kind == "text":
        return str(block["text"])
    if kind in ("latex", "board_line"):
        return str(block["latex"])
    if kind == "ledger_table":
        return str(block["text_fallback"])
    return ""


@dataclass
class _Open:
    """A learning session that has a `session_started` and no `session_ended` yet."""

    meta: dict[str, Any]
    last_activity: int
    active_ms: int = 0
    solved: bool = False  # the problem or item was finished, so a switch away is `completed`


class SessionRecords:
    """Transcript rows and learning events for the core's sessions.

    A learning session (LRN-01) begins with `session_started` and ends with exactly one `session_ended`: when the
    student switches to another session (`completed` if the problem was finished, else `student_left`), when it is
    next touched after the idle timeout (`idle_timeout`, at its last activity), when the app closes with it open
    (`interrupted`), or, if the process died first, at the next start (`crash_recovered`). Active time leaves out
    idle gaps. Touching an idle-ended session begins a new learning session in it."""

    def __init__(
        self,
        transcripts: TranscriptStore,
        learner: Learner | None = None,
        hardware_class: str = "mid",
        idle_timeout_ms: int = 30 * 60_000,
    ) -> None:
        self.transcripts = transcripts
        self.learner = learner
        self.hardware_class = hardware_class
        self.idle_timeout_ms = idle_timeout_ms
        self.card_answers: set[str] = set()  # client message ids of turns that answer a card
        self.voice_turns: set[str] = set()  # accepted voice turns, registered only at first-audio release
        self.failures = 0
        self.open: dict[str, _Open] = {}
        self.current: str | None = None
        # Called after a learning session ends (session id, reason): the session-end write point of MEM-06.
        self.ended: Callable[[str, str], None] | None = None

    # --- learning sessions (LRN-01) -------------------------------------------------------------------------------

    def session_started(self, meta: dict[str, Any], now: int | None = None) -> None:
        """A new session: the transcript row, and a learning session that ends any other open one (a switch)."""
        self.transcripts.start_session(meta, hardware_class=self.hardware_class)
        self._begin(meta, meta["created_ts"] if now is None else now)

    def _begin(self, meta: dict[str, Any], now: int) -> None:
        for other in [s for s in self.open if s != meta["session_id"]]:
            self.end(other, None, now)
        self.open[meta["session_id"]] = _Open(meta, now)
        self.current = meta["session_id"]
        if self.learner is not None:
            sid = bare(meta["session_id"])
            payload = {"session_id": sid, "mode": meta["mode"], "entry": "question",
                       "hardware_class": self.hardware_class, "subject": meta["subject"]}  # fmt: skip
            self.learner.record("session_started", payload, source="engine", session_id=sid, occurred_at=now)

    def touch(self, meta: dict[str, Any], now: int, *, solved: bool = False) -> None:
        """An accepted message, card answer or board input in a session."""
        sid = meta["session_id"]
        state = self.open.get(sid)
        if state is not None and now - state.last_activity > self.idle_timeout_ms:
            self.end(sid, "idle_timeout", state.last_activity)
            state = None
        if state is None:
            self._begin(meta, now)
            return
        for other in [s for s in self.open if s != sid]:  # a switch from another open session ends it
            self.end(other, None, now)
        state.active_ms += now - state.last_activity
        state.last_activity = now
        self.current = sid

    def solved(self, session_id: str) -> None:
        state = self.open.get(session_id)
        if state is not None:
            state.solved = True

    def end(self, session_id: str, reason: str | None, now: int) -> None:
        """End a learning session; reason None means a switch: completed when its problem was finished."""
        state = self.open.pop(session_id, None)
        if state is None:
            return
        if reason is None:
            reason = "completed" if state.solved else "student_left"
        if reason != "idle_timeout":
            state.active_ms += min(max(0, now - state.last_activity), self.idle_timeout_ms)
        ended_at = state.last_activity if reason == "idle_timeout" else now
        self._ended(session_id, reason, state.active_ms, ended_at)

    def _ended(self, session_id: str, reason: str, active_ms: int, ts: int) -> None:
        try:
            self.transcripts.end_session(session_id, reason=reason, ts=ts)
            if self.learner is not None:
                sid = bare(session_id)
                payload = {"session_id": sid, "reason": reason, "active_ms": max(0, active_ms)}
                self.learner.record("session_ended", payload, source="engine", session_id=sid, occurred_at=ts)
        except Exception:
            self.failures += 1
            log.exception("session end write failed")
            return
        if self.ended is not None:
            try:
                self.ended(session_id, reason)
            except Exception:  # the Reflector's queue never breaks a session's end
                log.exception("session end hook failed")

    def shutdown(self, now: int) -> None:
        """The app closes: open sessions end `interrupted`, or `idle_timeout` if they were already idle."""
        for sid in list(self.open):
            state = self.open[sid]
            idle = now - state.last_activity > self.idle_timeout_ms
            self.end(sid, "idle_timeout" if idle else "interrupted", now)

    def recover(self, now: int) -> list[str]:
        """At start: sessions a dead process left without `session_ended` end `crash_recovered`."""
        if self.learner is None:
            return []
        started: dict[str, int] = {}
        for event in self.learner.log.by_seq():
            sid = event.payload.get("session_id") if not event.erased else None
            if event.type == "session_started" and sid:
                started[sid] = event.occurred_at
            elif event.type == "session_ended" and sid:
                started.pop(sid, None)
        recovered = []
        for sid, began in started.items():
            starts = [t for t in self.transcripts.turn_starts(sid) if t >= began]
            active = sum(min(b - a, self.idle_timeout_ms) for a, b in itertools.pairwise([began, *starts]))
            last = starts[-1] if starts else began
            self._ended(f"ses_{sid}", "crash_recovered", active, max(last, began))
            recovered.append(sid)
        return recovered

    # --- learning evidence (G54) and note deletion (LRN-37) -------------------------------------------------------

    def learning(self, events: Sequence[NewEvent]) -> None:
        """Learning events the engine emits during a turn (attempt, verdict, hint, safety_flagged), appended and
        folded in one transaction. A failing write is counted and logged and never breaks the turn."""
        if self.learner is None or not events:
            return
        try:
            self.learner.record_batch(events)
        except Exception:
            self.failures += 1
            log.exception("learning event write failed")

    def safety_flagged(self, category: str, template_id: str, session_id: str | None = None) -> None:
        """The input guard answered something a student wrote outside a tutor turn (a note, an answer, a report): one
        `safety_flagged` event with the category and template only, never the text (PRV-14). The guarded input stands
        in for the turn, so it gets a turn id of its own."""
        payload = {"category": category, "template_id": template_id, "turn_id": new_id("trn")}
        session = bare(session_id) if session_id else None
        self.learning([NewEvent("safety_flagged", payload, source="engine", session_id=session)])

    def forget_note(self, note_id: str, actor: Actor) -> bool:
        """Delete a note and erase its text (LRN-37, MEM-14), then compact storage so no copy is left in the database
        file, its log or the full-text index. False when there is no such note."""
        if self.learner is None or forget(self.learner, note_id, actor) is None:
            return False
        compact_storage(self.learner.conn)
        return True

    def memory_notes(self) -> list[dict[str, Any]]:
        """The student's active notes for `GET /v1/profiles/{id}/memory` (MemoryList), oldest first, with who they came
        from: the student's own request, or the Reflector (LRN-36, MEM-14). Every note is listed, including one the
        render check keeps out of prompts (MEM-27), so it can be deleted."""
        if self.learner is None:
            return []
        notes = sorted(stored_notes(self.learner.conn, ("active",)), key=lambda n: (n.created_at, n.note_id))
        return [{"note_id": f"mem_{n.note_id}", "text": n.text, "created_ts": n.created_at,
                 "source": "student" if n.origin == "student_request" else "reflector"} for n in notes]  # fmt: skip

    def card_answer(self, client_message_id: str) -> None:
        self.card_answers.add(client_message_id)

    def restored(self, session_id: str, ts: int) -> None:
        self.transcripts.interrupt_open(session_id, ts)

    def voice_delivery(self, turn_id: str, delivery: dict[str, Any], ts: int) -> None:
        try:
            self.transcripts.voice_delivery(turn_id, delivery, ts)
        except Exception:
            self.failures += 1
            log.exception("voice delivery write failed")

    def on_event(self, session_id: str, envelope: dict[str, Any], state: dict[str, Any]) -> None:
        try:
            self._write(session_id, envelope, state)
        except Exception:
            self.failures += 1
            log.exception("transcript write failed")

    def _write(self, session_id: str, envelope: dict[str, Any], state: dict[str, Any]) -> None:
        event, turn_id, ts = envelope["event"], envelope["turn_id"], envelope["ts"]
        kind = event["type"]
        store = self.transcripts
        if kind == "turn.start":
            message = next((m for m in state["messages"] if m["role"] == "student"
                            and m.get("client_message_id") == event["client_message_id"]), None)  # fmt: skip
            text = "\n".join(block_text(b) for b in message["content"]).strip() if message else None
            card = event["client_message_id"] in self.card_answers
            voice = event["turn_id"] in self.voice_turns
            self.voice_turns.discard(event["turn_id"])
            store.start_turn(session_id, turn_id=event["turn_id"], message_id=event["user_message_id"],
                             student_text=text, input_kind="voice" if voice else "card_answer" if card else "typed",
                             ts=ts)  # fmt: skip
        elif kind == "part.end" and turn_id is not None:
            part = state["parts"].get(event["part_id"])
            if part is not None:
                store.put_part(turn_id, self._content(part, state), ts)
        elif kind == "notice" and turn_id is not None:
            part = state["parts"].get(event["notice_id"])
            if part is not None:
                notices = sum(1 for p in state["parts"].values() if p["kind"] == "notice" and p["turn_id"] == turn_id)
                store.put_part(turn_id, part, ts, idx=-notices)
        elif kind in ("card.answered", "card.cancel"):
            part = next((p for p in state["parts"].values()
                         if p.get("card", {}).get("request_id") == event["request_id"]), None)  # fmt: skip
            if part is not None and part.get("turn_id") and part["status"] not in ("started", "streaming"):
                store.put_part(part["turn_id"], self._content(part, state), ts)
        elif kind == "board.step":
            board = state["boards"][event["board_id"]]
            store.put_step(session_id, event["board_id"], board["part_id"], board["steps"][event["step_id"]], ts)
        elif kind == "board.reset":
            store.reset_board(event["board_id"], ts)
        elif kind == "session.updated":
            store.update_session(session_id, event)
        elif kind == "turn.end" and turn_id is not None:
            store.end_turn(turn_id, status=event["status"], usage=event["usage"], ts=ts)

        if kind.startswith("board.") or kind == "visual.await":
            board = state["boards"].get(event.get("board_id"))
            part = state["parts"].get(board["part_id"]) if board else None
            if part is not None and part.get("turn_id") and part["status"] not in ("started", "streaming"):
                store.put_part(part["turn_id"], self._content(part, state), ts)
        if kind in ("visual.step", "visual.focus"):
            visual = state["visuals"].get(event["visual_id"])
            part = state["parts"].get(visual["part_id"]) if visual else None
            if part is not None and part.get("turn_id") and part["status"] not in ("started", "streaming"):
                store.put_part(part["turn_id"], self._content(part, state), ts)

    @staticmethod
    def _content(part: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        content = dict(part)
        visual_id = part.get("visual_id")
        if visual_id is not None and visual_id in state["visuals"]:
            content["visual"] = state["visuals"][visual_id]
        boards = [b for b in state["boards"].values() if b["part_id"] == part["part_id"]]
        if boards:
            content["boards"] = boards
        return content
