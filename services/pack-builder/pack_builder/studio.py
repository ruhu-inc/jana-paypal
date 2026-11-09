"""Offline workspace commands used by `aut studio` and the internal desktop app (AUT-04/05/07).

JSON-lines on stdin/stdout; no socket, student profile, model, signing key or hidden content database.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import sqlite3
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from agent_core.config import Settings
from agent_core.contracts import errors
from agent_core.engine.learn import Fading, LearnDesk
from agent_core.engine.marking import Converted, mark
from agent_core.engine.signatures import SignatureContext, pack_context
from agent_core.store.pack_index import PackIndex, PackRef
from tool_worker.protocol import WorkerError

from pack_builder import api, curriculum, qa, source, structure, versioning
from pack_builder.accounts import AUTHOR_ENV, Accounts, authorise
from pack_builder.build import BuildError, charts_of, load_source
from pack_builder.form_schema import form_schema
from pack_builder.roles import Forbidden
from pack_builder.studio_io import Conflict, atomic_write, locked, recover_changes, revision
from pack_builder.verify import Verifier, run_check

TABLES = (
    "curriculum_nodes",
    "kcs",
    "lessons",
    "items",
    "worked_examples",
    "misconceptions",
    "item_templates",
    "visual_presets",
    "mock_papers",
    "charts_of_accounts",
)
LIMIT = 512 * 1024
ID = re.compile(r"[a-z][a-z0-9_.-]{1,159}\Z")


class Workspace:
    def __init__(self, root: Path, context: api.Context | None = None):
        self.root = root.resolve(strict=True)
        self.content = self.root / "content" / "curriculum"
        if not self.content.is_dir():
            raise ValueError("Choose a content repository containing content/curriculum.")
        if any(p.is_symlink() for p in (self.root / "content", self.content)):
            raise ValueError("Content directories must not be symbolic links.")
        self.context = context
        recover_changes(self.root)
        self.preflight()

    def preflight(self) -> None:
        # Check only authoring sources, never a learner directory or held-out store.
        for index, path in enumerate(self.content.rglob("*")):
            if index > 40_000:
                raise ValueError("The content workspace has too many files.")
            if path.is_symlink():
                raise ValueError("Content sources must not contain symbolic links.")
            if path.is_file() and path.stat().st_size > LIMIT:
                raise ValueError(f"Source file is larger than 512 KiB: {path.name}")

    def ctx(self, author: str | None = None) -> api.Context:
        if self.context is not None:
            return self.context
        account_id = author or os.environ.get(AUTHOR_ENV)
        return api.Context(Accounts().get(account_id) if account_id else None)

    def pack(self, name: str) -> Path:
        # Resolve from the inventory, never a webview-supplied filesystem path.
        for path in api.pack_dirs(self.content):
            if str(source._load(path / "pack.yaml").get("pack_id")) == name:
                return path
        raise ValueError("That pack is not in the open workspace.")

    def records(self, pack: str) -> tuple[dict[str, Any], dict[str, list[source.Located]]]:
        self.preflight()
        return source.read_pack(self.pack(pack))

    def locate(self, pack: str, table: str, identity: str) -> source.Located:
        if table not in (*TABLES, "diagrams"):
            raise ValueError("Choose an editable content type.")
        _, found = self.records(pack)
        match = next((item for item in found[table] if item.record.get("id") == identity), None)
        if match is None:
            raise KeyError("That record is not in this pack.")
        return match

    def open_record(self, pack: str, table: str, identity: str) -> dict[str, Any]:
        item = self.locate(pack, table, identity)
        return {
            "record": item.record,
            "revision": revision(item.path),
            "path": str(item.path.relative_to(self.root)),
            "table": table,
            "pack": pack,
        }

    def inventory(self) -> dict[str, Any]:
        from pack_builder.studio_skills import inventory as skill_inventory

        packs = []
        for directory in api.pack_dirs(self.content):
            manifest, found = source.read_pack(directory)
            packs.append(
                {
                    "id": manifest["pack_id"],
                    "subject": manifest["subject"],
                    "level": manifest["level"],
                    "records": [
                        {
                            "id": item.record["id"],
                            "table": table,
                            "title": item.record.get("title")
                            or item.record.get("statement")
                            or item.record.get("stem")
                            or item.record["id"],
                            "node_id": item.record.get("node_id") or item.record.get("parent_id"),
                            "code": item.record.get("code"),
                        }
                        for table in TABLES
                        for item in found[table]
                    ],
                }
            )
        ctx = self.ctx()
        accounts = Accounts(ctx.root).all()
        return {
            "root": str(self.root),
            "packs": packs,
            "tables": list(TABLES),
            "accounts": [
                {"id": a.account_id, "name": a.display_name, "roles": list(a.roles)} for a in accounts.values()
            ],
            "author": ctx.account.account_id if ctx.account else None,
            "schema": form_schema("content/content-pack"),
            "definitions": source.DEFINITION,
            "skills": skill_inventory(self),
            "diagrams": [
                {"pack": p["id"], "id": i.record["id"], "title": i.record.get("title", i.record["id"])}
                for p in packs
                for i in self.records(p["id"])[1]["diagrams"]
            ],
        }

    def candidate(self, pack: str, table: str, record: dict[str, Any]) -> dict[str, Any]:
        if table not in TABLES:
            raise ValueError("Unsupported content type.")
        clean = {k: v for k, v in record.items() if k not in source.SOURCE_ONLY}
        shape = errors(f"content/content-pack#/$defs/{source.DEFINITION[table]}", clean)
        verifier = Verifier({}, [])
        semantic: list[str] = []
        if not shape:
            loaded = load_source(self.pack(pack))
            loaded.tables[table] = [r for r in loaded.tables[table] if r.get("id") != record.get("id")] + [clean]
            verifier = Verifier({**loaded.checks, **record.get("checks", {})}, loaded.tables["conventions"])
            if table == "items":
                verifier.item(clean)
                semantic += structure.hint_problems(clean)
            elif table == "worked_examples":
                verifier.worked_example(clean)
                semantic += structure.worked_example_problems(clean)
            elif table == "lessons":
                semantic += structure.lesson_problems(clean, Settings())
            elif table in ("kcs", "curriculum_nodes"):
                semantic += curriculum.map_problems(loaded.tables["curriculum_nodes"], loaded.tables["kcs"], 0.02)
                semantic += curriculum.prerequisite_problems(loaded.tables["kcs"], loaded.pack)
                semantic += curriculum.mapping_problems(loaded.tables["curriculum_nodes"], loaded.tables["kcs"])
                semantic += curriculum.kc_problems(loaded.tables["kcs"], [], [], max_chars=200, published=False)
                semantic += versioning.migration_problems(loaded.tables, loaded.pack["version"])
            library_ids = [r["id"] for records in loaded.library.values() for r in records]
            semantic += structure.reference_problems(
                {table: [clean]}, structure.Known(loaded.tables, library_ids), loaded.pack
            )
            if table in ("items", "worked_examples", "lessons"):
                semantic += qa.rendering_problems({table: [clean]}, Settings())
            semantic += curriculum.id_problems(loaded.pack, loaded.tables, loaded.pack.get("retired_ids", []))
        preview = self.preview(pack, table, record) if not shape and not semantic else None
        return {
            "preview": preview,
            "ok": not shape and not semantic and not verifier.problems,
            "problems": [*shape, *semantic, *verifier.problems],
            "schema_problems": shape,
            "checks": [{**c.record(), "value": c.value} for c in verifier.results.values()],
        }

    def preview(self, pack: str, table: str, record: dict[str, Any]) -> dict[str, Any] | None:
        if table not in ("items", "lessons", "worked_examples"):
            return None
        loaded = load_source(self.pack(pack))
        loaded.tables[table] = [r for r in loaded.tables[table] if r.get("id") != record["id"]] + [record]
        index = PackIndex(
            records={
                t: {r["id"]: r for r in rs}
                for t, rs in loaded.tables.items()
                if t in ("items", "lessons", "worked_examples", "misconceptions")
            }
        )
        index.subject_packs[f"cur.{loaded.pack['subject']}.{loaded.pack['level']}"] = PackRef(
            pack, loaded.pack["version"]
        )
        # A new, empty in-memory projection supplies zero fading; no student database is opened.
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE TABLE kc_mastery (kc_id TEXT, fading_stage INTEGER)")
            desk = LearnDesk(index, conn, Settings())
            if table == "worked_examples":
                return {"example": desk._worked_example(record["id"], Fading({}, {}))}
            if table == "items":
                lesson = {
                    "id": "les." + record["id"][4:],
                    "node_id": "cur." + record["id"][4:],
                    "title": "Item preview",
                    "kc_ids": record["kc_ids"],
                    "estimated_minutes": 1,
                    "version": record["version"],
                    "blocks": [{"type": "quick_check", "item_ids": [record["id"]]}],
                }
                index.records.setdefault("lessons", {})[lesson["id"]] = lesson
                return {"lesson": desk.lesson(lesson["id"])}
            return {"lesson": desk.lesson(record["id"])}

    def duplicate(self, request: dict[str, Any]) -> dict[str, Any]:
        pack, table = request["pack"], request["table"]
        item = self.locate(pack, table, request["id"])
        _, found = self.records(pack)
        record = copy.deepcopy(item.record)
        identities = {i.record["id"] for i in found[table]}
        prefix, tail = record["id"].rsplit(".", 1)
        if tail.isdigit():
            number = 1
            while f"{prefix}.{number:04d}" in identities:
                number += 1
            identity = f"{prefix}.{number:04d}"
        else:
            slug = re.sub(r"[^a-z0-9]+", "_", request["title"].lower()).strip("_")[:60] or "new"
            identity = f"{prefix}.{slug}"
            number = 2
            while identity in identities:
                identity = f"{prefix}.{slug}_{number}"
                number += 1
        record["id"] = identity
        if "code" in record:
            match = re.fullmatch(r"(.*?)([0-9]+)", record["code"])
            if match:
                codes = {i.record.get("code") for i in found[table]}
                number = 1
                while f"{match[1]}{number:0{len(match[2])}d}" in codes:
                    number += 1
                record["code"] = f"{match[1]}{number:0{len(match[2])}d}"
        if "title" in record:
            record["title"] = request["title"]
        if "statement" in record:
            record["statement"] = request["title"]
        record["version"] = "0.1.0" if "version" in record else record.get("version")
        if record.get("version") is None:
            record.pop("version", None)
        if "review" in record:
            record["review"] = {"status": "drafted", "reviewers": []}
            record["authoring"] = {"history": ["drafted"]}
        path = source.record_path(self.pack(pack), table, record)
        return {
            "record": record,
            "revision": revision(path),
            "path": str(path.relative_to(self.root)),
            "pack": pack,
            "table": table,
            "new": True,
        }

    def cycle_guard(self, pack: str, table: str, identity: str, record: dict[str, Any]) -> None:
        if table not in ("kcs", "curriculum_nodes"):
            return
        manifest, found = self.records(pack)
        rows = [i.record for i in found[table] if i.record["id"] != identity] + [record]
        if table == "kcs":
            # Invalid descriptive fields can be saved as drafts. Malformed edges cannot hide a cycle.
            for row in rows:
                edges = row.get("prerequisites", [])
                if not isinstance(edges, list) or any(
                    not isinstance(e, dict) or not isinstance(e.get("kc_id"), str) for e in edges
                ):
                    raise ValueError("Each prerequisite needs a KC id.")
            normalized = [
                {"id": r["id"], "status": r.get("status", "active"), "prerequisites": r.get("prerequisites", [])}
                for r in rows
            ]
            cycles = [p for p in curriculum.prerequisite_problems(normalized, manifest) if "cycle:" in p]
            graph = {r["id"]: [edge["kc_id"] for edge in r["prerequisites"]] for r in normalized}
            for directory in api.pack_dirs(self.content):
                if directory == self.pack(pack):
                    continue
                _, other = source.read_pack(directory)
                for item in other["kcs"]:
                    graph[item.record["id"]] = [e["kc_id"] for e in item.record.get("prerequisites", [])]
            grey: list[str] = []
            black: set[str] = set()

            def visit(node: str) -> None:
                if node in grey:
                    cycles.append("prerequisite cycle: " + " -> ".join([*grey[grey.index(node) :], node]))
                    return
                if node in black or node not in graph:
                    return
                grey.append(node)
                for target in graph[node]:
                    visit(target)
                grey.pop()
                black.add(node)

            for node in graph:
                visit(node)
        else:
            by_id = {r["id"]: r.get("parent_id") for r in rows}
            cycles = []
            for start in by_id:
                chain: list[str] = []
                cursor = start
                while cursor in by_id:
                    if cursor in chain:
                        cycles.append("curriculum cycle: " + " -> ".join([*chain, cursor]))
                        break
                    chain.append(cursor)
                    cursor = by_id[cursor]
        if cycles:
            raise ValueError("; ".join(cycles))

    def save(self, request: dict[str, Any]) -> dict[str, Any]:
        pack, table, identity = request["pack"], request["table"], request["id"]
        ctx = self.ctx(request.get("author"))
        account = ctx.need()
        authorise(
            account, "edit_map" if table in ("curriculum_nodes", "kcs") else "edit_draft", ctx.audit, subject=identity
        )
        record = copy.deepcopy(request["record"])
        if not isinstance(record, dict) or record.get("id") != identity or not ID.fullmatch(identity):
            raise ValueError("Record ids are immutable. Create a new record to use another id.")
        if len(json.dumps(record).encode()) > LIMIT:
            raise ValueError("The record is too large.")
        directory = self.pack(pack)
        if table not in TABLES:
            raise ValueError("Unsupported content type.")
        with locked(self.content):
            self.cycle_guard(pack, table, identity, record)
            _, found = self.records(pack)
            prior = next((item for item in found[table] if item.record["id"] == identity), None)
            if prior is None and identity in source._load(directory / "pack.yaml").get("retired_ids", []):
                raise ValueError("A retired identity is permanently reserved; create a new record id.")
            path = prior.path if prior else source.record_path(directory, table, record)
            if not path.resolve().is_relative_to(self.content):
                raise ValueError("The record path leaves the workspace.")
            before = revision(path)
            if before != request["revision"]:
                raise Conflict("This file changed on disk. Copy your draft or reload before saving.")
            if prior and prior.record == record:
                return self.open_record(pack, table, identity)
            if "review" in record or (prior and "review" in prior.record):
                # The client cannot forge a publication state, version or pipeline history.
                baseline = prior.record if prior else record
                transition = api.edited_record(baseline, account)
                record.update({k: transition[k] for k in ("version", "review", "authoring") if k in transition})
            if table in source.PER_RECORD:
                text = source.canonical(record, table)
            else:
                rows = source._load(path) if path.exists() else []
                rows = [record if r["id"] == identity else r for r in rows]
                if prior is None:
                    rows.append(record)
                text = source.canonical(rows, table)
            if table == "kcs":
                manifest_path = directory / "pack.yaml"
                manifest_revision = revision(manifest_path)
                manifest = source._load(manifest_path)
                declared = {d["pack_id"] for d in manifest.get("depends_on_packs", [])}
                targets = {"pack." + ".".join(e["kc_id"].split(".")[1:3]) for e in record.get("prerequisites", [])} - {
                    pack
                }
                for target in sorted(targets - declared):
                    target_manifest, target_found = self.records(target)
                    known = {i.record["id"] for i in target_found["kcs"]}
                    if any(
                        e["kc_id"] not in known
                        for e in record["prerequisites"]
                        if "pack." + ".".join(e["kc_id"].split(".")[1:3]) == target
                    ):
                        raise ValueError("The prerequisite KC is not in the selected dependency pack.")
                    manifest.setdefault("depends_on_packs", []).append(
                        {"pack_id": target, "min_version": target_manifest["version"]}
                    )
                if targets - declared:
                    atomic_write(
                        manifest_path, yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), manifest_revision
                    )
            atomic_write(path, text, before)
            ctx.audit.append(account.account_id, "studio_save", "ok", identity, f"source revision {revision(path)}")
            if table == "items" and "review" in record:
                api.carry_papers(ctx, directory, {identity})  # a published paper travels with its item (ASM-15)
            return self.open_record(pack, table, identity)

    def references(self, pack: str, identity: str) -> dict[str, Any]:
        self.pack(pack)

        def contains(value: Any) -> bool:
            if isinstance(value, str):
                return value == identity
            if isinstance(value, dict):
                return any(contains(v) for v in value.values())
            if isinstance(value, list):
                return any(contains(v) for v in value)
            return False

        refs, known_paths = [], set()
        for directory in api.pack_dirs(self.content):
            _, found = source.read_pack(directory)
            for table, items in found.items():
                for item in items:
                    known_paths.add(item.path)
                    if item.record.get("id") != identity and table != "kc_migrations" and contains(item.record):
                        refs.append(
                            {"id": item.record.get("id", str(item.path.relative_to(self.root))), "table": table}
                        )
        skills, files = [], []
        skill_root = self.root / "skills"
        boundary = re.compile(r"(?<![a-z0-9_.-])" + re.escape(identity) + r"(?![a-z0-9_.-])")
        for path in skill_root.rglob("*.md") if skill_root.is_dir() and not skill_root.is_symlink() else []:
            if path.is_symlink() or not path.resolve().is_relative_to(skill_root) or path.stat().st_size > LIMIT:
                raise ValueError("Unsafe skill reference source")
            if boundary.search(path.read_text()):
                skills.append(str(path.relative_to(self.root)))
        for path in self.content.rglob("*.yaml"):
            if path in known_paths or path.name == "pack.yaml" or "migrations" in path.relative_to(self.content).parts:
                continue
            if contains(source._load(path)):
                files.append(str(path.relative_to(self.root)))
        return {"records": refs, "skills": skills, "files": files}

    async def marking(self, request: dict[str, Any]) -> dict[str, Any]:
        record = request["record"]
        issues = self.candidate(request["pack"], "items", record)
        if not issues["ok"]:
            return {"ok": False, "problems": issues["problems"]}
        checks: list[dict[str, Any]] = []

        async def run(name: str, args: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
            identity = f"chk_studio_{len(checks)}"
            try:
                result = run_check(identity, {"tool": name, "args": args})
            except WorkerError:
                return None, None
            checks.append(result.record())
            return result.value, identity

        async def convert(expression: str, unit: str) -> Converted | None:
            value, identity = await run("units_convert", {"quantity": expression, "to": unit})
            if value is None or identity is None:
                return None
            return Converted(Decimal(str(value["value"]["decimal"])), identity)

        verdict = await mark(record, request["part"], request["response"], convert, Settings(), run=run,
                             signatures=self.signatures(request["pack"], record))  # fmt: skip
        return {"ok": True, "verdict": asdict(verdict), "checks": checks}

    def signatures(self, pack: str, record: dict[str, Any]) -> SignatureContext:
        """What the app matches misconception signatures against (CUR-18), from this workspace's sources: the
        misconception library, the conventions and the `pack.ref` chart of accounts, as an installed pack gives them."""
        loaded = load_source(self.pack(pack))
        manifest = {"pack_id": loaded.pack.get("pack_id"), "version": loaded.pack.get("version"),
                    "subject": loaded.pack.get("subject")}  # fmt: skip
        index = PackIndex.from_tables([(manifest, {**loaded.tables, "charts_of_accounts": charts_of(loaded)})])
        return pack_context(index, record)

    def golden(self, request: dict[str, Any]) -> dict[str, Any]:
        ctx = self.ctx(request.get("author"))
        account = ctx.need()
        authorise(account, "edit_draft", ctx.audit, subject=request["id"])
        record = self.locate(request["pack"], "items", request["id"]).record
        result = asyncio.run(self.marking({**request, "record": record}))
        if not result["ok"] or result["verdict"]["outcome"] == "unverifiable":
            raise ValueError("Only a verified production-marker verdict can become a golden case.")
        case = {
            "item_id": request["id"],
            "pack": request["pack"],
            "part": request["part"],
            "response": request["response"],
            "item_sha256": source.content_sha256(record),
            "expected": {k: result["verdict"][k] for k in ("outcome", "marks_awarded", "marks_available")},
        }
        text = json.dumps(case, indent=2, ensure_ascii=False) + "\n"
        import hashlib

        identity = hashlib.sha256(text.encode()).hexdigest()[:20]
        path = self.root / "evals" / "marking" / "studio" / f"{identity}.json"
        if any(p.is_symlink() for p in (self.root / "evals", self.root / "evals/marking", path.parent, path)):
            raise ValueError("Golden cases must not follow symbolic links.")
        atomic_write(path, text)
        ctx.audit.append(account.account_id, "studio_golden", "ok", request["id"], identity)
        return {"path": str(path.relative_to(self.root)), "case": case}

    def goldens(self, pack: str | None = None) -> dict[str, Any]:
        cases = []
        directory = self.root / "evals/marking/studio"
        if directory.exists() and not directory.resolve().is_relative_to(self.root):
            raise ValueError("Golden cases leave this workspace.")
        for path in sorted(directory.glob("*.json")):
            if path.is_symlink() or path.stat().st_size > LIMIT:
                raise ValueError("Unsafe golden case file.")
            case = json.loads(path.read_text())
            if pack is not None and case["pack"] != pack:
                continue
            record = self.locate(case["pack"], "items", case["item_id"]).record
            result = asyncio.run(self.marking({**case, "record": record}))
            verdict = result.get("verdict", {})
            passed = source.content_sha256(record) == case["item_sha256"] and all(
                verdict.get(k) == v for k, v in case["expected"].items()
            )
            cases.append({"path": str(path.relative_to(self.root)), "ok": passed, "result": result})
        return {"ok": all(case["ok"] for case in cases), "cases": cases}

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        recover_changes(self.root)
        op = request["op"]
        if op in ("job_queue", "job_start", "job_result", "job_personas"):
            from pack_builder import studio_jobs

            if op == "job_personas":
                return {"personas": list(studio_jobs.personas()["personas"])}
            return {"job_queue": studio_jobs.queue, "job_start": studio_jobs.start, "job_result": studio_jobs.result}[
                op
            ](self, request)
        if op in ("import_preview", "import_apply"):
            from pack_builder import studio_import

            return (studio_import.preview if op == "import_preview" else studio_import.apply)(self, request)
        if op.startswith("diagram_"):
            from pack_builder.studio_diagrams import dispatch

            return dispatch(self, request)
        if op.startswith(("analytics_", "inbox_")):
            from pack_builder.studio_analytics import dispatch as analytics_dispatch

            return analytics_dispatch(self, request)
        if op.startswith("skill_"):
            from pack_builder.studio_skills import dispatch

            return dispatch(self, request)
        if op in ("version_plan", "set_version", "retire", "migration"):
            from pack_builder import studio_changes as changes

            if op == "version_plan":
                result = changes.version_plan(self, request["pack"])
                result["revision"] = revision(self.pack(request["pack"]) / "pack.yaml")
                return result
            return {"set_version": changes.set_version, "retire": changes.retire, "migration": changes.migration}[op](
                self, request
            )
        if op in ("promote", "bank"):
            self.preflight()
            ctx = self.ctx(request.get("author"))
            if op == "promote":
                return api.promote(ctx, self.pack(request["pack"]), [request["id"]] if request.get("id") else None)
            return {"ok": True, **api.bank(ctx, self.pack(request["pack"]))}
        if op in ("reference_open", "reference_check", "reference_save"):
            from pack_builder.studio_reference import dispatch

            return dispatch(self, request)
        if op == "workspace":
            return self.inventory()
        if op == "open":
            return self.open_record(request["pack"], request["table"], request["id"])
        if op == "duplicate":
            return self.duplicate(request)
        if op == "save":
            return self.save(request)
        if op == "validate":
            return self.candidate(request["pack"], request["table"], request["record"])
        if op == "references":
            return self.references(request["pack"], request["id"])
        if op == "check":
            category = request["category"]
            if category not in api.GATES:
                raise ValueError("Choose validate, verify, lint, qa or scan.")
            self.preflight()
            return api.check(category, self.pack(request["pack"]))
        if op == "golden":
            return self.golden(request)
        if op == "goldens":
            return self.goldens()
        if op == "graph":
            _, found = self.records(request["pack"])
            return {"nodes": [item.record for item in found["kcs"]]}
        if op == "mark":
            return asyncio.run(self.marking(request))
        if op == "signature_bench":
            from pack_builder.studio_bench import signature_bench

            loaded = load_source(self.pack(request["pack"]))
            record = {k: v for k, v in request["record"].items() if k not in source.SOURCE_ONLY}
            issues = errors("content/content-pack#/$defs/Misconception", record)
            if issues:
                return {"ok": False, "problems": issues}
            library = [m for m in loaded.tables["misconceptions"] if m["id"] != record["id"]] + [record]
            conventions = {c["key"]: str(c["value"]) for c in loaded.tables["conventions"]}
            return asyncio.run(signature_bench(loaded.tables["items"], library, conventions, record["id"]))
        if op in ("template_review_ranges", "template_variant"):
            from pack_builder import studio_templates

            action = studio_templates.review if op == "template_review_ranges" else studio_templates.variant
            return action(self, request)
        if op == "sample_template":
            from pack_builder.studio_bench import sample_template

            self.pack(request["pack"])
            record = {k: v for k, v in request["record"].items() if k not in source.SOURCE_ONLY}
            issues = errors("content/content-pack#/$defs/ItemTemplate", record)
            if issues:
                return {"ok": False, "problems": issues}
            from pack_builder.studio_templates import contexts

            return sample_template(record, request.get("count", 50), contexts(self, request["pack"]))
        if op in ("workflow", "comment", "diff", "evaluate", "publish"):
            from pack_builder import studio_workflow as workflow

            if op == "workflow":
                return workflow.read(self, request["pack"])[1]
            return {
                "comment": workflow.comment,
                "diff": workflow.diff,
                "evaluate": workflow.evaluate,
                "publish": workflow.publication,
            }[op](self, request)
        if op == "bootstrap":
            ctx = self.ctx()
            if Accounts(ctx.root).all():
                raise Forbidden("An authoring account already exists. Select it or use aut account as a lead.")
            result = api.create_account(ctx, request["id"], request["name"], request["email"], ["curriculum_lead"])
            return {"id": result["account_id"]}
        raise ValueError("Unknown studio command.")


def handle(workspace: Workspace, request: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"result": workspace.dispatch(request)}
    except Conflict as error:
        return {"error": "conflict", "message": str(error)}
    except Forbidden as error:
        return {"error": "forbidden", "message": str(error)}
    except (ValueError, KeyError, TypeError, AttributeError, OSError, BuildError, WorkerError, yaml.YAMLError) as error:
        return {"error": "invalid", "message": str(error)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--stdio", action="store_true")
    parser.add_argument("--run-job")
    args = parser.parse_args()
    workspace = Workspace(args.workspace)
    if args.run_job:
        from pack_builder.studio_jobs import run

        return run(workspace, args.run_job)
    while True:
        line = sys.stdin.buffer.readline(LIMIT + 1)
        if not line:
            break
        try:
            if len(line) > LIMIT:
                raise ValueError("Studio request exceeds 512 KiB.")
            request = json.loads(line)
            response = handle(workspace, request)
        except (ValueError, TypeError) as error:
            response = {"error": "invalid", "message": str(error)}
        print(json.dumps(response, ensure_ascii=False), flush=True)
        if not args.stdio:
            return int("error" in response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
