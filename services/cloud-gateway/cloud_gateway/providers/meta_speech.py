"""Muse Voice Transcribe realtime speech to text (JM-D12, SPEC §6.1), cloud voice's `Recognizer`, and the
`SpeechStack` a gateway builds from `voice_meta`.

The wire (dev.meta.ai/docs/api-reference/voice/realtime, read 2026-10-27):
  -> the first text frame, within 10 s: the key as `authorization.accessToken: "Bearer <key>"`, `model`,
     `audioEncoding: "PCM_24KHZ"`, `mode: "ENDPOINTING"`, `partialMode: "CUMULATIVE"`, `languageBias` (language
     names) and `keywords`. The key goes only here: the endpoint ignores the Authorization header and has no key
     parameter. Nothing in it can change mid-session.
  <- `{"sessionId": "…"}`, the acknowledgement and the only frame without a `type`. No audio is sent before it.
  -> binary frames of 24 kHz mono PCM16 at real time: never more than `ahead_ms` ahead of the stream's clock (Meta
     allows 5 s) and never behind it (below-real-time ingress is a policy failure, and a stream that stops sending
     is closed), so a gap in the device's audio is filled with silence.
  <- `speechStart`, `transcript` (cumulative: each partial replaces the last), `speechEnd`, `speechComplete` (the
     turn's final, post-processed text), each with `audioProcessedMs` from the stream's start; `error` before a
     fatal close; other types are ignored.
  -> `{"type": "endStream"}` ends the whole session; the server flushes what is pending and closes with 1000.

Closes: 1011 (the 60-minute session cap, or a backend failure) and 1013 (rate limited) end `events()`, and the voice
connection opens the next stream while it holds the student's audio; the recognizer waits out a short backoff first,
except after the session cap. Barge-in stays the gateway's: `speechStart` is `Heard("begin")`, which cancels the
reply being spoken. Nothing here logs audio, text or a frame; failures become `UpstreamError` with a reason only."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from cloud_gateway.config import GatewayConfig, MetaVoice
from cloud_gateway.custody import Secret, SecretSource
from cloud_gateway.providers.base import UpstreamError
from cloud_gateway.providers.speech import Heard, SpeechStack
from cloud_gateway.providers.vocabulary import glossary_keywords
from cloud_gateway.providers.vocabulary import glossary_sections as glossary_sections

BYTES_PER_MS = 48  # 24 kHz mono PCM16
FRAME = 3840  # 80 ms, the frame Meta's examples send
TICK_S = 0.1  # how often an open stream checks for a gap to fill with silence
RETRY_S = (0.5, 1.0, 2.0)  # backoff before reopening after a 1011 or 1013 close (the connection's open budget is 5 s)
KEYWORD_CHARS = 30  # Meta's longest keyword
CAPPED = "max session duration"  # the 1011 reason that asks for a new session, not a backoff


def handshake(voice: MetaVoice, key: Secret, language: str, keywords: list[str]) -> dict[str, Any]:
    """The stream's first frame. It holds the key: it is sent and never logged."""
    frame: dict[str, Any] = {
        "authorization": {"accessToken": "Bearer " + key.reveal()},
        "model": voice.model,
        "audioEncoding": "PCM_24KHZ",
        "mode": "ENDPOINTING",
        "partialMode": "CUMULATIVE",
        "emitAudioProgress": False,
    }
    bias = voice.language_bias.get(language) or voice.language_bias.get("en")
    if bias:
        frame["languageBias"] = list(bias)
    if keywords:
        frame["keywords"] = keywords
    return frame


def _closed(socket: Any) -> tuple[int | None, str]:
    code = getattr(socket, "close_code", None)
    reason = getattr(socket, "close_reason", None)
    return code, reason if isinstance(reason, str) else ""


