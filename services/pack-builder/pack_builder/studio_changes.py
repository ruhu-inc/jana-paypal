"""Preview and apply authored release changes, with impact checks before any source mutation."""

from __future__ import annotations

import copy
import io
import json
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from agent_core.contracts import errors
from agent_core.learner.kc_migrations import KcMigrations

from pack_builder import api, source, versioning
from pack_builder.accounts import authorise
from pack_builder.build import load_source
from pack_builder.studio_io import atomic_changes, revision
from pack_builder.studio_workflow import read

if TYPE_CHECKING:
    from pack_builder.studio import Workspace


def published_pack(workspace: Workspace, pack: str) -> tuple[dict[str, Any], dict[str, Any], str] | None:
    relative = str(workspace.pack(pack).relative_to(workspace.root))
    commits = subprocess.run(
        ["git", "log", "-100", "--format=%H", "--", relative],
        cwd=workspace.root,
        capture_output=True,
        text=True,
        check=False,
    )
    for commit in commits.stdout.splitlines() if commits.returncode == 0 else []:
        paths = subprocess.run(
            [
                "git",
                "ls-tree",
                "-r",
                "--name-only",
                commit,
                "--",
                relative,
                str(source.ref_dir(workspace.pack(pack)).relative_to(workspace.root)),
                str(source.migrations_dir(workspace.pack(pack), pack).relative_to(workspace.root)),
            ],
            cwd=workspace.root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        if len(paths) > 40000:
            raise ValueError("Published baseline is too large")
        with tempfile.TemporaryDirectory(prefix="studio-published-") as name:
            root = Path(name)
            archive = subprocess.run(
                ["git", "archive", "--format=tar", commit, "--", *paths],
                cwd=workspace.root,
                capture_output=True,
                check=True,
            )
            if len(archive.stdout) > 64 * 1024 * 1024:
                raise ValueError("Published baseline exceeds the authoring limit")
            with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
                if any(member.issym() or member.islnk() or member.size > 512 * 1024 for member in bundle.getmembers()):
                    raise ValueError("Unsafe published baseline member")
                bundle.extractall(root, filter="data")
            try:
                manifest, found = source.read_pack(root / relative)
            except (ValueError, OSError, KeyError):
                continue
            reviewed = [i.record for table in api.REVIEWED_TABLES for i in found[table]]
            if reviewed and all(r.get("review", {}).get("status") in ("published", "retired") for r in reviewed):
                loaded = load_source(root / relative)
                return manifest, loaded.tables, commit
    return None


def version_plan(workspace: Workspace, pack: str) -> dict[str, Any]:
    loaded = load_source(workspace.pack(pack))
    baseline = published_pack(workspace, pack)
    if baseline is None:
        return {
            "available": False,
            "version": loaded.pack["version"],
            "problems": ["No committed published pack baseline; the signing service must supply the release baseline."],
        }
    old, tables, commit = baseline
    diff = versioning.semantic_diff(tables, loaded.tables)
    return {
        "available": True,
        "commit": commit,
        "old_version": old["version"],
        "version": loaded.pack["version"],
        **diff.summary(),
        "problems": versioning.version_problems(old, loaded.pack, diff),
    }


def set_version(workspace: Workspace, request: dict[str, Any]) -> dict[str, Any]:
    pack = request["pack"]
    plan = version_plan(workspace, pack)
    ctx = workspace.ctx(request.get("author"))
    account = ctx.need()
    authorise(account, "request_publication", ctx.audit, subject=pack)
    if not plan["available"]:
        raise ValueError(plan["problems"][0])
    version = str(request["version"])
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version):
        raise ValueError("Use a semantic version such as 2.0.0")
    loaded = load_source(workspace.pack(pack))
    old = {"version": plan["old_version"]}
    baseline = published_pack(workspace, pack)
    assert baseline is not None
    problems = versioning.version_problems(
        old, {"version": version}, versioning.semantic_diff(baseline[1], loaded.tables)
    )
    if problems:
        raise ValueError("; ".join(problems))
    corrections = request.get("corrections", {})
    changed = [rid for values in plan["changed"].values() for rid in values]
    if any(not str(corrections.get(rid, "")).strip() for rid in changed):
        raise ValueError("Give one reader-facing correction sentence for each changed record")
    path = workspace.pack(pack) / "pack.yaml"
    manifest = source._load(path)
    manifest["version"] = version
    meta, data, expected = read(workspace, pack)
    data["release_notes"] = {**plan, "version": version, "corrections": corrections}
    atomic_changes(
        workspace.root,
        [
            {
                "path": str(path.relative_to(workspace.root)),
                "text": yaml.safe_dump(manifest, sort_keys=False),
                "expected": request["revision"],
            },
            {
                "path": str(meta.relative_to(workspace.root)),
                "text": json.dumps(data, indent=2) + "\n",
                "expected": expected,
            },
        ],
    )
    ctx.audit.append(account.account_id, "studio_version", "ok", pack, version)
    return {"ok": True, "version": version, "release_notes": data["release_notes"]}


