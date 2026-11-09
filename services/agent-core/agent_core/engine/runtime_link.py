"""The core's side of the model runtime the shell starts and supervises (ARC-02, ARC-22, ARC-24, MDL-19, SEC-24).

Launch plan. At bring-up, before `control.ready`, the core rebuilds the runtime's models folder from the verified
model manifest (SEC-24) and writes `<data_dir>/runtime/launch.json`. The shell reads it after `control.ready`:

    plan_version    1
    epoch           this core process's epoch: the shell ignores a plan another process wrote
    model           the model to start, or null: no runtime (content-only), and `reason` says why
    reason          null, or scripted | no_model_manifest | manifest_refused | no_verified_model |
                    content_only_class | no_model_fits | plan_failed
    hardware_class  the class the plan is for: measured when `runtime/hardware.json` holds a benchmark taken on this
                    hardware, else predicted from the probe (PLT-08, PLT-09; `router.hardware`)
    hardware_reason why that class, as a key for the health panel (OBS-15): predicted | measured | chosen |
                    below_minimum_ram | below_minimum_cpu | below_minimum_disk | below_minimum_speed
    benchmark_due   the core runs the PLT-08 benchmark once this launch is ready (first run, or new hardware)
    model_pack      {pack_id, version} of the verified model manifest, or null
    models_dir      the runtime's folder of links to verified model files
    attempts        [{model, context, args}]: the model to start, then at most the next smaller one (MDL-19)
    restart         {backoff_s, max_restarts, window_s}: the router configuration's `local.restart` (MDL-19)
    left_out        [[file or model, why]], for the health panel
    priority        {niceness, reserved_cores}: the shell starts the runtime below normal priority with its threads
                    capped at the physical cores minus `reserved_cores` (ARC-31)

`args` is the whole llama-server argument list except `--port` and `--api-key-file`: the shell sets those for each
launch, with a fresh key that is never in argv, the environment or a log.

Runtime build. The shell picks the llama.cpp build (`vulkan`, `cpu-avx2`, `cpu-baseline`, `metal`, `cpu-arm64`,
PLT-04) and writes what it chose to `<data_dir>/runtime/variant.json` for this core's epoch: `{epoch, variant,
fallbacks: [{variant, reason}], rejected: [{variant, reason}]}`. A fallback is a build that failed its touch
generation; the health panel shows the build in use and why it is not the first choice (OBS-15).

Runtime loss. The control contract has no shell-to-core message for "the runtime is gone", so the core watches the
runtime it was given: a request that finds nothing listening, or a failed liveness poll, marks it lost. The core
applies the plan's restart policy to the losses it has seen. While the shell will restart it (or switch to the next
smaller model) readiness is `model_loading` with the runtime `starting`; once the policy is spent, or no new endpoint
arrives within `runtime_restart_wait_s`, readiness is `degraded` with the runtime `down` (content-only, ARC-26). Each
`control.runtime_endpoint` attaches again and proves readiness by a real generation (MDL-20), so a late endpoint
always recovers."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent_core.config import Settings
from agent_core.config.environments import Environment
from agent_core.config.revocation import RevocationStore
from agent_core.router import hardware
from agent_core.router.config import load as load_router
from agent_core.router.fit import feature_bytes
from agent_core.router.launch_plan import Installed, attempts
from agent_core.router.probe import ClassChoice, Probe, probe_machine
from agent_core.store.model_pack_store import ModelPackStore
from agent_core.store.model_packs import ModelManifestError, Prepared, prepare_runtime_models


def app_version() -> str:
    """The running core's version, which model packs' `min_app_version` is checked against (PLT-10)."""
    try:
        return importlib.metadata.version("jana-agent-core")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


PLAN_VERSION = 1
ROUTER_YAML = Path(__file__).resolve().parents[1] / "router" / "router.yaml"

# Readiness (TABLE ARC-22b) and the runtime component for each state of the link.
STATES: dict[str, tuple[str, str]] = {
    "none": ("core_ready", "down"),  # no model planned: content-only is the final state
    "waiting": ("core_ready", "starting"),  # planned; the shell has not sent an endpoint yet
    "loading": ("model_loading", "starting"),  # attached; the touch generation runs
    "ready": ("ready", "ok"),
    "failed": ("degraded", "down"),  # the touch generation failed (MDL-20)
    "restarting": ("model_loading", "starting"),  # lost; the shell restarts it or starts the next smaller model
    "gave_up": ("degraded", "down"),  # the restart policy is spent: content-only (MDL-19)
}
SETTLED = frozenset({"ready", "failed", "gave_up", "none"})


def plan_path(data_dir: Path) -> Path:
    return data_dir / "runtime" / "launch.json"


def variant_path(data_dir: Path) -> Path:
    return data_dir / "runtime" / "variant.json"


def read_variant(data_dir: Path, epoch: str) -> dict[str, Any] | None:
    """The runtime build the shell chose for this core (PLT-04), or None before it has written one."""
    try:
        data = json.loads(variant_path(data_dir).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("epoch") == epoch else None


def write_launch_plan(data_dir: Path, settings: Settings, release: bool, epoch: str, scripted: bool = False,
                      probe: Probe | None = None) -> dict[str, Any]:  # fmt: skip
    """Build the runtime's models folder and write the launch plan; never raises (content-only still works)."""
    runtime_dir = data_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    local = load_router(yaml.safe_load(ROUTER_YAML.read_text())).local
    plan: dict[str, Any] = {"plan_version": PLAN_VERSION, "epoch": epoch, "model": None, "reason": None,
                            "hardware_class": None, "models_dir": str(runtime_dir / "models"), "attempts": [],
                            "restart": local["restart"], "left_out": [], "hardware_reason": None, "model_pack": None,
                            "benchmark_due": False,
                            "priority": {"niceness": settings.runtime_niceness,
                                         "reserved_cores": settings.runtime_reserved_cores}}  # fmt: skip
    try:
        plan["reason"] = "scripted" if scripted else _fill(plan, data_dir, settings, release, local, probe)
    except (OSError, ValueError, KeyError, TypeError) as error:
        plan.update(model=None, attempts=[], reason="plan_failed", left_out=[["plan", type(error).__name__]])
    target = plan_path(data_dir)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(plan, indent=1))
    os.replace(tmp, target)
    return plan


