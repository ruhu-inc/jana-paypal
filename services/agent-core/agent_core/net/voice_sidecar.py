"""Private speech-sidecar entry point. Launch configuration and both tokens arrive on stdin only (VOC-23).

This module loads heavy speech libraries only inside the separate speech bundle. Upstream is pinned; the build
must supply the exact verified revision. The tutor core itself never imports or installs these libraries.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import json
import logging
import os
import re
import secrets
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread
from typing import Any, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from agent_core.config.environments import Environment
from agent_core.net.local_address import is_literal_loopback
from agent_core.net.voice import LocalVoiceEndpoint
from agent_core.store.speech_packs import HardwareClass, SpeechPackError, SpeechProfile, held_trust, verify_pack

TOKEN = r"^[A-Za-z0-9_-]{32,256}$"  # noqa: S105 -- credential format, not a credential
# Text-to-speech engines this launcher loads from local pack paths only (D95): Qwen3-TTS, and Kokoro on the CPU from
# the pack's config, weights and voice file with misaki's G2P and no espeak-ng (`voice_speech.KokoroEngine`; upstream's
# own CPU Kokoro loader, which ignores its model path, is replaced by `local_kokoro`). Pocket accepts remote
# preset/config names and stays closed; a class whose pack offers only Pocket reads aloud through the OS synthesiser.
LOCAL_TTS_LOADERS = frozenset({"qwen3", "kokoro"})
# The Silero VAD as the pack ships it: the TorchScript weights at the root of the profile's `vad_path`, loaded without
# torch.hub (`local_silero`). The repository names upstream asks the hub for, with and without a branch.
SILERO_WEIGHTS = "silero_vad.jit"
SILERO_REPOSITORIES = frozenset({"snakers4/silero-vad", "snakers4/silero-vad:master"})


class SidecarLaunch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    protocol: int = Field(ge=1, le=1)
    pack_dir: str
    hardware: HardwareClass
    platform: str
    app_version: str
    environment: str
    adapter_url: str
    adapter_token: str = Field(pattern=TOKEN, repr=False)
    realtime_token: str = Field(pattern=TOKEN, repr=False)

    def adapter_base(self) -> str:
        parsed = urlsplit(self.adapter_url)
        try:
            valid = (
                parsed.scheme == "http"
                and parsed.hostname == "127.0.0.1"
                and parsed.port is not None
                and parsed.path == "/v1/voice/llm"
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise SpeechPackError("voice_adapter_url_refused")
        return self.adapter_url


class RealtimeGuard:
    """Authenticate before upstream can accept or claim a pipeline; expose no HTTP/proxy/WebRTC surface."""

    def __init__(self, app: Any, token: str) -> None:
        self.app, self._token = app, token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            headers = [
                v.decode("ascii", errors="replace")
                for k, v in scope.get("headers", [])
                if k.lower() == b"authorization"
            ]
            offered = [
                p.removeprefix("openai-insecure-api-key.")
                for p in scope.get("subprotocols", [])
                if p.startswith("openai-insecure-api-key.")
            ]
            tokens = [value.removeprefix("Bearer ") for value in headers if value.startswith("Bearer ")] + offered
            valid = (
                len(headers) <= 1
                and all(h.startswith("Bearer ") for h in headers)
                and len(tokens) == 1
                and re.fullmatch(TOKEN, tokens[0]) is not None
                and secrets.compare_digest(tokens[0], self._token)
            )
            if scope.get("path") != "/v1/realtime" or scope.get("query_string") or not valid:
                await send({"type": "websocket.close", "code": 4401})
                return
            # Do not echo the credential-bearing subprotocol or pass it to upstream logging.
            hidden = (b"authorization", b"sec-websocket-protocol")
            scope = {
                **scope,
                "subprotocols": [],
                "headers": [(k, v) for k, v in scope.get("headers", []) if k.lower() not in hidden],
            }
            await self.app(scope, receive, send)
            return
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})


def allow_connection(address: Any) -> bool:
    """Literal loopback only: no DNS or external socket, even if a dependency ignores HF_HUB_OFFLINE."""
    try:
        host = address[0] if isinstance(address, tuple) else address
        return isinstance(host, str) and is_literal_loopback(host)
    except (IndexError, TypeError):
        return False


def offline_process() -> None:
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")
    for key in list(os.environ):
        if key.lower().endswith("_proxy"):
            del os.environ[key]

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event == "socket.connect" and not allow_connection(args[1]):
            raise OSError("voice_external_network_refused")
        if event == "socket.getaddrinfo" and not allow_connection(args[0]):
            raise OSError("voice_dns_refused")

    sys.addaudithook(audit)


def argument_values(launch: SidecarLaunch, profile: SpeechProfile) -> dict[str, Any]:
    """Build the small fixed upstream configuration; pack paths are already verified and symlink-free."""
    root = Path(launch.pack_dir)
    if profile.tts not in LOCAL_TTS_LOADERS or (profile.tts == "kokoro" and profile.device != "cpu"):
        raise SpeechPackError("voice_local_tts_loader_unavailable")
    if profile.read_aloud_only or profile.stt_path is None or profile.vad_path is None:
        raise SpeechPackError("voice_profile_has_no_speech_input")  # served by `serve_speak`, never upstream
    # TABLE VOC-03a: high runs Qwen3-TTS with torch on CUDA and MLX on Apple silicon; mid runs the ggml build (the pack
    # ships the Q8_0 file for that class). TABLE VOC-05a: the low class (push-to-talk only) turns Smart Turn off and
    # keeps a short reopen window.
    backend = "mlx" if profile.device == "mps" else "ggml" if launch.hardware == "mid" else "torch"
    low = launch.hardware == "low"
    stt = (
        {
            "faster_whisper_stt_model_name": str(root / profile.stt_path),
            "faster_whisper_stt_device": profile.device,
            "faster_whisper_stt_compute_type": "int8",
            "faster_whisper_stt_gen_language": "en",
        }
        if profile.stt == "faster-whisper"
        else {"parakeet_tdt_model_name": str(root / profile.stt_path), "parakeet_tdt_device": profile.device}
    )
    return {
        "module": {
            "stt": profile.stt,
            "tts": profile.tts,
            "llm_backend": "chat-completions",
            "num_pipelines": 1,
            "enable_llm_proxy": False,
            "log_transcripts": False,
            "enable_live_transcription": False,
            "detect_llm_output_language": False,
        },
        "vad": {
            "vad": "silero",
            "min_silence_ms": 160,
            "speculative_reopen_ms": 200 if low else 1500,
            "unanswered_reopen_ms": 7000,
            "min_speech_ms": 384,
            "min_speech_continuation_ms": 192,
            "thresh": 0.6,
            "smart_turn": profile.turn_path is not None and not low,
            "smart_turn_model_path": str(root / profile.turn_path) if profile.turn_path and not low else None,
            "smart_turn_max_wait_ms": 4000,
            "smart_turn_incomplete_delay_ms": 800,
        },
        "stt": stt,
        "llm": {
            "model_name": "jana-tutor-voice",
            "init_chat_prompt": "jana-voice-unbound",
            "chat_size": 4,
            "compact_history": False,
            "stream_batch_sentences": 1,
            "responses_api_reasoning_effort": "none",
            "responses_api_disable_thinking": True,
            "responses_api_stream": True,
            "responses_api_base_url": launch.adapter_base(),
            "responses_api_api_key": launch.adapter_token,
        },
        "tts": {
            "qwen3_tts_model_name": str(root / profile.tts_path),
            "qwen3_tts_device": profile.device,
            "qwen3_tts_backend": backend,
            "qwen3_tts_speaker": profile.speaker,
            "qwen3_tts_language": "English",
            "qwen3_tts_ref_audio": None,
            "qwen3_tts_ref_spk": None,
        }
        if profile.tts == "qwen3"
        else {
            # The voice is the pack's .pt file: upstream's pipeline then reads it from disk (`local_kokoro`).
            "kokoro_model_name": str(root / profile.tts_path),
            "kokoro_device": "cpu",
            "kokoro_voice": str(root / profile.tts_path / "voices" / f"{profile.speaker}.pt"),
            "kokoro_lang_code": profile.speaker[0],
            "kokoro_speed": 1.0,
        },
    }


def build_arguments(launch: SidecarLaunch, profile: SpeechProfile) -> Any:
    pipeline = importlib.import_module("speech_to_speech.s2s_pipeline")
    registry = importlib.import_module("speech_to_speech.backend_registry")
    values = argument_values(launch, profile)
    selections = {}
    for stage, name in (("stt", profile.stt), ("llm", "chat-completions"), ("tts", profile.tts)):
        spec = getattr(registry, stage.upper() + "_BACKENDS")[name]
        selections[stage] = registry.BackendSelection(spec, spec.normalize(spec.config_type(**values[stage])))
    args = pipeline.ParsedArguments(
        module_kwargs=importlib.import_module("speech_to_speech.arguments_classes.module_arguments").ModuleArguments(
            **values["module"]
        ),
        realtime_server_kwargs=importlib.import_module(
            "speech_to_speech.arguments_classes.realtime_server_arguments"
        ).RealtimeServerArguments(host="127.0.0.1", port=0),
        local_audio_kwargs=importlib.import_module(
            "speech_to_speech.arguments_classes.local_audio_arguments"
        ).LocalAudioArguments(),
        vad_handler_kwargs=importlib.import_module(
            "speech_to_speech.arguments_classes.vad_arguments"
        ).VADHandlerArguments(**values["vad"]),
        stt_backend=selections["stt"],
        llm_backend=selections["llm"],
        tts_backend=selections["tts"],
    )
    pipeline.prepare_all_args(args)
    return args


def local_silero(root: Path) -> None:
    """Upstream's VAD handler asks `torch.hub` for Silero, which would import and run the repository's `hubconf.py`
    from the pack: Python code a pack supplies, run at launch (SEC-09). The weights load directly instead: the pack's
    TorchScript file through `torch.jit.load`, which runs TorchScript in torch's own interpreter and no Python from the
    pack. Any other hub request is refused, and nothing ever reaches torch.hub's own loader."""
    torch = importlib.import_module("torch")
    weights = root / SILERO_WEIGHTS
    if root.is_symlink() or weights.is_symlink() or not weights.is_file():
        raise SpeechPackError("voice_vad_weights_missing")

    def load(repo: str, model: str, *args: Any, **kwargs: Any) -> Any:
        if repo not in SILERO_REPOSITORIES or model != "silero_vad" or args or kwargs.get("onnx"):
            raise SpeechPackError("voice_torch_hub_refused")
        vad = torch.jit.load(str(weights), map_location="cpu")
        vad.eval()
        return vad, None  # upstream keeps the model and uses its own VAD iterator, not Silero's utilities

    def refused(*args: Any, **kwargs: Any) -> Any:
        raise SpeechPackError("voice_torch_hub_refused")

    torch.hub.load = load
    for name in ("load_state_dict_from_url", "download_url_to_file", "list", "help"):
        if hasattr(torch.hub, name):
            setattr(torch.hub, name, refused)


