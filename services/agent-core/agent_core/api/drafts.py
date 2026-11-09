"""Profile-local, bounded rich editor drafts. Revisions prevent a late save overwriting newer work."""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CoreService, require
from agent_core.api.errors import ApiError
from agent_core.api.profiles import Mutating
from agent_core.engine.input import normalise_input
from agent_core.engine.safety import redact_personal
from agent_core.store.checkpoints import CheckpointStore
from agent_core.store.sqlite import transaction


def clean_document(document: Any) -> dict[str, Any]:
    count = 0

    def walk(node: Any, depth: int = 0) -> dict[str, Any]:
        nonlocal count
        count += 1
        if depth > 8 or count > 2000 or not isinstance(node, dict):
            raise ApiError("validation_failed", "invalid editor document")
        kind = node.get("type")
        if kind not in ("doc", "paragraph", "text", "hardBreak", "math", "ledger_table"):
            raise ApiError("validation_failed", "unsupported editor node")
        clean: dict[str, Any] = {"type": kind}
        if (
            kind in ("text", "math")
            and isinstance(node.get("marks"), list)
            and {"type": "dictationUncertain"} in node["marks"]
        ):
            clean["marks"] = [{"type": "dictationUncertain"}]
        if kind == "text":
            text = node.get("text")
            if not isinstance(text, str):
                raise ApiError("validation_failed", "invalid text")
            clean["text"] = redact_personal(normalise_input(text))[0]
        if kind == "math":
            attrs = node.get("attrs", {})
            if not isinstance(attrs, dict) or attrs.get("mode", "maths") not in ("maths", "units", "chem"):
                raise ApiError("validation_failed", "invalid formula")
            clean["attrs"] = {"mode": attrs.get("mode", "maths")}
            for key in ("source", "latex"):
                text = attrs.get(key, "")
                if not isinstance(text, str) or len(text) > 4000:
                    raise ApiError("validation_failed", "formula too long")
                clean["attrs"][key] = redact_personal(normalise_input(text))[0]
        if kind == "ledger_table":
            attrs = node.get("attrs", {})
            template = attrs.get("template") if isinstance(attrs, dict) else None
            cols = {"journal": 5, "t_account": 6, "trial_balance": 3}.get(str(template))
            rows = attrs.get("rows") if isinstance(attrs, dict) else None
            if cols is None or not isinstance(rows, list) or not 1 <= len(rows) <= 60:
                raise ApiError("validation_failed", "invalid ledger")
            if any(
                not isinstance(row, list) or len(row) != cols or any(not isinstance(c, str) or len(c) > 80 for c in row)
                for row in rows
            ):
                raise ApiError("validation_failed", "invalid ledger cells")
            clean["attrs"] = {
                "template": template,
                "rows": [[redact_personal(normalise_input(c))[0] for c in row] for row in rows],
            }
        content = node.get("content")
        if content is not None:
            if kind not in ("doc", "paragraph") or not isinstance(content, list):
                raise ApiError("validation_failed", "invalid editor children")
            clean["content"] = [walk(child, depth + 1) for child in content]
        return clean

    result = walk(document)
    if result["type"] != "doc" or len(json.dumps(result, ensure_ascii=False).encode()) > 32768:
        raise ApiError("validation_failed", "editor draft too large")
    return result


class DraftDesk:
    def __init__(self, core: CoreService):
        self.core = core

    def store(self, session_id: str, slot: str) -> CheckpointStore:
        self.core.entry(session_id)
        if not re.fullmatch(r"[a-zA-Z0-9_:-]{1,96}", slot):
            raise ApiError("validation_failed", "invalid draft slot")
        if not isinstance(self.core.store, CheckpointStore):
            raise ApiError("feature_disabled", "persistent drafts require profile storage")
        return self.core.store

    def get(self, session_id: str, slot: str) -> dict[str, Any]:
        conn = self.store(session_id, slot).conn
        row = conn.execute(
            "SELECT document, revision, updated_at FROM editor_drafts WHERE session_id=? AND slot=?",
            (session_id[4:], slot),
        ).fetchone()
        return {
            "slot": slot,
            "document": json.loads(row[0]) if row else {"type": "doc", "content": []},
            "revision": row[1] if row else 0,
            "updated_at": row[2] if row else 0,
        }

    def save(self, session_id: str, slot: str, body: Any) -> dict[str, Any]:
        require("SaveEditorDraft", body)
        conn = self.store(session_id, slot).conn
        clean = clean_document(body["document"])
        self.core.persist(self.core.entry(session_id))
        with transaction(conn):
            current = self.get(session_id, slot)
            if current["revision"] == body["expected_revision"] + 1 and current["document"] == clean:
                return current
            if current["revision"] != body["expected_revision"]:
                raise ApiError("request_conflict", "draft changed on another editor")
            revision = current["revision"] + 1
            now = self.core.clock_ms()
            conn.execute(
                "INSERT INTO editor_drafts VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id,slot) DO UPDATE SET "
                "document=excluded.document, revision=excluded.revision, updated_at=excluded.updated_at",
                (session_id[4:], slot, json.dumps(clean, ensure_ascii=False), revision, now),
            )
        return {"slot": slot, "document": clean, "revision": revision, "updated_at": now}

    def delete(self, session_id: str, slot: str) -> None:
        self.store(session_id, slot).conn.execute(
            "DELETE FROM editor_drafts WHERE session_id=? AND slot=?", (session_id[4:], slot)
        )


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    desk = DraftDesk(core)

    @app.get("/v1/sessions/{session_id}/drafts/{slot}")
    async def get(session_id: str, slot: str) -> dict[str, Any]:
        return desk.get(session_id, slot)

    @app.put("/v1/sessions/{session_id}/drafts/{slot}")
    async def save(session_id: str, slot: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            return 200, desk.save(session_id, slot, body)

        return await mutating(request, run)

    @app.delete("/v1/sessions/{session_id}/drafts/{slot}")
    async def delete(session_id: str, slot: str, request: Request) -> Response:
        async def run(_: Any) -> tuple[int, Any]:
            desk.delete(session_id, slot)
            return 204, None

        return await mutating(request, run)
