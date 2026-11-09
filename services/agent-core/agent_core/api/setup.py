"""Where first run stands (PLT-17, UXS-10, D86): `GET|PUT /v1/setup` (`SetupState`,
`SetupStep`).

First run is the one step of TABLE PLT-17a: the student's name or names (`POST /v1/profiles`, every other field a
default of TABLE PLT-17b), then "Start learning". There is no PIN or recovery code (D86). The step is `students` until
"Start learning" is recorded, then `done`; students added before quitting are kept, so a restart resumes at the same
step with its entries. The step only moves forward, and `done` needs a student. Nothing here touches the network
(PLT-17)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CoreService, require
from agent_core.api.errors import ApiError
from agent_core.api.profiles import Mutating
from agent_core.store.events import iso_ms
from agent_core.store.profiles import ProfileDirectory
from agent_core.store.sqlite import transaction

KEY = "first_run"


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    def directory() -> ProfileDirectory:
        if not isinstance(core.directory, ProfileDirectory):
            raise ApiError("not_found", "profiles are not available")
        return core.directory

    def recorded(d: ProfileDirectory) -> dict[str, Any]:
        row = d.device.execute("SELECT value FROM app_settings WHERE key = ?", (KEY,)).fetchone()
        return dict(json.loads(row[0])) if row else {}

    def state() -> dict[str, Any]:
        d = directory()
        done_at = recorded(d).get("completed_at")
        return {"step": "done" if done_at is not None else "students", "profiles": len(d.summaries()["profiles"]),
                "completed_at": iso_ms(int(done_at)) if done_at is not None else None}  # fmt: skip

    @app.get("/v1/setup")
    async def setup() -> dict[str, Any]:
        return state()

    @app.put("/v1/setup")
    async def advance(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            require("SetupStep", body)
            current = state()
            if current["profiles"] == 0:
                raise ApiError("validation_failed", "add a student first")
            if current["step"] != "done":
                d = directory()
                now = core.clock_ms()
                with transaction(d.device):
                    d.device.execute(
                        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO"
                        " UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                        (KEY, json.dumps({"step": "done", "completed_at": now}), now),
                    )
            return 200, state()

        return await mutating(request, run)
