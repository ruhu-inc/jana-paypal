"""Assembling an engine from the shipped files: registry, router configuration, prompt files, skills, templates and
schemas. The app (M5), the evaluation harness and the tests all build engines here, so evaluation runs the code the
app ships (EVL-01: no eval-only code paths)."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime
import itertools
import json
import logging
import os
import sqlite3
import time
import weakref
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import yaml

from agent_core.assets.agent import AgentAssets
from agent_core.assets.bundle import BUNDLE_FILE, BundleRefused, rollback_bundle, select_bundle
from agent_core.assets.skills import SkillSet
from agent_core.config import Settings
from agent_core.config.environments import Environment
from agent_core.config.operations import DevicePolicy
from agent_core.config.revocation import HELD as HELD_REVOCATIONS
from agent_core.config.revocation import RevocationStore
from agent_core.config.trust import TrustStore
from agent_core.engine import adaptation, health, job_scope, soft_rules
from agent_core.engine.assessment.evidence import assessment_projections
from agent_core.engine.assessment.remark import RemarkReport, job_id, remark_withdrawn
from agent_core.engine.assessment.service import AssessmentDesk
from agent_core.engine.auxiliary import Auxiliaries
from agent_core.engine.events import EventStream, new_id
from agent_core.engine.input import normalise_input
from agent_core.engine.knows import KnowsDesk
from agent_core.engine.learn import LearnDesk
from agent_core.engine.memory_session import MemoryAccess, MemorySource
from agent_core.engine.operations import install_operations, presented_content
from agent_core.engine.pedagogy import Templates
from agent_core.engine.plans import PlanDesk
from agent_core.engine.records import SessionRecords
from agent_core.engine.reflection import ReflectorModel, ReflectorQueue
from agent_core.engine.runtime_link import RuntimeLink, app_version, read_variant
from agent_core.engine.safety import InputGuard, SafetyResources
from agent_core.engine.signatures import pack_context
from agent_core.engine.soft_rules import RuleMatcher
from agent_core.engine.state import SessionState
from agent_core.engine.turn import Engine, EngineDeps
from agent_core.engine.tutor_flows import SavedQuestions, record_wellbeing_shown
from agent_core.learner.mastery import DAY_MS, MasteryParams
from agent_core.learner.remark import take_notice
from agent_core.learner.service import Learner
from agent_core.memory.capture import CAPTURE_FILE, CaptureFilter
from agent_core.retrieval.index import KnowledgeIndex
from agent_core.router import LlamaServerProvider, Router, Shared, hardware, power
from agent_core.router.config import load as load_router
from agent_core.router.online import OnlineLayer, cloud_session
from agent_core.router.power import PowerPlan
from agent_core.router.probe import Probe, probe_machine
from agent_core.router.voice import CloudVoice, VoiceLaunch, VoiceRoute, memory_budget
from agent_core.store import subscription as subscriptions
from agent_core.store.checkpoints import CheckpointStore
from agent_core.store.egress import DeviceSpend, forget_first_use
from agent_core.store.integrity import Recovery, disk_low, full_check, open_checked
from agent_core.store.keystore import KeyStore, MemoryKeyStore
from agent_core.store.local import LocalStores, open_local
from agent_core.store.pack_index import PackIndex
from agent_core.store.pack_store import PackStore
from agent_core.store.packs import list_packs
from agent_core.store.perception_packs import PerceptionPackStore
from agent_core.store.profiles import ProfileDirectory, ProfileError
from agent_core.store.sqlite import record_migrations
from agent_core.store.transcript import TranscriptStore
from agent_core.tools.executor import ToolCall, ToolExecutor
from agent_core.tools.generated.register import REGISTER
from agent_core.tools.perception import PerceptionClient
from agent_core.tools.spec import SessionCtx
from agent_core.tools.specs import registry
from agent_core.tools.spill import SpillStore
from agent_core.tools.study_document import main as _study_document_main
from agent_core.tools.worker import WorkerSupervisor, landlock, winsandbox
from agent_core.tools.worker.launch import process_factory
from agent_core.trace.store import TraceStore

CORE = Path(__file__).resolve().parents[1]
DEV_SKILL_STATUSES = frozenset({"draft", "in_review", "reviewed"})
RELEASE_SKILL_STATUSES = frozenset({"reviewed"})

# The frozen core doubles as the Windows worker launcher (SEC-06, G16); the API entry dispatches the flag here.
WORKER_SANDBOX_FLAG = winsandbox.FROZEN_FLAG
worker_sandbox_main = winsandbox.main
WORKER_LANDLOCK_FLAG = landlock.FROZEN_FLAG
worker_landlock_main = landlock.main


def study_document_main() -> None:
    _study_document_main()


def agent_roots(content_root: Path, skills_root: Path, release: bool) -> tuple[Path, Path]:
    """The agent content and skills to run from. When the shell names a signed agent bundle (`bundle/<version>/` with
    its `bundle.json`), the newest installed bundle up to that version that verifies (SEC-14): an edited bundle falls
    back to the previous verified one, and with none the core does not start. Source trees are development-only."""
    if not (skills_root / BUNDLE_FILE).is_file():
        if release:
            raise BundleRefused("release builds require a signed agent bundle")
        return content_root, skills_root
    environment = Environment.PRODUCTION if release else Environment.DEV
    selected = select_bundle(skills_root.parent, environment, at_most=skills_root.name)
    return selected.agent_root, selected.skills_root


REJECTIONS_KEY = "router_feature_rejections"  # device.db app_settings (MDL-04)
REMARKS_KEY = "remark_corrections"  # device.db app_settings: the corrections re-marked here, by job id (CUR-36)
REMARK_FAILURES_KEY = "remark_failures"  # device.db app_settings: corrections whose re-mark failed, by job id
log = logging.getLogger(__name__)
_LESSON_CALLS = itertools.count(1)  # call ids for lesson checks (UXS-23)


def misconception_evidence(conn: sqlite3.Connection, misconception_id: str) -> int:
    """The learner model's matching events for a misconception so far (PED-22); 0 when it has none."""
    row = conn.execute("SELECT evidence_count FROM learner_misconceptions WHERE misconception_id = ?",
                       (misconception_id,)).fetchone()  # fmt: skip
    return int(row[0]) if row else 0


def stuck_cap(conn: sqlite3.Connection, kc_ids: list[str], settings: Settings) -> bool:
    """The stuck cap (LRN-26): a KC in play reached the cap (the configured opportunities or sessions without its
    mastery reaching the bar), so the mastery projection moved it on and its revisit date has not come."""
    now = int(time.time() * 1000)
    for kc_id in kc_ids:
        row = conn.execute("SELECT moved_on_at FROM kc_mastery WHERE kc_id = ?", (kc_id,)).fetchone()
        if row and row[0] is not None and now < int(row[0]) + int(settings.stuck_revisit_days * DAY_MS):
            return True
    return False


