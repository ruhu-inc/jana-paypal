"""Bounded results and spill files (TOL-12).

`output` keeps the head (70%) and tail (20%) of an over-long result with a marker, and the full text goes to the
session's spill area under an opaque reference, never a path. Spill files are deleted with the session.
(Encryption with the learner store arrives with DAT-02 and SEC-18 in M10.)"""

from __future__ import annotations

import secrets
import shutil
from pathlib import Path

MARKER = (
    "[… {n} characters omitted; the full result is kept for the board. Ask for a narrower result instead of "
    "repeating this call.]"
)


class SpillStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _dir(self, session_id: str) -> Path:
        return self.root / session_id

    def save(self, session_id: str, text: str) -> str:
        ref = "spill_" + secrets.token_hex(12)
        directory = self._dir(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ref).write_text(text, encoding="utf-8")
        return ref

    def resolve(self, session_id: str, ref: str) -> str | None:
        if not ref.startswith("spill_") or not ref[6:].isalnum():
            return None
        path = self._dir(session_id) / ref
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def delete_session(self, session_id: str) -> None:
        shutil.rmtree(self._dir(session_id), ignore_errors=True)


def bound(text: str, limit: int, store: SpillStore, session_id: str) -> tuple[str, dict[str, object] | None]:
    """The output within `limit` characters, and the `truncated` record when it was cut."""
    if len(text) <= limit:
        return text, None
    head, tail = int(limit * 0.7), int(limit * 0.2)
    omitted = len(text) - head - tail
    marker = MARKER.format(n=omitted)
    budget = limit - len(marker) - 2
    head = min(head, max(budget - tail, 0))
    omitted = len(text) - head - tail
    marker = MARKER.format(n=omitted)
    ref = store.save(session_id, text)
    return f"{text[:head]}\n{marker}\n{text[len(text) - tail :]}", {"omitted": omitted, "spill_ref": ref}
