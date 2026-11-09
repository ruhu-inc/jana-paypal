"""Atomic canonical saves shared by the CLI and studio; revisions compare the whole source file."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class Conflict(ValueError):
    """The source changed since the editor opened it. Keep the unsaved draft."""


def revision(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


@contextmanager
def locked(path: Path) -> Iterator[None]:
    directory = Path(tempfile.gettempdir()) / "jana-authoring-locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    with (directory / name).open("a+b") as stream:
        if sys.platform == "win32":
            import msvcrt

            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform == "win32":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def atomic_write(path: Path, text: str, expected: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path):
        if expected is not None and revision(path) != expected:
            raise Conflict("This file changed on disk. Reload it or copy your draft before retrying.")
        descriptor, name = tempfile.mkstemp(prefix=".studio-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(text.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            if path.exists():
                os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
            if os.name != "nt":
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


def recover_changes(root: Path) -> None:
    """Finish an interrupted source change set, refusing to overwrite an unrelated external edit."""
    import json

    journal = root / "reviews/studio/.pending.json"
    if any(p.is_symlink() for p in (root / "reviews", journal.parent, journal)):
        raise ValueError("Unsafe authoring transaction path")
    if not journal.exists():
        return
    if journal.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Authoring transaction is too large")
    with locked(root / "content/curriculum"):
        if not journal.exists():
            return
        entries = json.loads(journal.read_text())
        for row in entries:
            path = root / row["path"]
            if (
                Path(row["path"]).is_absolute()
                or ".." in Path(row["path"]).parts
                or not path.resolve().is_relative_to(root)
            ):
                raise ValueError("Unsafe authoring transaction target")
            if any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(root)):
                raise ValueError("Symbolic authoring transaction target")
            wanted = hashlib.sha256(row["text"].encode()).hexdigest() if row["text"] is not None else "missing"
            if revision(path) not in (row["expected"], wanted):
                raise Conflict("An interrupted change set conflicts with an external edit; preserve both for recovery")
        for row in entries:
            path = root / row["path"]
            if row["text"] is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, row["text"])
        journal.unlink()


def atomic_changes(root: Path, entries: list[dict[str, Any]]) -> None:
    """Journal a bounded multi-file change set before the first replacement (including source deletion)."""
    import json

    recover_changes(root)
    journal = root / "reviews/studio/.pending.json"
    text = json.dumps(entries, ensure_ascii=False)
    if len(text.encode()) > 4 * 1024 * 1024:
        raise ValueError("Split this change set into smaller releases")
    with locked(root / "content/curriculum"):
        for row in entries:
            path = root / row["path"]
            if revision(path) != row["expected"]:
                raise Conflict("Sources changed since the change-set preview; preview again")
        atomic_write(journal, text, "missing")
    recover_changes(root)
