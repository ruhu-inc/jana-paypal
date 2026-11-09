"""Model packs (PLT-10, PLT-12, UXS-11): one signed manifest plus files per hardware class and model.

A pack is a folder `<model_pack_id>/` holding `manifest.json` and the files it lists. The manifest names the pack
(`mp.<family>.<size>.<quantisation>.<hardware_class>[.<variant>]`), the class and router tiers it serves, the model's
family and quantisation, each file with its role, size, sha256 and the sha256 of every chunk (the download manager
verifies chunks against these, PLT-11), the weights licence with its attribution, recommended settings, the oldest app
version it runs on, the evaluation report that passed the release gates and the signature of a model intermediate
(TABLE SEC-12a). A tuned pack also names what it was tuned from, its training run and model card; an adapter pack
names the exact base pack and weights hash it applies to (`adapter_for`) and loads only on top of that base.

Every model is 7B parameters or larger (D58): the build refuses a smaller one, and so does the device. Only packs
whose evaluation report passed may be signed for release (`sign_pack` asks).

Installation is the same from every source (PLT-12): the files are copied into `.staging/<id>/`, the whole pack is
verified there (signature, structure, every size and hash, the adapter's base), and one atomic rename makes it
installed. A pack that fails is refused with the damaged-file message (`model.damaged`, UXS-11) and nothing installed
changes. Every load verifies the pack again before the runtime may see it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_core.config import Settings
from agent_core.config.environments import Environment
from agent_core.config.trust import SignatureRefused, TrustSource, TrustStore, bundled_trust, current_trust
from agent_core.store.channels import channel_accepts
from agent_core.store.model_packs import APPROVED_LICENCES, ATTRIBUTION_LICENCES

ARTEFACT = "model_manifest"  # signed by a model intermediate (TABLE SEC-12a)
MANIFEST = "manifest.json"
STAGING = ".staging"
PREVIOUS = ".previous"
DAMAGED = "model.damaged"  # UXS-11 error-final; also PLT-10's bad hash, size or signature
ADAPTER_BASE = "model.adapter_base_mismatch"
APP_TOO_OLD = "model.app_too_old"
TOO_SMALL = "model.too_small"
CHANNEL = "model.channel"
ROLES = frozenset({"weights", "vision_projector", "embedding", "lora_adapter"})
CLASSES = ("low", "mid", "high")
TIERS = frozenset({"fast", "strong", "judge"})
_SEGMENT = r"[a-z0-9_]+"
PACK_ID = re.compile(rf"^mp\.({_SEGMENT})\.([0-9_]+b)\.({_SEGMENT})\.(low|mid|high)(?:\.({_SEGMENT}))?$")
REQUIRED = ("model_pack_id", "manifest_version", "hardware_class", "tiers", "family", "quantisation", "files",
            "licence", "recommended", "min_app_version", "evaluation_report_id", "created_at", "signature")  # fmt: skip


class ModelPackError(ValueError):
    """A pack refused before load; `code` is the message key the model manager shows (UXS-11)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def parameters_b(model_pack_id: str) -> float | None:
    """The model size the id names, in billions of parameters (`8b` is 8.0, `0_6b` is 0.6)."""
    found = PACK_ID.match(model_pack_id)
    return float(found.group(2)[:-1].replace("_", ".")) if found else None


def _older(candidate: Mapping[str, Any], held: Mapping[str, Any]) -> bool:
    """`candidate` was signed before `held` (SEC-24: a replayed older release of an id is a rollback). A candidate
    whose signing time cannot be read counts as older."""
    from agent_core.store.events import ms_from_iso

    try:
        return ms_from_iso(str(candidate["created_at"])) < ms_from_iso(str(held["created_at"]))
    except (KeyError, ValueError):
        return True


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in str(text).split("-")[0].split("."))


