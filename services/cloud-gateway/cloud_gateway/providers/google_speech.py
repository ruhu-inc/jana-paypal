"""Cloud Speech-to-Text v2 streaming recognition and Cloud Text-to-Speech streaming synthesis (VOC-28, D124), and the
`SpeechStack` a gateway builds from `voice_google`.

Both are gRPC-only streaming APIs, reached through Google's client libraries (the `google-voice` extra, imported only
when cloud voice is switched on). Both take the attached service identity; there is no documented API-key path.
Neither the audio nor the text is logged; failures become `UpstreamError` with a reason code only."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from cloud_gateway.config import GatewayConfig, GoogleVoice
from cloud_gateway.custody import SecretSource
from cloud_gateway.providers.base import UpstreamError
from cloud_gateway.providers.gemini_live import GeminiLive
from cloud_gateway.providers.speech import Heard, SpeechStack

SCOPE = "https://www.googleapis.com/auth/cloud-platform"
RATE = 24_000
MAX_TEXT = 2000


def _credentials() -> Any:
    """The attached identity (the metadata server on Google Cloud; local application default credentials when the
    live smoke test runs on a developer's machine)."""
    import google.auth

    credentials, _ = google.auth.default(scopes=[SCOPE])
    return credentials


def _ms(offset: Any) -> int:
    if isinstance(offset, timedelta):
        return int(offset.total_seconds() * 1000)
    seconds = getattr(offset, "seconds", 0) or 0
    nanos = getattr(offset, "nanos", 0) or 0
    return int(seconds * 1000 + nanos // 1_000_000)


class ChirpRecognition:
    def __init__(self, client: Any, first: Any, types: Any) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)
        self._types = types
        self._first = first
        self._call: Any = None
        self._client = client

    async def start(self) -> None:
        async def requests() -> AsyncIterator[Any]:
            yield self._first
            while True:
                chunk = await self._queue.get()
                if chunk is None:
                    return
                yield self._types.StreamingRecognizeRequest(audio=chunk)

        try:
            self._call = await self._client.streaming_recognize(requests=requests())
            with contextlib.suppress(AttributeError):
                await self._call.wait_for_connection()
        except asyncio.CancelledError:
            if self._call is not None:
                self._call.cancel()
            raise
        except Exception:
            if self._call is not None:
                self._call.cancel()
            raise UpstreamError("network_unavailable", "recognition open failed") from None

    async def send(self, pcm: bytes) -> None:
        await self._queue.put(pcm)

    async def events(self) -> AsyncIterator[Heard]:
        types = self._types.StreamingRecognizeResponse.SpeechEventType
        try:
            async for response in self._call:
                kind = response.speech_event_type
                if kind == types.SPEECH_ACTIVITY_BEGIN:
                    yield Heard("begin", _ms(response.speech_event_offset))
                elif kind == types.SPEECH_ACTIVITY_END:
                    yield Heard("end", _ms(response.speech_event_offset))
                for result in response.results:
                    if not result.alternatives:
                        continue
                    text = result.alternatives[0].transcript
                    yield Heard("final" if result.is_final else "partial", _ms(result.result_end_offset), text)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise UpstreamError("server_error", "recognition stream failed") from None

    async def close(self) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)
        if self._call is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(self._drain()), 2)
            with contextlib.suppress(Exception):
                self._call.cancel()

    async def _drain(self) -> None:
        with contextlib.suppress(Exception):
            await self._call.done_writing()


class ChirpRecognizer:
    provider_id = "google_speech"

    def __init__(self, voice: GoogleVoice) -> None:
        self.voice = voice
        self.model_id = voice.stt_model
        self._client: Any = None

    def minute_usd(self) -> float:
        if self.voice.stt_minute_usd is None:
            raise UpstreamError("format_error", "recognition price is unconfigured", extra={"unbilled": True})
        return self.voice.stt_minute_usd

    def client(self) -> Any:
        if self._client is None:
            from google.api_core.client_options import ClientOptions
            from google.cloud.speech_v2 import SpeechAsyncClient

            self._client = SpeechAsyncClient(
                credentials=_credentials(),
                client_options=ClientOptions(api_endpoint=f"{self.voice.stt_location}-speech.googleapis.com"),
            )
        return self._client

    async def open(self, language: str, subject: str | None = None) -> ChirpRecognition:
        from google.cloud.speech_v2.types import cloud_speech as types
        from google.protobuf import duration_pb2  # type: ignore[import-untyped]  # SDK runtime protos have no stubs.

        locales = self.voice.stt_locales.get(language) or self.voice.stt_locales.get("en") or ["en-GB"]
        end = duration_pb2.Duration()
        end.FromMilliseconds(self.voice.stt_end_timeout_ms)
        config = types.StreamingRecognitionConfig(
            config=types.RecognitionConfig(
                explicit_decoding_config=types.ExplicitDecodingConfig(
                    encoding=types.ExplicitDecodingConfig.AudioEncoding.LINEAR16, sample_rate_hertz=RATE,
                    audio_channel_count=1),
                language_codes=list(locales), model=self.voice.stt_model),
            streaming_features=types.StreamingRecognitionFeatures(
                interim_results=True, enable_voice_activity_events=True,
                voice_activity_timeout=types.StreamingRecognitionFeatures.VoiceActivityTimeout(speech_end_timeout=end)),
        )  # fmt: skip
        if self.voice.stt_adaptation and (subject is not None or self.voice.stt_keywords is not None):
            from cloud_gateway.providers.vocabulary import glossary_keywords

            words = self.voice.stt_keywords if self.voice.stt_keywords is not None else glossary_keywords(subject)
            words = tuple(dict.fromkeys(word for word in words if word and len(word) <= 100))
            phrases = [types.PhraseSet.Phrase(value=word) for word in words[: self.voice.stt_keywords_max]]
            if phrases:
                config.config.adaptation = types.SpeechAdaptation(
                    phrase_sets=[
                        types.SpeechAdaptation.AdaptationPhraseSet(inline_phrase_set=types.PhraseSet(phrases=phrases))
                    ]
                )
        recognizer = f"projects/{self.voice.project}/locations/{self.voice.stt_location}/recognizers/_"
        first = types.StreamingRecognizeRequest(recognizer=recognizer, streaming_config=config)
        try:
            client = self.client()
        except Exception:
            raise UpstreamError("upstream_auth", "no identity for speech to text") from None
        recognition = ChirpRecognition(client, first, types)
        await recognition.start()
        return recognition

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.transport.close()


