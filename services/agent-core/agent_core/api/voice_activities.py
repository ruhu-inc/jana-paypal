"""Owned read-aloud capabilities and volatile dictation drafts (VOC-18/39/46). No tutor/model calls."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CURRENT_PROFILE, CoreService
from agent_core.api.errors import ApiError
from agent_core.api.profiles import Mutating
from agent_core.contracts import errors
from agent_core.contracts.input.captured_question import CapturedQuestion
from agent_core.engine.events import new_id
from agent_core.engine.safety import redact_personal
from agent_core.engine.verbaliser import verbalise
from agent_core.engine.voice_bindings import VoiceBinding
from agent_core.engine.voice_dictation import spoken_maths
from agent_core.store.voice_captures import promote

if TYPE_CHECKING:
    from agent_core.api.voice import VoiceRuntime


@dataclass(repr=False)
class Utterance:
    value: dict[str, Any]
    session_id: str
    profile_id: str | None
    binding_id: str | None
    used: bool = False
    ended: bool = False
    position: int = 0
    elapsed_ms: int = 0


class VoiceActivities:
    def __init__(self, core: CoreService, runtime: VoiceRuntime | None) -> None:
        self.core, self.runtime = core, runtime
        self.utterances: dict[str, Utterance] = {}
        self.note_drafts: set[str] = set()
        self.drafts: dict[str, tuple[VoiceBinding, dict[str, Any]]] = {}
        # A completed dictation binding remains an empty-only adapter tombstone for late speculative requests.
        self.dictation_bindings: dict[str, tuple[str, int]] = {}
        core.before_profile_forgotten.append(self.forget)
        core.closers.append(self.close)
        if runtime is not None:
            runtime.activities = self

    async def close(self) -> None:
        self.utterances.clear()
        self.drafts.clear()
        self.note_drafts.clear()
        self.dictation_bindings.clear()

    def forget(self, profile_id: str | None) -> None:
        self.utterances = {key: value for key, value in self.utterances.items() if value.profile_id != profile_id}
        removed = {binding.binding_id for binding, _ in self.drafts.values() if binding.profile_id == profile_id}
        self.drafts = {key: value for key, value in self.drafts.items() if value[0].profile_id != profile_id}
        self.dictation_bindings = {key: value for key, value in self.dictation_bindings.items() if key not in removed}
        self.note_drafts.intersection_update(self.drafts)

    def prune(self) -> None:
        now = self.core.clock_ms()
        self.utterances = {key: value for key, value in self.utterances.items() if value.value["expires_at"] > now}
        self.dictation_bindings = {key: value for key, value in self.dictation_bindings.items() if value[1] > now}
        self.drafts = {key: value for key, value in self.drafts.items() if value[1]["updated_at"] > now - 600_000}
        self.note_drafts.intersection_update(self.drafts)

    def binding(self, session_id: str) -> VoiceBinding:
        self.core.entry(session_id)
        runtime = self.runtime
        active = runtime._active if runtime else None
        if (
            runtime is None
            or active is None
            or active.bridge.binding.session_id != session_id
            or not runtime._live(active)
        ):
            raise ApiError("feature_disabled", "turn voice on before dictating")
        return active.bridge.binding

    def microphone_allowed(self, session_id: str) -> None:
        self.core._guard()
        self.core.assessment_guard()
        entry = self.core.entry(session_id)
        if entry.engine_state.mock_running or (self.core.mock_active is not None and self.core.mock_active()):
            raise ApiError("tutor_locked", "dictation is unavailable during a mock sitting")
        # D87 / VOC-16: local microphone use has only the OS permission prompt.

    def mint(self, session_id: str, part_id: str) -> dict[str, Any]:
        self.core._guard()
        self.core.assessment_guard()
        entry = self.core.entry(session_id)
        part = entry.state["parts"].get(part_id)
        message = next(
            (m for m in entry.state["messages"] if m["role"] == "tutor" and part_id in m.get("part_ids", [])), None
        )
        if (
            part is None
            or part.get("kind") != "text"
            or part.get("status") != "complete"
            or message is None
            or message.get("status") == "streaming"
        ):
            raise ApiError("request_conflict", "only a completed tutor message can be read aloud")
        text = verbalise(part.get("text", "")).text
        if not text or len(text) > 4000:
            raise ApiError("validation_failed", "choose a shorter message to read aloud")
        return self.mint_text(session_id, text, message["turn_id"], part_id)

    def mint_text(self, session_id: str, text: str, turn_id: str | None, part_id: str | None) -> dict[str, Any]:
        entry = self.core.entry(session_id)
        self.prune()
        if len(self.utterances) >= 64:
            raise ApiError("rate_limited", "finish reading before starting another message")
        active = self.runtime._active if self.runtime else None
        binding = active.bridge.binding if active and active.bridge.binding.session_id == session_id else None
        now = self.core.clock_ms()
        value: dict[str, Any] = {
            "type": "utterance",
            "utterance_id": new_id("utt"),
            "turn_id": turn_id,
            "part_id": part_id,
            "text": text,
            "transport": "sidecar" if binding else "os",
            "expires_at": now + 60_000,
        }
        self.utterances[value["utterance_id"]] = Utterance(
            value, session_id, entry.profile_id, binding.binding_id if binding else None
        )
        return value

    def owned(self, session_id: str, identity: str) -> Utterance:
        entry = self.core.entry(session_id)
        self.prune()
        item = self.utterances.get(identity)
        if (
            item is None
            or item.session_id != session_id
            or item.profile_id != entry.profile_id
            or (
                item.value["turn_id"] is not None
                and item.value["turn_id"]
                not in {m.get("turn_id") for m in entry.state["messages"] if m["role"] == "tutor"}
            )
        ):
            raise ApiError("not_found")
        return item

    def for_binding(self, binding: VoiceBinding, identity: str, *, consume: bool = False) -> Utterance:
        item = self.owned(binding.session_id, identity)
        if item.binding_id != binding.binding_id or item.ended or item.used:
            raise ApiError("request_expired", "start read-aloud again")
        if consume:
            item.used = True
            item.value["expires_at"] = self.core.clock_ms() + 600_000
        return item

    def playback(self, session_id: str, identity: str, body: dict[str, Any]) -> None:
        item = self.owned(session_id, identity)
        if item.binding_id is not None or item.ended:
            raise ApiError("request_conflict")
        phase, position, elapsed = body["phase"], body["char_index"], body["elapsed_ms"]
        if position > len(item.value["text"]) or position < item.position or elapsed < item.elapsed_ms:
            raise ApiError("validation_failed", "invalid read-aloud position")
        if phase == "start":
            if item.used or position or elapsed:
                raise ApiError("request_expired")
            item.used = True
            item.value["expires_at"] = self.core.clock_ms() + 600_000
        elif not item.used:
            raise ApiError("request_conflict")
        item.position, item.elapsed_ms = position, elapsed
        item.ended = phase in ("end", "stop", "error")
        self.core._announce(
            session_id,
            {
                "type": "voice.state",
                "state": "idle" if item.ended else "speaking",
                "reason_key": None,
                "turn_id": item.value["turn_id"],
                "listening_variant": None,
            },
        )
        words = [
            {"w": m[0][:64], "start_ms": elapsed}
            for m in re.finditer(r"\S+", item.value["text"])
            if m.start() == position
        ][:1]
        self.core._announce(
            session_id,
            {
                "type": "voice.caption",
                "speaker": "tutor",
                "item_id": identity,
                "turn_id": item.value["turn_id"],
                "committed_text": item.value["text"],
                "tentative_text": "",
                "words": words,
                "cues": [],
                "final": item.ended,
            },
        )

    def create_dictation(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if self.runtime is not None and not self.runtime.dictation():
            raise ApiError("feature_disabled", "this speech pack reads aloud only; type instead (VOC-46)")
        self.microphone_allowed(session_id)
        binding = self.binding(session_id)
        entry = self.core.entry(session_id)
        if (
            entry.active_turn is not None
            or session_id in self.core.voice_engines
            or (entry.task is not None and not entry.task.done())
        ):
            raise ApiError("turn_active", "Wait for the tutor to finish, or press Stop.")
        if (
            self.runtime
            and self.runtime._active
            and (
                self.runtime._active.bridge.dictation_id is not None
                or self.runtime._active.bridge._response is not None
            )
        ):
            raise ApiError("turn_active", "Wait for the tutor to finish, or press Stop.")
        self.prune()
        for old_binding, record in self.drafts.values():
            if old_binding is binding and record["client_capture_id"] == body["client_capture_id"]:
                return record
        if len(self.drafts) >= 32 or len(self.dictation_bindings) >= 128:
            raise ApiError("rate_limited", "finish or discard a dictated draft first")
        now = self.core.clock_ms()
        record = {
            "schema_version": 1,
            "capture_id": new_id("cap"),
            "client_capture_id": body["client_capture_id"],
            "session_id": session_id,
            "source": "voice",
            "purpose": body["purpose"],
            "locale": "en",
            "status": "drafting",
            "text": "",
            "regions": [],
            "questions": [],
            "attachments": [],
            "reader": {
                "path": "speech_to_text",
                "model_id": None,
                "hardware_class": entry.engine_state.hardware_class,
                "duration_ms": 0,
            },
            "quality": {"checks": [], "overridden": False},
            "confirmation": {"confirmed": False, "method": "pending", "edit_distance": 0, "confirmed_at": None},
            "injection": {"flagged": False, "spans_removed": 0},
            "refusal": None,
            "egress": {"left_device": False, "destination": "none"},
            "created_at": now,
            "updated_at": now,
        }
        CapturedQuestion.model_validate(record)
        if body.get("destination") == "note":
            self.note_drafts.add(record["capture_id"])
        self.drafts[record["capture_id"]] = binding, record
        return record

    def draft(self, session_id: str, identity: str) -> dict[str, Any]:
        self.prune()
        if self.core.entry(session_id).meta.get("archived"):
            raise ApiError("not_found")
        value = self.drafts.get(identity)
        if value is None or value[0].session_id != session_id or value[0].profile_id != CURRENT_PROFILE.get():
            raise ApiError("not_found")
        return value[1]

    def begin_dictation(self, binding: VoiceBinding, identity: str) -> None:
        self.microphone_allowed(binding.session_id)
        value = self.drafts.get(identity)
        if value is None or value[0] is not binding or value[1]["status"] != "drafting":
            raise ApiError("not_found")
        self.dictation_bindings[binding.binding_id] = identity, self.core.clock_ms() + 600_000

    def dictation_request(self, binding_id: str) -> str | None:
        self.prune()
        value = self.dictation_bindings.get(binding_id)
        return value[0] if value else None

    def transcript(self, binding: VoiceBinding, identity: str, text: str) -> None:
        value = self.drafts.get(identity)
        if value is None or value[0] is not binding or value[1]["status"] != "drafting":
            return
        result = spoken_maths(text)
        record = value[1]
        record.update(text=result.text, status="awaiting_confirmation", updated_at=self.core.clock_ms())
        record["reader"]["duration_ms"] = max(0, self.core.clock_ms() - record["created_at"])
        if result.uncertain:
            record["regions"] = [
                {
                    "region_id": "rgn_0",
                    "kind": "question_stem",
                    "order": 0,
                    "text": result.text,
                    "confidence": 0.0,
                    "needs_confirmation": True,
                    "low_confidence_spans": [{"start": 0, "end": len(result.text)}],
                    "language": "en",
                    "direction": "ltr",
                }
            ]
        CapturedQuestion.model_validate(record)

    def finish_dictation(self, identity: str, cancelled: bool) -> None:
        value = self.drafts.get(identity)
        if value is not None and cancelled and value[1]["status"] != "confirmed":
            value[1].update(status="cancelled", text="", regions=[], updated_at=self.core.clock_ms())

    def confirm(self, session_id: str, identity: str, text: str) -> dict[str, Any]:
        self.core._guard()
        self.core.assessment_guard()
        draft = self.draft(session_id, identity)
        if draft["status"] != "awaiting_confirmation":
            raise ApiError("capture_not_ready")
        text = redact_personal(text.strip())[0]
        if not text:
            raise ApiError("validation_failed", "review the draft before sending")
        record = copy.deepcopy(draft)
        distance = _edit_distance(draft["text"], text)
        now = self.core.clock_ms()
        record.update(text=text, status="confirmed", updated_at=now)
        record["confirmation"] = {
            "confirmed": True,
            "method": "edited" if distance else "sent",
            "edit_distance": distance,
            "confirmed_at": now,
        }
        for region in record["regions"]:
            region["needs_confirmation"] = False
        CapturedQuestion.model_validate(record)
        # Only explicit confirmation promotes the draft; Send resolves its text through M21's capture_id path.
        repository = getattr(self.core, "capture_desk", None)
        if repository is not None and identity not in self.note_drafts:
            promote(repository.conn(), record, now + self.core.settings.retention_attachment_days * 86_400_000)
        self.drafts[identity] = self.drafts[identity][0], record
        return record

    def binding_closed(self, binding: VoiceBinding) -> None:
        self.utterances = {
            key: value for key, value in self.utterances.items() if value.binding_id != binding.binding_id
        }
        for owner, record in self.drafts.values():
            if owner is binding and record["status"] == "drafting":
                self.finish_dictation(record["capture_id"], True)


def add_routes(app: FastAPI, core: CoreService, runtime: VoiceRuntime | None, mutating: Mutating) -> VoiceActivities:
    desk = VoiceActivities(core, runtime)
    from agent_core.api.study_notes import StudyNotes

    notes = StudyNotes(core, desk)
    notes.add_routes(app, mutating)

    def validate(body: Any, kind: str) -> dict[str, Any]:
        if not isinstance(body, dict) or body.get("type") != kind or errors("voice/activities", body):
            raise ApiError("validation_failed")
        return body

    def scoped(session_id: str) -> None:
        core._guard()
        if core.entry(session_id).meta.get("archived"):
            raise ApiError("not_found")

    @app.post("/v1/sessions/{session_id}/voice/utterances")
    async def mint(session_id: str, request: Request) -> Response:
        scoped(session_id)  # ownership precedes idempotency lookup as well as fresh execution

        async def run(body: Any) -> tuple[int, Any]:
            if isinstance(body, dict) and body.get("type") == "read_content":
                source = validate(body, "read_content")["source"]
                text = notes.read_source(source, session_id)
                rendered = verbalise(text).text
                if not rendered or len(rendered) > 4000:
                    raise ApiError("validation_failed", "choose a shorter passage to read aloud")
                part = core.entry(session_id).state["parts"].get(source.get("part_id"), {})
                return 200, desk.mint_text(session_id, rendered, part.get("turn_id"), source.get("part_id"))
            return 200, desk.mint(session_id, validate(body, "read_aloud")["part_id"])

        return await mutating(request, run)

    @app.post("/v1/sessions/{session_id}/voice/utterances/{identity}/playback")
    async def playback(session_id: str, identity: str, request: Request) -> Response:
        scoped(session_id)

        async def run(body: Any) -> tuple[int, Any]:
            desk.playback(session_id, identity, validate(body, "read_aloud_playback"))
            return 204, None

        return await mutating(request, run)

    @app.post("/v1/sessions/{session_id}/voice/dictations")
    async def dictate(session_id: str, request: Request) -> Response:
        scoped(session_id)

        async def run(body: Any) -> tuple[int, Any]:
            return 200, desk.create_dictation(session_id, validate(body, "create_dictation"))

        return await mutating(request, run)

    @app.get("/v1/sessions/{session_id}/voice/dictations/{identity}")
    async def draft(session_id: str, identity: str) -> dict[str, Any]:
        return desk.draft(session_id, identity)

    @app.post("/v1/sessions/{session_id}/voice/dictations/{identity}/confirm")
    async def confirm(session_id: str, identity: str, request: Request) -> Response:
        scoped(session_id)

        async def run(body: Any) -> tuple[int, Any]:
            return 200, desk.confirm(session_id, identity, validate(body, "confirm_dictation")["text"])

        return await mutating(request, run)

    @app.delete("/v1/sessions/{session_id}/voice/dictations/{identity}")
    async def discard(session_id: str, identity: str, request: Request) -> Response:
        scoped(session_id)

        async def run(_: Any) -> tuple[int, Any]:
            desk.draft(session_id, identity)
            if runtime and runtime._active and runtime._active.bridge.dictation_id == identity:
                await runtime.stop(runtime._active.bridge.binding)
            desk.drafts.pop(identity, None)
            return 204, None

        return await mutating(request, run)

    return desk


def _edit_distance(left: str, right: str) -> int:
    """Exact bounded Levenshtein distance using bit vectors, without quadratic Python loops."""
    if not left:
        return len(right)
    masks: dict[str, int] = {}
    for index, character in enumerate(left):
        masks[character] = masks.get(character, 0) | (1 << index)
    mask, highest = (1 << len(left)) - 1, 1 << (len(left) - 1)
    positive, negative, distance = mask, 0, len(left)
    for character in right:
        equal = masks.get(character, 0)
        vertical = equal | negative
        horizontal = (((equal & positive) + positive) ^ positive) | equal
        plus, minus = negative | ~(horizontal | positive), positive & horizontal
        distance += int(bool(plus & highest)) - int(bool(minus & highest))
        plus, minus = (plus << 1) | 1, minus << 1
        positive, negative = (minus | ~(vertical | plus)) & mask, (plus & vertical) & mask
    return distance
