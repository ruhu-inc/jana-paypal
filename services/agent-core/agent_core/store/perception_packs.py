"""Separately signed executable perception packs (PLT-02/10); never execute a model-key-authorized file."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from agent_core.config.environments import Environment
from agent_core.config.trust import SignatureRefused, TrustStore, bundled_trust
from agent_core.contracts import errors
from agent_core.store.channels import channel_accepts

ARTEFACT = "perception_pack"
MANIFEST = "manifest.json"
MAX_BYTES = 1024**3
PACK_ID = re.compile(r"^pp\.[a-z0-9_]+\.[a-z0-9_]+\.(windows|macos|linux)\.(x86_64|arm64)$")
# The highest perception pack version this device has installed, kept beside the packs: an older pack is refused even
# after the newer one was removed (SEC-15's rule for packs: roll forward, never back).
VERSION_FLOOR = ".version-floor.json"


class PerceptionPackError(ValueError):
    code = "pack.damaged"


class PerceptionPackNotApplicable(PerceptionPackError):
    """A pack for another platform, a newer app or another channel: it does not apply here, which is not a failed check,
    so older packs stay usable beside it (a revoked or damaged newer pack leaves none, SEC-16)."""


def target() -> tuple[str, str]:
    os_name = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}.get(platform.system(), "unsupported")
    arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x86_64", "AMD64": "x86_64"}.get(
        platform.machine(), "unsupported"
    )
    return os_name, arch


def version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("-")[0].split("."))


def relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or str(path) != value
        or any(part in (".", "..") or not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in path.parts)
    ):
        raise PerceptionPackError("unsafe perception pack path")
    return path


def inventory(folder: Path) -> dict[str, Path]:
    if folder.is_symlink() or not folder.is_dir():
        raise PerceptionPackError("not a regular perception pack folder")
    found = {}
    for path in folder.rglob("*"):
        if path.is_symlink() or path.is_junction():
            raise PerceptionPackError("links are not permitted in perception packs")
        if path.is_dir():
            continue
        if not stat.S_ISREG(path.stat().st_mode):
            raise PerceptionPackError("not a regular perception file")
        name = path.relative_to(folder).as_posix()
        relative(name)
        if name != MANIFEST:
            found[name] = path
        if len(found) > 4096:
            raise PerceptionPackError("too many perception files")
    return found


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    folder: Path,
    *,
    pack_id: str,
    release_version: str,
    min_app_version: str,
    channel: str,
    executable: str,
    hardware_classes: list[str],
    licences: list[dict[str, str]],
    report_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Unsigned build inputs. The signing side must independently establish report/licence/size gates."""
    os_name, arch = target()
    files = [
        {
            "path": name,
            "size_bytes": path.stat().st_size,
            "sha256": file_hash(path),
            "executable": bool(path.stat().st_mode & 0o111) or name == executable,
        }
        for name, path in sorted(inventory(folder).items())
    ]
    return {
        "format": "jana.perception.1",
        "perception_pack_id": pack_id,
        "version": release_version,
        "channel": channel,
        "os": os_name,
        "arch": arch,
        "executable": executable,
        "hardware_classes": hardware_classes,
        "files": files,
        "min_app_version": min_app_version,
        "licences": licences,
        "evaluation_report_id": report_id,
        "created_at": created_at,
    }


def sign_pack(
    manifest: Mapping[str, Any],
    sign: Callable[[str, Mapping[str, Any]], dict[str, Any]],
    report_passed: Callable[[str], bool],
) -> dict[str, Any]:
    unsigned = {key: value for key, value in manifest.items() if key != "signature"}
    shape = {**unsigned, "signature": {"alg": "ed25519", "key_id": "pending", "value": "pending"}}
    if errors("ops/perception-pack", shape) or not report_passed(str(unsigned.get("evaluation_report_id", ""))):
        raise PerceptionPackError("perception release gates have not passed")
    return sign(ARTEFACT, unsigned)


