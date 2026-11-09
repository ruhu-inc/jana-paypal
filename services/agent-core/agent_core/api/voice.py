"""Authenticated voice socket host with an injected Realtime connection (VOC-26/31).

The production app leaves this disabled until sidecar supervision and the provisional-turn adapter are attached.
Only this core-side host talks to a Realtime connection. UI state/captions go through the existing session stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from agent_core.api.core import CURRENT_PROFILE, CoreService
from agent_core.api.errors import ApiError
from agent_core.api.security import websocket_credential
from agent_core.engine.events import new_id
from agent_core.engine.pointing import Cue
from agent_core.engine.voice_bindings import VoiceBinding, VoiceBindings
from agent_core.engine.voice_bridge import Effects, Silence, VoiceBridge, VoiceProtocolError
from agent_core.engine.voice_place import Place, parse_place

logger = logging.getLogger(__name__)
# Launch failures (agent_core.net.voice_launch codes) and the reason copy each shows (VOC-14).
LAUNCH_REASONS = {
    "voice_pack_unavailable": "voice.pack_unavailable",
    "voice_entrypoint_missing": "voice.pack_unavailable",
    "voice_start_failed": "voice.stopped_working",
    "voice_not_ready": "voice.stopped_working",
}

if TYPE_CHECKING:
    from agent_core.api.voice_activities import VoiceActivities
    from agent_core.api.voice_adapter import VoiceTutorAdapter


class RealtimeConnection(Protocol):
    async def send(self, event: dict[str, Any]) -> None: ...
    async def receive(self) -> dict[str, Any]: ...
    async def close(self) -> None: ...


@dataclass
class _Active:
    bridge: VoiceBridge
    socket: WebSocket
    task: asyncio.Task[None] | None = None
    close_code: int = 1000
    dispatch: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    effects: Callable[[Effects], Coroutine[Any, Any, None]] | None = None
    path: str = "local_sidecar"  # the turn trace's voice path (VOC-22): the local sidecar or cloud voice (VOC-28)
    # The open chat this conversation belongs to (VOC-53, D130); None: read-aloud and dictation only.
    place: Place | None = None


class VoiceRuntime:
    def __init__(self, core: CoreService, connect: Callable[[], Awaitable[RealtimeConnection]],
                 on_action: Callable[[dict[str, Any]], Awaitable[None]], *, snapshots: bool = False,
                 strict: bool = False, connect_timeout_s: float = 10.0) -> None:  # fmt: skip
        self.core, self.connect, self.on_action = core, connect, on_action
        self.snapshots, self.strict = snapshots, strict
        self.connect_timeout_s = connect_timeout_s
        self.bindings = VoiceBindings(core.epoch)
        self._active: _Active | None = None
        self._switch = asyncio.Lock()
        self.adapter: VoiceTutorAdapter | None = None
        self.activities: VoiceActivities | None = None
        self.on_closed: Callable[[VoiceBinding], Awaitable[None]] | None = None
        self.on_failed: Callable[[VoiceBinding], Awaitable[None]] | None = None
        # Chooses the endpoint for each voice start (VOC-27, VOC-28): set when cloud voice is built in; otherwise
        # every start uses `connect`, the local sidecar.
        self.route: Callable[[VoiceBinding], Awaitable[RealtimeConnection]] | None = None
        # Sessions whose student was shown the calm screen by a voice turn: voice stays idle there, across voice
        # off and on, until the client resumes with a `mode` control; profile lock or switch clears it (VOC-20).
        self.calm_sessions: dict[str, str | None] = {}
        # Whether voice turns are offered here (TABLE VOC-03a, VOC-03 memory); read-aloud and dictation regardless.
        self.tutoring: Callable[[], bool] = lambda: True
        # Whether the selected pack hears speech; a read-aloud-only pack (`stt: none`) offers no dictation (VOC-46).
        self.dictation: Callable[[], bool] = lambda: True
        self._background: set[asyncio.Task[None]] = set()
        core.voice_close = self.close
        core.on_profile_forgotten.append(self._forget_profile)
        core.closers.insert(0, self.close)

    def _forget_profile(self, profile_id: str) -> None:
        self.calm_sessions = {s: p for s, p in self.calm_sessions.items() if p != profile_id}
        self.bindings.revoke_profile(profile_id)
        active = self._active
        if active is not None and active.bridge.binding.profile_id == profile_id and active.task is not None:
            active.close_code = 4401
            active.task.cancel()

    async def _stop(self, code: int) -> None:
        active = self._active
        if active is not None and active.task is not None:
            active.close_code = code
            active.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active.task
            if self._active is active:
                self._active = None

    async def close(self) -> None:
        self.bindings.clear()
        async with self._switch:
            await self._stop(1001)

    def authorise_response(self, binding: VoiceBinding, response_id: str, turn_id: str, spoken_text: str) -> None:
        active = self._active
        if active is None or active.bridge.binding is not binding or not self.bindings.contains(binding):
            raise VoiceProtocolError("voice_session_not_current")
        active.bridge.authorise_response(response_id, turn_id, spoken_text)

    def place_of(self, binding: VoiceBinding) -> Place | None:
        """The open chat the bound conversation is in (VOC-53); None when the socket offers no conversation."""
        active = self._active
        return active.place if active is not None and active.bridge.binding is binding else None

    def path_of(self, binding: VoiceBinding) -> str:
        active = self._active
        return active.path if active is not None and active.bridge.binding is binding else "local_sidecar"

    def current_response(self, binding: VoiceBinding) -> str | None:
        active = self._active
        if active is None or active.bridge.binding is not binding or not self.bindings.contains(binding):
            return None
        response = active.bridge._response
        return response.response_id if response is not None else None

    def begin_turn(self, binding: VoiceBinding, turn_id: str, *, replace: bool = False) -> bool:
        active = self._active
        if active is None or active.bridge.binding is not binding or not self.bindings.contains(binding):
            return False
        return active.bridge.begin_provisional(turn_id, replace=replace)

    def turn_pending(self, binding: VoiceBinding, turn_id: str) -> bool:
        """The bridge still waits for this provisional turn: no barge-in, stop or close since `begin_turn`."""
        active = self._active
        return (
            active is not None
            and active.bridge.binding is binding
            and self.bindings.contains(binding)
            and not active.bridge.closed
            and active.bridge._pending_turn == turn_id
        )

    def authorise_turn(self, binding: VoiceBinding, turn_id: str, spoken_text: str, cues: tuple[Cue, ...] = ()) -> None:
        active = self._active
        if active is None or active.bridge.binding is not binding or not self.bindings.contains(binding):
            raise VoiceProtocolError("voice_session_not_current")
        active.bridge.authorise_turn(turn_id, spoken_text, cues)

    def authorise_filler(self, binding: VoiceBinding, turn_id: str, filler: str) -> None:
        active = self._active
        if active is None or active.bridge.binding is not binding or not self.bindings.contains(binding):
            raise VoiceProtocolError("voice_session_not_current")
        active.bridge.authorise_filler(turn_id, filler)

    def _apply_later(self, active: _Active, effects: Effects) -> None:
        if active.effects is not None and (effects.upstream or effects.events or effects.ui or effects.actions):
            task = asyncio.get_running_loop().create_task(active.effects(effects))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    def await_work(self, binding: VoiceBinding, awaiting: bool) -> None:
        """VOC-05: the thinking extension while the tutor awaits worked input; restored when the next turn starts."""
        active = self._active
        if active is not None and active.bridge.binding is binding and self.bindings.contains(binding):
            self._apply_later(active, active.bridge.await_work(awaiting))

    def hold_calm(self, binding: VoiceBinding) -> None:
        """Distress was shown in this session (PRV-14): voice returns to idle until the student resumes (VOC-20)."""
        self.calm_sessions[binding.session_id] = binding.profile_id
        active = self._active
        if active is None or active.bridge.binding.session_id != binding.session_id:
            return
        self._apply_later(active, active.bridge.hold_calm())

    async def stop(self, binding: VoiceBinding) -> None:
        active = self._active
        if active is not None and active.bridge.binding is binding and active.dispatch is not None:
            await active.dispatch({"type": "stop"})

    async def serve(self, socket: WebSocket, session_id: str) -> None:
        try:
            self.core._guard()
            self.core.assessment_guard()
            entry = self.core.entry(session_id)
            if entry.meta.get("archived") or (self.core.mock_active is not None and self.core.mock_active()):
                raise ApiError("not_found")
        except ApiError:
            await socket.close(code=4404)
            return
        async with self._switch:
            await self._stop(1000)  # close and reset the old pipeline before claiming another one
            binding = self.bindings.mint(session_id, CURRENT_PROFILE.get())
            settings = self.core.settings
            silence = Silence(settings.voice_silence_ms, settings.voice_thinking_extension_ms,
                              settings.voice_hold_to_think_ms)  # fmt: skip
            bridge = VoiceBridge(binding, snapshots=self.snapshots, strict=self.strict, silence=silence)
            active = _Active(bridge, socket)
            active.bridge.calm = session_id in self.calm_sessions
            # D130: no conversation until the client names its open chat with a `place` control (VOC-53).
            active.bridge.tutoring = False
            self._active = active
            active.task = asyncio.create_task(self._run(active))
        try:
            await active.task
        except asyncio.CancelledError:
            # Session switching/profile close cancels the child, not the new socket's handler.
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        finally:
            if self._active is active:
                self._active = None

    def _place(self, active: _Active, control: dict[str, Any]) -> Effects:
        """VOC-53, D130: the conversation's open chat (Ask anything or a study set's Chat), or none. Leaving a place
        stops what it was saying; with no place only read-aloud and dictation remain."""
        place = parse_place(control)
        changed = place != active.place
        active.place = place
        effects = Effects()
        if changed and active.bridge.tutoring:
            effects = active.bridge.offer_conversation(False)  # what the old place was saying stops
        if place is not None and self.tutoring():
            later = active.bridge.offer_conversation(True)
            for name in ("upstream", "ui", "events", "actions"):
                getattr(effects, name).extend(getattr(later, name))
        return effects

    def _live(self, active: _Active) -> bool:
        binding = active.bridge.binding
        entry = self.core.sessions.get(binding.session_id)
        if self.core.shutting_down or self.core.epoch != self.bindings.epoch or not self.bindings.contains(binding):
            active.close_code = 4401
            return False
        directory = self.core.directory
        if directory is not None and directory.exists():
            presented = websocket_credential(active.socket.scope, "jana.profile.")
            if directory.profile_of(presented) != binding.profile_id or binding.profile_id is None:
                active.close_code = 4401
                return False
        if entry is None or entry.meta.get("archived") or entry.profile_id != binding.profile_id:
            active.close_code = 4404
            return False
        if self.core.mock_active is not None and self.core.mock_active():
            active.close_code = 4403
            return False
        return True

    async def _run(self, active: _Active) -> None:
        bridge, socket = active.bridge, active.socket
        endpoint: RealtimeConnection | None = None
        tasks: list[asyncio.Task[None]] = []
        serial = asyncio.Lock()
        unavailable_reason: str | None = None

        async def act(action: dict[str, Any]) -> None:
            if self.activities is not None:
                if action["type"] == "dictation_transcript":
                    self.activities.transcript(bridge.binding, action["capture_id"], action["text"])
                    return
                if action["type"] == "dictation_finished":
                    self.activities.finish_dictation(action["capture_id"], action["cancelled"])
                    # Late speculative requests retain the old empty-only binding. Subsequent speech gets a new one.
                    if self.bindings.contains(bridge.binding):
                        bridge.binding = self.bindings.mint(bridge.binding.session_id, bridge.binding.profile_id)
                        assert endpoint is not None
                        await endpoint.send(bridge._config())
                    return
                if action["type"] == "read_finished":
                    item = self.activities.utterances.get(action["utterance_id"])
                    if item is not None:
                        item.ended = True
                    return
            scoped = {**action, "binding_id": bridge.binding.binding_id, "session_id": bridge.binding.session_id}
            await asyncio.wait_for(self.on_action(scoped), timeout=2.0)

        async def apply(effects: Effects, *, closing: bool = False) -> None:
            assert endpoint is not None
            # Flush playback immediately. Release the provisional engine state before forwarding its first audio.
            for item in effects.ui:
                if isinstance(item, dict):
                    await socket.send_json(item)
            for action in effects.actions:
                await act(action)
            if not closing and not self._live(active):
                raise VoiceProtocolError("voice_binding_ended")
            for event in effects.events:
                self.core._announce(bridge.binding.session_id, event)
            for item in effects.ui:
                if isinstance(item, bytes):
                    await socket.send_bytes(item)
            for event in effects.upstream:
                if event.get("type") == "session.update":
                    # A completed dictation can rotate the binding in this same effect batch. Never send a
                    # pre-rotation instructions snapshot after the fresh binding's configuration.
                    event = {**event, "session": {**event["session"], "instructions": bridge.binding.instructions}}
                await asyncio.wait_for(endpoint.send(event), timeout=2.0)
            for reason in effects.diagnostics:
                # Constant reason codes only (VOC-25 unknown events, VOC-38 function calls).
                logger.warning("voice_bridge_%s", reason)

        async def client() -> None:
            while True:
                packet = await socket.receive()
                if packet["type"] == "websocket.disconnect":
                    return
                async with serial:
                    if not self._live(active):
                        return
                    if packet.get("bytes") is not None:
                        entry = self.core.sessions[bridge.binding.session_id]
                        typed = (
                            entry.task is not None
                            and not entry.task.done()
                            and entry.session_id not in self.core.voice_engines
                        )
                        effects = Effects() if typed else bridge.microphone(packet["bytes"])
                    else:
                        text = packet.get("text", "")
                        if len(text) > 16_384:
                            raise VoiceProtocolError("control_limit")
                        try:
                            control = json.loads(text)
                        except ValueError:
                            raise VoiceProtocolError("invalid_control") from None
                        if not isinstance(control, dict):
                            raise VoiceProtocolError("invalid_control")
                        if (
                            control.get("type") == "speak"
                            and set(control) == {"type", "utterance_id"}
                            and self.activities is not None
                        ):
                            item = self.activities.for_binding(bridge.binding, str(control["utterance_id"]))
                            effects = bridge.queue_read_aloud(
                                item.value["utterance_id"], item.value["turn_id"], item.value["text"]
                            )
                        elif control.get("type") == "place":
                            effects = self._place(active, control)
                        elif (
                            control.get("type") == "dictate"
                            and set(control) == {"type", "capture_id", "pressed"}
                            and control["pressed"] is True
                            and self.activities is not None
                        ):
                            entry = self.core.entry(bridge.binding.session_id)
                            if entry.active_turn is not None or (entry.task is not None and not entry.task.done()):
                                raise VoiceProtocolError("voice_busy")
                            self.activities.begin_dictation(bridge.binding, str(control["capture_id"]))
                            effects = bridge.start_dictation(str(control["capture_id"]), self.core.clock_ms())
                        else:
                            effects = bridge.control(control)
                            if control.get("type") == "mode" and not bridge.calm:
                                self.calm_sessions.pop(bridge.binding.session_id, None)
                    await apply(effects)

        async def dispatch(control: dict[str, Any]) -> None:
            async with serial:
                if self._live(active):
                    await apply(bridge.control(control))

        async def apply_locked(effects: Effects) -> None:
            async with serial:
                if self._live(active):
                    await apply(effects)

        async def fall_back() -> bool:
            """VOC-29: the online endpoint is gone mid-session. Interrupt at the played position, switch to the local
            sidecar (same binding, same tutor session) and tell the student; False when there is nothing to switch
            to, which ends voice as before."""
            nonlocal endpoint
            fallback = getattr(self.route, "fallback", None)
            if active.path != "cloud_voice" or fallback is None:
                return False
            async with serial:
                if not self._live(active):
                    return False
                await apply(bridge.endpoint_lost())
                lost, endpoint = endpoint, None
                if lost is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(lost.close(), timeout=2.0)
                try:
                    endpoint = await asyncio.wait_for(fallback(), timeout=self.connect_timeout_s)
                except Exception:
                    endpoint = lost  # closed already; teardown closes it again harmlessly
                    return False
                active.path = "local_sidecar"
                logger.warning("voice_fallback_to_local")  # a constant code: never a frame or a transcript
                self.core._announce(bridge.binding.session_id, {
                    "type": "notice", "notice_id": new_id("ntc"), "level": "info", "code": "voice_now_local",
                    "message_key": "voice.now_local", "params": {}, "action": None})  # fmt: skip
                return True

        async def upstream() -> None:
            assert endpoint is not None
            while True:
                try:
                    frame = await endpoint.receive()
                except ConnectionError:  # the transport's VoiceTransportError: closed, errored or heartbeats missed
                    if await fall_back():
                        continue
                    raise
                async with serial:
                    if not self._live(active):
                        return
                    if not isinstance(frame, dict):
                        raise VoiceProtocolError("invalid_realtime_frame")
                    await apply(bridge.server(frame, self.core.clock_ms()))

        async def watch() -> None:
            while True:
                await asyncio.sleep(0.1)
                async with serial:
                    if not self._live(active):
                        return
                    entry = self.core.sessions[bridge.binding.session_id]
                    if bridge.dictation_id is not None and entry.task is not None and not entry.task.done():
                        await apply(bridge.control({"type": "stop"}))
                    await apply(bridge.tick(self.core.clock_ms()))

        try:
            opening = self.route(bridge.binding) if self.route is not None else self.connect()
            endpoint = await asyncio.wait_for(opening, timeout=self.connect_timeout_s)
            active.path = str(getattr(endpoint, "path", "local_sidecar"))
            # First-use model loading completes before UI capture can begin or microphone frames can accumulate.
            if not self._live(active):
                raise VoiceProtocolError("voice_binding_ended")
            await socket.accept(subprotocol="jana.voice.1")
            active.dispatch, active.effects = dispatch, apply_locked
            tasks = [asyncio.create_task(work()) for work in (client, upstream, watch)]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except WebSocketDisconnect:
            pass
        except (VoiceProtocolError, ApiError):
            if active.close_code == 1000:
                active.close_code = 4400
                unavailable_reason = "voice.protocol_error"
        except Exception as error:
            # Endpoint exceptions may contain URLs, credentials, or transcripts. Never log exception repr/traceback;
            # only a fixed launch code is matched, to give the student the right copy (VOC-14).
            active.close_code = 1011
            unavailable_reason = LAUNCH_REASONS.get(str(error), "voice.endpoint_unavailable")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if unavailable_reason is not None and self.on_failed is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.on_failed(bridge.binding), timeout=2.0)
            effects = bridge.close()
            # Even a disconnected browser must discard/cancel its provisional work. A failed flush must not
            # prevent that cleanup, and teardown never calls the release path.
            for action in effects.actions:
                with contextlib.suppress(Exception):
                    await act(action)
            effects.actions.clear()
            if self.on_closed is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.on_closed(bridge.binding), timeout=2.0)
            if self.activities is not None:
                self.activities.binding_closed(bridge.binding)
            self.bindings.revoke(bridge.binding)
            if endpoint is not None:
                with contextlib.suppress(Exception):
                    await apply(effects, closing=True)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(endpoint.close(), timeout=2.0)
            if unavailable_reason is not None:
                self.core._announce(
                    bridge.binding.session_id,
                    {
                        "type": "voice.state",
                        "state": "unavailable",
                        "reason_key": unavailable_reason,
                        "turn_id": None,
                        "listening_variant": None,
                    },
                )
            with contextlib.suppress(WebSocketDisconnect, RuntimeError, OSError):
                await socket.close(code=active.close_code)


def add_routes(app: FastAPI, runtime: VoiceRuntime) -> None:
    async def voice(socket: WebSocket, session_id: str) -> None:
        await runtime.serve(socket, session_id)

    app.add_api_websocket_route("/v1/sessions/{session_id}/voice", voice)
