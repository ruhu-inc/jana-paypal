"""The agent core's local API with the shipped engine, for M5 tests: a real uvicorn server on 127.0.0.1, the
sandboxed worker, and a scripted provider (the development script, or a test's own). Replies are synthetic; the S1
problem is invented for the tests."""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from agent_core.api import licence as licence_routes
from agent_core.api.app import create_app
from agent_core.api.core import CoreService
from agent_core.api.security import LaunchSecret
from agent_core.config import Settings
from agent_core.engine.assembly import AppRuntime, start_worker_pool
from agent_core.engine.devscript import dev_scripted_provider
from agent_core.engine.events import new_id
from agent_core.router import ScriptedProvider
from agent_core.store.consents import load_notices
from jana_tools import REPO_ROOT

STATEMENT = "A ball is thrown straight up at 14 m/s. Take g = 9.8 m/s^2. How high does it rise?"
APP_ORIGIN = "tauri://localhost"


def scripted(script: Any) -> ScriptedProvider:
    """A scripted local provider with the router's models installed."""
    return ScriptedProvider(script, kind="local", installed_models=["qwen3-vl-8b", "gemma-4-12b"])


def cli() -> str:
    return new_id("cli")


def ask(text: str, intent: str = "ask") -> dict[str, Any]:
    return {"client_message_id": cli(), "intent": intent, "content": [{"type": "text", "text": text}]}


@dataclass
class Running:
    core: CoreService
    runtime: AppRuntime
    base: str
    token: str
    port: int
    client: httpx.AsyncClient
    data_dir: Path
    server: uvicorn.Server | None = None
    task: asyncio.Task[Any] | None = None
    log: list[str] = field(default_factory=list)

    def headers(self, mutating: bool = False, **extra: str) -> dict[str, str]:
        out = {"authorization": f"Bearer {self.token}", "origin": APP_ORIGIN}
        if mutating:
            out["idempotency-key"] = cli()
        out.update(extra)
        return out

    async def post(self, path: str, body: Any = None, key: str | None = None) -> httpx.Response:
        headers = self.headers(True)
        if key:
            headers["idempotency-key"] = key
        return await self.client.post(self.base + path, json=body if body is not None else {}, headers=headers)

    async def get(self, path: str, **extra: str) -> httpx.Response:
        return await self.client.get(self.base + path, headers=self.headers(**extra))

    async def session(self) -> str:
        reply = await self.post("/v1/sessions", {"client_session_id": cli(), "mode": "guided", "subject": "physics",
                                                 "level": "O", "origin": {"kind": "ask", "ref": None}})  # fmt: skip
        assert reply.status_code == 200, reply.text
        return str(reply.json()["session_id"])

    async def wait_idle(self, session_id: str, timeout: float = 30.0) -> None:
        entry = self.core.sessions[session_id]
        if entry.task is not None:
            await asyncio.wait_for(asyncio.shield(entry.task), timeout)


def checkpoints(data_dir: Path) -> Any:
    """The checkpoint store of a data directory's one learner database (no profiles), opened after the core stopped."""
    from agent_core.store.checkpoints import CheckpointStore
    from agent_core.store.sqlite import connect

    (learner,) = sorted(data_dir.glob("profiles/*/learner.db"))
    return CheckpointStore(connect(learner))


async def start_core(data_dir: Path, provider: Any = None, settings: Settings | None = None,
                     worker: bool = True, serve: bool = True, licence: bool = True,
                     stage: str = "S1") -> Running:  # fmt: skip
    """`stage` is the release stage the sessions' engines run (`Settings.build_stage`): the scripted dialogues here
    are S1 unless a test names S2."""
    settings = (settings or Settings(worker_pool_size=1, worker_respawn_backoff_s=(0.0,))).model_copy(
        update={"build_stage": stage}
    )
    runtime = AppRuntime(settings, REPO_ROOT / "content" / "agent", REPO_ROOT / "skills", data_dir, release=False)
    if worker:
        runtime.supervisor = await start_worker_pool(settings, data_dir / "scratch")
    runtime.providers["local"] = provider or dev_scripted_provider()
    runtime.read_battery = lambda: None  # tests never follow the host's battery into low-power mode (PLT-22)
    records = runtime.open_stores("0.0.0-test")  # the learner database, device database and trace store (M6)
    core = CoreService(settings, runtime.engine, runtime.checkpoint_store(), records=records)
    core.directory = runtime.directory
    core.store_factory = runtime.checkpoint_store
    core.cache_clearers.append(runtime.clear_model_caches)
    core.storage_low = runtime.storage_low
    core.recoveries = lambda: [*runtime.recoveries, *runtime.integrity_failures]
    core.activator = runtime.activate_profile
    core.rebuild_projections = runtime.rebuild_projections
    core.notices = load_notices(runtime.content_root / "notices" / "notices.json")
    core.packs = runtime.packs
    core.health_details = runtime.health_components
    core.models = runtime.model_catalogue
    core.knows = runtime.knows
    core.learn = runtime.learn
    core.erasers.register("lesson_positions", runtime.drop_lesson_positions)
    core.erasers.register("online", runtime.forget_profile_online)
    core.tutor_unavailable = runtime.tutor_unavailable
    core.session_class = runtime.session_class
    core.trace_records = runtime.traces.records if runtime.traces is not None else None
    core.capture_trace_sink = runtime._trace_sink if runtime.traces is not None else None
    core.job_trace_sink = runtime._trace_sink if runtime.traces is not None else None  # as __main__ wires it
    core.capture_summary = runtime.traces.perception_summary if runtime.traces is not None else None
    core.erasers.register("traces", runtime.delete_profile_traces)
    core.closers.append(runtime.close)
    # The licence state, as the app evaluates it at start (PLT-20).
    if licence:
        licence_routes.attach(core, data_dir)
    core.readiness = "ready"
    token = secrets.token_urlsafe(32)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    app = create_app(core, LaunchSecret(token), lambda: port)
    running = Running(core, runtime, f"http://127.0.0.1:{port}", token, port,
                      httpx.AsyncClient(timeout=30.0), data_dir)  # fmt: skip
    core.start()
    if serve:
        running.server = uvicorn.Server(
            uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off", timeout_graceful_shutdown=1)
        )
        running.task = asyncio.get_running_loop().create_task(running.server.serve(sockets=[sock]))
        while not running.server.started:
            await asyncio.sleep(0.01)
    return running


async def stop_core(running: Running) -> None:
    await running.core.shutdown()
    await running.client.aclose()
    if running.server is not None and running.task is not None:
        running.server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await running.task


@dataclass
class SseFrame:
    id: str | None
    event: str | None
    data: str | None
    comment: bool = False

    def json(self) -> Any:
        return json.loads(self.data or "null")


async def sse(response: httpx.Response) -> AsyncIterator[SseFrame]:
    """Parse an SSE byte stream into frames (comments included), as the client's streaming fetch reader does."""
    buffer: dict[str, str] = {}
    async for line in response.aiter_lines():
        if line == "":
            if buffer:
                yield SseFrame(buffer.get("id"), buffer.get("event"), buffer.get("data"))
                buffer = {}
            continue
        if line.startswith(":"):
            yield SseFrame(None, None, None, comment=True)
            continue
        name, _, value = line.partition(":")
        buffer[name] = value[1:] if value.startswith(" ") else value