def structure_problems(manifest: Mapping[str, Any], settings: Settings | None = None) -> list[str]:
    """What makes a manifest not a model pack, before any signature or file is looked at."""
    settings = settings or Settings()
    problems = [f"missing {name}" for name in REQUIRED if name not in manifest]
    pack_id = str(manifest.get("model_pack_id", ""))
    found = PACK_ID.match(pack_id)
    if not found:
        problems.append(f"{pack_id!r} is not a model pack id")
    else:
        if found.group(4) != manifest.get("hardware_class"):
            problems.append("the id names another hardware class")
        size = parameters_b(pack_id)
        if size is not None and size < settings.model_pack_min_params_b:
            problems.append(f"{size:g}B is below the {settings.model_pack_min_params_b:g}B floor (D58)")
    if manifest.get("hardware_class") not in CLASSES:
        problems.append("hardware_class must be low, mid or high")
    tiers = manifest.get("tiers")
    if not isinstance(tiers, list) or not tiers or set(tiers) - TIERS:
        problems.append("tiers must name fast, strong or judge")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        problems.append("files[] is empty")
        files = []
    roles = [f.get("role") for f in files if isinstance(f, Mapping)]
    for entry in files:
        if not isinstance(entry, Mapping) or set(("path", "role", "size_bytes", "sha256", "chunk_sha256")) - set(entry):
            problems.append("a file entry lacks path, role, size_bytes, sha256 or chunk_sha256")
            continue
        if entry["role"] not in ROLES:
            problems.append(f"{entry['path']}: role {entry['role']!r}")
        path = str(entry["path"])
        if "/" in path or "\\" in path or path in ("", ".", "..", MANIFEST):
            problems.append(f"{path!r} is not a plain file name in the pack")
    adapter = manifest.get("adapter_for")
    if adapter is not None:
        if "lora_adapter" not in roles:
            problems.append("adapter_for needs a lora_adapter file")
        if not isinstance(adapter, Mapping) or not adapter.get("model_pack_id") or not adapter.get("weights_sha256"):
            problems.append("adapter_for names the base model_pack_id and weights_sha256")
    elif "weights" not in roles:
        problems.append("a pack without adapter_for carries weights")
    if (manifest.get("tuned_from") is not None or adapter is not None) and not all(
        manifest.get(k) for k in ("tuned_from", "training_run_id", "model_card_ref")
    ):
        problems.append("a tuned pack names tuned_from, training_run_id and model_card_ref (TRN-30)")
    licence = manifest.get("licence")
    if not isinstance(licence, Mapping) or licence.get("id") not in APPROVED_LICENCES:
        problems.append("the licence is not on LIST SEC-24a")
    elif not licence.get("attribution") and licence.get("id") in ATTRIBUTION_LICENCES:
        problems.append(f"{licence['id']} needs its attribution text")
    elif adapter is not None and not licence.get("modification_notice"):
        problems.append("a tuned pack keeps the base's attribution with a notice of modification (LIST SEC-24a)")
    return problems


def sign_pack(manifest: Mapping[str, Any], sign: Callable[[str, Mapping[str, Any]], dict[str, Any]],
              report_passed: Callable[[str], bool], settings: Settings | None = None) -> dict[str, Any]:  # fmt: skip
    """The pack builder's last step: refuse a manifest that is not a valid pack or whose evaluation report did not pass
    the release gates, then sign it with a model key (`sign(artefact, document)`)."""
    unsigned = {k: v for k, v in manifest.items() if k != "signature"}
    problems = [p for p in structure_problems({**unsigned, "signature": {}}, settings) if p != "missing signature"]
    if problems:
        raise ModelPackError(TOO_SMALL if any("floor" in p for p in problems) else DAMAGED, "; ".join(problems))
    if not report_passed(str(unsigned["evaluation_report_id"])):
        raise ModelPackError(DAMAGED, f"evaluation report {unsigned['evaluation_report_id']} did not pass the gates")
    return sign(ARTEFACT, unsigned)


def file_entry(path: Path, role: str, chunk_bytes: int) -> dict[str, Any]:
    """A manifest file entry for a file on disk: size, sha256 and per-chunk sha256 (the builder writes these)."""
    whole = hashlib.sha256()
    chunks = []
    with path.open("rb") as handle:
        while block := handle.read(chunk_bytes):
            whole.update(block)
            chunks.append(hashlib.sha256(block).hexdigest())
    return {"path": path.name, "role": role, "size_bytes": path.stat().st_size, "sha256": whole.hexdigest(),
            "chunk_sha256": chunks}  # fmt: skip


def _matches(path: Path, entry: Mapping[str, Any]) -> bool:
    if not path.is_file() or path.stat().st_size != int(entry["size_bytes"]):
        return False
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest() == str(entry["sha256"])


def weights_sha256(manifest: Mapping[str, Any]) -> str | None:
    return next((str(f["sha256"]) for f in manifest.get("files", []) if f.get("role") == "weights"), None)


