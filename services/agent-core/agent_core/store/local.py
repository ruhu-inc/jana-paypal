"""The stores of one installation, opened at start (DAT-01): `device.db` and the learner database of the local profile.

Profiles with names, PINs and per-profile keys arrive in M10; until then the installation has one local profile,
found by its directory under `profiles/` or created on first start. Every migration either database receives is
recorded in the device database's ledger (DAT-12).

A `device.db` that fails its integrity check never stops the app (UXS-47, DAT-13, D84): it is set aside beside the
original, never deleted, a fresh one takes the defaults of TABLE PLT-17b with every online feature off, and the
student profiles are listed again from their own learner databases, whose learning data is untouched.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from agent_core.store.ids import new_ulid
from agent_core.store.integrity import Recovery, open_checked, quick_check, set_aside
from agent_core.store.sqlite import open_database, record_migrations, schema_version, transaction


@dataclass
class LocalStores:
    device: sqlite3.Connection
    learner: sqlite3.Connection
    device_id: str
    profile_id: str
    learner_path: Path
    recovery: Recovery | None = None  # the learner database was damaged and recovered at open (DAT-13)
    device_recovery: Recovery | None = None  # device.db was damaged, set aside and its profile list rebuilt (UXS-47)

    def close(self) -> None:
        self.learner.close()
        self.device.close()


def _hardware_binding() -> bytes:
    """A hash of a stable hardware identifier (PLT-21 refines what is hashed and how it is salted)."""
    return hashlib.sha256(b"jana-hardware\0" + uuid.getnode().to_bytes(6, "big")).digest()


def _readable_profile(path: Path) -> tuple[str, str, str, int] | None:
    """A learner database's own profile row (id, name, avatar, created), read-only; None when it cannot be read."""
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        return None
    try:
        row = conn.execute("SELECT profile_id, display_name, avatar, created_at FROM profile LIMIT 1").fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    return (str(row[0]), str(row[1]), str(row[2]), int(row[3])) if row is not None else None


def reindex_profiles(device: sqlite3.Connection, data_dir: Path) -> int:
    """List again every student profile whose learner database still names it (UXS-47 "Try again"): the profile
    index is rebuilt from the profiles' own files, and nothing is read from or written to a learner database. A
    profile whose file cannot be read at all stays on disk for "Restore from a profile archive"."""
    listed = 0
    with transaction(device):
        for path in sorted((data_dir / "profiles").glob("*/learner.db")):
            found = _readable_profile(path)
            if found is None or found[0] != path.parent.name:
                continue  # the implicit pre-profile database, or a file that cannot be read
            internal, name, avatar, created = found
            done = device.execute(
                "INSERT OR IGNORE INTO profiles_index (profile_id, kind, display_name, avatar, db_file, keystore_ref,"
                " created_at) VALUES (?, 'student', ?, ?, ?, ?, ?)",
                (internal, name, avatar, f"profiles/{internal}/learner.db", f"jana-meta.profile.prf_{internal.lower()}",
                 created),
            )  # fmt: skip
            listed += done.rowcount
    return listed


def open_local(data_dir: Path, *, app_version: str, now_ms: int) -> LocalStores:
    data_dir.mkdir(parents=True, exist_ok=True)
    device_path = data_dir / "device.db"
    damaged_device = None
    if device_path.exists() and device_path.stat().st_size > 0 and not quick_check(device_path):
        damaged_device = set_aside(device_path)
    device, applied = open_database(device_path, "device")
    record_migrations(device, "device", "device.db", applied, app_version, now_ms)
    row = device.execute("SELECT device_id FROM device").fetchone()
    if row is None:
        device_id = secrets.token_hex(16)  # random, stable, 128 bits as 32 lowercase hex characters (DAT-16)
        with transaction(device):
            device.execute(
                "INSERT INTO device (singleton, device_id, hardware_binding, install_kind, created_at, schema_version)"
                " VALUES (1, ?, ?, 'family', ?, ?)",
                (device_id, _hardware_binding(), now_ms, schema_version(device)),
            )
    else:
        device_id = row[0]
    device_recovery = None
    if damaged_device is not None:
        reindex_profiles(device, data_dir)
        device_recovery = Recovery("reindex", damaged_device.name, 0, rows_lost=True, needs_rebuild=False)

    profiles = data_dir / "profiles"
    existing = sorted(p.parent for p in profiles.glob("*/learner.db"))
    folder = existing[0] if existing else profiles / new_ulid(now_ms)
    folder.mkdir(parents=True, exist_ok=True)
    profile_id = folder.name
    learner_path = folder / "learner.db"
    learner, applied, recovery = open_checked(learner_path, "learner")
    record_migrations(device, "learner", f"profiles/{profile_id}/learner.db", applied, app_version, now_ms)
    with transaction(learner):
        learner.execute(
            "INSERT INTO meta (singleton, profile_id, schema_version, created_at, created_app_ver, last_opened_at,"
            " last_app_ver) VALUES (1, ?, ?, ?, ?, ?, ?) ON CONFLICT (singleton) DO UPDATE SET"
            " last_opened_at = excluded.last_opened_at, last_app_ver = excluded.last_app_ver",
            (profile_id, schema_version(learner), now_ms, app_version, now_ms, app_version),
        )
    return LocalStores(device, learner, device_id, profile_id, learner_path, recovery, device_recovery)
