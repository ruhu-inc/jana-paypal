"""The speech pack's own voices, loaded from local files only, and the read-aloud server (VOC-18, VOC-39, D95).

Runs only inside the speech bundle's process (VOC-23): the tutor core never imports this module's engines, which load
their libraries through `importlib` from the verified pack. Voices are the pack's preset speakers (D95: Kokoro-82M
`af_heart` on the low and mid classes, Qwen3-TTS 0.6B CustomVoice `Ryan` on MLX for the high class on Apple
silicon); no reference audio, speaker embedding or other voice-cloning input is ever passed (VOC-02).

A read-aloud-only profile (`stt: none`, TABLE VOC-03a) runs `SpeakServer`: the Realtime wire subset (VOC-25) on
loopback with the speak directive alone. The bridge's `conversation.item.create` + `response.create` reach the engine
LLM adapter exactly as the upstream sidecar would send them, the adapter answers the owned utterance's text verbatim
(VOC-39), and the text is spoken unit by unit. The first unit ends at the first clause of the first sentence, because
time to first audio grows with the length of the first unit on a slow laptop (VOC-18 budgets).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib
import json
import re
import secrets
import sys
import threading
import time
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Protocol

from agent_core.store.speech_packs import SpeechPackError, SpeechProfile

RATE = 24_000  # PCM16 mono, the Realtime output format the bridge relays (VOC-25)
FRAME_SAMPLES = 2_400  # 0.1 s per audio delta: well inside the bridge's per-frame bound
KOKORO_REPO = "hexgrad/Kokoro-82M"  # an identifier only: weights, config and voice load from the pack's files
KOKORO_VOICE = re.compile(r"^[ab][fm]_[a-z]+$")
SENTENCE_END = re.compile(r"(?<=[.?!])\s+")
DASHES = "-" + chr(0x2013) + chr(0x2014)  # hyphen, en dash, em dash between spaces end a clause too
CLAUSE_END = re.compile(r"[,;:]\s+|\s+[" + DASHES + r"]\s+")
WORD = re.compile(r"\S+")


def speech_units(text: str, first_clause_words: int) -> list[str]:
    """`text` split into the pieces synthesised one after another; joined, they are `text` exactly (the bridge checks
    the spoken transcript against the owned text). Sentences are the units; the first sentence is split once more at
    its first clause boundary when at least `first_clause_words` words come before it and some follow it."""
    pieces: list[str] = []
    start = 0
    for match in SENTENCE_END.finditer(text):
        pieces.append(text[start : match.end()])
        start = match.end()
    if start < len(text):
        pieces.append(text[start:])
    if not pieces or first_clause_words <= 0:
        return pieces
    first = pieces[0]
    for match in CLAUSE_END.finditer(first):
        before, after = first[: match.end()], first[match.end() :]
        if len(WORD.findall(before)) >= first_clause_words and WORD.search(after):
            return [before, after, *pieces[1:]]
        if len(WORD.findall(before)) >= first_clause_words:
            break
    return pieces


def pcm16(samples: Any) -> bytes:
    """Float samples in [-1, 1] (a NumPy array or a tensor) as little-endian PCM16 bytes."""
    numpy = importlib.import_module("numpy")
    array = samples.detach().cpu().numpy() if hasattr(samples, "detach") else numpy.asarray(samples)
    array = numpy.clip(array.astype(numpy.float32).reshape(-1), -1.0, 1.0)
    return bytes((array * 32767.0).astype("<i2").tobytes())


class SpeechEngine(Protocol):
    def stream(self, text: str) -> Iterator[bytes]: ...


def exclude_espeak() -> None:
    """espeak-ng is GPL and excluded from every bundle (TABLE VOC-02a). misaki imports its espeak module at load; this
    stand-in makes the fallback unavailable, so out-of-lexicon words are spelled by misaki's own rules instead."""
    stub = types.ModuleType("misaki.espeak")

    class _Excluded:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("espeak_excluded")

    stub.EspeakFallback = stub.EspeakG2P = _Excluded  # type: ignore[attr-defined]
    sys.modules["misaki.espeak"] = stub
    misaki: Any = importlib.import_module("misaki")
    misaki.espeak = stub