class PerceptionPackStore:
    def __init__(
        self,
        root: Path,
        environment: Environment,
        app_version: str,
        trust: Callable[[], TrustStore] = bundled_trust,
        channel: str | None = None,
    ) -> None:
        self.root, self.environment, self.app_version = root, environment, app_version
        self.trust, self.channel = trust, channel

    def verify(self, folder: Path) -> dict[str, Any]:
        try:
            manifest_path = folder / MANIFEST
            if manifest_path.is_symlink() or manifest_path.stat().st_size > 2_000_000:
                raise PerceptionPackError("invalid perception manifest")
            manifest: dict[str, Any] = json.loads(manifest_path.read_bytes())
            if errors("ops/perception-pack", manifest):
                raise PerceptionPackError("invalid perception manifest")
            self.trust().verify(ARTEFACT, manifest, self.environment, signed_at=manifest["created_at"])
            if (manifest["os"], manifest["arch"]) != target():
                raise PerceptionPackNotApplicable("perception pack is for another platform")
            if version(manifest["min_app_version"]) > version(self.app_version):
                raise PerceptionPackNotApplicable("perception pack needs a newer app")
            if not channel_accepts(
                self.channel or ("dev" if self.environment == Environment.DEV else "stable"), manifest["channel"]
            ):
                raise PerceptionPackNotApplicable("perception pack is from another channel")
            if not manifest["perception_pack_id"].endswith(f".{manifest['os']}.{manifest['arch']}"):
                raise PerceptionPackError("perception pack id names another platform")
            if not PACK_ID.fullmatch(manifest["perception_pack_id"]):
                raise PerceptionPackError("invalid perception pack id")
            actual = inventory(folder)
            declared = {f["path"]: f for f in manifest["files"]}
            if len(declared) != len(manifest["files"]) or set(actual) != set(declared):
                raise PerceptionPackError("undeclared, duplicate or missing perception files")
            if sum(f["size_bytes"] for f in declared.values()) > MAX_BYTES:
                raise PerceptionPackError("perception pack too large")
            entrypoint = str(relative(manifest["executable"]))
            if entrypoint not in declared or not declared[entrypoint]["executable"]:
                raise PerceptionPackError("no declared executable entry point")
            for licence in manifest["licences"]:
                approved = {
                    "MIT",
                    "MIT-0",
                    "MIT-CMU",
                    "BSD-2-Clause",
                    "BSD-3-Clause",
                    "0BSD",
                    "Apache-2.0",
                    "ISC",
                    "Zlib",
                    "PSF-2.0",
                    "Python-2.0",
                    "CC0-1.0",
                    "MPL-2.0",
                    "Unicode-3.0",
                    "Unicode-DFS-2016",
                }
                expression = licence["id"].replace("(", " ").replace(")", " ")
                alternatives = [re.split(r"\s+AND\s+", part.strip()) for part in re.split(r"\s+OR\s+", expression)]
                if not any(
                    all(term in approved or (term.startswith("LGPL-") and licence.get("review")) for term in terms)
                    for terms in alternatives
                ):
                    raise PerceptionPackError("unapproved perception component licence")
                if str(relative(licence["path"])) not in declared:
                    raise PerceptionPackError("missing licence attribution file")
            for name, file in declared.items():
                relative(name)
                path = actual[name]
                if path.stat().st_size != file["size_bytes"] or file_hash(path) != file["sha256"]:
                    raise PerceptionPackError("damaged perception file")
            return manifest
        except (OSError, ValueError, KeyError, TypeError, SignatureRefused) as error:
            if isinstance(error, PerceptionPackError):
                raise
            raise PerceptionPackError("perception pack did not verify") from error

    def _installed_version(self, folder: Path, hardware_class: str | None = None) -> tuple[int, ...] | None:
        """The version an installed pack's manifest states, read without verifying it (None when unreadable, or when
        the pack is not for `hardware_class`)."""
        try:
            raw = json.loads((folder / MANIFEST).read_bytes())
            if hardware_class is not None and hardware_class not in raw["hardware_classes"]:
                return None
            return version(str(raw["version"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def version_floor(self) -> tuple[int, ...]:
        """The oldest version this device may still install: the newest it has installed, still here or removed since
        (recorded in `VERSION_FLOOR`), or nothing on a fresh device."""
        folders = sorted(self.root.glob("pp.*")) if self.root.is_dir() else []
        versions = [v for v in (self._installed_version(f) for f in folders if PACK_ID.fullmatch(f.name)) if v]
        try:
            versions.append(version(str(json.loads((self.root / VERSION_FLOOR).read_bytes())["version"])))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return max(versions, default=())

    def _raise_floor(self, release: str) -> None:
        floor = self.root / VERSION_FLOOR
        try:
            recorded = version(str(json.loads(floor.read_bytes())["version"]))
        except (OSError, ValueError, KeyError, TypeError):
            recorded = ()
        if version(release) <= recorded:
            return
        pending = self.root / f"{VERSION_FLOOR}.partial"
        pending.write_text(json.dumps({"version": release}), encoding="utf-8")
        os.replace(pending, floor)

    def install(self, source: Path) -> str:
        manifest = self.verify(source)
        pack_id = manifest["perception_pack_id"]
        self.root.mkdir(parents=True, exist_ok=True)
        installed = self.root / pack_id
        if installed.exists():
            if self.verify(installed) != manifest:
                raise PerceptionPackError("perception pack ids are immutable")
            return str(pack_id)
        if version(manifest["version"]) < self.version_floor():
            raise PerceptionPackError("perception pack is older than the installed one")  # SEC-15: forward only
        stage_root = self.root / ".staging"
        stage_root.mkdir(exist_ok=True)
        import tempfile

        stage = Path(tempfile.mkdtemp(prefix="perception-", dir=stage_root))
        try:
            # Copy only files whose names came from the verified manifest. Recheck after copy, before rename.
            for file in manifest["files"]:
                destination = stage / str(relative(file["path"]))
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / file["path"], destination)
                destination.chmod(0o755 if file["executable"] else 0o644)
                with destination.open("rb") as stream:
                    os.fsync(stream.fileno())
            shutil.copyfile(source / MANIFEST, stage / MANIFEST)
            self.verify(stage)
            os.replace(stage, installed)
            self._raise_floor(str(manifest["version"]))
            return str(pack_id)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def command(self, hardware_class: str | None = None) -> tuple[str, ...] | None:
        candidates = []
        refused: list[tuple[int, ...]] = []  # versions of installed packs for this class that failed their checks
        for folder in sorted(self.root.glob("pp.*")) if self.root.is_dir() else []:
            try:
                manifest = self.verify(folder)  # signatures, revocations and every byte on every launch
                if hardware_class is None or hardware_class in manifest["hardware_classes"]:
                    candidates.append((version(manifest["version"]), folder, manifest))
            except PerceptionPackNotApplicable:
                continue
            except PerceptionPackError:
                stated = self._installed_version(folder, hardware_class)
                if stated is not None:
                    refused.append(stated)
        if not candidates:
            return None
        newest, folder, manifest = max(candidates, key=lambda row: row[0])
        if newest < max(refused, default=()):
            return None  # SEC-16: a revoked or damaged newest pack is never replaced by an older build
        return (str((folder / manifest["executable"]).resolve()),)

    def remove(self, pack_id: str) -> bool:
        if not PACK_ID.fullmatch(pack_id) or not (self.root / pack_id).is_dir():
            return False
        shutil.rmtree(self.root / pack_id)
        return True