def verify_pack(folder: Path, environment: Environment, app_version: str, trust: TrustStore | None = None,
                bases: Mapping[str, str] | None = None, settings: Settings | None = None,
                device_channel: str | None = None) -> dict[str, Any]:  # fmt: skip
    """Verify the pack in `folder`; returns its manifest or raises `ModelPackError`. `bases` maps each installed
    base pack id to its weights sha256, for an adapter pack's `adapter_for` (PLT-10). With `device_channel`, a pack
    of a less stable channel is refused (PLT-16)."""
    try:
        manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ModelPackError(DAMAGED, f"{folder.name}: no readable {MANIFEST}") from error
    if not isinstance(manifest, dict):
        raise ModelPackError(DAMAGED, f"{folder.name}: the manifest is not an object")
    problems = structure_problems(manifest, settings)
    if problems:
        raise ModelPackError(TOO_SMALL if any("floor" in p for p in problems) else DAMAGED, "; ".join(problems))
    created = manifest.get("created_at")
    try:
        (trust or bundled_trust()).verify(ARTEFACT, manifest, environment,
                                          signed_at=created if isinstance(created, str) else None)  # fmt: skip
    except SignatureRefused as error:
        raise ModelPackError(DAMAGED, f"{manifest['model_pack_id']}: {error}") from error
    listed = {str(f["path"]) for f in manifest["files"]}
    for entry in manifest["files"]:
        if not _matches(folder / str(entry["path"]), entry):
            raise ModelPackError(DAMAGED, f"{entry['path']} does not match its size and hash")
    strays = {p.name for p in folder.iterdir() if p.name != MANIFEST} - listed
    if strays:
        raise ModelPackError(DAMAGED, f"{sorted(strays)[0]} is not listed in the manifest")
    if _version(app_version) < _version(str(manifest["min_app_version"])):
        raise ModelPackError(APP_TOO_OLD, f"needs app {manifest['min_app_version']} or later")
    if device_channel is not None and not channel_accepts(device_channel, str(manifest.get("channel", "stable"))):
        raise ModelPackError(CHANNEL, f"a {manifest.get('channel')} pack is not for a {device_channel} device")
    adapter = manifest.get("adapter_for")
    if adapter is not None:
        base = (bases or {}).get(str(adapter["model_pack_id"]))
        if base is None or base != adapter["weights_sha256"]:
            raise ModelPackError(ADAPTER_BASE, f"{manifest['model_pack_id']} applies only to "
                                               f"{adapter['model_pack_id']} with its exact weights")  # fmt: skip
    return manifest