G2P_WEIGHTS = "g2p/checkpoint20.npz"  # spelling-to-sound weights in the Kokoro folder (`voice_g2p`)


class KokoroEngine:
    """Kokoro-82M on the CPU through the `kokoro` package, from the pack's config, weights and one voice file.

    misaki tags words with spaCy's `en_core_web_sm` (D95: shipped in the runtime, never downloaded at run time)."""

    def __init__(self, model_dir: Path, speaker: str, threads: int = 0) -> None:
        if not KOKORO_VOICE.fullmatch(speaker):
            raise SpeechPackError("voice_speaker_refused")
        voice = model_dir / "voices" / f"{speaker}.pt"
        config, weights = model_dir / "config.json", model_dir / "kokoro-v1_0.pth"
        if not (voice.is_file() and config.is_file() and weights.is_file()):
            raise SpeechPackError("voice_tts_files_missing")
        exclude_espeak()
        torch = importlib.import_module("torch")
        if threads > 0:
            torch.set_num_threads(threads)
        kokoro = importlib.import_module("kokoro")
        model = kokoro.KModel(repo_id=KOKORO_REPO, config=str(config), model=str(weights)).to("cpu").train(False)
        self.pipeline = kokoro.KPipeline(lang_code=speaker[0], repo_id=KOKORO_REPO, model=model)
        # Without espeak, misaki drops any word its lexicon lacks; spell those out instead (the tutor's name too).
        from agent_core.net.voice_g2p import Fallback

        self.pipeline.g2p.fallback = Fallback(model_dir / G2P_WEIGHTS)
        self.voice = str(voice)  # a .pt path: the pipeline reads it from disk, never from the hub
        self.torch = torch

    def stream(self, text: str) -> Iterator[bytes]:
        with self.torch.inference_mode():
            for result in self.pipeline(text, voice=self.voice, speed=1.0, split_pattern=None):
                if result.audio is not None:
                    yield pcm16(result.audio)


class Qwen3MlxEngine:
    """Qwen3-TTS 0.6B CustomVoice on Apple silicon through `mlx-audio`, streaming, with a preset speaker only."""

    def __init__(self, model_dir: Path, speaker: str, interval_s: float) -> None:
        load_model = importlib.import_module("mlx_audio.tts.utils").load_model
        self.model = load_model(model_dir)
        if getattr(self.model.config, "tts_model_type", None) != "custom_voice":
            raise SpeechPackError("voice_preset_model_required")
        speakers = [str(s).lower() for s in (self.model.get_supported_speakers() or [])]
        if speaker.lower() not in speakers:
            raise SpeechPackError("voice_speaker_refused")
        self.speaker, self.interval_s = speaker.lower(), interval_s
        self.numpy = importlib.import_module("numpy")

    def stream(self, text: str) -> Iterator[bytes]:
        # generate_custom_voice has no reference-audio parameter at all: preset speakers only (VOC-02).
        for result in self.model.generate_custom_voice(
            text=text, speaker=self.speaker, language="english", stream=True, streaming_interval=self.interval_s
        ):
            yield pcm16(self.numpy.asarray(result.audio))


def load_engine(root: Path, profile: SpeechProfile, *, qwen3_interval_s: float, threads: int = 0) -> SpeechEngine:
    """The class's text-to-speech engine from the verified pack (TABLE VOC-03a, D95)."""
    model_dir = root / profile.tts_path
    if profile.tts == "kokoro" and profile.device == "cpu":
        return KokoroEngine(model_dir, profile.speaker, threads)
    if profile.tts == "qwen3" and profile.device == "mps":
        return Qwen3MlxEngine(model_dir, profile.speaker, qwen3_interval_s)
    raise SpeechPackError("voice_local_tts_loader_unavailable")


