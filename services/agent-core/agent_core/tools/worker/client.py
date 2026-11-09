"""One tool worker process, spoken to over stdio with newline-delimited JSON-RPC (TOL-24)."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_core.contracts import errors as contract_errors

log = logging.getLogger(__name__)
HOST_PROTOCOL = {"major": 1, "minor": 0}  # ARC-29: the worker must have this major and at least this minor
HOST_VERSION = "0.1.0"


class WorkerExited(Exception):
    """The worker process ended or its stream closed."""


class WorkerCallError(Exception):
    """The worker answered with a JSON-RPC error."""

    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data or {}


@dataclass
class WorkerProcess:
    argv: Sequence[str]
    frame_limit: int = 4 * 1024 * 1024
    env: dict[str, str] | None = None
    cwd: str | None = None
    on_notification: Callable[[dict[str, Any]], None] | None = None
    scratch: Path | None = None  # per-launch scratch directory, deleted at exit (SEC-06)
    sandbox_mechanism: str = "none"
    reduced_protection: bool = True
    pass_fds: tuple[int, ...] = ()
    process: asyncio.subprocess.Process | None = None
    dropped_lines: int = 0
    hello_result: dict[str, Any] | None = None
    _pending: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    _ids: itertools.count[int] = field(default_factory=itertools.count)
    _reader: asyncio.Task[None] | None = None
    _stderr: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            cwd=self.cwd,
            limit=self.frame_limit + 2,
            pass_fds=self.pass_fds,
        )
        for fd in self.pass_fds:  # the child has its copy; ours is no longer needed
            with contextlib.suppress(OSError):
                os.close(fd)
        self._reader = asyncio.create_task(self._read())
        self._stderr = asyncio.create_task(self._drain_stderr())

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def _drain_stderr(self) -> None:
        assert self.process and self.process.stderr
        async for line in self.process.stderr:
            log.debug("worker stderr: %s", line.decode(errors="replace").rstrip()[:500])

    async def _read(self) -> None:
        assert self.process and self.process.stdout
        try:
            while True:
                try:
                    line = await self.process.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    log.warning("worker frame over the limit; recycling")
                    self.kill()
                    break
                if not line:
                    break
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    message = None
                if not isinstance(message, dict):
                    self.dropped_lines += 1  # TOL-24: dropped and counted
                    continue
                if "id" in message and ("result" in message or "error" in message):
                    future = self._pending.pop(str(message["id"]), None)
                    if future and not future.done():
                        future.set_result(message)
                elif self.on_notification:
                    self.on_notification(message)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(WorkerExited("the worker exited"))
            self._pending.clear()

    async def request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        if not self.alive or self.process is None or self.process.stdin is None:
            raise WorkerExited("the worker is not running")
        request_id = str(next(self._ids))
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        problems = contract_errors("tools/worker-protocol#/$defs/Request", message)
        if problems:
            raise ValueError(f"host request breaks the worker protocol: {problems[0]}")
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        frame = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
        try:
            self.process.stdin.write(frame)
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            self._pending.pop(request_id, None)
            raise WorkerExited("the worker closed its input") from error
        try:
            reply = await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)
        if "error" in reply:
            failure = reply["error"]
            raise WorkerCallError(failure["code"], failure["message"], failure.get("data"))
        result: dict[str, Any] = reply["result"]
        return result

    async def hello(
        self, placement: str = "device", protocol: dict[str, int] | None = None, timeout: float = 10
    ) -> dict[str, Any]:
        params = {"protocol": protocol or HOST_PROTOCOL, "host_version": HOST_VERSION, "placement": placement}
        self.hello_result = await self.request("worker.hello", params, timeout)
        return self.hello_result

    def kill(self) -> None:
        if self.alive and self.process:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()

    async def close(self, grace: float = 1.0) -> None:
        if self.alive:
            with contextlib.suppress(WorkerExited, WorkerCallError, TimeoutError):
                await self.request("worker.shutdown", {}, timeout=grace)
            if self.process:
                try:
                    await asyncio.wait_for(self.process.wait(), grace)
                except TimeoutError:
                    self.kill()
        if self.process and self.process.returncode is None:
            await self.process.wait()
        for task in (self._reader, self._stderr):
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self.scratch:
            shutil.rmtree(self.scratch, ignore_errors=True)