def local_nltk(root: Path) -> None:
    os.environ["NLTK_DATA"] = str(root)
    nltk: Any = importlib.import_module("nltk")
    nltk.data.path = [str(root)]
    for resource in ("tokenizers/punkt_tab", "taggers/averaged_perceptron_tagger_eng"):
        try:
            nltk.data.find(resource)
        except (LookupError, OSError):
            raise SpeechPackError("voice_nltk_data_missing") from None

    def no_download(*args: Any, **kwargs: Any) -> None:
        raise SpeechPackError("voice_runtime_download_refused")

    nltk.download = no_download


def local_kokoro(root: Path, profile: SpeechProfile) -> None:
    """Upstream's CPU Kokoro handler builds `KPipeline(lang_code, device)`, which fetches the model from the hub, at
    setup and again at every session end. Both build from the pack's files instead (`voice_speech.KokoroEngine`)."""
    handler = importlib.import_module("speech_to_speech.TTS.kokoro_handler").KokoroTTSHandler
    from agent_core.net.voice_speech import KokoroEngine

    loaded: list[Any] = []

    def setup_kokoro(self: Any) -> None:
        if not loaded:
            loaded.append(KokoroEngine(root / profile.tts_path, profile.speaker).pipeline)
        self.backend, self.pipeline = "kokoro", loaded[0]

    def on_session_end(self: Any) -> None:
        self.voice, self.lang_code = self._initial_voice, self._initial_lang_code

    handler._setup_kokoro = setup_kokoro
    handler.on_session_end = on_session_end


