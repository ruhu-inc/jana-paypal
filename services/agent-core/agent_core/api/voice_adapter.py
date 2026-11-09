"""OpenAI-compatible speech-sidecar LLM slot, backed exclusively by the existing tutor engine (VOC-30 to VOC-40)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from agent_core.api import projection
from agent_core.api.ask import answer_question, route_of
from agent_core.api.core import CURRENT_PROFILE, CoreService, SessionEntry
from agent_core.api.errors import ApiError
from agent_core.api.security import LaunchSecret
from agent_core.api.voice import RealtimeConnection, VoiceRuntime
from agent_core.contracts.voice.engine_llm_adapter import Request as AdapterRequest
from agent_core.engine.events import new_id
from agent_core.engine.safety import calm_screen_notice, redact_blocks
from agent_core.engine.study import StudyError
from agent_core.engine.turn import TurnIds, TurnResult
from agent_core.engine.voice_bindings import BindingRejected, VoiceBinding
from agent_core.engine.voice_bridge import VoiceProtocolError
from agent_core.engine.voice_place import FAILED as CHAT_FAILED
from agent_core.engine.voice_place import NOT_ANSWERED, Place, aid_spoken, ask_spoken
from agent_core.engine.voice_turn import ProvisionalVoiceTurn, VoiceTurnConflict

logger = logging.getLogger(__name__)
LOST = "Voice lost track of this lesson. Turn voice off and on again."
# VOC-12: engine-owned keyed fillers, with no content or numbers, spoken at most once per turn while the gates run.
FILLERS = ("Let me work that out.",)
FAILED = "Sorry, something went wrong. Please say that again."
GRADED = "That looks like graded work. Ask the tutor to guide you through it instead."


@dataclass(repr=False)
class _Job:
    binding: VoiceBinding
    entry: SessionEntry
    turn: ProvisionalVoiceTurn
    response_id: str | None = None
    expiry: asyncio.Task[None] | None = None
    solved_before: bool = False  # only a turn that solves the problem ends it (MEM-06), as for typed turns
    # A question in an installed subject spoken in Ask anything (VOC-53): announced as `voice.answer` on release.
    asked: str | None = None


@dataclass(repr=False)
class _Assembly:
    """A tutor turn whose fork's engine is still being assembled, off the event loop (VOC-12): until it is a job, a
    newer utterance, a cancel or the binding's end supersede it as they would the job."""

    binding: VoiceBinding
    turn_id: str
    text: str
    task: asyncio.Task[ProvisionalVoiceTurn]


@dataclass(repr=False)
class _Chat:
    """A question spoken in an open chat that the tutor does not answer (VOC-53): Ask anything or a study chat."""

    binding: VoiceBinding
    entry: SessionEntry
    place: Place
    question: str
    turn_id: str = field(default_factory=lambda: new_id("trn"))
    route: str = "ask"
    reply: dict[str, Any] | None = None
    aid_id: str | None = None
    spoken: str = ""
    released: bool = False
    expiry: asyncio.Task[None] | None = None
    evaluation: asyncio.Task[None] | None = None


def _chunk(identity: str, text: str, *, done: bool = False) -> str:
    frame = {
        "id": f"chatcmpl-{identity}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "jana-tutor-voice",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop" if done else None}],
    }
    return "data: " + json.dumps(frame, separators=(",", ":")) + "\n\n"


async def _fixed(text: str, identity: str = "unbound") -> AsyncIterator[str]:
    yield _chunk(identity, text, done=True)
    yield "data: [DONE]\n\n"