def class_reason(choice: ClassChoice, chosen: bool = False) -> str:
    """Why the machine is in its class, as a key the health panel names in words (OBS-15, PLT-08, PLT-09, PLT-29)."""
    if choice.shortfalls:
        return f"below_minimum_{choice.shortfalls[0].check}"
    if chosen:
        return "chosen"
    return "predicted" if choice.provisional else "measured"


def _fill(plan: dict[str, Any], data_dir: Path, settings: Settings, release: bool, local: dict[str, Any],
          probe: Probe | None) -> str | None:  # fmt: skip
    """Fill the plan's model and attempts; return why there is no model, or None."""
    installed = data_dir / "models"
    manifest_file = installed / "manifest.json"
    environment = Environment.PRODUCTION if release else Environment.DEV
    models_dir = Path(plan["models_dir"])
    manifest: dict[str, Any]
    if not manifest_file.is_file():
        # PLT-10 model packs (M20): each pack verified again now, then linked for the runtime like the M11 manifest.
        packs = installed / "packs"
        if not packs.is_dir() or not any(packs.iterdir()):
            return "no_model_manifest"
        if models_dir.exists():
            shutil.rmtree(models_dir)
        models_dir.mkdir(parents=True)
        # The device's revocation list applies to every model it links (SEC-16), not the bundled anchors alone.
        store = ModelPackStore(packs, environment, app_version(), RevocationStore(data_dir, environment).trust,
                               settings=settings)  # fmt: skip
        manifest = {"models": [], "pack_id": "", "version": ""}
        manifest["models"], refused = store.link_runtime(models_dir)
        prepared = Prepared(models_dir, [e["model"] for e in manifest["models"]], refused)
    else:
        try:
            manifest = json.loads(manifest_file.read_text())
            prepared = prepare_runtime_models(manifest, installed, models_dir, data_dir / "runtime" / "hashes.json",
                                              environment, RevocationStore(data_dir, environment).trust())  # fmt: skip
        except ModelManifestError as error:
            plan["left_out"] = [["manifest.json", str(error)]]
            return "manifest_refused"
    plan["left_out"] = [list(pair) for pair in prepared.refused]
    plan["model_pack"] = {"pack_id": str(manifest.get("pack_id", "")), "version": str(manifest.get("version", ""))}
    if not prepared.models:
        return "no_verified_model"
    machine = probe or probe_machine(data_dir)
    cur = hardware.current(machine, hardware.load_record(data_dir), settings)
    choice = cur.choice
    hw_class = choice.hardware_class
    plan["hardware_class"] = hw_class
    plan["hardware_reason"] = class_reason(choice, chosen=choice is not cur.measured)
    if hw_class == "content_only":
        return "content_only_class"
    # MDL-17: a model with a projector reads photos, so the photo reader's memory is part of its estimate.
    reader = feature_bytes(settings, hw_class, photo_reader=True)
    entries = [
        Installed(e["model"], models_dir / e["model"] / e["file"]["name"],
                  int(e["projector"]["bytes"]) if e.get("projector") else 0, reader if e.get("projector") else 0)
        for e in manifest["models"] if e["model"] in prepared.models
    ]  # fmt: skip
    slot_dir = data_dir / "runtime" / "kv-slots"  # the slot erase action needs it (DAT-14, D128)
    slot_dir.mkdir(parents=True, exist_ok=True)
    chosen, left_out = attempts(entries, hw_class, machine.ram_bytes, local, settings, models_dir,
                                machine.available_bytes, slot_dir)  # fmt: skip
    plan["left_out"] += [list(pair) for pair in left_out]
    if not chosen:
        return "no_model_fits"
    plan["attempts"] = chosen
    plan["model"] = chosen[0]["model"]
    plan["benchmark_due"] = cur.benchmark_due
    return None


