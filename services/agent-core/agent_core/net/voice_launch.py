"""Core half of lazy, shell-owned speech startup (VOC-23). No process or model is started by the core.

The sidecar starts on first use, except that a profile with read-aloud on has it started early, in the background,
when the profile opens, when read-aloud is turned on and when a speech pack is installed (D96): the voice holds about
2.6 GB, so nothing else starts it early, and an early start that nothing has used is stopped when read-aloud is
turned off or the profile closes.

Which pack and profile a device runs follows TABLE VOC-03a: content-only computers run no sidecar (read-aloud uses the
OS synthesiser); low runs the sidecar for read-aloud and dictation only (push-to-talk tutoring is S4); mid and high
also run voice tutoring when the voice stack and the tutor model fit memory with the VOC-03 headroom. Every launch
re-verifies the selected pack, byte for byte, against the live revocation list (SEC-16, D88), and mints a fresh
adapter token for that sidecar launch alone (VOC-23).
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from agent_core.config.environments import Environment
from agent_core.contracts.control.control import VoiceEndpoint, VoiceFailed, VoiceStart
from agent_core.net.voice import LocalVoiceEndpoint, RealtimeSocket, VoiceTransportError
from agent_core.net.voice_sidecar import LOCAL_TTS_LOADERS
from agent_core.store.speech_packs import (
    NOT_APPLICABLE,
    PACK_ID,
    HardwareClass,
    SpeechManifest,
    SpeechPackError,
    SpeechPackStore,
    Trust,
    pack_version,
    read_manifest,
)

DeviceClass = HardwareClass | Literal["content_only"]
# TABLE VOC-03a (D95): the text-to-speech engines each class may run, in order of preference. Kokoro-82M `af_heart` on
# the low and mid classes (Qwen3-TTS on a mid laptop's CPU ran at 1.8x real time in the bake-off); Qwen3-TTS 0.6B
# `Ryan` on the high class (MLX on Apple silicon), with Kokoro where a pack offers no Qwen3 for that platform.
TTS_BY_CLASS: dict[str, tuple[str, ...]] = {
    "low": ("kokoro", "pocket"),
    "mid": ("kokoro", "qwen3"),
    "high": ("qwen3", "kokoro"),
}
TUTORING_CLASSES = ("mid", "high")  # D40: voice tutoring in S3; low-class push-to-talk is S4 (TABLE PRD-15a)


def class_bytes(manifest: SpeechManifest, hardware: HardwareClass) -> int:
    """What the selected class installs: the size the installer shows (VOC-03)."""
    return sum(file.size_bytes for file in manifest.files if hardware in file.classes)


def memory_budget(
    plan: Mapping[str, Any] | None,
    installed_bytes: int | None,
    headroom_gb: float,
    resident_gb: Sequence[float] = (0.0, 0.0, 0.0),
) -> Callable[[SpeechManifest, HardwareClass], bool]:
    """VOC-03: the voice stack plus the planned tutor model keep `headroom_gb` below installed RAM. The voice stack's
    memory is its measured peak by class (`speech_pack_resident_gb`, low, mid, high): Kokoro's torch runtime peaks
    near 2.6 GB from 0.3 GB of files, so file sizes would understate it. Without a local model plan (scripted, cloud
    only) or a memory probe there is nothing resident to compete with."""

    def fits(manifest: SpeechManifest, hardware: HardwareClass) -> bool:
        attempts = (plan or {}).get("attempts") or []
        resident = attempts[0].get("resident_bytes") if attempts else None
        if not isinstance(resident, int) or installed_bytes is None:
            return True
        measured = resident_gb[("low", "mid", "high").index(hardware)] if len(resident_gb) == 3 else 0.0
        voice = max(int(measured * 1024**3), class_bytes(manifest, hardware))
        return resident + voice + int(headroom_gb * 1024**3) <= installed_bytes

    return fits


class ReadyVoiceConnection:
    """The readiness frame belongs to the bridge too; preserve it after probing a newly claimed connection."""

    def __init__(self, socket: RealtimeSocket, first: dict[str, Any]) -> None:
        self.socket = socket
        self.first: dict[str, Any] | None = first

    async def send(self, frame: dict[str, Any]) -> None:
        await self.socket.send(frame)

    async def receive(self) -> dict[str, Any]:
        if self.first is not None:
            first, self.first = self.first, None
            return first
        return await self.socket.receive()

    async def close(self) -> None:
        await self.socket.close()


class VoiceLaunch:
    def __init__(
        self,
        root: Path,
        hardware: DeviceClass,
        platform: str,
        app_version: str,
        environment: Environment,
        adapter_url: str,
        adapter_token: str,
        shell: Callable[[dict[str, Any]], None],
        *,
        start_timeout_s: float = 90,
        trust: Trust = None,
        memory_fits: Callable[[SpeechManifest, HardwareClass], bool] | None = None,
    ) -> None:
        self.device_class: DeviceClass = hardware
        store_class: HardwareClass = "low" if hardware == "content_only" else hardware
        self.store = SpeechPackStore(root, environment, platform, app_version, store_class, trust=trust)
        self.memory_fits = memory_fits
        self.adapter_url, self._adapter_token, self.shell = adapter_url, adapter_token, shell
        # Told each new adapter token, so the adapter accepts only the current sidecar launch's (VOC-23).
        self.on_token: Callable[[str], None] | None = None
        self.on_change: Callable[[], None] | None = None
        self.start_timeout_s = start_timeout_s
        self.selected: tuple[Path, SpeechManifest] | None = None
        self._request: str | None = None
        self._endpoint: LocalVoiceEndpoint | None = None
        self._waiting: asyncio.Future[LocalVoiceEndpoint] | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._warm_wanted = False  # the open profile has read-aloud on (D96)
        self._warm_only = False  # the running or starting sidecar was started early and nothing has used it yet
        self._warm_tasks: set[asyncio.Task[bool]] = set()

    @property
    def tutoring(self) -> bool:
        """Voice turns are offered (mid and high, within the memory budget, with a pack that hears speech); otherwise
        only read-aloud, and dictation where the pack has speech-to-text."""
        if self.selected is None or self.store.hardware not in TUTORING_CLASSES or not self.dictation:
            return False
        return self.memory_fits is None or self.memory_fits(self.selected[1], self.store.hardware)

    @property
    def dictation(self) -> bool:
        """The selected pack hears speech: a read-aloud-only profile (`stt: none`, TABLE VOC-03a) offers no
        dictation and no voice turns, only read-aloud through its voice (VOC-14, VOC-46)."""
        return self.selected is not None and not self.selected[1].profiles[self.store.hardware].read_aloud_only

    def rescan(self) -> bool:
        """A speech pack was installed or removed while the app runs: offer it without a restart."""
        found = self.scan(manifest_only=True)
        if self.on_change is not None:
            self.on_change()
        if found and self._warm_wanted:
            self._schedule_warm()
        return found

    def want_warm(self, read_aloud: bool) -> None:
        """The open profile's `read_aloud` preference, at profile open and whenever it changes; False when the profile
        closes (VOC-23, D96). On: start the sidecar in the background so the first read-aloud does not wait for the
        voice to load. Off: stop a sidecar that was started early and nothing has used, never one in use."""
        self._warm_wanted = read_aloud
        if read_aloud:
            self._schedule_warm()
        elif self._warm_only:
            self._warm_only = False
            self._stop()

    def _schedule_warm(self) -> None:
        if self._closed or self.device_class == "content_only" or self.selected is None:
            return
        try:
            task = asyncio.get_running_loop().create_task(self.prewarm())
        except RuntimeError:  # no event loop: nothing to start in the background
            return
        # Queued behind the lock: one already starting or running the sidecar makes this one a no-op.
        self._warm_tasks.add(task)
        task.add_done_callback(self._warm_tasks.discard)

    async def prewarm(self) -> bool:
        """Start the sidecar now, without opening a connection, if read-aloud still wants it and none runs. A failed
        start changes nothing: the next use tries again, and read-aloud falls back to the OS voice (VOC-18)."""
        async with self._lock:
            if self._closed or not self._warm_wanted or self._endpoint is not None or self._waiting is not None:
                return False
            self._warm_only = True
            try:
                await self._started()
            except VoiceTransportError:
                self._warm_only = False
                return False
            except asyncio.CancelledError:  # stopped while it loaded: read-aloud was turned off or the profile closed
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                return False
            return True

    def release(self, pack_id: str) -> None:
        """A speech pack is about to be removed: stop a sidecar running from it and stop offering it (VOC-03)."""
        if self.selected is not None and self.selected[0].name == pack_id:
            self._stop()
            self.selected = None
            if self.on_change is not None:
                self.on_change()

    def scan(self, *, manifest_only: bool = False) -> bool:
        # A refused or missing pack leaves typed tutoring unchanged; read-aloud then uses the OS synthesiser.
        self.selected = None
        if self.device_class == "content_only":
            return False
        engines = [engine for engine in TTS_BY_CLASS[self.store.hardware] if engine in LOCAL_TTS_LOADERS]
        candidates = []
        refused: list[tuple[int, ...]] = []  # versions of installed packs that failed their checks
        for folder in self.store.root.glob("sp.en.*"):
            try:
                manifest = (
                    read_manifest(folder, self.store.environment, trust=self.store.current_trust())
                    if manifest_only
                    else self.store.verify(folder)
                )
                if manifest.platform != self.store.platform or self.store.hardware not in manifest.profiles:
                    continue
                tts = manifest.profiles[self.store.hardware].tts
                if tts not in engines:
                    continue
                candidates.append(((pack_version(manifest.speech_pack_id), -engines.index(tts)), folder, manifest))
            except (SpeechPackError, OSError) as error:
                if str(error) not in NOT_APPLICABLE and PACK_ID.match(folder.name):
                    refused.append(pack_version(folder.name))
        if candidates:
            (version, _), folder, manifest = max(candidates, key=lambda candidate: candidate[0])
            # SEC-16: while the newest pack is revoked or damaged, voice waits for a fixed pack; it never falls back to
            # an older build the newer one replaced.
            if version >= max(refused, default=()):
                self.selected = folder, manifest
        return self.selected is not None

    async def connect(self) -> ReadyVoiceConnection:
        async with self._lock:
            if self._closed:
                raise VoiceTransportError("voice_closed")
            self._warm_only = False  # in use: an early start is now kept like any other
            endpoint = await self._started()
        return await self._ready(endpoint)

    async def _started(self) -> LocalVoiceEndpoint:
        """The running sidecar's endpoint, asking the shell to start it first if none runs. Called under the lock."""
        if self._endpoint is not None:
            try:
                if self.selected is None:
                    raise SpeechPackError("voice_pack_unavailable")
                read_manifest(self.selected[0], self.store.environment, trust=self.store.current_trust())
            except (SpeechPackError, OSError):
                self._stop()
                raise VoiceTransportError("voice_pack_unavailable") from None
        if self._endpoint is None:
            if self._waiting is None:
                if not await asyncio.to_thread(self.scan) or self.selected is None:
                    raise VoiceTransportError("voice_pack_unavailable")
                folder, manifest = self.selected
                profile = manifest.profiles[self.store.hardware]
                program = next(
                    (
                        file
                        for file in manifest.files
                        if file.path == profile.sidecar_path
                        and file.role == "sidecar"
                        and self.store.hardware in file.classes
                    ),
                    None,
                )
                if program is None:
                    raise VoiceTransportError("voice_entrypoint_missing")
                self._request = secrets.token_hex(16)
                self._rotate_token()
                self._waiting = asyncio.get_running_loop().create_future()
                frame = VoiceStart.model_validate(
                    {
                        "type": "control.voice_start",
                        "request_id": self._request,
                        "program": str(folder / program.path),
                        "program_sha256": program.sha256,
                        "pack_dir": str(folder),
                        "hardware": self.store.hardware,
                        "platform": self.store.platform,
                        "app_version": self.store.app_version,
                        "environment": self.store.environment.value,
                        "adapter_url": self.adapter_url,
                        "adapter_token": self._adapter_token,
                    }
                )
                self.shell(frame.model_dump())
            try:
                self._endpoint = await asyncio.wait_for(asyncio.shield(self._waiting), self.start_timeout_s)
            except (TimeoutError, VoiceTransportError):
                self._stop()
                raise VoiceTransportError("voice_start_failed") from None
        if self._endpoint is None:
            raise VoiceTransportError("voice_start_failed")
        return self._endpoint

    async def _ready(self, endpoint: LocalVoiceEndpoint) -> ReadyVoiceConnection:
        try:
            deadline = time.monotonic() + 3
            while True:
                socket = await endpoint.open()
                claimed = False
                try:
                    first = await asyncio.wait_for(socket.receive(), max(0.01, deadline - time.monotonic()))
                    if first.get("type") == "session.created":
                        claimed = True
                        return ReadyVoiceConnection(socket, first)
                    error = first.get("error")
                    busy = (
                        first.get("type") == "error"
                        and isinstance(error, dict)
                        and error.get("type") == "session_limit_reached"
                    )
                    if not busy or time.monotonic() >= deadline:
                        raise VoiceTransportError("voice_not_ready")
                finally:
                    if not claimed:
                        await socket.close()
                # A socket close acknowledges transport loss before the worker chain's SESSION_END has drained.
                await asyncio.sleep(0.05)
        except (VoiceTransportError, TimeoutError):
            if self._endpoint is endpoint:
                self._endpoint, self._waiting = None, None
            raise VoiceTransportError("voice_not_ready") from None

    def on_shell(self, message: dict[str, Any]) -> None:
        if self._closed or message.get("request_id") != self._request:
            return
        if message["type"] == "control.voice_endpoint":
            frame = VoiceEndpoint.model_validate(message)
            endpoint = LocalVoiceEndpoint(frame.url, frame.token)
            self._endpoint = endpoint
            if self._waiting is not None and not self._waiting.done():
                self._waiting.set_result(endpoint)
        elif message["type"] == "control.voice_failed":
            VoiceFailed.model_validate(message)
            self._endpoint = None
            self._rotate_token()
            if self._waiting is not None and not self._waiting.done():
                self._waiting.set_exception(VoiceTransportError("voice_start_failed"))
            else:
                self._waiting = None

    def _rotate_token(self) -> None:
        self._adapter_token = secrets.token_urlsafe(32)
        if self.on_token is not None:
            self.on_token(self._adapter_token)

    def _stop(self) -> None:
        if self._request is not None:
            self.shell({"type": "control.voice_stop", "request_id": self._request})
            self._rotate_token()  # the stopped launch's token no longer opens the adapter
        if self._waiting is not None and not self._waiting.done():
            self._waiting.cancel()
        self._waiting, self._endpoint, self._request = None, None, None

    async def close(self) -> None:
        self._closed = True
        for task in list(self._warm_tasks):
            task.cancel()
        self._stop()
        self._adapter_token = ""
