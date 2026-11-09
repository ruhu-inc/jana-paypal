"""Bounded UI crash cache in the profile folder, never browser storage (UIA-06).

This is not a checkpoint or a learner projection: it never feeds the engine or the authoritative snapshot.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CURRENT_PROFILE, CoreService, require
from agent_core.api.errors import ApiError
from agent_core.api.profiles import Mutating
from agent_core.store.checkpoints import CheckpointStore
from agent_core.store.profiles import ProfileDirectory, ProfileError

MAX_BYTES = 768 * 1024
EMPTY: dict[str, Any] = {"snapshot": None, "envelopes": []}


class JournalDesk:
    def __init__(self, core: CoreService):
        self.core = core
        core.on_profile_closing.append(self.forget)

    def root(self, profile: str | None) -> Path:
        if isinstance(self.core.directory, ProfileDirectory) and profile is not None:
            return Path(self.core.directory.learner_path(profile)).parent / "ui-journals"
        if isinstance(self.core.store, CheckpointStore):
            filename = self.core.store.conn.execute("PRAGMA database_list").fetchone()[2]
            if filename:
                return Path(filename).parent / "ui-journals"
        raise ApiError("feature_disabled", "profile storage required")

    def path(self, session_id: str) -> Path:
        self.core.entry(session_id)  # ownership and identifier validation before filesystem access
        return self.root(CURRENT_PROFILE.get()) / f"{session_id}.json"

    def forget(self, profile: str) -> None:
        """A profile-closing hook: it must not raise, or the lock would stop before the sessions are unloaded."""
        try:
            root = self.root(profile)
        except (ApiError, ProfileError):
            return  # no profile storage (or the profile is already gone), so no journals to remove
        shutil.rmtree(root, ignore_errors=True)

    def get(self, session_id: str) -> dict[str, Any]:
        path = self.path(session_id)
        try:
            if path.stat().st_size > MAX_BYTES:
                raise ValueError("oversize cache")
            body = json.loads(path.read_text())
            self.validate(session_id, body)
            return body  # type: ignore[no-any-return]
        except FileNotFoundError:
            return dict(EMPTY)
        except (ValueError, ApiError):
            path.unlink(missing_ok=True)
            return dict(EMPTY)

    def validate(self, session_id: str, body: Any) -> None:
        require("ClientJournal", body)
        snapshot = body["snapshot"]
        if snapshot is None:
            if body["envelopes"]:
                raise ApiError("validation_failed", "journal requires a base snapshot")
            return
        if snapshot["session"]["session_id"] != session_id or len(snapshot["messages"]) > 50:
            raise ApiError("validation_failed", "invalid journal tail")
        if any(e["session_id"] != session_id or e["event"]["type"] == "reasoning.delta" for e in body["envelopes"]):
            raise ApiError("validation_failed", "invalid journal envelope")

    def save(self, session_id: str, body: Any) -> dict[str, Any]:
        path = self.path(session_id)
        self.validate(session_id, body)
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        if len(raw) > MAX_BYTES:
            raise ApiError("validation_failed", "journal too large")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        try:
            with temp.open("wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
        return body  # type: ignore[no-any-return]


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    desk = JournalDesk(core)

    @app.get("/v1/sessions/{session_id}/journal")
    async def get(session_id: str) -> dict[str, Any]:
        return desk.get(session_id)

    @app.put("/v1/sessions/{session_id}/journal")
    async def save(session_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            desk.save(session_id, body)
            return 204, None

        return await mutating(request, run)

    @app.delete("/v1/sessions/{session_id}/journal")
    async def delete(session_id: str, request: Request) -> Response:
        async def run(_: Any) -> tuple[int, Any]:
            desk.path(session_id).unlink(missing_ok=True)
            return 204, None

        return await mutating(request, run)