class MetaRecognition:
    """One Voice Transcribe session. `send` paces the caller's audio at real time and a background task fills gaps
    with silence; offsets in `events()` count the caller's audio only, without that silence."""

    def __init__(self, socket: Any, voice: MetaVoice, owner: MetaRecognizer,
                 clock: Callable[[], float]) -> None:  # fmt: skip
        self.socket, self.voice, self.owner, self.clock = socket, voice, owner, clock
        self.origin: float | None = None  # when the first audio byte was sent: the stream's clock starts there
        self.sent_ms = 0.0  # audio sent, silence included (the stream's clock as the server counts it)
        self.padded_bytes = 0  # silence sent to fill gaps: billed like audio
        self._pads: list[tuple[float, float]] = []  # (stream ms where a fill began, its length in ms)
        self._lock = asyncio.Lock()
        self._ended = asyncio.Event()
        self._reading = False  # a reader is taking events, so what the server flushes after endStream is kept
        self._closing = False
        self._filler: asyncio.Task[None] | None = None

    # --- audio -------------------------------------------------------------------------------------------------------

    async def send(self, pcm: bytes) -> None:
        if self._closing or self._ended.is_set():
            raise UpstreamError("network_unavailable", "recognition closed")
        async with self._lock:
            if self.origin is None:
                self.origin = self.clock()
                self._filler = asyncio.create_task(self._fill())
            ahead = self.sent_ms + len(pcm) / BYTES_PER_MS - self._elapsed_ms()
            if ahead > self.voice.ahead_ms:
                await asyncio.sleep((ahead - self.voice.ahead_ms) / 1000)
            await self._write(pcm)

    def _elapsed_ms(self) -> float:
        return 0.0 if self.origin is None else (self.clock() - self.origin) * 1000

    async def _write(self, pcm: bytes) -> None:
        try:
            for at in range(0, len(pcm), FRAME):
                await self.socket.send(pcm[at : at + FRAME])
        except Exception:
            raise UpstreamError("network_unavailable", "recognition send failed") from None
        self.sent_ms += len(pcm) / BYTES_PER_MS

    async def _fill(self) -> None:
        """While the device sends nothing for `pad_after_ms`, send silence up to real time."""
        while not self._closing and not self._ended.is_set():
            await asyncio.sleep(TICK_S)
            async with self._lock:
                behind = self._elapsed_ms() - self.sent_ms
                if behind < self.voice.pad_after_ms or self._closing:
                    continue
                silence = bytes(int(behind) * BYTES_PER_MS)
                self._pads.append((self.sent_ms, len(silence) / BYTES_PER_MS))
                with contextlib.suppress(UpstreamError):  # a failed send ends the stream; events() reports it
                    await self._write(silence)
                    self.padded_bytes += len(silence)

    def offset(self, processed_ms: float) -> int:
        """The caller's audio position of a stream position: the stream's clock less the silence sent before it."""
        filled = sum(max(0.0, min(length, processed_ms - start)) for start, length in self._pads)
        return max(0, int(processed_ms - filled))

    # --- events ------------------------------------------------------------------------------------------------------

    def heard(self, message: Any) -> Heard | None:
        if not isinstance(message, dict):
            raise UpstreamError("bad_response", "recognition message is not an object")
        kind = message.get("type")
        at = message.get("audioProcessedMs")
        offset = self.offset(at) if isinstance(at, int | float) and not isinstance(at, bool) else 0
        text = message.get("transcript")
        text = text if isinstance(text, str) else ""
        if kind == "speechStart":
            return Heard("begin", offset)
        if kind == "transcript":
            return Heard("partial", offset, text)  # cumulative: the connection replaces its partial with it
        if kind == "speechEnd":
            return Heard("end", offset)
        if kind == "speechComplete":
            return Heard("final", offset, text)
        return None  # the acknowledgement, audio progress, an error (its close follows), or a type added later

    async def events(self) -> AsyncIterator[Heard]:
        self._reading = True
        try:
            while True:
                try:
                    raw = await self.socket.recv()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    code, reason = _closed(self.socket)
                    if code == 1000 or (code is None and self._closing):
                        return
                    if code in (1011, 1013):
                        self.owner.closed_with(code, reason)
                        return  # the session cap, a backend failure or a rate limit: the connection reopens
                    if code == 1008:
                        raise UpstreamError("format_error", "recognition stream refused by policy") from None
                    raise UpstreamError("network_unavailable", "recognition stream lost") from None
                if isinstance(raw, bytes):
                    continue
                try:
                    message = json.loads(raw)
                except ValueError:
                    raise UpstreamError("bad_response", "recognition message is not JSON") from None
                heard = self.heard(message)
                if heard is not None:
                    yield heard
        finally:
            self._ended.set()

    async def close(self) -> None:
        """End the session with `endStream`, let the reader take what the server flushes, then close the socket."""
        if self._closing:
            return
        self._closing = True
        if self._filler is not None:
            self._filler.cancel()
        if not self._ended.is_set():
            with contextlib.suppress(Exception):
                async with self._lock:
                    await self.socket.send(json.dumps({"type": "endStream"}))
            if self._reading:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._ended.wait(), 2)
        with contextlib.suppress(Exception):
            await self.socket.close()


