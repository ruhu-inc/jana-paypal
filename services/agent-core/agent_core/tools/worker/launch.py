"""How the agent core starts a tool worker (ARC-02: the core starts and owns it)."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from agent_core.config import Settings
from agent_core.tools.worker.client import WorkerProcess
from agent_core.tools.worker.sandbox import host_environment, sandboxed, tool_job_limits


class SandboxUnavailable(OSError):
    """A release build's worker would run without its OS sandbox: it is not started (SEC-06)."""


def worker_limits(settings: Settings) -> dict[str, int | float]:
    """The configured limits the worker enforces and advertises (SEC-08, TOL-24)."""
    return {
        "cpu_ms": settings.worker_call_cpu_ms,
        "memory_mb": settings.worker_call_memory_mb,
        "max_expression_chars": settings.worker_max_expression_chars,
        "max_data_points": settings.worker_max_data_points,
        "max_frame_bytes": settings.worker_frame_limit_bytes,
        "process_memory_mb": settings.worker_process_memory_mb,
        "max_open_files": settings.worker_max_open_files,
        "max_file_mb": settings.worker_max_file_mb,
        "max_depth": settings.ver_max_depth,
        "equivalence_points": settings.ver_equivalence_points,
        "equivalence_min_valid": settings.ver_equivalence_min_valid,
        "numeric_digits": settings.ver_numeric_digits,
        "relative_tolerance": settings.ver_relative_tolerance,
        "symbolic_slice_ms": settings.ver_symbolic_slice_ms,
        "step_candidates": settings.ver_step_candidates,
        "default_sf": settings.ver_default_sf,
        "min_sf": settings.ver_min_sf,
        "carried_rounding_share": settings.ver_carried_rounding_share,
        "money_dp": settings.ver_money_dp,
        "angle_dp": settings.ver_angle_dp,
        "angle_tolerance_deg": settings.ver_angle_tolerance_deg,
        "probability_sf": settings.ver_probability_sf,
        "stats_max_points": settings.ver_stats_max_points,
        "plot_max_points": settings.ver_plot_max_points,
        "plot_chord_tolerance": settings.ver_plot_chord_tolerance,
        "acc_dp": settings.ver_acc_dp,
    }


def worker_argv(settings: Settings, python: str = sys.executable) -> list[str]:
    """Development command line; release builds run the signed worker bundle (D36, PLT-03) instead."""
    if getattr(sys, "frozen", False):
        suffix = ".exe" if sys.platform == "win32" else ""
        worker = Path(python).resolve().parent.parent / "tool-worker" / f"tool-worker{suffix}"
        if not worker.is_file():
            raise FileNotFoundError("the signed tool worker bundle is missing")
        return [str(worker), "--limits", json.dumps(worker_limits(settings), sort_keys=True)]
    return [python, "-m", "tool_worker", "--limits", json.dumps(worker_limits(settings), sort_keys=True)]


def process_factory(
    settings: Settings,
    argv: Sequence[str] | None = None,
    scratch_root: Path | None = None,
    sandbox: bool = True,
    *,
    require_sandbox: bool = False,
) -> Callable[[], WorkerProcess]:
    """Make worker processes, each under the OS sandbox with its own scratch directory (SEC-06). With
    `require_sandbox` (release builds) a worker the platform's sandbox cannot wrap is refused with
    `SandboxUnavailable` instead of starting with reduced protection, like the perception worker."""
    command = list(argv) if argv is not None else worker_argv(settings)

    def make() -> WorkerProcess:
        if scratch_root is not None:
            scratch_root.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="worker-", dir=scratch_root))
        wrapped = sandboxed(command, scratch, limits=tool_job_limits(settings)) if sandbox or require_sandbox else None
        if require_sandbox and (wrapped is None or wrapped.reduced_protection):
            shutil.rmtree(scratch, ignore_errors=True)
            raise SandboxUnavailable(wrapped.reason if wrapped else "no sandbox")
        process = WorkerProcess(
            wrapped.argv if wrapped else command,
            frame_limit=settings.worker_frame_limit_bytes,
            cwd=str(scratch),
            scratch=scratch,
            env=host_environment(scratch),
            pass_fds=wrapped.pass_fds if wrapped else (),
        )
        process.sandbox_mechanism = wrapped.mechanism if wrapped else "none"
        process.reduced_protection = wrapped.reduced_protection if wrapped else True
        return process

    return make
