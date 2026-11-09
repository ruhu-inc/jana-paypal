"""Dry-run-first, licensed and idempotent source import (AUT-39). No learner data formats."""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import re
from typing import TYPE_CHECKING, Any

import yaml
from agent_core.contracts import errors

from pack_builder import api, licence, source
from pack_builder.accounts import authorise
from pack_builder.studio_io import atomic_changes, revision
from pack_builder.studio_workflow import fingerprint, read

if TYPE_CHECKING:
    from pack_builder.studio import Workspace

TABLES = {"kcs", "items", "misconceptions"}
HINTS = ("orient", "concept", "first_step", "worked_step", "full_step")


def structured(text: str, fallback: Any) -> Any:
    return yaml.safe_load(text) if text.strip() else fallback


def provenance(row: dict[str, Any], default: dict[str, Any]) -> dict[str, Any]:
    original = copy.deepcopy(row.get("provenance") or {})
    sources = original.get("sources") or [{}]
    if not isinstance(sources, list) or any(not isinstance(s, dict) for s in sources):
        raise ValueError("source_*: expected a list of attributed sources")
    primary = {**sources[0], **{k: v for k, v in default.items() if v}}
    fields = {name: str(row.get("source_" + name) or primary.get(name) or "").strip() for name in licence.SOURCE_FIELDS}
    sources[0] = fields
    for fields in sources:
        if any(not str(fields.get(k) or "").strip() for k in licence.SOURCE_FIELDS):
            raise ValueError("source_*: supply title, url, licence, attribution, modifications and retrieved date")
        found = errors("content/content-pack#/$defs/Source", fields)
        if found:
            raise ValueError("source_*: " + "; ".join(found))
    result = {
        **original,
        "origin": "imported",
        "licence": original.get("licence") or sources[0]["licence"],
        "drafted_with_ai": original.get("drafted_with_ai", False),
        "sources": sources,
    }
    issues = errors("content/content-pack#/$defs/Provenance", result)
    issues += licence.gate_problems("items", {"provenance": result}, "import")
    if issues:
        raise ValueError("source_licence: " + "; ".join(issues))
    return result


def csv_record(
    workspace: Workspace, table: str, row: dict[str, str], identities: set[str], pack: str, used_codes: set[str]
) -> dict[str, Any]:
    manifest, found = workspace.records(pack)
    subject, level = manifest["subject"], manifest["level"]
    if table == "kcs":
        node = next((i.record for i in found["curriculum_nodes"] if i.record["id"] == row["topic_id"]), None)
        if node is None:
            raise ValueError("topic_id: choose a curriculum node in this pack")
        slug = row["slug"]
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,60}", slug):
            raise ValueError("slug: use a lower-case identifier")
        identity = "kc." + row["topic_id"].split(".", 1)[1] + "." + slug
        codes = used_codes
        n = 1
        while node["code"] + f".K{n:02}" in codes:
            n += 1
        return {
            "id": identity,
            "code": node["code"] + f".K{n:02}",
            "node_id": node["id"],
            "statement": row["statement"],
            "kind": row["kind"],
            "tools": structured(row.get("tools", ""), []),
            "exam_weight": float(row["exam_weight"]),
            "prerequisites": structured(row.get("prerequisites", ""), []),
            "misconception_ids": [],
            "version": "0.1.0",
            "status": "active",
        }
    kcs = structured(row["kc_ids"], [])
    if not isinstance(kcs, list) or not kcs:
        raise ValueError("kc_ids: supply a JSON/YAML list of KC ids")
    prefix = ("itm" if table == "items" else "mc") + f".{subject}.{level}."
    n = 1
    if table == "items":
        topic = str(kcs[0]).split(".")[3]
        prefix += topic + "."
        while prefix + f"{n:04}" in identities:
            n += 1
        identity = prefix + f"{n:04}"
        kind, raw = row["type"], row["answer"]
        if kind not in ("mcq", "numeric", "algebraic"):
            raise ValueError("type: CSV supports mcq, numeric or algebraic")
        answer = structured(raw, None)
        check_id = f"chk_build_import_{n:04}_a"
        if not isinstance(answer, dict):
            if kind == "mcq":
                raise ValueError("answer: MCQ needs a JSON object with kind, diagnostic and options")
            answer = (
                {
                    "kind": "numeric",
                    "value": str(raw),
                    "unit": row.get("unit") or None,
                    "unit_policy": "equivalent" if row.get("unit") else "none",
                    "tolerance": {"mode": "relative", "value": float(row.get("tolerance") or 0.005)},
                    "check_id": check_id,
                }
                if kind == "numeric"
                else {
                    "kind": "algebraic",
                    "expression": raw,
                    "variables": sorted(set(re.findall(r"\b[a-zA-Z]\b", raw))),
                    "required_form": "any",
                    "check_id": check_id,
                }
            )
        marks = int(row["marks"])
        solution = structured(row["solution"], None)
        if not isinstance(solution, dict):
            solution = {
                "steps": solution
                if isinstance(solution, list)
                else [{"n": 1, "latex": row["solution"], "reason": "Imported solution"}]
            }
        return {
            "id": identity,
            "version": "0.1.0",
            "type": kind,
            "subject": subject,
            "level": level,
            "kc_ids": kcs,
            "stem": row["stem"],
            "parts": [
                {
                    "id": "a",
                    "prompt": row.get("prompt") or row["stem"],
                    "marks": marks,
                    "answer": answer,
                    "hints": [
                        {"rung": i + 1, "name": name, "text": {"en": row[f"hint_{i + 1}"]}}
                        for i, name in enumerate(HINTS)
                    ],
                    "misconception_ids": [],
                }
            ],
            "solution": solution,
            "difficulty": {"prior": 0, "band": "core"},
            "estimated_seconds": 60,
            "exam_style": {
                "paper_style": "objective" if kind == "mcq" else "structured",
                "total_marks": marks,
                "calculator": True,
            },
            "context_tags": ["none"],
            "checks": structured(row.get("checks", ""), {}),
        }
    slug = hashlib.sha256(row["external_ref"].encode()).hexdigest()[:16]
    return {
        "id": prefix + "import_" + slug,
        "kc_ids": kcs,
        "description": row["teacher_description"],
        "student_name": row["student_name"],
        "signatures": [dict(kind=row["signature_kind"], **structured(row["signature"], {}))],
        "fix": {"strategy": "rule_restatement", "text": {"en": row["fix_text"]}},
        "probe_item_ids": [],
        "version": "0.1.0",
    }


