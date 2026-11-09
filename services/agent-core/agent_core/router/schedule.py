"""Local scheduling and per-role concurrency (MDL-22).

Local generations share the runtime's slots (router config `local.slots` for the hardware class). Requests wait in
one queue ordered by role priority (the config's `priority`: the Coach highest, the Reflector lowest), then by
arrival. A request that is not background, arriving while every slot is busy, preempts the lowest-priority background
generation below it: that call is cancelled and queued again, which is safe because a model call has no side
effects until the engine runs its tool calls (MDL-10). Each role's `concurrency` bounds its calls in flight, local or
cloud. A cancelled generation that has not let go of its slot within `router_preempt_start_ms` is not waited for:
the foreground request starts anyway."""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass(eq=False)
class _Ticket:
    role: str
    priority: int
    background: bool
    local: bool
    seq: int
    granted: asyncio.Future[None]
    task: asyncio.Future[Any] | None = None
    preempted: bool = False


@dataclass
class Scheduler:
    slots: int
    preempt_wait_s: float
    limits: dict[str, int] = field(default_factory=dict)  # role -> calls in flight
    running: list[_Ticket] = field(default_factory=list)
    waiting: list[_Ticket] = field(default_factory=list)
    preemptions: int = 0
    _seq: itertools.count[int] = field(default_factory=itertools.count)

    async def run(
        self, role: str, priority: int, background: bool, local: bool, call: Callable[[], Awaitable[T]]
    ) -> tuple[T, int]:
        """Run `call` when the queue allows it; returns its result and the milliseconds spent queued."""
        queued = 0.0
        while True:
            loop = asyncio.get_running_loop()
            ticket = _Ticket(role, priority, background, local, next(self._seq), loop.create_future())
            started = time.monotonic()
            await self._acquire(ticket)
            queued += time.monotonic() - started
            try:
                if ticket.preempted:  # preempted between its grant and its start: queue again
                    continue
                ticket.task = asyncio.ensure_future(call())
                return await ticket.task, int(queued * 1000)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if not ticket.preempted or (current is not None and current.cancelling()):
                    raise  # the caller itself was cancelled
            finally:
                self._release(ticket)

    def in_flight(self, role: str) -> int:
        return sum(1 for t in self.running if t.role == role)

    def _role_full(self, ticket: _Ticket) -> bool:
        limit = self.limits.get(ticket.role)
        return limit is not None and self.in_flight(ticket.role) >= limit

    def _fits(self, ticket: _Ticket) -> bool:
        if self._role_full(ticket):
            return False
        return not ticket.local or sum(1 for t in self.running if t.local) < self.slots

    def _dispatch(self) -> None:
        for ticket in sorted(self.waiting, key=lambda t: (-t.priority, t.seq)):
            if self._fits(ticket):
                self._grant(ticket)

    def _grant(self, ticket: _Ticket) -> None:
        self.waiting.remove(ticket)
        self.running.append(ticket)
        ticket.granted.set_result(None)

    def _release(self, ticket: _Ticket) -> None:
        if ticket in self.running:
            self.running.remove(ticket)
            self._dispatch()

    def _preempt_for(self, ticket: _Ticket) -> _Ticket | None:
        """Cancel the lowest-priority background generation below a waiting foreground request, if any."""
        if ticket.background or not ticket.local:
            return None
        if self._role_full(ticket):
            return None  # the role's own limit holds it back; a free slot would not help
        victims = [t for t in self.running if t.local and t.background and not t.preempted and
                   t.priority < ticket.priority]  # fmt: skip
        if not victims:
            return None
        victim = min(victims, key=lambda t: (t.priority, -t.seq))
        victim.preempted = True
        if victim.task is not None:
            victim.task.cancel()
        self.preemptions += 1
        return victim

    async def _acquire(self, ticket: _Ticket) -> None:
        self.waiting.append(ticket)
        self._dispatch()
        try:
            if ticket.granted.done():
                return
            if self._preempt_for(ticket) is None:
                await ticket.granted
                return
            try:
                await asyncio.wait_for(asyncio.shield(ticket.granted), self.preempt_wait_s)
            except TimeoutError:
                if not ticket.granted.done():
                    self._grant(ticket)  # the cancelled generation has not let go of its slot yet: start anyway
        except asyncio.CancelledError:
            if ticket in self.waiting:
                self.waiting.remove(ticket)
            self._release(ticket)
            raise
