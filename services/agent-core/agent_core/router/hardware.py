"""The hardware class over the life of an install (PLT-08, PLT-09, PLT-29, MDL-16).

`probe.choose_class` places one probe and one benchmark in a class. This module keeps that choice: the benchmark's
result is recorded in `<data_dir>/runtime/hardware.json` with the probe's fingerprint, so a later start uses the
measured class without measuring again, and a changed memory, CPU or GPU (a different fingerprint) makes the class
provisional again and the benchmark due (PLT-09). The record also holds the student's class choice from Settings →
Tutor models (PLT-09): a lower class is taken as chosen; a higher one than the benchmark allows only once confirmed
after the warning; and no choice lifts a machine below the minimum laptop's hardware checks, since no smaller model
exists to run there (D58).

On the content-only class the tutor has one of three paths (PLT-29): a school server on the LAN when one is
reachable (PLT-25), else the online tier when an adult has enabled it (MDL-24), else none, and then no model call is
made at all. `report` is what the API tells the client about all this (the proposed `ModelCatalogue.hardware`).

Everything here is offline: the benchmark talks only to the loopback runtime.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from agent_core.config import Settings
from agent_core.router.probe import CLASSES, Benchmark, ClassChoice, Probe, Shortfall, choose_class, run_benchmark

RECORD_VERSION = 1
TutorPath = Literal["local", "school_server", "online", "none"]
OverrideCheck = Literal["allowed", "needs_confirmation", "refused"]
HIGHER_CLASS_WARNING = "class_override.too_slow"  # the client's copy key for PLT-09's warning
HARDWARE_SHORTFALLS = frozenset({"ram", "cpu", "disk"})  # what a class choice cannot get round (D58)


def record_path(data_dir: Path) -> Path:
    return data_dir / "runtime" / "hardware.json"


@dataclass(frozen=True)
class HardwareRecord:
    """What `hardware.json` holds: the probe the class was measured on, the benchmark and the student's choice."""

    fingerprint: str
    probe: dict[str, Any]
    benchmark: dict[str, Any] | None = None
    measured_at_ms: int | None = None
    override: str | None = None  # a class chosen in Settings → Tutor models (PLT-09)
    override_confirmed: bool = False  # the warning for a higher class was confirmed

    def to_json(self) -> dict[str, Any]:
        return {"record_version": RECORD_VERSION, "fingerprint": self.fingerprint, "probe": self.probe,
                "benchmark": self.benchmark, "measured_at_ms": self.measured_at_ms, "override": self.override,
                "override_confirmed": self.override_confirmed}  # fmt: skip

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> HardwareRecord:
        return cls(str(data["fingerprint"]), dict(data["probe"]), data.get("benchmark"), data.get("measured_at_ms"),
                   data.get("override"), bool(data.get("override_confirmed")))  # fmt: skip


def load_record(data_dir: Path) -> HardwareRecord | None:
    """The record, or None when there is none or it cannot be read (the class is then predicted again)."""
    try:
        data = json.loads(record_path(data_dir).read_text())
        if data.get("record_version") != RECORD_VERSION:
            return None
        return HardwareRecord.from_json(data)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_record(data_dir: Path, record: HardwareRecord) -> None:
    target = record_path(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(record.to_json(), indent=1))
    os.replace(tmp, target)


@dataclass(frozen=True)
class Current:
    """The class this start uses, why, and whether the benchmark should run once the runtime is ready."""

    choice: ClassChoice
    measured: ClassChoice  # before the student's choice
    benchmark: Benchmark | None
    benchmark_due: bool
    hardware_changed: bool
    override: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


def current(probe: Probe, record: HardwareRecord | None, settings: Settings) -> Current:
    """The class for this start: measured when the record's benchmark was taken on this hardware, else predicted from
    the probe and the benchmark due (first run, or after a hardware change, PLT-09). A machine below the minimum
    laptop's hardware checks needs no benchmark: no model runs there."""
    same = record is not None and record.fingerprint == probe.fingerprint()
    bench = Benchmark(**record.benchmark) if same and record is not None and record.benchmark else None
    measured = choose_class(probe, bench, settings)
    below_by_hardware = any(s.check in HARDWARE_SHORTFALLS for s in measured.shortfalls)
    changed = record is not None and not same
    due = bench is None and not below_by_hardware
    override = record.override if same and record is not None else None
    chosen = measured
    notes: tuple[str, ...] = ()
    if override is not None:
        verdict = override_check(measured, override)
        confirmed = record is not None and record.override_confirmed
        if verdict == "allowed" or (verdict == "needs_confirmation" and confirmed):
            chosen = replace(measured, hardware_class=override, reasons=(*measured.reasons, f"chosen: {override}"))
        else:
            notes = (f"class choice {override} not applied: {verdict}",)
    return Current(chosen, measured, bench, due, changed, override, notes)


def override_check(measured: ClassChoice, requested: str) -> OverrideCheck:
    """PLT-09: a lower class is the student's to choose (from low the only lower class is content-only, D58); a higher
    one than the benchmark allows needs the warning confirmed; nothing lifts a machine below the minimum laptop's
    hardware checks, where no model can run."""
    if requested not in CLASSES:
        return "refused"
    have, want = CLASSES.index(measured.hardware_class), CLASSES.index(requested)
    if want <= have:
        return "allowed"
    if any(s.check in HARDWARE_SHORTFALLS for s in measured.shortfalls):
        return "refused"
    return "needs_confirmation"


