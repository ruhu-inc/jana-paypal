"""Studio commands for analytics feedback to authors (AUT-37) and the content problem report inbox (AUT-38).

Reading needs the `see_analytics` permission. Taking in a payload is the `aut analytics intake` and `aut inbox intake`
commands' (a file the research team or the ingest hands over, never a path from the webview). The review queue is
refreshed against the open workspace's items, read only. The two commands that lead to a change are an author's own
actions under `edit_draft`: opening a draft change on a reported record, and adopting a calibrated difficulty as a
registered edit (ASM-21); neither is ever automatic.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from agent_core.config import Settings

from pack_builder import analytics, api, source
from pack_builder.accounts import authorise
from pack_builder.problem_inbox import Inbox

if TYPE_CHECKING:
    from pack_builder.studio import Workspace


def items(workspace: Workspace) -> dict[str, dict[str, Any]]:
    """Every item of the open workspace by id, with its pack."""
    found: dict[str, dict[str, Any]] = {}
    for directory in api.pack_dirs(workspace.content):
        manifest, tables = source.read_pack(directory)
        for item in tables["items"]:
            found[str(item.record["id"])] = {**item.record, "_pack": manifest["pack_id"]}
    return found


def dispatch(workspace: Workspace, request: dict[str, Any]) -> dict[str, Any]:
    op = request["op"]
    ctx = workspace.ctx(request.get("author"))
    account = ctx.need()
    settings = Settings()
    store = analytics.Analytics(ctx.root)
    if op in ("analytics_apply_calibration", "inbox_open"):
        authorise(account, "edit_draft", ctx.audit, subject=str(request.get("id", "")))
    else:
        authorise(account, "see_analytics", ctx.audit, subject=op)
    if op == "analytics_queue":
        entries = analytics.refresh_queue(store, items(workspace), settings)
        return {"entries": sorted(entries.values(), key=lambda e: (e["status"] != "open", e["record_id"]))}
    if op == "analytics_records":
        tallies = store.tallies()
        mine = {k: v for k, v in items(workspace).items() if v["_pack"] == request["pack"]}
        shown = []
        for record_id, record in sorted(mine.items()):
            if record_id in tallies:
                prior = float(((record.get("difficulty") or {}).get("prior")) or 0.0)
                calibration = analytics.calibrate(tallies[record_id], prior, settings)
                shown.append(analytics.statistics(record_id, tallies[record_id], settings, record)
                             | {"calibration": calibration})  # fmt: skip
        return {"records": shown}
    if op == "analytics_resolve":
        result = analytics.resolve(store, request["id"], request["outcome"], account.rev_id)
        ctx.audit.append(account.account_id, "review_queue", "ok", request["id"], request["outcome"])
        return result
    if op == "analytics_apply_calibration":
        located = workspace.locate(request["pack"], "items", request["id"])
        tally = store.tallies().get(request["id"])
        prior = float(((located.record.get("difficulty") or {}).get("prior")) or 0.0)
        calibration = analytics.calibrate(tally or analytics.Tally(), prior, settings)
        if not calibration["replaced"]:
            raise ValueError(f"The calibration needs {settings.calibration_min_responses} responses before it "
                             "replaces the author's difficulty (ASM-21).")  # fmt: skip
        record = copy.deepcopy(located.record)
        record["difficulty"] = {**record["difficulty"], "prior": calibration["difficulty"]}
        return workspace.save({"pack": request["pack"], "table": "items", "id": request["id"], "record": record,
                               "revision": request["revision"], "author": request.get("author")})  # fmt: skip
    inbox = Inbox(ctx.root)
    if op == "inbox_list":
        return {"rows": inbox.rows()}
    if op == "inbox_open":
        opened = workspace.open_record(request["pack"], request["table"], request["id"])
        inbox.opened(request["id"], opened["record"].get("version"), account.rev_id)
        return opened
    raise ValueError("Unknown analytics command.")
