"""Cloud voice: our hosted Realtime endpoint (VOC-28, D124) and the desktop voice tunnel.

  WS /v1/voice/realtime    Authorization: Bearer <access token>  (the same service credential as the cloud tier)

The device's voice bridge speaks the Realtime wire subset of VOC-25 here exactly as it does to its local sidecar, so
the bridge and the UI do not know which path they are on. The pipeline (D124, JM-D12):

  hearing   Muse Voice Transcribe realtime recognition (`providers/meta_speech.py`). Its speech events drive
            turn-taking and barge-in (`speech_started` / `speech_stopped`); its completed turns are the student's
            transcript.
  thinking  never a model here: each chat-completions request the local sidecar would send to the engine adapter
            (VOC-30) is forwarded down this connection to the device, whose engine runs the whole turn and every gate
            (VOC-31, VOC-32, VOC-33) and answers with the accepted spoken words only. Learner data stays on the device.
  voice     the verbatim synthesizer (Kokoro-82M) reads those accepted words, one sentence at
            a time, as they arrive (the Meta stack has no Live voice). A stack with a Live voice gives it the
            sentences under a fixed read-exactly instruction, never the student's audio; its output transcription is
            compared with the sentence as it streams: any departure beyond whitespace, punctuation and letter case
            cuts its audio before the next chunk is forwarded, and the rest of the turn is read by the verbatim
            synthesizer. Two departures in one connection switch the connection to the synthesizer only.

Every turn re-checks the credential and the quotas, as each request does, so a connection never outlives a revoked
credential or a spent cap. Under a deployment inference budget, speech charges are reserved before they are incurred
(a recognition stream's longest possible bill, each sentence's characters) and settled with what was used.

Latency (the Codex practices adopted by D124): the recognition stream and the Live session are opened when the
connection is, and `session.created` (which opens the device's microphone) waits for them up to a budget; audio that
arrives while a stream opens, rotates or resumes is held in memory and sent in order, so no first word is lost. An
adaptive idle timeout releases both upstream sessions when nobody has spoken for a while (never while a turn runs),
and a sound reopens them. Each reply's `response.done` carries per-phase timings in `speech_to_speech.turn_latency`.

Tunnel frames (gateway <-> device, on the Realtime connection, never seen by the bridge):
  -> {"type": "jana.voice_llm.request", "request_id": "vlr_…", "body": <chat-completions request>}
  -> {"type": "jana.voice_llm.cancel", "request_id": "vlr_…"}
  <- {"type": "jana.voice_llm.chunk", "request_id": "vlr_…", "data": "<one chat.completion.chunk as JSON text>"}
  <- {"type": "jana.voice_llm.done", "request_id": "vlr_…", "status": "ok" | "error"}

Zero retention (VOC-28, PRV-25, PRV-26): audio is held in memory only while it waits for a stream, and overwritten
when released; transcripts and spoken text live only while their utterance or reply runs. Nothing here writes audio,
text or a frame to disk or a log: the request ledger gets metadata only (model, audio seconds or characters, latency,
cost), and errors are logged by type."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import math
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from cloud_gateway.auth import AuthError, Session
from cloud_gateway.config import ModelSpec
from cloud_gateway.providers.base import UpstreamError, Usage
from cloud_gateway.providers.speech import Heard, LiveSession, Recognition, SpeechStack
from cloud_gateway.providers.vocabulary import SUBJECTS

FEATURE = "cloud_voice"
RATE = 24_000
BYTES_PER_MS = 48  # 24 kHz mono PCM16
WINDOW = 960  # 20 ms
OUT_FRAME = 24_000  # at most half a second of audio per output delta
STT_CHUNK = 4800  # 100 ms per recognition send
MAX_IN_EVENT = 6400  # base64 characters of one input append (the wire subset's bound, VOC-25)
MAX_FRAME = 64_000  # characters of one client text frame
MAX_REPLY = 8000  # characters of one spoken reply
DEPARTURES = 2  # departures from the text in one connection before Live is no longer used (D124)
BINDING = re.compile(r"^jana-voice-binding: vb_[0-9A-HJKMNP-TV-Z]{26}$")
SPEAK = re.compile(r"^jana-speak: utt_[0-9A-HJKMNP-TV-Z]{26}$")
REQUEST_ID = re.compile(r"^vlr_[0-9a-f]{24}$")
SPOKEN = re.compile(r"^[^\\$^_{}<>`*#|]*$")  # the adapter's spoken-text alphabet (engine LLM adapter contract)
SENTENCE = re.compile(r"[.!?]+[\"')\]]*\s+")  # a sentence ends at terminal punctuation followed by whitespace
CLIENT_EVENTS = ("input_audio_buffer.append", "session.update", "conversation.item.create", "response.create",
                 "response.cancel")  # fmt: skip
log = logging.getLogger("cloud_gateway.voice")


def _id(prefix: str) -> str:
    return prefix + secrets.token_hex(12)


def level(window: bytes) -> float:
    """RMS of a PCM16 window, 0 to 1."""
    count = len(window) // 2
    if not count:
        return 0.0
    samples = memoryview(window).cast("h")
    return math.sqrt(sum(s * s for s in samples) / count) / 32768


def _wipe(buffer: bytearray) -> None:
    buffer[:] = b"\0" * len(buffer)
    buffer.clear()


def sentences(pending: str) -> tuple[list[str], str]:
    """Complete sentences of `pending` (each with its trailing whitespace, so they concatenate back exactly) and the
    unfinished rest."""
    out, at = [], 0
    for match in SENTENCE.finditer(pending):
        out.append(pending[at : match.end()])
        at = match.end()
    return out, pending[at:]


class Reading:
    """Checks Live's output transcription against the sentence it was given (D124). The two are compared on letters
    and digits only, case folded: whitespace and punctuation may differ, nothing else may."""

    def __init__(self, sentence: str) -> None:
        self.sentence = sentence
        # The sentence's comparable characters and, for each, its index in the sentence.
        self.places = [i for i, c in enumerate(sentence) if c.isalnum()]
        self.expected = "".join(sentence[i] for i in self.places).casefold()
        self.heard = ""
        self.departed = False

    def hear(self, increment: str) -> bool:
        """Add an increment of the transcription; False once it is not a prefix of the sentence."""
        if not self.departed:
            self.heard += "".join(c for c in increment if c.isalnum()).casefold()
            self.departed = not self.expected.startswith(self.heard)
        return not self.departed

    def matched(self) -> int:
        """Comparable characters heard as written."""
        count = 0
        for a, b in zip(self.heard, self.expected, strict=False):
            if a != b:
                break
            count += 1
        return count

    def complete(self) -> bool:
        return not self.departed and self.heard == self.expected

    def confirmed(self) -> int:
        """Index in the sentence after the last word read exactly (0 if none)."""
        count = self.matched()
        if count == 0:
            return 0
        if count == len(self.expected):
            return len(self.sentence)
        nxt = self.places[count]  # the first character not yet heard
        start = self._word_start(nxt)
        return start

    def resume(self) -> int:
        """Where the verbatim voice takes over: the start of the first word not read exactly."""
        count = self.matched()
        if count >= len(self.expected):
            return len(self.sentence)
        return self._word_start(self.places[count])

    def _word_start(self, index: int) -> int:
        while index > 0 and not self.sentence[index - 1].isspace():
            index -= 1
        return index


@dataclass
class _Timing:
    """Per-phase clocks of one reply (content-free, VOC-22)."""

    speech_end: float | None = None  # the estimated end of the student's speech
    end_event: float | None = None  # when the recognizer said speech ended
    stopped: float | None = None  # when we decided the utterance was over (`speech_stopped`)
    transcribed: float | None = None  # the final transcript
    vad_decision_s: float = 0.0
    asked: float | None = None
    first_words: float | None = None
    first_audio: float | None = None
    voices: set[str] = field(default_factory=set)

    def latency(self, prewarm_s: float) -> dict[str, Any]:
        def span(a: float | None, b: float | None) -> float:
            return round(max(0.0, b - a), 3) if a is not None and b is not None else 0.0

        voice = "live_then_tts" if self.voices == {"live", "tts"} else next(iter(self.voices), "none")
        return {
            "vad_decision_s": round(self.vad_decision_s, 3),
            "hold_s": span(self.end_event, self.stopped),
            "stt_s": span(self.stopped, self.transcribed),
            "llm_s": span(self.asked, self.first_words),
            "tts_ttfa_s": span(self.first_words, self.first_audio),
            "e2e_s": span(self.speech_end if self.speech_end is not None else self.asked, self.first_audio),
            # Gateway extras (the device keeps only the fields it knows): which voice spoke and the prewarm time.
            "voice_out": voice,
            "prewarm_s": round(prewarm_s, 3),
        }


@dataclass(repr=False)
class _Utterance:
    item: str
    start_ms: int
    finals: list[str] = field(default_factory=list)
    partial: str = ""
    end_ms: int | None = None
    stopped: bool = False
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    timing: _Timing = field(default_factory=_Timing)

    def text(self) -> str:
        return " ".join(t for t in (*self.finals, self.partial) if t).strip()[:4000]


class _Ended(Exception):
    pass


class Listener:
    """The connection's streaming recognition: one open stream at a time, rotated before the provider's stream limit
    at a quiet moment, reopened when it ends, released when idle and resumed by sound. Audio that arrives while no
    stream is open is held (bounded) and sent first once one is: the student's first words are never lost."""

    def __init__(self, connection: VoiceConnection) -> None:
        self.c = connection
        self.config = connection.gw.config
        self.language = "en"
        self.stream: Recognition | None = None
        self.opening: asyncio.Task[None] | None = None
        self.readers: set[asyncio.Task[None]] = set()
        self.held = bytearray()
        self.held_from = 0  # connection audio position (bytes) of the first held byte
        self.position = 0  # connection audio position in bytes
        self.base = 0  # connection position of the open stream's first byte
        self.opened_at = 0.0
        self.sent: dict[int, int] = {}  # bytes sent per stream (metered as audio seconds)
        self.failures = 0
        self.suspended = False
        self.preroll = bytearray()
        self._pending = bytearray()
        self._loud = 0
        self.closed = False

    def ms(self, position: int) -> int:
        return position // BYTES_PER_MS

    def start(self) -> asyncio.Task[None]:
        self.held_from = self.position
        self.opening = asyncio.create_task(self._open())
        return self.opening

    async def feed(self, pcm: bytes) -> None:
        if self.closed:
            return
        self.position += len(pcm)
        if self.suspended:
            self._wake(pcm)
            return
        if self.stream is None:
            self._hold(pcm)
            if self.opening is None or self.opening.done():
                self.opening = asyncio.create_task(self._open())
            return
        stream = self.stream
        try:
            await self._send(stream, pcm)
        except UpstreamError:
            # The stream closed under us (its session cap or a rate limit) before its reader saw it: this audio is
            # held for the next stream, which opens now.
            if self.stream is stream:
                self.stream = None
                self._close_later(stream)
            self._hold(pcm)
            if self.opening is None or self.opening.done():
                self.opening = asyncio.create_task(self._open())
            return
        if self.c.utterance is None and self.c.gw.clock() - self.opened_at >= self.config.voice_stt_rotate_s:
            self.rotate()

    def _hold(self, pcm: bytes) -> None:
        if not self.held:
            self.held_from = self.position - len(pcm)
        self.held += pcm
        cap = self.config.voice_first_words_ms * BYTES_PER_MS
        if len(self.held) > cap:  # the oldest audio goes first; overwritten before it is released
            drop = len(self.held) - cap
            self.held[:drop] = b"\0" * drop
            del self.held[:drop]
            self.held_from += drop

    def _wake(self, pcm: bytes) -> None:
        """While released: keep `voice_preroll_ms` of audio and reopen on two loud windows (a cheap energy gate)."""
        self.preroll += pcm
        cap = self.config.voice_preroll_ms * BYTES_PER_MS
        if len(self.preroll) > cap:
            drop = len(self.preroll) - cap
            self.preroll[:drop] = b"\0" * drop
            del self.preroll[:drop]
        self._pending += pcm
        while len(self._pending) >= WINDOW:
            window = bytes(self._pending[:WINDOW])
            self._pending[:WINDOW] = b"\0" * WINDOW
            del self._pending[:WINDOW]
            self._loud = self._loud + 1 if level(window) >= 0.015 else 0
            if self._loud >= 2:
                self.resume()
                return

    def resume(self) -> None:
        if not self.suspended or self.closed:
            return
        self.suspended, self._loud = False, 0
        _wipe(self._pending)
        self.held += self.preroll  # the sound that woke us, and what led into it, goes to the new stream first
        self.held_from = self.position - len(self.preroll)
        _wipe(self.preroll)
        self.c.resumed()
        self.opening = asyncio.create_task(self._open())

    async def suspend(self) -> None:
        if self.suspended or self.closed:
            return
        self.suspended = True
        await self._release()
        _wipe(self.held)

    def rotate(self) -> None:
        """Close the open stream and open the next; audio meanwhile is held."""
        old, self.stream = self.stream, None
        if old is not None:
            self._close_later(old)
        if self.opening is None or self.opening.done():
            self.held_from = self.position
            self.opening = asyncio.create_task(self._open())

    def restart(self, language: str) -> None:
        if language != self.language:
            self.language = language
            if not self.suspended:
                self.rotate()

    async def _open(self) -> None:
        recognizer = self.c.stack.recognizer
        assert recognizer is not None
        while not self.closed and not self.suspended:
            try:
                hold = self.c.service.reserve_stt()
            except UpstreamError:
                await self.c.fail("quota_exceeded")
                return
            try:
                stream = await asyncio.wait_for(recognizer.open(self.language, self.c.subject),
                                                self.config.voice_connect_timeout_s)  # fmt: skip
            except asyncio.CancelledError:  # released (idle, barge-in, close) while opening
                self.c.service.release(hold)  # a stream that never opened bills nothing
                raise
            except (UpstreamError, TimeoutError) as failure:
                self.c.service.release(hold)
                self.failures += 1
                log.warning("voice recognition open failed %s", getattr(failure, "reason", type(failure).__name__))
                if self.failures >= 3:
                    await self.c.fail("speech_to_text_unavailable")
                    return
                await asyncio.sleep(0.2 * self.failures)
                continue
            if self.closed or self.suspended:
                self.c.service.release(hold)  # before the await: a cancellation there must not keep it
                await self._close(stream)
                return
            self.failures = 0
            self.base = self.held_from if self.held else self.position
            self.opened_at = self.c.gw.clock()
            reader = asyncio.create_task(self._read(stream, self.base, hold))
            self.readers.add(reader)
            reader.add_done_callback(self.readers.discard)
            try:
                while self.held:  # what arrived meanwhile, in order; feed() keeps holding until the stream is set
                    chunk = bytes(self.held[:STT_CHUNK])
                    self.held[: len(chunk)] = b"\0" * len(chunk)
                    del self.held[: len(chunk)]
                    await self._send(stream, chunk)
            except UpstreamError:
                await self._close(stream)
                continue
            except asyncio.CancelledError:  # released while feeding held audio: the reader settles the hold
                self._close_later(stream)
                raise
            self.stream = stream
            return

    async def _send(self, stream: Recognition, pcm: bytes) -> None:
        for at in range(0, len(pcm), STT_CHUNK):
            await stream.send(pcm[at : at + STT_CHUNK])
        self.sent[id(stream)] = self.sent.get(id(stream), 0) + len(pcm)

    async def _read(self, stream: Recognition, base: int, hold: str | None) -> None:
        started, outcome = self.c.gw.clock(), "ok"
        try:
            async for heard in stream.events():
                await self.c.heard(heard, self.ms(base) + max(0, heard.offset_ms))
        except UpstreamError as failure:
            outcome = failure.reason
        except _Ended:
            return
        finally:
            # Silence a recognizer sent to keep its stream at real time is billed like the student's audio.
            sent = self.sent.pop(id(stream), 0) + getattr(stream, "padded_bytes", 0)
            self.c.service.meter_stt(self.c.session, started, sent / (BYTES_PER_MS * 1000), outcome, hold)
        if stream is self.stream and not self.closed:
            self.stream = None  # the provider ended the stream (its time limit): open the next, holding audio
            if self.opening is None or self.opening.done():
                self.held_from = self.position
                self.opening = asyncio.create_task(self._open())

    def _close_later(self, stream: Recognition) -> None:
        task = asyncio.create_task(self._close(stream))
        self.c.background.add(task)
        task.add_done_callback(self.c.background.discard)

    async def _close(self, stream: Recognition) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(stream.close(), 2)

    async def _release(self) -> None:
        opening, self.opening = self.opening, None
        if opening is not None and not opening.done() and opening is not asyncio.current_task():
            opening.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await opening
        stream, self.stream = self.stream, None
        if stream is not None:
            await self._close(stream)

    async def close(self) -> None:
        self.closed = True
        await self._release()
        for reader in list(self.readers):
            reader.cancel()
        await asyncio.gather(*self.readers, return_exceptions=True)
        for buffer in (self.held, self.preroll, self._pending):
            _wipe(buffer)


