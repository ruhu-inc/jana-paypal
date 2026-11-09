"""Ownership checks for work that outlives an await on a shared, switchable learner desk."""

from __future__ import annotations

from dataclasses import dataclass

from agent_core.learner.service import Learner


class ProfileChanged(LookupError):
    """An operation's learner was closed, replaced, or invalidated while it was running."""


@dataclass(frozen=True)
class ProfileScope:
    learner: Learner
    generation: int

    @classmethod
    def capture(cls, learner: Learner | None, generation: int) -> ProfileScope:
        if learner is None:
            raise ProfileChanged("no learner database is open")
        return cls(learner, generation)

    def require(self, learner: Learner | None, generation: int) -> None:
        # Identity catches replacement connections, including reopening the same profile; generation also
        # catches close/reopen when the runtime retains the same learner for a draining background job.
        if learner is not self.learner or generation != self.generation:
            raise ProfileChanged("the operation's profile is no longer open")
