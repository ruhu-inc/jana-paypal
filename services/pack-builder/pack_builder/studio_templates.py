"""Authored template recipes; parameter review records are evidence, never fabricated publication approval."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import TYPE_CHECKING, Any

from pack_builder import source
from pack_builder.accounts import authorise, now_iso
from pack_builder.studio_bench import render, sample_template
from pack_builder.studio_workflow import read, store

if TYPE_CHECKING:
    from pack_builder.studio import Workspace


def digest(record: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps({k: v for k, v in record.items() if k not in source.SOURCE_ONLY}, sort_keys=True).encode()
    ).hexdigest()


def contexts(workspace: Workspace, pack: str) -> dict[str, list[str]]:
    path = source.ref_dir(workspace.pack(pack)).parent / "common/context.yaml"
    value = source._load(path) if path.exists() else {}
    return {
        name: value.get(key, []) for name, key in (("name", "names"), ("place", "places"), ("currency", "currencies"))
    }


def review(workspace: Workspace, request: dict[str, Any]) -> dict[str, Any]:
    ctx = workspace.ctx(request.get("author"))
    account = ctx.need()
    authorise(account, "edit_draft", ctx.audit, subject=request["id"])
    record = workspace.locate(request["pack"], "item_templates", request["id"]).record
    sampled = sample_template(record, contexts=contexts(workspace, request["pack"]))
    if not sampled["ok"]:
        return {
            **sampled,
            "problems": ["Resolve every sampled constraint and markability failure before recording reviewed ranges"],
        }
    path, metadata, expected = read(workspace, request["pack"])
    evidence = {
        "template_sha256": digest(record),
        "parameters": record["parameters"],
        "constraints": record.get("constraints", []),
        "sample_count": len(sampled["variants"]),
        "reviewed_by": account.account_id,
        "reviewed_on": now_iso(),
        "scope": "Sampled parameter ranges; generated drafts still require the publication gates",
    }
    metadata.setdefault("template_ranges", {})[record["id"]] = evidence
    store(path, metadata, expected)
    ctx.audit.append(account.account_id, "studio_template_ranges", "ok", request["pack"], record["id"])
    return {"ok": True, "reviewed_ranges": evidence}


def variant(workspace: Workspace, request: dict[str, Any]) -> dict[str, Any]:
    template = workspace.locate(request["pack"], "item_templates", request["template_id"]).record
    original = workspace.locate(request["pack"], "worked_examples", request["id"]).record
    if set(template["kc_ids"]) != set(original["kc_ids"]):
        raise ValueError("Choose a template for the same knowledge components")
    samples = sample_template(template, contexts=contexts(workspace, request["pack"]))["variants"]
    index = int(request.get("sample", 0))
    if not 0 <= index < len(samples) or not samples[index]["ok"]:
        raise ValueError("Choose a sampled variant with a verified markable key")
    sample = samples[index]
    recipe = request["recipe"]
    if not isinstance(recipe, dict) or set(recipe) - {"problem", "steps", "final_answer", "checks", "fading_order"}:
        raise ValueError("Supply an authored recipe with problem, steps, final_answer, checks and fading_order")
    # Render only the authored recipe, retaining identity/licensing from the source example. Every check is rerun.
    parameters = {**sample["parameters"], "key": sample["key_text"]}
    record = copy.deepcopy(original)
    record.update(render(recipe, parameters))
    record.update(
        id=request["new_id"], variant_of=original["id"], version="0.1.0", review={"status": "drafted", "reviewers": []}
    )
    record["problem"] = sample["stem"]
    final_check = record["final_answer"]["check_id"]
    record.setdefault("checks", {})[final_check] = render(template["answer_tool"], parameters)
    record["kc_ids"] = template["kc_ids"]
    record["authoring"] = {
        "history": ["drafted"],
        "template_id": template["id"],
        "parameters": sample["parameters"],
        "template_sha256": digest(template),
    }
    # An isomorphic recipe may add no uncredited external material.
    report = workspace.candidate(request["pack"], "worked_examples", record)
    if not report["ok"]:
        return {**report, "record": record}
    _, found = workspace.records(request["pack"])
    if any(i.record["id"] == record["id"] for i in found["worked_examples"]):
        raise ValueError("Choose a new worked-example identity")
    path = source.record_path(workspace.pack(request["pack"]), "worked_examples", record)
    opened = workspace.save(
        {
            "pack": request["pack"],
            "table": "worked_examples",
            "id": record["id"],
            "record": record,
            "revision": "missing",
            "author": request.get("author"),
        }
    )
    return {"ok": True, "opened": opened, "report": report, "path": str(path.relative_to(workspace.root))}
