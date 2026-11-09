"""Cooperative time slices for symbolic work (VER-04, VER-27).

SymPy cannot be interrupted from outside, so a slice installs a per-thread trace function that raises
`SliceExpired` at a Python function call after the deadline. Checkers catch it and answer `unknown` with
reason `timeout`, never a verdict they did not reach. The host's wall-clock deadline (TOL-11) remains the backstop.

A slice is measured in the calling thread's CPU time, not wall time: equal arguments must give a byte-identical
result (VER-01), and a wall-clock slice turned a verdict into `unknown`/`timeout` whenever the machine was busy
(other processes, other threads holding the GIL), a different golden case each run. The CPU clock costs about six
times a monotonic read, so it is read every `CHECK_EVERY` calls.

The cyclic collector runs in whichever thread's allocation starts it, and its cost follows the size of the whole heap,
not the question being checked: a slice does not charge it. The per-call budget below still does (SEC-08).
"""

from __future__ import annotations

import gc
import sys
import threading
import time
import tracemalloc
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import FrameType
from typing import Any


class SliceExpired(Exception):
    """The time slice ran out before the computation finished."""


CHECK_EVERY = 32  # calls between reads of the thread's CPU clock


@contextmanager
def time_slice(seconds: float, clock: Callable[[], float] = time.thread_time) -> Iterator[None]:
    deadline = clock() + seconds
    previous = sys.gettrace()
    thread = threading.get_ident()
    calls = 0
    collecting = False
    collection_started = 0.0
    collected = 0.0  # this thread's CPU time in the collector, since the slice began

    def on_collection(phase: str, info: dict[str, int]) -> None:
        nonlocal collecting, collection_started, collected
        if threading.get_ident() != thread:
            return  # another thread's collection is not on this thread's clock
        if phase == "start":
            collecting = True
            collection_started = clock()
        elif collecting:
            collected += clock() - collection_started
            collecting = False

    def tracer(frame: FrameType, event: str, arg: Any) -> None:
        nonlocal calls
        # Raising during a collection would be lost, and would remove this trace function with the slice still open.
        if event == "call" and not collecting and frame.f_code is not on_collection.__code__:
            calls += 1
            if calls % CHECK_EVERY == 0 and clock() - collected > deadline:
                raise SliceExpired
        return None  # no line tracing: only calls are checked

    gc.callbacks.append(on_collection)
    sys.settrace(tracer)
    try:
        yield
    finally:
        sys.settrace(previous)
        gc.callbacks.remove(on_collection)


# --- the per-call budget (SEC-08, TOL-24) ------------------------------------------------------------------------

try:  # POSIX only: Windows has no `resource` module, and the worker must still start there
    import resource as _resource
except ImportError:  # pragma: no cover - exercised on Windows
    _resource = None  # type: ignore[assignment]

WATCH_S = 0.05  # how often the watchdog reads the call's CPU time and memory


class BudgetExceeded(BaseException):  # not an Exception, so a function's broad `except` cannot hide it
    """The call used more CPU time or memory than its limit; `limit` is "cpu" or "memory"."""

    def __init__(self, limit: str) -> None:
        super().__init__(limit)
        self.limit = limit


def peak_rss_mb() -> float | None:
    """The process's peak resident memory in MB, or None where the platform cannot say."""
    if _resource is None:
        return None
    peak = float(_resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss)
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def _cpu_clock(thread_id: int) -> Callable[[], float]:
    """The call thread's own CPU clock where the platform has one (Linux), else the process's CPU time."""
    getter = getattr(time, "pthread_getcpuclockid", None)
    if getter is not None:
        try:
            clock = getter(thread_id)
            return lambda: time.clock_gettime(clock)
        except OSError:
            pass
    return time.process_time


def enforcement() -> dict[str, str]:
    """How each per-call limit is enforced on this platform; reported at start, never fatal."""
    cpu = "the call thread's CPU time" if hasattr(time, "pthread_getcpuclockid") else "the process's CPU time"
    out = {"cpu_ms": f"enforced: {cpu} during the call"}
    if _resource is None:
        out["memory_mb"] = "not fully enforced: Python allocation growth only; no native resident-memory reading"
    else:
        out["memory_mb"] = "enforced: traced Python allocation growth and process peak resident memory growth"
    return out


@contextmanager
def call_budget(cpu_ms: int, memory_mb: int, watch_s: float = WATCH_S) -> Iterator[None]:
    """Run a call within `cpu_ms` of CPU time and `memory_mb` of memory growth. A watchdog thread reads both every
    `watch_s`; past either limit it installs a trace function that raises `BudgetExceeded` in the call's thread at
    its next Python function call, so a running call pays nothing until then. A single native operation is not
    interrupted (the grammar bounds those, and the host's deadline kills a worker that outlives it, TOL-26)."""
    target = threading.get_ident()
    own_tracing = not tracemalloc.is_tracing()
    if own_tracing:
        tracemalloc.start(1)
    start_allocated, start_peak = tracemalloc.get_traced_memory()
    cpu_now = _cpu_clock(target)
    start_cpu, start_mem = cpu_now(), peak_rss_mb()
    done = threading.Event()
    over: list[str] = []

    def exceeded() -> str | None:
        if cpu_now() - start_cpu > cpu_ms / 1000:
            return "cpu"
        # RSS is a lifetime high-water mark: earlier calls may have set a peak that this call never exceeds.
        # Python allocations must still be bounded when the allocator reuses those resident pages.
        current, peak = tracemalloc.get_traced_memory()
        allocated = max(current - start_allocated, peak - (start_allocated if own_tracing else start_peak))
        if allocated > memory_mb * 1024 * 1024 or (
            start_mem is not None and (peak_rss_mb() or 0.0) - start_mem > memory_mb
        ):
            return "memory"
        return None

    def tracer(frame: FrameType, event: str, arg: Any) -> None:
        if over and event == "call" and threading.get_ident() == target:
            raise BudgetExceeded(over[0])
        return None

    def watch() -> None:
        while not done.wait(watch_s):
            limit = exceeded()
            if limit is not None:
                over.append(limit)
            if over:
                threading.settrace_all_threads(tracer)  # reaches the running call thread (Python 3.12)
                threading.settrace(None)  # threads started later are not traced
                return

    watchdog = threading.Thread(target=watch, name="call-budget", daemon=True)
    watchdog.start()
    try:
        yield
        # Short calls can finish before the watchdog's first tick.
        if limit := exceeded():
            raise BudgetExceeded(limit)
    finally:
        done.set()
        watchdog.join()
        if over:
            sys.settrace(None)
        if own_tracing:
            tracemalloc.stop()