def frames(chunk: bytes) -> Iterator[bytes]:
    step = FRAME_SAMPLES * 2
    for offset in range(0, len(chunk), step):
        yield chunk[offset : offset + step]


class AdapterText:
    """The engine LLM adapter's reply to a speak directive (VOC-30, VOC-39): one streamed chat completion, read whole.
    The request carries only the binding marker and the directive, as the upstream sidecar's would."""

    def __init__(self, base_url: str, token: str, *, timeout_s: float = 30.0) -> None:
        self.base_url, self._token, self.timeout_s = base_url, token, timeout_s

    async def __call__(self, instructions: str, directive: str) -> str:
        return await asyncio.to_thread(self._request, instructions, directive)

    def _request(self, instructions: str, directive: str) -> str:
        from http.client import HTTPConnection
        from urllib.parse import urlsplit

        parsed = urlsplit(self.base_url)
        body = json.dumps(
            {
                "model": "jana-tutor-voice",
                "messages": [{"role": "system", "content": instructions}, {"role": "user", "content": directive}],
                "stream": True,
                "reasoning_effort": "none",
            },
            separators=(",", ":"),
        )
        connection = HTTPConnection("127.0.0.1", parsed.port, timeout=self.timeout_s)
        try:
            headers = {"Authorization": "Bearer " + self._token, "Content-Type": "application/json"}
            connection.request("POST", parsed.path + "/chat/completions", body=body, headers=headers)
            reply = connection.getresponse()
            payload = reply.read(1_000_000)
            if reply.status != 200:
                raise SpeechPackError("voice_adapter_refused")
        finally:
            connection.close()
        text = []
        for line in payload.decode("utf-8").splitlines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            for choice in json.loads(line[6:]).get("choices", []):
                text.append(choice.get("delta", {}).get("content") or "")
        return "".join(text)


