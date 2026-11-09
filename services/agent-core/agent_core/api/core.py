"""The service behind the local API: sessions, turns, cancel, idempotency, snapshots, streams and persistence (API-14,
API-16, API-17, API-23, API-24, API-31; ARC-23 shutdown).

Everything runs on one event loop. The engine's event sink is synchronous, so folding an event into the projection,
storing it in the replay ring and queueing it for each connection happen together: a snapshot's cursor always matches
its state, and a stream registered between two events misses nothing and repeats nothing."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import contextvars
import datetime
import hashlib
import json
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agent_core.api import projection
from agent_core.api.app_channel import AppChannel
from agent_core.api.errors import ApiError, invalid
from agent_core.api.exposure import student_view
from agent_core.api.stream import PROTOCOL, Connection, RingPool, control, frame, parse_cursor
from agent_core.config import Settings
from agent_core.contracts import errors as contract_errors
from agent_core.engine import offers
from agent_core.engine.companion import CompanionContext, CompanionDesk
from agent_core.engine.events import new_id
from agent_core.engine.input import normalise_blocks, normalise_input
from agent_core.engine.lessons import LessonDesk
from agent_core.engine.offers import answered as offer_answered
from agent_core.engine.pedagogy import asks_why
from agent_core.engine.records import SessionRecords
from agent_core.engine.reflection import ReflectorQueue
from agent_core.engine.safety import redact_blocks, redact_personal
from agent_core.engine.session_notes import SessionNotesQueue
from agent_core.engine.state import SessionState, state_from_json, state_to_json
from agent_core.engine.steering import Steer
from agent_core.engine.study import StudyQueue
from agent_core.engine.study_session import StudyVisits
from agent_core.engine.titles import chat_title
from agent_core.engine.turn import Engine, TurnIds
from agent_core.engine.tutor_flows import MODES, decide_mode_change
from agent_core.engine.tutoring_usage import TutoringUsage, asks_usage
from agent_core.store.checkpoints import CheckpointStore
from agent_core.store.profile_stores import Erasers
from agent_core.store.sessions import SessionStore

SERVER_VERSION = "0.0.0"
COMMANDS = "api/commands#/$defs/"
TERMINAL_STEP = frozenset({"final", "correct", "incorrect", "equivalent_unsimplified", "unchecked"})

EngineFactory = Callable[[SessionState], Engine]
# The profile the current request acts for (API-04, DAT-14): set by the API from `X-Profile-Token` once profiles
# exist; None is the one implicit profile of a device without profiles (S1). Every session belongs to one profile, and
# another profile's session answers 404, indistinguishable from a missing one. Each request runs in its own task, so
# the value never leaks between requests.
CURRENT_PROFILE: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_profile", default=None)


def _companion_schema() -> tuple[str, dict[str, Any], Any]:
    """The companion role's output (contracts/agents/companion-turn), for the opener's wording call (§19.5.2)."""
    from jsonschema import Draft202012Validator

    from agent_core.contracts import document

    schema = {k: v for k, v in document("agents/companion-turn").items() if k not in ("$schema", "$id")}
    return "CompanionTurn", schema, Draft202012Validator(schema)


def _checkin_schema() -> dict[str, Any]:
    """The check-in extractor's output (contracts/agents/commitment, SPEC §19.5.5)."""
    from agent_core.contracts import document

    return {k: v for k, v in document("agents/commitment").items() if k not in ("$schema", "$id")}


def new_epoch() -> str:
    """16 lowercase hex characters, as the control contract's `control.ready.epoch` states (API-05)."""
    return secrets.token_hex(8)


def body_errors(definition: str, body: Any) -> list[str]:
    return contract_errors(COMMANDS + definition, body)


def require(definition: str, body: Any) -> dict[str, Any]:
    refused = invalid(COMMANDS + definition, body)  # value-free: no message ever quotes the body
    if refused is not None:
        raise refused
    assert isinstance(body, dict)
    return body


@dataclass
class _Cached:
    digest: str
    status: int
    body: Any
    expires: float


class TokenBucket:
    """A token bucket (API-33): `rate` tokens a second up to `burst`; `take` answers how long to wait, or 0."""

    def __init__(self, rate: float, burst: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.rate, self.burst, self.clock = rate, burst, clock
        self.tokens, self.at = burst, clock()

    def take(self) -> float:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate)
        self.at = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) / self.rate