class VoiceTutorAdapter:
    def __init__(self, core: CoreService, runtime: VoiceRuntime, *, secret: str | None = None,
                 keep_alive_s: float = 5.0, unreleased_s: float = 10.0) -> None:  # fmt: skip
        self.core, self.runtime = core, runtime
        self.secret = LaunchSecret(secret or secrets.token_urlsafe(32))
        self.keep_alive_s, self.unreleased_s = keep_alive_s, unreleased_s
        # Where a question spoken in Ask anything goes (UXS-67): `tutor`, `study` or `ask` (VOC-53).
        self.route: Callable[[str], str] = lambda text: str(route_of(core, text)["route"])
        self.chat_timeout_s = 180.0  # proposal: the longest a spoken study question waits for its chat job (VOC-53)
        self.jobs: dict[str, _Job] = {}
        self.assembling: dict[str, _Assembly] = {}
        self.chats: dict[str, _Chat] = {}
        self.carried: dict[str, str] = {}
        self._lock = asyncio.Lock()
        runtime.adapter = self
        runtime.on_action = self.action
        runtime.on_closed = self.binding_closed
        runtime.on_failed = self.transport_failed
        core.voice_cancel = self.cancel
        core.before_profile_forgotten.append(self._close_profile)
        core.closers.append(self.close)

    def _finish(
        self,
        job: _Job,
        *,
        interrupted: bool,
        played_ms: int = 0,
        total_ms: int = 0,
        ended_by: str = "flush",
        heard_chars: int | None = None,
    ) -> None:
        job.turn.finish(
            interrupted=interrupted, played_ms=played_ms, total_ms=total_ms, ended_by=ended_by, heard_chars=heard_chars
        )
        if self.core.records is not None and job.turn.delivery is not None:
            self.core.records.voice_delivery(job.turn.ids.turn_id, job.turn.delivery, self.core.clock_ms())
        # VOC-05: a turn that awaits the student's worked answer gives their pauses the thinking extension.
        accepted = job.turn.result.tutor_turn if job.turn.result is not None else None
        self.runtime.await_work(job.binding, bool(accepted and accepted.get("expects") == "student_answer"))
        # The typed turn's post-turn steps (VOC-32): the solved record, the Reflector's idle clock and the end of a
        # problem (MEM-06).
        solved = bool(job.turn.engine.state.solved)
        if self.core.records is not None and solved:
            self.core.records.solved(job.entry.session_id)
        if self.core.reflector is not None:
            self.core.reflector.activity()
            if solved and not job.solved_before:
                self.core._reflect("problem_end", job.entry)

    def _persist_delivery(self, job: _Job) -> None:
        # Shutdown invalidates capabilities first, but this exact entry's database is still open for final delivery.
        if self.core.sessions.get(job.entry.session_id) is job.entry:
            self.core.persist(job.entry)

    async def transport_failed(self, binding: VoiceBinding) -> None:
        assembly = self.assembling.pop(binding.binding_id, None)
        if assembly is not None:  # nothing ran yet: the words alone become the typed turn
            assembly.task.cancel()
            text = assembly.text
            if self.core.sessions.get(binding.session_id) is None or not self.runtime.bindings.contains(binding):
                return
        else:
            job = self.jobs.get(binding.binding_id)
            if job is None or job.turn.released or not self._valid(job):
                return
            self._preserve_safety(job)
            if job.turn.safety_preserved:
                await self._discard(job, carry=False)
                return
            pending = job.turn.engine.steering.end_of_turn()
            text = "\n".join([job.turn.text, *[message.text for message in pending]])
            await self._discard(job, carry=False)
        context = CURRENT_PROFILE.set(binding.profile_id)
        try:
            accepted = self.core.start_turn(
                binding.session_id,
                {"client_message_id": new_id("cli"), "intent": "answer", "content": [{"type": "text", "text": text}]},
            )
            if self.core.records is not None:
                self.core.records.voice_turns.add(accepted["turn_id"])
        finally:
            CURRENT_PROFILE.reset(context)

    def _close_profile(self, profile_id: str | None) -> None:
        # Lock/switch is synchronous: reconcile while the learner DB is still open, before checkpoint/unload.
        for job in list(self.jobs.values()):
            if job.binding.profile_id != profile_id:
                continue
            if job.turn.released and not job.turn.finished:
                active = self.runtime._active
                response = active.bridge._response if active is not None else None
                current = response is not None and response.turn_id == job.turn.ids.turn_id
                played = response.played_turn_ms() if response is not None and current else 0
                heard = response.heard_chars(response.played_ms) if response is not None and current else None
                self._finish(job, interrupted=True, played_ms=played, heard_chars=heard)
            # An unreleased turn's flag reaches this profile's learner database before it closes (VOC-20, PRV-14).
            self._preserve_safety(job)
            job.turn.engine.cancel()
        self.runtime.bindings.revoke_profile(profile_id)

    def _valid(self, job: _Job) -> bool:
        entry = self.core.sessions.get(job.binding.session_id)
        return (
            self.runtime.bindings.contains(job.binding)
            and entry is job.entry
            and entry.profile_id == job.binding.profile_id
            and not entry.meta.get("archived")
            and not self.core.shutting_down
            and not (self.core.mock_active is not None and self.core.mock_active())
        )

    def _forget(self, job: _Job) -> None:
        if job.expiry is not None and job.expiry is not asyncio.current_task():
            job.expiry.cancel()
        if self.jobs.get(job.binding.binding_id) is job:
            del self.jobs[job.binding.binding_id]
        if self.core.voice_engines.get(job.entry.session_id) is job.turn.engine:
            del self.core.voice_engines[job.entry.session_id]
        if self.core.reflector is not None and not job.turn.released:
            self.core.reflector.activity()  # a discarded turn also ended activity: the idle delay counts from now
        if job.entry.active_turn == job.turn.ids.turn_id:
            job.entry.active_turn = None
        if job.turn.released and self._valid(job):
            pending = job.turn.engine.steering.end_of_turn()
            if pending:
                asyncio.get_running_loop().call_soon(
                    self.core._start_carried, job.entry, [message.text for message in pending]
                )

    def _owned(self, job: _Job) -> bool:
        return self.core.sessions.get(job.entry.session_id) is job.entry

    def _preserve_safety(self, job: _Job) -> None:
        # Never write a late cancelled fork into the next profile's database. Profile shutdown invokes this
        # synchronously, while the exact session and its database still belong to the closing profile.
        if self._owned(job) and job.turn.salvage_safety():
            self.core.persist(job.entry)

    async def _discard_turn(self, job: _Job) -> None:
        """Cancel the fork; a flag it raised while being cancelled is still salvaged, under the same ownership check."""
        owned = self._owned(job)
        await job.turn.discard(preserve_safety=owned)
        if owned and job.turn.safety_preserved and self._owned(job):
            self.core.persist(job.entry)
        if job.turn.distress:
            self.runtime.hold_calm(job.binding)

    async def _discard(self, job: _Job, *, carry: bool = True) -> None:
        if not job.turn.released:
            self._preserve_safety(job)
            if carry and not (job.turn.safety_preserved or job.turn.distress):
                job.turn.carried_forward = True
                pending = job.turn.engine.steering.end_of_turn()
                self.carried[job.binding.binding_id] = "\n".join([job.turn.text, *[m.text for m in pending]])
            if job.turn.safety_preserved:
                self.carried.pop(job.binding.binding_id, None)  # distress is never replayed as another turn
            await self._discard_turn(job)
        elif not job.turn.finished:
            self._finish(job, interrupted=True)
            self._persist_delivery(job)
        self._forget(job)

    async def cancel(self, turn_id: str) -> bool:
        assembly = next((item for item in self.assembling.values() if item.turn_id == turn_id), None)
        if assembly is not None:
            await self.runtime.stop(assembly.binding)  # the filler may be playing already
            if self.assembling.get(assembly.binding.binding_id) is assembly:
                del self.assembling[assembly.binding.binding_id]
                assembly.task.cancel()
                self.carried[assembly.binding.binding_id] = assembly.text
            return True
        job = next((job for job in self.jobs.values() if job.turn.ids.turn_id == turn_id), None)
        chat = next((chat for chat in self.chats.values() if chat.turn_id == turn_id), None)
        if chat is not None:
            await self.runtime.stop(chat.binding)
            await self._drop_chat(chat)
            return True
        if job is None:
            return False
        active = self.runtime._active
        if active is not None and active.bridge.binding is job.binding:
            # API cancel shares exactly the playback flush and heard-text path of the voice Stop key.
            await self.runtime.stop(job.binding)
        if self.jobs.get(job.binding.binding_id) is job:
            await self._discard(job)
        return True

    async def action(self, action: dict[str, Any]) -> None:
        chat = self.chats.get(action["binding_id"])
        if chat is not None:
            await self._chat_action(chat, action)
            return
        job = self.jobs.get(action["binding_id"])
        if job is None:
            if action["type"] == "release":
                raise VoiceTurnConflict("voice_turn_not_current")
            return
        if action["type"] == "release":
            if not self._valid(job) or action["turn_id"] != job.turn.ids.turn_id:
                raise VoiceTurnConflict("voice_binding_ended")
            stream = self.core._engine(job.entry).events
            # Preflight before adopting so a mode/lock change cannot overwrite the live session.
            if job.turn.before != self._state(job.entry):
                raise VoiceTurnConflict("voice_state_changed")
            job.entry.engine, job.entry.engine_state = job.turn.engine, job.turn.engine.state
            assert job.turn.ids.client_message_id is not None
            # As a typed turn starts (VOC-32): the session is touched, the message projected, a first title set.
            if self.core.records is not None:
                self.core.records.touch(job.entry.meta, self.core.clock_ms())
            projection.student_message(
                job.entry.state, job.turn.ids.client_message_id, [{"type": "text", "text": job.turn.text}]
            )
            if not job.entry.meta.get("title"):
                stream.emit({"type": "session.updated", "title": job.turn.text.splitlines()[0][:80]})
            if self.core.records is not None:
                self.core.records.voice_turns.add(job.turn.ids.turn_id)
            job.turn.release(stream)
            self.core.persist(job.entry)
            if job.asked is not None:
                # VOC-53: the tutor answered a subject question spoken in Ask anything; Home shows the reply with
                # "Continue in the tutor", once its audio starts (VOC-34).
                self.core._announce(job.entry.session_id, {
                    "type": "voice.answer", "place": "ask", "turn_id": job.turn.ids.turn_id, "question": job.asked,
                    "route": "tutor", "reply": None, "aid_id": None,
                    "text": job.turn.spoken_text or ""})  # fmt: skip
        elif action["type"] == "discard":
            await self._discard(job)
        elif action["type"] == "playback_started":
            if action.get("turn_id") == job.turn.ids.turn_id and "playback_start_ms" not in job.turn.latency:
                job.turn.latency["playback_start_ms"] = job.turn._offset()
        elif action["type"] in ("cancel", "complete"):
            if isinstance(action.get("turn_latency"), dict):
                job.turn.latency.update(action["turn_latency"])  # the sidecar's phase timings (VOC-22)
            if job.turn.released:
                if not job.turn.finished:
                    self._finish(
                        job,
                        interrupted=action["type"] == "cancel",
                        played_ms=int(action.get("played_ms", 0)),
                        total_ms=int(action.get("total_ms", 0)),
                        ended_by=action.get("ended_by", "flush"),
                        heard_chars=action["heard_chars"] if isinstance(action.get("heard_chars"), int) else None,
                    )
                    self._persist_delivery(job)
            else:
                self._preserve_safety(job)
                await self._discard_turn(job)
            self._forget(job)

    @staticmethod
    def _state(entry: SessionEntry) -> dict[str, Any]:
        from agent_core.engine.state import state_to_json

        return state_to_json(entry.engine_state)

    async def close(self) -> None:
        for assembly in self.assembling.values():
            assembly.task.cancel()
        self.assembling.clear()
        for job in list(self.jobs.values()):
            await self._discard(job, carry=False)
        for chat in list(self.chats.values()):
            await self._drop_chat(chat)
        self.carried.clear()

    async def binding_closed(self, binding: VoiceBinding) -> None:
        assembly = self.assembling.pop(binding.binding_id, None)
        if assembly is not None:
            assembly.task.cancel()
        job = self.jobs.get(binding.binding_id)
        if job is not None:
            await self._discard(job, carry=False)
        chat = self.chats.get(binding.binding_id)
        if chat is not None:
            await self._drop_chat(chat)
        self.carried.pop(binding.binding_id, None)

    async def _expire(self, job: _Job) -> None:
        await asyncio.sleep(self.unreleased_s)
        if self.jobs.get(job.binding.binding_id) is job and not job.turn.released:
            await self._discard(job)

    # ---- open-chat answers (VOC-53, D130) -------------------------------------------------------------------------

    def _chat_valid(self, chat: _Chat) -> bool:
        entry = self.core.sessions.get(chat.binding.session_id)
        return (
            self.runtime.bindings.contains(chat.binding)
            and self.chats.get(chat.binding.binding_id) is chat
            and entry is chat.entry
            and entry.profile_id == chat.binding.profile_id
            and not self.core.shutting_down
            and not (self.core.mock_active is not None and self.core.mock_active())
        )

    def _refused(self, chat: _Chat, error: ApiError) -> bool:
        """The typed route's input guard refused the spoken question (PRV-13): its template is spoken, the flag was
        recorded by the guard, and distress shows the calm screen and rests voice (PRV-14, VOC-20)."""
        details = getattr(error, "details", None) or {}
        if error.code != "validation_failed" or details.get("reason") != "safety_template":
            return False
        template = str(details.get("template_id") or "")
        templates = self.core._engine(chat.entry).deps.templates
        try:
            chat.spoken = templates.spoken(template)
        except KeyError:
            chat.spoken = CHAT_FAILED
        chat.reply = {"route": "ask", "state": "failed", "reason": "safety_template", "answer": None}
        if details.get("calm_screen"):
            self.core._announce(chat.binding.session_id, calm_screen_notice(new_id("ntc")))
            self.runtime.hold_calm(chat.binding)
        return True

    async def _answer(self, chat: _Chat) -> None:
        """The spoken question answered as its typed twin would be, under the same guard (VOC-53)."""
        from agent_core.api import screening

        if chat.place.kind == "ask":
            try:
                screening.screen(self.core, "POST /v1/ask", {}, {"question": chat.question})
            except ApiError as error:
                if self._refused(chat, error):
                    return
                raise
            reply = await answer_question(self.core, chat.question)
            chat.route = str(reply.get("route", "ask"))
            chat.reply = reply if chat.route == "ask" else None
            chat.spoken = ask_spoken(reply)
            return
        chat.route = "study"
        body = {"kind": "chat", "execution": "local", "prompt": chat.question,
                "sources": [{"source_id": chat.place.source_id, "revision": chat.place.revision}]}  # fmt: skip
        try:
            screening.screen(self.core, "POST /v1/study/jobs", {}, body)
        except ApiError as error:
            if self._refused(chat, error):
                chat.reply = None
                return
            raise
        study = self.core.study
        if not self.core.settings.study_workspace_enabled or study is None:
            chat.spoken = NOT_ANSWERED
            return
        try:
            assert chat.place.source_id is not None
            study.authorise(chat.place.source_id, chat.place.grant, chat.place.revision)
            desk = study.desk()
            job = desk.create_job(body)
        except StudyError as error:
            chat.spoken = GRADED if error.reason == "graded_work" else NOT_ANSWERED
            return
        study.poll()
        deadline = time.monotonic() + self.chat_timeout_s
        while job["state"] not in ("completed", "failed", "cancelled"):
            if time.monotonic() > deadline:
                with contextlib.suppress(StudyError):
                    desk.action(job["job_id"], {"action": "cancel"})
                chat.spoken = CHAT_FAILED
                return
            await asyncio.sleep(0.2)
            job = study.desk().job(job["job_id"])
        if job["state"] != "completed" or not job["result_ids"]:
            chat.spoken = NOT_ANSWERED
            return
        aid = study.desk().aid(job["result_ids"][0])
        chat.aid_id, chat.spoken = aid["aid_id"], aid_spoken(aid)

    def _filler_after_s(self, binding: VoiceBinding) -> float:
        """How long a turn runs before its filler (VOC-12): less on cloud voice, whose request comes after the
        recognizer's final transcript, so the filler plays at about the same time after the student stops."""
        settings = self.core.settings
        cloud = self.runtime.path_of(binding) == "cloud_voice"
        return (settings.voice_cloud_filler_after_ms if cloud else settings.voice_filler_after_ms) / 1000

    async def _chat_reply(self, chat: _Chat) -> AsyncIterator[str]:
        """The keyed filler while the answer is made, then only its accepted spoken form (VOC-12, VOC-33)."""
        evaluation = chat.evaluation
        assert evaluation is not None
        delivered = False
        try:
            filler_at = time.monotonic() + self._filler_after_s(chat.binding)
            filled = False
            while not evaluation.done():
                wait = self.keep_alive_s if filled else max(0.0, min(self.keep_alive_s, filler_at - time.monotonic()))
                done, _ = await asyncio.wait({evaluation}, timeout=wait)
                if done:
                    break
                if not filled and time.monotonic() >= filler_at:
                    filled = True
                    try:
                        self.runtime.authorise_filler(chat.binding, chat.turn_id, FILLERS[0])
                    except VoiceProtocolError:
                        pass
                    else:
                        yield _chunk(chat.turn_id, FILLERS[0] + " ")
                        continue
                yield ": keep-alive\n\n"
            try:
                evaluation.result()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("voice_chat_answer_failed")  # a constant code: never the question or the answer
                chat.reply = (
                    {"route": "ask", "state": "failed", "reason": "processing_failed", "answer": None}
                    if chat.place.kind == "ask"
                    else None
                )
                chat.spoken = CHAT_FAILED
            if not self._chat_valid(chat):
                raise VoiceTurnConflict("voice_turn_superseded")
            self.runtime.authorise_turn(chat.binding, chat.turn_id, chat.spoken)
            for sentence in re.split(r"(?<=[.!?])(?=\s)", chat.spoken):
                yield _chunk(chat.turn_id, sentence)
            yield _chunk(chat.turn_id, "", done=True)
            yield "data: [DONE]\n\n"
            delivered = True
            chat.expiry = asyncio.create_task(self._chat_expire(chat))
        except asyncio.CancelledError:
            raise
        except Exception:
            async for line in _fixed(""):
                yield line
        finally:
            if not delivered and self.chats.get(chat.binding.binding_id) is chat:
                await self._drop_chat(chat)

    async def _chat_expire(self, chat: _Chat) -> None:
        await asyncio.sleep(self.unreleased_s)
        if self.chats.get(chat.binding.binding_id) is chat and not chat.released:
            await self._drop_chat(chat)

    async def _drop_chat(self, chat: _Chat) -> None:
        if self.chats.get(chat.binding.binding_id) is chat:
            del self.chats[chat.binding.binding_id]
        for task in (chat.evaluation, chat.expiry):
            if task is not None and task is not asyncio.current_task() and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _chat_action(self, chat: _Chat, action: dict[str, Any]) -> None:
        if action["type"] == "release":
            if not self._chat_valid(chat) or action["turn_id"] != chat.turn_id:
                raise VoiceTurnConflict("voice_binding_ended")
            chat.released = True
            # The answer shows once its audio starts, never before (VOC-34): Ask anything's view or the Chat tab.
            self.core._announce(chat.binding.session_id, {
                "type": "voice.answer", "place": chat.place.kind, "turn_id": chat.turn_id,
                "question": chat.question[:4000], "route": chat.route, "reply": chat.reply, "aid_id": chat.aid_id,
                "text": chat.spoken})  # fmt: skip
            if self.core.reflector is not None:
                self.core.reflector.activity()
        elif action["type"] in ("discard", "cancel", "complete"):
            await self._drop_chat(chat)

    async def stream(self, request: AdapterRequest) -> AsyncIterator[str]:
        arrived = time.monotonic()
        activities = self.runtime.activities
        lines = [
            line
            for message in request.messages
            if message.role == "system"
            for line in message.content.splitlines()
            if "jana-voice-binding:" in line
        ]
        match = re.fullmatch(r"jana-voice-binding: (vb_[0-9A-HJKMNP-TV-Z]{26})", lines[0]) if len(lines) == 1 else None
        capture_id = activities.dictation_request(match[1]) if activities is not None and match else None
        if capture_id is not None:
            async for line in _fixed("", capture_id):
                yield line
            return
        unbound = any(
            message.role == "system" and message.content == "jana-voice-unbound" for message in request.messages
        ) and not any(
            message.role == "system" and "jana-voice-binding:" in message.content for message in request.messages
        )
        if unbound:
            async for line in _fixed("Ready."):
                yield line
            return
        try:
            utterance = self.runtime.bindings.resolve(request)
        except BindingRejected:
            # VOC-31: logged with a constant code only; the request's text never reaches a log.
            logger.warning("voice_binding_rejected")
            async for line in _fixed(LOST):
                yield line
            return
        binding = utterance.binding
        context = CURRENT_PROFILE.set(binding.profile_id)
        if utterance.text.startswith("jana-speak:"):
            try:
                identity = re.fullmatch(r"jana-speak: (utt_[0-9A-HJKMNP-TV-Z]{26})", utterance.text)
                active = self.runtime._active
                if (
                    activities is None
                    or identity is None
                    or active is None
                    or active.bridge._read is None
                    or active.bridge._read[0] != identity[1]
                ):
                    raise VoiceTurnConflict("invalid_speak_directive")
                item = activities.for_binding(binding, identity[1], consume=True)
                async for line in _fixed(item.value["text"], identity[1]):
                    yield line
            except Exception:
                async for line in _fixed(""):
                    yield line
            finally:
                CURRENT_PROFILE.reset(context)
            return
        job: _Job | None = None
        chat: _Chat | None = None
        assembly: _Assembly | None = None
        evaluation: asyncio.Task[TurnResult] | None = None
        delivered = False
        # VOC-12: the filler's time counts from the request, not from when the turn's engine is ready.
        filler_at = arrived + self._filler_after_s(binding)
        filled = False

        async def waiting(work: asyncio.Future[Any], turn_id: str) -> AsyncIterator[str]:
            """Keep-alives while `work` runs, and the turn's one keyed filler once its time has come (VOC-12)."""
            nonlocal filled
            while not work.done():
                wait = self.keep_alive_s if filled else max(0.0, min(self.keep_alive_s, filler_at - time.monotonic()))
                done, _ = await asyncio.wait({work}, timeout=wait)
                if done:
                    break
                if not filled and time.monotonic() >= filler_at:
                    filled = True
                    # VOC-12: no accepted spoken form yet. The one honest filler is the only speech allowed before
                    # every gate passes (VOC-33); it never carries model text and never releases the turn.
                    try:
                        self.runtime.authorise_filler(binding, turn_id, FILLERS[0])
                    except VoiceProtocolError:
                        pass
                    else:
                        yield _chunk(turn_id, FILLERS[0] + " ")
                        continue
                yield ": keep-alive\n\n"

        try:
            async with self._lock:
                entry = self.core.entry(binding.session_id)
                if (
                    not self.runtime.bindings.contains(binding)
                    or entry.meta.get("archived")
                    or (self.core.mock_active is not None and self.core.mock_active())
                ):
                    raise VoiceTurnConflict("voice_binding_ended")
                old = self.jobs.get(binding.binding_id)
                if old is not None:
                    await self._discard(old)
                old_chat = self.chats.get(binding.binding_id)
                if old_chat is not None:
                    await self._drop_chat(old_chat)
                superseded = self.assembling.pop(binding.binding_id, None)
                if superseded is not None:
                    superseded.task.cancel()
                    self.carried[binding.binding_id] = superseded.text  # merged into this utterance, as for a job
                replaced = old is not None or old_chat is not None or superseded is not None
                # Redact before the words go anywhere: a typed turn's steering queue included (PRV-03, VOC-20).
                blocks = redact_blocks([{"type": "text", "text": utterance.text}])
                text = str(blocks[0]["text"]).strip()
                if entry.task is not None and not entry.task.done():
                    self.core.steer(
                        entry.active_turn or "",
                        {"client_message_id": new_id("cli"), "content": [{"type": "text", "text": text}]},
                    )
                    async for line in _fixed("", entry.active_turn or "unbound"):
                        yield line
                    return
                previous = self.carried.pop(binding.binding_id, "")

                def normal(value: str) -> str:
                    return " ".join(re.findall(r"\w+", value.casefold()))

                if previous and normal(previous) not in normal(text):
                    text = previous + "\n" + text
                if not text or len(text) > 4000:
                    raise VoiceTurnConflict("voice_utterance_limit")
                place = self.runtime.place_of(binding)
                # VOC-53: in an open chat a question is answered there, except one in an installed subject spoken in
                # Ask anything, which the tutor answers (PED-01; the owner's choice, D130). On the tutor screen every
                # spoken turn is a tutor turn (the owner's choice for this entry, 2026-10-27, SPEC §6).
                to_tutor = (place is None or place.kind == "tutor"
                            or (place.kind == "ask" and self.route(text) == "tutor"))  # fmt: skip
                if not to_tutor:
                    assert place is not None
                    chat = _Chat(binding, entry, place, text)
                    if not self.runtime.begin_turn(binding, chat.turn_id, replace=replaced):
                        async for line in _fixed(""):
                            yield line
                        return
                    self.runtime.await_work(binding, False)
                    self.chats[binding.binding_id] = chat
                    chat.evaluation = asyncio.create_task(self._answer(chat))
                else:
                    live = self.core._engine(entry)
                    if self.core.reflector is not None:
                        self.core.reflector.pause()
                    ids = TurnIds(client_message_id=new_id("cli"))
                    if not self.runtime.begin_turn(binding, ids.turn_id, replace=replaced):
                        active = self.runtime._active
                        if active is not None and active.bridge.thinking:
                            self.carried[binding.binding_id] = text
                        async for line in _fixed(""):
                            yield line
                        return
                    self.runtime.await_work(binding, False)  # the next turn has started: back to the normal silence
                    # Assembling the fork's engine takes most of a second: off the event loop and outside the lock, so
                    # the filler still plays on time (VOC-12) and a newer utterance can supersede it meanwhile.
                    assembly = _Assembly(binding, ids.turn_id, text, asyncio.create_task(
                        ProvisionalVoiceTurn.assemble(live, self.core.engine_factory, text, ids)))  # fmt: skip
                    self.assembling[binding.binding_id] = assembly
            if chat is not None:
                async for line in self._chat_reply(chat):
                    yield line
                return
            assert assembly is not None
            async for line in waiting(assembly.task, assembly.turn_id):
                yield line
            async with self._lock:
                if self.assembling.get(binding.binding_id) is not assembly:
                    # Superseded while assembled: a newer utterance carried these words, or voice ended.
                    async for line in _fixed(""):
                        yield line
                    return
                del self.assembling[binding.binding_id]
                provisional = assembly.task.result()
                # What the event loop did meanwhile, as if it had come just after the request.
                if (
                    not self.runtime.bindings.contains(binding)
                    or self.core.sessions.get(binding.session_id) is not entry
                    or entry.profile_id != binding.profile_id
                    or entry.meta.get("archived")
                    or (self.core.mock_active is not None and self.core.mock_active())
                ):
                    raise VoiceTurnConflict("voice_binding_ended")
                if entry.task is not None and not entry.task.done():
                    self.core.steer(
                        entry.active_turn or "",
                        {"client_message_id": new_id("cli"), "content": [{"type": "text", "text": text}]},
                    )
                    async for line in _fixed("", entry.active_turn or "unbound"):
                        yield line
                    return
                if not self.runtime.turn_pending(binding, assembly.turn_id):
                    self.carried[binding.binding_id] = text  # interrupted before evaluation (VOC-05): said again later
                    async for line in _fixed(""):
                        yield line
                    return
                provisional.path = self.runtime.path_of(binding)
                if self.runtime._active is not None:
                    provisional.talk_mode = self.runtime._active.bridge.mode
                job = _Job(binding, entry, provisional, solved_before=bool(entry.engine_state.solved))
                self.jobs[binding.binding_id] = job
                self.core.voice_engines[entry.session_id] = provisional.engine
                entry.active_turn = provisional.ids.turn_id
                evaluation = asyncio.create_task(provisional.evaluate())
                if place is not None and place.kind == "ask":
                    job.asked = text
            async for line in waiting(evaluation, provisional.ids.turn_id):
                yield line
            evaluation.result()
            self._preserve_safety(job)
            if not self._valid(job) or self.jobs.get(binding.binding_id) is not job:
                raise VoiceTurnConflict("voice_turn_superseded")
            # The pinned upstream emits response.created only once output is available. Authorize the accepted
            # turn first; the bridge binds it when that event arrives, without guessing a response id or speaking early.
            job.response_id = self.runtime.current_response(binding)
            self.runtime.authorise_turn(binding, provisional.ids.turn_id, provisional.spoken_text, provisional.cues)
            if provisional.distress:
                self.runtime.hold_calm(binding)  # the keyed template is spoken, then voice rests idle (VOC-20)
            for sentence in re.split(r"(?<=[.!?])(?=\s)", provisional.spoken_text):
                yield _chunk(provisional.ids.turn_id, sentence)
            yield _chunk(provisional.ids.turn_id, "", done=True)
            yield "data: [DONE]\n\n"
            delivered = True
            job.expiry = asyncio.create_task(self._expire(job))
        except asyncio.CancelledError:
            raise
        except Exception:
            if job is not None and self._valid(job) and self.jobs.get(binding.binding_id) is job:
                await self._discard(job, carry=False)
                fallback = ProvisionalVoiceTurn(self.core._engine(job.entry), self.core.engine_factory, job.turn.text)
                fallback.path = job.turn.path
                fallback.prepare_error()
                job = _Job(binding, job.entry, fallback)
                self.jobs[binding.binding_id] = job
                self.core.voice_engines[job.entry.session_id] = fallback.engine
                job.entry.active_turn = fallback.ids.turn_id
                if self.runtime.begin_turn(binding, fallback.ids.turn_id):
                    job.response_id = self.runtime.current_response(binding)
                    self.runtime.authorise_turn(binding, fallback.ids.turn_id, fallback.spoken_text)
                    async for line in _fixed(FAILED, fallback.ids.turn_id):
                        yield line
                    delivered = True
                    job.expiry = asyncio.create_task(self._expire(job))
                else:
                    async for line in _fixed(FAILED):
                        yield line
            else:
                async for line in _fixed(FAILED):
                    yield line
        finally:
            if assembly is not None and self.assembling.get(binding.binding_id) is assembly:
                del self.assembling[binding.binding_id]  # the stream was closed while the engine was assembled
                assembly.task.cancel()
            if job is not None and not delivered and self.jobs.get(binding.binding_id) is job:
                if evaluation is not None and not evaluation.done():
                    evaluation.cancel()
                    await asyncio.gather(evaluation, return_exceptions=True)
                await self._discard(job)
            CURRENT_PROFILE.reset(context)