@dataclass
class RuntimeLink:
    """What the core knows of the runtime the shell supervises: the plan, the attached launch and the losses seen."""

    settings: Settings
    plan: dict[str, Any]
    alive: Callable[[], Awaitable[bool]] | None = None  # the attached runtime still listens
    on_change: Callable[[str, str], None] | None = None  # (readiness, runtime component), on every change
    clock: Callable[[], float] = time.monotonic
    generation: int = 0  # one per control.runtime_endpoint
    model: str | None = None
    losses: dict[str, list[float]] = field(default_factory=dict)  # per model: when the core saw it lost
    state: str = field(init=False)
    since: float = field(init=False)
    _changed: asyncio.Event = field(init=False, default_factory=asyncio.Event)
    _watcher: asyncio.Task[None] | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.state, self.since = ("waiting" if self.plan.get("model") else "none"), self.clock()

    def readiness(self) -> tuple[str, str]:
        return STATES[self.state]

    @property
    def on_fallback(self) -> bool:
        """The attached runtime serves a model other than the plan's first choice: the next smaller one (MDL-19)."""
        return self.model is not None and self.model != self.plan.get("model")

    def _set(self, state: str) -> None:
        changed = state != self.state
        self.state, self.since = state, self.clock()
        old, self._changed = self._changed, asyncio.Event()
        old.set()
        if changed and self.on_change is not None:
            self.on_change(*STATES[state])

    # --- what the control channel and the requests report ---------------------------------------------------------

    def attaching(self, model: str) -> int:
        """A new endpoint (a launch). One arriving while a launch was attached means the shell restarted it."""
        if self.state in ("ready", "loading", "failed") and self.model is not None:
            self._record_loss(self.model)
        self.generation += 1
        self.model = model
        self._set("loading")
        return self.generation

    def attached(self, generation: int, ready: bool) -> None:
        if generation == self.generation:  # a newer launch supersedes an older one's touch generation
            self._set("ready" if ready else "failed")

    def lost(self, generation: int) -> None:
        """The launch `generation` stopped answering. Restarting until the plan's policy is spent (MDL-19)."""
        if generation != self.generation or self.state not in ("ready", "loading", "failed") or self.model is None:
            return
        spent = self._record_loss(self.model)
        models = [a["model"] for a in self.plan.get("attempts", [])]
        smaller = self.model in models and models.index(self.model) + 1 < len(models)
        self._set("gave_up" if spent and not smaller else "restarting")

    def _record_loss(self, model: str) -> bool:
        """Note a loss; True when it is past the policy's restarts inside the window (the shell stops restarting)."""
        restart = self.plan["restart"]
        now = self.clock()
        recent = [t for t in self.losses.get(model, []) if now - t < float(restart["window_s"])] + [now]
        self.losses[model] = recent
        return len(recent) > int(restart["max_restarts"])

    def tick(self) -> None:
        """Waiting for an endpoint longer than `runtime_restart_wait_s` means the shell has given up."""
        waiting = self.state in ("waiting", "restarting")
        if waiting and self.clock() - self.since > self.settings.runtime_restart_wait_s:
            self._set("gave_up")

    # --- what the engine and the watcher ask ----------------------------------------------------------------------

    async def await_restart(self) -> bool:
        """After a request found the runtime down: True once a restarted runtime is ready (or the runtime still
        answers), False when it does not come back in time or the policy is spent (MDL-19)."""
        generation = self.generation
        if self.state in ("ready", "failed"):
            if self.alive is None or await self.alive():
                return self.state == "ready"  # a dropped connection to a runtime that still answers
            self.lost(generation)
        deadline = self.clock() + self.settings.runtime_restart_wait_s
        while self.state not in SETTLED:
            left = deadline - self.clock()
            if left <= 0:
                self.tick()
                break
            try:
                await asyncio.wait_for(self._changed.wait(), left)
            except TimeoutError:
                continue
        return self.state == "ready"

    def start(self) -> None:
        if self._watcher is None and self.plan.get("model"):
            self._watcher = asyncio.get_running_loop().create_task(self._watch())

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self.settings.runtime_liveness_interval_s)
            self.tick()
            if self.state == "ready" and self.alive is not None:
                generation = self.generation
                if not await self.alive():
                    self.lost(generation)

    async def close(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
            self._watcher = None