class IdempotencyCache:
    """Responses of mutating commands by `Idempotency-Key` (API-31): the same key and body returns the stored response;
    the same key with a different body is 409 `request_conflict`."""

    def __init__(self, hours: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = hours * 3600
        self.clock = clock
        self.entries: dict[str, _Cached] = {}

    @staticmethod
    def digest(method: str, path: str, body: Any) -> str:
        return hashlib.sha256(json.dumps([method, path, body], sort_keys=True).encode()).hexdigest()

    def lookup(self, key: str, digest: str) -> tuple[int, Any] | None:
        now = self.clock()
        for stale in [k for k, v in self.entries.items() if v.expires <= now]:
            del self.entries[stale]
        cached = self.entries.get(key)
        if cached is None:
            return None
        if cached.digest != digest:
            raise ApiError("request_conflict", "this idempotency key was used with a different request")
        return cached.status, cached.body

    def store(self, key: str, digest: str, status: int, body: Any) -> None:
        self.entries[key] = _Cached(digest, status, body, self.clock() + self.ttl)


@dataclass
class SessionEntry:
    session_id: str
    meta: dict[str, Any]
    state: dict[str, Any]  # the projection (snapshot) state
    engine_state: SessionState
    engine: Engine | None = None
    connections: set[Connection] = field(default_factory=set)
    task: asyncio.Task[Any] | None = None
    active_turn: str | None = None
    profile_id: str | None = None


class CoreService:
    def __init__(
        self,
        settings: Settings,
        engine_factory: EngineFactory,
        store: SessionStore | CheckpointStore,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        epoch: str | None = None,
        records: SessionRecords | None = None,
    ) -> None:
        self.settings = settings
        self._records: SessionRecords | None = None
        self.reflector: ReflectorQueue | None = None
        # The router the research agent's conversation calls through (SPEC §18.1a), once the app attaches it: the
        # Study model handle exists only with a local model, and the conversation is online only.
        self.research_router: Callable[[], Any] | None = (
            None  # the device's Reflector jobs (AGT-26), once the app attaches it
        )
        self.records = records
        self.engine_factory = engine_factory
        self.voice_engines: dict[str, Engine] = {}  # provisional/released voice work, owned by the adapter
        self.voice_cancel: Callable[[str], Awaitable[bool]] | None = None
        self.voice_close: Callable[[], Awaitable[None]] | None = None
        self.voice_available = False
        self.voice_tutoring: Callable[[], bool] = lambda: False  # voice turns offered too (VOC-03, set by the adapter)
        self.voice_dictation: Callable[[], bool] = lambda: False  # the pack hears speech (VOC-46, set by the adapter)
        self.store = store
        self.clock_ms = clock_ms
        self.epoch = epoch or new_epoch()
        self.pool = RingPool(
            max_bytes=int(settings.api_ring_process_mb * 1024 * 1024),
            max_sessions=settings.api_ring_sessions,
            ring_events=settings.api_ring_events,
            ring_bytes=int(settings.api_ring_mb * 1024 * 1024),
            event_bytes=int(settings.api_ring_event_kb * 1024),
        )
        self.idempotency = IdempotencyCache(settings.api_idempotency_hours)
        self.command_bucket = TokenBucket(settings.api_command_burst_per_s, settings.api_command_burst_per_s)
        self.turn_buckets: dict[str | None, TokenBucket] = {}  # per profile (API-33)
        self.sessions: dict[str, SessionEntry] = {}
        self.readiness = "core_ready"
        self.components: dict[str, str] = {"core": "ok"}
        self.shutting_down = False
        self.recovery_required = False
        self.closers: list[Callable[[], Awaitable[None]]] = []
        self.capture_desk: Any = None
        self.directory: Any = None  # the profile directory (store.profiles), once the device database is open
        self.on_profile_closing: list[Callable[[str], None]] = []
        self.on_profile_forgotten: list[Callable[[str], None]] = []  # caches to clear on lock or switch (DAT-14)
        self.before_profile_forgotten: list[Callable[[str], None]] = []  # finalize delivery before closing its DB
        # Every per-profile store's eraser, walked in the registry's order on profile deletion (DAT-09).
        self.erasers = Erasers()
        self.erasers.register("core", lambda profile_id: self.forget_profile(profile_id, drop=True))
        self.erasers.register("model_caches", lambda _profile_id: self.clear_caches())
        self.activator: Callable[[str | None], SessionRecords] | None = None  # opens a profile's learner DB (DAT-14)
        self.records_profile: str | None = None  # whose learner database the records write to
        self.store_factory: Callable[[], Any] | None = None  # the open learner database's checkpoint store (DAT-07)
        self.study_draining: str | None = None  # an idled-out profile whose learner DB stays open for its study jobs
        self.storage_low: Callable[[], bool] | None = None  # free disk under the DAT-13 floor
        self.cache_clearers: list[Callable[[], Awaitable[Any]]] = []  # the model runtime's KV caches (DAT-14)
        self.recoveries: Callable[[], list[Any]] | None = None  # learner databases recovered at open (DAT-13)
        self.jobs: dict[str, dict[str, Any]] = {}  # export and import jobs (API-29 GET /v1/jobs)
        self.capture_summary: Callable[[], list[dict[str, Any]]] | None = None
        # One checker call outside any turn, as the Checker (VSL-42 manipulations); None without a worker.
        self.check_tool: Callable[[str, dict[str, Any]], Awaitable[tuple[Any, str | None]]] | None = None
        self.capture_trace_sink: Callable[[dict[str, Any]], None] | None = None
        self.job_trace_sink: Callable[[dict[str, Any]], None] | None = None  # job traces written by a route
        self.trace_records: Callable[[], list[dict[str, Any]]] | None = None  # the local trace store's records
        self.rebuild_projections: Callable[[Any, str], None] | None = None  # rebuild a learner DB's projections
        self.retention: Callable[[], None] | None = None  # the DAT-08 job: at start and every interval
        self.on_profile_created: list[Callable[[str], Any]] = []
        # The open profile's effective preferences, when it is opened and after each change (the voice sidecar's
        # early start with read-aloud on, VOC-23, D96).
        self.on_preferences: list[Callable[[str, dict[str, Any]], None]] = []
        self.notices: dict[str, list[str]] = {}  # shipped notice versions per consent scope (PRV-12)
        self.packs: Callable[[], list[dict[str, Any]]] | None = None  # installed packs, verified (API-29, CUR-08)
        self.network_state = "off"  # without the connectivity layer (tests of earlier milestones): always off
        # The connectivity layer (`net.layer.OnlineLayer`, ARC-06), attached by the app; reached duck-typed (ARC-18).
        self.online: Any = None
        # Ask anything (STU-20): the installed packs' search index for routing, and the gateway transport.
        self.knowledge: Any = None
        self.ask_cloud: Any = None
        # App-channel events (API-15 `connectivity.status`, non-session `egress.notice`): the latest status and the
        # recent notices, for the app channel of API-22 (M20.b) to stream.
        self.app_events: collections.deque[dict[str, Any]] = collections.deque(maxlen=64)
        # The app channel (API-22) and, from M20.b, the packs, models and downloads behind API-29 (`api.packs`).
        self.app_channel = AppChannel(settings, self.epoch, clock_ms, SERVER_VERSION)
        self.pack_desk: Any = None
        self.update_desk: Any = None
        # CUR-36: called when content or a safety configuration arrives (a pack, a kit, the update desk, a catalogue
        # import), so a correction's re-mark runs promptly; the app sets it to the runtime's correction sweep.
        self.corrections_due: Callable[[], Any] | None = None
        self.start_benchmark: Callable[[], Any] | None = None
        self.app_tasks: list[asyncio.Task[Any]] = []
        self.release = False
        self.entitlement_trust: Any = None  # the trust store entitlements verify against (tests use the dev root)
        self.licence: Any = None  # the school licence file's state, fixed at start (PLT-19, PLT-20; api.licence)
        self.crashes: Any = None  # the crash store (OBS-16); its records wait for a decision (OBS-17, api.support)
        self.health_details: Callable[[], dict[str, str]] | None = None  # the health panel's conditions (OBS-15)
        self.models: Callable[[], dict[str, Any]] | None = None  # GET /v1/models (API-29 subset)
        # ARC-32: a mock sitting is in progress (mock exams arrive in M16); no background job runs while it is.
        self.mock_active: Callable[[], bool] | None = None
        self.assessments: Callable[[], Any] | None = None
        # The plan desk of the open profile (LRN-28 to LRN-33): Home's plan card is answered through `answer_card`.
        self.planner: Callable[[], Any] | None = None
        # PLT-29: below the minimum laptop with no school server and no online tier, no model call is made at all.
        self.tutor_unavailable: Callable[[], bool] | None = None
        # The class a new session follows (MDL-16): the device's local class, or `cloud` on the online path (PLT-29).
        self.session_class: Callable[[], str | None] | None = None
        # The knows page's desk for a profile (`engine.knows.KnowsDesk`, or None when there is no such profile; G90).
        self.knows: Callable[[str | None], contextlib.AbstractContextManager[Any]] | None = None
        # The subject map and lesson player's desk on the open learner database (`engine.learn.LearnDesk`, or None
        # when none is open; UXS-22, UXS-23).
        self.learn: Callable[[], contextlib.AbstractContextManager[Any]] | None = None
        self._retainer: asyncio.Task[Any] | None = None
        self._flusher: asyncio.Task[Any] | None = None
        self._idler: asyncio.Task[Any] | None = None
        self._assessment_clock: asyncio.Task[Any] | None = None
        self._idle_reflected: dict[str, int] = {}  # session -> the last activity its idle job was queued for
        self.session_notes = SessionNotesQueue(
            lambda: getattr(self.store, "conn", None) if self.records_profile is not None else None,
            lambda: self.reflector.model() if self.reflector is not None else None,
            lambda: (
                (source.capture if (source := self.reflector.source()) else None)
                if self.reflector is not None
                else None
            ),
            lambda: (
                self._background_held()
                or bool(self.reflector and self.reflector.running)
                or bool(getattr(self, "study", None) and self.study.task and not self.study.task.done())
            ),
            self.clock_ms,
        )
        self.study = StudyQueue(
            lambda: getattr(self.store, "conn", None) if self.records_profile is not None else None,
            lambda: self.reflector.model() if self.reflector is not None else None,
            lambda: (
                self._background_held()
                or bool(self.reflector and self.reflector.running)
                or bool(self.session_notes.task and not self.session_notes.task.done())
            ),
            self.clock_ms,
            self.settings,
        )
        # The Study screen's session of the day and the activity it records (SPEC §19.3.2, W8): on the open
        # profile's learner database only, with the installed packs' labels; its end is the study_end write point.
        self.study_visits = StudyVisits(
            lambda: self.records.learner if self.records is not None and self.records_profile is not None else None,
            self.clock_ms,
            self.settings,
            content=lambda: self.study.content() if self.study.content is not None else None,
            hardware_class=lambda: self.records.hardware_class if self.records is not None else "mid",
            on_end=self._study_ended,
        )
        self.study.activity = self.study_visits
        # Your tutor in Study (SPEC §19, W8 phase 2): the profile's companion thread and its daily episodes; the
        # opener's wording goes through the online router the research conversation uses.
        self.companion = CompanionDesk(
            lambda: self.records.learner if self.records is not None and self.records_profile is not None else None,
            self.clock_ms,
            self.settings,
            content=lambda: self.study.content() if self.study.content is not None else None,
            router=lambda: self.research_router() if self.research_router is not None else None,
            schema=_companion_schema,
        )
        self.before_profile_forgotten.append(self.companion.invalidate)
        # Check-ins (SPEC §19.5.5, W8 phase 3): the extractor after a companion turn, the heartbeat, the due query.
        from agent_core.engine.checkins import CheckinDesk

        self.checkins = CheckinDesk(
            lambda: self.records.learner if self.records is not None and self.records_profile is not None else None,
            self.clock_ms,
            self.settings,
            schema=_checkin_schema,
            study_cards_due=self.companion.study_cards_due,
        )
        for session_id, record in store.load_all().items():
            self._restore(session_id, record)
        if records is not None:
            records.recover(self.clock_ms())  # sessions a dead process left open end crash_recovered (LRN-01)

    def publish_app(self, event: dict[str, Any]) -> None:
        """An app-channel event (API-22): kept for the latest-state readers and streamed in sequence."""
        self.app_events.append(event)
        self.app_channel.publish(event)

    @property
    def records(self) -> SessionRecords | None:
        return self._records

    @records.setter
    def records(self, records: SessionRecords | None) -> None:
        """The open learner database's records; a session's end there is a Reflector write point (MEM-06)."""
        self._records = records
        if records is not None:
            records.ended = self._session_ended

    # --- lifecycle -------------------------------------------------------------------------------------------------

    def _restore(self, session_id: str, record: dict[str, Any]) -> None:
        if "assessment_runtime" in record:
            return
        state = record["projection"]
        projection.interrupt(state)
        state["cursor"] = {"epoch": self.epoch, "seq": 0}  # a new epoch: old cursors get 409 epoch_mismatch
        self.sessions[session_id] = SessionEntry(session_id, state["session"], state, state_from_json(record["engine"]),
                                                 profile_id=record.get("profile_id"))  # fmt: skip
        if self.records is not None:
            self.records.restored(session_id, self.clock_ms())

    def start(self) -> None:
        if self._assessment_clock is None:
            self._assessment_clock = asyncio.get_running_loop().create_task(self._assessment_loop())
            self.app_tasks.append(self._assessment_clock)
        if self._flusher is None:
            self._flusher = asyncio.get_running_loop().create_task(self._flush_loop())
        if self.retention is not None and self._retainer is None:
            self._retainer = asyncio.get_running_loop().create_task(self._retention_loop())
        if self.reflector is not None:
            self.attach_reflector(self.reflector)

    async def _assessment_loop(self) -> None:
        while not self.shutting_down:
            if self.settings.study_workspace_enabled:
                self.study.poll()
            if self.study_draining is not None and not self.study.outstanding():
                self._close_learner()  # the idled-out profile's study jobs are done: its learner database closes
            self.session_notes.poll()
            if self.assessments is not None:
                opened = self.assessments()
                if opened is not None:
                    opened.checkpoint()
            await asyncio.sleep(self.settings.mock_checkpoint_ms / 1000)

    # --- the Reflector's write points (MEM-06, AGT-26) ---------------------------------------------------------

    def attach_reflector(self, queue: ReflectorQueue) -> None:
        """Run the device's Reflector queue: it starts a job only while no turn runs, and the startup scan queues
        the open profile's ended sessions that were never reflected."""
        self.reflector = queue
        queue.busy = lambda: (
            self._background_held()
            or bool(self.session_notes.task and not self.session_notes.task.done())
            or bool(self.study.task and not self.study.task.done())
        )
        queue.announce = self._announce
        queue.clock_ms = self.clock_ms
        queue.start()
        queue.backlog(self.records_profile or "prof_local")
        if self._idler is None:
            self._idler = asyncio.get_running_loop().create_task(self._idle_loop())

    def _turn_running(self) -> bool:
        return bool(self.voice_engines) or any(e.task is not None and not e.task.done() for e in self.sessions.values())

    def _background_held(self) -> bool:
        """ARC-32: background work waits while a turn runs or a mock sitting is in progress."""
        return self.recovery_required or self._turn_running() or (self.mock_active is not None and self.mock_active())

    def mock_started(self) -> None:
        """ARC-32: a mock sitting starts (M16 calls this): a running background job stops at once and waits."""
        if self.reflector is not None:
            self.reflector.pause()
        self.session_notes.pause()
        self.study.pause()

    def _today(self) -> str:
        return datetime.datetime.fromtimestamp(self.clock_ms() / 1000, datetime.UTC).date().isoformat()

    def _reflect(self, point: str, entry: SessionEntry, request: str | None = None, turn_id: str | None = None) -> bool:
        """Queue one Reflector job for a session of the open profile (never another profile's, DAT-14)."""
        if self.reflector is None or entry.profile_id != self.records_profile:
            return False
        subject = entry.meta.get("subject")
        return self.reflector.trigger(point, entry.profile_id or "prof_local", entry.session_id,  # type: ignore[arg-type]
                                      session_date=self._today(), subject=subject, request=request,
                                      turn_id=turn_id)  # fmt: skip

    def _study_ended(self, session_id: str, subject: str | None, session_date: str) -> None:
        """The Study screen closed or went idle: its session's `study_end` Reflector job (SPEC §19.4, MEM-06)."""
        if self.reflector is not None and self.records_profile is not None and not self.shutting_down:
            self.reflector.trigger("study_end", self.records_profile, session_id, session_date=session_date,
                                   subject=subject)  # fmt: skip

    def _session_ended(self, session_id: str, reason: str) -> None:
        self.session_notes.scan()
        entry = self.sessions.get(session_id)
        if entry is not None and not self.shutting_down:
            self._reflect("session_end", entry)

    def _memory_request(self, entry: SessionEntry, words: str, turn_id: str | None = None) -> bool:
        """An explicit "remember that" (MEM-06): one job at once after the reply, on a paraphrase of the words; its
        outcome is told in the turn that asked."""
        return self._reflect("explicit_request", entry, words, turn_id)

    def _correct_memory(self, engine: Engine, turn: str, words: str, note_id: str, seen_version: str) -> bool:
        engine.companion.require_scope()
        entry = self.sessions.get(engine.state.session_id)
        if entry is None or self.reflector is None or entry.profile_id != self.records_profile:
            return False
        return self.reflector.trigger(
            "explicit_request",
            entry.profile_id or "prof_local",
            entry.session_id,
            session_date=self._today(),
            subject=entry.meta.get("subject"),
            request=words,
            turn_id=turn,
            correction=(note_id, seen_version),
        )

    def memory_changed(self, *, cancel_pending: bool = True) -> None:
        """Explicit preference/note edits become visible to every open session of this learner immediately."""
        if cancel_pending and self.reflector is not None and self.records_profile is not None:
            self.reflector.cancel_profile(self.records_profile)
        for entry in self.sessions.values():
            if entry.profile_id != self.records_profile or entry.engine is None:
                continue
            if entry.engine.memory is not None:
                entry.engine.memory.refresh()
            entry.engine.compactor.previous = None
            entry.engine.compactor.history.clear()
            if entry.engine.companion is not None:
                entry.engine.companion.status_key = None

    def _announce(self, session_id: str, event: dict[str, Any], turn_id: str | None = None) -> None:
        """Publish memory and voice events on their owning session (MEM-11, VOC-24). Profile checks apply when a
        profile activator is attached; standalone voice sessions need no learner database."""
        entry = self.sessions.get(session_id)
        if entry is not None and (self.activator is None or entry.profile_id == self.records_profile):
            if event.get("type") == "memory.saved":
                self.memory_changed(cancel_pending=False)
            self._engine(entry).events.emit(event, turn_id=turn_id)

    def memory_forgotten(self, note_id: str) -> None:
        """A note was forgotten: every open session of the profile drops it from its frozen snapshot, so the next
        turn's tier 6 lacks it, and the stream says so (MEM-11, MEM-14, UXS-28). The text of its `memory.saved`
        announcement leaves the session's frames and replay ring too (LRN-37), and a Reflector job running for the
        profile starts again from a view without the note."""
        if self.reflector is not None:
            self.reflector.cancel_profile(self.records_profile or "prof_local")
        for entry in self.mine():
            if entry.engine is not None:
                entry.engine.forget_note(note_id)
                for seq in entry.engine.events.scrub_saved(f"mem_{note_id}"):
                    self.pool.rewrite(entry.session_id, seq, frame(entry.engine.events.frame_at(seq)))
                entry.engine.events.emit({"type": "memory.forgotten", "note_id": f"mem_{note_id}"})

    async def _idle_loop(self) -> None:
        """The long-pause write point (MEM-06): a learning session idle for `memory_idle_write_min` gets one job per
        idle period (G93: separate from the 30-minute end of a learning session)."""
        idle_ms = int(self.settings.memory_idle_write_min * 60_000)
        while True:
            await asyncio.sleep(min(30.0, idle_ms / 4000))
            with contextlib.suppress(Exception):
                self.idle_check()

    def idle_check(self) -> list[str]:
        """Queue the idle job of every open learning session that has been quiet for `memory_idle_write_min`, once
        per quiet period. Returns the sessions queued."""
        idle_ms = int(self.settings.memory_idle_write_min * 60_000)
        records, now, queued = self.records, self.clock_ms(), []
        if (ended := self.study_visits.idle_check(now)) is not None:  # the Study screen, quiet for 20 minutes
            queued.append(ended)
        queued += self.end_stale_companion()  # the companion's episodes: idle 20 minutes, or of an earlier day
        if self.records is not None and self.records.learner is not None and self.records_profile is not None:
            with contextlib.suppress(Exception):  # check-ins' heartbeat: on app open, then every 30 minutes
                self.checkins.heartbeat(now)
        for session_id, state in list(records.open.items()) if records is not None else []:
            entry = self.sessions.get(session_id)
            quiet = now - state.last_activity >= idle_ms and not self._turn_running()
            if entry is not None and quiet and self._idle_reflected.get(session_id) != state.last_activity:
                self._idle_reflected[session_id] = state.last_activity
                if self._reflect("idle", entry):
                    queued.append(session_id)
        return queued

    async def _retention_loop(self) -> None:
        """DAT-08: at app start, then every configured interval; a failing pass is retried at the next one. Each pass
        waits until no turn runs and no mock sitting is in progress (ARC-32)."""
        while True:
            while self._background_held():
                await asyncio.sleep(self.settings.background_cancel_budget_s)
            with contextlib.suppress(Exception):
                assert self.retention is not None
                self.retention()
            await asyncio.sleep(self.settings.retention_job_interval_h * 3600)

    async def _flush_loop(self) -> None:
        """Coalesced deltas leave on the timer even when no other event follows (API-18)."""
        while True:
            await asyncio.sleep(self.settings.api_flush_ms / 1000)
            for entry in self.sessions.values():
                if entry.engine is not None:
                    entry.engine.events.flush()

    async def shutdown(self, grace_s: float | None = None) -> None:
        """ARC-23: stop taking commands, end running turns `interrupted`, persist, close streams and workers. The
        shell may shorten the grace for running turns (`control.shutdown.grace_ms`)."""
        self.shutting_down = True
        if self._assessment_clock is not None:
            self._assessment_clock.cancel()
            await asyncio.gather(self._assessment_clock, return_exceptions=True)
        if self.voice_close is not None:
            await self.voice_close()  # heard-text reconciliation precedes checkpoint/session/database closure
        if self.reflector is not None:
            await self.reflector.close()  # an unfinished reflection is done at the next start (AGT-26)
        await self.study.close()
        await self.session_notes.close()
        if self._idler is not None:
            self._idler.cancel()
        running = [e for e in self.sessions.values() if e.task is not None and not e.task.done()]
        for entry in running:
            assert entry.engine is not None
            entry.engine.cancel()
        if running:
            grace = self.settings.shutdown_grace_s if grace_s is None else min(grace_s, self.settings.shutdown_grace_s)
            await asyncio.wait([e.task for e in running if e.task], timeout=grace)
        self.study_visits.end("interrupted", notify=False)  # an open Study session ends with the app (§19.3.2)
        if self.records is not None:
            self.records.shutdown(self.clock_ms())  # open learning sessions end interrupted (LRN-01)
        for entry in self.sessions.values():
            if entry.engine is not None:
                entry.engine.events.flush()
            self.persist(entry)
            for connection in list(entry.connections):
                connection.put_frames([control({"type": "stream.closing", "reason": "shutdown"})])
                connection.close()
        for connection in list(self.app_channel.connections):  # the app channel closes the same way (API-22)
            connection.put_frames([control({"type": "stream.closing", "reason": "shutdown"})])
            connection.close()
        if self._flusher is not None:
            self._flusher.cancel()
        if self._retainer is not None:
            self._retainer.cancel()
        for close in self.closers:  # the worker pool and other resources the app owns (worker.shutdown)
            await close()

    def _guard(self, *, allow_recovery: bool = False) -> None:
        if self.shutting_down:
            raise ApiError("core_starting", "the core is shutting down")
        if self.recovery_required and not allow_recovery:
            raise ApiError("request_conflict", "the failed update must be recovered before changing learner data",
                           details={"reason": "update_recovery_required"})  # fmt: skip

    # --- engine and events -----------------------------------------------------------------------------------------

    def _engine(self, entry: SessionEntry) -> Engine:
        if entry.engine is None:
            engine = self.engine_factory(entry.engine_state)
            engine.profile_ref = entry.profile_id or engine.profile_ref  # traces carry the owning profile (DAT-09)
            events = engine.events
            events.epoch = self.epoch
            events.flush_bytes = int(self.settings.api_flush_kb * 1024)
            events.clock_ms = self.clock_ms
            events.seq = entry.state["cursor"]["seq"]
            events.sink = lambda envelope, entry=entry: self._on_event(entry, envelope)
            engine.learning = self._learning  # attempts, verdicts, hints and safety flags (LRN-01, G54)
            engine.on_memory_request = lambda turn, words, entry=entry: self._memory_request(entry, words, turn)
            if self.learn is not None:  # the lesson player's reads for a lesson session (AGT-22)
                engine.lesson_desk = LessonDesk(self.learn)
            if entry.meta.get("mode") == "study_companion":
                self._attach_companion(engine)
            entry.engine = engine
        return entry.engine

    def _share_materials(self) -> bool:
        """`study_materials` effective for the open profile (§19.6): a cloud companion call may then search the
        learner's own materials, as Ask anything's `share_own` decides."""
        layer = getattr(self, "online", None)
        conn = self.records.learner.conn if self.records is not None and self.records.learner is not None else None
        try:
            return bool(layer is not None and layer.study_effective(self.records_profile, conn))
        except Exception:
            return False

    def _attach_companion(self, engine: Engine) -> None:
        """A `study_companion` session's engine: its companion context and the study toolset, bound to the session's
        profile through the same `MemoryAccess` as the memory tools (SPEC §19.3.3, §19.4)."""
        from agent_core.api.ask import _index
        from agent_core.engine.memory_tools import StudyTools

        def make_job(body: dict[str, Any]) -> dict[str, Any]:
            job = dict(self.study.desk().create_job(body))
            self.study.poll()  # it runs once the turn is over (ARC-32: background work waits for the student)
            return job

        engine.companion = CompanionContext(self.companion, share_materials=self._share_materials,
                                            knowledge=lambda: _index(self), checkins=self.checkins,
                                            correct_memory=lambda turn, words, note, seen:
                                                self._correct_memory(engine, turn, words, note, seen),
                                            memory_changed=self.memory_changed,
                                            note_forgotten=self.memory_forgotten)  # fmt: skip
        if engine.memory is not None:
            tools = StudyTools(engine.memory, self.settings, engine._cloud, share=self._share_materials,
                               planner=lambda: self.planner() if self.planner is not None else None,
                               make_job=make_job, clock=self.clock_ms)  # fmt: skip
            tools.bind(engine.deps.executor.host_executors)
            engine.study_tools = tools
        engine.companion.guard_tools(engine.deps.executor.host_executors)

    async def open_companion(self, checkin: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST /v1/companion: today's episode of the thread, opened or resumed (SPEC §19.3.3, JM-D23); with a
        check-in (POST /v1/checkins/{id}/open, §19.3.4), the episode carries its id and opens with its question."""
        self._guard()
        learner = self.records.learner if self.records is not None else None
        if learner is None or self.records_profile is None:
            raise ApiError("not_found", "the companion needs an open learner database")

        def start_session() -> str:
            row = learner.conn.execute("SELECT level, subjects FROM profile").fetchone()
            subjects = json.loads(row[1] or "[]") if row else []
            first = next((x if isinstance(x, str) else x.get("subject") for x in subjects), None) or "physics"
            body = {"client_session_id": new_id("cli"), "mode": "study_companion", "subject": first,
                    "level": row[0] if row and row[0] in ("O", "A") else "O",
                    "origin": {"kind": "companion", "ref": None}}  # fmt: skip
            return str(self.create_session(body)["session_id"])

        def resume(session_id: str) -> bool:
            entry = self.sessions.get(session_id)
            if entry is not None:
                return entry.profile_id == CURRENT_PROFILE.get()
            # An encrypted archive includes authorized transcripts, never executable engine checkpoints.
            row = learner.conn.execute(
                "SELECT subject,started_at FROM sessions WHERE session_id=? AND mode='study_companion'",
                (session_id.removeprefix("ses_"),),
            ).fetchone()
            if row is None:
                return False
            profile = learner.conn.execute("SELECT level FROM profile").fetchone()
            meta = {
                "session_id": session_id,
                "title": "",
                "mode": "study_companion",
                "subject": row[0],
                "topic_id": None,
                "created_ts": row[1],
                "updated_ts": self.clock_ms(),
                "archived": False,
                "origin": "companion",
            }
            state = SessionState(
                session_id,
                subject=row[0],
                level=profile[0] if profile else "O",
                mode="study_companion",
                origin="companion",
            )
            if self.session_class is not None:
                hardware = self.session_class()
                if hardware is not None:
                    state.hardware_class = hardware
            entry = SessionEntry(
                session_id, meta, projection.empty(meta, self.epoch), state, profile_id=CURRENT_PROFILE.get()
            )
            self.sessions[session_id] = entry
            self.persist(entry)
            return True

        return await self.companion.open(start_session, resume, checkin=checkin)

    def end_companion(self, reason: str = "student_left") -> list[str]:
        """End today's open episodes (the Study screen's close) and any stale one: the summary is folded first, then
        the core's learning session ends, which queues its `session_end` Reflector job (§19.5.4)."""
        if self.records is None or self.records.learner is None or self.records_profile is None:
            return []
        ended = []
        try:
            episodes = self.companion.open_episodes()
        except Exception:
            return []
        for episode in episodes:
            if self.companion.end(episode, reason):
                if self.records is not None:
                    state = self.records.open.get(episode.session_id)
                    if state is not None:
                        self.records.end(episode.session_id, reason if reason in ("completed", "student_left",
                                         "idle_timeout", "interrupted") else None, self.clock_ms())  # fmt: skip
                    else:
                        self._reflect_ended(episode.session_id)
                ended.append(episode.session_id)
        return ended

    def end_stale_companion(self) -> list[str]:
        """Episodes of an earlier day, or idle for 20 minutes (JM-D23), end; on restart this finds the stale ones."""
        if self.records is None or self.records.learner is None or self.records_profile is None:
            return []
        ended = []
        with contextlib.suppress(Exception):
            for episode in self.companion.stale():
                reason = "idle_timeout" if episode.profile_day == self.companion.day() else "completed"
                if self.companion.end(episode, reason):
                    state = self.records.open.get(episode.session_id)
                    if state is not None:
                        self.records.end(episode.session_id, reason, self.clock_ms())
                    else:
                        self._reflect_ended(episode.session_id)
                    ended.append(episode.session_id)
        return ended

    def _reflect_ended(self, session_id: str) -> None:
        """An episode whose learning session is not open here (a restart): its session_end job, as `backlog` would."""
        if self.reflector is None or self.records_profile is None:
            return
        row = self.records.learner.conn.execute(  # type: ignore[union-attr]
            "SELECT subject, started_at FROM sessions WHERE session_id = ?", (session_id.removeprefix("ses_"),)
        ).fetchone()
        day = time.strftime("%Y-%m-%d", time.gmtime(int(row[1]) / 1000)) if row else self._today()
        self.reflector.trigger("session_end", self.records_profile, session_id, session_date=day,
                               subject=row[0] if row else None)  # fmt: skip

    def _on_event(self, entry: SessionEntry, envelope: dict[str, Any]) -> None:
        if self.sessions.get(entry.session_id) is not entry or (
            self.activator is not None and entry.profile_id != self.records_profile
        ):
            return  # late callbacks cannot reach a switched profile or a replacement session after reopening
        shown = student_view(envelope)  # every stream and snapshot today is student scope (API-11)
        if shown is None:
            return
        envelope = shown
        projection.apply(entry.state, envelope)
        if self.records is not None:
            self.records.on_event(entry.session_id, envelope, entry.state)  # DAT-06 rows, from the folded state

        self.pool.add(entry.session_id, envelope["seq"], frame(envelope))
        for connection in list(entry.connections):
            connection.put(envelope)
        event = envelope["event"]
        if event["type"] in ("turn.end", "part.end", "board.annotate", "visual.await") or (
            event["type"] == "board.step" and event["status"] in TERMINAL_STEP
        ):
            self.persist(entry)  # a crash loses at most the in-flight step (API-17)

    def _learning(self, events: list[Any]) -> None:
        """A turn's learning events go to the learner database that is open now (the session's profile's)."""
        if self.records is not None:
            self.records.learning(events)

    def persist(self, entry: SessionEntry) -> None:
        if self.sessions.get(entry.session_id) is not entry:
            return  # an old task cannot overwrite the checkpoint of a reopened session with the same id
        if self.activator is not None and entry.profile_id != self.records_profile:
            return  # never into another profile's learner database (DAT-14, SEC-19)
        self.store.save(entry.session_id, {"projection": entry.state, "engine": state_to_json(entry.engine_state),
                                           "profile_id": entry.profile_id})  # fmt: skip

    def entry(self, session_id: str) -> SessionEntry:
        entry = self.sessions.get(session_id)
        if entry is None or entry.profile_id != CURRENT_PROFILE.get():
            raise ApiError("not_found", "no such session")
        return entry

    def forget_profile(self, profile_id: str, drop: bool = False, idle: bool = False) -> None:
        """Lock or switch (SEC-19, DAT-14): persist and unload the profile's sessions, so nothing of its transcript or
        board stays in memory; `drop` also removes them (profile deletion, the `core` eraser of `erase_profile`). Its
        Reflector jobs are cancelled. An `idle` lock (the inactivity close) lets the profile's queued and running study
        jobs finish into its learner database, which closes once they are done; a switch, close or deletion pauses
        local study work and cancels cloud work (STU-14)."""
        scope = CURRENT_PROFILE.set(profile_id)
        try:
            for close in self.on_profile_closing:
                close(profile_id)
        finally:
            CURRENT_PROFILE.reset(scope)
        for finish in self.before_profile_forgotten:
            finish(profile_id)
        if self.reflector is not None:
            self.reflector.cancel_profile(profile_id)
        self.checkins.cancel_all()  # SPEC §19.3.4: extractions still running write nothing for the closed profile
        self.session_notes.pause()
        draining = (
            idle
            and not drop
            and self.activator is not None
            and self.records_profile == profile_id
            and self.settings.study_workspace_enabled
            and self.study.outstanding()
        )
        if draining:
            self.study.grants.clear()  # source-open grants still end with the profile (STU-15)
        else:
            self.study.pause(clear=True)
        for entry in [e for e in self.sessions.values() if e.profile_id == profile_id]:
            if entry.task is not None and not entry.task.done() and entry.engine is not None:
                entry.engine.cancel()
            if not drop:
                self.persist(entry)
            for connection in list(entry.connections):
                connection.put_frames([control({"type": "stream.closing", "reason": "profile_locked"})])
                connection.close()
            self.pool.forget(entry.session_id)
            del self.sessions[entry.session_id]
        if self.activator is not None and self.records_profile == profile_id:
            self.study_visits.end("interrupted", notify=False)  # and its Study session (§19.3.2)
            if self.records is not None:
                self.records.shutdown(self.clock_ms())  # its open learning sessions end interrupted (LRN-01)
            if draining:
                # No token reaches the profile any more (API-04); only its study queue writes until it is done.
                self.study_draining = profile_id
            else:
                self._close_learner()
        if drop:
            for session_id, record in self.store.load_all().items():
                if record.get("profile_id") == profile_id:
                    self.store.delete(session_id)
            # Every stored reply of the profile's own commands (keyed by the profile) and any that names it: a note's
            # or an answer's reply names neither the profile nor its id in the body (DAT-09).
            self.idempotency.entries = {k: v for k, v in self.idempotency.entries.items()
                                        if not k.startswith(f"{profile_id}:")
                                        and profile_id not in json.dumps(v.body)}  # fmt: skip
        for clear in self.on_profile_forgotten:
            clear(profile_id)

    def _close_learner(self) -> None:
        assert self.activator is not None
        self.records = self.activator(None)  # its learner database closes on lock, switch and deletion (DAT-14)
        self.records_profile = None
        self.study_draining = None
        if self.store_factory is not None:
            self.store = self.store_factory()  # checkpoints follow the open learner database (DAT-07)

    async def erase_profile(self, profile_id: str) -> dict[str, Any]:
        """DAT-09: delete a profile by walking the registry of per-profile stores (`store.profile_stores`): each
        eraser once, memory first and the profile's folder, key and device rows last. Returns each eraser's result."""
        return await self.erasers.erase(profile_id)

    def load_profile(self, profile_id: str) -> None:
        """Unlock: open the profile's learner database for its records and bring its saved sessions back."""
        if self.activator is not None:
            if self.reflector is not None:
                self.reflector.cancel_all()  # another profile's jobs never run against this database (AGT-26)
            self.records = self.activator(profile_id)  # the same database again when its study jobs kept it open
            self.records_profile = profile_id
            self.study_draining = None
            if self.store_factory is not None:
                self.store = self.store_factory()
        for session_id, record in self.store.load_all().items():
            if record.get("profile_id") == profile_id and session_id not in self.sessions:
                self._restore(session_id, record)
        if self.reflector is not None and self.activator is not None:
            self.reflector.backlog(profile_id)

    def diagnostic_traces(self, profile_id: str) -> list[dict[str, Any]]:
        """The profile's turn traces for a support bundle: redacted at the writer, so no learner text (OBS-19)."""
        records = self.trace_records() if self.trace_records is not None else []
        return [r for r in records if r.get("profile_ref") == profile_id]

    async def clear_caches(self) -> None:
        """After a lock or switch: no cached prompt tokens of the previous profile stay in the runtime (DAT-14)."""
        for clear in self.cache_clearers:
            with contextlib.suppress(Exception):
                await clear()

    def locks(self, profile_id: str) -> dict[str, Any]:
        """Settings forced by an active mock sitting or the restrict-only safety configuration (DAT-15); none until
        mock exams (M16)."""
        return {}

    def mine(self) -> list[SessionEntry]:
        """The sessions of the profile the request acts for."""
        profile = CURRENT_PROFILE.get()
        return [e for e in self.sessions.values() if e.profile_id == profile]

    # --- commands --------------------------------------------------------------------------------------------------

    def early_scopes(self) -> tuple[str, ...]:
        """Consent scopes of a later stage this build carries as an explicit capability (VOC-28)."""
        return ("cloud_voice",) if self.settings.cloud_voice_enabled else ()

    def meta(self) -> dict[str, Any]:
        return {"protocol": {"major": 1, "min_minor": 0, "max_minor": PROTOCOL["minor"]},
                "server_version": SERVER_VERSION, "epoch": self.epoch, "deployment": "local",
                "capabilities": ["sse_session", "snapshot", "cancel", "idempotency",
                                 *(["study_workspace"] if self.settings.study_workspace_enabled else []),
                                 *(["voice"] if self.voice_available else []),
                                 *(["voice_tutoring"] if self.voice_available and self.voice_tutoring() else []),
                                 *(["voice_dictation"] if self.voice_available and self.voice_dictation() else [])],
                "deprecated": []}  # fmt: skip

    def health(self) -> dict[str, Any]:
        status = "starting" if self.readiness == "starting" else "degraded" if self.readiness == "degraded" else "ok"
        components = dict(self.components)
        if self.health_details is not None:
            components.update(self.health_details())  # worker, sandbox, keychain, runtime fallback, speed (OBS-15)
        if self.storage_low is not None and self.storage_low():
            # DAT-13: nearly out of space; progress is still saved, photos and traces are off until space is freed.
            components.update({"storage": "degraded", "capture": "down"})
        if self.recoveries is not None and self.recoveries():
            components["learner_store"] = "degraded"  # a damaged learner database was recovered: tell the adult
        result: dict[str, Any] = {"status": status, "components": components, "readiness": self.readiness}
        if self.capture_summary is not None:
            result["perception"] = self.capture_summary()
        return result

    def model_catalogue(self) -> dict[str, Any]:
        result = (
            dict(self.models())
            if self.models is not None
            else {
                "installed": [],
                "available": [],
                "assignment": {"fast": None, "strong": None},
                "recommended": {"fast": None, "strong": None, "reason_key": "no_class.unknown"},
            }
        )
        profile = CURRENT_PROFILE.get()
        learner = (
            self.records.learner.conn
            if self.records is not None
            and self.records.learner is not None
            and self.records_profile == profile
            and profile is not None
            else None
        )
        if self.online is not None:
            result["online_inference"] = self.online.service_availability(learner)
        else:
            result.pop("online_inference", None)  # optional field; a present projection must name every service
        return result

    def assessment_guard(self) -> None:
        if self.assessments is not None:
            from agent_core.engine.assessment.errors import Refused

            opened = self.assessments()
            if opened is not None:
                try:
                    opened.guard_tutor()
                except Refused:
                    raise ApiError("tutor_locked", "an assessment is open") from None
        if self.mock_active is not None and self.mock_active():
            raise ApiError("tutor_locked", "a mock is open")

    def create_session(self, body: dict[str, Any]) -> dict[str, Any]:
        self.assessment_guard()
        self._guard()
        require("CreateSession", body)
        session_id = new_id("ses")
        now = self.clock_ms()
        meta = {"session_id": session_id, "title": "", "mode": body["mode"], "subject": body["subject"],
                "topic_id": body.get("topic_id"), "created_ts": now, "updated_ts": now, "archived": False,
                # Its subject and topic can change after it starts (D94): the list names where it was opened from.
                "origin": body["origin"]["kind"]}  # fmt: skip
        engine_state = SessionState(session_id, subject=body["subject"], level=body.get("level", "O"),
                                    mode=body["mode"], origin=body["origin"]["kind"],
                                    topic_id=body.get("topic_id"))  # fmt: skip
        if body["origin"]["kind"] == "lesson" and self.assessments is not None:
            from agent_core.engine.assessment.errors import Refused
            from agent_core.engine.learning_journey import LearningJourney

            opened = self.assessments()
            if opened is not None:
                try:
                    # Journey reviews have already exposed the explanation and example.
                    # Their tutor help is assistance, not a new entry diagnostic.
                    lesson_id = body["origin"]["ref"]
                    probe = (
                        None if LearningJourney(opened).reviewing_lesson(lesson_id) else opened.lesson_probe(lesson_id)
                    )
                except Refused as error:
                    raise ApiError("validation_failed", error.reason) from None
                if probe is not None:
                    raise ApiError(
                        "tutor_locked",
                        "answer the entry probe before starting this lesson",
                        details={"probe_id": probe["probe_id"]},
                    )
        if body["origin"]["kind"] == "lesson":  # the session plays an installed lesson (AGT-22, UXS-23)
            engine_state.lesson_id, meta["title"] = self._installed_lesson(body["origin"].get("ref"))
        if body["origin"]["kind"] in ("exam_review", "practice_item") and self.assessments is not None:
            from agent_core.engine.assessment.errors import Refused
            from agent_core.engine.assessment.tutor import load_context

            opened = self.assessments()
            if opened is None:
                raise ApiError("not_found", "no open assessment database")
            try:
                loaded = opened.tutor_context(body["origin"])
                load_context(engine_state, loaded)
                meta["mode"] = engine_state.mode
            except Refused as error:
                raise ApiError("validation_failed", error.reason) from None
        hardware_class = self.session_class() if self.session_class is not None else None
        if hardware_class is not None:
            engine_state.hardware_class = hardware_class
        entry = SessionEntry(session_id, meta, projection.empty(meta, self.epoch), engine_state,
                             profile_id=CURRENT_PROFILE.get())  # fmt: skip
        self.sessions[session_id] = entry
        if self.records is not None:
            self.records.session_started(meta)
        self.persist(entry)
        return {"session_id": session_id, "cursor": dict(entry.state["cursor"])}

    def _installed_lesson(self, lesson_id: Any) -> tuple[str, str]:
        """The lesson a lesson session opens on, one an installed pack holds, and its title (AGT-22)."""
        if not isinstance(lesson_id, str) or self.learn is None:
            raise ApiError("validation_failed", "a lesson session names an installed lesson")
        with self.learn() as desk:
            found = desk.lesson(lesson_id) if desk is not None else None
        if found is None:
            raise ApiError("validation_failed", "that lesson is not installed")
        return lesson_id, str(found["title"])[:120]

    def list_sessions(self, limit: int = 20, before: str | None = None) -> dict[str, Any]:
        # The companion's daily episodes are opened from Study (SPEC §19.4), not listed with tutoring sessions.
        listed = (e.meta for e in self.mine() if not e.meta["archived"] and e.meta["mode"] != "study_companion")
        visible = sorted(listed, key=lambda m: m["session_id"], reverse=True)
        if before is not None:
            visible = [m for m in visible if m["session_id"] < before]
        page = visible[: max(1, min(limit, 100))]
        more = len(visible) > len(page)
        return {"sessions": [dict(m) for m in page], "next_before": page[-1]["session_id"] if more else None}

    def learning_state(self, session_id: str) -> dict[str, Any]:
        """A quiet UI follows the authoritative problem checkpoint, including after reconnect."""
        entry = self.entry(session_id)
        messages = entry.state["messages"]
        last = next((m["turn_id"] for m in reversed(messages) if m["role"] == "tutor"), None)
        state = entry.engine_state
        return {"session_id": session_id, "at_turn": last,
                "problem_status": "none" if state.problem is None else "complete" if state.solved else "open"}

    def snapshot(self, session_id: str) -> dict[str, Any]:
        entry = self.entry(session_id)
        if entry.engine is not None:
            entry.engine.events.flush()
        return projection.snapshot(entry.state)

    def update_session(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.assessment_guard()
        self._guard()
        require("UpdateSession", body)
        entry = self.entry(session_id)
        changed: dict[str, Any] = {}
        if "title" in body:
            entry.meta["title"] = changed["title"] = body["title"]
            if entry.engine is not None:
                entry.engine.titled = True  # a renamed chat keeps the student's title (UXS-68)
        # The mock lock is checked in code before any mode change (AGT-25); a refused change leaves the mode as it was.
        allowed = body.get("mode") not in MODES or decide_mode_change(entry.engine_state, body["mode"]).allowed
        if body.get("mode") == "solution" and entry.engine_state.mode != "solution":
            # The full worked solution whenever the student asks (PED-09, PED-02, AGT-20, D87): the engine switches
            # the mode, says so and presents the verified plan as a turn of its own (or the mock lock's template).
            if entry.session_id in self.voice_engines or (entry.task is not None and not entry.task.done()):
                raise ApiError("turn_active", "a turn is running in this session",
                               details={"turn_id": entry.active_turn})  # fmt: skip
            engine = self._engine(entry)
            engine.voice_mode = False
            ids = TurnIds()
            entry.active_turn = ids.turn_id
            entry.task = asyncio.get_running_loop().create_task(
                self._run(entry, engine.show_solution(ids=ids), entry.engine_state.solved)
            )
        elif "mode" in body and body["mode"] != entry.meta["mode"] and allowed:
            entry.meta["mode"] = changed["mode"] = body["mode"]
            entry.engine_state.mode = body["mode"]
        if changed:
            self._engine(entry).events.emit({"type": "session.updated", **changed})
        self.persist(entry)
        return dict(entry.meta)

    def archive(self, session_id: str) -> None:
        self._guard()
        entry = self.entry(session_id)
        entry.meta["archived"] = True
        self.persist(entry)

    def limit_command(self, path: str) -> None:
        """API-33: a burst limit on commands; card answers and cancels are exempt (snapshots are reads)."""
        if path.endswith("/cancel") or path.startswith("/v1/cards/"):
            return
        wait = self.command_bucket.take()
        if wait:
            raise ApiError("rate_limited", "too many commands; wait a moment", retry_after_ms=max(1, int(wait * 1000)))

    def limit_turn_start(self) -> None:
        """API-33: turn starts per profile, at the API boundary (a card answer's own turn is exempt)."""
        per_min = self.settings.api_turn_starts_per_min
        bucket = self.turn_buckets.setdefault(CURRENT_PROFILE.get(), TokenBucket(per_min / 60, per_min))
        wait = bucket.take()
        if wait:
            raise ApiError("rate_limited", "too many turns started; wait a moment", retry_after_ms=int(wait * 1000) + 1)

    async def preflight_input(self, session_id: str, body: Any) -> None:
        """VER-03/INP-07: input errors precede acceptance, board changes and any model call."""
        require("StartTurn", body)
        if self.capture_desk is not None:
            body.update(self.capture_desk.turn_body(session_id, body))
        if body["intent"] != "board_line":
            return
        self.assessment_guard()
        self._guard()
        entry = self.entry(session_id)
        if entry.task is not None and not entry.task.done():
            raise ApiError("turn_active", "a turn is running in this session")
        from agent_core.engine.slice import for_tool, worded

        blocks = redact_blocks(normalise_blocks(body["content"]))
        if self._guarded(entry, blocks):  # the input guard answers it in the turn, before any check (PRV-13)
            return
        if entry.engine_state.mock_running or (self.mock_active is not None and self.mock_active()):
            raise ApiError("tutor_locked", "working checks are locked during a mock sitting")
        if any(b["type"] == "ledger_table" for b in blocks):
            from agent_core.engine.ledger_input import response

            if entry.engine_state.subject != "accounting":
                raise ApiError("validation_failed", "accounting working belongs to an accounting session")
            try:
                response(blocks)
            except ValueError as error:
                raise ApiError("validation_failed", str(error)) from None
            if entry.engine_state.problem is None:
                raise ApiError("request_conflict", "start a question before adding working")
            return
        lines = [line.strip() for block in blocks for line in _block_text(block).splitlines() if line.strip()]
        if not 1 <= len(lines) <= 30:
            raise ApiError("validation_failed", "use up to 30 working lines of at most 2000 characters")
        for index, line in enumerate(lines):
            if len(line) > 2000:
                raise ApiError(
                    "validation_failed",
                    "split the long working line into steps",
                    details={
                        "input_error": "LIMIT_EXCEEDED",
                        "line": index + 1,
                        "position": 2000,
                        "hint_key": "too_long",
                        "word": "",
                    },
                )
        worker = self._engine(entry).deps.executor.supervisor
        if worker is None or not worker.compute_available():
            raise ApiError("worker_unavailable", "the working checker is unavailable; your draft is kept")
        for index, line in enumerate(lines):
            if (body.get("capture_id") and "[?]" in line) or worded(line):
                continue  # a worded step is never parsed as algebra; it stays on the board unchecked (VER-27)
            outcome = await worker.call(
                "math_parse", 1, {"text": for_tool(line)}, new_id("chk"), 1000, f"input_parse_{index}"
            )
            if not outcome.ok:
                if outcome.error in ("invalid_args", "limit_exceeded"):
                    unmatched = any(line.count(a) > line.count(b) for a, b in (("(", ")"), ("{", "}"), ("[", "]")))
                    hint = (
                        "too_long"
                        if outcome.error == "limit_exceeded"
                        else "unbalanced_bracket"
                        if unmatched
                        else "empty_slot"
                        if "end of input" in outcome.detail
                        else "unknown_word"
                    )
                    position = outcome.data.get("position", 0)
                    tail = for_tool(line)[position:] if isinstance(position, int) else ""
                    word = re.match(r"\\?[A-Za-z]+", tail)
                    raise ApiError(
                        "validation_failed",
                        "check the brackets and symbols in your working",
                        details={
                            "input_error": "PARSE_REJECTED" if outcome.error == "invalid_args" else "LIMIT_EXCEEDED",
                            "line": index + 1,
                            "position": position,
                            "hint_key": hint,
                            "word": word[0][:40] if word else tail[:1],
                        },
                    )
                raise ApiError("worker_unavailable", "the working checker is unavailable; your draft is kept")

    @staticmethod
    def _why_step(entry: SessionEntry, body: dict[str, Any]) -> str:
        """ "Why?" names a line of the open problem's board (UXS-15): the `board_step` ref is required and must be a
        line on that board with something written on it."""
        ref = body.get("ref") or {}
        if ref.get("kind") not in ("board_step", "visual_part"):
            raise ApiError("validation_failed", "Why? asks about a board line, a term of it or a part of a visual",
                           details={"errors": ["$.ref: a board_step or visual_part ref is required"]})  # fmt: skip
        from agent_core.engine.pointing_back import resolve_ref

        if resolve_ref(entry.engine_state, str(ref["id"])) is None:
            raise ApiError("not_found", "that is not on the board or on screen")
        return str(ref["id"])

    def set_selection(self, session_id: str, body: dict[str, Any]) -> None:
        """UXS-58: what the student has selected or hovers, so "this" in a message refers to it (ids only)."""
        self._guard()
        require("SelectionSet", body)
        entry = self.entry(session_id)
        ref = body.get("ref")
        if ref is None:
            entry.engine_state.selection = None
            return
        from agent_core.engine.pointing_back import resolve_ref

        if resolve_ref(entry.engine_state, str(ref["id"])) is None:
            raise ApiError("not_found", "that is not on the board or on screen")
        entry.engine_state.selection = str(ref["id"])

    def _guarded(self, entry: SessionEntry, blocks: list[dict[str, Any]]) -> bool:
        """The input guard routes this content to a template (PRV-13): the turn then goes straight to the guard as a
        message, with no parse check, board change or ledger validation first. Personal details alone do not count:
        they are already redacted and the work goes on."""
        text = "\n".join(_block_text(b) for b in blocks).strip()
        if not text:
            return False
        result = self._engine(entry).guard.check(text, entry.engine_state.level)
        return result.template is not None and result.category != "personal_data"

    async def tutoring_usage(self, session_id: str) -> TutoringUsage:
        from agent_core.store import subscription
        from agent_core.store.egress import DeviceSpend

        entry = self.entry(session_id)
        profile, online = entry.profile_id, self.online
        if profile is None or online is None or self.records_profile != profile:
            return TutoringUsage("unavailable")
        stored, now = online.subscription(), self.clock_ms()
        if not subscription.covers(stored, profile, now):
            return TutoringUsage("uncovered")
        _, start, end = subscription.allowance_window(stored, now)

        def current() -> bool:
            return (self.sessions.get(session_id) is entry and self.records_profile == profile
                    and start <= self.clock_ms() < end and online.subscription() == stored)

        def may_read_online() -> bool:
            return current() and online.cloud_effective(profile, getattr(self.store, "conn", None))

        remote = await online.tutoring_balance(profile, may_read_online) if stored.get("seats") == 1 else None
        if not current():
            return TutoringUsage("unavailable")
        period_ends = end == stored["period_end"]
        tokens = stored.get("allowance_tokens")
        device_left = None
        if type(tokens) is int and tokens >= 0:
            meter = DeviceSpend(online.device, profile, subscription.allowance_period(stored, now), self.clock_ms)
            device_left = max(0.0, 1 - meter.period_tokens() / tokens) if tokens else 0.0
        if (remote and remote.get("subscription_id") == stored["subscription_id"]
                and remote.get("window_start_ms") == start):
            fraction = remote.get("remaining_fraction")
            if type(fraction) in (float, int) and 0 <= fraction <= 1:
                limiting_device = device_left if device_left is not None and device_left < fraction else None
                return TutoringUsage("subscription", fraction, end, period_ends,
                                     remote.get("has_reservations") is True, limiting_device)
        if device_left is None:
            return TutoringUsage("unavailable")
        return TutoringUsage("device", device_left, end, period_ends)

    def start_turn(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.assessment_guard()
        self._guard()
        require("StartTurn", body)
        input_source = "student_text"
        if self.capture_desk is not None:
            from agent_core.api.captures import PHOTO_SOURCES

            body = self.capture_desk.turn_body(session_id, body)
            # Only words read from a photo are photo text; typed, pasted and dictated captures are the student's own
            # (PRM-18, PRM-19).
            if body.get("capture_id") and self.capture_desk.get(body["capture_id"])["source"] in PHOTO_SOURCES:
                input_source = "ocr_text"
        body = {**body, "content": normalise_blocks(body["content"])}
        entry = self.entry(session_id)
        if entry.session_id in self.voice_engines or (entry.task is not None and not entry.task.done()):
            raise ApiError("turn_active", "a turn is running in this session", details={"turn_id": entry.active_turn})
        lesson = entry.engine_state.lesson_id is not None
        why_step = self._why_step(entry, body) if body["intent"] == "why" else None
        sketch = body["intent"] == "check_my_working" and bool(body.get("capture_id"))  # a photographed sketch
        if (
            body["intent"] not in ("ask", "answer", "stuck", "board_line", "show_step", "why")
            and not (lesson and body["intent"] == "next_step")
            and not sketch
        ):
            raise ApiError("validation_failed", f"intent {body['intent']!r} is not available yet",
                           details={"errors": ["$.intent: not available in this version"]})  # fmt: skip
        if any(block["type"] == "image_ref" for block in body["content"]):
            # A turn starts only from a confirmed capture (INP-01); the capture store arrives in S2, so no image here
            # can belong to one yet (`capture_unconfirmed` is not in TABLE API-32a, gaps.md).
            raise ApiError("capture_not_ready", "that image is not part of a confirmed capture")
        # Personal details a student types are redacted before anything is stored or reaches a model (PRV-20).
        body = {**body, "content": redact_blocks(body["content"])}
        text = "\n".join(_block_text(b) for b in body["content"]).strip()
        if not text:
            raise ApiError("validation_failed", "the message is empty", details={"errors": ["$.content: empty"]})
        from agent_core.engine.companion_memory import parse as memory_command

        explicit_memory = entry.meta.get("mode") == "study_companion" and memory_command(text) is not None
        usage_question = (body["intent"] in ("ask", "answer") and input_source == "student_text"
                          and all(b["type"] == "text" for b in body["content"]) and asks_usage(text))
        if (not usage_question and not explicit_memory
                and self.tutor_unavailable is not None and self.tutor_unavailable()):
            # PLT-29: content-only with neither a school server nor the online tier; nothing is sent to any model.
            raise ApiError("model_unavailable", "the tutor cannot run on this computer offline")
        guarded = self._guarded(entry, body["content"])
        if any(b["type"] == "ledger_table" for b in body["content"]) and not guarded:
            from agent_core.engine.ledger_input import response

            try:
                response(body["content"])
            except ValueError as error:
                raise ApiError("validation_failed", str(error)) from None
        engine = self._engine(entry)
        board_lines = None
        ledger_work = any(b["type"] == "ledger_table" for b in body["content"]) and not guarded
        if body["intent"] == "board_line" and not ledger_work and not guarded:
            if entry.engine_state.problem is None:
                raise ApiError("request_conflict", "start a question before adding working")
            problem = entry.engine_state.problem
            expected_board = f"{problem.problem_id}:{hashlib.sha256(problem.statement.encode()).hexdigest()[:16]}"
            current_board = entry.engine_state.board.problem_key == expected_board
            existing = {s.step_id: s for s in entry.engine_state.board.steps}
            waiting = next(
                (
                    s
                    for s in existing.values()
                    if current_board and s.owner == "student" and s.status == "awaiting" and not s.latex.strip()
                ),
                None,
            )
            board_lines = []
            for block in body["content"]:
                if block["type"] == "board_line":
                    board_lines.append((block["step_id"], block["latex"]))
                elif block["type"] in ("text", "latex"):
                    for line in _block_text(block).splitlines():
                        if line.strip():
                            step_id = waiting.step_id if waiting is not None else new_id("stp")
                            board_lines.append((step_id, line.strip()))
                            waiting = None
                else:
                    raise ApiError("validation_failed", "working must contain text or board lines")
            if not 1 <= len(board_lines) <= 30 or any(not line.strip() or len(line) > 2000 for _, line in board_lines):
                raise ApiError("validation_failed", "use up to 30 working lines of at most 2000 characters")
            seen = set()
            for step_id, _ in board_lines:
                if step_id in seen or (
                    step_id in existing and (existing[step_id].owner != "student" or not current_board)
                ):
                    raise ApiError("request_conflict", "only your own working lines can be edited")
                seen.add(step_id)
            student_count = sum(
                current_board and s.owner == "student" and bool(s.latex.strip()) for s in existing.values()
            )
            added = sum(step_id not in existing or not existing[step_id].latex.strip() for step_id, _ in board_lines)
            if student_count + added > 30:
                raise ApiError("validation_failed", "the working board allows up to 30 student lines")
            if body.get("capture_id") and self.capture_desk is not None:
                # Lines read from the student's own photo keep their place on it (JM-D10), for pointing.
                from agent_core.engine.photo_regions import photo_lines

                state = entry.engine_state
                on_board = set(existing)
                state.photo_lines = {k: v for k, v in state.photo_lines.items() if k in on_board}
                state.photo_lines.update(photo_lines(self.capture_desk.get(body["capture_id"]), board_lines))
        engine.voice_mode = False
        if self.reflector is not None:
            self.reflector.pause()  # the student goes first: a running reflection stops and waits (AGT-26)
        self.session_notes.pause()
        self.study.pause()
        if self.records is not None:
            self.records.touch(entry.meta, self.clock_ms())
        ids = TurnIds(client_message_id=body["client_message_id"])
        projection.student_message(entry.state, body["client_message_id"], body["content"])
        if not entry.meta["title"] and chat_title(text):
            # Titled from the first exchange, at most six words (UXS-68); the student can rename it.
            engine.events.emit({"type": "session.updated", "title": chat_title(text)})
        entry.active_turn = ids.turn_id
        new_problem = entry.engine_state.problem is None or entry.engine_state.solved or body["intent"] == "ask"
        new_problem = new_problem and not (guarded and entry.engine_state.problem is not None)
        if (entry.engine_state.problem is not None and input_source == "student_text"
                and body["intent"] == "answer" and asks_why(text)):
            # A reasoning follow-up belongs to the existing problem even after its answer is reached.
            # Starting a problem here would replace the question and re-withhold its checked answer.
            # Explicit "ask" still starts a new question, including a new conceptual why-question.
            new_problem = False
        if usage_question and not guarded:
            work = engine.usage_turn(lambda: self.tutoring_usage(session_id), ids, body["intent"])
        elif entry.meta.get("mode") == "study_companion":
            # The companion's thread (SPEC §19.5.1): the same input guard first, then its own turn, never a problem.
            work = engine.companion_turn(text, ids=ids, source=input_source)
        elif sketch and not guarded:
            from agent_core.engine.sketch_photo import sketch_turn

            # Snap your sketch (JM-D10): the drawing is compared with the problem's checked diagram.
            work = sketch_turn(engine, self.capture_desk.get(body["capture_id"]), ids)
        elif why_step is not None and not guarded:
            work = engine.why_line(why_step, text, ids=ids)  # "Why?" on a board line (UXS-15, SKL-08)
        elif lesson and body["intent"] == "next_step":
            work = engine.lesson_next(ids=ids)  # the lesson's next block, or its saved one (AGT-22)
        elif lesson and body["intent"] == "ask" and not guarded:
            work = engine.lesson_question(text, ids=ids)  # a side question, or a problem for after the lesson
        elif ledger_work and not new_problem:
            work = engine.ledger_turn(text, body["content"], ids=ids)
        elif board_lines is not None:
            work = engine.board_turn(board_lines, ids=ids, source=input_source)
        elif new_problem:
            work = engine.start_problem(text, ids=ids, source=input_source)
        else:
            work = engine.student_turn(
                text,
                ids=ids,
                stuck=body["intent"] == "stuck",
                show_step=body["intent"] == "show_step",
                source=input_source,
            )  # PED-06, UXS-17
        solved_before = entry.engine_state.solved  # a new problem resets it; only a turn that solves one is an end
        entry.task = asyncio.get_running_loop().create_task(self._run(entry, work, solved_before))
        return {"turn_id": ids.turn_id, "message_id": ids.message_id}

    async def _run(self, entry: SessionEntry, work: Any, solved_before: bool = False) -> None:
        try:
            # A crash inside the turn has already ended it with `turn.end` error and kept what was shown (AGT-12);
            # the engine logged it. Nothing else is owed to the client here.
            with contextlib.suppress(Exception):
                await work
        finally:
            entry.active_turn = None
            if self.sessions.get(entry.session_id) is entry:
                if entry.engine_state.mode in ("guided", "solution", "check_working"):
                    entry.meta["mode"] = entry.engine_state.mode  # the engine switches modes on a request (PED-02)
                carried = entry.engine.steering.end_of_turn() if entry.engine is not None else []
                if carried:  # steering that never reached a model call starts the next turn, in order (AGT-17)
                    asyncio.get_running_loop().call_soon(self._start_carried, entry, [s.text for s in carried])
                if self.records is not None and entry.engine_state.solved:
                    self.records.solved(entry.session_id)
                self.persist(entry)
                if self.reflector is not None:
                    self.reflector.activity()  # the idle delay counts from the end of the reply
                    if entry.engine_state.solved and not solved_before:
                        self._reflect("problem_end", entry)  # the end of a problem (MEM-06)

    def answer_card(self, request_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """API-14: an open card is answered once; the answer becomes the student's next turn. A card answered or
        cancelled before says so; open cards survive a restart through the snapshot."""
        self.assessment_guard()
        self._guard()
        require("CardAnswer", body)
        planner = self.planner() if self.planner is not None else None
        if planner is not None and planner.owns(request_id):  # Home's plan card (LRN-31, UXS-12): no session needed
            from agent_core.engine.plans import PlanRefused

            try:
                return dict(planner.answer_card(request_id, body["answer"]))
            except PlanRefused as error:
                raise ApiError("validation_failed", error.reason, details={"reason": error.reason}) from None
        entry, request, part = self._card(request_id)
        card = (part or {}).get("card", {})
        if request is None:
            if card.get("state") == "answered":
                return {"status": "already_answered", "answered_ts": card.get("answered_ts")}
            if card.get("state") == "cancelled":
                return {"status": "cancelled", "answered_ts": None}
            raise ApiError("not_found", "no such card request")
        if entry.session_id in self.voice_engines or (entry.task is not None and not entry.task.done()):
            raise ApiError("turn_active", "the tutor is still answering", details={"turn_id": entry.active_turn})
        answer = body["answer"]
        labels = {o["id"]: o["label"] for o in request["card"].get("options", [])}
        text = answer.get("text") or ", ".join(labels.get(i, i) for i in answer.get("option_ids", [])) or ""
        text = redact_personal(normalise_input(text))[0]  # nothing unredacted is emitted or kept (PRV-20)
        summary = text or answer.get("action") or ("skipped" if answer.get("skipped") else "")
        now = self.clock_ms()
        engine = self._engine(entry)
        if self.records is not None and not text:
            self.records.touch(entry.meta, now)  # a text answer is touched as the turn it starts
        engine.events.emit({"type": "card.answered", "request_id": request_id, "answer_summary": summary[:400],
                            "answered_ts": now})  # fmt: skip
        topics = engine.card_answered(request_id)
        self.persist(entry)
        if topics is not None:  # the topic chooser after the Diagnoser failed (AGT-27): the problem goes on
            ids = TurnIds(client_message_id=body["client_answer_id"])
            entry.active_turn = ids.turn_id
            entry.task = asyncio.get_running_loop().create_task(
                self._run(entry, engine.continue_problem(text or summary, ids=ids))
            )
            return {"status": "accepted", "answered_ts": now}
        if engine.answer_egress(request_id, answer):
            return {"status": "accepted", "answered_ts": now}  # a first-use egress notice (ARC-10): no turn follows
        offered = offer_answered(engine, request_id, answer)
        if offered is not None:  # the engine's hint or detour offer (LRN-20, LRN-23): declining starts no turn
            choice, offer = offered
            if choice != "declined":
                ids = TurnIds(client_message_id=body["client_answer_id"])
                entry.active_turn = ids.turn_id
                entry.task = asyncio.get_running_loop().create_task(
                    self._run(entry, engine.accept_offer(choice, offer, text or summary, ids))
                )
            self.persist(entry)
            return {"status": "accepted", "answered_ts": now}
        if text:
            if self.records is not None:
                self.records.card_answer(body["client_answer_id"])
            self.start_turn(entry.session_id, {"client_message_id": body["client_answer_id"], "intent": "answer",
                                               "content": [{"type": "text", "text": text}]})  # fmt: skip
        return {"status": "accepted", "answered_ts": now}

    def delete_sketch(self, sketch_id: str) -> None:
        """VSL-43: "Remove sketch" removes it from the board, the session's history and replay: the engine's record of
        the part and the saved session projection both lose it."""
        self._guard()
        for entry in self.mine():
            part = next(
                (
                    p["part_id"]
                    for p in entry.state["parts"].values()
                    if (p.get("sketch") or {}).get("sketch_id") == sketch_id
                ),
                None,
            )
            if part is None:
                continue
            if entry.engine is not None:
                entry.engine.sketches.remove(sketch_id)
                entry.engine.persisted.pop(part, None)
            projection.remove_part(entry.state, part)
            if self.records is not None:
                self.records.transcripts.remove_part(entry.session_id, part)
            self.persist(entry)
            return
        raise ApiError("not_found", "no sketch with that id")

    def board_replay(self, board_id: str) -> dict[str, Any]:
        """UXS-59: a board played again from its recorded events and the tutor's words; no model, no network, no
        audio."""
        from agent_core.engine.replay import board_replay

        self._guard()
        for entry in self.mine():
            persisted = projection.replay_parts(entry.state)
            replay = board_replay(persisted, board_id)
            if replay is None and entry.engine is not None:
                replay = board_replay(entry.engine.persisted, board_id)
            if replay is not None:
                return replay
        raise ApiError("not_found", "no recorded board with that id")

    @contextlib.contextmanager
    def _visual_event_turn(self, entry: SessionEntry, visual_id: str) -> Any:
        """A diagram interaction belongs to the tutor turn that displayed it, also after reopening."""
        engine = self._engine(entry)
        events = engine.events
        previous = events.turn_id, events._index
        visual = entry.state["visuals"][visual_id]
        events.turn_id = entry.state["parts"][visual["part_id"]]["turn_id"]
        events._index = (
            max((p["index"] for p in entry.state["parts"].values() if p["turn_id"] == events.turn_id), default=-1) + 1
        )
        try:
            yield engine
        finally:
            events.turn_id, events._index = previous

    def start_manipulation(self, visual_id: str, body: dict[str, Any]) -> dict[str, Any]:
        from agent_core.engine.manipulation import ManipulationRefused, exercise_key

        self._guard()
        self.assessment_guard()
        require("ManipulationStart", body)
        entry = next((e for e in self.mine() if visual_id in e.state["visuals"]), None)
        if entry is None:
            raise ApiError("not_found", "no such diagram")
        if entry.session_id in self.voice_engines or (entry.task is not None and not entry.task.done()):
            raise ApiError("tutor_locked", "the tutor is still answering")
        if entry.engine_state.manipulation is not None:
            raise ApiError("request_conflict", "finish the current diagram exercise first")
        spec = entry.state["visuals"][visual_id]["spec"]
        try:
            key = exercise_key(spec, body["part_id"])
            with self._visual_event_turn(entry, visual_id) as engine:
                step = engine.open_manipulation(
                    visual_id,
                    {body["part_id"]: key},
                    "Match the selected part on the diagram.",
                    str(spec.get("params", {}).get("variable", "x")),
                )
        except ManipulationRefused as refused:
            raise ApiError(refused.code, refused.detail) from None
        self.persist(entry)
        return {"step_id": step}

    async def answer_manipulation(self, visual_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """VSL-42: the values the student set on a manipulable visual, checked by the same tool as the typed line."""
        from agent_core.engine.manipulation import ManipulationRefused, checker_value

        self._guard()
        self.assessment_guard()
        require("ManipulationSubmit", body)
        entry = next(
            (e for e in self.mine() if (e.engine_state.manipulation or {}).get("visual_id") == visual_id), None
        )
        if entry is None:
            raise ApiError("not_found", "this visual is not waiting for the student")
        if entry.session_id in self.voice_engines or (entry.task is not None and not entry.task.done()):
            raise ApiError("tutor_locked", "the tutor is still answering", details={"turn_id": entry.active_turn})

        async def checked(name: str, args: dict[str, Any]) -> tuple[Any, str | None]:
            return await checker_value(self._engine(entry), name, args)

        try:
            with self._visual_event_turn(entry, visual_id) as engine:
                result: dict[str, Any] = await engine.answer_manipulation(visual_id, body, self.check_tool or checked)
        except ManipulationRefused as refused:
            raise ApiError(refused.code, refused.detail) from None
        self.persist(entry)
        return result

    def _card(self, request_id: str) -> tuple[SessionEntry, dict[str, Any] | None, dict[str, Any] | None]:
        for entry in self.mine():
            open_request = next((r for r in entry.state["open_requests"] if r["request_id"] == request_id), None)
            part = next((p for p in entry.state["parts"].values()
                         if p.get("card", {}).get("request_id") == request_id), None)  # fmt: skip
            if open_request is not None or part is not None:
                return entry, open_request, part
        raise ApiError("not_found", "no such card request")

    def _start_carried(self, entry: SessionEntry, texts: list[str]) -> None:
        """Queued steering the turn ended before: the next turn starts from it, in order (AGT-17)."""
        if self.shutting_down or (entry.task is not None and not entry.task.done()) or entry.engine is None:
            return
        ids = TurnIds()
        entry.active_turn = ids.turn_id
        entry.engine.voice_mode = False
        text = "\n".join(texts)
        state = entry.engine_state
        work = (entry.engine.start_problem(text, ids=ids) if state.problem is None or state.solved
                else entry.engine.student_turn(text, ids=ids))  # fmt: skip
        entry.task = asyncio.get_running_loop().create_task(self._run(entry, work, state.solved))

    def steer(self, turn_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """API-25: a message for the running turn is queued (`queued`); when that turn has ended the client starts a
        new turn with the same client id (`turn_ended`)."""
        self._guard()
        require("Steer", body)
        # Personal details are redacted before anything is queued, projected or kept (PRV-20).
        body = {**body, "content": redact_blocks(normalise_blocks(body["content"]))}
        text = "\n".join(_block_text(b) for b in body["content"]).strip()
        if not text:
            raise ApiError("validation_failed", "the message is empty", details={"errors": ["$.content: empty"]})
        entry = next((e for e in self.mine() if e.active_turn == turn_id), None)
        if entry is None:
            if any(e.engine is not None and any(t.get("turn_id") == turn_id for t in e.engine.turn_log)
                   for e in self.mine()):  # fmt: skip
                return {"message_id": None, "status": "turn_ended"}
            raise ApiError("not_found", "no such turn")
        voice_engine = self.voice_engines.get(entry.session_id)
        engine = voice_engine or self._engine(entry)
        steer = engine.steer(text, client_message_id=body["client_message_id"])
        if steer is None and voice_engine is not None:
            steer = engine.steering.push(Steer(new_id("msg"), text, body["client_message_id"]))
            engine.events.emit(
                {
                    "type": "message.accepted",
                    "client_message_id": body["client_message_id"],
                    "message_id": steer.message_id,
                    "turn_id": turn_id,
                    "disposition": "steer_queued",
                },
                turn_id=turn_id,
            )
        if steer is None:
            return {"message_id": None, "status": "turn_ended"}
        projection.student_message(entry.state, body["client_message_id"], body["content"])
        return {"message_id": steer.message_id, "status": "queued"}

    def side_question(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """API-26: a forked turn with tools denied and nothing written to the lesson (AGT-18)."""
        self.assessment_guard()
        self._guard()
        require("SideQuestion", body)
        body = {**body, "content": redact_blocks(normalise_blocks(body["content"]))}  # PRV-20
        entry = self.entry(session_id)
        if entry.session_id in self.voice_engines or (entry.task is not None and not entry.task.done()):
            raise ApiError("turn_active", "a turn is running in this session", details={"turn_id": entry.active_turn})
        text = "\n".join(_block_text(b) for b in body["content"]).strip()
        if not text:
            raise ApiError("validation_failed", "the message is empty", details={"errors": ["$.content: empty"]})
        if self.tutor_unavailable is not None and self.tutor_unavailable():
            raise ApiError("model_unavailable", "the tutor cannot run on this computer offline")
        engine = self._engine(entry)
        ids = TurnIds(client_message_id=body["client_message_id"])
        entry.active_turn = ids.turn_id
        entry.task = asyncio.get_running_loop().create_task(self._run(entry, engine.side_question(text, ids=ids),
                                                                      entry.engine_state.solved))  # fmt: skip
        return {"turn_id": ids.turn_id, "message_id": ids.message_id}

    def step_idle(self, session_id: str, body: dict[str, Any]) -> None:
        """LRN-20, API 1.10 (D104): the client's report that the open step has had no input for `idle_ms`, and whether
        the window had focus all that time. The engine decides by the policy of the wrong-answer trigger; an offer
        arrives as a `card.open` on the session's stream, from an engine-written turn with no model call."""
        self._guard()
        idle = require("StepIdle", body)
        entry = self.entry(session_id)
        if entry.session_id in self.voice_engines or (entry.task is not None and not entry.task.done()):
            raise ApiError("turn_active", "a turn is running in this session", details={"turn_id": entry.active_turn})
        try:
            self.assessment_guard()
        except ApiError:
            return  # never during an assessment or a mock sitting (LRN-20: no offer in timed modes)
        engine = self._engine(entry)
        decision = offers.idle_decision(engine, idle["idle_ms"] / 1000, idle["focused"])
        if decision is None:
            return
        ids = TurnIds()
        entry.active_turn = ids.turn_id
        entry.task = asyncio.get_running_loop().create_task(self._run(entry, engine.idle_offer(decision, ids),
                                                                      entry.engine_state.solved))  # fmt: skip

    def sim_prediction(self, session_id: str, visual_id: str, body: dict[str, Any]) -> None:
        """VSL-47 in a session: the learner's prediction for a predict-first simulation on screen (or their
        explanation of its result) becomes a short tutor turn on the session's stream, which points at the result. The
        engine decides whether the prediction matched; a visual not on screen or not predicting is refused."""
        from agent_core.engine.predictions import PredictionRefused, scenario_on_screen

        self._guard()
        asked = require("SimPrediction", body)
        entry = self.entry(session_id)
        if entry.session_id in self.voice_engines or (entry.task is not None and not entry.task.done()):
            raise ApiError("turn_active", "a turn is running in this session", details={"turn_id": entry.active_turn})
        self.assessment_guard()
        engine = self._engine(entry)
        try:
            scenario_on_screen(engine, visual_id, str(asked["scenario_id"]))
            if asked["event"] == "explained" and visual_id not in engine.sim_predictions:
                raise PredictionRefused("not_predicted")
        except PredictionRefused as error:
            code = "not_found" if error.reason == "not_found" else "validation_failed"
            raise ApiError(code, error.reason, details={"reason": error.reason}) from None
        ids = TurnIds(client_message_id=str(asked["client_event_id"]))
        entry.active_turn = ids.turn_id
        entry.task = asyncio.get_running_loop().create_task(
            self._run(entry, engine.sim_prediction(visual_id, dict(asked), ids), entry.engine_state.solved)
        )

    async def cancel(self, turn_id: str) -> dict[str, Any]:
        entry = next((e for e in self.mine() if e.active_turn == turn_id), None)
        if entry is not None and entry.session_id in self.voice_engines and self.voice_cancel is not None:
            return {"result": "cancelled" if await self.voice_cancel(turn_id) else "inactive"}
        if entry is None or entry.task is None or entry.task.done():
            return {"result": "inactive"}
        assert entry.engine is not None
        entry.engine.cancel()
        done, _ = await asyncio.wait([entry.task], timeout=self.settings.api_cancel_budget_ms / 1000)
        return {"result": "cancelled" if done else "requested"}

    # --- streams ---------------------------------------------------------------------------------------------------

    def open_stream(self, session_id: str, cursor_text: str | None) -> tuple[Connection, int, int | None]:
        """Register a connection at a cursor; returns it with the replayed count. Raises 409 when the cursor cannot
        resume (API-17): another epoch, or events no longer in the ring."""
        entry = self.entry(session_id)
        try:
            cursor = parse_cursor(cursor_text)
        except ValueError:
            raise ApiError("invalid_cursor", "cursor must be <epoch>:<seq>") from None
        if entry.engine is not None:
            entry.engine.events.flush()
        epoch, seq = cursor if cursor is not None else (self.epoch, 0)
        if epoch != self.epoch:
            raise ApiError("epoch_mismatch", "the core restarted; reload the snapshot",
                           details={"epoch": self.epoch})  # fmt: skip
        current = entry.state["cursor"]["seq"]
        if seq == current:
            replay: list[bytes] | None = []
        else:
            replay = self.pool.ring(session_id).after(seq) if session_id in self.pool.rings else None
        if replay is None:
            raise ApiError("cursor_expired", "events after this cursor are no longer kept; reload the snapshot")
        connection = Connection(self.settings.api_queue_events, int(self.settings.api_queue_mb * 1024 * 1024))
        connection.put_frames(replay)
        entry.connections.add(connection)
        return connection, len(replay), seq if cursor is not None else None

    async def stream(self, session_id: str, connection: Connection, replayed: int,
                     resumed_from: int | None) -> AsyncIterator[bytes]:  # fmt: skip
        entry = self.entry(session_id)
        try:
            yield control({"type": "stream.hello", "protocol": PROTOCOL, "server_version": SERVER_VERSION,
                           "epoch": self.epoch, "channel": "session", "session_id": session_id,
                           "resumed_from": resumed_from, "replayed": replayed})  # fmt: skip
            async for data in connection.frames(self.settings.api_heartbeat_s):
                yield data
        finally:
            entry.connections.discard(connection)


def _block_text(block: dict[str, Any]) -> str:
    kind = block["type"]
    if kind == "text":
        return str(block["text"])
    if kind in ("latex", "board_line"):
        return str(block["latex"])
    if kind == "ledger_table":
        return str(block["text_fallback"])
    return ""
