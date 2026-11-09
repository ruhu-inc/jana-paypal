"""Kokoro-82M as cloud voice's verbatim voice (JM-D13, SPEC §6.4): Apache-2.0, so what judges hear is licence-clean.

The model runs in one worker process (`cloud_gateway.kokoro_worker`) under the speech runtime's interpreter, which has
torch, kokoro and misaki; the gateway's own environment needs none of them. The worker is started by `warm()` (the
gateway's startup) and loads in about 10 s; after that a sentence's first audio takes 0.5-0.9 s on a laptop CPU. Its
environment carries no secret. Requests are served one at a time; a reply abandoned by a barge-in is skipped by the
next request (frames carry their request's id). The engine's keyed phrases (`fixed_phrases`: the filler, no
learner content) are read once at warm-up and replayed. Other text and its audio are never kept, logged or written."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import struct
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from cloud_gateway.config import VoiceTts
from cloud_gateway.providers.base import UpstreamError

MAX_TEXT = 2000
REPO = Path(__file__).resolve().parents[4]
WORKER = Path(__file__).resolve().parents[1] / "kokoro_worker.py"
log = logging.getLogger("cloud_gateway.voice")


def worker_command(voice: VoiceTts) -> list[str]:
    """`-P`: the worker's own folder is not put on the path, so no gateway module shadows a library."""
    return [str(Path(voice.kokoro_python).expanduser()), "-P", str(WORKER),
            str(Path(voice.kokoro_model_dir).expanduser()), voice.kokoro_speaker, str(voice.kokoro_threads),
            str(voice.kokoro_first_clause_words)]  # fmt: skip


def worker_env() -> dict[str, str]:
    """Only what the interpreter needs: never the gateway's environment, which may hold `META_API_KEY`."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"),
            "PYTHONPATH": str(REPO / "services" / "agent-core"), "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false", "PYTHONWARNINGS": "ignore"}  # fmt: skip


class KokoroSynthesizer:
    provider_id = "kokoro"

    def __init__(self, voice: VoiceTts, *, command: Sequence[str] | None = None,
                 env: dict[str, str] | None = None) -> None:  # fmt: skip
        self.settings = voice
        self.voice = f"kokoro-82m/{voice.kokoro_speaker}"  # the ledger's model name for this voice
        self._command = list(command) if command is not None else worker_command(voice)
        self._env = env if env is not None else worker_env()
        self._process: asyncio.subprocess.Process | None = None
        self._starting: asyncio.Task[asyncio.subprocess.Process] | None = None
        self._lock = asyncio.Lock()
        self._next = 1
        self._fixed: dict[str, list[bytes]] = {}  # `tts_fixed_phrases` audio (engine-owned, content-free)

    def million_chars_usd(self) -> float:
        return 0.0  # runs on our own machine

    async def warm(self) -> None:
        with contextlib.suppress(UpstreamError):
            await self._ready()
            for phrase in self.settings.fixed_phrases:
                self._fixed[phrase] = [chunk async for chunk in self._synthesize(phrase)]

    async def _ready(self) -> asyncio.subprocess.Process:
        if self._process is not None and self._process.returncode is None:
            return self._process
        if self._starting is None or self._starting.done():
            self._starting = asyncio.create_task(self._start())
        return await asyncio.shield(self._starting)

    async def _start(self) -> asyncio.subprocess.Process:
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=self._env, cwd=str(REPO))  # fmt: skip
        except OSError:
            raise UpstreamError("server_error", "voice worker could not start") from None
        assert process.stdout is not None
        try:
            header = await asyncio.wait_for(process.stdout.readexactly(8), self.settings.kokoro_start_timeout_s)
            if struct.unpack(">Ii", header) != (0, -1):
                raise ValueError
        except (TimeoutError, asyncio.IncompleteReadError, ValueError):
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            log.warning("voice worker failed to load")
            raise UpstreamError("server_error", "voice worker failed to load") from None
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            raise
        self._process = process
        return process

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        if not text or len(text) > MAX_TEXT:
            raise UpstreamError("format_error", "speech text out of bounds")
        fixed = self._fixed.get(text)
        if fixed is not None:
            for chunk in fixed:
                yield chunk
            return
        async for chunk in self._synthesize(text):
            yield chunk

    async def _synthesize(self, text: str) -> AsyncIterator[bytes]:
        async with self._lock:
            process = await self._ready()
            assert process.stdin is not None and process.stdout is not None
            ident, self._next = self._next, self._next + 1
            try:
                process.stdin.write(json.dumps({"id": ident, "text": text}).encode() + b"\n")
                await process.stdin.drain()
                while True:
                    request, size = struct.unpack(">Ii", await process.stdout.readexactly(8))
                    payload = await process.stdout.readexactly(size) if size > 0 else b""
                    if request != ident:
                        continue  # the rest of a reply a barge-in abandoned
                    if size == -1:
                        return
                    if size == -2:
                        raise UpstreamError("server_error", "synthesis failed")
                    if payload:
                        yield payload
            except (asyncio.IncompleteReadError, BrokenPipeError, ConnectionResetError):
                await self._stop()
                raise UpstreamError("server_error", "voice worker ended") from None

    async def _stop(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), 2)

    async def aclose(self) -> None:
        starting = self._starting
        if starting is not None and not starting.done():
            starting.cancel()
            with contextlib.suppress(BaseException):
                await starting
        await self._stop()
