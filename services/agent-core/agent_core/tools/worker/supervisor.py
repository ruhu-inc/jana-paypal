"""Supervision of the tool worker pool (TOL-26, ARC-24).

Workers are started and owned by the agent core. A worker that times out, breaches a limit, crashes or misses
pings is killed and replaced after a backoff; too many restarts in a window open the circuit, during which every
compute call fails `worker_unavailable` and compute tools are hidden, so no unchecked number is ever shown.
A protocol mismatch at the handshake opens the circuit too (TOL-24)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_core.config import Settings
from agent_core.tools.worker.client import WorkerCallError, WorkerExited, WorkerProcess
from agent_core.tools.worker.errors import BUSY, tool_error_code
from agent_core.tools.worker.launch import SandboxUnavailable

log = logging.getLogger(__name__)
# Errors after which the worker process is replaced (a limit breach or a crash), not just reported.
RECYCLE_AFTER = frozenset({"limit_exceeded", "timeout", "worker_crashed", "policy_denied"})


@dataclass(frozen=True)
class CallOutcome:
    """The worker's answer to one call: a result, or a tool error code (TABLE TOL-10a) with its detail."""

    result: dict[str, Any] | None = None
    error: str | None = None
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    action_taken: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class WorkerSupervisor:
    factory: Callable[[], WorkerProcess]
    settings: Settings
    placement: str = "device"
    clock: Callable[[], float] = time.monotonic
    functions: dict[str, dict[str, Any]] = field(default_factory=dict)
    restarts: list[float] = field(default_factory=list)
    circuit_open_until: float = 0.0
    _idle: asyncio.Queue[WorkerProcess] = field(default_factory=asyncio.Queue)
    _all: list[WorkerProcess] = field(default_factory=list)
    _missed: dict[int, int] = field(default_factory=dict)
    _respawning: int = 0
    _replacements: set[asyncio.Task[None]] = field(default_factory=set)
    _changed: asyncio.Event = field(default_factory=asyncio.Event)
    _closed: bool = False
    # OBS-16: told of each worker killed for a limit, a timeout, a crash or a policy denial (tool, code, call id), so
    # the core keeps a crash record; it never sees arguments or values.
    on_recycle: Callable[[str, str, str], object] | None = None
    # A release build's worker refused because its OS sandbox could not be applied (SEC-06): compute tools stay
    # hidden and the health panel shows the sandbox degraded and the worker down.
    sandbox_refused: str = ""

    # --- lifecycle ----------------------------------------------------------------------------------------

    async def start(self) -> None:
        for _ in range(self.settings.worker_pool_size):
            await self._add_worker()

    async def _add_worker(self) -> bool:
        if self._closed:
            return False
        try:
            worker = self.factory()
        except SandboxUnavailable as error:
            if not self.sandbox_refused:
                log.error("the tool worker is not started without its OS sandbox: %s", error)
            self.sandbox_refused = str(error) or "sandbox unavailable"
            return False
        try:
            await worker.start()
            hello = await worker.hello(self.placement, timeout=self.settings.worker_handshake_timeout_s)
        except WorkerCallError as error:
            log.error("worker handshake refused (code %s); opening the circuit", error.code)
            await worker.close()
            self._open_circuit()
            return False
        except (WorkerExited, TimeoutError, OSError):
            await worker.close()
            return False
        except asyncio.CancelledError:
            await worker.close()
            raise
        if self._closed:
            await worker.close()
            return False
        self.functions = {f["name"]: f for f in hello["functions"]}
        self._all.append(worker)
        self._return_worker(worker)
        return True

    async def close(self) -> None:
        self._closed = True
        self._changed.set()
        workers = list(self._all)
        self._all.clear()
        while not self._idle.empty():
            self._idle.get_nowait()
        # Join in-flight startup rather than cancelling subprocess creation. A late handshake sees _closed and
        # closes its process without putting it back into the pool.
        await asyncio.gather(*(worker.close() for worker in workers), *list(self._replacements))

    def _return_worker(self, worker: WorkerProcess) -> None:
        if not self._closed:
            self._idle.put_nowait(worker)
            self._changed.set()

    async def _take_worker(self) -> WorkerProcess | None:
        while not self._closed and not self.circuit_open:
            try:
                return self._idle.get_nowait()
            except asyncio.QueueEmpty:
                if not (self._all or self._respawning):
                    return None
                # No await between checking state and clearing the event: a returning worker or finished
                # replacement cannot notify in that gap. Failed startup wakes every waiter to return unavailable.
                self._changed.clear()
                await self._changed.wait()
        return None

    # --- circuit ------------------------------------------------------------------------------------------

    def _open_circuit(self) -> None:
        self.circuit_open_until = self.clock() + self.settings.worker_circuit_open_s
        self._changed.set()

    @property
    def circuit_open(self) -> bool:
        return self.clock() < self.circuit_open_until

    @property
    def reduced_protection(self) -> bool:
        """True when a worker runs without its OS sandbox (health panel: "Checks are running with reduced
        protection"; release builds for that platform fail the gate, SEC-06)."""
        return bool(self.sandbox_refused) or any(w.reduced_protection for w in self._all)

    def compute_available(self) -> bool:
        """Compute tools are offered only while the circuit is closed and a worker answered the handshake."""
        return not self._closed and not self.circuit_open and bool(self._all)

    def _begin_replacement(self, worker: WorkerProcess) -> asyncio.Task[None] | None:
        # Kill and remove synchronously before returning the failed check. Warm-up/backoff belongs to pool
        # recovery, not the rejected call's deadline; no next call can acquire this worker again.
        worker.kill()
        if self._closed:
            return None  # close() already owns every remaining worker and any pending startup
        if worker in self._all:
            self._all.remove(worker)
        self._missed.pop(id(worker), None)
        now = self.clock()
        window = self.settings.worker_circuit_window_s
        self.restarts = [t for t in self.restarts if now - t < window] + [now]
        if len(self.restarts) > self.settings.worker_circuit_restarts:
            log.error("%d worker restarts in %.0f s; opening the circuit", len(self.restarts), window)
            self._open_circuit()
        backoff = self.settings.worker_respawn_backoff_s
        delay = backoff[min(len(self.restarts) - 1, len(backoff) - 1)] if backoff else 0
        self._respawning += 1
        task = asyncio.create_task(self._respawn(worker, delay))
        self._replacements.add(task)
        task.add_done_callback(self._replacements.discard)
        self._changed.set()
        return task

    async def _respawn(self, worker: WorkerProcess, delay: float) -> None:
        try:
            with contextlib.suppress(Exception):
                await worker.close(grace=0.2)
            if not self._closed:
                await asyncio.sleep(delay)
                await self._add_worker()
        except Exception:
            log.exception("tool worker replacement failed")
        finally:
            self._respawning -= 1
            self._changed.set()

    async def _replace(self, worker: WorkerProcess) -> None:
        task = self._begin_replacement(worker)
        if task is not None:
            await task

    # --- calls --------------------------------------------------------------------------------------------

    async def call(self, name: str, version: int, args: dict[str, Any], check_id: str, deadline_ms: int,
                   call_id: str | None = None, conventions: dict[str, Any] | None = None) -> CallOutcome:  # fmt: skip
        if self._closed or self.circuit_open or not (self._all or self._respawning):
            return CallOutcome(error="worker_unavailable", detail="the checking worker is unavailable")
        params: dict[str, Any] = {
            "call_id": call_id or check_id,
            "name": name,
            "version": version,
            "args": args,
            "check_id": check_id,
            "deadline_ms": deadline_ms,
        }
        if conventions:
            params["conventions"] = conventions
        for attempt in (1, 2):
            worker = await self._take_worker()
            if worker is None:
                return CallOutcome(error="worker_unavailable", detail="the checking worker is unavailable")
            outcome = await self._call_once(worker, params, deadline_ms / 1000)
            if outcome.error in RECYCLE_AFTER:
                if self.on_recycle is not None:
                    with contextlib.suppress(Exception):  # a failed record never fails the call
                        self.on_recycle(name, str(outcome.error), str(params["call_id"]))
                self._begin_replacement(worker)
            else:
                self._return_worker(worker)
            if outcome.data.get("busy") and attempt == 1:
                continue  # BUSY is retried once (TOL-25)
            return outcome
        return outcome

    async def _call_once(self, worker: WorkerProcess, params: dict[str, Any], timeout: float) -> CallOutcome:
        try:
            result = await worker.request("fn.call", params, timeout=timeout)
        except TimeoutError:
            return CallOutcome(error="timeout", detail="the check did not finish before its deadline")
        except WorkerExited:
            return CallOutcome(error="worker_crashed", detail="the checking worker stopped during the call")
        except WorkerCallError as error:
            data = dict(error.data)
            if error.code == BUSY:
                data["busy"] = True
            code = tool_error_code(error.code)
            return CallOutcome(error=code, detail=error.message, data=data)
        return CallOutcome(result=result)

    # --- health -------------------------------------------------------------------------------------------

    async def ping_round(self) -> None:
        """Ping each idle worker once; recycle a worker that has missed the configured number of pings."""
        idle = [self._idle.get_nowait() for _ in range(self._idle.qsize())]
        for worker in idle:
            try:
                await worker.request("worker.ping", {}, timeout=self.settings.worker_ping_interval_s)
                self._missed[id(worker)] = 0
            except (TimeoutError, WorkerExited, WorkerCallError):
                self._missed[id(worker)] = self._missed.get(id(worker), 0) + 1
            if not worker.alive or self._missed.get(id(worker), 0) >= self.settings.worker_missed_pings_recycle:
                self._missed.pop(id(worker), None)
                await self._replace(worker)
            else:
                self._return_worker(worker)
