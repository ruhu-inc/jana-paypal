"""The roles and who may address the student (AGT-01, AGT-14).

Only a role whose manifest has `audience: student` may produce what the student sees, and only the Coach has it.
The engine runs each delegation with the role as the current speaker; while a team role (or an auxiliary role) is
the speaker, no text, board, card or visual part can open on the session stream. The engine itself (templates, its
own short lines, notices) speaks as `engine`. Every student-facing part records who it came from, so a test or an
audit can show that nothing reached the student from any role but the Coach."""

from __future__ import annotations

import contextvars
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

STUDENT_FACING = frozenset({"text", "board", "card", "visual"})
ENGINE = "engine"
ORIGINS_KEPT = 4096
SPEAKER: contextvars.ContextVar[str] = contextvars.ContextVar("jana_speaker", default=ENGINE)


class AudienceError(AssertionError):
    """A part a student would see was opened while a role without a student audience was speaking."""


@contextmanager
def speaking(role: str) -> Iterator[None]:
    token = SPEAKER.set(role)
    try:
        yield
    finally:
        SPEAKER.reset(token)


@dataclass
class AudienceGuard:
    """The session stream's part guard: allows student-facing parts only from the student-audience roles."""

    audiences: dict[str, str]  # role -> audience, from the manifests
    # (part kind, speaker) for the most recent parts opened, bounded so a long session does not grow it forever.
    origins: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=ORIGINS_KEPT))

    def __call__(self, kind: str) -> None:
        speaker = SPEAKER.get()
        self.origins.append((kind, speaker))
        if kind in STUDENT_FACING and speaker != ENGINE and self.audiences.get(speaker) != "student":
            raise AudienceError(f"a {kind} part was opened while the {speaker} role was speaking")


def audiences(manifests: dict[str, Any]) -> dict[str, str]:
    return {role: str(m.meta["audience"]) for role, m in manifests.items()}