class ChirpSynthesizer:
    """Chirp 3 HD streaming synthesis: plain text (no SSML, no markup), read as given."""

    provider_id = "google_speech"

    def __init__(self, voice: GoogleVoice, language: str = "en") -> None:
        self.settings = voice
        self.voice = voice.tts_voices.get(language) or next(iter(voice.tts_voices.values()))
        self._client: Any = None

    def million_chars_usd(self) -> float:
        if self.settings.tts_million_chars_usd is None:
            raise UpstreamError("format_error", "synthesis price is unconfigured", extra={"unbilled": True})
        return self.settings.tts_million_chars_usd

    def client(self) -> Any:
        if self._client is None:
            from google.api_core.client_options import ClientOptions
            from google.cloud import texttospeech

            host = ("texttospeech.googleapis.com" if self.settings.tts_location == "global"
                    else f"{self.settings.tts_location}-texttospeech.googleapis.com")  # fmt: skip
            self._client = texttospeech.TextToSpeechAsyncClient(
                credentials=_credentials(), client_options=ClientOptions(api_endpoint=host)
            )
        return self._client

    async def warm(self) -> None:
        with contextlib.suppress(Exception):
            self.client()

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        if not text or len(text) > MAX_TEXT:
            raise UpstreamError("format_error", "speech text out of bounds")
        from google.cloud import texttospeech as tts

        config = tts.StreamingSynthesizeConfig(
            voice=tts.VoiceSelectionParams(name=self.voice, language_code="-".join(self.voice.split("-")[:2])),
            streaming_audio_config=tts.StreamingAudioConfig(audio_encoding=tts.AudioEncoding.PCM,
                                                            sample_rate_hertz=RATE))  # fmt: skip

        async def requests() -> AsyncIterator[Any]:
            yield tts.StreamingSynthesizeRequest(streaming_config=config)
            yield tts.StreamingSynthesizeRequest(input=tts.StreamingSynthesisInput(text=text))

        try:
            call = await self.client().streaming_synthesize(requests=requests())
        except Exception:
            raise UpstreamError("network_unavailable", "synthesis open failed") from None
        odd = b""
        try:
            async for response in call:
                pcm = odd + bytes(response.audio_content)
                cut = len(pcm) - len(pcm) % 2
                odd = pcm[cut:]
                if cut:
                    yield pcm[:cut]
            if odd:
                raise UpstreamError("bad_response", "synthesis ended with an incomplete PCM sample")
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                call.cancel()
            raise
        except UpstreamError:
            raise
        except Exception:
            raise UpstreamError("server_error", "synthesis stream failed") from None
        finally:
            with contextlib.suppress(Exception):
                call.cancel()

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.transport.close()


def google_live(config: GatewayConfig, secrets: SecretSource) -> GeminiLive:
    """Build only the optional Live reader; recognition and synthesis remain independently selected."""
    voice = config.voice_google
    credentials: list[Any] = []

    async def token() -> str:
        import google.auth.transport.requests

        if not credentials:
            credentials.append(_credentials())
        current = credentials[0]
        if not current.valid:
            await asyncio.to_thread(current.refresh, google.auth.transport.requests.Request())
        return str(current.token)

    return GeminiLive(api=voice.live_api, model=voice.live_model, voice=voice.live_voice,
                      key=secrets.get(voice.live_secret) if voice.live_api == "gemini" else None,
                      project=voice.project, location=voice.live_location, token=token)  # fmt: skip


def google_stack(config: GatewayConfig, secrets: SecretSource) -> SpeechStack:
    """Explicit legacy composition; normal deployments select each service through speech_selection."""
    voice = config.voice_google
    if not voice.project or not voice.stt_model or not voice.tts_voices:
        return SpeechStack()
    return SpeechStack(
        recognizer=ChirpRecognizer(voice),
        live=google_live(config, secrets),
        tts=ChirpSynthesizer(voice),
        recognizer_provider="google_speech",
        recognizer_model=voice.stt_model,
        synthesizer_provider="google_speech",
        live_provider=voice.live_api,
    )