def preview(workspace: Workspace, request: dict[str, Any]) -> dict[str, Any]:
    pack, text, table = request["pack"], str(request["text"]), request["table"]
    if table not in TABLES or len(text.encode()) > 512 * 1024:
        raise ValueError("Choose items, KCs or misconceptions and a source no larger than 512 KiB")
    _, metadata, metadata_revision = read(workspace, pack)
    digest = hashlib.sha256(
        json.dumps(
            [
                fingerprint(workspace, pack),
                metadata_revision,
                table,
                request["format"],
                text,
                request.get("source", {}),
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if request["format"] == "csv":
        reader = csv.DictReader(io.StringIO(text))
        headers = reader.fieldnames or []
        private = [
            h
            for h in headers
            if re.search(r"(^|_)(student|learner|pupil|name|email|phone|class|score|address|dob)($|_)", h, re.I)
            and not (table == "misconceptions" and h == "student_name")
        ]
        if private:
            raise ValueError("Personal-data column headers are refused: " + ", ".join(private))
        rows = list(reader)
        start = 2
    elif request["format"] == "yaml":
        value = yaml.safe_load(text)
        rows = value if isinstance(value, list) else [value]
        start = 1
    else:
        raise ValueError("Choose csv or yaml")
    if not rows or len(rows) > 5000:
        raise ValueError("Import needs 1-5000 rows")
    _, found = workspace.records(pack)
    known = {i.record["id"]: i for i in found[table]}
    identities = set(known)
    plans, problems, refs = [], [], set()
    used_codes = {i.record["code"] for i in found["kcs"]}
    for number, raw in enumerate(rows, start):
        try:
            row = copy.deepcopy(raw)
            if not isinstance(row, dict):
                raise ValueError("record: expected a mapping")
            external = str(row.get("external_ref") or row.get("authoring", {}).get("external_ref") or "").strip()
            if not external or len(external) > 120 or external in refs:
                raise ValueError("external_ref: supply a unique reference of at most 120 characters")
            refs.add(external)
            prov = provenance(row, request.get("source", {}))
            previous = metadata.get("imports", {}).get(external)
            record = (
                csv_record(workspace, table, row, identities, pack, used_codes)
                if request["format"] == "csv"
                else {k: v for k, v in row.items() if k != "external_ref" and not k.startswith("source_")}
            )
            if previous:
                if previous["table"] != table:
                    raise ValueError("external_ref: that reference belongs to another content type")
                record["id"] = previous["id"]
                if table == "kcs" and previous["id"] in known:
                    record["code"] = known[previous["id"]].record["code"]
            elif record["id"] in identities:
                raise ValueError("id: that identity already exists; choose a new one")
            if record["id"] in source._load(workspace.pack(pack) / "pack.yaml").get("retired_ids", []):
                raise ValueError("id: retired identities are permanently reserved")
            if table != "kcs":
                record["provenance"] = prov
                record["review"] = {"status": "drafted", "reviewers": []}
            record["authoring"] = {"external_ref": external, "import_provenance": prov, "history": ["drafted"]}
            shape = errors(
                "content/content-pack#/$defs/" + source.DEFINITION[table],
                {k: v for k, v in record.items() if k not in source.SOURCE_ONLY},
            )
            if shape:
                raise ValueError("record: " + "; ".join(shape))
            if table == "kcs":
                used_codes.add(record["code"])
            sha = hashlib.sha256(json.dumps([source.content_sha256(record), prov], sort_keys=True).encode()).hexdigest()
            plans.append(
                {
                    "row": number,
                    "external_ref": external,
                    "table": table,
                    "record": record,
                    "unchanged": bool(previous and previous.get("sha256") == sha),
                    "sha256": sha,
                }
            )
            identities.add(record["id"])
        except (ValueError, KeyError, TypeError, yaml.YAMLError) as error:
            column, _, message = str(error).partition(":")
            problems.append({"row": number, "column": column, "message": message.strip() or str(error)})
    return {
        "ok": not problems,
        "preview_sha256": digest,
        "problems": problems,
        "records": plans,
        "note": "Imported records are drafts. Tool verification and all publication gates still apply.",
    }


def apply(workspace: Workspace, request: dict[str, Any]) -> dict[str, Any]:
    ctx = workspace.ctx(request.get("author"))
    account = ctx.need()
    authorise(account, "bulk_import", ctx.audit, subject=request["pack"])
    report = preview(workspace, request)
    if report["preview_sha256"] != request.get("preview_sha256"):
        raise ValueError("Import inputs changed; run a fresh dry run")
    if not report["ok"]:
        return report
    pack = request["pack"]
    metadata_path, metadata, metadata_revision = read(workspace, pack)
    files: dict[Any, Any] = {}
    imported = []
    _, found = workspace.records(pack)
    known = {i.record["id"]: i.record for i in found[request["table"]]}
    for plan in report["records"]:
        if plan["unchanged"]:
            continue
        record = plan["record"]
        if record["id"] in known and "review" in record:
            changed = api.edited_record(known[record["id"]], account)
            record.update({k: changed[k] for k in ("version", "review") if k in changed})
        path = source.record_path(workspace.pack(pack), plan["table"], record)
        if path not in files:
            files[path] = {
                "rows": source._load(path) if path.exists() and plan["table"] not in source.PER_RECORD else [],
                "revision": revision(path),
                "table": plan["table"],
            }
        if plan["table"] in source.PER_RECORD:
            files[path]["rows"] = record
        else:
            files[path]["rows"] = [r for r in files[path]["rows"] if r["id"] != record["id"]] + [record]
        metadata.setdefault("imports", {})[plan["external_ref"]] = {
            "table": plan["table"],
            "id": record["id"],
            "sha256": plan["sha256"],
        }
        imported.append(record["id"])
    if not imported:
        return {"ok": True, "imported": [], "unchanged": len(report["records"])}
    entries = [
        {
            "path": str(p.relative_to(workspace.root)),
            "text": source.canonical(v["rows"], v["table"]),
            "expected": v["revision"],
        }
        for p, v in files.items()
    ]
    entries.append(
        {
            "path": str(metadata_path.relative_to(workspace.root)),
            "text": json.dumps(metadata, indent=2) + "\n",
            "expected": metadata_revision,
        }
    )
    atomic_changes(workspace.root, entries)
    ctx.audit.append(account.account_id, "studio_import", "ok", pack, f"{len(imported)} drafts")
    return {"ok": True, "imported": imported, "unchanged": len(report["records"]) - len(imported)}