def attach(
    core: CoreService,
    connect: Callable[[], Awaitable[RealtimeConnection]],
    *,
    secret: str | None = None,
    strict: bool = False,
    connect_timeout_s: float = 10.0,
    available: bool = True,
    tutoring: Callable[[], bool] | None = None,
    dictation: Callable[[], bool] | None = None,
) -> VoiceRuntime:
    """`available`: a speech pack is selected (read-aloud through the sidecar); `dictation`: the pack hears speech
    (not a read-aloud-only profile, TABLE VOC-03a); `tutoring`: voice turns are offered too (TABLE VOC-03a class and
    the VOC-03 memory budget). All can change while the app runs."""

    async def pending(_: dict[str, Any]) -> None:
        raise VoiceTurnConflict("voice_adapter_starting")

    runtime = VoiceRuntime(core, connect, pending, strict=strict, connect_timeout_s=connect_timeout_s)
    if tutoring is not None:
        runtime.tutoring = tutoring
    if dictation is not None:
        runtime.dictation = dictation
    VoiceTutorAdapter(core, runtime, secret=secret)
    core.voice_available = available
    core.voice_tutoring = runtime.tutoring
    core.voice_dictation = runtime.dictation
    return runtime


def follow_read_aloud(core: CoreService, want_warm: Callable[[bool], None]) -> None:
    """VOC-23, D96: the voice sidecar starts early, in the background, only while the open profile has read-aloud on.
    `want_warm` hears that preference when a profile is opened and whenever it changes, and False when the profile
    closes (lock, switch, idle lock or deletion)."""
    core.on_preferences.append(lambda _profile, preferences: want_warm(preferences.get("read_aloud") is True))
    core.on_profile_forgotten.append(lambda _profile: want_warm(False))


