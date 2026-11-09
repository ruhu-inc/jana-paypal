"""App updates on the device: the offline update pack, channels, downgrade refusal, safe application and rollback
(PLT-14, PLT-15, PLT-16, SEC-15, SEC-16).

**What an update is.** A signed update manifest (`update.json`) names the version, the release channel, the OS family
and architecture, what the update changes (shown to the adult before they accept it), the installer file with its
size and sha256, and optionally the revocation list current when it was built (SEC-16). Online, the Tauri updater
fetches the installer through the shell's `net` module (feature `update_checks`, then `downloads`); offline, the same
manifest and installer arrive together as a `.janaupdate` file (a zip) from removable media. Both are signed with the
updater key (TABLE SEC-12a: `updater_artefact` online, `offline_update_pack` from media).

**Verification before any write (SEC-15).** `read_update_pack` reads the manifest out of the zip into memory, checks
its signature, its channel against the device's (PLT-16), its OS and architecture, and that its version is higher than
the installed one, then hashes the installer inside the zip, all without writing a byte. Only a verified update is
staged (`UpdateState.stage`), by a copy to a temporary name and one atomic rename. A lower version is refused even
with a valid signature: the only way back is the rollback below, which reinstalls the cached previous installer.

**Safe application (PLT-15).** An update never installs during a turn, a mock exam or a download of learner-relevant
data; it installs at quit or on the adult's "Restart now" (`can_install_now`). Before the installer runs, the learner
store is snapshotted (`prepare_install`). The new version's first launch runs its health check (core ready, worker
hello, and a touch generation when a tutor model is installed; a content-only machine skips that step). A failure
offers "Go back to the previous version": `rollback` restores the snapshot and hands the shell the cached previous
installer to run.

The device's channel comes from its licence when it has one (a school's or a team device's), and is `stable` without
one; there is no adult channel setting (D53). No step here checks a licence: the free offline app updates from USB or
download with none (D84).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import zipfile
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_core.config.environments import Environment
from agent_core.config.revocation import RevocationStore
from agent_core.config.trust import SignatureRefused, TrustStore, bundled_trust
from agent_core.contracts import errors as contract_errors
from agent_core.store.channels import channel_accepts

FORMAT = "jana.update.1"
EXTENSION = ".janaupdate"
MANIFEST = "update.json"
ONLINE_ARTEFACT = "updater_artefact"
OFFLINE_ARTEFACT = "offline_update_pack"
MANIFEST_MAX_BYTES = 256 * 1024  # a manifest is a few kilobytes; a larger one is not read
STATE = "update-state.json"
HASH_BLOCK = 1024 * 1024


class UpdateRefused(ValueError):
    """The update is not applied; `code` says why (signature, channel, downgrade, platform, damaged)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def version_tuple(text: str) -> tuple[int, ...]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([A-Za-z0-9.-]+))?(?:\+[A-Za-z0-9.-]+)?", str(text))
    if match is None:
        raise ValueError("invalid app version")
    prerelease = match[4]
    rc = re.fullmatch(r"rc\.(\d+)", prerelease or "")
    return (*map(int, match.group(1, 2, 3)), int(prerelease is None), int(rc[1]) if rc else -1)


