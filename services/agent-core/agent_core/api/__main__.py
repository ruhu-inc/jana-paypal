"""`python -m agent_core.api`: the agent core sidecar (ARC-02, ARC-04, ARC-22, ARC-23, API-02).

The shell speaks newline-delimited JSON on stdin; the core answers on stdout, exactly as the control contract
(`contracts/control`) defines. Secrets cross only this channel, never argv, environment, files or logs.

    shell -> core   control.hello {protocol: {major, minor}}   first; a differing major or a shell minor below the
                                                               core's ends the core with protocol_mismatch (ARC-29)
                    control.keys {store, entries}             the device secrets the shell keeps in the OS keychain
                                                               (SEC-17, D97); a release core starts only after it
                    control.key_result {ref, op, ok, store}   the shell's answer to each key_put and key_delete
                    control.config {data_dir, content_root, skills_root, release, dev_origins?, scripted?,
                                    pilot_country_file?}
                    control.secret {name: "launch_token", value}
                    control.runtime_endpoint {url, key, model}
                    control.net_state {online}
                    control.download_state {download_id, state, bytes_done, bytes_total, eta_s?, error_code?}
                                                               the shell's download manager (PLT-11)
                    control.shutdown {grace_ms?}
    core -> shell   control.ready {protocol, api_port, epoch}    once the API listens on 127.0.0.1
                    control.readiness {readiness, components?}  as the runtime attaches
                    control.bye {}                              after a clean shutdown
                    control.error {code, expected_protocol?}    on a bad control message or a failed start
                    control.download {download_id, item, files, target}, control.download_control
                                                                {download_id, action}: downloads (API-29, PLT-11)
                    control.key_put {ref, value}, control.key_delete {ref}
                                                                store or delete a device secret in the shell's
                                                                keychain (SEC-17, D97)

`dev_origins` (development builds only) adds origins such as the Vite dev server's; `scripted` attaches a scripted
provider instead of llama-server, for UI work without a model. Release builds refuse both.

Before `control.ready` the core writes the runtime's launch plan (`engine.runtime_link`), which the shell reads to
start llama-server. Without a model the plan says why, and the shell starts nothing (content-only). The core reports
runtime restarts and loss as `control.readiness` (MDL-19)."""

from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import socket
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import uvicorn

from agent_core.api import licence as licence_routes
from agent_core.api.app import create_app
from agent_core.api.core import SERVER_VERSION, CoreService
from agent_core.api.packs import ControlDownloads, PackDesk
from agent_core.api.security import APP_ORIGINS, LaunchSecret
from agent_core.api.updates import UpdateDesk
from agent_core.api.voice_adapter import attach as attach_voice
from agent_core.api.voice_adapter import follow_read_aloud
from agent_core.api.voice_adapter import reply as voice_reply
from agent_core.config import Settings
from agent_core.config.environments import Environment
from agent_core.config.operations import DevicePolicy
from agent_core.config.resolve import app_settings
from agent_core.config.revocation import RevocationStore
from agent_core.contracts import errors as contract_errors
from agent_core.engine.assembly import (
    WORKER_LANDLOCK_FLAG,
    WORKER_SANDBOX_FLAG,
    AppRuntime,
    agent_roots,
    start_worker_pool,
    study_document_main,
    worker_landlock_main,
    worker_sandbox_main,
)
from agent_core.engine.crash_capture import diagnostic_logging, start_crash_capture
from agent_core.engine.devscript import dev_scripted_provider
from agent_core.engine.runtime_link import RuntimeLink, write_launch_plan
from agent_core.store.consents import load_notices
from agent_core.store.egress import ledger_record
from agent_core.store.keystore import FileKeyStore, ShellKeyStore
from agent_core.store.updates import HealthCheck

CONTROL = "control/control"
# This core's control-channel version (ARC-29): the shell must have the same major and at least this minor.
CONTROL_PROTOCOL = {"major": 1, "minor": 4}


def protocol_compatible(shell: dict[str, int]) -> bool:
    return shell["major"] == CONTROL_PROTOCOL["major"] and shell["minor"] >= CONTROL_PROTOCOL["minor"]


