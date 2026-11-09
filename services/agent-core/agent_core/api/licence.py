"""The school licence file over the local API (PLT-19, PLT-20, BIZ-06, BIZ-09, D84): `GET /v1/licence` and
`POST /v1/licence` (`LicenceStatus`, `LicenceInstall`).

The free offline product needs no activation (D30, D84): an installation without a licence file is the whole offline
product, and nothing here is on a family's path. Settings → Licence (open, TABLE UXS-46a) shows the licence a school
installed and takes its signed licence file. No code is typed, and no payment, price or purchase form exists here or
anywhere a student can reach (BIZ-06, BIZ-10). The state is fixed when the core starts (`attach`); applying a file
applies at once, and nothing a licence does ever blocks lessons, practice, progress or an export (BIZ-09)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CoreService, require
from agent_core.api.errors import ApiError
from agent_core.api.profiles import Mutating
from agent_core.config.environments import Environment
from agent_core.contracts.api.commands import LicenceInstall
from agent_core.store import subscription as subscriptions
from agent_core.store.licence import LicenceDesk, LicenceRefused
from agent_core.store.profiles import ProfileDirectory


def attach(core: CoreService, data_dir: Path, *, wall_ms: Callable[[], int] | None = None, trust: Any = None
           ) -> LicenceDesk | None:  # fmt: skip
    """Evaluate this installation's licence once, at start (PLT-20), and give it to the core."""
    if not isinstance(core.directory, ProfileDirectory):
        return None
    environment = Environment.PRODUCTION if core.release else Environment.DEV
    core.licence = LicenceDesk(data_dir, core.directory.device, core.settings, environment,
                               trust or core.entitlement_trust, wall_ms)  # fmt: skip
    return core.licence  # type: ignore[no-any-return]


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    def desk() -> LicenceDesk:
        if not isinstance(core.licence, LicenceDesk):
            raise ApiError("not_found", "licensing is not available")
        return core.licence

    def subscribed() -> bool:
        """The online subscription is active now (D70); it switches on online features only (BIZ-06a)."""
        stored = subscriptions.load(desk().device)
        return subscriptions.state(stored, core.clock_ms()) == "active"

    @app.get("/v1/licence")
    async def licence() -> dict[str, Any]:
        return desk().state.view(subscribed())

    @app.post("/v1/licence")
    async def install(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = LicenceInstall.model_validate(require("LicenceInstall", body))
            try:
                state = desk().apply(asked.licence_file.model_dump(mode="json"))
            except LicenceRefused as refused:
                raise ApiError("validation_failed", refused.message, details={"reason": refused.reason}) from None
            view = state.view(subscribed())
            if core.pack_desk is not None:
                core.pack_desk.set_channel(str(view["channel"]))
            return 200, view

        return await mutating(request, run)
