"""Isolated tutor evaluation and explicit speech delivery commit (VOC-33 to VOC-36).

A fork has its own state, event stream, tool cache and spill overlay. External writes are deferred. The existing
engine still performs every guard, tool round and acceptance gate; this layer only controls when its effects land.
"""

from __future__ import annotations

import asyncio
import copy
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from agent_core.engine import pointing
from agent_core.engine.events import EventStream, new_id
from agent_core.engine.pointing import Cue, Targets, first_unheard, resolve, units
from agent_core.engine.state import SessionState, state_to_json
from agent_core.engine.turn import Engine, TurnIds, TurnResult
from agent_core.engine.verbaliser import verbalise
from agent_core.tools.spill import SpillStore

# Learning evidence about tutor output, not the student's own work: it counts only once the content it refers to was
# heard (VOC-36, VOC-52). The student's attempts, verdicts and safety records count on release.
TUTOR_OUTPUT_SIGNALS = frozenset({"bottom_out", "solution_view"})
# The engine state a held hint changed, restored when its content was never heard (PED-03, PED-07, VOC-36).
HINT_STATE = ("rung", "hint_step", "hint_at_ms", "bottom_outs", "scheduled")


def _tutor_output(item: Any) -> bool:
    kind = getattr(item, "type", None)
    payload = getattr(item, "payload", None) or {}
    return kind == "hint" or (kind == "integrity_signal" and payload.get("kind") in TUTOR_OUTPUT_SIGNALS)


class VoiceTurnConflict(ValueError):
    pass


def _calm(event: dict[str, Any]) -> bool:
    return event.get("type") == "notice" and event.get("code") == "calm_screen"


class _SpillOverlay(SpillStore):
    """Speculative tool output never reaches disk. Retain the overlay on the committed engine."""

    def __init__(self, parent: SpillStore) -> None:
        super().__init__(parent.root)
        self.parent = parent
        self.values: dict[tuple[str, str], str] = {}

    def save(self, session_id: str, text: str) -> str:
        ref = "spill_" + secrets.token_hex(12)
        self.values[session_id, ref] = text
        return ref

    def resolve(self, session_id: str, ref: str) -> str | None:
        return self.values.get((session_id, ref)) or self.parent.resolve(session_id, ref)

    def delete_session(self, session_id: str) -> None:
        self.values = {key: text for key, text in self.values.items() if key[0] != session_id}

    def publish(self) -> SpillStore:
        try:
            for (session_id, ref), text in self.values.items():
                directory = self.parent.root / session_id
                directory.mkdir(parents=True, exist_ok=True)
                (directory / ref).write_text(text, encoding="utf-8")
        except OSError:
            return self  # a full disk keeps the accepted results in memory instead of breaking delivery
        self.values.clear()
        return self.parent


