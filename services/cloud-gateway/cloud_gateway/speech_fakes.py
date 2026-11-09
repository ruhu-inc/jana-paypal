"""Test doubles for cloud voice's speech services (VOC-28, D124): the gateway's and the core's tests drive a real
voice connection against these, never a real speech service. Not used by the running gateway.

  FakeRecognizer  hears 20 ms windows by energy, like a provider's voice-activity detector: `begin` after two loud
                  windows, `end` after `end_ms` of quiet, then the scripted transcript as a final result.
  FakeLive        reads a sentence word by word (transcription and audio together), or departs from it on cue.
  FakeTts         reads text verbatim as two chunks of tone.

Each records what it was given, so tests can check what reached which service."""

from __future__ import annotations

import asyncio
import math
import struct
from collections.abc import AsyncIterator
from typing import Any

from cloud_gateway.providers.base import UpstreamError, Usage
from cloud_gateway.providers.speech import Heard, SpeechStack, Voiced

WINDOW = 960


def tone(ms: int, loud: bool = True, mark: bytes = b"") -> bytes:
    if not loud:
        return b"\0\0" * (24 * ms)
    samples = [int(9000 * math.sin(2 * math.pi * 220 * n / 24000)) for n in range(24 * ms)]
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    return mark + pcm[len(mark) :]


def _loud(window: bytes) -> bool:
    samples = memoryview(window).cast("h")
    return math.sqrt(sum(s * s for s in samples) / max(1, len(samples))) / 32768 > 0.02


class FakeRecognition:
    def __init__(self, owner: FakeRecognizer, language: str) -> None:
        self.owner, self.language = owner, language
        self.audio = bytearray()
        self.queue: asyncio.Queue[Heard | None] = asyncio.Queue()
        self._pending = bytearray()
        self._position = 0
        self._loud = 0
        self._quiet = 0
        self._speaking = False
        self.closed = False

    async def send(self, pcm: bytes) -> None:
        if self.closed:
            raise UpstreamError("network_unavailable", "closed")
        self.audio += pcm
        self.owner.received += pcm
        self._pending += pcm
        while len(self._pending) >= WINDOW:
            window = bytes(self._pending[:WINDOW])
            del self._pending[:WINDOW]
            self._position += 20
            loud = _loud(window)
            if not self._speaking:
                self._loud = self._loud + 1 if loud else 0
                if self._loud >= 2:
                    self._speaking, self._quiet = True, 0
                    self.queue.put_nowait(Heard("begin", self._position - 40))
                    self.queue.put_nowait(Heard("partial", self._position, "…"))
                continue
            self._quiet = 0 if loud else self._quiet + 20
            if self._quiet >= self.owner.end_ms:
                self._speaking, self._loud = False, 0
                self.queue.put_nowait(Heard("end", self._position - self._quiet))
                self.queue.put_nowait(Heard("final", self._position, self.owner.next_transcript()))

    async def events(self) -> AsyncIterator[Heard]:
        while True:
            heard = await self.queue.get()
            if heard is None:
                return
            if heard.kind == "begin" and self.owner.fail_on_begin:
                raise UpstreamError("server_error", "scripted failure")
            yield heard

    async def close(self) -> None:
        self.closed = True
        self.owner.closed += 1
        self.queue.put_nowait(None)


class FakeRecognizer:
    model_id = "synthetic-asr-model"

    def __init__(self, transcripts: list[str] | None = None, *, open_delay: float = 0.0, end_ms: int = 300) -> None:
        self.transcripts = list(transcripts or ["what is two x plus three"])
        self.open_delay, self.end_ms = open_delay, end_ms
        self.streams: list[FakeRecognition] = []
        self.received = bytearray()
        self.closed = 0
        self.opened_at: list[float] = []
        self.fail_open = False
        self.fail_on_begin = False
        self.subjects: list[str | None] = []  # the session subject each stream was opened for

    def next_transcript(self) -> str:
        return self.transcripts.pop(0) if len(self.transcripts) > 1 else self.transcripts[0]

    def minute_usd(self) -> float:
        return 0.016

    async def open(self, language: str, subject: str | None = None) -> FakeRecognition:
        self.subjects.append(subject)
        if self.open_delay:
            await asyncio.sleep(self.open_delay)
        if self.fail_open:
            raise UpstreamError("network_unavailable", "scripted")
        stream = FakeRecognition(self, language)
        self.streams.append(stream)
        self.opened_at.append(asyncio.get_running_loop().time())
        return stream


class FakeLiveSession:
    def __init__(self, owner: FakeLive) -> None:
        self.owner = owner
        self.closed = False
        self.cancelled = False

    async def say(self, sentence: str) -> AsyncIterator[Voiced]:
        if not isinstance(sentence, str):
            raise TypeError("Live is given text only")
        self.owner.sentences.append(sentence)
        departure = self.owner.departures.pop(sentence, None)
        words = sentence.split(" ")
        try:
            for index, word in enumerate(words):
                if self.owner.delay:
                    await asyncio.sleep(self.owner.delay)
                if departure is not None and index == departure[0]:
                    # The model says something else: the transcript and the audio of that piece arrive together.
                    yield Voiced(audio=tone(100, mark=self.owner.mark), transcript=departure[1])
                    for _ in range(3):
                        yield Voiced(audio=tone(100, mark=self.owner.mark))
                    yield Voiced(done=True)
                    return
                yield Voiced(audio=tone(100), transcript=word + (" " if index < len(words) - 1 else ""))
            yield Voiced(done=True, usage=Usage(input_tokens=12, output_tokens=40))
        except (asyncio.CancelledError, GeneratorExit):
            self.cancelled = True
            raise

    async def close(self) -> None:
        self.closed = True


class FakeLive:
    model_id = "live-voice-stub"

    def __init__(self, *, open_delay: float = 0.0, delay: float = 0.0) -> None:
        self.open_delay, self.delay = open_delay, delay
        self.sessions: list[FakeLiveSession] = []
        self.sentences: list[str] = []
        # sentence -> (word index where it departs, what it says instead)
        self.departures: dict[str, tuple[int, str]] = {}
        self.mark = struct.pack("<8h", 7777, -7777, 7777, -7777, 7777, -7777, 7777, -7777)

    async def open(self) -> FakeLiveSession:
        if self.open_delay:
            await asyncio.sleep(self.open_delay)
        session = FakeLiveSession(self)
        self.sessions.append(session)
        return session


class FakeTts:
    voice = "synthetic-tts-voice"

    def __init__(self) -> None:
        self.texts: list[str] = []

    def million_chars_usd(self) -> float:
        return 0.0  # Kokoro runs on our own machine

    async def warm(self) -> None:
        return None

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        self.texts.append(text)
        for _ in range(2):
            yield tone(150)

    async def aclose(self) -> None:
        return None


def fake_stack(**options: Any) -> tuple[SpeechStack, FakeRecognizer, FakeLive, FakeTts]:
    recognizer = options.pop("recognizer", None) or FakeRecognizer()
    live = options.pop("live", None) or FakeLive()
    tts = options.pop("tts", None) or FakeTts()
    return (
        SpeechStack(recognizer=recognizer, live=live, tts=tts, recognizer_model="synthetic-asr-model"),
        recognizer,
        live,
        tts,
    )
