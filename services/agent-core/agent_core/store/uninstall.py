"""Uninstall and data removal (PLT-24, SEC-17, DAT-09, DAT-11).

The uninstaller removes the binaries, the packs and models, and every cache, and asks the adult whether to keep or
delete the learner data ("keep" or "delete"). Deleting removes the device and learner databases, every other file of
the data folder, and every key the app holds by reference (the key store's `jana.*` entries: the install marker, the
device key, profile keys, the service credential). Keeping leaves the learner data and its keys for a reinstall.

Before deleting, `preflight` says whether an enabled personal online feature still holds queued tombstones (DAT-11):
the uninstaller then asks the adult to connect once so they are delivered, and says so when that is not possible.

The Windows uninstallers run `agent-core --uninstall keep|delete --data-dir DIR [--keystore-dir DIR]`; on macOS and
Linux the app offers the same from Settings before the app is moved to the bin.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from agent_core.store.keystore import KeyStore

KEY_PREFIX = "jana-meta."
# What is learner data (kept on "keep"); everything else in the data folder is packs, models, caches or logs.
LEARNER_DATA = ("device.db", "device.db-wal", "device.db-shm", "profiles", "updates/snapshots")
CHOICES = ("keep", "delete")


def preflight(data_dir: Path) -> dict[str, Any]:
    """Queued tombstones an enabled personal online feature still has to deliver before data is deleted."""
    device = data_dir / "device.db"
    if not device.is_file():
        return {"tombstones_pending": 0}
    conn = sqlite3.connect(f"{device.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT count(*) FROM outbox WHERE kind = 'tombstone' AND delivered_at IS NULL").fetchone()
    except sqlite3.Error:
        row = (0,)
    finally:
        conn.close()
    return {"tombstones_pending": int(row[0])}


def key_refs(keys: KeyStore) -> list[str]:
    """Every reference the key store holds for this app."""
    listed: Iterable[str] = keys.refs() if hasattr(keys, "refs") else ()
    return sorted(ref for ref in listed if ref.startswith(KEY_PREFIX))


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=False)
    elif path.exists() or path.is_symlink():
        path.chmod(0o644)
        path.unlink()


def uninstall(choice: str, data_dir: Path, keys: KeyStore | None, install_dirs: Iterable[Path] = ()) -> list[str]:
    """Remove what the choice says; returns what was removed (for the uninstaller's log, no content)."""
    if choice not in CHOICES:
        raise ValueError("choose keep or delete")
    removed = []
    for folder in install_dirs:
        if folder.exists():
            _remove(folder)
            removed.append(str(folder))
    if data_dir.exists():
        for entry in sorted(data_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            relative = entry.relative_to(data_dir).as_posix()
            learner = any(relative == kept or relative.startswith(kept + "/") or kept.startswith(relative + "/")
                          for kept in LEARNER_DATA)  # fmt: skip
            if choice == "keep" and learner:
                continue
            if entry.is_file() or entry.is_symlink():
                _remove(entry)
                removed.append(relative)
            elif entry.is_dir() and not any(entry.iterdir()):
                entry.rmdir()
        if choice == "delete":
            shutil.rmtree(data_dir, ignore_errors=True)
    if choice == "delete" and keys is not None:
        for ref in key_refs(keys):
            keys.delete(ref)
            removed.append(f"key {ref}")
    return removed
