"""AUT-07 conventions use the same schema form and atomic working-copy protocol as other records."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_core.contracts import errors

from pack_builder import source
from pack_builder.accounts import authorise
from pack_builder.form_schema import form_schema
from pack_builder.studio_io import atomic_write, revision

if TYPE_CHECKING:
    from pack_builder.studio import Workspace


def dispatch(workspace: Workspace, request: dict[str, Any]) -> dict[str, Any]:
    if request.get("table") != "conventions":
        raise ValueError("Only the conventions reference file is edited here; KC identities use migration preview.")
    directory = workspace.pack(request["pack"])
    path = source.ref_dir(directory) / "conventions.yaml"
    if path.is_symlink() or not path.resolve().is_relative_to(workspace.content):
        raise ValueError("Reference file leaves the workspace")
    if request["op"] == "reference_save":
        ctx = workspace.ctx(request.get("author"))
        account = ctx.need()
        authorise(account, "edit_draft", ctx.audit, subject="conventions")
        rows = request["rows"]
        if not isinstance(rows, list):
            raise ValueError("Conventions must be an array")
        text = source.canonical(rows, "conventions")
        if len(text.encode()) > 512 * 1024:
            raise ValueError("Reference file is too large")
        atomic_write(path, text, request["revision"])
        ctx.audit.append(account.account_id, "studio_save", "ok", "conventions", "reference working copy")
    rows = request.get("rows") if request["op"] == "reference_check" else source._load(path) if path.exists() else []
    problems = errors("content/content-pack#/properties/conventions", rows)
    return {
        "rows": rows,
        "revision": revision(path),
        "path": str(path.relative_to(workspace.root)),
        "report": {"ok": not problems, "problems": problems},
        "schema": form_schema("content/content-pack"),
    }
