"""The three speech services behind cloud voice (VOC-28, D124), as interfaces the voice connection drives.

  Recognizer   streaming speech to text with speech events
  Live         an optional reader of accepted sentences, with an output transcription
  Synthesizer  verbatim streaming text to speech

Adapters and model identifiers are independently selected by deployment configuration.

Tests give fakes with the same shape (`speech_fakes.py`). Nothing here
holds audio or text beyond the call that carries it, and nothing here logs either."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol

from cloud_gateway.providers.base import Usage


@dataclass(frozen=True, repr=False)
class Heard:
    """One recognition event. `offset_ms` counts from the first audio byte this stream was sent."""

    kind: Literal["begin", "end", "partial", "final"]
    offset_ms: int = 0
    text: str = ""


class Recognition(Protocol):
    """One open streaming-recognition call. `events()` ends when the call ends (the provider's stream limit, or
    `close`), and raises `UpstreamError` when it fails."""

    async def send(self, pcm: bytes) -> None: ...

    def events(self) -> AsyncIterator[Heard]: ...

    async def close(self) -> None: ...


class Recognizer(Protocol):
    async def open(self, language: str, subject: str | None = None) -> Recognition:
        """`subject`: the session's subject, when the device named it (it orders the vocabulary bias)."""
        ...

    def minute_usd(self) -> float: ...


@dataclass(frozen=True, repr=False)
class Voiced:
    """A piece of a Live turn: audio, an increment of its output transcription, the turn's end, or its usage."""

    audio: bytes = b""
    transcript: str = ""
    done: bool = False
    usage: Usage | None = None


class LiveSession(Protocol):
    """One Live voice session, set up with a read-exactly instruction and no audio input. `say` sends one sentence
    as a turn and yields what comes back until the turn completes."""

    def say(self, sentence: str) -> AsyncIterator[Voiced]: ...

    async def close(self) -> None: ...


class Live(Protocol):
    model_id: str

    async def open(self) -> LiveSession: ...


class Synthesizer(Protocol):
    """Verbatim streaming synthesis: 24 kHz mono PCM16 chunks of exactly the text given."""

    @property
    def voice(self) -> str: ...

    def synthesize(self, text: str) -> AsyncIterator[bytes]: ...

    def million_chars_usd(self) -> float: ...

    async def warm(self) -> None: ...

    async def aclose(self) -> None: ...


@dataclass
class SpeechStack:
    """What one gateway uses for cloud voice. A missing part makes the endpoint refuse voice connections (Recognizer,
    Synthesizer) or read with the synthesizer only (Live)."""

    recognizer: Recognizer | None = None
    live: Live | None = None
    tts: Synthesizer | None = None
    recognizer_provider: str = "custom"
    recognizer_model: str = "configured-recognizer"
    synthesizer_provider: str = "custom"
    live_provider: str = "custom"

    async def warm(self) -> None:
        if self.tts is not None:
            await self.tts.warm()

    async def aclose(self) -> None:
        if self.tts is not None:
            await self.tts.aclose()
        close_recognizer = getattr(self.recognizer, "aclose", None)
        if close_recognizer is not None:
            await close_recognizer()