def prompt_sentences() -> None:
    """Upstream speaks a sentence only once the next one starts arriving (the tokenizer cannot tell the last sentence is
    complete), so the keyed filler and every accepted turn's first sentence wait for more text. A sentence that ends
    in . ! or ? followed by a space is complete: an empty last piece flushes it to text-to-speech at once (VOC-12)."""
    module: Any = importlib.import_module("speech_to_speech.LLM.base_openai_compatible_language_model")
    tokenize = cast(Callable[..., list[str]], module.sent_tokenize)

    def sent_tokenize(text: str, *args: Any, **kwargs: Any) -> list[str]:
        sentences: list[str] = tokenize(text, *args, **kwargs)
        if sentences and re.search(r"[.!?][\"')\]]?\s+$", text):
            return [*sentences, ""]
        return sentences

    module.sent_tokenize = sent_tokenize


def engine_tools_only() -> None:
    handler = importlib.import_module("speech_to_speech.LLM.chat_completions_language_model")

    def optional_kwargs(self: Any, tools: Any, choice: Any) -> dict[str, Any]:
        if tools or choice not in (None, "auto", "none"):
            raise SpeechPackError("voice_sidecar_tools_refused")
        # Upstream forwards the session's default tool_choice even with tools=[]; our adapter accepts no tools.
        return {}

    handler.ChatCompletionsApiModelHandler._build_optional_kwargs = optional_kwargs


