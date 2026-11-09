"""Installed content packs: install, load and update by signed diff, each swap atomic (CUR-08, CUR-09, SEC-13).

The packs folder holds the active version of each pack as `<pack_id>.pack` (the files the pack list and the content
index read), `previous/` with the version an update replaced, `.staging/` for work in progress, and `installed.json`,
the record of what was verified at install: each pack's version, the sha256 of its signed manifest and the sha256 of
its file.

- Install and update verify a new version completely (signature, every table hash, the SQLite hardening and the
  content contract) in `.staging/` before anything active changes.
- The swap is one atomic rename onto `<pack_id>.pack`. The record of the new version is written as *pending* before
  the rename and committed after it, so after a power loss at any point `recover` finds either the old or the new
  version, never a mix: a staged file is discarded, and the active file is matched by its hash to the committed or
  the pending record.
- Every load compares the file and its stored manifest with the installed record and verifies the pack again; a pack
  that fails is refused (`pack.damaged`) and every other pack keeps working (SEC-13).
- The previous version is kept until the new one has loaded once (PLT-13).
- An update that arrives while a lesson, practice set or mock exam is open (`busy`) is verified now and kept in
  `ready/`; it replaces the active version only at the next session start (`apply_ready`), verified again (PLT-13).
- A revocation list embedded in an installed or updated pack is offered to the device's revocation store, and every
  later verification refuses what it revokes, whichever pack brought it (SEC-16).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from agent_core.config.environments import Environment
from agent_core.config.revocation import RevocationStore
from agent_core.config.trust import SignatureRefused, TrustStore, bundled_trust
from agent_core.store import pack_diff
from agent_core.store.channels import channel_accepts
from agent_core.store.packs import (
    PackRejectedError,
    read_pack,
    read_signed_files,
    schema_problems,
    verify,
    verify_for_install,
)

REGISTRY = "installed.json"
STAGING = ".staging"
PREVIOUS = "previous"
READY = "ready"  # verified updates waiting for the next session start (PLT-13)
READ_ONLY = 0o444
CHUNK = 1024 * 1024

# Fault points a test can stop at to stand for a power loss (`fault(point)` raising): staged, rows, pending, previous,
# swap, commit.
Fault = Callable[[str], None]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(path: Path, text: str) -> None:
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    _fsync(path.parent)


class PackStore:
    def __init__(self, folder: Path, environment: Environment, trust: TrustStore | None = None,
                 fault: Fault | None = None, revocations: RevocationStore | None = None,
                 busy: Callable[[], bool] | None = None, channel: str | None = None,
                 recover: bool = True) -> None:  # fmt: skip
        self.folder = folder
        self.channel = channel  # the device's channel from its licence (PLT-16); None accepts every channel
        self.environment = environment
        self.base_trust = trust or bundled_trust()
        self.fault = fault
        self.revocations = revocations
        self.busy = busy  # a lesson, practice set or mock exam is open: updates wait for the next session (PLT-13)
        folder.mkdir(parents=True, exist_ok=True)
        # A reader (the tutor's content index) never finishes or discards another writer's work in progress.
        self.recovered = self.recover() if recover else []

    @property
    def trust(self) -> TrustStore:
        """The anchors, less every key the newest revocation list this device holds revokes (SEC-16)."""
        return self.revocations.trust() if self.revocations is not None else self.base_trust

    def _adopt_embedded(self, path: Path) -> None:
        """Offer each revocation list the pack carries to the device's store; an invalid or older one is ignored."""
        if self.revocations is None:
            return
        for entry in read_signed_files(path).values():
            if entry["kind"] == "revocation_list":
                self.revocations.adopt(entry["document"])

    # --- the installed record -------------------------------------------------------------------------------------

    @property
    def registry_path(self) -> Path:
        return self.folder / REGISTRY

    def registry(self) -> dict[str, Any]:
        if not self.registry_path.is_file():
            return {"packs": {}, "pending": {}, "ready": {}}
        data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        return {"packs": dict(data.get("packs", {})), "pending": dict(data.get("pending", {})),
                "ready": dict(data.get("ready", {}))}  # fmt: skip

    def _save(self, registry: Mapping[str, Any]) -> None:
        _write_atomic(self.registry_path, json.dumps(registry, indent=1, sort_keys=True) + "\n")

    def active_path(self, pack_id: str) -> Path:
        return self.folder / f"{pack_id}.pack"

    def installed(self, pack_id: str) -> dict[str, Any] | None:
        entry = self.registry()["packs"].get(pack_id)
        return dict(entry) if entry else None

    def _step(self, point: str) -> None:
        if self.fault is not None:
            self.fault(point)

    # --- start-up recovery ----------------------------------------------------------------------------------------

    def recover(self) -> list[str]:
        """Finish or roll back an interrupted swap; returns what was done, for the health log."""
        done = []
        staging = self.folder / STAGING
        if staging.is_dir():
            for leftover in sorted(staging.iterdir()):
                leftover.unlink()
                done.append(f"discarded {leftover.name}")
        registry = self.registry()
        for pack_id, pending in sorted(registry["pending"].items()):
            path = self.active_path(pack_id)
            actual = file_sha256(path) if path.is_file() else None
            if actual == pending["file_sha256"]:
                registry["packs"][pack_id] = pending
                done.append(f"{pack_id} {pending['version']}: the update completed")
            else:
                done.append(f"{pack_id} {pending['version']}: the update did not complete; the installed version stays")
                if pending.get("previous"):  # the copy of the version that stays active is not needed
                    (self.folder / PREVIOUS / str(pending["previous"]["file"])).unlink(missing_ok=True)
        if registry["pending"]:
            registry["pending"] = {}
            self._save(registry)
        return done

    # --- install and update ---------------------------------------------------------------------------------------

    def install(self, source: Path) -> dict[str, Any]:
        """Install a whole signed pack (first install, or a full replacement from the USB kit or a download)."""
        staging = self.folder / STAGING
        staging.mkdir(exist_ok=True)
        staged = staging / f"{source.stem}.pack.partial"
        shutil.copyfile(source, staged)
        return self._swap_in(staged, expected=None)

    def apply_diff(self, diff: Mapping[str, Any]) -> dict[str, Any]:
        """Apply a signed row-level diff to the installed version it names; returns the new manifest. Any failure
        leaves the installed version active and unchanged (raises `PackRejectedError`)."""
        try:
            self.trust.verify(pack_diff.ARTEFACT, diff, self.environment, signed_at=str(diff.get("created_at")))
        except SignatureRefused as error:
            raise PackRejectedError(f"the update for {diff.get('pack_id')}: {error}") from error
        pack_id = str(diff["pack_id"])
        entry = self.installed(pack_id)
        if entry is None:
            raise PackRejectedError(f"{pack_id} is not installed; install the full pack first")
        if entry["version"] != diff["from_version"] or entry["manifest_sha256"] != diff["from_manifest_sha256"]:
            raise PackRejectedError(f"{pack_id} {entry['version']} is installed; the update applies to "
                                    f"{diff['from_version']}")  # fmt: skip
        current = self.load(pack_id)
        staging = self.folder / STAGING
        staging.mkdir(exist_ok=True)
        staged = staging / f"{pack_id}-{diff['to_version']}.pack.partial"
        shutil.copyfile(current, staged)
        os.chmod(staged, 0o644)
        conn = sqlite3.connect(staged, isolation_level=None)
        try:
            conn.execute("PRAGMA trusted_schema = OFF")
            conn.execute("PRAGMA journal_mode = DELETE")
            if schema_problems(conn):
                raise PackRejectedError(f"{pack_id}: the installed copy is not a plain pack file")
            conn.execute("BEGIN IMMEDIATE")
            try:
                pack_diff.apply_rows(conn, diff)
                self._step("rows")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        except (sqlite3.Error, ValueError, KeyError, TypeError) as error:
            staged.unlink(missing_ok=True)
            if isinstance(error, PackRejectedError):
                raise
            raise PackRejectedError(f"{pack_id}: the update could not be applied ({type(error).__name__})") from error
        finally:
            conn.close()
        return self._swap_in(staged, expected=diff["manifest"])

    def remove(self, pack_id: str) -> bool:
        """Uninstall a pack (API-29 `DELETE /v1/packs/{id}`): its file, any kept previous version and waiting update."""
        registry = self.registry()
        entry = registry["packs"].pop(pack_id, None)
        if entry is None:
            return False
        registry["ready"].pop(pack_id, None)
        self._save(registry)
        for path in (
            self.active_path(pack_id),
            self.folder / READY / f"{pack_id}.pack",
            *((self.folder / PREVIOUS / str(entry["previous"]["file"]),) if entry.get("previous") else ()),
        ):
            if path.is_file():
                os.chmod(path, 0o644)
                path.unlink()
        return True

    def ready_updates(self) -> dict[str, str]:
        """Verified updates waiting for the next session start: pack id -> version (PLT-13)."""
        return {pack_id: str(entry["version"]) for pack_id, entry in sorted(self.registry()["ready"].items())}

    def apply_ready(self) -> list[dict[str, Any]]:
        """At a session start: swap in each waiting update, verified again first. One that no longer verifies is
        dropped and its pack keeps the installed version. Returns the manifests now active."""
        applied = []
        registry = self.registry()
        for pack_id, entry in sorted(registry["ready"].items()):
            waiting = self.folder / READY / f"{pack_id}.pack"
            staging = self.folder / STAGING
            staging.mkdir(exist_ok=True)
            staged = staging / f"{pack_id}-{entry['version']}.pack.partial"
            try:
                if not waiting.is_file():
                    raise PackRejectedError(f"{pack_id}: the waiting update is missing")
                os.chmod(waiting, 0o644)
                os.replace(waiting, staged)
                manifest = self._swap_in(staged, expected=None, defer=False)
                if pack_diff.manifest_sha256(manifest) != entry["manifest_sha256"]:
                    raise PackRejectedError(f"{pack_id}: the waiting update changed")  # pragma: no cover - verified
                applied.append(manifest)
            except PackRejectedError:
                staged.unlink(missing_ok=True)
            current = self.registry()
            current["ready"].pop(pack_id, None)
            self._save(current)
        return applied

    def _swap_in(self, staged: Path, expected: Mapping[str, Any] | None, defer: bool = True) -> dict[str, Any]:
        try:
            _fsync(staged)
            self._step("staged")
            manifest = verify_for_install(staged, self.environment, self.trust)
            if expected is not None and pack_diff.manifest_sha256(manifest) != pack_diff.manifest_sha256(expected):
                raise PackRejectedError(f"{staged.name}: the result is not the version the update names")
            if self.channel is not None and not channel_accepts(self.channel, str(manifest.get("channel"))):
                raise PackRejectedError(f"{manifest['pack_id']}: a {manifest.get('channel')} pack is not for a "
                                        f"{self.channel} device")  # fmt: skip
        except PackRejectedError:
            staged.unlink(missing_ok=True)
            raise
        self._adopt_embedded(staged)
        pack_id = str(manifest["pack_id"])
        registry = self.registry()
        old = registry["packs"].get(pack_id)
        if old is not None and _version(manifest["version"]) <= _version(old["version"]):
            staged.unlink(missing_ok=True)
            raise PackRejectedError(f"{pack_id} {manifest['version']} is not newer than the installed {old['version']}")
        if defer and old is not None and self.busy is not None and self.busy():
            waiting = self.folder / READY / f"{pack_id}.pack"
            waiting.parent.mkdir(exist_ok=True)
            waiting.unlink(missing_ok=True)
            os.chmod(staged, READ_ONLY)
            os.replace(staged, waiting)
            registry["ready"][pack_id] = {"version": manifest["version"],
                                          "manifest_sha256": pack_diff.manifest_sha256(manifest)}  # fmt: skip
            self._save(registry)
            return manifest
        new = {"version": manifest["version"], "manifest_sha256": pack_diff.manifest_sha256(manifest),
               "file_sha256": file_sha256(staged), "loaded": False, "previous": None}  # fmt: skip
        active = self.active_path(pack_id)
        if old is not None and active.is_file():
            kept = self.folder / PREVIOUS / f"{pack_id}-{old['version']}.pack"
            new["previous"] = {**{k: v for k, v in old.items() if k != "previous"}, "file": str(kept.name)}
        registry["pending"][pack_id] = new
        self._save(registry)
        self._step("pending")
        if new["previous"] is not None:
            kept = self.folder / PREVIOUS / str(new["previous"]["file"])
            kept.parent.mkdir(exist_ok=True)
            kept.unlink(missing_ok=True)
            try:
                os.link(active, kept)
            except OSError:
                shutil.copyfile(active, kept)
            self._step("previous")
        os.chmod(staged, READ_ONLY)
        os.replace(staged, active)
        _fsync(self.folder)
        self._step("swap")
        registry = self.registry()
        registry["packs"][pack_id] = registry["pending"].pop(pack_id)
        self._save(registry)
        self._step("commit")
        return manifest

    # --- load -----------------------------------------------------------------------------------------------------

    def load(self, pack_id: str) -> Path:
        """The active pack's file, checked against the installed record and verified again (SEC-13). Raises
        `PackRejectedError` (`pack.damaged`) when it does not match or does not verify."""
        entry = self.installed(pack_id)
        if entry is None:
            raise PackRejectedError(f"{pack_id} is not installed")
        path = self.active_path(pack_id)
        if not path.is_file() or file_sha256(path) != entry["file_sha256"]:
            raise PackRejectedError(f"{pack_id}: the installed file has changed since it was verified")
        manifest = verify(path, self.environment, self.trust)
        if pack_diff.manifest_sha256(manifest) != entry["manifest_sha256"]:
            raise PackRejectedError(f"{pack_id}: the manifest differs from the one verified at install")
        if not entry.get("loaded"):
            self._loaded_once(pack_id)
        return path

    def _loaded_once(self, pack_id: str) -> None:
        """The new version has loaded: the previous one is no longer needed (PLT-13)."""
        registry = self.registry()
        entry = registry["packs"][pack_id]
        previous = entry.get("previous")
        if previous:
            (self.folder / PREVIOUS / str(previous["file"])).unlink(missing_ok=True)
        registry["packs"][pack_id] = {**entry, "loaded": True, "previous": None}
        self._save(registry)

    def load_all(self) -> tuple[dict[str, Path], list[tuple[str, str]]]:
        """Every installed pack that loads, and the refused ones with why; one refusal never stops the others."""
        loaded: dict[str, Path] = {}
        refused: list[tuple[str, str]] = []
        for pack_id in sorted(self.registry()["packs"]):
            try:
                loaded[pack_id] = self.load(pack_id)
            except PackRejectedError as error:
                refused.append((pack_id, str(error)))
        return loaded, refused

    def tables(self, pack_id: str) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
        return read_pack(self.load(pack_id))


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in str(text).split("."))