def installer_name(name: Any) -> str:
    """Only a plain filename may become an installer path or a native command argument."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", name):
        raise UpdateRefused("damaged", "the update names an unsafe installer file")
    return name


@dataclass(frozen=True)
class Platform:
    os_family: str  # windows, macos or linux
    arch: str  # x86_64 or arm64


@dataclass(frozen=True)
class VerifiedUpdate:
    manifest: dict[str, Any]
    source: Path

    @property
    def version(self) -> str:
        return str(self.manifest["version"])

    def summary(self) -> dict[str, Any]:
        """What the adult sees before accepting: size and what changes (PLT-14)."""
        return {"version": self.version, "size_bytes": int(self.manifest["installer"]["size_bytes"]),
                "changes": list(self.manifest.get("changes", []))}  # fmt: skip


def check_manifest(manifest: Mapping[str, Any], artefact: str, *, installed_version: str, device_channel: str,
                   platform: Platform, environment: Environment, trust: TrustStore | None = None,
                   allow_installed: bool = False) -> None:  # fmt: skip
    """Signature first, then channel, platform and version (SEC-15, PLT-16). Raises `UpdateRefused`."""
    if manifest.get("format") != FORMAT:
        raise UpdateRefused("damaged", "not an update manifest")
    created = manifest.get("created_at")
    try:
        (trust or bundled_trust()).verify(artefact, manifest, environment,
                                          signed_at=created if isinstance(created, str) else None)  # fmt: skip
    except SignatureRefused as error:
        raise UpdateRefused("signature", f"the update's signature is refused: {error}") from error
    if contract_errors("ops/update-manifest", dict(manifest)):
        raise UpdateRefused("damaged", "the signed update manifest is not valid")
    installer_name(manifest["installer"]["name"])
    if not channel_accepts(device_channel, str(manifest.get("channel"))):
        raise UpdateRefused("channel", f"a {manifest.get('channel')} update is not for a {device_channel} device")
    if manifest.get("os") != platform.os_family or manifest.get("arch") != platform.arch:
        raise UpdateRefused("platform", f"the update is for {manifest.get('os')} {manifest.get('arch')}")
    try:
        offered, installed = version_tuple(str(manifest["version"])), version_tuple(installed_version)
        newer = offered > installed or (allow_installed and offered == installed)
    except (KeyError, ValueError) as error:
        raise UpdateRefused("damaged", "the update names no valid version") from error
    if not newer:
        raise UpdateRefused("downgrade", f"{manifest['version']} is not newer than the installed {installed_version}")


def read_update_pack(path: Path, *, installed_version: str, device_channel: str, platform: Platform,
                     environment: Environment, trust: TrustStore | None = None,
                     allow_installed: bool = False) -> VerifiedUpdate:  # fmt: skip
    """Verify a `.janaupdate` from removable media without writing any file (SEC-15, PLT-14)."""
    try:
        return _read_update_pack(path, installed_version=installed_version, device_channel=device_channel,
                                 platform=platform, environment=environment, trust=trust,
                                 allow_installed=allow_installed)  # fmt: skip
    except (OSError, zipfile.BadZipFile, KeyError, TypeError) as error:  # a damaged archive or manifest
        raise UpdateRefused("damaged", f"{path.name} is not a readable update pack") from error


def _read_update_pack(path: Path, *, installed_version: str, device_channel: str, platform: Platform,
                      environment: Environment, trust: TrustStore | None,
                      allow_installed: bool = False) -> VerifiedUpdate:  # fmt: skip
    archive = zipfile.ZipFile(path)
    with archive:
        try:
            info = archive.getinfo(MANIFEST)
        except KeyError as error:
            raise UpdateRefused("damaged", f"{path.name} has no {MANIFEST}") from error
        if info.file_size > MANIFEST_MAX_BYTES:
            raise UpdateRefused("damaged", f"{MANIFEST} is too large")
        try:
            manifest = json.loads(archive.read(info))
        except ValueError as error:
            raise UpdateRefused("damaged", f"{MANIFEST} is not JSON") from error
        if not isinstance(manifest, dict):
            raise UpdateRefused("damaged", f"{MANIFEST} is not an object")
        check_manifest(manifest, OFFLINE_ARTEFACT, installed_version=installed_version, device_channel=device_channel,
                       platform=platform, environment=environment, trust=trust,
                       allow_installed=allow_installed)  # fmt: skip
        installer = manifest.get("installer") or {}
        names = archive.namelist()
        if len(names) != 2 or set(names) != {MANIFEST, installer.get("name")}:
            raise UpdateRefused("damaged", "the update pack holds files its manifest does not list")
        entry = archive.getinfo(str(installer["name"]))
        if entry.file_size != int(installer["size_bytes"]):
            raise UpdateRefused("damaged", "the installer's size does not match the manifest")
        digest = hashlib.sha256()
        with archive.open(entry) as handle:
            while block := handle.read(HASH_BLOCK):
                digest.update(block)
        if digest.hexdigest() != installer["sha256"]:
            raise UpdateRefused("damaged", "the installer does not match its hash")
    return VerifiedUpdate(manifest, path)


def adopt_revocations(update: VerifiedUpdate, revocations: RevocationStore) -> bool:
    """An update carries the revocation list current when it was built (SEC-16); a newer valid one is adopted."""
    carried = update.manifest.get("revocation_list")
    return isinstance(carried, Mapping) and revocations.adopt(carried)


def write_update_pack(path: Path, signed_manifest: Mapping[str, Any], installer: Path) -> Path:
    """The release pipeline's `.janaupdate`: the signed manifest and the installer it names."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(MANIFEST, json.dumps(dict(signed_manifest), indent=1, sort_keys=True))
        archive.write(installer, str(signed_manifest["installer"]["name"]))
    return path