class ProvisionalVoiceTurn:
    def __init__(
        self,
        live: Engine,
        factory: Callable[[SessionState], Engine],
        text: str,
        *,
        ids: TurnIds | None = None,
        before: dict[str, Any] | None = None,
        engine: Engine | None = None,
    ) -> None:
        """`before` and `engine` come from `assemble`: the snapshot taken, and the fork's engine built from it."""
        self.live = live
        self.before = before if before is not None else state_to_json(live.state)
        self.text = text
        self.ids = ids or TurnIds(client_message_id=new_id("cli"))
        self.engine = engine if engine is not None else factory(copy.deepcopy(live.state))
        self.engine.voice_mode = True
        self.engine.profile_ref = live.profile_ref
        for name in (
            "turn_number",
            "persisted",
            "traces",
            "turn_log",
            "receipts",
            "reminders",
            "compactor",
            "diagnosis",
            "titled",
            "open_cards",
            "topic_choices",
            "card_receipts",
            "_egress_cards",
        ):
            setattr(self.engine, name, copy.deepcopy(getattr(live, name)))
        self.engine.voice_interruption = live.voice_interruption
        self.engine.guard, self.engine.critic = copy.copy(live.guard), copy.copy(live.critic)
        self.engine.start_rung, self.engine.rule_signals = live.start_rung, live.rule_signals
        if live.memory is not None and self.engine.memory is not None:
            for name in ("frozen", "refs", "progress_seen", "topic", "_blocks", "_topic_kcs"):
                setattr(self.engine.memory, name, copy.deepcopy(getattr(live.memory, name)))
        self.engine.deps.executor.dedupe = copy.deepcopy(live.deps.executor.dedupe)
        self.engine.deps.executor.spill = _SpillOverlay(live.deps.executor.spill)
        self.events = EventStream(live.state.session_id, live.events.epoch, clock_ms=live.events.clock_ms)
        self.events.part_guard = self.engine.audience
        self.engine.events = self.events
        self.learning: list[list[Any]] = []
        self.deferred: list[Callable[[], Any]] = []
        self.traces: list[dict[str, Any]] = []
        self.engine.deps = replace(self.engine.deps, trace_sink=self.traces.append)
        self.engine.learning = lambda items: self.learning.append(copy.deepcopy(items))
        self.engine.on_memory_request = lambda turn, words: self.deferred.append(
            lambda: live.on_memory_request(turn, words) if live.on_memory_request is not None else None
        )
        saved = self.engine.save_question
        if saved is not None:

            def defer_saved(state: SessionState, reason: str) -> None:
                snapshot = copy.deepcopy(state)
                self.deferred.append(lambda: saved(snapshot, reason))

            self.engine.save_question = defer_saved
        notice = self.engine.record_cloud_notice
        if notice is not None:
            self.engine.record_cloud_notice = lambda accepted: self.deferred.append(lambda: notice(accepted))
        voice_notice = self.engine.record_voice_notice
        if voice_notice is not None:
            self.engine.record_voice_notice = lambda accepted: self.deferred.append(lambda: voice_notice(accepted))
        self.engine.logger, self.engine.worker_logger = None, None
        self.task: asyncio.Task[TurnResult] | None = None
        self.result: TurnResult | None = None
        self.spoken_text = ""
        self.cues: tuple[Cue, ...] = ()  # the accepted spoken form's cues (VOC-10), offsets into spoken_text
        self.cue_misses: tuple[str, ...] = ()  # reason codes of markers that resolved to nothing (VSL-45)
        self.held: list[Any] = []  # tutor-output evidence waiting for its content to be heard (VOC-52)
        self.counted: bool | None = None  # whether the held evidence was written (None until finish)
        self.released = False
        self.finished = False
        self.safety_preserved = False  # a distress flag or calm screen was delivered ahead of release (VOC-20)
        self._terminal: dict[str, Any] | None = None
        self._stream: EventStream | None = None
        self.delivery: dict[str, Any] | None = None
        self.started = time.monotonic()
        self.latency: dict[str, Any] = {}  # phase timings only, never text (VOC-22)
        self.talk_mode = "push_to_talk"
        self.path = "local_sidecar"  # or "cloud_voice" (VOC-22, VOC-28)
        self.carried_forward = False
        self.fallbacks: tuple[str, ...] = ()
        self._salvaged_calm = False

    @classmethod
    async def assemble(
        cls, live: Engine, factory: Callable[[SessionState], Engine], text: str, ids: TurnIds
    ) -> ProvisionalVoiceTurn:
        """The fork with its engine assembled off the event loop (its files and stores take most of a second), so
        the turn's filler clock keeps running meanwhile (VOC-12). The snapshot is taken on the loop, before."""
        started = time.monotonic()
        before, state = state_to_json(live.state), copy.deepcopy(live.state)
        engine = await asyncio.to_thread(factory, state)
        turn = cls(live, factory, text, ids=ids, before=before, engine=engine)
        turn.started = started
        return turn

    async def evaluate(self) -> TurnResult:
        if self.task is not None or self.finished:
            raise VoiceTurnConflict("voice_turn_already_started")
        new_problem = self.engine.state.problem is None or self.engine.state.solved
        work = (
            self.engine.start_problem(self.text, ids=self.ids)
            if new_problem
            else self.engine.student_turn(self.text, ids=self.ids)
        )
        self.task = asyncio.create_task(work)
        self.result = await self.task
        self.events.flush()
        if self.finished:
            raise VoiceTurnConflict("voice_turn_discarded")
        if self.result.status not in ("complete", "truncated"):
            raise VoiceTurnConflict("voice_turn_not_accepted")
        accepted = self.result.tutor_turn
        # Model speech is used only from the final accepted candidate; templates are engine-owned text.
        text = (
            (accepted.get("say_spoken") or accepted["say"])
            if accepted is not None
            else next(
                (
                    row.get("spoken_form") or row["content"]  # a template's keyed spoken form (PED-26)
                    for row in reversed(self.engine.state.transcript)
                    if row.get("role") == "assistant"
                ),
                "",
            )
        )
        if accepted is not None and accepted.get("say_spoken"):
            # The accepted spoken form with its cue markers, against the accepted board and the visuals on screen
            # (VOC-10, VSL-45). Markers never reach the sidecar: only `spoken.text` does.
            before = {step["step_id"] for step in self.before["board"]["steps"]}
            new_steps = frozenset(
                s.step_id for s in self.engine.state.board.steps if s.step_id not in before and s.owner == "tutor"
            )
            spoken = resolve(str(accepted["say_spoken"]), Targets.of(self.engine.state, new_steps))
            self.spoken_text, self.fallbacks = spoken.text, spoken.fallbacks
            self.cues, self.cue_misses = spoken.cues, spoken.misses
        else:
            speech = verbalise(text)
            self.spoken_text = speech.text
            self.fallbacks = speech.fallbacks
        self.latency["adapter_first_sentence_ms"] = self._offset()
        for row in self.engine.state.transcript[len(self.before["transcript"]) :]:
            if row.get("role") == "user":
                row["input_kind"] = "voice"
        if not self.spoken_text:
            raise VoiceTurnConflict("voice_turn_has_no_speech")
        return self.result

    @property
    def distress(self) -> bool:
        """The input guard showed the distress template in this turn (PRV-14): voice holds idle afterwards (VOC-20)."""
        return any(_calm(frame["event"]) for frame in self.events.frames) or self._salvaged_calm

    def salvage_safety(self) -> bool:
        """Deliver the safety record and the calm-screen notice of a turn not (yet) released (VOC-20, PRV-14).

        A discard drops every speculative effect (VOC-35) except these: a student who disclosed distress must see the
        calm screen and the flag must be recorded, whatever happens to the voice turn afterwards (voice off, socket
        loss, session switch, profile close, expiry), independently of whether the speech is ever played. Each effect
        is removed from the fork once delivered, so a later release or discard never repeats it (exactly once).
        Synchronous, so profile close can run it before the learner database closes; the owner calls it only while the
        owning profile's session is still loaded. Returns whether this call delivered anything.
        """
        if self.released:
            return False  # release published these effects with the rest of the turn
        self.events.flush()
        delivered = False
        for batch in self.learning:
            flagged = [item for item in batch if getattr(item, "type", None) == "safety_flagged"]
            if not flagged:
                continue
            batch[:] = [item for item in batch if getattr(item, "type", None) != "safety_flagged"]
            delivered = True
            if self.live.learning is not None:
                try:
                    self.live.learning(flagged)
                except Exception as error:
                    self.live._log("warning", "engine", "voice_safety_write_failed", error=type(error).__name__)
        for envelope in [frame for frame in self.events.frames if _calm(frame["event"])]:
            self.events.frames.remove(envelope)
            self.live.events.emit(copy.deepcopy(envelope["event"]))
            self._salvaged_calm = delivered = True
        self.safety_preserved |= delivered
        return delivered

    def prepare_error(self) -> None:
        """A fixed engine-owned failure reply; never includes a rejected model candidate (VOC-40)."""
        if self.result is not None or self.task is not None or self.finished:
            raise VoiceTurnConflict("voice_turn_already_started")
        turn = self.engine._begin("answer", self.ids)
        turn.status = "error"
        self.engine.state.transcript.append({"role": "user", "content": self.text, "input_kind": "voice"})
        self.spoken_text = "Sorry, something went wrong. Please say that again."
        self.result = self.engine._finish_text(turn, self.spoken_text, None, "provider_error", "provider_error")
        self.latency["adapter_first_sentence_ms"] = self._offset()

    def _offset(self) -> int:
        return max(0, int((time.monotonic() - self.started) * 1000))

    def _trace(self, record: dict[str, Any], *, discarded: bool, ended_by: str, heard_ms: int = 0) -> dict[str, Any]:
        extra = ["verbaliser_" + reason for reason in self.fallbacks]
        if self.cue_misses:
            extra.append("pointer_target_missed")  # VSL-45: a marker drew nothing; its reason codes are in the log
        if self.cues:
            # VOC-51: the sidecar gives no word alignment, so each cue fires at the caption synchroniser's estimate of
            # its word on the client's measured playback clock; the turn records that timing as estimated.
            extra.append("cue_timing_estimated")
        if self.counted is False:
            extra.append("unheard_evidence_dropped")  # VOC-52: the hint or demonstration was not heard
        gates = [
            *record["gates"],
            *[
                {"gate": name, "fired": True, "nudges": 0, "attempt": 1, "outcome": "passed"}
                for name in dict.fromkeys(extra)
            ],
        ][:16]
        return {
            **record,
            "gates": gates,
            "turn_source": "voice",
            "voice": {
                "speculative_discard": discarded,
                "carried_forward": self.carried_forward,
                "path": self.path,
                "talk_mode": self.talk_mode,
                "ended_by": ended_by,
                "heard_ms": max(0, heard_ms),
                "latency": dict(self.latency),
            },
        }

    def release(self, stream: EventStream) -> Engine:
        """No awaits: validate the snapshot, adopt the fork, and flush its events as one event-loop operation.

        The owner attaches this engine before calling, so event persistence observes the adopted state. Completion
        is held until playback finishes; a later interruption can then produce exactly one interrupted turn.end.
        """
        if self.finished or self.released or self.result is None or (self.task is not None and not self.task.done()):
            raise VoiceTurnConflict("voice_turn_not_ready")
        if state_to_json(self.live.state) != self.before:
            raise VoiceTurnConflict("voice_state_changed")
        self.released = True
        self.latency["release_ms"] = self._offset()
        self._stream = stream
        self.engine.events = stream
        spill = self.engine.deps.executor.spill
        if isinstance(spill, _SpillOverlay):
            self.engine.deps.executor.spill = spill.publish()
        stream.part_guard = self.engine.audience
        stream.begin_turn(self.ids.turn_id)
        for envelope in self.events.frames:
            event = copy.deepcopy(envelope["event"])
            if event["type"] == "turn.end":
                self._terminal = event
            else:
                stream.emit(event, turn_id=self.ids.turn_id)
        # Evidence about tutor output counts once its content was heard or shown (VOC-36, VOC-52). The turn's text and
        # board lines are shown on release (VOC-34), so it lands now, except for a demonstration written on in time with
        # speech (write cues, UXS-55, UXS-64): that line is revealed only as playback reaches its words, so the
        # evidence waits for them. The student's own evidence (attempts, verdicts, safety) always lands now.
        paced = self.demonstrates()
        for batch in self.learning:
            self.held.extend(item for item in batch if paced and _tutor_output(item))
            batch = [item for item in batch if not (paced and _tutor_output(item))]
            if self.live.learning is not None and batch:
                try:
                    self.live.learning(batch)
                except Exception as error:
                    self.live._log("warning", "engine", "voice_learning_write_failed", error=type(error).__name__)
        self.learning.clear()
        for reason in self.cue_misses:
            self.live._log("info", "engine", "pointer_target_missed", turn_id=self.ids.turn_id, reason=reason)
        for callback in self.deferred:
            try:
                callback()
            except Exception as error:
                self.live._log("warning", "engine", "voice_deferred_write_failed", error=type(error).__name__)
        self.deferred.clear()
        self.engine.learning = self.live.learning
        self.engine.on_memory_request = self.live.on_memory_request
        self.engine.save_question = self.live.save_question
        self.engine.record_cloud_notice = self.live.record_cloud_notice
        self.engine.deps = replace(self.engine.deps, trace_sink=self.live.deps.trace_sink)
        self.engine.logger, self.engine.worker_logger = self.live.logger, self.live.worker_logger
        return self.engine

    def finish(
        self,
        *,
        interrupted: bool = False,
        played_ms: int = 0,
        total_ms: int = 0,
        ended_by: str = "flush",
        heard_chars: int | None = None,
    ) -> None:
        if not self.released or self.finished or self._stream is None or self._terminal is None:
            raise VoiceTurnConflict("voice_turn_not_released")
        self.finished = True
        if heard_chars is not None:
            # VOC-36: the sidecar's output transcript, aligned with the audio played, says what was heard. Cut back
            # to a word boundary so a half-played word is never counted.
            cut = max(0, min(heard_chars, len(self.spoken_text)))
            whole = cut == len(self.spoken_text) or self.spoken_text[cut : cut + 1].isspace()
            end = cut if whole else max(0, self.spoken_text.rfind(" ", 0, cut))
            end = len(self.spoken_text[:end].rstrip())
        else:
            # Without transcript deltas, the same syllable-paced fallback as captions (VOC-09). Full playback is
            # exact; partial playback is deliberately conservative and never invents a spoken suffix.
            budget = max(0, played_ms) / 1000 * 3.83
            end = 0
            for word in re.finditer(r"\S+", self.spoken_text):
                syllables = max(1, len(re.findall(r"[aeiouy]+", word.group().lower())))
                if budget < syllables:
                    break
                budget -= syllables
                end = word.end()
        heard = self.spoken_text[:end] if interrupted else self.spoken_text
        self._settle_evidence(interrupted, end)
        self.delivery = dict(
            spoken_text=self.spoken_text, heard_text=heard, heard_ms=max(0, played_ms), interrupted=interrupted
        )
        for row in reversed(self.engine.state.transcript):
            if row.get("role") == "assistant":
                row.update(self.delivery)
                break
        terminal = copy.deepcopy(self._terminal)
        if interrupted:
            terminal.update(status="interrupted", stop_reason="student_stop")
            self.engine.voice_interruption = heard
            self.engine.voice_unheard = first_unheard(self.spoken_text, heard)
        self._stream.emit(terminal, turn_id=self.ids.turn_id)
        for record in self.traces:
            if interrupted:
                record = {**record, "status": "interrupted"}
            record = self._trace(
                record, discarded=False, ended_by=ended_by if interrupted else "completed", heard_ms=played_ms
            )
            self.engine.traces = [
                record if old["trace_id"] == record["trace_id"] else old for old in self.engine.traces
            ]
            self.engine._sink_trace(record)
        self.traces.clear()

    def demonstrates(self) -> bool:
        """The turn demonstrates a step written on with its words: it has a write cue the client receives as one (the
        events contract carries write cues from API 1.9; before that a write travels as a point and the line shows
        whole on release)."""
        return pointing.cue_actions_supported() and any(c.action == "write" for c in self.cues)

    def content_span(self) -> tuple[int, int]:
        """Where the turn's teaching content lies in the spoken text: its demonstration (the span of its last write
        cue) when it has one, else its first teaching unit (VOC-52, UXS-64)."""
        writes = [(c.char_offset, c.end_offset) for c in self.cues if c.action == "write" and c.end_offset is not None]
        if writes:
            return max(writes, key=lambda span: span[1])
        spans = units(self.spoken_text)
        return spans[0] if spans else (0, len(self.spoken_text))

    def _settle_evidence(self, interrupted: bool, heard_end: int) -> None:
        """Write the held tutor-output evidence when its content was heard, or drop it and restore the hint state the
        turn advanced (VOC-36: the rung advances, with the PED-07 effects of rung 5, only if heard or shown)."""
        held, self.held = self.held, []
        if not held:
            return
        start, end = self.content_span()
        needed = start + int((end - start) * self.live.deps.settings.heard_unit_share)
        self.counted = not interrupted or heard_end >= needed
        if self.counted:
            if self.live.learning is not None:
                try:
                    self.live.learning(held)
                except Exception as error:
                    self.live._log("warning", "engine", "voice_learning_write_failed", error=type(error).__name__)
            return
        state = self.engine.state
        rung = state.rung
        for name in HINT_STATE:
            setattr(state, name, copy.deepcopy(self.before[name]))
        if state.rung != rung and self._stream is not None:
            self._stream.emit({"type": "session.updated", "hint_rung": state.rung}, turn_id=self.ids.turn_id)
        self.live._log("info", "engine", "unheard_evidence_dropped", turn_id=self.ids.turn_id, count=len(held))

    async def discard(self, *, preserve_safety: bool = True) -> None:
        """Drop the fork. `preserve_safety=False` only for an owner that has already salvaged under its own check
        that this turn's profile is still the loaded one (a late discard never writes into another profile)."""
        if self.released:
            raise VoiceTurnConflict("voice_turn_already_released")
        if self.finished:
            return
        self.finished = True
        self.engine.cancel()
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if preserve_safety:
            self.salvage_safety()
        self.events.frames.clear()
        self.learning.clear()
        self.deferred.clear()
        for record in self.traces:
            discarded = {
                **record,
                "status": "interrupted",
                "gates": [
                    *record["gates"],
                    {
                        "gate": "voice_speculative_discard",
                        "fired": True,
                        "nudges": 0,
                        "attempt": 1,
                        "outcome": "passed",
                    },
                ],
            }
            discarded = self._trace(discarded, discarded=True, ended_by="flush")
            self.live._sink_trace(discarded)
        self.traces.clear()
        self.engine.deps.executor.spill = self.live.deps.executor.spill
        self.live._log("info", "engine", "voice_speculative_discard", turn_id=self.ids.turn_id)