def retire(workspace: Workspace, request: dict[str, Any]) -> dict[str, Any]:
    pack, table, identity = request["pack"], request["table"], request["id"]
    ctx = workspace.ctx(request.get("author"))
    account = ctx.need()
    authorise(account, "retire_or_migrate", ctx.audit, subject=identity)
    item = workspace.locate(pack, table, identity)
    if table == "kcs":
        raise ValueError("Use the KC migration editor to retire a knowledge component")
    reason = str(request.get("reason", "")).strip()
    if not reason or len(reason) > 300:
        raise ValueError("Retirement needs a reason of at most 300 characters")
    replacement = request.get("replacement")
    if replacement:
        if replacement == identity:
            raise ValueError("A retired record cannot replace itself")
        workspace.locate(pack, table, replacement)
    refs = workspace.references(pack, identity)
    if refs["records"] or refs["skills"] or refs.get("files"):
        return {"ok": False, "problems": ["Update every reference before retirement"], "impact": refs}
    if not request.get("apply"):
        return {
            "ok": True,
            "impact": refs,
            "record_revision": revision(item.path),
            "manifest_revision": revision(workspace.pack(pack) / "pack.yaml"),
        }
    record = copy.deepcopy(item.record)
    record["review"] = {
        **record.get("review", {}),
        "status": "retired",
        "retired_reason": reason,
        **({"replaced_by": replacement} if replacement else {}),
    }
    manifest_path = workspace.pack(pack) / "pack.yaml"
    manifest = source._load(manifest_path)
    manifest["retired_ids"] = sorted(set(manifest.get("retired_ids", [])) | {identity})
    meta, data, expected = read(workspace, pack)
    data.setdefault("retirements", []).append({"record": record, "reason": reason, "replacement": replacement})
    atomic_changes(
        workspace.root,
        [
            {
                "path": str(manifest_path.relative_to(workspace.root)),
                "text": yaml.safe_dump(manifest, sort_keys=False),
                "expected": request["manifest_revision"],
            },
            {
                "path": str(meta.relative_to(workspace.root)),
                "text": json.dumps(data, indent=2) + "\n",
                "expected": expected,
            },
            {"path": str(item.path.relative_to(workspace.root)), "text": None, "expected": request["record_revision"]},
        ],
    )
    ctx.audit.append(account.account_id, "studio_retire", "ok", identity, reason)
    return {"ok": True, "retired": identity}


def migration(workspace: Workspace, request: dict[str, Any]) -> dict[str, Any]:
    pack = request["pack"]
    loaded = load_source(workspace.pack(pack))
    ctx = workspace.ctx(request.get("author"))
    account = ctx.need()
    authorise(account, "retire_or_migrate", ctx.audit, subject=pack)
    rows = request["migrations"]
    if not isinstance(rows, list) or not rows or len(rows) > 100:
        raise ValueError("Supply 1-100 KC migration rows")
    problems = [p for row in rows for p in errors("content/content-pack#/$defs/KcMigration", row)]
    if problems:
        return {"ok": False, "problems": problems}
    KcMigrations.from_rows([*loaded.tables["kc_migrations"], *rows])
    tables = copy.deepcopy(loaded.tables)
    old_ids = {r["from_kc_id"] for r in rows}
    active = {k["id"] for k in loaded.tables["kcs"] if k.get("status") == "active"}
    if not old_ids <= active or any(r["from_version"] != loaded.pack["version"] for r in rows):
        raise ValueError("Migrations must start from active KCs and the current pack version")
    for kc in tables["kcs"]:
        if kc["id"] in old_ids:
            kc["status"] = "retired"
    tables["kc_migrations"] += rows
    version = rows[0]["to_version"]
    if any(row["to_version"] != version for row in rows):
        raise ValueError("A migration change set must target one release version")
    problems += versioning.migration_problems(tables, version, loaded.tables)
    impact = {identity: workspace.references(pack, identity) for identity in old_ids}
    stale = [identity for identity, refs in impact.items() if refs["records"] or refs["skills"] or refs.get("files")]
    if stale:
        problems.append("Update all references before applying migration: " + ", ".join(stale))
    from pack_builder.studio_replay import replay

    evidence = replay(loaded.pack, loaded.tables, rows)
    if not evidence["ok"]:
        problems.append("Migration projector replay is not reproducible")
    result = {
        "ok": not problems,
        "problems": problems,
        "impact": impact,
        "replay": evidence,
        "required_bump": "major",
        "migrations": rows,
        "version": version,
    }
    paths = {source.record_path(workspace.pack(pack), "kcs", kc) for kc in tables["kcs"] if kc["id"] in old_ids}
    manifest_path = workspace.pack(pack) / "pack.yaml"
    migration_path = source.record_path(workspace.pack(pack), "kc_migrations", rows[0])
    paths |= {manifest_path, migration_path}
    revisions = {str(p.relative_to(workspace.root)): revision(p) for p in paths}
    result["revisions"] = revisions
    if not request.get("apply") or problems:
        return result
    if request.get("revisions") != revisions:
        raise ValueError("Sources changed since migration preview; preview again")
    manifest = source._load(manifest_path)
    manifest["version"] = version
    entries = [
        {
            "path": str(manifest_path.relative_to(workspace.root)),
            "text": yaml.safe_dump(manifest, sort_keys=False),
            "expected": revisions[str(manifest_path.relative_to(workspace.root))],
        }
    ]
    existing = source._load(migration_path) if migration_path.exists() else []
    entries.append(
        {
            "path": str(migration_path.relative_to(workspace.root)),
            "text": source.canonical([*existing, *rows], "kc_migrations"),
            "expected": revisions[str(migration_path.relative_to(workspace.root))],
        }
    )
    for path in paths - {manifest_path, migration_path}:
        current = source._load(path)
        updated = [{**kc, "status": "retired"} if kc["id"] in old_ids else kc for kc in current]
        entries.append(
            {
                "path": str(path.relative_to(workspace.root)),
                "text": source.canonical(updated, "kcs"),
                "expected": revisions[str(path.relative_to(workspace.root))],
            }
        )
    atomic_changes(workspace.root, entries)
    ctx.audit.append(account.account_id, "studio_migration", "ok", pack, version)
    return {**result, "applied": True}