class _Out:
    """The audio and transcript of one reply, in wire events. Transcript deltas follow the audio they belong to
    (VOC-09), so heard text lines up with what was played (VOC-36); they are always a prefix of the accepted words."""

    def __init__(self, connection: VoiceConnection, response_id: str, timing: _Timing) -> None:
        self.c, self.response_id, self.timing = connection, response_id, timing
        self.place = {"response_id": response_id, "item_id": _id("item_"), "output_index": 0, "content_index": 0}
        self.spoken = ""  # the accepted words received so far
        self.said = 0  # characters of them sent as transcript
        self.cursor = 0  # where the next sentence starts in `spoken`
        self.audio_bytes = 0
        self.tts = False  # the rest of this reply is read by the verbatim voice

    async def created(self) -> None:
        self.c.response_id = self.response_id
        await self.c.emit({"type": "response.created", "response": {
            "id": self.response_id, "object": "realtime.response", "status": "in_progress",
            "output_modalities": ["audio"]}})  # fmt: skip

    async def audio(self, pcm: bytes, voice: str) -> None:
        if not pcm:
            return
        if self.timing.first_audio is None:
            self.timing.first_audio = self.c.gw.clock()
        self.timing.voices.add(voice)
        for at in range(0, len(pcm), OUT_FRAME):
            await self.c.emit({"type": "response.output_audio.delta", **self.place,
                               "delta": base64.b64encode(pcm[at : at + OUT_FRAME]).decode()})  # fmt: skip
        self.audio_bytes += len(pcm)

    async def caption(self, upto: int) -> None:
        upto = min(upto, len(self.spoken))
        if upto > self.said:
            delta, self.said = self.spoken[self.said : upto], upto
            await self.c.emit({"type": "response.output_audio_transcript.delta", **self.place, "delta": delta})