def choose_override(data_dir: Path, probe: Probe, requested: str | None, confirmed: bool,
                    settings: Settings) -> OverrideCheck:  # fmt: skip
    """Record the student's class choice (None clears it). Returns the check; a refused or unconfirmed higher class is
    not recorded, and the client shows the PLT-09 warning (`HIGHER_CLASS_WARNING`) to ask for confirmation."""
    record = load_record(data_dir)
    if record is None or record.fingerprint != probe.fingerprint():
        record = HardwareRecord(probe.fingerprint(), probe.to_json())
    if requested is None:
        save_record(data_dir, replace(record, override=None, override_confirmed=False))
        return "allowed"
    verdict = override_check(current(probe, record, settings).measured, requested)
    if verdict == "refused" or (verdict == "needs_confirmation" and not confirmed):
        return verdict
    save_record(data_dir, replace(record, override=requested, override_confirmed=verdict == "needs_confirmation"))
    return verdict


async def measure(provider: Any, model: str, probe: Probe, data_dir: Path, settings: Settings,
                  clock_ms: Any = None) -> tuple[ClassChoice, Benchmark]:  # fmt: skip
    """PLT-08: one probe generation on `model` (the smallest installed one), the class it places this machine in, and
    the record kept for later starts. Only the loopback runtime is involved, so it runs offline. A kept class choice
    survives a re-measure on the same hardware."""
    bench = await run_benchmark(provider, model, settings)
    old = load_record(data_dir)
    keep = old if old is not None and old.fingerprint == probe.fingerprint() else None
    now = int(time.time() * 1000) if clock_ms is None else int(clock_ms())
    record = HardwareRecord(probe.fingerprint(), probe.to_json(), _bench_json(bench), now,
                            keep.override if keep else None, keep.override_confirmed if keep else False)  # fmt: skip
    save_record(data_dir, record)
    return choose_class(probe, bench, settings), bench


def _bench_json(bench: Benchmark) -> dict[str, Any]:
    return {"model": bench.model, "prompt_tokens_s": bench.prompt_tokens_s,
            "generation_tokens_s": bench.generation_tokens_s, "seconds": bench.seconds}  # fmt: skip


def suggest_lower(rows: Sequence[dict[str, Any]], hardware_class: str | None, settings: Settings) -> bool:
    """PLT-09: suggest a lower class when more than `class_suggest_lower_share` of the window's completed guided steps
    were over the class's step budget (AGT-28), once `health_min_turns` steps are recorded. Never below low: content-
    only is offered as a choice, not suggested."""
    if hardware_class not in ("mid", "high"):
        return False
    budget = settings.guided_step_budget_s[CLASSES.index(hardware_class) - 1]
    steps = [float(r["duration_ms"]) / 1000 for r in rows
             if (r.get("kind"), r.get("mode"), r.get("status")) == ("turn", "guided", "complete")
             and r.get("duration_ms") is not None]  # fmt: skip
    if len(steps) < settings.health_min_turns:
        return False
    return sum(s > budget for s in steps) / len(steps) > settings.class_suggest_lower_share


def tutor_path(hardware_class: str | None, cloud_tier_on: bool, school_server_reachable: bool) -> TutorPath:
    """PLT-29: where the tutor runs. On every class with a local model it is this computer. Below the minimum laptop
    it is a reachable school server (PLT-25), else the online tier an adult enabled (MDL-24), else nowhere: the
    content-only class, with no model call attempted. The same consent and notices apply as on any other class."""
    if hardware_class in ("low", "mid", "high"):
        return "local"
    if school_server_reachable:
        return "school_server"
    if cloud_tier_on:
        return "online"
    return "none"


def shortfall_json(short: Shortfall) -> dict[str, Any]:
    """One failed minimum-laptop check with the figures the unsupported-offline notice names (PLT-29, UXS-10)."""
    return {"check": short.check, "have": short.have, "need": short.need, "unit": short.unit}


def report(cur: Current | None, path: TutorPath, variant: dict[str, Any] | None) -> dict[str, Any]:
    """The hardware part of `GET /v1/models` (contract addition `ModelCatalogue.hardware`, PLT-04, PLT-08, PLT-09,
    PLT-29, OBS-15): the class and why, the failed checks with their figures, the benchmark, the student's choice,
    the tutor's path and the runtime build with any fallback and why."""
    if cur is None:
        return {"hardware_class": None, "basis": None, "provisional": True, "shortfalls": [], "benchmark": None,
                "benchmark_due": False, "class_choice": None, "tutor_path": path,
                "runtime": _runtime(variant)}  # fmt: skip
    choice = cur.choice
    if choice.shortfalls:
        basis = f"below_minimum_{choice.shortfalls[0].check}"
    else:
        basis = "chosen" if cur.override and choice is not cur.measured else ("predicted" if choice.provisional
                                                                              else "measured")  # fmt: skip
    bench = None
    if cur.benchmark is not None:
        bench = {"model": cur.benchmark.model, "generation_tokens_s": round(cur.benchmark.generation_tokens_s, 2),
                 "prompt_tokens_s": round(cur.benchmark.prompt_tokens_s, 2)}  # fmt: skip
    return {"hardware_class": choice.hardware_class, "basis": basis, "provisional": choice.provisional,
            "shortfalls": [shortfall_json(s) for s in choice.shortfalls], "benchmark": bench,
            "benchmark_due": cur.benchmark_due, "class_choice": cur.override, "tutor_path": path,
            "runtime": _runtime(variant)}  # fmt: skip


def _runtime(variant: dict[str, Any] | None) -> dict[str, Any] | None:
    if not variant or not variant.get("variant"):
        return None
    fallbacks = [{"variant": str(f.get("variant", ""))[:40], "reason": str(f.get("reason", ""))[:200]}
                 for f in variant.get("fallbacks", []) if isinstance(f, dict)][:8]  # fmt: skip
    return {"variant": str(variant["variant"])[:40], "fallbacks": fallbacks}
