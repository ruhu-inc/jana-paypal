"""The steering queue (AGT-17, API-25).

A message the student sends while a turn runs is queued (at most `steer_queue_max`; a further message merges into
the last) and injected at the next Prepare after the newest tool result. It goes in as an app event whose engine
metadata alone is trusted (it carries the session's nonce); the student's words inside it are escaped and wrapped
exactly as ordinary student text, after the same input guard. A message the guard does not find in scope is never
injected: it is carried to start the next turn, where the guard answers it. If the turn ends first, whatever is queued
starts the next turn, in order."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from agent_core.prompts.untrusted import AppEvents


@dataclass
class Steer:
    message_id: str
    text: str
    client_message_id: str | None = None


@dataclass
class SteeringQueue:
    limit: int
    queued: list[Steer] = field(default_factory=list)
    carried: list[Steer] = field(default_factory=list)  # what the next turn starts with (guarded, or never reached)
    applied: list[Steer] = field(default_factory=list)

    def push(self, steer: Steer) -> Steer:
        """Queue one message; beyond the limit it merges into the last queued one (which keeps its id)."""
        if len(self.queued) >= self.limit:
            last = self.queued[-1]
            last.text = f"{last.text}\n{steer.text}"
            return last
        self.queued.append(steer)
        return steer

    def take(
        self, wrap: Callable[[str], str], admitted: Callable[[str], bool], events: AppEvents
    ) -> list[tuple[Steer, str]]:
        """Everything queued, as (steer, the app event to insert); a message `admitted` (the full input guard) does
        not let through is carried instead."""
        out = []
        for steer in self.queued:
            if not admitted(steer.text):
                self.carried.append(steer)
                continue
            out.append((steer, app_event(steer, wrap, events)))
            self.applied.append(steer)
        self.queued = []
        return out

    def end_of_turn(self) -> list[Steer]:
        """The turn ended: queued messages (never injected) and carried ones start the next turn, in order."""
        pending = [*self.carried, *self.queued]
        self.carried, self.queued = [], []
        return pending

    def pending(self) -> bool:
        return bool(self.queued or self.carried)


def app_event(steer: Steer, wrap: Callable[[str], str], events: AppEvents) -> str:
    """The trusted engine wrapper around untrusted student text: the nonce, `kind` and `message_id` come from the
    engine (PRM-19)."""
    return (f'<app-event nonce="{events.nonce}" kind="steer" message_id="{steer.message_id}">\n'
            "The student sent this while you were working. Take it into account in this reply.\n"
            f"{wrap(steer.text)}\n</app-event>")  # fmt: skip