async def reply(adapter: VoiceTutorAdapter, body: Any) -> tuple[int, dict[str, Any] | AsyncIterator[str]]:
    """One chat-completions request to the adapter (VOC-30), from the sidecar over loopback or forwarded by the
    online voice endpoint (VOC-28): an error body, the warm-up's single completion, or the streamed SSE lines."""
    if isinstance(body, dict) and "tools" in body:
        return 400, {"error": {"code": "tools_not_supported"}}
    try:
        parsed = AdapterRequest.model_validate(body, strict=True)
    except (ValueError, ValidationError):
        return 400, {"error": {"code": "invalid_request"}}
    if parsed.stream is not True:
        return 200, {
            "id": "chatcmpl-unbound",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "jana-tutor-voice",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Ready."}, "finish_reason": "stop"}],
        }
    return 200, adapter.stream(parsed)


def add_routes(app: FastAPI, adapter: VoiceTutorAdapter) -> None:
    async def completions(request: Request) -> JSONResponse | StreamingResponse:
        raw = await request.body()
        if len(raw) > 512_000:
            return JSONResponse({"error": {"code": "invalid_request"}}, status_code=400)
        try:
            body = json.loads(raw)
        except ValueError:
            return JSONResponse({"error": {"code": "invalid_request"}}, status_code=400)
        status, answer = await reply(adapter, body)
        if isinstance(answer, dict):
            return JSONResponse(answer, status_code=status)
        return StreamingResponse(
            answer,
            media_type="text/event-stream",
            headers={"cache-control": "no-store", "x-accel-buffering": "no"},
        )

    app.add_api_route("/v1/voice/llm/chat/completions", completions, methods=["POST"], response_model=None)
