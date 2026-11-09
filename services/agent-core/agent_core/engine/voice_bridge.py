"""Transport-independent Realtime bridge reducer (VOC-04/09/11/25/31/41).

Each instance owns one socket generation and returns effects; it never opens a socket, runs a model, or stores
audio. The adapter must explicitly authorise a response after Finalise before any of its audio can pass through.
Release/discard/cancel effects must be implemented by the provisional-turn adapter before enabling tutor voice.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from agent_core.contracts import errors as contract_errors
from agent_core.contracts.voice import realtime_events as wire
from agent_core.engine.pointing import Cue, cue_actions_supported
from agent_core.engine.voice_bindings import VoiceBinding

MAX_PCM = 4800  # 100 ms of 24 kHz mono PCM16
KEY_UP_MARGIN_MS = 240  # trailing silence beyond the configured pause sent on push-to-talk key-up
MAX_HELD_PCM = 480_000  # 10 s of microphone audio held while the endpoint is replaced (VOC-29)
SYLLABLES_PER_S = 3.83  # the VOC-09 fallback synchroniser's rate
# A cue names a board step, a part of a visual on screen (VSL-19, UXS-58 `<visual_id>:<part_id>`) or a skill on the
# concept map (`kc:<kc_id>`), by id only.
CUE_TARGET = re.compile(
    r"stp_[0-9A-HJKMNP-TV-Z]{26}|vis_[0-9A-HJKMNP-TV-Z]{26}:[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*"
    r"|kc:kc\.(mth|phy|chm|bio|acc)\.(o|a)\.[a-z0-9_]+\.[a-z0-9_]+"
)
MAX_OUTPUT_PCM = 240_000  # bound an endpoint's single output frame to five seconds
MODELS: dict[str, type[BaseModel]] = {
    "session.created": wire.SessionCreated,
    "session.updated": wire.SessionUpdated,
    "error": wire.Error,
    "conversation.item.created": wire.ConversationItemCreated,
    "input_audio_buffer.speech_started": wire.SpeechStarted,
    "input_audio_buffer.speech_stopped": wire.SpeechStopped,
    "conversation.item.input_audio_transcription.delta": wire.InputTranscriptionDelta,
    "conversation.item.input_audio_transcription.completed": wire.InputTranscriptionCompleted,
    "speech_to_speech.input_audio_transcription.snapshot": wire.InputTranscriptionSnapshot,
    "response.created": wire.ResponseCreated,
    "response.done": wire.ResponseDone,
    "response.output_audio.delta": wire.OutputAudioDelta,
    "response.output_audio.done": wire.OutputAudioDone,
    "response.output_audio_transcript.delta": wire.OutputAudioTranscriptDelta,
    "response.output_audio_transcript.done": wire.OutputAudioTranscriptDone,
    "response.function_call_arguments.done": wire.FunctionCallArgumentsDone,
}


LATENCY_FIELDS = ("stt_s", "llm_s", "tts_ttfa_s", "e2e_s", "vad_decision_s", "hold_s")
LATENCY_KEY = "speech_to_speech.turn_latency"


def turn_latency(metadata: Any) -> dict[str, Any]:
    """The sidecar's per-turn latency from `response.done` metadata (VOC-22): timings only, never text. Accepts the
    fields as one JSON object under the key or as one value per `<key>.<field>`; anything else is dropped."""
    if not isinstance(metadata, dict):
        return {}
    raw: dict[str, Any] = {}
    whole = metadata.get(LATENCY_KEY)
    if isinstance(whole, str):
        try:
            parsed = json.loads(whole)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            raw.update(parsed)
    for key, value in metadata.items():
        if isinstance(key, str) and key.startswith(LATENCY_KEY + "."):
            raw[key.removeprefix(LATENCY_KEY + ".")] = value
    out: dict[str, Any] = {}
    for name in LATENCY_FIELDS:
        try:
            value = float(raw[name])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value) and value >= 0:
            out[name] = value
    status = raw.get("smart_status")
    if isinstance(status, str) and re.fullmatch(r"[a-z_]{1,32}", status):
        out["smart_status"] = status
    return out


class VoiceProtocolError(ValueError):
    """Reason codes only; never include endpoint frames or audio in the exception."""


@dataclass(repr=False)
class Effects:
    upstream: list[dict[str, Any]] = field(default_factory=list)
    ui: list[dict[str, Any] | bytes] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)


@dataclass(repr=False)
class _Response:
    response_id: str
    turn_id: str | None = None
    spoken_text: str | None = None
    released: bool = False
    done: bool = False
    drained: bool = False
    pcm_bytes: int = 0
    played_ms: int = 0
    caption: str = ""
    caption_final: bool = False
    utterance_id: str | None = None
    # (output audio bytes received when a transcript delta arrived, transcript length after it): aligns the spoken
    # text with the audio, so heard text follows the transcript rather than a syllable estimate (VOC-36).
    marks: list[tuple[int, int]] = field(default_factory=list)
    latency: dict[str, Any] = field(default_factory=dict)  # the sidecar's turn_latency (VOC-22)
    playback_started: bool = False
    # The VOC-12 filler: content-free keyed speech allowed before the turn is accepted. Its audio is played but never
    # releases the turn; the turn's own audio, after acceptance, starts at `offset_bytes`.
    filler: str | None = None
    offset_bytes: int = 0
    # The accepted turn's cues (VOC-10): offsets into `spoken_text`, sent with the caption once its words are there.
    cues: tuple[Cue, ...] = ()

    def played_turn_ms(self) -> int:
        return max(0, self.played_ms - self.offset_bytes // 48)

    def turn_ms(self) -> int:
        return max(0, (self.pcm_bytes - self.offset_bytes) // 48)

    def heard_chars(self, played_ms: int) -> int | None:
        """Characters of the spoken text whose audio has fully played; None without transcript deltas. Conservative
        whichever side of its audio a delta arrives on: a segment counts only once the next one has started."""
        if not self.marks:
            return None
        played, heard = max(0, played_ms) * 48, 0
        ends = [pcm for pcm, _ in self.marks[1:]] + [self.pcm_bytes if self.done else None]
        for (_, length), end in zip(self.marks, ends, strict=True):
            if end is None or played < end:
                break
            heard = length
        return heard


@dataclass(frozen=True)
class Silence:
    """Run-time `silence_duration_ms` values (TABLE VOC-05a, VOC-06), from the named configuration keys."""

    normal_ms: int = 160
    thinking_ms: int = 1200
    hold_ms: int = 10000


class VoiceBridge:
    def __init__(
        self, binding: VoiceBinding, *, snapshots: bool = False, strict: bool = False, silence: Silence | None = None
    ) -> None:
        self.binding = binding
        self.silence = silence or Silence()
        # The tutor's last voice turn awaits worked input (expects a student answer): pauses get the thinking
        # extension until the next turn starts (VOC-05).
        self.awaiting_work = False
        self.snapshots = snapshots
        self.strict = strict
        self.closed = False
        self.ready = False
        self.mode = "push_to_talk"
        self.pressed = False
        self.thinking = False
        self.echo_safe = False
        # After distress (PRV-14) voice rests idle with the mic closed until the student leaves the calm screen,
        # which the client signals by re-sending its talk mode (VOC-20).
        self.calm = False
        # False where only read-aloud and dictation are offered (low class, or over the VOC-03 memory budget).
        self.tutoring = True
        self.state = "idle"
        self._last_state: dict[str, Any] | None = None
        self._unknown: set[str] = set()
        self._item: str | None = None
        self._committed = ""
        self._final_input = False
        self._stopped_input = False
        self._response: _Response | None = None
        self._speech_end: int | None = None
        self._pending = False
        self._pending_turn: str | None = None
        self._accepted_pending: tuple[str, str, tuple[Cue, ...]] | None = None
        self._filler: tuple[str, str] | None = None  # (pending turn, keyed filler) before response.created
        self._interrupted_at: int | None = None
        self._responses_seen: set[str] = set()
        self._items_seen: set[str] = set()
        self._cancelled_pending = False
        self._read: tuple[str, str | None, str] | None = None  # trusted utterance id, accepted turn id, verbaliser text
        self._queued_read: tuple[str, str | None, str] | None = None
        self.dictation_id: str | None = None
        self._dictation_item: str | None = None
        self._dictation_final = False
        self._dictation_done = False
        self._dictation_deadline: int | None = None
        self._dictation_now = 0
        self._dictation_segments: dict[str, tuple[str, bool]] = {}

        # VOC-29: while the online endpoint is replaced by the local sidecar, microphone audio is held in memory (never
        # written anywhere) and sent to the new endpoint after its configuration, so the student's words are kept.
        self._reconnecting = False
        self._held: list[bytes] = []

    def endpoint_lost(self) -> Effects:
        """The online endpoint closed, errored or missed its heartbeats mid-session (VOC-29): an active response is
        interrupted at the played position (VOC-36) and the bridge waits for the replacement endpoint's
        `session.created`, which replays this binding's `session.update` before any held audio."""
        effects = Effects()
        if self.closed:
            return effects
        self._drop_queued_read(effects)
        if self.dictation_id is not None:
            self._end_dictation(effects, cancelled=True)
        self._interrupt(effects, upstream=False)
        self.ready = False
        self._reconnecting = True
        self._item, self._committed = None, ""
        return effects

    def queue_read_aloud(self, utterance_id: str, turn_id: str | None, text: str) -> Effects:
        """Called only after the core resolves an owned, accepted text part."""
        if self.dictation_id is not None:
            raise VoiceProtocolError("dictation_active")
        effects = Effects()
        if self._read is not None:
            self._interrupt(effects, upstream=True)
        self._queued_read = utterance_id, turn_id, text
        self._start_read(effects)
        return effects

    def _start_read(self, effects: Effects) -> None:
        if self.ready and self._queued_read is not None and self._response is None and not self._pending:
            self._read, self._queued_read = self._queued_read, None
            self._cancelled_pending = False
            self._pending = True
            effects.upstream.extend(
                [
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "jana-speak: " + self._read[0]}],
                        },
                    },
                    {"type": "response.create"},
                ]
            )
            self._state("thinking", effects)

    def _drop_queued_read(self, effects: Effects) -> None:
        if self._queued_read is not None:
            effects.actions.append({"type": "read_finished", "utterance_id": self._queued_read[0]})
            self._queued_read = None

    def start_dictation(self, capture_id: str, now_ms: int) -> Effects:
        if self.dictation_id is not None or self._response is not None or self._pending or self._read is not None:
            raise VoiceProtocolError("voice_busy")
        self.dictation_id = capture_id
        self._dictation_item = None
        self._dictation_segments.clear()
        self._dictation_final, self._dictation_done = False, False
        self._dictation_deadline, self._dictation_now = now_ms + 120_000, now_ms
        self.pressed = True
        self._state("listening", effects := Effects())
        effects.upstream.append(self._config())
        return effects

    def _end_dictation(self, effects: Effects, *, cancelled: bool) -> None:
        self._items_seen.update(self._dictation_segments)
        if self.dictation_id is not None:
            if not cancelled and self._dictation_final:
                effects.actions.append(
                    {"type": "dictation_transcript", "capture_id": self.dictation_id, "text": self._committed}
                )
                effects.events.append(
                    {
                        "type": "voice.caption",
                        "speaker": "student",
                        "item_id": self.dictation_id,
                        "turn_id": None,
                        "committed_text": self._committed,
                        "tentative_text": "",
                        "words": [],
                        "cues": [],
                        "final": True,
                    }
                )
            effects.actions.append(
                {"type": "dictation_finished", "capture_id": self.dictation_id, "cancelled": cancelled}
            )
        self.dictation_id, self._dictation_item, self._dictation_deadline = None, None, None
        self._dictation_segments.clear()
        self._committed = ""
        self.pressed = False
        self._pending = False
        self._cancelled_pending = True
        self._state("idle", effects)

    def _state(self, value: str, effects: Effects, reason: str | None = None) -> None:
        self.state = value
        event = {
            "type": "voice.state",
            "state": value,
            "reason_key": reason,
            "turn_id": self._response.turn_id if self._response else None,
            "listening_variant": ("take_your_time" if self.thinking else "normal") if value == "listening" else None,
        }
        if event != self._last_state:
            effects.events.append(event)
            self._last_state = event

    def _rest(self, effects: Effects) -> None:
        hands_free = self.mode == "hands_free" or self.pressed
        # A held dictation listens wherever it is (the tutor screen offers no conversation): reporting `idle` here
        # would tell the client the dictation ended, and it would never send the release.
        dictating = self.dictation_id is not None and self.pressed
        listening = self.ready and not self.calm and ((self.tutoring and hands_free) or dictating)
        self._state("listening" if listening else "idle", effects)

    def offer_conversation(self, offered: bool) -> Effects:
        """Trusted runtime callback (VOC-53, D130): whether this socket's place holds a conversation. Withdrawn, any
        tutor response or pending turn stops and capture closes; read-aloud and dictation are unaffected."""
        effects = Effects()
        if self.closed or offered == self.tutoring:
            return effects
        if not offered and self._read is None and self.dictation_id is None and (self._response or self._pending):
            self._cancelled_pending = True
            self._interrupt(effects, upstream=True, ended_by="stop_key")
        self.tutoring, self.pressed, self.thinking = offered, False, False
        if self._response is None and not self._pending and self.dictation_id is None:
            self._rest(effects)
        return effects

    def await_work(self, awaiting: bool) -> Effects:
        """Trusted adapter callback: the accepted turn's `expects` (VOC-05 thinking extension), or a new turn."""
        effects = Effects()
        if awaiting != self.awaiting_work and not self.closed:
            self.awaiting_work = awaiting
            if self.ready:
                effects.upstream.append(self._config())
        return effects

    def hold_calm(self) -> Effects:
        """Distress was shown (VOC-20): no capture and no new voice turn until the client resumes with `mode`."""
        effects = Effects()
        self.calm, self.pressed, self.thinking = True, False, False
        if self.dictation_id is not None:
            effects.upstream.append({"type": "response.cancel"})
            self._end_dictation(effects, cancelled=True)
        if self._response is None and not self._pending:
            self._rest(effects)
        return effects

    def _complete(self, effects: Effects) -> None:
        response = self._response
        if response is not None and response.utterance_id is None:
            effects.actions.append(
                {
                    "type": "complete",
                    "response_id": response.response_id,
                    "turn_id": response.turn_id,
                    "played_ms": response.played_turn_ms(),
                    "total_ms": response.turn_ms(),
                    "turn_latency": dict(response.latency),
                }
            )
        elif response is not None:
            effects.actions.append({"type": "read_finished", "utterance_id": response.utterance_id})
        self._response = None
        self._read = None
        self._pending_turn, self._accepted_pending, self._pending = None, None, False
        self._rest(effects)

    def _silence_ms(self) -> int:
        """The `silence_duration_ms` the endpoint is configured with (TABLE VOC-05a, VOC-06)."""
        if self.thinking or (self.dictation_id is not None and self.pressed):
            return self.silence.hold_ms
        if self.awaiting_work and self.dictation_id is None:
            return self.silence.thinking_ms
        return self.silence.normal_ms

    def _config(self) -> dict[str, Any]:
        session: dict[str, Any] = {
            "type": "realtime",
            "instructions": self.binding.instructions,
            "tools": [],
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"language": "en"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.6,
                        "silence_duration_ms": self._silence_ms(),
                        "interrupt_response": self.echo_safe,
                    },
                },
                "output": {"format": {"type": "audio/pcm", "rate": 24000}},
            },
        }
        if self.snapshots:
            session["extensions"] = ["speech_to_speech.input_audio_transcription.snapshot"]
        return {"type": "session.update", "session": session}

    def authorise_response(self, response_id: str, turn_id: str, spoken_text: str, cues: tuple[Cue, ...] = ()) -> None:
        """Trusted adapter callback after all gates; not a UI or endpoint control."""
        response = self._response
        if self.closed or response is None or response.response_id != response_id or response.released:
            raise VoiceProtocolError("response_not_current")
        self._validate_spoken(turn_id, spoken_text)
        self._validate_cues(spoken_text, cues)
        if response.turn_id is not None:
            raise VoiceProtocolError("response_already_authorised")
        response.turn_id, response.spoken_text, response.cues = turn_id, spoken_text, cues

    @staticmethod
    def _validate_cues(spoken_text: str, cues: tuple[Cue, ...]) -> None:
        """Cues name ids only (VSL-19) and index the spoken text; anything else is a defect, never sent (VOC-10)."""
        if len(cues) > 50:
            raise VoiceProtocolError("invalid_cues")
        for cue in cues:
            end = cue.end_offset if cue.end_offset is not None else cue.char_offset
            if not (0 <= cue.char_offset <= end <= len(spoken_text)) or not CUE_TARGET.fullmatch(cue.target):
                raise VoiceProtocolError("invalid_cues")

    @staticmethod
    def _validate_spoken(turn_id: str, spoken_text: str) -> None:
        if (
            not re.fullmatch(r"trn_[0-9A-HJKMNP-TV-Z]{26}", turn_id)
            or not spoken_text
            or len(spoken_text) > 4000
            or re.search(r"[\\$^_{}<>`*#|]", spoken_text)
        ):
            raise VoiceProtocolError("invalid_spoken_text")

    def begin_provisional(self, turn_id: str, *, replace: bool = False) -> bool:
        """Trusted adapter ownership, before evaluation; response.created may follow the LLM request upstream."""
        if (
            self.closed
            or self.calm
            or not self.tutoring
            or self.thinking
            or self._cancelled_pending
            or self._read is not None
            or self.dictation_id is not None
        ):
            return False
        if replace and self._response is not None:
            if self._response.released:
                raise VoiceProtocolError("response_still_playing")
            self._response = None  # superseded before audio; its id remains in the stale-response set
        self._pending_turn, self._accepted_pending, self._pending = turn_id, None, True
        return True

    def authorise_filler(self, turn_id: str, filler: str) -> None:
        """Trusted adapter callback (VOC-12): the one keyed filler of a pending turn may play before acceptance."""
        self._validate_spoken(turn_id, filler)
        if self.closed or self._pending_turn != turn_id or self.thinking or self._cancelled_pending:
            raise VoiceProtocolError("voice_turn_not_current")
        if self._response is not None and not self._response.released and self._response.spoken_text is None:
            self._response.filler = filler
        else:
            self._filler = turn_id, filler

    def authorise_turn(self, turn_id: str, spoken_text: str, cues: tuple[Cue, ...] = ()) -> None:
        self._validate_spoken(turn_id, spoken_text)
        self._validate_cues(spoken_text, cues)
        if self.closed or self._pending_turn != turn_id or self.thinking or self._cancelled_pending:
            raise VoiceProtocolError("voice_turn_not_current")
        if self._response is None:
            self._accepted_pending = turn_id, spoken_text, cues
        else:
            self.authorise_response(self._response.response_id, turn_id, spoken_text, cues)

    def _interrupt(self, effects: Effects, *, upstream: bool, ended_by: str = "flush") -> None:
        self._pending_turn, self._accepted_pending, self._filler = None, None, None
        response = self._response
        if response is None:
            if self._pending:
                if upstream:
                    effects.upstream.append({"type": "response.cancel"})
                if self._read is None:
                    effects.actions.append({"type": "discard", "response_id": None, "turn_id": None, "played_ms": 0})
                else:
                    effects.actions.append({"type": "read_finished", "utterance_id": self._read[0]})
                    self._read = None
                self._pending = False
                self._speech_end = None
                self._state("interrupted", effects)
            return
        if upstream and not response.done:
            effects.upstream.append({"type": "response.cancel", "response_id": response.response_id})
        effects.ui.append(
            {
                "type": "flush",
                "utterance_id" if response.utterance_id else "response_id": response.utterance_id
                or response.response_id,
            }
        )
        if response.utterance_id is not None:
            effects.actions.append({"type": "read_finished", "utterance_id": response.utterance_id})
        else:
            effects.actions.append(
                {
                    "type": "cancel" if response.released else "discard",
                    "response_id": response.response_id,
                    "turn_id": response.turn_id,
                    "played_ms": response.played_turn_ms(),
                    "total_ms": response.turn_ms(),
                    "ended_by": ended_by,
                    "heard_chars": response.heard_chars(response.played_ms),
                    "turn_latency": dict(response.latency),
                }
            )
        self._response = None
        self._read = None
        self._speech_end = None
        self._pending = False
        self._state("interrupted", effects)

    def close(self) -> Effects:
        effects = Effects()
        if not self.closed:
            self._drop_queued_read(effects)
            if self.dictation_id is not None:
                self._end_dictation(effects, cancelled=True)
            self._interrupt(effects, upstream=True)
            self.ready = False
            self.pressed = False
            self.closed = True
            self._reconnecting, self._held = False, []
            self._item, self._committed = None, ""
            self._rest(effects)
        return effects

    def tick(self, now_ms: int) -> Effects:
        effects = Effects()
        self._dictation_now = now_ms
        if self.dictation_id is not None:
            if self._dictation_deadline is not None and now_ms >= self._dictation_deadline:
                effects.upstream.append({"type": "response.cancel"})
                self._end_dictation(effects, cancelled=not self._dictation_final)
            return effects
        self._start_read(effects)
        if not self.closed and not self.thinking and self._speech_end is not None and now_ms - self._speech_end >= 600:
            self._speech_end = None
            self._state("thinking", effects)
        if not self.closed and self.state == "interrupted":
            if self._interrupted_at is None:
                self._interrupted_at = now_ms
            elif now_ms - self._interrupted_at >= 1000:
                self._interrupted_at = None
                self._rest(effects)
        return effects

    def microphone(self, pcm: bytes) -> Effects:
        effects = Effects()
        if not pcm or len(pcm) > MAX_PCM or len(pcm) % 2:
            raise VoiceProtocolError("invalid_pcm_frame")
        if (self._reconnecting and not self.closed and not self.calm and self.tutoring
                and (self.pressed or self.mode == "hands_free")):  # fmt: skip
            if sum(len(held) for held in self._held) + len(pcm) <= MAX_HELD_PCM:
                self._held.append(pcm)
        if (
            self.closed
            or self.calm
            or not self.ready
            or (self.dictation_id is None and not self.tutoring)
            or not (self.pressed or self.mode == "hands_free")
            or (self.dictation_id is not None and not self.pressed)
        ):
            return effects
        if self._response is not None and self._response.pcm_bytes and not self.echo_safe:
            return effects
        effects.upstream.append({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode("ascii")})
        return effects

    def control(self, control: dict[str, Any]) -> Effects:
        effects = Effects()
        if self.closed:
            return effects
        kind = control.get("type")
        if kind == "stop" and set(control) == {"type", "utterance_id"}:
            identity = control["utterance_id"]
            if self._queued_read is not None and self._queued_read[0] == identity:
                self._queued_read = None
                effects.actions.append({"type": "read_finished", "utterance_id": identity})
            if self._read is not None and self._read[0] == identity:
                self._cancelled_pending = True
                self._interrupt(effects, upstream=True)
            return effects  # a queued read's Stop must never cancel the live tutor response
        if kind == "stop" and set(control) == {"type"}:
            self._drop_queued_read(effects)
            if self.dictation_id is not None:
                # Only the five client events of VOC-25: buffered audio is not cleared upstream. Its late transcript
                # is ignored (the capture ended) and its adapter request is answered empty on the retired binding.
                effects.upstream.append({"type": "response.cancel"})
                self._end_dictation(effects, cancelled=True)
                return effects
            if self._response is not None or self._pending:
                self._cancelled_pending = True
            self._interrupt(effects, upstream=True, ended_by="stop_key")
        elif (
            kind == "dictate"
            and set(control) == {"type", "capture_id", "pressed"}
            and control["capture_id"] == self.dictation_id
            and control["pressed"] is False
        ):
            self.pressed = False
            effects.upstream.append(self._config())
            silence = base64.b64encode(bytes(MAX_PCM)).decode("ascii")
            effects.upstream.extend({"type": "input_audio_buffer.append", "audio": silence} for _ in range(4))
            self._dictation_deadline = self._dictation_now + (20_000 if self._dictation_item else 1_000)
            if self._dictation_final and self._dictation_done:
                self._end_dictation(effects, cancelled=False)
            else:
                self._state("thinking", effects)
        elif kind == "mode" and set(control) == {"type", "mode"} and control["mode"] in ("push_to_talk", "hands_free"):
            self.mode = control["mode"]
            self.pressed, self.calm = False, False
            if self._response is None and not self._pending:
                self._rest(effects)
        elif kind == "ptt" and set(control) == {"type", "pressed"} and type(control["pressed"]) is bool:
            if self.dictation_id is not None:
                raise VoiceProtocolError("dictation_active")
            previous = self.pressed
            self.pressed = control["pressed"] and not self.calm
            if self.pressed and not previous:
                self._drop_queued_read(effects)
                self._interrupt(effects, upstream=True, ended_by="barge_in")
                self._cancelled_pending = False
            elif previous and not self.pressed and self.ready:
                # Capture closes on key-up, so the VAD hears no more audio: it soft-ends the utterance only on trailing
                # silence longer than its configured `silence_duration_ms`. After a turn that awaits worked input that
                # is the thinking extension (VOC-05), not the normal pause; a fixed 400 ms left the next utterance open
                # and never transcribed. The held think key keeps its long hold (VOC-06): the student is not done.
                pause = self.silence.normal_ms if self.thinking else self._silence_ms()
                silence = base64.b64encode(bytes(MAX_PCM)).decode("ascii")
                frames = -(-(pause + KEY_UP_MARGIN_MS) // 100)  # 100 ms per MAX_PCM frame
                effects.upstream.extend({"type": "input_audio_buffer.append", "audio": silence} for _ in range(frames))
            if self._response is None and not self._pending:
                self._rest(effects)
        elif (
            kind == "capture"
            and set(control) == {"type", "echo_cancellation", "headphones"}
            and all(type(control[key]) is bool for key in ("echo_cancellation", "headphones"))
        ):
            self.echo_safe = control["echo_cancellation"] or control["headphones"]
            if self.ready:
                effects.upstream.append(self._config())
        elif kind == "think" and set(control) == {"type", "pressed"} and type(control["pressed"]) is bool:
            before, speech_end = self.state, self._speech_end
            self.thinking = control["pressed"]
            if self.thinking and (self._pending or (self._response is not None and not self._response.released)):
                self._interrupt(effects, upstream=True)
                self._speech_end = speech_end
            if self.ready:
                effects.upstream.append(self._config())
                if self.state == "listening" or (self.thinking and before in ("listening", "thinking")):
                    self._state("listening", effects)
        elif kind == "playback":
            response = self._response
            identity = "utterance_id" if "utterance_id" in control else "response_id"
            valid = (
                set(control) == {"type", identity, "played_ms", "ended"}
                and type(control["played_ms"]) is int
                and control["played_ms"] >= 0
                and type(control["ended"]) is bool
            )
            if not valid:
                raise VoiceProtocolError("invalid_playback")
            # Binary frames identify responses by their 16-byte tag. The playback client reports its hex form;
            # the original upstream id is also accepted for protocol fixtures and native clients that know it.
            raw = response.utterance_id or response.response_id if response is not None else None
            tag = hashlib.sha256(raw.encode()).hexdigest()[:32] if raw is not None else None
            audible = response is not None and (response.released or response.filler is not None)
            if response is not None and control[identity] in (raw, tag) and audible:
                played = control["played_ms"]
                if response.played_ms <= played <= response.pcm_bytes // 48:
                    response.played_ms = played
                    response.drained = control["ended"] and played == response.pcm_bytes // 48
                    if played:
                        self._state("speaking", effects)
                        if response.utterance_id:
                            self._read_caption(effects, response)
                        elif (
                            response.released and not response.playback_started and played * 48 > response.offset_bytes
                        ):
                            # The UI's playback start, for the turn's latency record (VOC-22).
                            response.playback_started = True
                            effects.actions.append({"type": "playback_started", "turn_id": response.turn_id})
                    if response.done and response.drained and response.released:
                        self._complete(effects)
        else:
            raise VoiceProtocolError("unsupported_control")
        return effects

    def _caption(self, effects: Effects, *, tentative: str = "", final: bool = False) -> None:
        if len(self._committed) > 4000 or len(tentative) > 1000:
            raise VoiceProtocolError("caption_limit")
        effects.events.append(
            {
                "type": "voice.caption",
                "speaker": "student",
                "item_id": self._item,
                "turn_id": None,
                "committed_text": self._committed,
                "tentative_text": tentative,
                "words": [],
                "cues": [],
                "final": final,
            }
        )

    def server(self, frame: dict[str, Any], now_ms: int) -> Effects:
        effects = Effects()
        if self.closed:
            return effects
        kind = frame.get("type")
        model = MODELS.get(kind) if isinstance(kind, str) else None
        if model is None:
            # Constant diagnostic, bounded dedupe: never log an endpoint-controlled name or frame.
            if "unknown_event" not in self._unknown:
                self._unknown.add("unknown_event")
                effects.diagnostics.append("unknown_event")
            return effects
        assert isinstance(kind, str)
        if not self.strict and set(frame) - set(model.model_fields) and "unknown_field" not in self._unknown:
            self._unknown.add("unknown_field")
            effects.diagnostics.append("unknown_field")
        try:
            if self.strict and contract_errors("voice/realtime-events", frame):
                raise VoiceProtocolError("invalid_realtime_frame")
            event = model.model_validate(frame, strict=True, extra="ignore").model_dump(exclude_none=True)
        except ValidationError:
            raise VoiceProtocolError("invalid_realtime_frame") from None
        if kind == "session.created":
            if not self.ready:
                self.ready = True
                effects.upstream.append(self._config())
                if self._reconnecting:
                    # VOC-29: the same binding on the local endpoint, then the words said meanwhile.
                    self._reconnecting = False
                    held, self._held = self._held, []
                    for pcm in held:
                        effects.upstream.append(
                            {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode("ascii")}
                        )
                self._rest(effects)
            return effects
        if not self.ready:
            return effects
        if self.dictation_id is not None and kind != "error":
            self._dictation_server(kind, event, now_ms, effects)
            return effects
        if kind == "error":
            self._interrupt(effects, upstream=False)
            self.ready = False
            self._state("unavailable", effects, "voice.endpoint_error")
        elif kind == "input_audio_buffer.speech_started":
            if event["item_id"] in self._items_seen:
                return effects
            if len(self._items_seen) >= 1024:
                raise VoiceProtocolError("connection_event_limit")
            if self._response is not None and self._response.pcm_bytes and not self.echo_safe:
                return effects
            self._drop_queued_read(effects)
            self._interrupt(effects, upstream=False, ended_by="barge_in")
            self._items_seen.add(event["item_id"])
            self._item, self._committed = event["item_id"], ""
            self._final_input = False
            self._stopped_input = False
            self._speech_end = None
            self._interrupted_at = None
            self._cancelled_pending = False
            self._pending = True
            self._state("listening", effects)
        elif kind == "input_audio_buffer.speech_stopped" and event["item_id"] == self._item:
            if not self._stopped_input:
                self._speech_end = now_ms
                self._pending = True
                self._stopped_input = True
        elif (
            kind
            in (
                "conversation.item.input_audio_transcription.delta",
                "conversation.item.input_audio_transcription.completed",
                "speech_to_speech.input_audio_transcription.snapshot",
            )
            and event["item_id"] == self._item
        ):
            if self._final_input:
                return effects
            if kind.endswith(".completed"):
                self._committed = event["transcript"]
                self._final_input = True
                self._caption(effects, final=True)
            elif kind.endswith(".snapshot"):
                if self.snapshots:
                    self._caption(effects, tentative=event["transcript"].removeprefix(self._committed))
            else:
                self._committed += event.get("delta", "")
                self._caption(effects)
        elif kind == "response.created":
            if self._cancelled_pending or self.thinking:
                return effects  # a pending Stop can race with the endpoint assigning its response id
            response_id = event["response"].get("id")
            if not isinstance(response_id, str) or not 1 <= len(response_id) <= 128:
                raise VoiceProtocolError("invalid_response_id")
            if response_id in self._responses_seen:
                return effects
            if len(self._responses_seen) >= 1024:
                raise VoiceProtocolError("connection_event_limit")
            accepted, pending_turn, filler = self._accepted_pending, self._pending_turn, self._filler
            self._pending = False
            self._interrupt(effects, upstream=False)
            self._responses_seen.add(response_id)
            self._response = _Response(response_id)
            self._pending_turn = pending_turn
            if filler is not None and filler[0] == pending_turn:
                # The adapter has already streamed the filler ahead of the accepted text, whether or not the turn was
                # accepted before the endpoint named its response: keep it, so its transcript is not a mismatch.
                self._response.filler = filler[1]
            if self._read is not None:
                identity, turn_id, text = self._read
                self._response.utterance_id = identity
                self._response.turn_id, self._response.spoken_text = turn_id, text
            elif accepted is not None:
                self.authorise_response(response_id, *accepted)
        elif kind.startswith("response."):
            response = self._response
            response_id = event.get("response_id", event.get("response", {}).get("id"))
            if response is None or response_id != response.response_id:
                return effects
            if kind == "response.output_audio.delta":
                if response.done:
                    return effects
                if response.spoken_text is None and response.filler is None:
                    raise VoiceProtocolError("response_not_authorised")
                encoded = event["delta"]
                if len(encoded) > MAX_OUTPUT_PCM * 4 // 3 + 4:
                    raise VoiceProtocolError("output_audio_limit")
                try:
                    pcm = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error):
                    raise VoiceProtocolError("invalid_output_audio") from None
                if not pcm or len(pcm) % 2:
                    raise VoiceProtocolError("invalid_output_audio")
                if not response.released and response.spoken_text is not None:
                    # First audio of the accepted turn (after any filler) releases it (VOC-34).
                    response.released = True
                    response.offset_bytes = response.pcm_bytes
                    if response.utterance_id is None:
                        effects.actions.append(
                            {"type": "release", "response_id": response_id, "turn_id": response.turn_id}
                        )
                response.pcm_bytes += len(pcm)
                response.drained = False
                self._speech_end = None
                if response.utterance_id:
                    self._read_caption(effects, response)
                elif response.caption:
                    self._tutor_caption(effects, response)
                # Stable 16-byte tag; a UI computes the same SHA-256 prefix from a response/flush id.
                tag = hashlib.sha256((response.utterance_id or response.response_id).encode()).digest()[:16]
                effects.ui.append(tag + pcm)
            elif kind in ("response.output_audio_transcript.delta", "response.output_audio_transcript.done"):
                if response.spoken_text is None:
                    return effects  # the filler's own transcript is not the turn's
                text = event["transcript"] if kind.endswith(".done") else response.caption + event["delta"]
                if response.filler and (kind.endswith(".done") or not response.caption):
                    # The filler's words, then the space the adapter streams between it and the accepted text (the
                    # final transcript repeats the whole response, filler included).
                    text = (text[len(response.filler) :] if text.startswith(response.filler) else text).lstrip()
                if not response.spoken_text.startswith(text) or (
                    kind.endswith(".done") and text != response.spoken_text
                ):
                    raise VoiceProtocolError("spoken_transcript_mismatch")
                if len(text) > len(response.caption) and len(response.marks) < 4096:
                    response.marks.append((response.pcm_bytes, len(text)))
                response.caption = text
                response.caption_final = kind.endswith(".done")
                if response.released:
                    if response.utterance_id:
                        self._read_caption(effects, response)
                    else:
                        self._tutor_caption(effects, response)
            elif kind == "response.function_call_arguments.done":
                # VOC-38: never executed; answered so the sidecar's chat has no pending call, and logged as a
                # protocol anomaly (a constant code: never the call's name or arguments).
                effects.diagnostics.append("function_call_rejected")
                effects.upstream.append(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": event["call_id"],
                            "output": '{"error":"not_supported"}',
                        },
                    }
                )
            elif kind == "response.done":
                response.latency = turn_latency(event["response"].get("metadata"))
                if event["response"].get("status") != "completed":
                    self._interrupt(effects, upstream=False)
                else:
                    response.done = True
                    if not response.released:
                        self._interrupt(effects, upstream=False)
                        self._rest(effects)
                    elif response.drained:
                        self._complete(effects)
        return effects

    def _dictation_server(self, kind: str, event: dict[str, Any], now_ms: int, effects: Effects) -> None:
        if kind == "input_audio_buffer.speech_started" and event["item_id"] != self._dictation_item:
            if event["item_id"] in self._items_seen:
                return
            if len(self._items_seen) + len(self._dictation_segments) >= 1024 or len(self._dictation_segments) >= 64:
                raise VoiceProtocolError("connection_event_limit")
            if event["item_id"] in self._dictation_segments:
                return
            self._dictation_item = event["item_id"]
            self._dictation_segments[self._dictation_item] = "", False
            self._dictation_final, self._dictation_done = False, False
            if not self.pressed:
                self._dictation_deadline = now_ms + 20_000
        elif event.get("item_id") in self._dictation_segments:
            item = event["item_id"]
            text, already_final = self._dictation_segments[item]
            if already_final:
                return
            final = kind == "conversation.item.input_audio_transcription.completed"
            if kind == "conversation.item.input_audio_transcription.delta":
                text += event["delta"]
            elif final:
                text = event["transcript"]
            elif kind != "speech_to_speech.input_audio_transcription.snapshot":
                return
            self._dictation_segments[item] = text, final
            self._committed = " ".join(text for text, _ in self._dictation_segments.values() if text)
            self._dictation_final = all(final for _, final in self._dictation_segments.values())
            tentative = event.get("transcript", "").removeprefix(text) if kind.endswith(".snapshot") else ""
            if len(self._committed) > 4000 or len(tentative) > 1000:
                raise VoiceProtocolError("caption_limit")
            effects.events.append(
                {
                    "type": "voice.caption",
                    "speaker": "student",
                    "item_id": self.dictation_id,
                    "turn_id": None,
                    "committed_text": self._committed,
                    "tentative_text": tentative,
                    "words": [],
                    "cues": [],
                    "final": False,
                }
            )
        elif kind == "response.done":
            self._dictation_done = True
        # Dictation never creates a response or forwards audio, even if the endpoint sends it unexpectedly.
        if self._dictation_final and self._dictation_done and not self.pressed:
            self._end_dictation(effects, cancelled=False)

    @staticmethod
    def _read_caption(effects: Effects, response: _Response) -> None:
        text = response.spoken_text or ""
        words = re.findall(r"\S+", text)[:400]
        # Re-align the fallback to the audio received so far; playback, rather than receipt time, drives highlighting.
        weights = [max(1, len(re.findall(r"[aeiouy]+", word.lower()))) for word in words]
        total, offset = max(1, sum(weights)), 0
        duration = max(1, response.pcm_bytes // 48) if response.done else round(total * 1000 / 3.83)
        timed = []
        for word, weight in zip(words, weights, strict=True):
            timed.append({"w": word[:64], "start_ms": offset * duration // total})
            offset += weight
        effects.events.append(
            {
                "type": "voice.caption",
                "speaker": "tutor",
                "item_id": response.utterance_id,
                "turn_id": response.turn_id,
                "committed_text": text,
                "tentative_text": "",
                "words": timed,
                "cues": [],
                "final": response.done,
            }
        )

    @staticmethod
    def _tutor_caption(effects: Effects, response: _Response) -> None:
        shown = len(response.caption)
        actions = cue_actions_supported()
        cues = [
            cue.event(actions=actions and cue.end_offset is not None and cue.end_offset <= shown)
            for cue in response.cues
            if cue.char_offset <= shown
        ]
        effects.events.append(
            {
                "type": "voice.caption",
                "speaker": "tutor",
                "item_id": None,
                "turn_id": response.turn_id,
                "committed_text": response.caption,
                "tentative_text": "",
                "words": aligned_words(response),
                "cues": cues,
                "final": response.caption_final,
            }
        )


def aligned_words(response: _Response) -> list[dict[str, Any]]:
    """Word start times of a tutor response, in milliseconds of the response's audio (the clock the client's playback
    reports use, filler included), for captions and cues (VOC-09, VOC-51).

    Each transcript delta of the sidecar opens one spoken unit and is anchored where it arrived in the audio stream.
    The pinned sidecar sends a unit's text just after the unit's first audio frame (measured on this build's speech
    packs: one 100 ms frame), so an anchor is at most one frame late and never early. Inside a unit the words share its
    audio by syllables, the VOC-09 fallback synchroniser: those positions are estimates, and the turn records cue
    timing as estimated. A unit still arriving runs at the synchroniser's rate or over the audio already received for
    it, whichever is longer."""
    text = response.caption
    if not text or not response.marks:
        return []
    out: list[dict[str, Any]] = []
    starts = [max(pcm, response.offset_bytes) // 48 for pcm, _ in response.marks]
    lengths = [length for _, length in response.marks]
    for n, (start, length) in enumerate(zip(starts, lengths, strict=True)):
        begin = lengths[n - 1] if n else 0
        segment = text[begin:length]
        words = [(m.start() + begin, m.group()) for m in re.finditer(r"\S+", segment)]
        if not words:
            continue
        weights = [max(1, len(re.findall(r"[aeiouy]+", w.lower()))) for _, w in words]
        total = sum(weights)
        if n + 1 < len(starts):
            span = max(0, starts[n + 1] - start)
        elif response.done:
            span = max(0, response.pcm_bytes // 48 - start)
        else:
            # Still arriving: at least the synchroniser's length, and at least the audio already received for it
            # (synthesis runs ahead of playback), so a slow voice never pulls its words earlier than they are spoken.
            span = max(round(total * 1000 / SYLLABLES_PER_S), response.pcm_bytes // 48 - start)
        offset = 0
        for (_, word), weight in zip(words, weights, strict=True):
            if len(out) >= 400:
                return out
            out.append({"w": word[:64], "start_ms": start + offset * span // total})
            offset += weight
    return out
