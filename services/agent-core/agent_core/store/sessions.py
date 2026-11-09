"""Session records on disk (API-17, DAT-06 provider built early; the SQLite stores, migrations and encryption at rest
arrive in M6 and M10).

One JSON file per session, written atomically (temporary file, fsync, rename), so a crash leaves either the previous
record or the new one. The record holds the normalised state the snapshot serves and the engine state needed to
continue; the API writes it after every completed board step, card answer, mode change and turn end. This is the
working-memory checkpoint of MEM-02 in S1 (the learner database holds it from S2, DAT-07). The previous good record is
kept beside the current one; a record that cannot be read falls back to it."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_ID = re.compile(r"^ses_[0-9A-HJKMNP-TV-Z]{26}$")


class SessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        if not _ID.match(session_id):
            raise ValueError("not a session id")
        return self.root / f"{session_id}.json"

    def save(self, session_id: str, record: dict[str, Any]) -> None:
        path = self._path(session_id)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        if _readable(path):
            os.replace(path, path.with_suffix(".json.prev"))  # the last good checkpoint stays until the next one
        os.replace(tmp, path)
        _sync_dir(self.root)

    def delete(self, session_id: str) -> None:
        """Profile deletion (DAT-09): the record and its previous good copy."""
        path = self._path(session_id)
        for candidate in (path, path.with_suffix(".json.prev"), path.with_suffix(".json.tmp")):
            candidate.unlink(missing_ok=True)

    def load(self, session_id: str) -> dict[str, Any] | None:
        path = self._path(session_id)
        for candidate in (path, path.with_suffix(".json.prev")):
            data = _readable(candidate)
            if data is not None:
                return data
        return None

    def load_all(self) -> dict[str, dict[str, Any]]:
        ids = {p.name.split(".")[0] for p in self.root.glob("ses_*.json*") if not p.name.endswith(".tmp")}
        records = {}
        for session_id in sorted(ids):
            record = self.load(session_id)
            if record is not None:
                records[session_id] = record
        return records


def _readable(path: Path) -> dict[str, Any] | None:
    """The record at `path`, or None when it is missing or torn."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _sync_dir(root: Path) -> None:
    """Make the renames durable (POSIX); a no-op where directories cannot be opened."""
    try:
        fd = os.open(root, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