class SpeakServer:
    """The read-aloud-only sidecar: the Realtime subset on loopback for the speak directive (VOC-18, VOC-25, VOC-39).

    One connection at a time (VOC-24); no audio input is read (a read-aloud-only install sends none); output is PCM16
    mono at 24 kHz in 0.1 s deltas, each unit's transcript after its first audio, the whole text on completion."""

    def __init__(
        self,
        engine: SpeechEngine,
        adapter: Callable[[str, str], Any],
        realtime_token: str,
        *,
        first_clause_words: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine, self.adapter, self._token = engine, adapter, realtime_token
        self.first_clause_words, self.clock = first_clause_words, clock
        self.active = False
        self._synthesis = threading.Lock()  # one engine, one utterance at a time

    def authorised(self, path: str, headers: Any) -> bool:
        authorization = headers.get_all("Authorization") if hasattr(headers, "get_all") else []
        offered = [
            value.strip().removeprefix("openai-insecure-api-key.")
            for header in (headers.get_all("Sec-WebSocket-Protocol") if hasattr(headers, "get_all") else [])
            for value in header.split(",")
            if value.strip().startswith("openai-insecure-api-key.")
        ]
        tokens = [value.removeprefix("Bearer ") for value in authorization if value.startswith("Bearer ")] + offered
        return (
            path == "/v1/realtime"
            and len(authorization) <= 1
            and all(value.startswith("Bearer ") for value in authorization)
            and len(tokens) == 1
            and re.fullmatch(r"[A-Za-z0-9_-]{32,256}", tokens[0]) is not None
            and secrets.compare_digest(tokens[0], self._token)
        )

    @staticmethod
    def _event(kind: str, **fields: Any) -> dict[str, Any]:
        return {"type": kind, "event_id": "evt_" + secrets.token_hex(8), **fields}

    async def session(self, websocket: Any) -> None:
        if self.active:
            await websocket.send(
                json.dumps(self._event("error", error={"type": "session_limit_reached", "message": "busy"}))
            )
            return
        self.active = True
        instructions = ""
        directive: str | None = None
        running: asyncio.Task[None] | None = None
        try:
            await websocket.send(json.dumps(self._event("session.created", session={"type": "realtime"})))
            async for raw in websocket:
                if not isinstance(raw, str):
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                kind = event.get("type") if isinstance(event, dict) else None
                if kind == "session.update":
                    session = event.get("session") or {}
                    if isinstance(session.get("instructions"), str):
                        instructions = session["instructions"]
                    await websocket.send(json.dumps(self._event("session.updated", session={"type": "realtime"})))
                elif kind == "conversation.item.create":
                    item = event.get("item") or {}
                    content = item.get("content") or []
                    if item.get("type") == "message" and item.get("role") == "user" and content:
                        text = content[0].get("text") if isinstance(content[0], dict) else None
                        directive = text if isinstance(text, str) else None
                elif kind == "response.create" and directive is not None:
                    if running is not None and not running.done():
                        running.cancel()
                    running = asyncio.create_task(self.respond(websocket, instructions, directive))
                    directive = None
                elif kind == "response.cancel" and running is not None and not running.done():
                    running.cancel()
                # input_audio_buffer.append: a read-aloud-only profile has no speech-to-text; audio is ignored.
        finally:
            if running is not None and not running.done():
                running.cancel()
                with contextlib.suppress(BaseException):
                    await running
            self.active = False

    async def respond(self, websocket: Any, instructions: str, directive: str) -> None:
        response_id, item_id = "resp_" + secrets.token_hex(8), "item_" + secrets.token_hex(8)
        where = {"response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0}
        status = "completed"
        cancel = threading.Event()
        await websocket.send(
            json.dumps(
                self._event(
                    "response.created",
                    response={"id": response_id, "object": "realtime.response", "status": "in_progress"},
                )
            )
        )
        started = self.clock()
        first_audio: float | None = None
        try:
            text = await self.adapter(instructions, directive)
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[tuple[int, bytes] | None] = asyncio.Queue(maxsize=64)
            units = speech_units(text, self.first_clause_words)

            def produce() -> None:
                try:
                    with self._synthesis:
                        for index, unit in enumerate(units):
                            if not unit.strip():
                                continue
                            for chunk in self.engine.stream(unit):
                                if cancel.is_set():
                                    return
                                for frame in frames(chunk):
                                    asyncio.run_coroutine_threadsafe(queue.put((index, frame)), loop).result()
                finally:
                    asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

            worker = loop.run_in_executor(None, produce)
            spoken = -1
            try:
                while (entry := await queue.get()) is not None:
                    index, frame = entry
                    await websocket.send(
                        json.dumps(
                            self._event(
                                "response.output_audio.delta", **where, delta=base64.b64encode(frame).decode("ascii")
                            )
                        )
                    )
                    if first_audio is None:
                        first_audio = self.clock()
                    while spoken < index:
                        spoken += 1
                        await websocket.send(
                            json.dumps(
                                self._event("response.output_audio_transcript.delta", **where, delta=units[spoken])
                            )
                        )
            finally:
                cancel.set()
                with contextlib.suppress(Exception):
                    while not worker.done():
                        with contextlib.suppress(asyncio.QueueEmpty):
                            queue.get_nowait()
                        await asyncio.sleep(0.005)
                await worker
            if first_audio is not None:
                await websocket.send(json.dumps(self._event("response.output_audio.done", **where)))
                await websocket.send(
                    json.dumps(self._event("response.output_audio_transcript.done", **where, transcript=text))
                )
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception:
            status = "failed"
        metadata = {"first_audio_ms": str(round((first_audio - started) * 1000))} if first_audio is not None else None
        with contextlib.suppress(Exception):
            await websocket.send(
                json.dumps(
                    self._event(
                        "response.done",
                        response={
                            "id": response_id,
                            "object": "realtime.response",
                            "status": status,
                            "metadata": metadata,
                        },
                    )
                )
            )
        if status == "cancelled":
            raise asyncio.CancelledError