def update_manifest(version: str, channel: str, platform: Platform, installer: Path, changes: list[str],
                    created_at: str, revocation_list: Mapping[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """An unsigned update manifest for `installer` (the release pipeline signs it with the updater key)."""
    digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    manifest: dict[str, Any] = {
        "format": FORMAT, "version": version, "channel": channel, "os": platform.os_family, "arch": platform.arch,
        "installer": {"name": installer.name, "size_bytes": installer.stat().st_size, "sha256": digest},
        "changes": changes, "created_at": created_at,
    }  # fmt: skip
    if revocation_list is not None:
        manifest["revocation_list"] = dict(revocation_list)
    return manifest


# --- safe application and rollback (PLT-15) ------------------------------------------------------------------------


def can_install_now(turn_running: bool, mock_active: bool, learner_download_active: bool) -> bool:
    """An update never installs during a turn, a mock exam or a download of learner-relevant data (PLT-15)."""
    return not (turn_running or mock_active or learner_download_active)


@dataclass(frozen=True)
class HealthCheck:
    """The new version's first-launch health check (PLT-15)."""

    core_ready: bool
    worker_hello: bool
    touch_generation: bool | None  # None on a content-only machine: the step is skipped

    def passed(self) -> bool:
        return self.core_ready and self.worker_hello and self.touch_generation is not False


def _learner_files(data_dir: Path) -> list[Path]:
    """The learner store: the device database and every profile's learner database, with their journals."""
    found = []
    for pattern in ("device.db*", "profiles/*/learner.db*"):
        found += [p for p in sorted(data_dir.glob(pattern)) if p.is_file()]
    return found


def _copy_durable(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    shutil.copyfile(source, temp)
    with temp.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temp, target)


class UpdateState:
    """The device's update record in `<data_dir>/updates/`: installed version, staged update, the cached previous
    installer, the learner-store snapshot and the first-launch outcome."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.folder = data_dir / "updates"
        self.folder.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self.folder / STATE

    def read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"installed": None, "staged": None, "previous": None, "snapshot": None, "phase": "idle"}
        loaded: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        return loaded

    def _write(self, state: Mapping[str, Any]) -> None:
        temp = self.path.with_name(STATE + ".tmp")
        temp.write_text(json.dumps(dict(state), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, self.path)

    def local_path(self, relative: str) -> Path:
        """State-file paths stay in the update folder, including through symlinks."""
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise UpdateRefused("damaged", "an update state path leaves the update folder")
        target = (self.folder / path).resolve()
        if not target.is_relative_to(self.folder.resolve()):
            raise UpdateRefused("damaged", "an update state path leaves the update folder")
        return target

    def record_installed(self, version: str, installer: Path | None = None) -> None:
        """The running version, and its installer cached for a later rollback (copied once)."""
        state = self.read()
        previous = state.get("installed") or {}
        cached_installer = previous.get("installer") if previous.get("version") == version else None
        cached_hash = previous.get("sha256") if previous.get("version") == version else None
        state["installed"] = {"version": version, "installer": cached_installer, "sha256": cached_hash}
        if previous.get("version") == version and isinstance(previous.get("manifest"), dict):
            state["installed"]["manifest"] = previous["manifest"]
        if installer is not None:
            version_tuple(version)
            cached = self.local_path(f"installers/{version}/{installer_name(installer.name)}")
            _copy_durable(installer, cached)
            state["installed"]["installer"] = str(cached.relative_to(self.folder))
            state["installed"]["sha256"] = hashlib.sha256(cached.read_bytes()).hexdigest()
        self._write(state)

    def stage(self, update: VerifiedUpdate, *, cache_current: bool = False) -> Path:
        """Extract the verified installer to `staged/<version>/`; the update installs at quit (PLT-15)."""
        installer = update.manifest["installer"]
        version_tuple(update.version)
        if cache_current and (self.read().get("installed") or {}).get("version") != update.version:
            raise UpdateRefused("version_mismatch", "only the current installer can become its rollback copy")
        branch = "installers" if cache_current else "staged"
        target = self.local_path(f"{branch}/{update.version}/{installer_name(installer['name'])}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".partial")
        try:
            with zipfile.ZipFile(update.source) as archive, archive.open(str(installer["name"])) as src:
                if json.loads(archive.read(MANIFEST)) != update.manifest:
                    raise UpdateRefused("damaged", "the manifest changed while it was copied")
                with temp.open("wb") as out:
                    digest = hashlib.sha256()
                    size = 0
                    while block := src.read(HASH_BLOCK):
                        size += len(block)
                        if size > installer["size_bytes"]:
                            raise UpdateRefused("damaged", "the installer exceeds its signed size")
                        digest.update(block)
                        out.write(block)
                    out.flush()
                    os.fsync(out.fileno())
            if size != installer["size_bytes"] or digest.hexdigest() != installer["sha256"]:
                raise UpdateRefused("damaged", "the installer changed while it was copied")
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        state = self.read()
        if cache_current:
            state["installed"] = {"version": update.version, "installer": str(target.relative_to(self.folder)),
                                  "sha256": installer["sha256"], "manifest": update.manifest}  # fmt: skip
        else:
            state["staged"] = {"version": update.version, "installer": str(target.relative_to(self.folder)),
                               "summary": update.summary(), "manifest": update.manifest}  # fmt: skip
            state["accepted"] = False
        state.pop("failure_code", None)
        self._write(state)
        return target

    def prepare_install(self) -> dict[str, Any]:
        """At quit or "Restart now": snapshot the learner store, then hand the shell the installer to run."""
        state = self.read()
        if not state.get("staged") or not state.get("installed"):
            raise UpdateRefused("nothing_staged", "no update is staged")
        self.installer_plan(state["installed"], "rollback")
        self.installer_plan(state["staged"], "update")
        from_version = state["installed"]["version"]
        snapshot = self.local_path(f"snapshots/{from_version}")
        pending = snapshot.with_name(snapshot.name + ".partial")
        if pending.exists():
            shutil.rmtree(pending)
        pending.mkdir(parents=True)
        inventory: dict[str, str] = {}
        # SQLite's backup API includes committed WAL pages in one consistent database image. Copying the database
        # and its journals independently can produce a snapshot that never existed (PLT-15, DAT-12).
        for source in _learner_files(self.data_dir):
            if source.suffix != ".db":
                continue
            target = pending / source.relative_to(self.data_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not source.resolve().is_relative_to(self.data_dir.resolve()):
                raise UpdateRefused("snapshot_failed", "a learner database leaves its data folder")
            try:
                with (
                    closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src,
                    closing(sqlite3.connect(target)) as dst,
                ):
                    src.backup(dst)
            except sqlite3.Error as error:
                raise UpdateRefused("snapshot_failed", "the learner database could not be snapshotted") from error
            with target.open("rb") as handle:
                os.fsync(handle.fileno())
            inventory[str(target.relative_to(pending))] = hashlib.sha256(target.read_bytes()).hexdigest()
        if snapshot.exists():
            shutil.rmtree(snapshot)
        os.replace(pending, snapshot)
        state.update(phase="installing", snapshot=str(snapshot.relative_to(self.folder)),
                     snapshot_inventory=inventory, previous=dict(state["installed"]))  # fmt: skip
        self._write(state)
        return {
            "run_installer": str(self.local_path(state["staged"]["installer"])),
            "to_version": state["staged"]["version"],
        }

    def installer_plan(self, entry: Mapping[str, Any], action: str) -> dict[str, Any]:
        """Recheck cached bytes immediately before a shutdown hands an installer to the shell."""
        relative = entry.get("installer")
        if not isinstance(relative, str):
            raise UpdateRefused("no_rollback_installer", "the previous installer is unavailable")
        path = self.local_path(relative)
        # The signed manifest's hash decides, never the state file's own copy, which nothing signs (SEC-15, PLT-15).
        manifest = entry.get("manifest")
        installer = manifest.get("installer") if isinstance(manifest, Mapping) else None
        signed = installer.get("sha256") if isinstance(installer, Mapping) else None
        expected = signed if isinstance(signed, str) else entry.get("sha256")
        if signed is not None and entry.get("sha256") not in (None, signed):
            raise UpdateRefused("damaged", "the cached installer does not match its signed manifest")
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise UpdateRefused("damaged", "the cached installer is missing or changed")
        return {"installer": str(path), "sha256": expected, "size_bytes": path.stat().st_size,
                "version": entry["version"], "action": action}  # fmt: skip

    def first_launch(self, running_version: str, health: HealthCheck) -> dict[str, Any]:
        """The new version's first start: commit on a passing health check, otherwise offer the rollback."""
        state = self.read()
        if state.get("phase") != "installing":
            return {"offer_rollback": False}
        if health.passed():
            staged = state["staged"]
            if running_version != staged["version"]:
                raise UpdateRefused("version_mismatch", "the running version is not the staged update")
            installer = self.local_path(staged["installer"])
            cached = self.local_path(f"installers/{running_version}/{installer_name(installer.name)}")
            _copy_durable(installer, cached)
            installed = {"version": running_version, "installer": str(cached.relative_to(self.folder)),
                         "sha256": hashlib.sha256(cached.read_bytes()).hexdigest(),
                         "manifest": staged.get("manifest")}  # fmt: skip
            state.update(phase="idle", staged=None, installed=installed)
            self._write(state)
            return {"offer_rollback": False}
        state["phase"] = "rollback_offered"
        self._write(state)
        return {"offer_rollback": True, "previous_version": state["previous"]["version"]}

    def rollback(self) -> dict[str, Any]:
        """ "Go back to the previous version": restore the learner snapshot, then the shell reinstalls the cached
        previous installer. This is the one path to a lower version (SEC-15)."""
        state = self.read()
        if state.get("phase") != "rollback_offered" or not state.get("previous") or not state.get("snapshot"):
            raise UpdateRefused("no_rollback", "there is no failed update to roll back")
        snapshot = self.local_path(state["snapshot"])
        if not snapshot.is_dir():
            raise UpdateRefused("damaged", "the learner snapshot is missing")
        inventory = state.get("snapshot_inventory")
        if not isinstance(inventory, dict):
            raise UpdateRefused("damaged", "the learner snapshot has no integrity record")
        for relative, digest in inventory.items():
            saved = (snapshot / relative).resolve()
            target = (self.data_dir / relative).resolve()
            if (
                not saved.is_relative_to(snapshot.resolve())
                or not target.is_relative_to(self.data_dir.resolve())
                or not saved.is_file()
                or hashlib.sha256(saved.read_bytes()).hexdigest() != digest
            ):
                raise UpdateRefused("damaged", "the learner snapshot is missing or changed")
        previous = state["previous"]
        installer = previous.get("installer")
        cached = self.local_path(installer) if installer else None
        if cached is None or not cached.is_file():
            raise UpdateRefused("no_rollback_installer", "the previous installer is unavailable")
        if hashlib.sha256(cached.read_bytes()).hexdigest() != previous.get("sha256"):
            raise UpdateRefused("damaged", "the cached previous installer has changed")
        for current in _learner_files(self.data_dir):
            if not (snapshot / current.relative_to(self.data_dir)).is_file():
                current.unlink()  # a journal or database the failed version created
        for relative in sorted(inventory):
            _copy_durable(snapshot / relative, self.data_dir / relative)
        state.update(phase="idle", staged=None, installed=previous)
        self._write(state)
        return {"reinstall": str(cached), "version": previous["version"]}