class VoiceConnection:
    """One device's Realtime connection: one bridge, one tutor session at a time (VOC-24)."""

    def __init__(self, service: VoiceService, socket: WebSocket, session: Session) -> None:
        self.service, self.socket, self.session = service, socket, session
        self.gw = service.gw
        self.stack: SpeechStack = service.stack
        self.instructions: str | None = None
        self.interrupt = True
        self.silence_ms = 500
        self.directive: str | None = None
        self.work: asyncio.Task[None] | None = None
        self.response_id: str | None = None
        self.replies: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self.listener = Listener(self)
        self.utterance: _Utterance | None = None
        self.ending: asyncio.Task[None] | None = None
        self.background: set[asyncio.Task[Any]] = set()
        # The voice: one prewarmed Live session, replaced after a cut, a barge-in or `voice_live_max_sentences`.
        self.live: LiveSession | None = None
        self.live_opening: asyncio.Task[LiveSession | None] | None = None
        self.live_sentences = 0
        self.departures = 0
        # A Live voice is billed by tokens only after it speaks, so a budgeted deployment reads with the synthesizer.
        self.live_off = self.stack.live is None or self.gw.inference_budget is not None
        self.authorization = socket.headers.get("authorization")
        self.refused: int | None = None  # the close code when a turn's re-check refused the connection
        # The session's subject, when the device named it (`?subject=`): it orders speech to text's vocabulary bias.
        subject = socket.query_params.get("subject") if hasattr(socket, "query_params") else None
        self.subject: str | None = subject if subject in SUBJECTS else None
        self.prewarm_s = 0.0
        # The adaptive idle timeout: the longest quiet stretch before the student spoke, times a factor.
        self.last_active = self.gw.clock()
        self.longest_quiet = 0.0
        self.failed = asyncio.Event()
        self.failure: str | None = None
        self._send = asyncio.Lock()
        self.closed = False

    # --- the wire ------------------------------------------------------------------------------------------------

    async def emit(self, event: dict[str, Any]) -> None:
        if self.closed:
            raise _Ended
        async with self._send:
            await self.socket.send_text(json.dumps({**event, "event_id": _id("evt_")}, separators=(",", ":")))

    async def error(self, code: str, message: str) -> None:
        await self.emit({"type": "error", "error": {"type": "invalid_request_error", "code": code,
                                                     "message": message}})  # fmt: skip

    async def fail(self, reason: str) -> None:
        """An upstream service is gone for good: end the connection so the device falls back to local (VOC-29)."""
        if self.failure is None:
            self.failure = reason
            with contextlib.suppress(_Ended, Exception):
                await self.emit({"type": "error", "error": {"type": "server_error", "code": reason,
                                                             "message": "Online voice is unavailable."}})  # fmt: skip
            self.failed.set()

    async def prewarm(self) -> None:
        """Open the recognition stream and the Live session before the device's microphone opens (D124)."""
        started = self.gw.clock()
        opening: list[asyncio.Task[Any]] = [self.listener.start()]
        if not self.live_off:
            opening.append(self._open_live())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(asyncio.gather(*opening, return_exceptions=True)),
                                   self.gw.config.voice_prewarm_s)  # fmt: skip
        self.prewarm_s = self.gw.clock() - started

    async def run(self) -> None:
        deadline = self.gw.clock() + self.gw.config.voice_max_session_s
        await self.prewarm()
        await self.emit({"type": "session.created", "session": {"type": "realtime", "object": "realtime.session",
                                                                 "id": _id("sess_")}})  # fmt: skip
        idle = asyncio.create_task(self._idle())
        try:
            while not self.failed.is_set():
                remaining = deadline - self.gw.clock()
                if remaining <= 0:
                    return
                receive = asyncio.ensure_future(self.socket.receive())
                failed = asyncio.ensure_future(self.failed.wait())
                done, _ = await asyncio.wait({receive, failed}, timeout=min(remaining, 60),
                                             return_when=asyncio.FIRST_COMPLETED)  # fmt: skip
                failed.cancel()
                if receive not in done:
                    receive.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await receive
                    continue
                packet = receive.result()
                if packet["type"] == "websocket.disconnect":
                    return
                text = packet.get("text")
                if text is None or len(text) > MAX_FRAME:
                    await self.error("invalid_frame", "Only JSON text frames are accepted.")
                    return
                try:
                    event = json.loads(text)
                except ValueError:
                    event = None
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    await self.error("invalid_frame", "A frame is one JSON object with a type.")
                    return
                await self.dispatch(event)
        finally:
            idle.cancel()

    async def dispatch(self, event: dict[str, Any]) -> None:
        kind = event["type"]
        if kind in ("jana.voice_llm.chunk", "jana.voice_llm.done"):
            queue = self.replies.get(str(event.get("request_id")))
            if queue is not None:
                queue.put_nowait(event)
            return
        if kind not in CLIENT_EVENTS:
            await self.error("unsupported_event", "That event is not part of this endpoint's subset.")
            return
        if kind == "input_audio_buffer.append":
            await self.audio(event.get("audio"))
        elif kind == "session.update":
            await self.configure(event.get("session"))
        elif kind == "conversation.item.create":
            item = event.get("item")
            content = item.get("content") if isinstance(item, dict) else None
            text = content[0].get("text") if isinstance(content, list) and len(content) == 1 and isinstance(
                content[0], dict) else None  # fmt: skip
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                return  # VOC-38: nothing here calls a function; the device's answer to a stray call needs no work
            if not isinstance(text, str) or not SPEAK.match(text):
                await self.error("invalid_item", "Only engine-owned speak directives are accepted.")
                return
            self.directive = text
            await self.emit({"type": "conversation.item.created", "item": {"id": _id("item_"), "type": "message",
                                                                           "role": "user"}})  # fmt: skip
        elif kind == "response.create":
            if self.directive is None:
                await self.error("no_directive", "A response follows a speak directive only.")
                return
            directive, self.directive = self.directive, None
            await self.cancel("client_cancelled")
            if await self.admitted():
                self.work = asyncio.create_task(self.respond(directive, _Timing()))
        elif kind == "response.cancel":
            await self.cancel("client_cancelled")

    async def configure(self, config: Any) -> None:
        if not isinstance(config, dict) or config.get("type") != "realtime" or config.get("tools") not in (None, []):
            await self.error("invalid_session", "Realtime sessions here take no tools.")
            return
        instructions = config.get("instructions")
        if instructions is not None:
            if not isinstance(instructions, str) or not BINDING.match(instructions):
                await self.error("invalid_session", "Instructions carry a voice binding only (VOC-31).")
                return
            if instructions != self.instructions:
                await self.cancel("client_cancelled")  # a new binding never inherits the old one's reply
            self.instructions = instructions
        audio: dict[str, Any] = config["audio"] if isinstance(config.get("audio"), dict) else {}
        given: dict[str, Any] = audio["input"] if isinstance(audio.get("input"), dict) else {}
        transcription = given.get("transcription")
        language = transcription.get("language") if isinstance(transcription, dict) else None
        if language in ("en", "fr", "pt", "ar"):
            self.listener.restart(language)
        detection = given.get("turn_detection")
        if isinstance(detection, dict) and detection.get("type") == "server_vad":
            silence = detection.get("silence_duration_ms")
            if isinstance(silence, int) and 32 <= silence <= 10_000 and silence != self.silence_ms:
                self.silence_ms = silence
                self._rehold()
            if isinstance(detection.get("interrupt_response"), bool):
                self.interrupt = detection["interrupt_response"]
        await self.emit({"type": "session.updated", "session": {"type": "realtime"}})

    async def audio(self, encoded: Any) -> None:
        if not isinstance(encoded, str) or len(encoded) > MAX_IN_EVENT:
            await self.error("invalid_audio", "Audio comes in bounded base64 PCM16 appends.")
            return
        try:
            pcm = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            await self.error("invalid_audio", "Audio is base64 PCM16.")
            return
        if len(pcm) % 2:
            await self.error("invalid_audio", "Audio is base64 PCM16.")
            return
        await self.listener.feed(pcm)
        utterance = self.utterance
        if utterance is not None and not utterance.stopped:
            length = self.listener.ms(self.listener.position) - utterance.start_ms
            if length >= self.gw.config.voice_max_utterance_s * 1000 and (self.ending is None or self.ending.done()):
                utterance.end_ms = self.listener.ms(self.listener.position)
                self.ending = asyncio.create_task(self._stop(utterance))  # an utterance never outgrows its bound

    async def cancel(self, reason: str) -> None:
        work, self.work = self.work, None
        if work is not None and not work.done():
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError, _Ended):
                await work
        if self.response_id is not None:
            response_id, self.response_id = self.response_id, None
            with contextlib.suppress(_Ended):
                await self.emit({"type": "response.done", "response": {
                    "id": response_id, "object": "realtime.response", "status": "cancelled",
                    "status_details": {"type": "cancelled", "reason": reason}}})  # fmt: skip

    # --- hearing: recognition events to turn-taking ----------------------------------------------------------------

    async def heard(self, heard: Heard, at_ms: int) -> None:
        utterance = self.utterance
        if heard.kind == "begin":
            if utterance is not None and utterance.stopped:
                await self._complete(utterance)  # a new utterance: the last one is final with what it has
                utterance = None
            if utterance is None:
                await self._begin(at_ms)
            elif self.ending is not None and not self.ending.done():
                self.ending.cancel()  # speech went on within the session's silence: the same utterance
                utterance.end_ms = None
        elif heard.kind in ("partial", "final"):
            if not heard.text.strip():
                if heard.kind == "final" and utterance is not None:
                    utterance.partial = ""
                    utterance.settled.set()
                return
            if utterance is None:
                utterance = await self._begin(at_ms)  # a result without an activity event still starts a turn
            if heard.kind == "partial":
                utterance.partial = heard.text.strip()
                return
            text = heard.text.strip()
            delta = (" " if utterance.finals else "") + text
            utterance.finals.append(text)
            utterance.partial = ""
            utterance.settled.set()
            await self.emit({"type": "conversation.item.input_audio_transcription.delta", "item_id": utterance.item,
                             "content_index": 0, "delta": delta})  # fmt: skip
        elif heard.kind == "end" and utterance is not None and not utterance.stopped:
            now = self.gw.clock()
            utterance.end_ms = at_ms
            utterance.timing.end_event = now
            utterance.timing.vad_decision_s = max(0.0, (self.listener.ms(self.listener.position) - at_ms) / 1000)
            utterance.timing.speech_end = now - utterance.timing.vad_decision_s
            if self.ending is None or self.ending.done():
                self.ending = asyncio.create_task(self._hold(utterance))

    async def _begin(self, at_ms: int) -> _Utterance:
        now = self.gw.clock()
        if self.work is None or self.work.done():
            self.longest_quiet = max(self.longest_quiet, now - self.last_active)
        self.last_active = now
        utterance = _Utterance(_id("item_"), at_ms)
        self.utterance = utterance
        if self.interrupt or self.response_id is None:
            # Barge-in: a new utterance supersedes a reply or a turn still being worked out (VOC-05, VOC-11). The
            # Live turn is cancelled with the reply; the device truncates at the played position (VOC-36).
            await self.cancel("turn_detected")
        await self.emit({"type": "input_audio_buffer.speech_started", "audio_start_ms": at_ms,
                         "item_id": utterance.item})  # fmt: skip
        return utterance

    async def _hold(self, utterance: _Utterance, since: float | None = None) -> None:
        """The recognizer's end of speech, held for the rest of the session's `silence_duration_ms` (hold-to-think
        sends ten seconds), and at least `min_hold_ms`: speech within it continues the same utterance, so a question
        spoken as two sentences stays one turn. `since`: the hold started then (a re-timed hold)."""
        config = self.gw.config
        hold = max(config.voice_min_hold_ms, self.silence_ms - config.voice_end_ms) / 1000
        if since is not None:
            hold -= self.gw.clock() - since
        if hold > 0:
            await asyncio.sleep(hold)
        await self._stop(utterance)

    def _rehold(self) -> None:
        """The silence changed while an utterance is held after its end of speech (hold-to-dictate's release sends the
        short silence after the ten seconds it held with): the hold runs on with the new length, from the same start."""
        utterance = self.utterance
        if (utterance is None or utterance.stopped or utterance.timing.end_event is None or self.ending is None
                or self.ending.done()):  # fmt: skip
            return
        self.ending.cancel()
        self.ending = asyncio.create_task(self._hold(utterance, since=utterance.timing.end_event))

    async def _stop(self, utterance: _Utterance) -> None:
        if utterance.stopped or self.utterance is not utterance:
            return
        utterance.stopped = True
        utterance.timing.stopped = self.gw.clock()
        if utterance.timing.end_event is None:
            utterance.timing.end_event = utterance.timing.speech_end = utterance.timing.stopped
        end_ms = utterance.end_ms if utterance.end_ms is not None else self.listener.ms(self.listener.position)
        await self.emit({"type": "input_audio_buffer.speech_stopped", "audio_end_ms": max(end_ms, utterance.start_ms),
                         "item_id": utterance.item})  # fmt: skip
        if utterance.partial or not utterance.finals:
            utterance.settled.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(utterance.settled.wait(), self.gw.config.voice_final_wait_ms / 1000)
        await self._complete(utterance)

    async def _complete(self, utterance: _Utterance) -> None:
        if self.utterance is not utterance:
            return
        self.utterance = None
        if self.ending is not None and self.ending is not asyncio.current_task() and not self.ending.done():
            self.ending.cancel()
        text = utterance.text()
        utterance.finals.clear()
        utterance.partial = ""
        utterance.timing.transcribed = self.gw.clock()
        if utterance.timing.stopped is None:
            utterance.timing.stopped = utterance.timing.transcribed
        await self.emit({"type": "conversation.item.input_audio_transcription.completed", "item_id": utterance.item,
                         "content_index": 0, "transcript": text,
                         "usage": {"type": "duration", "seconds": 0}})  # fmt: skip
        await self.emit({"type": "conversation.item.created", "item": {"id": utterance.item, "type": "message",
                                                                       "role": "user"}})  # fmt: skip
        self.last_active = self.gw.clock()
        if not text:
            return
        if self.interrupt or self.response_id is None:
            await self.cancel("turn_detected")
        elif self.work is not None and not self.work.done():
            return  # the reply plays on: no barge-in without echo cancellation (VOC-11)
        if await self.admitted():
            self.work = asyncio.create_task(self.respond(text, utterance.timing))

    async def admitted(self) -> bool:
        """Re-check the credential and the quotas before a turn, as every request is checked (§6.1 review finding:
        a connection admitted once must not run past a revocation, a lapse or a spent cap). A refusal ends the
        connection with the code a new connection would get."""
        code = self.service.refusal(self.authorization)[0]
        if code is None:
            return True
        self.refused = code
        await self.fail("quota_exceeded" if code == 4429 else "unauthorized" if code == 4401 else "voice_unavailable")
        return False

    # --- idle: release the upstream sessions while nobody speaks ----------------------------------------------------

    def idle_timeout(self) -> float:
        config = self.gw.config
        return float(
            min(config.voice_idle_max_s, max(config.voice_idle_min_s, config.voice_idle_factor * self.longest_quiet))
        )

    def busy(self) -> bool:
        return (self.utterance is not None or self.response_id is not None
                or (self.work is not None and not self.work.done()))  # fmt: skip

    async def _idle(self) -> None:
        while True:
            await asyncio.sleep(self.gw.config.voice_idle_tick_s)
            now = self.gw.clock()
            if self.busy():
                self.last_active = now  # the timeout is suspended while a turn runs
            elif not self.listener.suspended and now - self.last_active >= self.idle_timeout():
                await self.listener.suspend()
                await self._drop_live(reopen=False)

    def resumed(self) -> None:
        """A sound after an idle release: the recognition stream reopens (the listener) and so does the voice."""
        self.last_active = self.gw.clock()
        if not self.live_off and self.live is None and (self.live_opening is None or self.live_opening.done()):
            self._open_live()

    # --- the voice: Live reads accepted sentences, checked; Cloud Text-to-Speech reads verbatim ----------------------

    def _open_live(self) -> asyncio.Task[LiveSession | None]:
        async def opening() -> LiveSession | None:
            live = self.stack.live
            if live is None or self.live_off:
                return None
            try:
                session = await asyncio.wait_for(live.open(), self.gw.config.voice_connect_timeout_s)
            except (UpstreamError, TimeoutError) as failure:
                log.warning("voice live open failed %s", getattr(failure, "reason", type(failure).__name__))
                return None
            if self.closed or self.live_off:
                await _close_quietly(session)
                return None
            self.live, self.live_sentences = session, 0
            return session

        self.live_opening = asyncio.create_task(opening())
        return self.live_opening

    async def _live_session(self) -> LiveSession | None:
        if self.live_off:
            return None
        if self.live is not None:
            return self.live
        opening = self.live_opening if self.live_opening is not None else self._open_live()
        if opening.done() and (opening.cancelled() or opening.result() is None):
            opening = self._open_live()  # the last attempt failed: one more, within the wait budget
        try:
            return await asyncio.wait_for(asyncio.shield(opening), self.gw.config.voice_live_wait_s)
        except TimeoutError:
            return None

    async def _drop_live(self, *, reopen: bool) -> None:
        """Discard the Live session (its turn is cut) and, unless Live is off or idle, prewarm the next."""
        session, self.live = self.live, None
        opening, self.live_opening = self.live_opening, None
        if opening is not None and not opening.done():
            opening.cancel()
        if session is not None:
            task = asyncio.create_task(_close_quietly(session))
            self.background.add(task)
            task.add_done_callback(self.background.discard)
        if reopen and not self.live_off and not self.closed:
            self._open_live()

    async def say(self, sentence: str, out: _Out) -> None:
        start = out.cursor
        out.cursor += len(sentence)
        if not sentence.strip():
            await out.caption(out.cursor)
            return
        rest = 0
        if not out.tts:
            spoken = await self._live_say(sentence, start, out)
            if spoken is None:
                return
            rest = spoken
            out.tts = True  # once the verbatim voice has spoken in a reply, it reads the rest of it
        await self._tts_say(sentence[rest:], start + rest, out)

    async def _live_say(self, sentence: str, start: int, out: _Out) -> int | None:
        """Read one sentence with Live; None when it was read exactly, else where the verbatim voice takes over."""
        session = await self._live_session()
        if session is None:
            return 0
        reading = Reading(sentence)
        self.live_sentences += 1
        began, usage, outcome, finished, played = self.gw.clock(), Usage(), "ok", False, False
        try:
            async with contextlib.aclosing(session.say(sentence.strip())) as stream:  # type: ignore[type-var]
                async for voiced in stream:
                    if voiced.usage is not None:
                        usage = voiced.usage
                    if voiced.transcript and not reading.hear(voiced.transcript):
                        break  # departed: this message's audio and everything after it are never forwarded
                    if voiced.audio:
                        played = True
                        await out.audio(voiced.audio, "live")
                        await out.caption(start + reading.confirmed())
                    if voiced.done:
                        finished = True
                        break
        except UpstreamError as failure:
            outcome = failure.reason
        except asyncio.CancelledError:
            await self._drop_live(reopen=True)  # barge-in or stop: Live's turn is cancelled with the reply
            raise
        finally:
            self.service.meter_live(self.session, began, usage, outcome)
        if reading.complete() and finished:
            await out.caption(start + len(sentence))
            if self.live_sentences >= self.gw.config.voice_live_max_sentences:
                await self._drop_live(reopen=True)  # a fresh session keeps its context small
            return None
        if outcome == "ok":  # a departure: different words, or words left out
            self.departures += 1
            if self.departures >= DEPARTURES:
                self.live_off = True
            log.info("voice live departure count=%d", self.departures)  # a count, never the words
        await self._drop_live(reopen=True)
        return reading.resume() if played else 0

    async def _tts_say(self, text: str, start: int, out: _Out) -> None:
        if not text.strip():
            await out.caption(start + len(text))
            return
        tts = self.stack.tts
        assert tts is not None
        hold = self.service.reserve_tts(len(text.strip()))  # raises UpstreamError when the budget is spent
        began, outcome, first = self.gw.clock(), "ok", True
        try:
            async with contextlib.aclosing(tts.synthesize(text.strip())) as stream:  # type: ignore[type-var]
                async for pcm in stream:
                    await out.audio(pcm, "tts")
                    if first and pcm:
                        first = False
                        await out.caption(start + len(text))  # the unit's words follow its first audio (VOC-09)
            await out.caption(start + len(text))
        except UpstreamError as failure:
            outcome = failure.reason
            raise
        finally:
            self.service.meter_tts(self.session, began, len(text.strip()), outcome, hold)

    async def respond(self, user_text: str, timing: _Timing) -> None:
        """Ask the device for the turn's spoken words and speak them. `response.created` follows the first accepted
        words, so the device has authorised the reply before its id exists (VOC-33, VOC-34)."""
        request_id = "vlr_" + secrets.token_hex(12)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self.replies[request_id] = queue
        body = {"model": "jana-tutor-voice", "stream": True, "reasoning_effort": "none",
                "messages": [{"role": "system", "content": self.instructions or "jana-voice-unbound"},
                             {"role": "user", "content": user_text}]}  # fmt: skip
        response_id = _id("resp_")
        out = _Out(self, response_id, timing)
        status, pending = "completed", ""
        timing.asked = self.gw.clock()
        try:
            await self.emit({"type": "jana.voice_llm.request", "request_id": request_id, "body": body})
            while True:
                reply = await asyncio.wait_for(queue.get(), timeout=self.gw.config.voice_llm_timeout_s)
                if reply["type"] == "jana.voice_llm.done":
                    if reply.get("status") != "ok":
                        status = "failed"
                    break
                words = _content(reply.get("data"))
                if not words:
                    continue
                if len(out.spoken) + len(words) > MAX_REPLY or not SPOKEN.match(words):
                    status = "failed"
                    break
                if self.response_id is None:
                    timing.first_words = self.gw.clock()
                    await out.created()
                out.spoken += words
                done, pending = sentences(pending + words)
                for sentence in done:  # the voice is given whole sentences, one at a time (D124)
                    await self.say(sentence, out)
            if status == "completed" and pending:
                await self.say(pending, out)
        except (TimeoutError, UpstreamError) as failure:
            log.warning("voice reply failed %s", getattr(failure, "reason", type(failure).__name__))
            status = "failed"
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                async with self._send:
                    await self.socket.send_text(json.dumps({"type": "jana.voice_llm.cancel",
                                                            "request_id": request_id}))  # fmt: skip
            raise
        finally:
            self.replies.pop(request_id, None)
        if self.response_id != response_id:
            # Nothing was accepted to say (dictation, a steered or refused turn): the sidecar's empty reply.
            await self.emit({"type": "response.created", "response": {"id": response_id,
                                                                       "object": "realtime.response",
                                                                       "status": "in_progress"}})  # fmt: skip
            self.response_id = response_id
        elif out.spoken and status == "completed":
            await out.caption(len(out.spoken))
            await self.emit({"type": "response.output_audio_transcript.done", **out.place, "transcript": out.spoken})
            await self.emit({"type": "response.output_audio.done", **out.place})
        latency = timing.latency(self.prewarm_s)
        self.response_id = None
        self.last_active = self.gw.clock()
        done_event: dict[str, Any] = {"id": response_id, "object": "realtime.response", "status": status,
                                      "metadata": {"speech_to_speech.turn_latency": json.dumps(latency)}}  # fmt: skip
        if status != "completed":
            done_event["status_details"] = {"type": "failed", "error": {"type": "server_error",
                                                                        "code": "voice_reply"}}  # fmt: skip
        await self.emit({"type": "response.done", "response": done_event})

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.cancel("client_cancelled")
        self.closed = True
        if self.ending is not None:
            self.ending.cancel()
        self.utterance = None
        await self.listener.close()
        await self._drop_live(reopen=False)
        await asyncio.gather(*self.background, return_exceptions=True)
        self.replies.clear()


