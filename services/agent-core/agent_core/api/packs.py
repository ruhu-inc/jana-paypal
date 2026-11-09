"""Packs, models and jobs over the local API (API-29), reporting on the app channel (API-22).

- Content packs and model packs install from a file, a folder or the USB kit with no network (PLT-12, PLT-13); a
  download needs the device-wide `downloads` switch and otherwise answers `feature_disabled`, whatever token comes
  with it (D53). While offline a download is accepted and queued; the shell's download manager resumes it when the
  connectivity layer reports `online` (PLT-11).
- Downloads run in the shell (ARC-06: the shell's `net` module is the enforcement point for `downloads`). The core
  verifies the pack's signed manifest first and hands the shell each file's URL, size, sha256 and chunk hashes
  (`control.download`); the shell reports its states back (`control.download_state`). When the shell has moved the
  verified files into `models/incoming/<id>/`, the core places the signed manifest beside them and installs the pack
  through the model pack store, which verifies everything again before one atomic rename (UXS-11).
- Every endpoint takes the launch token only (TABLE UXS-46a: Tutor models and Content are open sections). Long work
  returns `JobAccepted` (202) and reports `pack.install`, `model.download` and `job.update` events on the app channel.

The shell's states map onto the contract's: `waiting_for_network` is `queued` with `error_code` `waiting_for_network`
("Waiting for internet"), `installing` is `verifying`, and a cancelled download is `failed` with `cancelled` on the
app channel and `cancelled` in `DownloadState`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CoreService, require
from agent_core.api.errors import ApiError
from agent_core.config import Settings
from agent_core.config.environments import Environment
from agent_core.config.operations import DevicePolicy
from agent_core.config.revocation import RevocationStore
from agent_core.config.trust import SignatureRefused, TrustStore, bundled_trust
from agent_core.contracts import errors as contract_errors
from agent_core.engine.events import new_id
from agent_core.store import kit_install
from agent_core.store.ids import CROCKFORD
from agent_core.store.model_pack_store import ARTEFACT as MODEL_ARTEFACT
from agent_core.store.model_pack_store import DAMAGED, MANIFEST, PACK_ID, ModelPackError, ModelPackStore
from agent_core.store.pack_channels import KIT_FILE, ChannelError, kit_listing
from agent_core.store.pack_store import PackStore
from agent_core.store.packs import PackRejectedError
from agent_core.store.perception_packs import PerceptionPackError, PerceptionPackStore
from agent_core.store.speech_packs import PACK_ID as SPEECH_PACK_ID
from agent_core.store.speech_packs import SpeechPackError, SpeechPackStore

Mutating = Callable[[Request, Callable[[Any], Awaitable[tuple[int, Any]]]], Awaitable[Response]]
SHELL_TO_CONTRACT = {"waiting_for_network": "queued", "installing": "verifying", "cancelled": "failed"}
DOWNLOAD_STATES = ("queued", "downloading", "paused", "verifying", "done", "failed", "cancelled")
ACTIONS = ("pause", "resume", "cancel")


class StorageFullError(OSError):
    """Too little free disk to copy an install source (PLT-06, PLT-23): nothing was copied."""

    def __init__(self, needed_bytes: int, free_bytes: int) -> None:
        super().__init__("storage_full")
        self.code = "storage_full"
        self.needed_bytes, self.free_bytes = needed_bytes, free_bytes


def _free_disk(path: Path) -> int:
    """Free bytes on the disk that holds `path` (its nearest existing folder)."""
    while not path.exists() and path != path.parent:
        path = path.parent
    return shutil.disk_usage(path).free


def _source_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else 0


class DownloadsPort(Protocol):
    """The shell's download manager, reached over the control channel (`control.download`, PLT-11)."""

    def start(self, request: dict[str, Any]) -> None: ...

    def control(self, download_id: str, action: str) -> None: ...

    def configure(self, download_id: str, limits: dict[str, Any]) -> None: ...


@dataclass
class ControlDownloads:
    """The production port: writes `control.download` frames to the shell (PLT-11)."""

    say: Callable[[dict[str, Any]], None]

    def start(self, request: dict[str, Any]) -> None:
        self.say({"type": "control.download", **request})

    def control(self, download_id: str, action: str) -> None:
        self.say({"type": "control.download_control", "download_id": download_id, "action": action})

    def configure(self, download_id: str, limits: dict[str, Any]) -> None:
        self.say({"type": "control.download_control", "download_id": download_id,
                  "action": "configure", "limits": limits})  # fmt: skip


