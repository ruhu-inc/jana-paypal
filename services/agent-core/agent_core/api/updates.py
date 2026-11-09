"""Device-wide update commands and the shutdown handoff (PLT-14, PLT-15, SEC-15)."""

from __future__ import annotations

import asyncio
import platform as host_platform
import shutil
import tempfile
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CoreService
from agent_core.api.errors import ApiError
from agent_core.config.environments import Environment
from agent_core.config.revocation import RevocationStore
from agent_core.config.trust import TrustStore
from agent_core.contracts import errors as contract_errors
from agent_core.engine.events import new_id
from agent_core.store.pack_channels import ChannelError, from_kit, kit_listing
from agent_core.store.updates import (
    OFFLINE_ARTEFACT,
    HealthCheck,
    Platform,
    UpdateRefused,
    UpdateState,
    adopt_revocations,
    check_manifest,
    read_update_pack,
)


def this_platform() -> Platform:
    os_family = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[host_platform.system()]
    return Platform(os_family, "arm64" if host_platform.machine().lower() in ("arm64", "aarch64") else "x86_64")


class UpdateDesk:
    def __init__(self, core: CoreService, data_dir: Path, environment: Environment, app_version: str,
                 *, platform: Platform | None = None, trust: TrustStore | None = None,
                 say: Callable[[dict[str, Any]], None] | None = None) -> None:  # fmt: skip
        self.core, self.environment, self.app_version = core, environment, app_version
        self.platform, self.trust, self.say = platform or this_platform(), trust, say
        self.state = UpdateState(data_dir)
        self.revocations = RevocationStore(data_dir, environment, trust)
        self.tasks: set[asyncio.Task[None]] = set()
        self.exit_action: str | None = None
        self.exit_held = False
        self._lock = threading.Lock()
        held = self.state.read()
        # A staged new version is checked before the installed record changes. Keep the cached previous installer.
        if held["phase"] not in ("installing", "rollback_offered"):
            self.state.record_installed(app_version)
        else:
            core.recovery_required = held["phase"] == "rollback_offered"

    def device_channel(self) -> str:
        return str(getattr(self.core.pack_desk, "channel", None) or "stable")

    def cache_from_kit(self, folder: Path) -> None:
        """A signed kit may carry the current .janaupdate, preserving rollback without a network."""
        listing = kit_listing(folder, self.environment, self.revocations.trust())
        with tempfile.TemporaryDirectory(dir=self.state.folder) as scratch:
            for entry in listing.files:
                if entry.kind != "app_update":
                    continue
                try:
                    copied = from_kit(folder, entry.path, Path(scratch) / "current.janaupdate",
                                      self.environment, self.revocations.trust())  # fmt: skip
                    verified = read_update_pack(copied, installed_version=self.app_version,
                                                device_channel=self.device_channel(), platform=self.platform,
                                                environment=self.environment, trust=self.revocations.trust(),
                                                allow_installed=True)  # fmt: skip
                    if verified.version == self.app_version:
                        with self._lock:
                            self.state.stage(verified, cache_current=True)
                except (UpdateRefused, ChannelError, OSError):
                    continue  # a kit can contain installers for other platforms and app versions

    def blocker(self, *, rollback: bool = False) -> str | None:
        core = self.core
        if core._turn_running():
            return "turn_active"
        mock_running = any(e.engine_state.mock_running for e in core.sessions.values())
        if (core.mock_active and core.mock_active()) or mock_running:
            return "mock_running"
        if core.pack_desk is not None and any(
            d["state"] not in ("done", "failed", "cancelled") for d in core.pack_desk.downloads.values()
        ):
            return "download_active"
        if self.tasks or any(not t.done() for t in core.app_tasks):
            return "verification_running"
        if self.say is None:
            return "shell_unavailable"
        state = self.state.read()
        if state.get("failure_code"):
            return "installation_failed"
        try:
            previous = state.get("previous" if rollback else "installed") or {}
            manifest = previous.get("manifest")
            if self.environment is Environment.PRODUCTION and not isinstance(manifest, dict):
                return "rollback_unavailable"
            if isinstance(manifest, dict):
                check_manifest(manifest, OFFLINE_ARTEFACT, installed_version=previous["version"],
                               device_channel=self.device_channel(), platform=self.platform,
                               environment=self.environment, trust=self.revocations.trust(),
                               allow_installed=True)  # fmt: skip
            cached = previous.get("installer")
            if not isinstance(cached, str) or not self.state.local_path(cached).is_file():
                return "rollback_unavailable"
        except (UpdateRefused, OSError):
            return "rollback_unavailable"
        return None

    def status(self) -> dict[str, Any]:
        state = self.state.read()
        # The installed offline updater is available now; the automatic online updater is not wired yet.
        checks_on = False
        return {"installed_version": self.app_version, "channel": self.device_channel(),
                "checks": "online" if checks_on else "usb_or_school_only",
                "staged": {**state["staged"]["summary"], "accepted": state.get("accepted", False)}
                    if state.get("staged") else None,
                "rollback_offered": (state.get("previous") or {}).get("version")
                    if state["phase"] == "rollback_offered" else None,
                "apply_blocked": self.blocker(rollback=state["phase"] == "rollback_offered")}  # fmt: skip

    def stage(self, path: Path) -> dict[str, Any]:
        self.core._guard()
        if self.tasks or self.exit_action is not None or self.state.read()["phase"] == "rollback_offered":
            raise ApiError("validation_failed", "an update operation is already in progress")
        job_id = new_id("job")
        job: dict[str, Any] = {"job_id": job_id, "kind": "update_install", "state": "running", "progress": None,
               "result_ref": None, "error": None}  # fmt: skip
        self.core.jobs[job_id] = job

        def verify() -> None:
            verified = read_update_pack(path, installed_version=self.app_version, device_channel=self.device_channel(),
                                        platform=self.platform, environment=self.environment,
                                        trust=self.revocations.trust(), allow_installed=True)  # fmt: skip
            size = verified.manifest["installer"]["size_bytes"]
            if shutil.disk_usage(self.state.data_dir).free < size * (1 + self.core.settings.download_disk_margin_share):
                raise UpdateRefused("storage_full", "not enough disk space to stage the installer")
            with self._lock:
                self.state.stage(verified, cache_current=verified.version == self.app_version)
                adopt_revocations(verified, self.revocations)
                policy = getattr(self.core.pack_desk, "operations", None)
                if policy is not None:
                    policy.adopt_matrix(verified.manifest.get("compatibility_matrix", {}))
                    policy.adopt_safety(verified.manifest.get("safety_configuration", {}))

        async def run() -> None:
            try:
                await asyncio.to_thread(verify)
            except (UpdateRefused, OSError, ValueError) as error:
                code = "storage_full" if getattr(error, "code", None) == "storage_full" else "validation_failed"
                job.update(state="failed", error={"code": code,
                                                  "message": getattr(error, "code", "damaged")})  # fmt: skip
            else:
                job.update(state="done", result_ref="updates/staged")
                # CUR-36: a withdrawal the update carried completes a correction already installed promptly.
                due = getattr(self.core, "corrections_due", None)
                if due is not None:
                    due()
            self.core.publish_app({"type": "job.update", **{k: job[k] for k in
                                  ("job_id", "kind", "state", "progress", "result_ref")}})  # fmt: skip

        task = asyncio.create_task(run())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return {"job_id": job_id, "queued_until_online": False}

    def request_exit(self, action: str) -> None:
        self.core._guard(allow_recovery=action == "rollback")
        state = self.state.read()
        rollback = action == "rollback"
        if (
            action not in ("install-at-quit", "restart-now", "rollback")
            or (rollback and state["phase"] != "rollback_offered")
            or (not rollback and not state.get("staged"))
        ):
            raise ApiError("not_found", "no such pending update action")
        reason = self.blocker(rollback=rollback)
        if reason is not None:
            raise ApiError("turn_active" if reason == "turn_active" else "request_conflict",
                           "the update cannot install yet", details={"reason": reason})  # fmt: skip
        try:
            previous = state.get("previous" if rollback else "installed") or {}
            self.state.installer_plan(previous, "rollback")
        except (UpdateRefused, OSError) as error:
            raise ApiError("validation_failed", "the previous installer could not be verified") from error
        if not rollback:
            state["accepted"] = True
            self.state._write(state)
        if action == "install-at-quit":
            return
        self.exit_action = action
        # Stop accepting new learning and pack operations between acceptance and the shell's shutdown.
        self.core.shutting_down = True
        assert self.say is not None
        self.say({"type": "control.exit_requested"})

    def hold_exit(self) -> None:
        """Latch activity before shutdown cancels turns; a blocked update stays blocked."""
        self.exit_held = self.blocker(rollback=self.exit_action == "rollback") is not None

    async def close(self) -> None:
        if self.tasks:
            await asyncio.gather(*self.tasks)

    async def prepare_exit(self) -> None:
        """Called after API shutdown and every store is closed. No migration or learning can race the snapshot."""
        if self.exit_held or self.say is None:
            return
        state = self.state.read()
        if state["phase"] in ("installing", "rollback_offered") and self.exit_action != "rollback":
            return
        if self.exit_action != "rollback" and not state.get("staged"):
            return
        if self.exit_action != "rollback" and not state.get("accepted"):
            return
        try:
            if self.exit_action == "rollback":
                previous = dict(state["previous"])
                await asyncio.to_thread(self.state.rollback)
                plan = self.state.installer_plan(previous, "rollback")
            else:
                manifest = state["staged"].get("manifest")
                check_manifest(
                    manifest,
                    OFFLINE_ARTEFACT,
                    installed_version=self.app_version,
                    device_channel=self.device_channel(),
                    platform=self.platform,
                    environment=self.environment,
                    trust=self.revocations.trust(),
                )
                await asyncio.to_thread(self.state.prepare_install)
                plan = self.state.installer_plan(state["staged"], "update")
        except (UpdateRefused, OSError, ValueError) as error:
            state = self.state.read()
            state["failure_code"] = getattr(error, "code", "damaged")
            self.state._write(state)
            return
        self.say({"type": "control.update_install", **plan})

    def check_first_launch(self, health: HealthCheck) -> None:
        state = self.state.read()
        if state["phase"] != "installing":
            return
        if (state.get("staged") or {}).get("version") != self.app_version:
            state.update(phase="idle", failure_code="installation_failed")
            self.state._write(state)
            return
        outcome = self.state.first_launch(self.app_version, health)
        self.core.recovery_required = bool(outcome["offer_rollback"])


def add_routes(
    app: FastAPI,
    core: CoreService,
    mutating: Callable[[Request, Callable[[Any], Awaitable[tuple[int, Any]]]], Awaitable[Response]],
) -> None:
    def desk() -> UpdateDesk:
        if not isinstance(core.update_desk, UpdateDesk):
            raise ApiError("not_found", "updates are unavailable in this build")
        return core.update_desk

    @app.get("/v1/updates")
    async def status() -> dict[str, Any]:
        return desk().status()

    @app.post("/v1/updates/install")
    async def install(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            if contract_errors("api/commands#/$defs/InstallUpdate", body):
                raise ApiError("validation_failed", "invalid update source")
            return 202, desk().stage(Path(body["source"]["path"]))

        return await mutating(request, run)

    @app.post("/v1/updates/{action}")
    async def action(action: str, request: Request) -> Response:
        async def run(_: Any) -> tuple[int, Any]:
            desk().request_exit(action)
            return 204, None

        return await mutating(request, run)
