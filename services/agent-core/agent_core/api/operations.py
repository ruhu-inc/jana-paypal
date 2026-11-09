"""Offline Help and section discovery (OPS-23, OPS-25, UXS-46)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CURRENT_PROFILE, SERVER_VERSION, CoreService, require
from agent_core.api.errors import ApiError
from agent_core.api.profiles import Mutating
from agent_core.config.support import HelpCatalogue, settings_sections


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    catalogue = HelpCatalogue(SERVER_VERSION)

    @app.get("/v1/help")
    async def offline_help() -> dict[str, Any]:
        """Every article as catalogue keys; the client searches the words in the student's locale (OPS-23, LOC-01)."""
        desk = core.pack_desk
        policy = getattr(desk, "operations", None)
        matrix = policy.matrix() if policy else {}
        directory = core.directory
        return {
            "app_version": SERVER_VERSION,
            "channel": getattr(desk, "channel", None) or "stable",
            "device_id": directory.device_id if directory else None,
            "articles": catalogue.articles(),
            "known_issues": matrix.get("known_issues", []),
            "contacts": list(core.settings.ops_support_contacts),
            "health": core.health(),
            "safety": policy.summary()
            if policy
            else {"sequence": 0, "pending_sequence": 0, "ignored_entries": 0, "entries": []},
        }

    @app.get("/v1/settings/sections")
    async def sections() -> dict[str, Any]:
        profile = CURRENT_PROFILE.get()
        school = bool(profile and core.directory and not core.directory.self_consents(profile))
        return {
            "stage": core.settings.build_stage,
            "sections": settings_sections(core.settings.build_stage, school=school),
        }

    @app.post("/v1/operations/catalogue")
    async def import_catalogue(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            require("CatalogueImport", body)
            policy = getattr(core.pack_desk, "operations", None)
            if policy is None:
                raise ApiError("not_found", "OPS-10: operations policy is not attached")
            adopted = {
                "matrix_adopted": policy.adopt_matrix(body.get("compatibility_matrix", {})),
                "safety_adopted": policy.adopt_safety(body.get("safety_configuration", {})),
            }
            if adopted["safety_adopted"] and core.corrections_due is not None:
                core.corrections_due()  # CUR-36: a withdrawal may complete an installed correction
            return 200, adopted

        return await mutating(request, run)