@dataclass
class ModelPackStore:
    """Installed model packs under `root` (the data folder's `models/packs`)."""

    root: Path
    environment: Environment
    app_version: str
    # Read at each verification, so a revocation list adopted after the store was made applies at once (SEC-16).
    trust: TrustSource = None
    settings: Settings | None = None
    channel: str | None = None  # the device's channel from its licence (PLT-16); None accepts every channel

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        previous = self.root / PREVIOUS
        if previous.is_dir():
            for backup in sorted(previous.iterdir()):
                if not backup.is_dir() or not PACK_ID.match(backup.name):
                    continue
                target = self.root / backup.name
                if not target.exists():
                    os.replace(backup, target)
                else:
                    shutil.rmtree(backup)
        staging = self.root / STAGING
        if staging.is_dir():  # an install a power loss interrupted never became visible: discard it
            shutil.rmtree(staging)

    def _folders(self) -> list[Path]:
        return sorted(p for p in self.root.iterdir() if p.is_dir() and PACK_ID.match(p.name))

    def _manifest(self, folder: Path) -> dict[str, Any] | None:
        try:
            loaded: dict[str, Any] = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
            return loaded
        except (OSError, ValueError):
            return None

    def bases(self, exclude: str | None = None) -> dict[str, str]:
        """Installed base packs (not adapters) and their weights hash, as recorded in their manifests."""
        out = {}
        for folder in self._folders():
            manifest = self._manifest(folder)
            if folder.name != exclude and manifest and not manifest.get("adapter_for"):
                sha = weights_sha256(manifest)
                if sha:
                    out[folder.name] = sha
        return out

    def verify(self, model_pack_id: str) -> dict[str, Any]:
        """The installed pack, verified again before load (PLT-10, UXS-11)."""
        folder = self.root / model_pack_id
        if not folder.is_dir():
            raise ModelPackError(DAMAGED, f"{model_pack_id} is not installed")
        manifest = verify_pack(folder, self.environment, self.app_version, current_trust(self.trust),
                               self.bases(exclude=model_pack_id), self.settings, self.channel)  # fmt: skip
        adapter = manifest.get("adapter_for")
        if adapter is not None:  # an adapter loads only on its exact base, whose files must still verify
            try:
                self.verify(str(adapter["model_pack_id"]))
            except ModelPackError as error:
                raise ModelPackError(ADAPTER_BASE, f"the base of {model_pack_id} does not verify ({error})") from error
        return manifest

    def install(self, source: Path) -> dict[str, Any]:
        """Install the pack folder `source` (USB kit, school share or a finished download): copy into staging, verify
        there, then one atomic rename. A newer version of an installed id is a new id (ids are never reused)."""
        manifest = self._manifest(source)
        pack_id = str((manifest or {}).get("model_pack_id", ""))
        if not PACK_ID.match(pack_id):
            raise ModelPackError(DAMAGED, f"{source.name}: not a model pack")
        staged = self.root / STAGING / pack_id
        if staged.exists():
            shutil.rmtree(staged)
        staged.mkdir(parents=True)
        for entry in source.iterdir():
            if entry.is_file():
                shutil.copyfile(entry, staged / entry.name)
        try:
            verified = verify_pack(staged, self.environment, self.app_version, current_trust(self.trust),
                                   self.bases(exclude=pack_id), self.settings, self.channel)  # fmt: skip
        except ModelPackError:
            shutil.rmtree(staged)
            raise
        held = self._signed_installed(pack_id)
        if held is not None and _older(verified, held):
            shutil.rmtree(staged)  # an older signed release never replaces the one held (a replayed rollback)
            raise ModelPackError(DAMAGED, f"{pack_id}: older than the installed release; a downgrade is refused")
        target = self.root / pack_id
        backup = self.root / PREVIOUS / pack_id
        if target.exists():
            backup.parent.mkdir(exist_ok=True)
            os.replace(target, backup)
        try:
            os.replace(staged, target)
        except OSError:
            if backup.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        return verified

    def _signed_installed(self, pack_id: str) -> dict[str, Any] | None:
        """The installed manifest of `pack_id` when its signature still verifies (its files may be damaged: a repair
        reinstalls the same release), else None."""
        manifest = self._manifest(self.root / pack_id)
        if manifest is None:
            return None
        created = manifest.get("created_at")
        try:
            current_trust(self.trust).verify(ARTEFACT, manifest, self.environment,
                                             signed_at=created if isinstance(created, str) else None)  # fmt: skip
        except SignatureRefused:
            return None
        return manifest

    def remove(self, model_pack_id: str) -> bool:
        folder = self.root / model_pack_id
        if not folder.is_dir() or not PACK_ID.match(model_pack_id):
            return False
        shutil.rmtree(folder)
        return True

    def installed(self) -> list[dict[str, Any]]:
        """Every installed pack as the model manager lists it, verified now: size, class, tiers, licence and
        attribution (Settings → About → Licences), or the damaged state with its message key."""
        out = []
        for folder in self._folders():
            row: dict[str, Any] = {"model_pack_id": folder.name, "model_id": folder.name}
            manifest: dict[str, Any]
            try:
                manifest = self.verify(folder.name)
            except ModelPackError as error:
                row.update(state="error", error_code=error.code)
                manifest = self._manifest(folder) or {}
            else:
                row.update(state="installed", error_code=None)
            raw_licence = manifest.get("licence")
            licence: Mapping[str, Any] = raw_licence if isinstance(raw_licence, Mapping) else {}
            row.update(hardware_class=manifest.get("hardware_class"), tiers=manifest.get("tiers", []),
                       family=manifest.get("family"), quantisation=manifest.get("quantisation"),
                       size_bytes=sum(int(f.get("size_bytes", 0)) for f in manifest.get("files", [])),
                       licence=licence.get("id"), attribution=licence.get("attribution"),
                       modification_notice=licence.get("modification_notice"),
                       adapter_for=(manifest.get("adapter_for") or {}).get("model_pack_id"))  # fmt: skip
            out.append(row)
        return out

    def link_runtime(self, runtime_dir: Path) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
        """Build the runtime's models folder from the installed packs that verify now (as `prepare_runtime_models`
        does for the M11 manifest): one folder per pack with links to its weights and vision projector. Returns the
        entries in the launch plan's shape (`model`, `file`, `projector`) and the packs left out with why. Adapter
        packs are not linked: the runtime loads merged weights only until LoRA serving is planned."""
        entries: list[dict[str, Any]] = []
        refused: list[tuple[str, str]] = []
        for folder in self._folders():
            try:
                manifest = self.verify(folder.name)
            except ModelPackError as error:
                refused.append((folder.name, error.code))
                continue
            if manifest.get("adapter_for"):
                refused.append((folder.name, "adapter packs are not served yet"))
                continue
            files = {str(f["role"]): f for f in manifest["files"]}
            target = runtime_dir / folder.name
            target.mkdir(parents=True, exist_ok=True)
            weights = files["weights"]
            entry: dict[str, Any] = {"model": folder.name, "projector": None,
                                     "file": {"name": weights["path"], "bytes": weights["size_bytes"]}}  # fmt: skip
            for role in ("weights", "vision_projector"):
                if role in files:
                    os.symlink((folder / str(files[role]["path"])).resolve(), target / str(files[role]["path"]))
            if "vision_projector" in files:
                entry["projector"] = {"name": files["vision_projector"]["path"],
                                      "bytes": files["vision_projector"]["size_bytes"]}  # fmt: skip
            entries.append(entry)
        return entries, refused

    def licences(self) -> list[dict[str, Any]]:
        """Settings → About → Licences: every installed pack's licence and attribution (PLT-10)."""
        return [{"model_pack_id": r["model_pack_id"], "licence": r["licence"], "attribution": r["attribution"],
                 "modification_notice": r["modification_notice"]} for r in self.installed()]  # fmt: skip
