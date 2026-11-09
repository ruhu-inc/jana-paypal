"""Podcast narration (JM-D13, SPEC §6.4): the gateway's own verbatim voice reads each frozen passage.

The voice is the separately selected, permitted verbatim synthesizer, with its own model/service review. Audio may be
sent only to that configured synthesis backend. Text and audio are ephemeral here; durable receipts
contain opaque identities and character counts. An uncertain or failed reading remains counted, and a transport retry
never repeats synthesis automatically.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import io
import json
import re
import wave
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from cloud_gateway import minimise
from cloud_gateway.auth import AuthError
from cloud_gateway.providers.speech import Synthesizer

RATE = 24_000  # the synthesizer's mono PCM16 (providers/speech.py)
MAX_TEXT = 600
MAX_AUDIO = 6_000_000
SCHEMA = """
CREATE TABLE IF NOT EXISTS podcast_speech_receipts (
 identity TEXT PRIMARY KEY, input_digest TEXT NOT NULL, day TEXT NOT NULL,
 characters INTEGER NOT NULL, state TEXT NOT NULL
);
"""


class SpeechError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def validate_audio(data: bytes, rate: int = RATE) -> None:
    """Reject error pages, truncated WAVs and unbounded/unsupported audio before the browser sees it."""
    if not 44 <= len(data) <= MAX_AUDIO:
        raise SpeechError("neural_speech_invalid_audio")
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            frames = audio.getnframes()
            if (
                audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getframerate() != rate
                or audio.getcomptype() != "NONE"
                or not 0 < frames <= rate * 120
                or len(audio.readframes(frames)) != frames * 2
            ):
                raise SpeechError("neural_speech_invalid_audio")
    except (wave.Error, EOFError):
        raise SpeechError("neural_speech_invalid_audio") from None


def to_wav(pcm: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        audio.writeframes(pcm)
    return output.getvalue()


class PodcastSpeech:
    def __init__(self, gw: Any, tts: Callable[[], Synthesizer | None]):
        self.gw = gw
        self.tts = tts  # cloud voice's synthesizer, looked up when it reads (the voice routes own its lifetime)
        self.lock = asyncio.Lock()
        gw.store._db.executescript(SCHEMA)

    def status(self) -> dict[str, Any]:
        cfg = self.gw.config.podcast_speech
        active = cfg.enabled and (cfg.expires_at is None or self.gw.clock() < cfg.expires_at.timestamp())
        return {
            "enabled": cfg.enabled,
            "configured": bool(active and self.tts() is not None),
            "provider": self.gw.config.speech.synthesizer,
        }

    def reserve(self, owner: str, body: dict[str, Any]) -> str:
        cfg, now = self.gw.config.podcast_speech, self.gw.clock()
        if not cfg.enabled or (cfg.expires_at is not None and now >= cfg.expires_at.timestamp()):
            raise SpeechError("neural_speech_limit")
        if self.tts() is None:
            raise SpeechError("neural_speech_not_configured")
        identity = hashlib.sha256(f"{owner}:{body['request_id']}".encode()).hexdigest()
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        day = dt.datetime.fromtimestamp(now, dt.UTC).date().isoformat()
        size = len(body["text"])
        with self.gw.store._tx() as db:
            prior = db.execute(
                "SELECT input_digest FROM podcast_speech_receipts WHERE identity=?", (identity,)
            ).fetchone()
            if prior:
                raise SpeechError("neural_speech_already_sent")
            daily, total, calls, all_calls = db.execute(
                "SELECT coalesce(sum(CASE WHEN day=? OR state='held' THEN characters ELSE 0 END),0),"
                "coalesce(sum(characters),0),coalesce(sum(CASE WHEN day=? OR state='held' THEN 1 ELSE 0 END),0),"
                "count(*) FROM podcast_speech_receipts",
                (day, day),
            ).fetchone()
            if (
                daily + size > cfg.daily_characters
                or total + size > cfg.total_characters
                or calls + 1 > cfg.daily_requests
                or all_calls + 1 > cfg.total_requests
            ):
                raise SpeechError("neural_speech_limit")
            db.execute("INSERT INTO podcast_speech_receipts VALUES (?,?,?,?,'held')", (identity, digest, day, size))
        return identity

    async def synthesize(self, owner: str, body: dict[str, Any]) -> bytes:
        async with self.lock:
            identity = self.reserve(owner, body)
            tts = self.tts()
            assert tts is not None
            pcm, size = [], 0
            try:
                async for chunk in tts.synthesize(body["text"]):
                    size += len(chunk)
                    if size > MAX_AUDIO:
                        raise SpeechError("neural_speech_invalid_audio")
                    pcm.append(chunk)
            except SpeechError:
                raise
            except Exception:
                raise SpeechError("neural_speech_unavailable") from None
            audio = to_wav(b"".join(pcm))
            validate_audio(audio)
            self.gw.store._db.execute(
                "UPDATE podcast_speech_receipts SET state='completed' WHERE identity=?", (identity,)
            )
            return audio


def add_routes(app: FastAPI, gw: Any) -> PodcastSpeech:
    def tts() -> Synthesizer | None:
        voice = getattr(app.state, "voice", None)
        from cloud_gateway.providers.speech_selection import allowed

        if not allowed(gw.config.speech.synthesis_permission, gw.clock()):
            return None
        return voice.stack.tts if voice is not None else None

    speech = PodcastSpeech(gw, tts)

    @app.get("/v1/podcast/voice")
    async def status(request: Request) -> Any:
        try:
            gw.auth.authenticate(request.headers.get("authorization"))
            return JSONResponse(speech.status(), headers={"Cache-Control": "no-store"})
        except AuthError:
            return JSONResponse({"error": {"code": "auth"}}, status_code=401)

    @app.post("/v1/podcast/speech")
    async def narrate(request: Request) -> Any:
        try:
            session = gw.auth.authenticate(request.headers.get("authorization"))
            body = await gw.json_body(request, 8192)
            if (
                not isinstance(body, dict)
                or set(body) != {"request_id", "text", "voice_index"}
                or not isinstance(body.get("request_id"), str)
                or re.fullmatch(r"cli_[0-9A-HJKMNP-TV-Z]{26}", body["request_id"]) is None
                or not isinstance(body.get("text"), str)
                or not 1 <= len(body["text"]) <= MAX_TEXT
                or not body["text"].strip()
                or type(body.get("voice_index")) is not int
                or body["voice_index"] not in (0, 1)
                or minimise.refusal(request.headers, body)
            ):
                raise SpeechError("neural_speech_invalid_request")
            voice = speech.tts()
            if voice is not None and gw.operations.blocked(
                gw.config.speech.synthesizer, voice.voice, session.licence_id
            ):
                raise SpeechError("neural_speech_unavailable")
            audio = await speech.synthesize(session.credential_id, body)
            return JSONResponse(
                {"audio_base64": base64.b64encode(audio).decode(), "voice": voice.voice if voice else ""},
                headers={"Cache-Control": "no-store"},
            )
        except AuthError:
            return JSONResponse({"error": {"code": "auth"}}, status_code=401)
        except SpeechError as error:
            return JSONResponse(
                {"error": {"code": error.reason}}, status_code=503, headers={"Cache-Control": "no-store"}
            )

    return speech