def self_contained(schema: dict[str, Any], documents: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    """The schema with every reference to another contract document copied into its own `$defs`, so a decoder that
    resolves only local references (a provider's structured output, llama.cpp's grammar) sees the whole contract.
    Copied definitions are named `<document>__<definition>`; their own references are resolved the same way."""
    base = str(schema.get("$id", ""))
    imported: dict[str, str] = {}
    extra: dict[str, Any] = {}

    def target(document: str, pointer: str) -> Any:
        node: Any = documents[document]
        for part in [p for p in pointer.split("/") if p]:
            node = node[part.replace("~1", "/").replace("~0", "~")]
        return node

    def rewrite(node: Any, document: str) -> Any:
        if isinstance(node, list):
            return [rewrite(item, document) for item in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key != "$ref" or not isinstance(value, str):
                out[key] = rewrite(value, document)
                continue
            source, _, pointer = value.partition("#")
            source = urljoin(document, source) if source else document  # relative to the document it sits in
            if source == base:
                out[key] = "#" + pointer
                continue
            wanted = f"{source}#{pointer}"
            if wanted not in imported:
                stem = source.rsplit("/contracts/", 1)[-1].replace("/", "_").replace("-", "_")
                imported[wanted] = name = f"{stem}__{pointer.rsplit('/', 1)[-1] or 'root'}"
                copied = json.loads(json.dumps(target(source, pointer)))
                for drop in ("$id", "$schema", "$defs"):  # a copied root's own definitions come in one by one
                    copied.pop(drop, None)
                extra[name] = {}
                extra[name] = rewrite(copied, source)
            out[key] = "#/$defs/" + imported[wanted]
        return out

    result: dict[str, Any] = rewrite(schema, base)
    if extra:
        result["$defs"] = {**result.get("$defs", {}), **extra}
    # The copy is its own document: without the contract's `$id`, its local references resolve within it, even where a
    # validator built on the contract registry (which holds the original document) checks it.
    result.pop("$id", None)
    return result


def bundled_schemas(root: Path = CORE / "contracts" / "schemas") -> dict[str, dict[str, Any]]:
    """Self-contained output schemas for decoding, by contract name (`agents/tutor-turn`): references to other contract
    documents are copied in (`self_contained`), since no decoder fetches them. The tutor turn's optional
    `sketch_wanted` (VSL-43, D106) is left out of the decoding schema, so no local model's grammar or prompt carries it
    (VSL-01); an online candidate's call gets it back through the engine's prepare step (MDL-33, sketch_flow.py)."""
    raw = {
        f"{p.parent.name}/{p.name.removesuffix('.schema.json')}": json.loads(p.read_text())
        for p in sorted(root.glob("*/*.schema.json"))
    }
    every = (json.loads(p.read_text()) for p in sorted(root.rglob("*.schema.json")))
    documents = {str(s["$id"]): s for s in every if "$id" in s}
    schemas = {name: self_contained(schema, documents) for name, schema in raw.items()}
    turn = schemas.get("agents/tutor-turn")
    if turn is not None:
        turn["properties"].pop("sketch_wanted", None)
    return schemas


def assemble(
    providers: dict[str, Any],
    settings: Settings,
    state: SessionState,
    content_root: Path,
    skills_root: Path,
    spill_root: Path,
    supervisor: WorkerSupervisor | None = None,
    release: bool = False,
    router_overrides: Callable[[dict[str, Any]], None] | None = None,
    sleep: Callable[[float], Any] | None = None,
    session_date: str = "2026-01-01",
    disabled_gates: frozenset[str] = frozenset(),
    trace_sink: Callable[[dict[str, Any]], None] | None = None,
    runtime_restarted: Callable[[], Awaitable[bool]] | None = None,
    router_shared: Shared | None = None,
    device_class: str | None = None,
    cloud_on: Callable[[str], bool] | None = None,
    power: Callable[[], PowerPlan | None] | None = None,
    visual_sources: Callable[[str], dict[str, Any]] | None = None,
    misconception_context: Callable[[dict[str, Any]], Any] | None = None,
    tutor_name: Callable[[], str | None] | None = None,
) -> Engine:
    """`device_class` is the device's class when the session's own is not a local one (content-only below the
    minimum laptop, PLT-29); `cloud_on` is the adult's online-tier switch the router reads (MDL-24)."""
    reg = registry()
    executor = ToolExecutor(reg, settings, SpillStore(spill_root), supervisor)
    data = yaml.safe_load((CORE / "router" / "router.yaml").read_text())
    if router_overrides:
        router_overrides(data)
    kwargs: dict[str, Any] = {"sleep": sleep} if sleep else {}
    if router_shared is not None:  # one local queue and one set of learned rejections per device (MDL-04, MDL-22)
        kwargs["shared"] = router_shared
    if cloud_on is not None:
        kwargs["cloud_on"] = cloud_on
    if power is not None:
        kwargs["power"] = power
    hardware = state.hardware_class if state.hardware_class in ("low", "mid", "high") else device_class
    router = Router(load_router(data), settings, providers, hardware_class=hardware, **kwargs)
    assets = AgentAssets.load(content_root, skills_root, RELEASE_SKILL_STATUSES if release else DEV_SKILL_STATUSES)
    templates = Templates.load(content_root / "templates" / "en.yaml")
    # Only verified helplines fill the distress template (PRV-14); the pack is country configuration (D2).
    templates = templates.with_verified(SafetyResources.load(content_root / "safety" / "resources.yaml").values())
    deps = EngineDeps(
        router,
        executor,
        reg,
        assets,
        templates,
        settings,
        REGISTER["roleGrants"],
        bundled_schemas(),
        session_date=session_date,
        disabled_gates=disabled_gates,
        trace_sink=trace_sink,
        runtime_restarted=runtime_restarted,
        visual_sources=visual_sources,
        misconception_context=misconception_context,
        tutor_name=tutor_name,
    )
    events = EventStream(state.session_id, "epoch-" + new_id("ep")[3:15].lower())
    engine = Engine(deps, state, events)
    rules = content_root / "rules"
    # The shipped soft rules, matched each Coach turn (PED-15). From S2 the rule matcher role judges the candidates on
    # its auxiliary slot (AGT-02); without it, on the low class or when its call fails, the rules' keywords decide.
    if rules.is_dir():
        judge = engine.auxiliary.judge_rules if deps.stage >= "S2" else None
        engine.rules = RuleMatcher(soft_rules.load(rules), settings, judge=judge)
    return engine


@dataclass
class AppRuntime:
    """What the running app shares across its sessions: settings, file roots, the worker pool and the model providers
    (M5). The local runtime provider arrives over the control channel once the shell has started llama-server."""

    settings: Settings
    content_root: Path
    skills_root: Path
    data_root: Path
    supervisor: WorkerSupervisor | None = None
    providers: dict[str, Any] = field(default_factory=dict)
    release: bool = False
    model: str | None = None  # pin every tier to this local model, when the shell names one
    traces: TraceStore | None = None  # OBS-08: every session's committed turn records
    stores: LocalStores | None = None  # DAT-01: device.db and the local profile's learner.db
    app_version: str = "0.0.0"
    release_channel: Callable[[], str] = lambda: "stable"
    implicit: tuple[str, Path] = ("", Path())  # the no-profile learner store of S1 (DAT-14)
    recoveries: list[Recovery] = field(default_factory=list)  # damaged learner databases recovered (DAT-13)
    integrity_failures: list[str] = field(default_factory=list)  # files the weekly full check found damaged
    keys: KeyStore | None = None  # SEC-17: data keys by reference, outside the data directory (shell keychain)
    directory: ProfileDirectory | None = None  # D9: the profile directory on device.db
    link: RuntimeLink | None = None  # the runtime the shell supervises (MDL-19); none with a scripted provider
    plan: dict[str, Any] | None = None  # the launch plan this core wrote (health panel, GET /v1/models)
    probe: Probe | None = None  # this machine, read at bring-up (MDL-16): the hardware class and its record
    epoch: str = ""  # this core process's epoch: the shell's runtime-build file is read for it (PLT-04)
    # PLT-25 (M19): whether a paired school server answers on the LAN; none can until the LAN client exists.
    school_server_reachable: Callable[[], bool] = lambda: False
    measuring: asyncio.Task[Any] | None = None  # the PLT-08 benchmark, once the runtime is ready
    read_battery: Callable[[], power.Battery | None] = power.read_battery  # PLT-22
    _battery: tuple[float, power.Battery | None] | None = field(default=None, init=False, repr=False)
    _speed: tuple[tuple[int, int, str | None], str | None, bool] | None = field(default=None, init=False, repr=False)
    engines: weakref.WeakSet[Engine] = field(default_factory=weakref.WeakSet)  # open sessions' engines, to re-pin
    router_shared: Shared = field(default_factory=Shared)  # MDL-04, MDL-22: shared by every session's router
    learner: Learner | None = None  # the open learner database's writer (DAT-14: one profile at a time)
    online: OnlineLayer | None = None  # the connectivity layer (ARC-06), attached by the app from S2
    _installed: tuple[tuple[Any, ...], tuple[Any, ...], list[Path], list[tuple[str, str]]] | None = field(
        default=None, init=False, repr=False)  # fmt: skip
    _content: tuple[tuple[Any, ...], PackIndex] | None = field(default=None, init=False, repr=False)
    _operations: DevicePolicy | None = field(default=None, init=False, repr=False)
    _knowledge: tuple[tuple[Any, ...], KnowledgeIndex] | None = field(default=None, init=False, repr=False)
    _capture: CaptureFilter | None = field(default=None, init=False, repr=False)
    _capture_loaded: bool = field(default=False, init=False, repr=False)
    _reflector: ReflectorQueue | None = field(default=None, init=False, repr=False)
    _router_data: dict[str, Any] | None = field(default=None, init=False, repr=False)
    # Lesson positions kept in memory per profile while the learner database has no table for them (UXS-23).
    _assessment_desk: AssessmentDesk | None = field(default=None, init=False, repr=False)
    # CUR-36, D98: the installed corrections by content index, and the background re-mark of the waiting ones. The
    # app sets `background_held` so a re-mark waits while a turn runs or a mock sitting is open (ARC-32).
    background_held: Callable[[], bool] = lambda: False
    remark_clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)  # the re-mark back-off's clock
    _apply_pending: bool = field(default=False, init=False, repr=False)
    _corrections: tuple[PackIndex, tuple[tuple[str, str], ...]] | None = field(default=None, init=False, repr=False)
    _sweep: asyncio.Task[list[RemarkReport]] | None = field(default=None, init=False, repr=False)
    _sweep_again: bool = field(default=False, init=False, repr=False)
    _remark_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)  # one re-mark at a time
    _plan_desk: PlanDesk | None = field(default=None, init=False, repr=False)
    _lesson_positions: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict, init=False, repr=False)
    # The learner's map per profile for the life of the core (SPEC §21): the visit's baseline and the dated replays.
    _map_states: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _lesson_tools: ToolExecutor | None = field(default=None, init=False, repr=False)

    async def study_perception(self, data: bytes) -> dict[str, Any]:
        command = await asyncio.to_thread(self.perception_command)
        return await PerceptionClient(command, settings=self.settings).run(data)

    def perception_command(self) -> tuple[str, ...] | None:
        environment = Environment.PRODUCTION if self.release else Environment.DEV
        store = PerceptionPackStore(
            self.data_root / "packs" / "perception", environment, self.app_version, self.revocations().trust
        )
        return store.command(self.hardware_class())

    def key_store(self) -> KeyStore:
        """The device secrets (SEC-17, D97): the shell's keychain bridge. A release core never falls back to a store
        held only in memory; a development build without the shell's keys gets one, for this run only."""
        if self.keys is None:
            if self.release:
                raise RuntimeError("SEC-17: a release core needs the shell's keys (control.keys)")
            self.keys = MemoryKeyStore()
        return self.keys

    def open_stores(self, app_version: str) -> SessionRecords:
        """Open the device and learner databases and the trace store; return what sessions record into."""
        settings = self.settings
        now = int(time.time() * 1000)
        self.app_version = app_version
        self.stores = open_local(self.data_root, app_version=app_version, now_ms=now)
        self.directory = ProfileDirectory(self.stores.device, self.data_root, settings, self.key_store(),
                                          app_version=app_version)  # fmt: skip
        self.traces = TraceStore(self.data_root / "traces" / "traces.db", max_bytes=settings.trace_store_max_bytes,
                                 retention_days=settings.trace_retention_days)  # fmt: skip
        self.implicit = (self.stores.profile_id, self.stores.learner_path)
        self._keep_rejections()
        if self.stores.device_recovery is not None:
            self.recoveries.append(self.stores.device_recovery)  # the adult is told; learning goes on (UXS-47)
        if self.stores.recovery is not None:
            self.recoveries.append(self.stores.recovery)
            if self.stores.recovery.needs_rebuild:
                self.rebuild_projections(self.stores.learner, self.stores.profile_id)
        return self._records()

    def _keep_rejections(self) -> None:
        """The router's learned feature rejections live in device.db, so they hold for the install (MDL-04)."""
        assert self.stores is not None
        device = self.stores.device
        held = device.execute("SELECT value FROM app_settings WHERE key = ?", (REJECTIONS_KEY,)).fetchone()
        rejections = self.router_shared.rejections
        if held is not None:
            rejections.fixes = json.loads(held[0])

        def save(fixes: dict[str, dict[str, str | None]]) -> None:
            device.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET"
                " value = excluded.value, updated_at = excluded.updated_at",
                (REJECTIONS_KEY, json.dumps(fixes), int(time.time() * 1000)),
            )

        rejections.on_change = save

    def activate_profile(self, profile_id: str | None) -> SessionRecords:
        """Exactly one learner database is open (DAT-14): close the previous one and open this profile's, or the
        implicit profile's of a device without profiles (None)."""
        assert self.stores is not None and self.directory is not None
        internal, path = (profile_id[4:], self.directory.learner_path(profile_id)) if profile_id else self.implicit
        if internal != self.stores.profile_id:
            if self._assessment_desk is not None:
                self._assessment_desk.interrupt_all()
                self._assessment_desk = None
            self._plan_desk = None
            path.parent.mkdir(parents=True, exist_ok=True)
            conn, applied, recovery = open_checked(path, "learner")  # a refusal leaves the previous one open
            self.stores.learner.close()
            if recovery is not None:
                self.recoveries.append(recovery)
            record_migrations(self.stores.device, "learner", str(path.relative_to(self.data_root)), applied,
                              self.app_version, int(time.time() * 1000))  # fmt: skip
            self.stores = dataclasses.replace(self.stores, learner=conn, profile_id=internal, learner_path=path,
                                              recovery=recovery)  # fmt: skip
        if self.stores.recovery is not None and self.stores.recovery.needs_rebuild:
            self.rebuild_projections(self.stores.learner, self.stores.profile_id)  # from the events that survived
            self.stores = dataclasses.replace(self.stores, recovery=None)
        return self._records()

    def retention_pass(self, now_ms: int | None = None) -> None:
        """The DAT-08 retention job over every profile's learner database and the device database."""
        from agent_core.store.retention import run_retention

        assert self.stores is not None and self.directory is not None
        now = int(time.time() * 1000) if now_ms is None else now_ms
        device = self.stores.device
        checked = device.execute("SELECT value FROM app_settings WHERE key = 'integrity_checked_at'").fetchone()
        week = self.settings.integrity_full_check_days * 86_400_000
        full = checked is None or now - int(json.loads(checked[0])) >= week
        targets = [self.implicit] + [(p["profile_id"][4:], self.directory.learner_path(p["profile_id"]))
                                     for p in self.directory.summaries()["profiles"]]  # fmt: skip
        for internal, path in targets:
            if not path.is_file():
                continue
            conn, _, _ = open_checked(path, "learner")
            try:
                if full and not full_check(conn):
                    self.integrity_failures.append(str(path.relative_to(self.data_root)))  # the weekly check (DAT-13)
                learner = Learner.open(conn, profile_id=internal, device_id=self.stores.device_id,
                                       wall_ms=lambda: int(time.time() * 1000),
                                       max_skew_ms=self.settings.sync_clock_skew_max_ms,
                                       params=self.mastery_params(),
                                       content=self.content(),
                                       projections=assessment_projections(self.settings))  # fmt: skip
                run_retention(conn, self.stores.device, self.settings, now_ms=now, profile_dir=path.parent,
                              log=learner.log, fold=learner.projector.apply,
                              prune_traces=self.traces.prune if self.traces else None)  # fmt: skip
            finally:
                conn.close()
        if full:
            device.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES ('integrity_checked_at', ?, ?) ON CONFLICT"
                " (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (json.dumps(now), now),
            )

    async def remark_withdrawn(self, old_item_id: str, new_item_id: str) -> list[RemarkReport]:
        """CUR-36, OPS-27, ASM-23: re-mark the attempts at a withdrawn item against its installed correction in every
        learner database on this device, each rebuilt from its own log. Local only; nothing leaves the device. The
        item must already be withdrawn here (its safety configuration entry applied), so a re-mark never runs for a
        record still in use."""
        assert self.stores is not None and self.directory is not None
        self.operation_boundary()
        if self.operation_policy().decision("item", old_item_id).action != "withdraw":
            raise ValueError(f"CUR-36: {old_item_id} is not withdrawn on this device")
        async with self._remark_lock:
            reports = await self._remark_everywhere(old_item_id, new_item_id)
            self._remarked(old_item_id, new_item_id, reports)
        if any(r.changed_results for r in reports):
            self.tell_remarks()
        return reports

    async def _remark_everywhere(self, old_item_id: str, new_item_id: str) -> list[RemarkReport]:
        assert self.stores is not None and self.directory is not None
        targets = [self.implicit] + [(p["profile_id"][4:], self.directory.learner_path(p["profile_id"]))
                                     for p in self.directory.summaries()["profiles"]]  # fmt: skip
        reports = []
        for internal, path in targets:
            if not path.is_file():
                continue
            conn, _, _ = open_checked(path, "learner")
            try:
                learner = Learner.open(conn, profile_id=internal, device_id=self.stores.device_id,
                                       wall_ms=lambda: int(time.time() * 1000),
                                       max_skew_ms=self.settings.sync_clock_skew_max_ms,
                                       params=MasteryParams.from_settings(self.settings),
                                       content=self.content(),
                                       projections=assessment_projections(self.settings))  # fmt: skip
                learner.projector.ensure_current()
                desk = AssessmentDesk(learner, self.content(), self.settings, run_tool=self._lesson_tool)
                reports.append(await remark_withdrawn(desk, old_item_id, new_item_id))
            finally:
                conn.close()
        return reports

    def _remarks_done(self) -> dict[str, Any]:
        assert self.stores is not None
        row = self.stores.device.execute("SELECT value FROM app_settings WHERE key = ?", (REMARKS_KEY,)).fetchone()
        done = json.loads(row[0]) if row is not None else {}
        return done if isinstance(done, dict) else {}

    def _remarked(self, old_item_id: str, new_item_id: str, reports: Sequence[RemarkReport]) -> None:
        """Records a correction re-marked on every learner database, so it is never run again (CUR-36), and clears
        any failures it had."""
        assert self.stores is not None
        now = int(time.time() * 1000)
        job = job_id(old_item_id, new_item_id)
        done = {**self._remarks_done(), job: {
            "old_item_id": old_item_id, "new_item_id": new_item_id, "at": now,
            "changed_results": sum(r.changed_results for r in reports)}}  # fmt: skip
        self._put_setting(REMARKS_KEY, done, now)
        failures = self.remark_failures()
        if failures.pop(job, None) is not None:
            self._put_setting(REMARK_FAILURES_KEY, failures, now)

    def _put_setting(self, key: str, value: Any, now: int) -> None:
        assert self.stores is not None
        self.stores.device.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET"
            " value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value, sort_keys=True), now),
        )

    def remark_failures(self) -> dict[str, Any]:
        """The corrections whose re-mark failed and has not succeeded since, by job id: how many times in a row, the
        last error's type, and when it may be tried again (CUR-36)."""
        assert self.stores is not None
        row = self.stores.device.execute(
            "SELECT value FROM app_settings WHERE key = ?", (REMARK_FAILURES_KEY,)
        ).fetchone()
        failures = json.loads(row[0]) if row is not None else {}
        return failures if isinstance(failures, dict) else {}

    def _remark_failed(self, old_item_id: str, new_item_id: str, error: BaseException) -> None:
        """A re-mark that failed backs off: it is tried again after `remark_retry_first_delay_s`, doubling with each
        failure in a row up to `remark_retry_max_delay_s`, never at every start or turn boundary (CUR-36)."""
        now = self.remark_clock_ms()
        job = job_id(old_item_id, new_item_id)
        failures = self.remark_failures()
        count = int((failures.get(job) or {}).get("failures", 0)) + 1
        settings = self.settings
        delay_s = min(settings.remark_retry_first_delay_s * 2 ** (count - 1), settings.remark_retry_max_delay_s)
        failures[job] = {"old_item_id": old_item_id, "new_item_id": new_item_id, "failures": count,
                         "error": type(error).__name__, "failed_at": now,
                         "retry_at": now + int(delay_s * 1000)}  # fmt: skip
        self._put_setting(REMARK_FAILURES_KEY, failures, now)

    def remark_failing(self) -> bool:
        """Health (OBS-15): a correction's re-mark has failed `remark_failures_reported` times in a row."""
        if self.stores is None:
            return False
        try:
            failures = self.remark_failures()
        except sqlite3.Error:
            return False
        threshold = self.settings.remark_failures_reported
        return any(isinstance(f, dict) and int(f.get("failures", 0)) >= threshold for f in failures.values())

    def corrections(self) -> list[tuple[str, str]]:
        """CUR-36, D98: the (withdrawn item, correcting item) pairs waiting for a re-mark here: an installed item
        whose `corrects` names an item this device has withdrawn (its safety configuration applied), not re-marked
        yet. Whichever arrives second, the correcting pack or the withdrawal, makes a pair wait."""
        if self.stores is None:
            return []
        content = self.content()
        if self._corrections is None or self._corrections[0] is not content:
            pairs = tuple((str(i["corrects"]), str(i["id"])) for i in content.table("items")
                          if isinstance(i.get("corrects"), str) and i["corrects"] != i["id"])  # fmt: skip
            self._corrections = (content, pairs)
        if not self._corrections[1]:
            return []
        decide, done = self.operation_policy().frozen_decider(), self._remarks_done()
        now, failures = self.remark_clock_ms(), self.remark_failures()

        def backing_off(old: str, new: str) -> bool:
            failed = failures.get(job_id(old, new))
            return isinstance(failed, dict) and int(failed.get("retry_at", 0)) > now

        return [(old, new) for old, new in self._corrections[1] if job_id(old, new) not in done
                and not backing_off(old, new)
                and decide("item", old, None).action == "withdraw"
                and decide("item", new, None).action != "withdraw"]  # fmt: skip

    def safety_pending(self) -> bool:
        """Whether a safety configuration adopted since the last turn boundary waits to apply (OPS-10)."""
        summary = self.operation_policy().summary()
        return int(summary["pending_sequence"]) > int(summary["sequence"])

    def schedule_corrections(self, apply_pending: bool = False) -> asyncio.Task[list[RemarkReport]] | None:
        """Start the waiting re-marks in the background, off the request path (CUR-36, D98): called at start, at
        every turn boundary, where a withdrawal is applied, and when content or a safety configuration arrives (a
        pack, a USB kit, the update desk: `apply_pending`). Content that arrives with its withdrawal is re-marked
        promptly: the sweep applies the waiting safety configuration as soon as no turn or mock sitting runs (the
        turn boundary OPS-10 asks for), rather than at the next turn. Cheap when nothing waits; a no-op outside the
        event loop. A call while a sweep runs makes it look again when it ends."""
        try:
            loop = asyncio.get_running_loop()
            pending = apply_pending and self.stores is not None and self.safety_pending()
            waiting = pending or bool(self.corrections())
        except RuntimeError:
            return None
        self._apply_pending = self._apply_pending or pending
        if self._sweep is not None and not self._sweep.done():
            self._sweep_again = self._sweep_again or waiting
            return self._sweep
        if not waiting:
            return None
        self._sweep = loop.create_task(self._sweep_corrections())
        return self._sweep

    async def _sweep_corrections(self) -> list[RemarkReport]:
        reports: list[RemarkReport] = []
        while True:
            self._sweep_again = False
            while self.background_held():  # ARC-32: never during a turn or a mock sitting
                await asyncio.sleep(self.settings.background_cancel_budget_s)
            if self._apply_pending:  # no turn runs: this is a turn boundary for the waiting safety configuration
                self._apply_pending = False
                self.operation_boundary()
            async with self._remark_lock:  # the list is read under the lock: a pair re-marked meanwhile is done
                for old, new in self.corrections():
                    try:
                        found = await self._remark_everywhere(old, new)
                    except Exception as error:  # backs off, and is named in health when it keeps failing
                        log.warning("CUR-36: re-mark of %s failed (%s)", old, type(error).__name__)
                        self._remark_failed(old, new, error)
                        continue
                    self._remarked(old, new, found)
                    reports += found
            if not self._sweep_again:
                break
        if any(r.changed_results for r in reports):
            self.tell_remarks()
        return reports

    def tell_remarks(self) -> bool:
        """ASM-23: an open session of the open profile shows the re-mark notice at once, in its latest turn; with
        none open, the next turn of that profile shows it."""
        ordered = sorted(list(self.engines), key=lambda e: e.turn_started() or 0.0, reverse=True)
        return any(engine.deliver_remark_notice() for engine in ordered)

    def checkpoint_store(self) -> CheckpointStore:
        """The open learner database's session checkpoints (DAT-07): sessions resume from here after a crash."""
        assert self.stores is not None
        return CheckpointStore(self.stores.learner, keep=self.settings.retention_checkpoints_per_session)

    def rebuild_projections(self, conn: Any, profile_internal: str) -> None:
        """Fold a learner database's events into its projections from the start (archive import, DAT-10)."""
        assert self.stores is not None
        learner = Learner.open(conn, profile_id=profile_internal, device_id=self.stores.device_id,
                               wall_ms=lambda: int(time.time() * 1000),
                               max_skew_ms=self.settings.sync_clock_skew_max_ms,
                               params=self.mastery_params(),
                               content=self.content(),
                                       projections=assessment_projections(self.settings))  # fmt: skip
        learner.projector.rebuild()

    def _records(self) -> SessionRecords:
        assert self.stores is not None
        settings = self.settings
        learner = Learner.open(
            self.stores.learner,
            profile_id=self.stores.profile_id,
            device_id=self.stores.device_id,
            wall_ms=lambda: int(time.time() * 1000),
            max_skew_ms=settings.sync_clock_skew_max_ms,
            params=self.mastery_params(),
            content=self.content(),
            projections=assessment_projections(settings),
        )
        # A new projection version or parameter set rebuilds (DAT-05). The installed packs are part of the parameters
        # (their difficulties and prerequisite graph, LRN-02), so the first open with this pack set rebuilds once, in
        # one transaction, before any session reads the projections; later opens find them current.
        learner.projector.ensure_current()
        self.learner = learner
        idle_ms = int(settings.session_idle_timeout_min * 60_000)
        return SessionRecords(TranscriptStore(self.stores.learner), learner, idle_timeout_ms=idle_ms)

    # --- memory (MEM-03, TOL-23, AGT-26) ------------------------------------------------------------------

    def revocations(self) -> RevocationStore:
        """The device's revocation list (SEC-16): its `trust` is read at each use, so a list adopted from any source
        applies at once to every pack and model this runtime reads."""
        environment = Environment.PRODUCTION if self.release else Environment.DEV
        return RevocationStore(self.data_root, environment)

    def _installed_packs(self) -> tuple[tuple[Any, ...], list[Path], list[tuple[str, str]]]:
        """The content packs the pack store installed (`installed.json`), each loaded and verified with the live
        trust, and a cheap key that changes when the pack files, the installed record or the revocation list do. A
        `.pack` file dropped in the folder without an install is never read (SEC-13, SEC-16)."""
        folder = self.data_root / "packs"

        def stats() -> tuple[Any, ...]:
            watched = [*(sorted(folder.glob("*.pack")) if folder.is_dir() else []), folder / "installed.json",
                       self.data_root / HELD_REVOCATIONS]  # fmt: skip
            return tuple((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in watched if p.is_file())

        if self._installed is not None and self._installed[0] == stats():
            return self._installed[1:]
        environment = Environment.PRODUCTION if self.release else Environment.DEV
        loaded, refused = PackStore(folder, environment, revocations=self.revocations(), recover=False).load_all()
        paths = [loaded[p] for p in sorted(loaded)]
        # The indexes are keyed by what loaded, so the store's own note that a pack loaded changes nothing.
        key = (tuple((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in paths), tuple(refused))
        self._installed = (stats(), key, paths, refused)
        return self._installed[1:]

    def operation_policy(self) -> DevicePolicy:
        if self._operations is None or self._operations.channel != self.release_channel():
            environment = Environment.PRODUCTION if self.release else Environment.DEV
            self._operations = DevicePolicy(
                self.data_root, environment, self.release_channel(), self.app_version, self.revocations().trust
            )
        return self._operations

    def operation_boundary(self) -> dict[str, str]:
        installed = {p.pack_id: p.version for p in self.content().subject_packs.values()}
        installed["app"] = self.app_version
        if (self.skills_root / BUNDLE_FILE).is_file():
            installed["agent_bundle"] = self.skills_root.name
        self.operation_policy().turn_boundary(installed)
        if self.online is not None:
            self.operation_policy().apply_online(self.online)
        self.schedule_corrections()  # a withdrawal applied now may complete a correction already installed (CUR-36)
        return installed

    def content(self) -> PackIndex:
        """The verified installed packs' labels and difficulties (`store.pack_index`), rebuilt when the pack files,
        their installed record or the revocation list change. The same index serves every learner open, so the
        projections' parameter hash is stable."""
        key, paths, refused = self._installed_packs()
        if self._content is None or self._content[0] != key:
            environment = Environment.PRODUCTION if self.release else Environment.DEV
            index = PackIndex.load(paths, environment, self.revocations().trust)
            index.rejected = [*refused, *index.rejected]
            self._content = (key, index)
        return self._content[1]

    def mastery_params(self) -> MasteryParams:
        """The learner's mastery parameters: the configuration keys with the fitted per-KC values of the installed
        packs' verified mastery-params files (LRN-11). Part of every projection's parameter hash (LRN-02)."""
        return MasteryParams.from_settings(self.settings, kc_overrides=self.content().kc_overrides())

    def knowledge(self) -> KnowledgeIndex:
        """The verified installed packs' search index (CUR-30, MEM-21), rebuilt as `content` is. No embedding model
        ships yet, so packs are searched by keyword (BM25) only."""
        key, paths, refused = self._installed_packs()
        if self._knowledge is None or self._knowledge[0] != key:
            environment = Environment.PRODUCTION if self.release else Environment.DEV
            index = KnowledgeIndex.load(paths, environment, self.revocations().trust(), settings=self.settings)
            index.rejected = [*refused, *index.rejected]
            self._knowledge = (key, index)
        return self._knowledge[1]

    def capture(self) -> CaptureFilter | None:
        """The do-not-capture filter of the shipped agent bundle, loaded once (MEM-10). None when the bundle has no
        capture file: every write then refuses, the filter is never skipped."""
        if not self._capture_loaded:
            path = self.content_root / CAPTURE_FILE
            self._capture = CaptureFilter.load(path, self.settings) if path.is_file() else None
            self._capture_loaded = True
        return self._capture

    def memory_source(self) -> MemorySource | None:
        """The open learner database with its labels and filter: what sessions and the Reflector read and write."""
        if self.learner is None or self.stores is None or self.learner.conn is not self.stores.learner:
            return None
        return MemorySource(self.learner, self.content(), self.capture())

    @contextlib.contextmanager
    def knows(self, profile_id: str | None) -> Iterator[KnowsDesk | None]:
        """The knows page's desk for one profile (G90): the open learner database when it is that profile's (so open
        sessions and the Reflector hear of a deletion through the core), else a short-lived connection to it, as the
        device adult may open a locked profile's page (MEM-14, PRV-10). None when there is no such profile."""
        if self.stores is None:
            yield None
            return
        internal = profile_id[4:] if profile_id else self.implicit[0]
        if internal == self.stores.profile_id:
            source = self.memory_source()
            yield KnowsDesk(source, self.settings, memory_online=self.memory_online_on,
                            terms=self.profile_terms) if source is not None else None  # fmt: skip
            return
        if profile_id is None or self.directory is None:
            yield None
            return
        try:
            opened = self.directory.open_learner(profile_id)
        except (ProfileError, ValueError):
            yield None
            return
        with opened as conn:
            learner = Learner.open(conn, profile_id=internal, device_id=self.stores.device_id,
                                   wall_ms=lambda: int(time.time() * 1000),
                                   max_skew_ms=self.settings.sync_clock_skew_max_ms,
                                   params=self.mastery_params(),
                                   content=self.content(),
                                       projections=assessment_projections(self.settings))  # fmt: skip
            learner.projector.ensure_current()
            yield KnowsDesk(MemorySource(learner, self.content(), self.capture()), self.settings)

    def assessments(self) -> AssessmentDesk | None:
        source = self.memory_source()
        if source is None:
            return None
        if self._assessment_desk is None or self._assessment_desk.learner is not source.learner:
            self._assessment_desk = AssessmentDesk(
                source.learner,
                self.content(),
                self.settings,
                run_tool=self._lesson_tool,
                judge_method=self._assessment_judge,
                explain_judge=self._explanation_judge,
                explain_possible=self._explanation_possible,
                recover=True,
            )
        else:
            self._assessment_desk.settings = self.settings
            self._assessment_desk.content = self.content()
        return self._assessment_desk

    def planner(self) -> PlanDesk | None:
        """The plan desk on the open learner database (LRN-28 to LRN-33, UXS-12, UXS-27); None when none is open.
        Its Keep-learning request keys live as long as the profile stays open."""
        source = self.memory_source()
        if source is None:
            return None
        if self._plan_desk is None or self._plan_desk.learner is not source.learner:
            self._plan_desk = PlanDesk(source.learner, self.content(), self.settings)
        else:
            self._plan_desk.settings = self.settings
            self._plan_desk.content = self.content()
        return self._plan_desk

    @contextlib.contextmanager
    def learn(self) -> Iterator[LearnDesk | None]:
        """The subject map and lesson player's desk on the open learner database (UXS-22, UXS-23), with the installed
        packs; None when no learner database is open."""
        source = self.memory_source()
        if source is None or self.stores is None:
            yield None
            return
        positions = self._lesson_positions.setdefault(self.stores.profile_id, {})
        self.operation_boundary()
        from agent_core.engine.learner_map import MapState

        map_state = self._map_states.setdefault(self.stores.profile_id, MapState())
        yield LearnDesk(presented_content(self.content(), self.operation_policy()), source.learner.conn,
                        self.settings, listed=self.packs,
                        run_tool=self._lesson_tool, positions=positions,
                        record=source.learner.record_batch, projector=source.learner.projector,
                        device=self.stores.device, profile_id=self.stores.profile_id, map_state=map_state)  # fmt: skip

    def drop_lesson_positions(self, profile_id: str) -> None:
        """A deleted profile's in-memory lesson positions go with it (DAT-09), and so do its learner's map state and
        the bands kept at its last map open in the device database (SPEC §21, JM-D45)."""
        from agent_core.engine.learner_map import OPENED_KEY

        internal = profile_id.removeprefix("prf_")
        self._lesson_positions.pop(internal, None)
        self._map_states.pop(internal, None)
        if self.stores is not None:
            self.stores.device.execute("DELETE FROM app_settings WHERE key = ?", (OPENED_KEY.format(profile=internal),))

    async def _lesson_tool(self, name: str, args: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """One checker call for a lesson's quick check (unit conversion, structured answers): the worker, as the
        Checker, outside any session. (None, None) when the worker refuses or cannot run (ASM-02: never guessed)."""
        if self.supervisor is None:
            return None, None
        if self._lesson_tools is None:
            spill = self.stores.learner_path.parent / "spill" if self.stores else self.data_root / "spill"
            self._lesson_tools = ToolExecutor(registry(), self.settings, SpillStore(spill), self.supervisor)
        session = SessionCtx(profile="small", role="checker")
        call = ToolCall(f"call_lesson_{next(_LESSON_CALLS)}", name, args)
        result = await self._lesson_tools.run_one(call, "checker", session)
        if result.get("status") != "ok":
            return None, None
        checks = (result.get("meta") or {}).get("checks") or []
        data = result.get("data") or {}
        value = data.get("value") if isinstance(data, dict) else None
        return (value if isinstance(value, dict) else None), (str(checks[0]["check_id"]) if checks else None)

    async def study_check(self, name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """A private Study check: worker only, no session trace, shared dedupe cache or spill file."""
        if self.supervisor is None or name not in ("math_check_equivalence", "units_convert"):
            return None
        from agent_core.engine.events import new_id

        result = await self.supervisor.call(name, 1, args, new_id("chk"), 5000)
        return result.result if result.ok else None

    async def _assessment_judge(self, criterion: Mapping[str, Any], working: Sequence[str]) -> bool | None:
        """One independent local Judge call (ASM-17); no cloud fallback and no default award."""
        local = self.providers.get("local")
        if local is None or getattr(local, "kind", "local") != "local":
            return None
        if self._assessment_desk is not None and self._assessment_desk.mock_active():
            return None
        router = self._router()
        router.providers = {"local": local}
        router.cloud_on = lambda role: False
        router.network = lambda: "off"
        hardware = self.hardware_class()
        auxiliary = Auxiliaries.load(router, self.settings, self.content_root, hardware or "mid")
        rule = "\n".join(str(criterion.get(key) or "") for key in ("problem", "prompt", "pass_if", "fail_if"))
        text = judge_text(self.settings, working)
        if text is None:
            return None
        with job_scope(new_id("job")):
            judged, fallback = await auxiliary.judge([(str(criterion["criterion_id"]), rule)], text)
        if fallback or not judged or judged[0]["status"] != "judged":
            return None
        return bool(judged[0]["pass"])

    async def _explanation_judge(self, criteria: Sequence[tuple[str, str]], text: str) -> list[dict[str, Any]] | None:
        """Explain it back: the Checker's judge on the learner's own explanation of a KC, one criterion per key point,
        on whichever runtime the router has (local or online); the input guard and redaction run first. None when no
        judge answered or the text may not reach a model, so the explanation is not counted (never a pass)."""
        if self._assessment_desk is not None and self._assessment_desk.mock_active():
            return None
        words = judge_text(self.settings, [text])
        if not words:
            return None
        auxiliary = Auxiliaries.load(self._judge_router(), self.settings, self.content_root,
                                     self.hardware_class() or "mid")  # fmt: skip
        # The router refuses a call outside a turn or a job, and this one comes from an API request, not a tutor turn.
        with job_scope(new_id("job")):
            judged, fallback = await auxiliary.judge(criteria, words, explanation=True)
        if fallback:
            # Fixed reason codes only, never the learner's words: an unanswered judge blocks mastery, so it must show.
            reasons = [o.fallback_reason for o in auxiliary.log]
            log.warning("explanation judge did not answer: %s", reasons)
        return None if fallback else judged

    def _judge_router(self) -> Router:
        """The explanation judge's router, with the profile's cloud tier when it has one, because every skill now asks
        for an explanation: a learner with no local model must still be able to reach mastery."""
        cloud = self.cloud_for_session()
        if cloud is None:
            return self._router()
        router = self._router(cloud.providers)
        router.network, router.recheck, router.recheck_soon = cloud.network, cloud.recheck, cloud.recheck_soon
        router.budget = cloud.budget
        return router

    def _explanation_possible(self) -> bool:
        """Whether any judge could answer: a local tutor model, or the profile's cloud tier turned on. With neither, a
        check confirms as it did before key points: it never waits on a judge that cannot exist."""
        return not self.tutor_unavailable() or self.cloud_tier_on()

    def research_router(self) -> Router | None:
        """The router for the research agent's conversation (SPEC §18.1a), online only: the open profile's cloud tier
        (its gateway provider, connectivity view, re-checks and budget) as a session's router has them; None without
        the connectivity layer or an open profile."""
        cloud = self.cloud_for_session()
        if cloud is None:
            return None
        router = self._router(cloud.providers)
        router.network, router.recheck, router.recheck_soon = cloud.network, cloud.recheck, cloud.recheck_soon
        router.budget = cloud.budget
        return router

    def _router(self, extra: dict[str, Any] | None = None) -> Router:
        if self._router_data is None:
            self._router_data = yaml.safe_load((CORE / "router" / "router.yaml").read_text())
        data = json.loads(json.dumps(self._router_data))
        if self.model:
            _pin(data, self.model)
        hardware = self.hardware_class()
        router = Router(load_router(data), self.settings, {**self.providers, **(extra or {})},
                        hardware_class=hardware if hardware in ("low", "mid", "high", "content_only") else None,
                        shared=self.router_shared, cloud_on=lambda role: self.cloud_tier_on(),
                        power=self.power_plan)  # fmt: skip
        if self.online is not None:
            router.network = lambda: self.online.connectivity.state if self.online is not None else "off"
        return router

    def reflector(self) -> ReflectorQueue:
        """The device's Reflector queue (AGT-26), sharing the local scheduler with every session (MDL-22)."""
        if self._reflector is None:
            assets = AgentAssets.load(self.content_root, self.skills_root,
                                      RELEASE_SKILL_STATUSES if self.release else DEV_SKILL_STATUSES)  # fmt: skip
            reg = registry()
            schemas = bundled_schemas()

            def model() -> ReflectorModel | None:
                # SPEC §19.4, §19.6 (W8 phase 3): while tutor_memory_online is effective the Reflector's router also
                # holds the cloud tier's providers, and its gate lets a reflector call reach them; otherwise it is the
                # device's runtime alone, as before (MDL-25).
                online = self.memory_online_on()
                cloud = self.cloud_for_session() if online else None
                if "local" not in self.providers and cloud is None:
                    return None
                router = self._router(cloud.providers if cloud is not None else None)
                router.memory_online = self.memory_online_on
                if cloud is not None:
                    router.network, router.budget = cloud.network, cloud.budget
                return ReflectorModel(router, assets, self.settings, schemas, frozenset(reg.specs),
                                      memory_online=self.memory_online_on, terms=self.profile_terms)  # fmt: skip

            spill = self.stores.learner_path.parent / "spill" if self.stores else self.data_root / "spill"
            self._reflector = ReflectorQueue(
                self.settings,
                model,
                ToolExecutor(reg, self.settings, SpillStore(spill)),
                self.memory_source,
                hardware_class=lambda: str((self.plan or {}).get("hardware_class") or "mid"),
                trace_sink=self._trace_sink if self.traces else None,
            )
        return self._reflector

    def engine(self, state: SessionState) -> Engine:
        def pin(data: dict[str, Any]) -> None:
            if self.model:
                _pin(data, self.model)

        # The spill store is per profile (TABLE DAT-01a), once the profile's stores are open.
        spill = self.stores.learner_path.parent / "spill" if self.stores else self.data_root / "spill"
        today = datetime.datetime.now(datetime.UTC).date().isoformat()  # the session date: notes are dated by it
        cloud = self.cloud_for_session()
        providers = {**self.providers, **(cloud.providers if cloud is not None else {})}
        engine = assemble(providers, self.settings, state, self.content_root, self.skills_root,
                          spill, self.supervisor, release=self.release,
                          router_overrides=pin, trace_sink=self._trace_sink if self.traces else None,
                          runtime_restarted=self.link.await_restart if self.link else None,
                          router_shared=self.router_shared, session_date=today,
                          device_class=self.hardware_class(), cloud_on=lambda role: self.cloud_tier_on(),
                          power=self.power_plan,
                          visual_sources=lambda code: self.content().visual_sources(code),
                          misconception_context=lambda item: pack_context(self.content(), item),
                          tutor_name=self.tutor_name_preference)  # fmt: skip

        engine.deps.perception_command = self.perception_command
        engine.deps.router.prefer_fast = self.faster_tutor_preference  # "Use the faster tutor", remembered (UXS-03)
        if cloud is not None:
            engine.deps.router.network = cloud.network
            engine.deps.router.recheck = cloud.recheck
            engine.deps.router.recheck_soon = cloud.recheck_soon
            engine.deps.router.budget = cloud.budget
        if self.online is not None:
            profile = self.public_profile()
            online = self.online
            learner = self.stores.learner if self.stores else None
            engine.cloud_possible = lambda: online.cloud_effective(profile, learner)
            engine.cloud_notice = lambda: online.notice_answer(profile)
            engine.record_cloud_notice = lambda accepted: online.record_notice(profile, "cloud_tier", accepted)
            engine.voice_possible = lambda: online.voice_effective(profile, learner)
            engine.voice_notice = lambda: online.notice_answer(profile, "cloud_voice")
            engine.record_voice_notice = lambda accepted: online.record_notice(profile, "cloud_voice", accepted)
        if self.stores is not None:
            # The session belongs to the profile whose learner database is open now; its memory tools stay bound to
            # that profile and refuse once another profile's database is open (TOL-23, DAT-14).
            memory = engine.bind_memory(MemoryAccess(self.stores.profile_id, self.memory_source))
            bound_profile_id = self.stores.profile_id

            def memory_online() -> bool:  # the session's own profile only (§19.6): never another's consent
                same = self.stores is not None and self.stores.profile_id == bound_profile_id
                return same and self.memory_online_on()

            memory.memory_online = memory_online
            memory.terms = self.profile_terms

            def completed_question(text: str, subject: str) -> tuple[dict[str, Any], str] | None:
                if self.stores is None or self.stores.profile_id != bound_profile_id:
                    return None
                desk = self.assessments()
                return desk.completed_tutor_question(text, subject) if desk is not None else None

            engine.completed_assessment_question = completed_question
            # Saved questions for human help go to this profile's learner database only (PED-24).
            saved = SavedQuestions(self.stores.learner, int(self.settings.retention_saved_content_days * 86_400_000),
                                   lambda: int(time.time() * 1000))  # fmt: skip
            engine.save_question = saved.save
            # The local parent view's wellbeing entry (TCH-14, PRV-14): this profile's learner database only.
            alerts = self.stores.learner
            engine.wellbeing_shown = lambda category: record_wellbeing_shown(alerts, category,
                                                                             int(time.time() * 1000))  # fmt: skip
            # The learner model's reads the S2 flows take (PED-22, LRN-26), from this profile's learner database.
            learner = self.stores.learner
            engine.misconception_evidence = lambda mc_id: misconception_evidence(learner, mc_id)
            engine.stuck_cap = lambda kc_ids: stuck_cap(learner, kc_ids, self.settings)
            # ASM-23: the re-mark notice waiting in this profile's learner database, taken (and so shown) once.
            engine.remark_notice = lambda: take_notice(learner, int(time.time() * 1000))
            params = self.mastery_params()
            engine.start_rung = lambda kc_ids: adaptation.start_rung(learner, params, kc_ids, self.settings,
                                                                     int(time.time() * 1000))  # fmt: skip
            # The engine's own offers (LRN-20, LRN-23): the student's preference and what a detour reads.
            engine.unprompted_help = lambda: adaptation.unprompted_help_on(learner)
            engine.detours = adaptation.LearnerDetours(learner, params, self.content(),
                                                       lambda: int(time.time() * 1000))  # fmt: skip
        knowledge = self.knowledge()
        if knowledge.packs:  # the knowledge tools are offered only over installed, verified packs (TOL-06, CUR-08)
            engine.bind_knowledge(knowledge)
        previous = None
        if (self.skills_root / BUNDLE_FILE).is_file():
            try:
                selected = rollback_bundle(
                    self.skills_root.parent,
                    Environment.PRODUCTION if self.release else Environment.DEV,
                    self.skills_root.name,
                    self.revocations().trust(),
                )
                previous = SkillSet.load(selected.skills_root)
            except BundleRefused:
                pass
        install_operations(
            engine,
            self.operation_policy(),
            self.operation_boundary,
            previous_skills=previous,
            policy_source=self.operation_policy,
            local_models=lambda: {self.model} if self.model else set(),
        )
        self.engines.add(engine)
        return engine

    async def clear_model_caches(self) -> None:
        """Erase the local runtime's KV caches (DAT-14)."""
        provider = self.providers.get("local")
        if provider is not None and hasattr(provider, "clear_cache"):
            if hasattr(provider, "stop_warming"):
                provider.stop_warming()
            await provider.clear_cache()
            await self.start_warmup(provider)  # the static prefixes again, with the new profile's tutor name

    def packs(self) -> list[dict[str, Any]]:
        """The installed packs under `packs/`, verified as listed; a release build trusts only release roots."""
        environment = Environment.PRODUCTION if self.release else Environment.DEV
        return list_packs(self.data_root / "packs", environment, self.revocations().trust())

    def storage_low(self) -> bool:
        return disk_low(self.data_root, self.settings.disk_low_free_mb)

    def health_components(self, now_ms: int | None = None) -> dict[str, str]:
        """The health panel's conditions as `Health.components` entries (OBS-15; see `engine.health`)."""
        out: dict[str, str] = {}
        if self.supervisor is not None:
            out["worker"] = "ok" if self.supervisor.compute_available() else "down"
            out["sandbox"] = "degraded" if self.supervisor.reduced_protection else "ok"
        # ok only while the shell keeps every device secret in the OS keychain (SEC-17, D97); the file fallback, a
        # secret the shell could not store and a development build's memory store read as degraded.
        out["keychain"] = "ok" if getattr(self.keys, "os_keychain", False) else "degraded"
        if self.remark_failing():
            out["remark"] = "degraded"  # CUR-36: a correction's re-mark keeps failing (it backs off meanwhile)
        if self.link is not None:
            out["runtime_fallback"] = "degraded" if self.link.on_fallback else "ok"
        # PLT-22: the client's low-battery banner reads this ("Saving battery: shorter answers").
        out["power"] = "degraded" if power.saving(self.settings, self.low_power_preference(), self.battery()) else "ok"
        variant = read_variant(self.data_root, self.epoch)
        if variant is not None and variant.get("variant"):
            # PLT-04: the runtime runs on a build other than the first choice, which failed its touch generation.
            out["runtime_variant"] = "degraded" if variant.get("fallbacks") else "ok"
        if self.traces is not None:
            speed, lower = self._speed_status(int(time.time() * 1000) if now_ms is None else now_ms)
            if speed is not None:
                out["speed"] = speed
            if lower:
                out["class_lower"] = "degraded"  # PLT-09: suggest a lower class in Settings, under Tutor models
        return out

    def _speed_status(self, now_ms: int) -> tuple[str | None, bool]:
        """The speed component and whether to suggest a lower class (PLT-09), read again only when the trace store
        changed or the window moved on by an hour: the health answer stays cheap enough to poll during a runtime
        restart (ARC-02)."""
        assert self.traces is not None
        hw_class = (self.plan or {}).get("hardware_class")
        key = (self.traces.changes, now_ms // health.RECHECK_MS, hw_class)
        if self._speed is None or self._speed[0] != key:
            try:
                rows = self.traces.timings_since(health.window_start_ms(now_ms, self.settings))
            except (sqlite3.Error, OSError):
                rows = []  # an unreadable trace store hides the speed line; it never fails the health answer
            measured = health.speed(rows, hw_class, self.settings)
            self._speed = (key, None if measured is None else "degraded" if measured.slow else "ok",
                           hardware.suggest_lower(rows, hw_class, self.settings))  # fmt: skip
        return self._speed[1], self._speed[2]

    def model_catalogue(self) -> dict[str, Any]:
        """`GET /v1/models` from the launch plan and the model the runtime serves (API-29 subset, OBS-15), with the
        hardware report (PLT-04, PLT-08, PLT-29) once the contract carries it (`health.catalogue`)."""
        return health.catalogue(self.plan, self.model, self.hardware_report())

    # --- the hardware class, its benchmark and the tutor's path (PLT-08, PLT-09, PLT-29) -----------------------

    def read_probe(self) -> Probe:
        """Read this machine (MDL-16): offline, without elevated rights."""
        return probe_machine(self.data_root)

    def hardware_current(self) -> hardware.Current | None:
        if self.probe is None:
            return None
        return hardware.current(self.probe, hardware.load_record(self.data_root), self.settings)

    def hardware_class(self) -> str | None:
        if self.model and self.operation_policy().decision("model_pack", self.model).action == "disable":
            return "content_only"
        cur = self.hardware_current()
        return cur.choice.hardware_class if cur is not None else (self.plan or {}).get("hardware_class")

    def attach_online(self) -> OnlineLayer:
        """The device's connectivity layer on device.db (ARC-06), with the keystore holding its service credential."""
        assert self.stores is not None
        environment = (
            Environment.PILOT
            if self.settings.environment == "pilot"
            else Environment.PRODUCTION
            if self.release
            else Environment.DEV
        )
        self.online = OnlineLayer(self.settings, self.stores.device, self.key_store(), environment,
                                  self.release)  # fmt: skip
        return self.online

    def prepare_voice(self, api_port: int, adapter_token: str, shell: Callable[[dict[str, Any]], None],
                      trust: Callable[[], TrustStore] | None = None) -> VoiceLaunch:  # fmt: skip
        import platform
        import sys

        family = {"darwin": "macos", "win32": "windows", "linux": "linux"}.get(sys.platform, "unsupported")
        arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
        hardware_class = (self.plan or {}).get("hardware_class", "low")
        if not self.release and os.environ.get("JANA_DEV_HARDWARE") in ("mid", "high"):
            hardware_class = os.environ["JANA_DEV_HARDWARE"]  # dev only (apps/desktop/dev/sidecar.ts)
        # TABLE VOC-03a by class; SEC-16 and D88: the live revocation list at install and every launch; VOC-03 memory.
        link = VoiceLaunch(self.data_root / "speech-packs", hardware_class if hardware_class in
                           ("content_only", "mid", "high") else "low", f"{family}-{arch}", app_version(),
                           Environment.PRODUCTION if self.release else Environment.DEV,
                           f"http://127.0.0.1:{api_port}/v1/voice/llm", adapter_token, shell, trust=trust,
                           memory_fits=memory_budget(self.plan, self.probe.ram_bytes if self.probe else None,
                                                     self.settings.voice_memory_headroom_gb,
                                                     self.settings.speech_pack_resident_gb))  # fmt: skip
        link.scan(manifest_only=True)  # startup checks signatures; first use hashes the selected class before launch
        return link

    def prepare_cloud_voice(self, announce: Callable[[str, dict[str, Any]], None],
                            respond: Callable[[Any], Any]) -> CloudVoice | None:  # fmt: skip
        """Cloud voice for the open profile (VOC-28), when this build carries it and the connectivity layer runs.
        `announce` writes a session's `egress.notice`; `respond` is the engine adapter the tunnel answers from."""
        if self.online is None or not self.settings.cloud_voice_enabled:
            return None
        return CloudVoice(self.online, self.public_profile,
                          lambda: self.stores.learner if self.stores is not None else None, announce,
                          respond)  # fmt: skip

    @staticmethod
    def voice_route(cloud: CloudVoice, local: Callable[[], Any], local_available: Callable[[], bool]) -> VoiceRoute:
        return VoiceRoute(cloud, local, local_available)

    def delete_profile_traces(self, profile_id: str) -> None:
        """The `traces` eraser (DAT-09): the profile's turn traces, when this runtime keeps a trace store."""
        if self.traces is not None:
            self.traces.delete_profile(profile_id)

    def forget_profile_online(self, profile_id: str) -> None:
        """A deleted profile leaves no egress-notice decision behind (DAT-09)."""
        if self.online is not None:
            forget_first_use(self.online.device, profile_id, int(time.time() * 1000))

    def public_profile(self) -> str | None:
        return f"prf_{self.stores.profile_id}" if self.stores is not None else None

    def cloud_for_session(self) -> Any:
        """The cloud tier's providers, connectivity view and budget for a new session of the open profile (ARC-12,
        MDL-29), or None without the connectivity layer. Its redaction terms are the profile's own identifiers."""
        if self.online is None or self.stores is None:
            return None
        profile = self.public_profile()
        terms = self.profile_terms()
        stored = subscriptions.load(self.stores.device)
        device = self.stores.device
        clock_ms = self.online.clock_ms
        spend = DeviceSpend(device, profile or "",
                            lambda: subscriptions.allowance_period(subscriptions.load(device), clock_ms()),
                            clock_ms)  # fmt: skip
        return cloud_session(self.online, self.settings, profile_id=profile, terms=terms, spend=spend,
                             learner=lambda: (self.stores.learner
                                              if self.stores and self.public_profile() == profile else None),
                             allowance_tokens=stored.get("allowance_tokens"))  # fmt: skip

    def profile_terms(self) -> list[str]:
        """The Redactor's terms for the open profile: its ids and display name (cloud requests, and the egress scrub
        of notes under tutor_memory_online, SPEC §19.6)."""
        if self.stores is None:
            return []
        terms = [self.stores.profile_id, self.stores.device_id, self.public_profile() or ""]
        row = self.stores.learner.execute("SELECT display_name FROM profile LIMIT 1").fetchone()
        if row is not None:
            terms.append(str(row[0]))
        return [t for t in terms if t]

    def memory_online_on(self) -> bool:
        """Personal tutor memory online is in effect for the open profile (SPEC §19.6, JM-D21): the cloud tier is on
        (switch, subscription, consent and its accepted notice) and the profile's `tutor_memory_online` consent and
        switch are too. Without the connectivity layer it is never on."""
        if self.online is None or self.stores is None or not self.cloud_tier_on():
            return False
        try:
            return bool(self.online.memory_online_effective(self.public_profile(), self.stores.learner))
        except Exception:
            return False

    def cloud_tier_on(self) -> bool:
        """The cloud tier is in effect for the open profile (MDL-24, ARC-11, D70): with the connectivity layer, the
        switch, an active subscription naming the profile, its consent and its accepted first-use notice (ARC-10);
        without it (tests of earlier milestones), the adult's switch alone."""
        if self.online is not None:
            profile = self.public_profile()
            learner = self.stores.learner if self.stores is not None else None
            return self.online.cloud_effective(profile, learner) and self.online.notice_answer(profile) == "accepted"
        if self.directory is None:
            return False
        row = self.directory.device.execute(
            "SELECT 1 FROM online_features WHERE feature = 'cloud_tier' AND enabled = 1"
        ).fetchone()
        return row is not None

    def tutor_path(self) -> hardware.TutorPath:
        return hardware.tutor_path(self.hardware_class(), self.cloud_tier_on(), self.school_server_reachable())

    def tutor_unavailable(self) -> bool:
        """No model can answer, so no model call is made: below the minimum laptop with neither a school server nor
        the online tier (PLT-29), or a runtime that is gone for good (the restart policy spent, or none planned) with
        neither of those to turn to (ARC-26, MDL-19)."""
        if self.hardware_class() == "content_only":
            return self.tutor_path() == "none"
        if self.link is not None and self.link.state in ("gave_up", "none"):
            return not self.cloud_tier_on() and not self.school_server_reachable()
        return False

    def session_class(self) -> str | None:
        """The class a new session's prompts and limits follow: the plan's local class, or `cloud` when the tutor
        runs through the online tier below the minimum laptop (PLT-29). Below it with no tutor at all the session is
        `content_only`, so what needs a model on this computer, the photo reader first, is off there (MDL-23, UXS-13,
        TABLE INP-21a); None keeps the engine's default."""
        hw = self.hardware_class()
        if hw in ("low", "mid", "high"):
            return hw
        if hw != "content_only":
            return None
        path = self.tutor_path()
        return "cloud" if path == "online" else "content_only" if path == "none" else None

    # --- low-power mode (PLT-22) --------------------------------------------------------------------------

    def battery(self) -> power.Battery | None:
        """The battery, read at most every `low_power_battery_poll_s`."""
        now = time.monotonic()
        if self._battery is None or now - self._battery[0] >= self.settings.low_power_battery_poll_s:
            self._battery = (now, self.read_battery())
        return self._battery[1]

    def tutor_name_preference(self) -> str | None:
        """The open profile's chosen tutor name (PRM-03, D99), or None for the identity file's default."""
        if self.stores is not None:
            row = self.stores.learner.execute("SELECT value FROM settings WHERE key = 'tutor_name'").fetchone()
            if row is not None:
                name = json.loads(row[0])
                return name if isinstance(name, str) and name.strip() else None
        return None

    def faster_tutor_preference(self) -> bool:
        """The open profile chose "Use the faster tutor" (UXS-03, MDL-05): the router serves strong-tier roles from the
        fast tier. `faster_tutor` is a student preference of the Settings contract (D103), written with the profile's
        settings patch; unset, the default (off) holds."""
        if self.stores is not None:
            row = self.stores.learner.execute("SELECT value FROM settings WHERE key = 'faster_tutor'").fetchone()
            if row is not None:
                return bool(json.loads(row[0]))
        return False

    def low_power_preference(self) -> bool:
        """The open profile's `low_power` preference (D53), or its default."""
        if self.stores is not None:
            row = self.stores.learner.execute("SELECT value FROM settings WHERE key = 'low_power'").fetchone()
            if row is not None:
                return bool(json.loads(row[0]))
        return bool(self.settings.low_power)

    def power_plan(self) -> power.PowerPlan | None:
        """What saving power changes for the next call, or None when the app is not saving."""
        models = [a["model"] for a in (self.plan or {}).get("attempts", [])]
        return power.plan(self.settings, self.low_power_preference(), self.battery(), models, self.model)

    def hardware_report(self) -> dict[str, Any]:
        return hardware.report(self.hardware_current(), self.tutor_path(), read_variant(self.data_root, self.epoch))

    def start_benchmark(self, provider: Any) -> asyncio.Task[Any] | None:
        """PLT-08: once a launch is ready, measure on the smallest planned model when the plan says it is due (first
        run, or new hardware). The result is kept for the next start; this launch keeps its class and context."""
        plan = self.plan or {}
        if not plan.get("benchmark_due") or self.probe is None or self.measuring is not None:
            return None
        smallest = plan["attempts"][-1]["model"] if plan.get("attempts") else None
        if smallest is None:
            return None
        probe = self.probe

        async def run() -> Any:
            with contextlib.suppress(Exception):  # a failed benchmark leaves the class predicted; it runs next start
                choice, _ = await hardware.measure(provider, smallest, probe, self.data_root, self.settings)
                if self.plan is not None:
                    self.plan["benchmark_due"] = False
                return choice
            return None

        self.measuring = asyncio.get_running_loop().create_task(run())
        return self.measuring

    def _trace_sink(self, record: dict[str, Any]) -> None:
        """Traces stop first when the disk is nearly full; events and checkpoints keep being written (DAT-13)."""
        if self.traces is not None and not self.storage_low():
            self.traces.submit(record)
        if record.get("job_name") == "capture" and record.get("perception", {}).get("outcome") not in (
            "confirmed",
            "refused",
            "unreadable",
            "cancelled",
        ):
            return  # partial observations are local; one terminal observation contributes to aggregate telemetry
        if self.online is not None and "telemetry" in self.online.features_on():
            from agent_core.trace.metrics import catalogue

            with contextlib.suppress(ValueError, OSError, sqlite3.Error):
                for row in catalogue([record], [], self.settings)["series"]:
                    metrics = row.pop("metrics")
                    self.online.telemetry.add(metrics, **row)

    async def connect_runtime(self, url: str, key: str, model: str) -> bool:
        """Attach llama-server; ready only after a real touch generation, never on health (ARC-22, MDL-20). Each
        launch the shell starts (a restart included) arrives here with its own key."""
        generation = self.link.attaching(model) if self.link else 0
        old = self.providers.get("local")
        launch = self.plan or (self.link.plan if self.link else {})
        families = {
            attempt["model"]: attempt["family"] for attempt in launch.get("attempts", [])
            if isinstance(attempt.get("model"), str) and isinstance(attempt.get("family"), str)
        }
        provider = LlamaServerProvider(url, key, model_families=families)
        self.providers["local"] = provider
        for engine in list(self.engines):  # a session with the cloud tier routes over its own copy (ARC-12)
            engine.deps.router.providers["local"] = provider
        if model != self.model:  # a session opened earlier, or the next smaller model after a restart (MDL-19)
            for engine in list(self.engines):
                _pin(engine.deps.router.config.data, model)
        self.model = model
        if old is not None and hasattr(old, "close"):
            await old.close()
        ready = bool(await provider.ready(model))
        if self.link:
            self.link.attached(generation, ready)
        if ready:
            self.start_benchmark(provider)
            await self.start_warmup(provider)
        return ready

    async def start_warmup(self, provider: Any) -> None:
        """D128: read each foreground role's static prompt prefix into the runtime in the background, from a session
        the student has not opened (no transcript, learner block or memory), so the first turn reads only its own
        task. A failure to build the prompts leaves the runtime cold, never the start blocked (ARC-22)."""
        if not hasattr(provider, "start_warming") or not self.model:
            return
        warm_class = self.session_class()
        if warm_class in self.settings.offline_lean_classes:
            warm_class = "low"  # this primer serves the local model, whose turns use the lean path
        if warm_class not in self.settings.runtime_warmup_classes:
            return
        try:
            state = SessionState(new_id("ses"), hardware_class=warm_class or "mid")
            engine = assemble({"local": provider}, self.settings, state, self.content_root, self.skills_root,
                              self.data_root / "spill", self.supervisor, release=self.release,
                              router_overrides=lambda data: _pin(data, str(self.model)),
                              device_class=self.hardware_class(),
                              tutor_name=self.tutor_name_preference)  # fmt: skip
            requests = await engine.warm_requests()
        except Exception as error:
            log.warning("runtime.warmup_skipped: %s", type(error).__name__)
            return
        provider.start_warming(self.model, requests)

    async def runtime_alive(self) -> bool:
        """The attached runtime still listens (MDL-19 loss detection; not readiness)."""
        provider = self.providers.get("local")
        return provider is None or not hasattr(provider, "alive") or bool(await provider.alive())

    async def close(self) -> None:
        if self._assessment_desk is not None:
            self._assessment_desk.interrupt_all()
        if self.measuring is not None:
            self.measuring.cancel()
            await asyncio.gather(self.measuring, return_exceptions=True)
        if self._reflector is not None:
            await self._reflector.close()
        if self.link is not None:
            await self.link.close()
        if self.supervisor is not None:
            await self.supervisor.close()
        if self.traces is not None:
            self.traces.close()
        if self.stores is not None:
            self.stores.close()
        for provider in self.providers.values():
            if hasattr(provider, "close"):
                await provider.close()


def _pin(data: dict[str, Any], model: str) -> None:
    """Pin each tier's local candidate while preserving its consented cloud routes."""
    for tier in data["tiers"]:
        cloud = [candidate for candidate in data["tiers"][tier] if candidate.startswith("cloud:")]
        data["tiers"][tier] = [f"local:{model}", *cloud]


async def start_worker_pool(settings: Settings, scratch_root: Path, *, release: bool = False) -> WorkerSupervisor:
    """The sandboxed tool worker pool the core owns (ARC-02: the core starts and supervises the worker). A release
    build never starts a worker its OS sandbox cannot wrap (SEC-06)."""
    factory = process_factory(settings, scratch_root=scratch_root, require_sandbox=release)
    supervisor = WorkerSupervisor(factory, settings)
    await supervisor.start()
    return supervisor


def judge_text(settings: Settings, working: Sequence[str]) -> str | None:
    """The student's working as the local Judge may read it (ASM-17, PRV-13, PRV-16, PRV-20): the input guard runs
    first, on the whole text. Working the guard would answer with a template (distress above all), working that
    carries instructions, or a guard that could not run sends nothing to the model; the criterion is then unresolved
    and no mark is awarded by default. Personal details are redacted before the model sees anything."""
    joined = normalise_input("\n".join(working))
    if not joined.strip():
        return joined
    result = InputGuard(settings).check(joined)
    if result.failed or result.injection is not None or result.category not in ("in_scope", "personal_data"):
        return None
    return result.text