class MetaRecognizer:
    """Opens Voice Transcribe sessions with the Meta Model API key."""

    provider_id = "meta"

    def __init__(self, voice: MetaVoice, key: Secret, *, connect: Callable[..., Awaitable[Any]] | None = None,
                 clock: Callable[[], float] | None = None) -> None:  # fmt: skip
        self.voice, self.key = voice, key
        self.model_id = voice.model
        self.keywords = self.keywords_for(None)
        self._connect = connect
        self._clock = clock
        self.retry_at = 0.0  # no stream opens before this (a backoff after 1011 or 1013)
        self.retries = 0

    def keywords_for(self, subject: str | None) -> list[str]:
        """The handshake's keywords for a session in `subject`: `voice.keywords` if configured, else the glossary's,
        its subject first (`glossary_keywords`). Meta refuses more than 500, or one longer than 30 characters
        (measured 2026-10-27), so longer ones are left out and the list is capped at `keywords_max`."""
        keywords = self.voice.keywords if self.voice.keywords is not None else glossary_keywords(subject)
        fitting = [word for word in dict.fromkeys(keywords) if len(word) <= KEYWORD_CHARS]
        return fitting[: self.voice.keywords_max]

    def minute_usd(self) -> float:
        return self.voice.hour_usd / 60

    def clock(self) -> float:
        return self._clock() if self._clock is not None else asyncio.get_running_loop().time()

    def closed_with(self, code: int, reason: str) -> None:
        if code == 1011 and CAPPED in reason.lower():
            return  # a new session may start at once
        self.retry_at = self.clock() + RETRY_S[min(self.retries, len(RETRY_S) - 1)]
        self.retries += 1

    async def open(self, language: str, subject: str | None = None) -> MetaRecognition:
        wait = self.retry_at - self.clock()
        if wait > 0:
            await asyncio.sleep(wait)
        connect = self._connect
        if connect is None:
            from websockets.asyncio.client import connect as websocket_connect

            connect = websocket_connect
        try:
            socket = await connect(self.voice.url, proxy=None, compression=None, max_size=1_000_000,
                                   open_timeout=self.voice.handshake_timeout_s, close_timeout=2)  # fmt: skip
        except Exception:
            raise UpstreamError("network_unavailable", "recognition connect failed") from None
        try:
            keywords = self.keywords_for(subject)
            await socket.send(json.dumps(handshake(self.voice, self.key, language, keywords)))
            reply = json.loads(await asyncio.wait_for(socket.recv(), self.voice.handshake_timeout_s))
        except asyncio.CancelledError:  # the caller gave up on the open (a timeout, idle, barge-in): no socket left
            with contextlib.suppress(Exception):
                await socket.close()
            raise
        except Exception:
            code, reason = _closed(socket)
            with contextlib.suppress(Exception):
                await socket.close()
            if code in (1011, 1013):
                self.closed_with(code, reason)
            if code == 1013:
                raise UpstreamError("rate_limit", "recognition rate limited") from None
            raise UpstreamError("network_unavailable", "recognition handshake failed") from None
        if not isinstance(reply, dict) or "type" in reply or not isinstance(reply.get("sessionId"), str):
            with contextlib.suppress(Exception):
                await socket.close()
            if isinstance(reply, dict) and reply.get("type") == "error":
                # A request Meta finds invalid (too many keywords, a bad setting) is ours to fix, not the key's.
                if "invalid_request" in str(reply.get("errorType") or ""):
                    raise UpstreamError("format_error", "recognition handshake invalid")
                raise UpstreamError("upstream_auth", "recognition handshake refused")
            raise UpstreamError("bad_response", "recognition handshake not acknowledged")
        self.retries = 0
        return MetaRecognition(socket, self.voice, self, self.clock)


def meta_stack(config: GatewayConfig, secrets: SecretSource) -> SpeechStack:
    """Cloud voice's services: Voice Transcribe hears, no Live voice (the verbatim reader path), and `voice_tts` reads:
    Kokoro-82M (SPEC §6.4). Without the Meta key there is no speech to text and the endpoint refuses voice
    connections."""
    from cloud_gateway.providers.kokoro_speech import KokoroSynthesizer

    key = secrets.get(config.voice_meta.secret)
    recognizer = MetaRecognizer(config.voice_meta, key) if key is not None else None
    return SpeechStack(recognizer=recognizer, live=None, tts=KokoroSynthesizer(config.voice_tts))