@dataclass
class PackDesk:
    """The packs and models the API works on, and the downloads the shell runs for it."""

    data_dir: Path
    environment: Environment
    app_version: str
    publish: Callable[[dict[str, Any]], Any]
    downloads_on: Callable[[], bool] = lambda: False
    online: Callable[[], bool] = lambda: False
    free_bytes: Callable[[], int] | None = None  # None: the data folder's disk, measured
    disk_margin: float = field(default_factory=lambda: Settings().download_disk_margin_share)
    pause_free_bytes: int = field(default_factory=lambda: int(Settings().download_pause_free_mb * 1024 * 1024))
    shell: DownloadsPort | None = None
    trust: TrustStore | None = None
    revocations: RevocationStore | None = None
    channel: str | None = None
    available: Callable[[], list[dict[str, Any]]] = lambda: []  # downloadable model packs (signed manifests, URLs)
    on_kit: Callable[[Path], None] | None = None
    # Told what installed from a file, folder or kit, on the event loop: an installed correction re-marks (CUR-36).
    on_installed: Callable[[list[str]], Any] | None = None
    operations: DevicePolicy | None = None
    packs: PackStore = field(init=False)
    models: ModelPackStore = field(init=False)
    perception: PerceptionPackStore = field(init=False)
    # Speech packs (VOC-03) install from a USB or file folder like model packs; the voice launcher owns the store and is
    # told after an install so the pack is offered without a restart.
    speech: SpeechPackStore | None = None
    on_speech: Callable[[], Any] | None = None
    # Told the id of a speech pack about to be removed, so a sidecar running from it stops first.
    before_speech_remove: Callable[[str], Any] | None = None
    downloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    _installs: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.free_bytes is None:
            self.free_bytes = lambda: _free_disk(self.data_dir)
        self.packs = PackStore(self.data_dir / "packs", self.environment, self.trust, revocations=self.revocations,
                               channel=self.channel)  # fmt: skip
        self.models = ModelPackStore(self.data_dir / "models" / "packs", self.environment, self.app_version,
                                     self._trust, channel=self.channel)  # fmt: skip
        self.perception = PerceptionPackStore(
            self.data_dir / "packs" / "perception", self.environment, self.app_version, self._trust, self.channel
        )
        path = self.data_dir / "models" / "downloads.json"
        if path.is_file():
            try:
                records = json.loads(path.read_text())
                for key, record in records.items():
                    model_id = str(record["model_id"])
                    if (
                        record["download_id"] != key
                        or not key.startswith("dl_")
                        or len(key) != 29
                        or any(c not in CROCKFORD for c in key[3:])
                        or not PACK_ID.fullmatch(model_id)
                        or record["state"] not in DOWNLOAD_STATES
                        or record["manifest"]["model_pack_id"] != model_id
                    ):
                        continue
                    self._trust().verify(
                        MODEL_ARTEFACT,
                        record["manifest"],
                        self.environment,
                        signed_at=str(record["manifest"].get("created_at")),
                    )
                    record["incoming"] = str(self.data_dir / "models" / "incoming" / model_id)
                    self.downloads[key] = record
            except (OSError, ValueError, KeyError, TypeError, AttributeError, SignatureRefused):
                self.downloads.clear()

    def _save_downloads(self) -> None:
        path = self.data_dir / "models" / "downloads.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.downloads, sort_keys=True))
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, path)

    def _send_download(self, record: dict[str, Any]) -> None:
        if self.shell is None:
            return
        manifest = record["manifest"]
        files = [
            {
                "name": f["path"],
                "url": f"{record['base_url'].rstrip('/')}/{f['path']}",
                "size_bytes": f["size_bytes"],
                "sha256": f["sha256"],
                "chunk_sha256": f["chunk_sha256"],
                "chunk_bytes": manifest.get("chunk_bytes"),
            }
            for f in manifest["files"]
        ]
        self.shell.start(
            {
                "download_id": record["download_id"],
                "item": {"kind": "model_pack", "id": record["model_id"]},
                "files": files,
                "target": record["incoming"],
            }
        )
        if record.get("limits") is not None:
            configure = getattr(self.shell, "configure", None)
            if configure is not None:
                configure(record["download_id"], record["limits"])

    def recover_downloads(self) -> None:
        """After ready: reconnect active jobs or finish a shell/core interrupted after the final rename."""
        for record in self.downloads.values():
            if record["state"] in ("done", "failed", "cancelled"):
                continue
            incoming = Path(record["incoming"])
            installed_manifest = self.models.root / record["model_id"] / MANIFEST
            already_installed = False
            if installed_manifest.is_file():
                try:
                    already_installed = json.loads(installed_manifest.read_text()) == record["manifest"]
                except (OSError, ValueError):
                    pass
            if incoming.is_dir() or already_installed:
                self._begin_install(record)
            elif "base_url" in record:
                self._send_download(record)
                if record["state"] == "paused" and self.shell is not None:
                    self.shell.control(record["download_id"], "pause")

    async def close_downloads(self) -> None:
        if self._installs:
            await asyncio.gather(*self._installs.values(), return_exceptions=True)

    def _trust(self) -> TrustStore:
        if self.revocations is not None:
            return self.revocations.trust()
        return self.trust or bundled_trust()

    def set_channel(self, channel: str) -> None:
        if channel not in {"dev", "pilot", "stable"}:
            raise ValueError("OPS-02: invalid channel")
        self.channel = self.packs.channel = channel
        if self.operations is not None:
            self.operations = DevicePolicy(self.data_dir, self.environment, channel, self.app_version, self._trust)

    def _offers(self) -> list[dict[str, Any]]:
        """Persisted downloads keep their rows and retry manifests even when the catalogue is temporarily absent."""
        remembered = {
            r["model_id"]: {"model_pack_id": r["model_id"], "manifest": r["manifest"], "base_url": r["base_url"]}
            for r in self.downloads.values()
            if "base_url" in r
        }
        remembered.update({str(o["model_pack_id"]): o for o in self.available()})
        offers = list(remembered.values())
        if self.operations is None:
            return offers
        return [
            offer
            for offer in offers
            if self.operations.offered(
                "model_pack",
                str(offer["model_pack_id"]),
                str(offer.get("manifest", {}).get("manifest_version", "")),
                format_major=Settings().ops_model_format_major,
            )
        ]

    # --- downloads (PLT-11, API-29) -------------------------------------------------------------------------------

    def offer(self, model_pack_id: str) -> dict[str, Any]:
        entry = next((a for a in self._offers() if a.get("model_pack_id") == model_pack_id), None)
        if entry is None:
            raise ApiError("not_found", "no such model pack")
        manifest = entry["manifest"]
        if manifest.get("model_pack_id") != model_pack_id or contract_errors("ops/model-pack", manifest):
            raise ApiError("not_found", "the offered model manifest is invalid")
        try:
            self._trust().verify(MODEL_ARTEFACT, manifest, self.environment, signed_at=str(manifest.get("created_at")))
        except SignatureRefused as error:
            raise ApiError("not_found", f"the offered model pack does not verify ({error})") from None
        return dict(entry)

    def start_download(self, model_pack_id: str) -> dict[str, Any]:
        if not self.downloads_on():
            raise ApiError("feature_disabled", "downloads are off; copy the model from a USB stick")
        offer = self.offer(model_pack_id)
        existing = next(
            (
                r
                for r in self.downloads.values()
                if r["model_id"] == model_pack_id and r["state"] not in ("done", "failed", "cancelled")
            ),
            None,
        )
        if existing is not None:
            return {"download_id": existing["download_id"], "queued_until_online": not self.online()}
        size = sum(int(f["size_bytes"]) for f in offer["manifest"]["files"])
        # PLT-11: the size plus a margin must be free; PLT-23: at or under the low-disk floor no download starts.
        if self._free() < size * (1 + self.disk_margin) or self._free() <= self.pause_free_bytes:
            raise ApiError("storage_full", "not enough free space for this model",
                           details={"needed_bytes": int(size * (1 + self.disk_margin)),
                                    "free_bytes": self._free()})  # fmt: skip
        if self.shell is None:
            raise ApiError("feature_disabled", "downloads are unavailable in this build; install from a USB stick")
        download_id = new_id("dl")
        incoming = self.data_dir / "models" / "incoming" / model_pack_id
        manifest = offer["manifest"]
        self.downloads[download_id] = {"download_id": download_id, "model_id": model_pack_id, "state": "queued",
                                       "bytes_done": 0, "bytes_total": size, "manifest": manifest,
                                       "incoming": str(incoming), "base_url": offer["base_url"]}  # fmt: skip
        self._save_downloads()
        self._send_download(self.downloads[download_id])
        self._announce(download_id, None, None)
        return {"download_id": download_id, "queued_until_online": not self.online()}

    def control(self, download_id: str, action: str) -> dict[str, Any]:
        record = self.downloads.get(download_id)
        if record is None or action not in ACTIONS:
            raise ApiError("not_found", "no such download")
        if download_id in self._installs and not self._installs[download_id].done():
            raise ApiError("validation_failed", "the verified model is being installed; wait for it to finish")
        if (
            self.shell is not None
            and record["state"] != "done"
            and (action == "cancel" or record["state"] not in ("failed", "cancelled"))
        ):
            self.shell.control(download_id, action)
        if action == "cancel" and record["state"] not in ("done",):
            record["state"] = "cancelled"
            self._announce(download_id, None, "cancelled")
        return self.state(download_id)

    def state(self, download_id: str) -> dict[str, Any]:
        record = self.downloads[download_id]
        return {"download_id": download_id, "state": record["state"], "bytes_done": record["bytes_done"],
                "bytes_total": record["bytes_total"],
                "limits": record.get("limits", {"bandwidth_bytes_per_s": None, "allow_metered": False})}  # fmt: skip

    def configure(self, download_id: str, limits: Any) -> dict[str, Any]:
        record = self.downloads.get(download_id)
        if record is None or record["state"] in ("done", "failed", "cancelled"):
            raise ApiError("not_found", "no active download")
        if contract_errors("api/commands#/$defs/DownloadLimits", limits):
            raise ApiError("validation_failed", "invalid download limits")
        configure = getattr(self.shell, "configure", None)
        if configure is None:
            raise ApiError("validation_failed", "download controls are unavailable in this build")
        record["limits"] = dict(limits)
        self._save_downloads()
        configure(download_id, record["limits"])
        return self.state(download_id)

    def on_shell_state(self, frame: Mapping[str, Any]) -> None:
        """A `control.download_state` from the shell: record it, and install once the files are in place."""
        record = self.downloads.get(str(frame.get("download_id")))
        if record is None or record["state"] in ("done", "cancelled"):
            return
        shell_state = str(frame["state"])
        record["bytes_done"] = int(frame.get("bytes_done", record["bytes_done"]))
        record["bytes_total"] = int(frame.get("bytes_total", record["bytes_total"]))
        error = frame.get("error_code")
        if shell_state == "done":
            record["state"] = "verifying"
            self._announce(record["download_id"], frame.get("eta_s"), None)
            self._begin_install(record)
            return
        record["state"] = "cancelled" if shell_state == "cancelled" else SHELL_TO_CONTRACT.get(shell_state, shell_state)
        if shell_state == "waiting_for_network":
            error = error or "waiting_for_network"
        self._announce(record["download_id"], frame.get("eta_s"), error)

    def _verify_download(self, record: dict[str, Any]) -> None:
        incoming = Path(record["incoming"])
        installed = self.models.root / record["model_id"]
        if not incoming.exists() and (installed / MANIFEST).is_file():
            manifest = self.models.verify(record["model_id"])
            if manifest != record["manifest"]:
                raise ModelPackError(DAMAGED, "the installed model is not this download")
            return
        try:
            (incoming / MANIFEST).write_text(json.dumps(record["manifest"], indent=1), encoding="utf-8")
            self.models.install(incoming)
        finally:
            shutil.rmtree(incoming, ignore_errors=True)

    def _begin_install(self, record: dict[str, Any]) -> None:
        if record["download_id"] in self._installs:
            return
        record["state"] = "verifying"
        self._announce(record["download_id"], None, None)

        async def finish() -> None:
            try:
                await asyncio.to_thread(self._verify_download, dict(record))
            except (ModelPackError, OSError) as error:
                record["state"] = "failed"
                self._announce(record["download_id"], None, getattr(error, "code", DAMAGED))
            else:
                record["state"] = "done"
                record["bytes_done"] = record["bytes_total"]
                self._announce(record["download_id"], 0, None)

        self._installs[record["download_id"]] = asyncio.get_running_loop().create_task(finish())

    def _announce(self, download_id: str, eta_s: Any, error_code: Any) -> None:
        record = self.downloads[download_id]
        state = record["state"]
        event_state = "failed" if state == "cancelled" else state
        record["error_code"] = error_code  # the model manager's row reads it with the catalogue (UXS-11)
        record["eta_s"] = int(eta_s) if isinstance(eta_s, int | float) else None
        self._save_downloads()
        self.publish({"type": "model.download", "download_id": download_id, "model_id": record["model_id"],
                      "state": event_state, "bytes_done": record["bytes_done"], "bytes_total": record["bytes_total"],
                      "eta_s": int(eta_s) if isinstance(eta_s, int | float) else None,
                      "error_code": (error_code or ("cancelled" if state == "cancelled" else None))})  # fmt: skip

    def _free(self) -> int:
        assert self.free_bytes is not None
        return int(self.free_bytes())

    def _room_for(self, size: int) -> None:
        """PLT-06, PLT-23: before anything is copied, the source's size plus the margin must be free; otherwise the
        install stops with `storage_full`, naming the space needed and the space free."""
        needed, free = int(size * (1 + self.disk_margin)), self._free()
        if free < needed:
            raise StorageFullError(needed, free)

    # --- installs from a file, a folder or the USB kit (PLT-12, PLT-13) ------------------------------------------

    def install_source(self, source: Mapping[str, Any], job_id: str) -> list[str]:
        """Install synchronously (the caller runs it off the event loop); returns what was installed. Free disk is
        checked before anything is copied (PLT-06)."""
        path = Path(str(source["path"]))

        def say(item: str, state: str) -> None:
            self.publish({"type": "pack.install", "job_id": job_id, "pack_id": item[:96], "state": state,
                          "progress": None, "error_code": "pack.damaged" if state == "failed" else None})  # fmt: skip

        if (path / KIT_FILE).is_file():
            listing = kit_listing(path, self.environment, self._trust())
            if listing.candidate and self.channel not in {"dev", "pilot"}:
                raise ChannelError("OPS-15: candidate kit cannot install on a stable device")
            if self.operations is not None:
                if self.revocations is not None and listing.revocation_list is not None:
                    self.revocations.adopt(listing.revocation_list.model_dump(mode="json"))
                self.operations.adopt_folder(path)
            self._room_for(sum(entry.size_bytes for entry in listing.files))
            if self.on_kit is not None:
                self.on_kit(path)
            report = kit_install.install_kit(path, self.packs, self.models, self.environment, trust=self._trust(),
                                             revocations=self.revocations, progress=say,
                                             speech=self.speech)  # fmt: skip
            if report.speech and self.on_speech is not None:
                self.on_speech()  # offered without a restart (VOC-03)
            if report.refused and not (report.content or report.models or report.speech):
                raise PackRejectedError(f"nothing in the kit installed: {report.refused[0][0]}")
            return [*report.content, *report.models, *report.speech]
        self._room_for(_source_bytes(path))
        if path.is_dir() and (path / MANIFEST).is_file():
            item = path.name
            try:
                manifest = json.loads((path / MANIFEST).read_bytes())
            except (OSError, ValueError):
                raise PackRejectedError("invalid pack manifest") from None
            if "speech_pack_id" in manifest:
                if self.speech is None:
                    raise PackRejectedError("speech packs are not supported on this computer")
                say(item, "verifying")
                try:
                    item = self.speech.install(path).name
                except SpeechPackError:
                    say(item, "failed")
                    raise
                if self.on_speech is not None:
                    self.on_speech()
                say(item, "done")
                return [item]
            if manifest.get("format") == "jana.perception.1":
                say(item, "verifying")
                try:
                    item = self.perception.install(path)
                except PerceptionPackError:
                    say(item, "failed")
                    raise
                say(item, "done")
                return [item]
            say(item, "verifying")
            try:
                self.models.install(path)
            except ModelPackError:
                say(item, "failed")
                raise
            say(item, "done")
            return [item]
        item = path.stem
        say(item, "verifying")
        try:
            if self.operations is not None:
                from agent_core.store.packs import read_signed_files

                try:
                    embedded_files = list(read_signed_files(path).values())
                except (sqlite3.Error, ValueError, KeyError, TypeError) as error:
                    raise PackRejectedError("OPS-10: damaged embedded operations inventory") from error
                for embedded in embedded_files:
                    if embedded["kind"] == "revocation_list" and self.revocations is not None:
                        self.revocations.adopt(embedded["document"])
                for embedded in embedded_files:
                    if embedded["kind"] == "compatibility_matrix":
                        self.operations.adopt_matrix(embedded["document"])
                    elif embedded["kind"] == "safety_configuration":
                        self.operations.adopt_safety(embedded["document"])
            manifest = self.packs.install(path)
        except (PackRejectedError, OSError):
            say(item, "failed")
            raise
        say(str(manifest["pack_id"]), "done")
        return [str(manifest["pack_id"])]

    def remove(self, pack_id: str) -> bool:
        if pack_id.startswith("sp."):
            return self._remove_speech(pack_id)
        if pack_id.startswith("pp."):
            return self.perception.remove(pack_id)
        if PACK_ID.match(pack_id):
            return self.models.remove(pack_id)
        return self.packs.remove(pack_id)

    def _remove_speech(self, pack_id: str) -> bool:
        """A speech pack leaves like any other pack: its sidecar stops first, then voice is offered again from whatever
        pack remains, or read-aloud falls back to the OS synthesiser (VOC-03, VOC-14)."""
        if self.speech is None or not SPEECH_PACK_ID.match(pack_id) or not (self.speech.root / pack_id).is_dir():
            return False
        if self.before_speech_remove is not None:
            self.before_speech_remove(pack_id)
        try:
            removed = self.speech.remove(pack_id)
        finally:
            if self.on_speech is not None:
                self.on_speech()
        return removed

    # --- the model manager's list (UXS-11) ------------------------------------------------------------------------

    def catalogue(self, base: dict[str, Any]) -> dict[str, Any]:
        installed = self.models.installed()
        names = {row["model_pack_id"] for row in installed}
        active = {r["model_id"]: r for r in self.downloads.values() if r["state"] not in ("done", "cancelled")}
        available = []
        for offer in self._offers():
            pack_id = str(offer.get("model_pack_id"))
            if pack_id in names:
                continue
            manifest = offer.get("manifest", {})
            row = {"model_pack_id": pack_id, "hardware_class": manifest.get("hardware_class"),
                   "size_bytes": sum(int(f.get("size_bytes", 0)) for f in manifest.get("files", [])),
                   "licence": (manifest.get("licence") or {}).get("id")}  # fmt: skip
            if pack_id in active:
                record = active[pack_id]
                row["download"] = {**self.state(record["download_id"]), "error_code": record.get("error_code"),
                                   "eta_s": record.get("eta_s")}  # fmt: skip
            available.append(row)
        # Speech packs are listed with each component's licence and attribution for Settings → About → Licences
        # (VOC-02); their rows carry no model id, so the model manager's rows ignore them.
        speech = self.speech.installed() if self.speech is not None else []
        return {**base, "installed": [*base.get("installed", []), *installed, *speech], "available": available}

    def assignment(self, tier: str, model_id: str) -> dict[str, Any]:
        known = {row["model_pack_id"] for row in self.models.installed() if row["state"] == "installed"}
        if model_id not in known:
            raise ApiError("not_found", "no such installed model")
        path = self.data_dir / "models" / "assignment.json"
        current = json.loads(path.read_text()) if path.is_file() else {}
        current[tier] = model_id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, sort_keys=True))
        return {"tier": tier, "model_id": model_id}


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    def desk() -> PackDesk:
        found = getattr(core, "pack_desk", None)
        if not isinstance(found, PackDesk):
            raise ApiError("not_found", "packs are not available")
        return found

    def job(kind: str) -> str:
        job_id = new_id("job")
        core.jobs[job_id] = {"job_id": job_id, "kind": kind, "state": "queued", "progress": None,
                             "result_ref": None, "error": None}  # fmt: skip
        return job_id

    async def run_install(job_id: str, source: Mapping[str, Any]) -> None:
        d = desk()
        core.jobs[job_id]["state"] = "running"
        try:
            installed = await asyncio.to_thread(d.install_source, source, job_id)
        except StorageFullError as error:
            core.jobs[job_id].update(state="failed", error={
                "code": "storage_full", "message": "not enough free space to copy these files",
                "details": {"needed_bytes": error.needed_bytes, "free_bytes": error.free_bytes}})  # fmt: skip
            return
        except (PackRejectedError, ModelPackError, PerceptionPackError, SpeechPackError, ChannelError,
                OSError) as error:  # fmt: skip
            code = getattr(error, "code", "pack.damaged")
            core.jobs[job_id].update(state="failed", error={"code": "validation_failed", "message": str(code)})
            return
        core.jobs[job_id].update(state="done", result_ref=",".join(installed)[:400])
        if d.on_installed is not None:
            d.on_installed(installed)

    @app.post("/v1/packs/install")
    async def install(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = require("InstallPack", body)
            source = asked["source"]
            d = desk()
            if source["kind"] == "download":
                if not d.downloads_on():
                    raise ApiError("feature_disabled", "downloads are off; install from a USB stick instead")
                accepted = d.start_download(source["pack_id"])
                job_id = job("pack_install")
                core.jobs[job_id]["result_ref"] = accepted["download_id"]
                return 202, {"job_id": job_id, "queued_until_online": accepted["queued_until_online"]}
            job_id = job("pack_install")
            core.app_tasks.append(asyncio.get_running_loop().create_task(run_install(job_id, source)))
            return 202, {"job_id": job_id, "queued_until_online": False}

        return await mutating(request, run)

    @app.delete("/v1/packs/{pack_id}")
    async def remove(pack_id: str, request: Request) -> Response:
        async def run(_: Any) -> tuple[int, Any]:
            if not desk().remove(pack_id):
                raise ApiError("not_found", "no such pack")
            return 204, None

        return await mutating(request, run)

    @app.post("/v1/models/{model_id}/download")
    async def download(model_id: str, request: Request) -> Response:
        async def run(_: Any) -> tuple[int, Any]:
            return 202, desk().start_download(model_id)

        return await mutating(request, run)

    @app.post("/v1/models/downloads/{download_id}/{action}")
    async def control(download_id: str, action: str, request: Request) -> Response:
        async def run(_: Any) -> tuple[int, Any]:
            return 200, desk().control(download_id, action)

        return await mutating(request, run)

    @app.put("/v1/models/assignment")
    async def assign(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = require("ModelAssignment", body)
            return 200, desk().assignment(asked["tier"], asked["model_id"])

        return await mutating(request, run)

    @app.put("/v1/models/downloads/{download_id}/limits")
    async def limits(download_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            return 200, desk().configure(download_id, body)

        return await mutating(request, run)

    @app.post("/v1/models/benchmark")
    async def benchmark(request: Request) -> Response:
        async def run(_: Any) -> tuple[int, Any]:
            job_id = job("benchmark")
            start = getattr(core, "start_benchmark", None)
            if start is None or start() is None:
                core.jobs[job_id].update(state="failed", error={"code": "model_unavailable",
                                                                "message": "no tutor model is running"})  # fmt: skip
            else:
                core.jobs[job_id]["state"] = "running"
            core.publish_app({"type": "job.update", "job_id": job_id, "kind": "benchmark",
                              "state": core.jobs[job_id]["state"], "progress": None, "result_ref": None})  # fmt: skip
            return 202, {"job_id": job_id, "queued_until_online": False}

        return await mutating(request, run)

    @app.get("/v1/events")
    async def events(request: Request, cursor: str | None = None) -> Any:
        from fastapi.responses import StreamingResponse

        channel = core.app_channel
        connection, replayed, resumed = channel.open(request.headers.get("last-event-id") or cursor)
        return StreamingResponse(channel.stream(connection, replayed, resumed), media_type="text/event-stream",
                                 headers={"cache-control": "no-store", "x-accel-buffering": "no"})  # fmt: skip