async def _close_quietly(session: LiveSession) -> None:
    with contextlib.suppress(Exception):
        await asyncio.wait_for(session.close(), 2)


def _content(data: Any) -> str:
    """The spoken words of one forwarded chat.completion.chunk ('' for anything else)."""
    if not isinstance(data, str) or len(data) > 16_384:
        return ""
    try:
        chunk = json.loads(data)
        choices = chunk.get("choices") or []
        delta = choices[0].get("delta") or {} if choices else {}
        words = delta.get("content")
    except (ValueError, AttributeError, IndexError, TypeError):
        return ""
    return words if isinstance(words, str) else ""


class VoiceService:
    def __init__(self, gw: Any, stack: SpeechStack) -> None:
        self.gw = gw
        self.stack = stack
        self.live: dict[str, VoiceConnection] = {}  # credential id -> its one connection

    def available(self) -> bool:
        from cloud_gateway.providers.speech_selection import allowed

        stack = self.stack
        return (stack.recognizer is not None and stack.tts is not None
                and allowed(self.gw.config.speech.recognition_permission, self.gw.clock())
                and allowed(self.gw.config.speech.synthesis_permission, self.gw.clock())
                and not self.gw.operations.blocked(stack.recognizer_provider, stack.recognizer_model, "")
                and not self.gw.operations.blocked(stack.synthesizer_provider, stack.tts.voice, ""))  # fmt: skip

    def refusal(self, authorization: str | None) -> tuple[int | None, Session | None]:
        """The close code refusing a connection or a turn now (None when admitted), and the credential's session."""
        try:
            session = self.gw.auth.authenticate(authorization)
        except AuthError:
            return 4401, None
        now = self.gw.clock()
        if self.gw.operations.blocked("", "", session.licence_id) or not self.available():
            return 4503, session
        device_hash = self.gw.store.device_hash(session.account, now)
        over = self.gw.quotas.refusal(session.account, device_hash, session.licence_id, now,
                                      period_start=session.period_start, seats=session.seats)  # fmt: skip
        return (4429 if over is not None else None), session

    # --- the deployment inference budget: holds for speech charges, settled with what was billed --------------------

    def _reserve(self, usd: float, role: str, model: str) -> str | None:
        budget = self.gw.inference_budget
        return budget.reserve_flat(usd, role=role, model=model) if budget is not None else None

    def reserve_stt(self) -> str | None:
        """A recognition stream's longest bill: Voice Transcribe closes a session at 60 minutes."""
        recognizer = self.stack.recognizer
        assert recognizer is not None
        return self._reserve(61 * recognizer.minute_usd(), "voice_stt", self.stack.recognizer_model)

    def reserve_tts(self, characters: int) -> str | None:
        tts = self.stack.tts
        assert tts is not None
        return self._reserve(characters / 1_000_000 * tts.million_chars_usd(), "voice_tts", tts.voice)

    def release(self, hold: str | None) -> None:
        if hold is not None and self.gw.inference_budget is not None:
            self.gw.inference_budget.release(hold)

    def _settle(self, hold: str | None, cost: float) -> None:
        if hold is not None and self.gw.inference_budget is not None:
            self.gw.inference_budget.settle_flat(hold, cost)

    def _meter(self, session: Session, provider: str, model_id: str, role: str, started: float, usage: Usage,
               outcome: str, cost: float | None) -> None:  # fmt: skip
        from cloud_gateway.app import _Call

        device_hash = self.gw.store.device_hash(session.account, started)
        call = _Call(session, device_hash, provider, None, ModelSpec(id=model_id), started, role,  # type: ignore[arg-type]
                     feature=FEATURE)  # fmt: skip
        self.gw.finish(call, 200 if outcome == "ok" else 502, outcome, usage, cost=cost)

    def meter_stt(self, session: Session, started: float, seconds: float, outcome: str,
                  hold: str | None = None) -> None:  # fmt: skip
        recognizer = self.stack.recognizer
        if recognizer is None or (seconds <= 0 and outcome == "ok"):
            self.release(hold)
            return
        # Billed by the second ($0.18 per audio hour for Voice Transcribe), rounded up per stream.
        cost = math.ceil(seconds) / 60 * recognizer.minute_usd()
        self._settle(hold, cost)
        self._meter(
            session,
            self.stack.recognizer_provider,
            self.stack.recognizer_model,
            "voice_stt",
            started,
            Usage(),
            outcome,
            cost,
        )

    def meter_tts(self, session: Session, started: float, characters: int, outcome: str,
                  hold: str | None = None) -> None:  # fmt: skip
        tts = self.stack.tts
        if tts is None:
            return
        cost = characters / 1_000_000 * tts.million_chars_usd()
        self._settle(hold, cost)
        self._meter(session, self.stack.synthesizer_provider, tts.voice, "voice_tts", started, Usage(), outcome, cost)

    def meter_live(self, session: Session, started: float, usage: Usage, outcome: str) -> None:
        live = self.stack.live
        if live is None:
            return
        self._meter(session, self.stack.live_provider, live.model_id, "voice_live", started, usage, outcome, None)

    async def serve(self, socket: WebSocket) -> None:
        config = self.gw.config
        if not config.voice_enabled:
            await socket.close(code=4404)
            return
        refused, session = self.refusal(socket.headers.get("authorization"))
        if refused is not None or session is None:
            await socket.close(code=refused or 4401)
            return
        await socket.accept()
        old = self.live.get(session.credential_id)
        if old is not None:  # the device reconnected: its stale connection is closed first (VOC-24)
            await old.close()
            with contextlib.suppress(Exception):
                await old.socket.close(code=1001)
        connection = VoiceConnection(self, socket, session)
        self.live[session.credential_id] = connection
        code = 1000
        try:
            await connection.run()
            if connection.failure is not None:
                code = connection.refused or 1011
        except (WebSocketDisconnect, _Ended):
            pass
        except Exception as raised:  # logged by type only: never a frame, transcript or audio (OBS-22)
            log.error("voice connection failure %s", type(raised).__name__)
            code = 1011
        finally:
            await connection.close()
            if self.live.get(session.credential_id) is connection:
                del self.live[session.credential_id]
            with contextlib.suppress(Exception):
                await socket.close(code=code)


def add_routes(app: FastAPI, gw: Any, stack: SpeechStack | Callable[[Any], SpeechStack] | None = None) -> VoiceService:
    from cloud_gateway.providers.speech_selection import build_stack, protect_stack

    if stack is None:
        stack = (
            build_stack(gw.config, gw.secrets, gw.clock)
            if (gw.config.voice_enabled or gw.config.podcast_speech.enabled)
            else SpeechStack()
        )
    elif callable(stack):
        stack = stack(gw)
    stack = protect_stack(gw.config, stack, gw.clock)
    service = VoiceService(gw, stack)

    async def realtime(socket: WebSocket) -> None:
        await service.serve(socket)

    app.add_api_websocket_route("/v1/voice/realtime", realtime)
    return service
