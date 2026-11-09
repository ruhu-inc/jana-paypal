"""Independent speech factories and permission boundaries; no backend is selected implicitly."""

from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import Any

from cloud_gateway.config import GatewayConfig, ServicePermission
from cloud_gateway.custody import SecretSource
from cloud_gateway.providers.base import UpstreamError
from cloud_gateway.providers.speech import Heard, Recognition, Recognizer, SpeechStack, Synthesizer, Voiced


def allowed(permission: ServicePermission | None, now: float) -> bool:
    return bool(permission and permission.permits(now))


def require_permission(permission: ServicePermission | None, clock: Callable[[], float]) -> None:
    if not allowed(permission, clock()):
        raise UpstreamError("overloaded", "speech service permission unavailable", extra={"unbilled": True})


class PermittedRecognition:
    def __init__(self, stream: Recognition, permission: ServicePermission | None, clock: Callable[[], float]):
        self.stream, self.permission, self.clock = stream, permission, clock

    async def send(self, pcm: bytes) -> None:
        require_permission(self.permission, self.clock)
        await self.stream.send(pcm)

    async def events(self) -> AsyncIterator[Heard]:
        async for event in self.stream.events():
            require_permission(self.permission, self.clock)
            yield event

    async def close(self) -> None:
        await self.stream.close()


class PermittedRecognizer:
    def __init__(self, adapter: Recognizer, permission: ServicePermission | None, clock: Callable[[], float]):
        self.adapter, self.permission, self.clock = adapter, permission, clock

    async def open(self, language: str, subject: str | None = None) -> Recognition:
        require_permission(self.permission, self.clock)
        stream = await self.adapter.open(language, subject)
        return PermittedRecognition(stream, self.permission, self.clock)

    def minute_usd(self) -> float:
        return self.adapter.minute_usd()

    async def aclose(self) -> None:
        close = getattr(self.adapter, "aclose", None)
        if close is not None:
            await close()


class PermittedSynthesizer:
    def __init__(self, adapter: Synthesizer, permission: ServicePermission | None, clock: Callable[[], float]):
        self.adapter, self.permission, self.clock = adapter, permission, clock

    @property
    def voice(self) -> str:
        return self.adapter.voice

    def million_chars_usd(self) -> float:
        return self.adapter.million_chars_usd()

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        require_permission(self.permission, self.clock)
        audio = self.adapter.synthesize(text)
        try:
            async for chunk in audio:
                yield chunk
        finally:
            close = getattr(audio, "aclose", None)
            if close is not None:
                await close()

    async def warm(self) -> None:
        if allowed(self.permission, self.clock()):
            await self.adapter.warm()

    async def aclose(self) -> None:
        await self.adapter.aclose()


class PermittedLiveSession:
    def __init__(self, session: Any, permission: ServicePermission | None, clock: Callable[[], float]):
        self.session, self.permission, self.clock = session, permission, clock

    async def say(self, sentence: str) -> AsyncIterator[Voiced]:
        require_permission(self.permission, self.clock)
        output = self.session.say(sentence)
        try:
            async for event in output:
                yield event
        finally:
            close = getattr(output, "aclose", None)
            if close is not None:
                await close()

    async def close(self) -> None:
        await self.session.close()


class PermittedLive:
    def __init__(self, adapter: Any, permission: ServicePermission | None, clock: Callable[[], float]):
        self.adapter, self.permission, self.clock = adapter, permission, clock
        self.model_id = str(adapter.model_id)

    async def open(self) -> PermittedLiveSession:
        require_permission(self.permission, self.clock)
        return PermittedLiveSession(await self.adapter.open(), self.permission, self.clock)


def build_stack(config: GatewayConfig, secrets: SecretSource, clock: Callable[[], float]) -> SpeechStack:
    selection, stack = config.speech, SpeechStack()
    if config.voice_enabled and allowed(selection.recognition_permission, clock()):
        if selection.recognizer == "meta":
            from cloud_gateway.providers.meta_speech import MetaRecognizer

            key = secrets.get(config.voice_meta.secret)
            if key is not None:
                stack.recognizer = MetaRecognizer(config.voice_meta, key)
                stack.recognizer_provider, stack.recognizer_model = "meta", config.voice_meta.model
        elif selection.recognizer == "google":
            from cloud_gateway.providers.google_speech import ChirpRecognizer

            stack.recognizer = ChirpRecognizer(config.voice_google)
            stack.recognizer_provider, stack.recognizer_model = "google_speech", config.voice_google.stt_model
    if allowed(selection.synthesis_permission, clock()):
        if selection.synthesizer == "kokoro":
            from cloud_gateway.providers.kokoro_speech import KokoroSynthesizer

            stack.tts = KokoroSynthesizer(config.voice_tts)
            stack.synthesizer_provider = "kokoro"
        elif selection.synthesizer == "google":
            from cloud_gateway.providers.google_speech import ChirpSynthesizer

            stack.tts = ChirpSynthesizer(config.voice_google)
            stack.synthesizer_provider = "google_speech"
    if config.voice_enabled and selection.live == "google" and allowed(selection.live_permission, clock()):
        from cloud_gateway.providers.google_speech import google_live

        stack.live = google_live(config, secrets)
        stack.live_provider = config.voice_google.live_api
    return stack


def protect_stack(config: GatewayConfig, stack: SpeechStack, clock: Callable[[], float]) -> SpeechStack:
    """Covers injected adapters too; injection cannot bypass a product service-permission check."""
    stack = replace(stack)
    if stack.recognizer is not None:
        if stack.recognizer_model == "configured-recognizer":
            stack.recognizer_model = str(getattr(stack.recognizer, "model_id", stack.recognizer_model))
        if stack.recognizer_provider == "custom":
            stack.recognizer_provider = str(getattr(stack.recognizer, "provider_id", "custom"))
    if stack.tts is not None and stack.synthesizer_provider == "custom":
        stack.synthesizer_provider = str(getattr(stack.tts, "provider_id", "custom"))
    selected = config.speech
    if selected.recognizer == "none" or not allowed(selected.recognition_permission, clock()):
        stack.recognizer = None
    elif stack.recognizer is not None:
        stack.recognizer = PermittedRecognizer(stack.recognizer, selected.recognition_permission, clock)
    if selected.synthesizer == "none" or not allowed(selected.synthesis_permission, clock()):
        stack.tts = None
    elif stack.tts is not None:
        stack.tts = PermittedSynthesizer(stack.tts, selected.synthesis_permission, clock)
    if selected.live == "none" or not allowed(selected.live_permission, clock()):
        stack.live = None
    elif stack.live is not None:
        stack.live = PermittedLive(stack.live, selected.live_permission, clock)
    return stack