async def serve(launch: SidecarLaunch, control_out: Any) -> None:
    # The core verified the signature against the live revocation list and hashed every byte for this launch
    # (D88, SEC-16); the shell checked the trust chain, the revocations and the entry point's digest. Here: the
    # signature against the anchors, the list this build shipped with and the device's held list (a revocation adopted
    # since the core's check still stops this launch), layout and sizes, without hashing gigabytes a second time.
    pack_dir = Path(launch.pack_dir)
    environment = Environment(launch.environment)
    manifest = verify_pack(
        pack_dir,
        launch.hardware,
        launch.platform,
        launch.app_version,
        environment,
        trust=held_trust(pack_dir.parent.parent, environment),
        hashes=False,
    )
    profile = manifest.profiles[launch.hardware]
    launch.adapter_base()
    offline_process()
    root = Path(launch.pack_dir).resolve()
    if profile.read_aloud_only:
        await serve_speak(launch, profile, root, control_out)
        return
    spec = importlib.util.find_spec("speech_to_speech")
    if spec is None or spec.origin is None or not Path(spec.origin).resolve().is_relative_to(root):
        raise SpeechPackError("voice_upstream_outside_verified_pack")
    assert profile.vad_path is not None  # a profile with speech input names its VAD (read_manifest)
    local_nltk((root / profile.sidecar_path).parent / "nltk_data")
    local_silero(root / profile.vad_path)
    if profile.tts == "kokoro":
        local_kokoro(root, profile)
    engine_tools_only()
    prompt_sentences()
    args = build_arguments(launch, profile)
    stop = Event()
    runtime = importlib.import_module("speech_to_speech.configured_runtime")._build_runtime({"jana": args}, stop)
    server, sock, task = None, None, None
    try:
        create_app = importlib.import_module("speech_to_speech.api.openai_realtime.websocket_router").create_app
        app = RealtimeGuard(
            create_app(pool=runtime.pools["jana"], stop_event=stop, llm_proxy_config=None),
            launch.realtime_token,
        )
        uvicorn = importlib.import_module("uvicorn")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        port = sock.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                access_log=False,
                log_config=None,
                lifespan="on",
                ws_max_size=512000,
                ws_max_queue=8,
                timeout_graceful_shutdown=2,
            )
        )
        runtime.start()
        task = asyncio.create_task(server.serve(sockets=[sock]))
        endpoint = LocalVoiceEndpoint(f"ws://127.0.0.1:{port}/v1/realtime", launch.realtime_token)
        async with asyncio.timeout(15):
            while not server.started:
                if task.done():
                    raise SpeechPackError("voice_listener_failed")
                await asyncio.sleep(0.02)
            probe = await endpoint.open()
            try:
                if (await probe.receive()).get("type") != "session.created":
                    raise SpeechPackError("voice_probe_failed")
            finally:
                await probe.close()
            while any(unit.session is not None for pool in runtime.pools.values() for unit in pool):
                await asyncio.sleep(0.01)  # SESSION_END must drain before the bridge claims the sole unit
        control_out.write(json.dumps({"type": "voice.ready", "protocol": 1, "port": port}) + "\n")
        control_out.flush()

        # The shell keeps stdin open; EOF also stops voice after a shell crash.
        def parent_closed() -> None:
            while sys.stdin.buffer.read(1):
                pass
            stop.set()

        Thread(target=parent_closed, daemon=True).start()
        while not task.done():
            if stop.is_set() or runtime._stop.is_set():
                server.should_exit = True
            await asyncio.sleep(0.05)
        await task
    finally:
        stop.set()
        if server is not None:
            server.should_exit = True
        if task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), 3)
        if sock is not None:
            sock.close()
        runtime.stop()