def _say(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


async def _lines() -> asyncio.StreamReader:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    return reader


def bind_loopback() -> socket.socket:
    """127.0.0.1 only, on a port the OS chooses (API-02)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    return sock


async def main(
    *,
    runtime_factory: Callable[..., AppRuntime] = AppRuntime,
    settings_transform: Callable[[Settings], Settings] | None = None,
    before_ready: Callable[[AppRuntime, CoreService], Awaitable[None]] | None = None,
    development_app: Callable[[Any, AppRuntime, CoreService], Any] | None = None,
) -> int:
    diagnostic_logging()  # stderr carries diagnostics only: no request body, prompt or answer (OBS-19, PRV-09)
    reader = await _lines()
    secret = LaunchSecret()
    config: dict[str, Any] | None = None
    keys: dict[str, Any] | None = None  # the shell's control.keys (SEC-17, D97)
    core: CoreService | None = None
    runtime: AppRuntime | None = None
    voice_link: Any = None
    server: uvicorn.Server | None = None
    serving: asyncio.Task[Any] | None = None
    port: int | None = None
    greeted = False
    grace_s: float | None = None

    async def bring_up(cfg: dict[str, Any]) -> None:
        nonlocal core, runtime, server, serving, port, voice_link
        release = bool(cfg.get("release", True))
        if release and development_app is not None:
            raise ValueError("development routes in a release build")
        if release and (cfg.get("dev_origins") or cfg.get("scripted")):
            raise ValueError("dev options in a release build")
        # The build's stage from its configuration; a development build may run an earlier one (ARC-27).
        from agent_core.config.pilot import deployment_settings

        settings = deployment_settings(app_settings("release" if release else "development"), cfg)
        if settings_transform is not None:
            settings = settings_transform(settings)
        data_dir = Path(cfg["data_dir"])
        # SEC-14: a signed agent bundle is verified at start; an edited one falls back to the previous verified bundle.
        content_root, skills_root = agent_roots(Path(cfg["content_root"]), Path(cfg["skills_root"]), release)
        runtime = runtime_factory(settings, content_root, skills_root, data_dir, release=release)
        # SEC-17, D97: the device secrets come only from the shell's keychain bridge; a release core never runs
        # without them (`AppRuntime.key_store` refuses).
        runtime.keys = ShellKeyStore.from_control(keys, _say) if keys is not None else None
        # OBS-16: crash capture from the first moment; earlier crashes (shell panics, fatal errors) are taken in now.
        crashes = start_crash_capture(data_dir, runtime.key_store(), SERVER_VERSION, settings)
        runtime.supervisor = await start_worker_pool(settings, data_dir / "scratch", release=release)
        runtime.supervisor.on_recycle = lambda tool, code, call_id: crashes.record_worker_kill(
            tool, code, correlation={"call_id": call_id}
        )
        records = runtime.open_stores(SERVER_VERSION)
        core = CoreService(settings, runtime.engine, runtime.checkpoint_store(), records=records)
        core.directory = runtime.directory
        core.crashes = crashes  # crash reports wait for "Send this report?" (OBS-17)
        core.store_factory = runtime.checkpoint_store
        core.cache_clearers.append(runtime.clear_model_caches)
        core.storage_low = runtime.storage_low
        core.recoveries = lambda: [*runtime.recoveries, *runtime.integrity_failures]
        core.activator = runtime.activate_profile
        core.rebuild_projections = runtime.rebuild_projections
        core.retention = runtime.retention_pass
        core.notices = load_notices(runtime.content_root / "notices" / "notices.json")
        core.packs = runtime.packs
        core.health_details = runtime.health_components
        core.models = runtime.model_catalogue
        core.knows = runtime.knows
        core.learn = runtime.learn
        core.assessments = runtime.assessments
        core.planner = runtime.planner
        core.mock_active = lambda: bool((opened := runtime.assessments()) and opened.mock_active())
        core.erasers.register("lesson_positions", runtime.drop_lesson_positions)
        core.tutor_unavailable = runtime.tutor_unavailable
        core.session_class = runtime.session_class
        core.trace_records = runtime.traces.records if runtime.traces is not None else None
        core.capture_trace_sink = runtime._trace_sink if runtime.traces is not None else None
        core.job_trace_sink = runtime._trace_sink if runtime.traces is not None else None  # research chat (§19 fix)
        core.capture_summary = runtime.traces.perception_summary if runtime.traces is not None else None
        core.erasers.register("traces", runtime.delete_profile_traces)
        core.closers.append(runtime.close)
        # The connectivity layer (ARC-06): zero egress until a feature is enabled (PRV-02).
        online = runtime.attach_online()
        core.study.cloud = online.study_client(lambda: core.records_profile, core.study.connection)
        core.online, core.release = online, release
        core.knowledge = runtime.knowledge  # the one Home input routes against the installed packs (UXS-67)
        core.ask_cloud = online.ask_client(
            lambda: core.records_profile,
            lambda: getattr(core.store, "conn", None) if core.records_profile else None,
        )
        online.publish.append(core.publish_app)  # the app channel (API-22)
        licence_routes.attach(core, data_dir)  # the school licence file, if any, once per start (PLT-20)
        environment = (
            Environment.PILOT
            if settings.environment == "pilot"
            else Environment.PRODUCTION
            if release
            else Environment.DEV
        )

        def cache_update(folder: Path) -> None:
            # The kit's signed safety configuration is adopted by the pack desk's DevicePolicy (OPS-10, PRV-21).
            core.update_desk.cache_from_kit(folder)

        runtime.release_channel = lambda: str(core.licence.state.view(False)["channel"]) if core.licence else "stable"
        # Safety restrictions accepted before this start apply to online features at once; newer ones at the next
        # turn boundary, through the same policy (OPS-10, OPS-11, PRV-21).
        runtime.operation_policy().apply_online(online)
        core.pack_desk = PackDesk(data_dir, environment, SERVER_VERSION, core.publish_app,
                                  on_kit=cache_update,
                                  shell=ControlDownloads(_say),
                                  downloads_on=lambda: "downloads" in online.features_on(), online=online.usable,
                                  free_bytes=lambda: shutil.disk_usage(data_dir).free,
                                  disk_margin=settings.download_disk_margin_share,
                                  channel="pilot" if settings.environment == "pilot" else runtime.release_channel(),
                                  revocations=RevocationStore(data_dir, environment))  # fmt: skip
        core.pack_desk.operations = DevicePolicy(
            data_dir, environment, runtime.release_channel(), SERVER_VERSION, core.pack_desk._trust
        )
        core.closers.insert(0, core.pack_desk.close_downloads)
        core.update_desk = UpdateDesk(core, data_dir, environment, SERVER_VERSION, say=_say)
        core.pack_desk.on_kit = cache_update
        # CUR-36, D98: a correcting pack re-marks the withdrawn item's attempts with no other step, in the background
        # and never during a turn or a mock sitting; at start too, for whatever arrived while the app was closed.
        # Content or a safety configuration arriving from a pack, a kit, the update desk or a catalogue import applies a
        # waiting withdrawal as soon as no turn runs, so its correction re-marks promptly.
        runtime.background_held = core._background_held
        core.corrections_due = lambda: runtime.schedule_corrections(apply_pending=True)
        core.pack_desk.on_installed = lambda _installed: runtime.schedule_corrections(apply_pending=True)
        runtime.schedule_corrections()
        core.closers.insert(0, core.update_desk.close)
        core.start_benchmark = lambda: runtime.start_benchmark(runtime.providers.get("local"))  # PLT-08 on request
        core.erasers.register("online", runtime.forget_profile_online)
        core.closers.insert(0, online.stop)
        core.study.perception = runtime.study_perception
        core.study.checker = runtime.study_check
        core.study.content = runtime.content
        core.reflector = runtime.reflector()  # the background Reflector, started with the core (AGT-26)
        core.research_router = runtime.research_router  # the research conversation (SPEC §18.1a), online only
        core.components["worker"] = "ok"
        runtime.probe = await asyncio.to_thread(runtime.read_probe)  # MDL-16: once per start, offline
        runtime.epoch = core.epoch
        plan = await asyncio.to_thread(write_launch_plan, data_dir, settings, release, core.epoch,
                                       bool(cfg.get("scripted")), runtime.probe)  # fmt: skip
        runtime.plan = plan
        if cfg.get("scripted"):
            runtime.providers["local"] = dev_scripted_provider()
            core.readiness = "ready"
        else:
            runtime.link = RuntimeLink(settings, plan, alive=runtime.runtime_alive)
            core.readiness, core.components["runtime"] = runtime.link.readiness()
        sock = bind_loopback()
        port = sock.getsockname()[1]
        origins = APP_ORIGINS | frozenset(cfg.get("dev_origins") or ())
        adapter_token = secrets.token_urlsafe(32)
        # The live revocation list verifies speech packs at install and at every launch (SEC-16, D88).
        voice_link = await asyncio.to_thread(runtime.prepare_voice, port, adapter_token, _say, core.pack_desk._trust)
        # Attached even with no pack yet: one installed later is offered without a restart (VOC-03).
        link = voice_link

        # Cloud voice (VOC-28), when built in: the same bridge and socket, the endpoint chosen at each voice start.
        async def respond(body: Any) -> Any:  # the forwarded chat-completions request, answered in process
            assert voice.adapter is not None
            return await voice_reply(voice.adapter, body)

        cloud = runtime.prepare_cloud_voice(core._announce, respond)
        if cloud is not None:  # the session's current subject orders speech to text's vocabulary bias

            def subject_of(session_id: str) -> str | None:
                entry = core.sessions.get(session_id)
                return entry.engine_state.subject if entry is not None else None

            cloud.subject_of = subject_of

        def online_voice() -> bool:
            return cloud is not None and cloud.ready()

        voice = attach_voice(core, link.connect, secret=adapter_token, connect_timeout_s=95,
                             available=link.selected is not None or online_voice(),
                             tutoring=lambda: link.tutoring or online_voice(),
                             dictation=lambda: link.dictation or online_voice())  # fmt: skip
        assert voice.adapter is not None
        link.on_token = voice.adapter.secret.set  # one adapter token per sidecar launch (VOC-23)

        def voice_offered(*_: Any) -> None:
            setattr(core, "voice_available", link.selected is not None or online_voice())  # noqa: B010

        link.on_change = voice_offered
        if cloud is not None:
            voice.route = runtime.voice_route(cloud, link.connect, lambda: link.selected is not None)
            online.publish.append(voice_offered)  # the switch or the network changed (ARC-11)
            # Cloud voice is ready only for an open profile with its consent and first-use answer: opening or closing
            # one re-checks, so after a restart voice is offered at once, not at the next network change.
            core.on_preferences.append(voice_offered)
            core.on_profile_forgotten.append(voice_offered)
        core.pack_desk.speech, core.pack_desk.on_speech = link.store, link.rescan
        core.pack_desk.before_speech_remove = link.release
        core.closers.insert(1, link.close)
        follow_read_aloud(core, link.want_warm)  # D96: the sidecar starts early only with read-aloud on (VOC-23)
        if before_ready is not None:
            await before_ready(runtime, core)
        app = create_app(core, secret, lambda: port, origins, voice=voice)
        if development_app is not None:
            app = development_app(app, runtime, core)
        # log_config=None: uvicorn adds no handler of its own, so its records go through the diagnostic formatter.
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off",
                                               timeout_graceful_shutdown=1, log_config=None))  # fmt: skip
        serving = asyncio.get_running_loop().create_task(server.serve(sockets=[sock]))
        core.start()
        online.start()
        _say({"type": "control.ready", "protocol": CONTROL_PROTOCOL, "api_port": port, "epoch": core.epoch})
        if cfg.get("scripted") or not plan.get("model"):
            core.update_desk.check_first_launch(HealthCheck(True, runtime.supervisor.compute_available(), None))
        core.pack_desk.recover_downloads()
        if runtime.link is not None:
            runtime.link.on_change = report
            runtime.link.start()

    def report(readiness: str, component: str) -> None:
        assert core is not None
        core.readiness, core.components["runtime"] = readiness, component
        _say({"type": "control.readiness", "readiness": readiness, "components": dict(core.components)})
        if core.update_desk is not None and runtime is not None and readiness in ("ready", "degraded"):
            worker_ok = runtime.supervisor is not None and runtime.supervisor.compute_available()
            core.update_desk.check_first_launch(HealthCheck(True, worker_ok,
                                                         readiness == "ready"))  # fmt: skip

    async def attach_runtime(message: dict[str, Any]) -> None:
        assert core is not None and runtime is not None
        if runtime.link is not None:  # the link reports loading, ready or failed as they happen
            await runtime.connect_runtime(message["url"], message["key"], message["model"])
            return
        core.readiness = "model_loading"
        _say({"type": "control.readiness", "readiness": core.readiness})
        ok = await runtime.connect_runtime(message["url"], message["key"], message["model"])
        core.readiness = "ready" if ok else "degraded"
        core.components["runtime"] = "ok" if ok else "down"
        _say({"type": "control.readiness", "readiness": core.readiness, "components": dict(core.components)})

    while True:
        raw = await reader.readline()
        if not raw:  # the shell went away: shut down as if asked (ARC-23)
            break
        try:
            message = json.loads(raw)
            kind = message["type"]
        except (ValueError, KeyError, TypeError):
            _say({"type": "control.error", "code": "invalid_control_message"})
            continue
        from agent_core.config.pilot import control_errors

        invalid = (
            control_errors(message)
            if kind == "control.config"
            else contract_errors(f"{CONTROL}#/$defs/ToCore", message)
        )
        if invalid:
            _say({"type": "control.error", "code": "invalid_control_message"})
            continue
        if kind == "control.hello":
            if not protocol_compatible(message["protocol"]):
                _say({"type": "control.error", "code": "protocol_mismatch", "expected_protocol": CONTROL_PROTOCOL})
                return 3
            greeted = True
            continue
        if not greeted:
            _say({"type": "control.error", "code": "unexpected_control_message"})
            continue
        if kind == "control.config":
            config = message
        elif kind == "control.keys":
            if keys is not None or core is not None:  # once per launch, before the core starts
                _say({"type": "control.error", "code": "unexpected_control_message"})
                continue
            keys = message
        elif kind == "control.key_result":
            if runtime is not None and isinstance(runtime.keys, ShellKeyStore):
                runtime.keys.on_result(message)
            continue
        elif kind == "control.secret" and message.get("name") == "launch_token":
            secret.set(str(message["value"]))
        elif kind == "control.runtime_endpoint" and core is not None:
            asyncio.get_running_loop().create_task(attach_runtime(message))
        elif kind in ("control.voice_endpoint", "control.voice_failed") and voice_link is not None:
            voice_link.on_shell(message)
        elif kind == "control.net_state":
            if runtime is not None and runtime.online is not None:  # an OS network hint from the shell (ARC-08)
                runtime.online.connectivity.os_hint(online=bool(message["online"]))
            continue
        elif kind == "control.download_state":  # the shell's download manager (PLT-11)
            if core is not None and core.pack_desk is not None:
                core.pack_desk.on_shell_state(message)
            continue
        elif kind == "control.egress_notice" and core is not None:
            notice = message["notice"]
            if notice["feature"] not in ("downloads", "update_checks") or notice["data_classes"]:
                _say({"type": "control.error", "code": "invalid_shell_egress"})
            else:
                core.publish_app(notice)
            continue
        elif kind == "control.egress_ledger" and runtime is not None and runtime.stores is not None:
            ledger_record(
                runtime.stores.device,
                message["at_ms"],
                message["feature"],
                message["destination"],
                message["bytes"],
                message["payload_class"],
            )
            continue
        elif kind == "control.shutdown":
            grace_s = message.get("grace_ms", 1000) / 1000
            break
        else:
            _say({"type": "control.error", "code": "unexpected_control_message"})
        if core is None and config is not None and secret.ready and (keys is not None or not config["release"]):
            try:
                await bring_up(config)
            except (ValueError, KeyError, OSError, RuntimeError) as error:
                _say({"type": "control.error", "code": type(error).__name__})
                return 2

    if core is not None:
        if core.update_desk is not None:
            core.update_desk.hold_exit()
        await core.shutdown(grace_s)
    if server is not None and serving is not None:
        server.should_exit = True
        await serving
    if core is not None and core.update_desk is not None:
        await core.update_desk.prepare_exit()
    _say({"type": "control.bye"})
    return 0


def uninstall_main(argv: list[str]) -> int:
    """`--uninstall keep|delete --data-dir DIR [--keystore-dir DIR] [--install-dir DIR]` (PLT-24), run by the
    Windows uninstallers with the adult's choice; prints what was removed, never content."""
    from agent_core.store.uninstall import preflight, uninstall

    def option(name: str) -> str | None:
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None

    choice, data = option("--uninstall"), option("--data-dir")
    if choice not in ("keep", "delete") or data is None:
        sys.stderr.write("usage: --uninstall keep|delete --data-dir DIR [--keystore-dir DIR] [--install-dir DIR]\n")
        return 64
    data_dir = Path(data)
    if "--preflight" in argv:
        _say(preflight(data_dir))
        return 0
    keys = FileKeyStore(Path(option("--keystore-dir") or ""), data_dir) if option("--keystore-dir") else None
    installed = [Path(p) for p in [option("--install-dir")] if p]
    _say({"removed": len(uninstall(choice, data_dir, keys, installed))})
    return 0


def update_recovery_main(argv: list[str]) -> int:
    """Standalone recovery, with no stores opened or migrations run. The shell has already stopped the core."""
    from agent_core.api.updates import this_platform
    from agent_core.store.updates import OFFLINE_ARTEFACT, UpdateRefused, UpdateState, check_manifest

    try:
        action = argv[argv.index("--update-recovery") + 1]
        data_dir = Path(argv[argv.index("--data-dir") + 1])
        state = UpdateState(data_dir)
        held = state.read()
        if held["phase"] == "installing":
            state.first_launch(str(held["staged"]["version"]), HealthCheck(False, False, None))
            held = state.read()
        if held["phase"] != "rollback_offered":
            raise UpdateRefused("no_rollback", "no update recovery is pending")
        if action == "inspect":
            state.installer_plan(held["previous"], "rollback")
            _say({"previous_version": held["previous"]["version"]})
        elif action == "rollback":
            previous = dict(held["previous"])
            environment = Environment.PRODUCTION if "--release" in argv else Environment.DEV
            manifest = previous.get("manifest")
            if environment is Environment.PRODUCTION and not isinstance(manifest, dict):
                raise UpdateRefused("signature", "the previous installer has no signed manifest")
            if isinstance(manifest, dict):
                check_manifest(
                    manifest,
                    OFFLINE_ARTEFACT,
                    installed_version=previous["version"],
                    device_channel="stable",
                    platform=this_platform(),
                    environment=environment,
                    trust=RevocationStore(data_dir, environment).trust(),
                    allow_installed=True,
                )
            state.rollback()
            _say({"type": "control.update_install", **state.installer_plan(previous, "rollback")})
        else:
            return 64
    except (UpdateRefused, OSError, ValueError, KeyError, IndexError):
        return 2
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--study-document-worker"]:
        study_document_main()
        raise SystemExit(0)
    if sys.argv[1:2] == [WORKER_SANDBOX_FLAG]:  # the frozen core doubles as the Windows worker launcher (SEC-06)
        raise SystemExit(worker_sandbox_main(sys.argv[2:]))
    if sys.argv[1:2] == [WORKER_LANDLOCK_FLAG]:  # and as the Linux Landlock launcher (TABLE SEC-06a)
        raise SystemExit(worker_landlock_main(sys.argv[2:]))
    if "--update-recovery" in sys.argv:
        raise SystemExit(update_recovery_main(sys.argv[1:]))
    if "--uninstall" in sys.argv:
        raise SystemExit(uninstall_main(sys.argv[1:]))
    raise SystemExit(asyncio.run(main()))