async def serve_speak(launch: SidecarLaunch, profile: SpeechProfile, root: Path, control_out: Any) -> None:
    """A read-aloud-only profile (`stt: none`): the speak directive alone on the Realtime subset, with the pack's voice
    loaded and warmed before readiness so the first read pays no model load (VOC-18, VOC-39)."""
    from http import HTTPStatus

    from agent_core.config import Settings
    from agent_core.net.voice_speech import AdapterText, SpeakServer, load_engine

    settings = Settings()
    engine = await asyncio.to_thread(
        load_engine, root, profile, qwen3_interval_s=settings.voice_qwen3_stream_interval_s
    )
    await asyncio.to_thread(lambda: [None for _ in engine.stream("Ready.")])
    speaker = SpeakServer(
        engine,
        AdapterText(launch.adapter_base(), launch.adapter_token),
        launch.realtime_token,
        first_clause_words=settings.read_aloud_first_clause_words,
    )
    serving: Any = importlib.import_module("websockets.asyncio.server")

    def admit(connection: Any, request: Any) -> Any:
        if speaker.authorised(request.path, request.headers):
            return None
        return connection.respond(HTTPStatus.UNAUTHORIZED, "")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    async with serving.serve(
        speaker.session,
        "127.0.0.1",
        0,
        process_request=admit,
        compression=None,
        max_size=512_000,
        max_queue=8,
        ping_interval=20,
    ) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        probe = await LocalVoiceEndpoint(f"ws://127.0.0.1:{port}/v1/realtime", launch.realtime_token).open()
        try:
            if (await probe.receive()).get("type") != "session.created":
                raise SpeechPackError("voice_probe_failed")
        finally:
            await probe.close()
        async with asyncio.timeout(5):
            while speaker.active:
                await asyncio.sleep(0.01)  # the probe's session ends before the bridge claims the one unit
        control_out.write(json.dumps({"type": "voice.ready", "protocol": 1, "port": port}) + "\n")
        control_out.flush()

        def parent_closed() -> None:
            while sys.stdin.buffer.read(1):
                pass
            loop.call_soon_threadsafe(stop.set)

        Thread(target=parent_closed, daemon=True).start()
        await stop.wait()


def main() -> int:
    control_out = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
    # Upstream console/log messages are not a control channel and may contain transcript or provider diagnostics.
    logging.disable(logging.CRITICAL)
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        try:
            raw = sys.stdin.buffer.readline(65537)
            if len(raw) > 65536 or not raw.endswith(b"\n"):
                raise SpeechPackError("voice_launch_limit")
            launch = SidecarLaunch.model_validate_json(raw)
            asyncio.run(serve(launch, control_out))
            return 0
        except Exception:
            control_out.write('{"type":"voice.error","code":"voice_start_failed"}\n')
            control_out.flush()
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
