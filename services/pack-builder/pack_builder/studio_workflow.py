"""Local authoring records. Comments/evidence stay outside pack inputs; publishing uses the signing inbox."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from agent_core.config import Settings

from pack_builder import api, source, versioning
from pack_builder.accounts import authorise, now_iso
from pack_builder.build import categorised_problems, load_source
from pack_builder.studio_io import atomic_write, revision

if TYPE_CHECKING:
    from pack_builder.studio import Workspace


def comment_blockers(root: Path, pack: str) -> list[str]:
    if not re.fullmatch(r"pack\.[a-z0-9_.-]{1,100}", pack):
        return ["Invalid pack identity for authoring metadata"]
    path = root / "reviews" / "studio" / f"{pack}.json"
    if any(p.is_symlink() for p in (root / "reviews", path.parent, path)):
        return ["Unsafe authoring comment metadata"]
    if not path.exists():
        return []
    try:
        if path.stat().st_size > 512 * 1024:
            raise ValueError("too large")
        comments = json.loads(path.read_text())["comments"]
        return [f"Open blocking comment: {c['id']}" for c in comments if c["blocking"] and c["status"] == "open"]
    except (ValueError, KeyError, TypeError):
        return ["Invalid authoring comment metadata"]


def metadata_path(workspace: Workspace, pack: str) -> Path:
    workspace.pack(pack)
    if not re.fullmatch(r"pack\.[a-z0-9_.-]{1,100}", pack):
        raise ValueError("Invalid pack identity")
    path = workspace.root / "reviews" / "studio" / f"{pack}.json"
    for parent in (workspace.root / "reviews", path.parent, path):
        if parent.is_symlink():
            raise ValueError("Authoring metadata must not follow symbolic links")
    if path.exists() and path.stat().st_size > 512 * 1024:
        raise ValueError("Authoring metadata is too large")
    return path


def read(workspace: Workspace, pack: str) -> tuple[Path, dict[str, Any], str]:
    path = metadata_path(workspace, pack)
    return (
        path,
        json.loads(path.read_text()) if path.exists() else {"comments": [], "evaluations": [], "simulations": []},
        revision(path),
    )


def store(path: Path, data: dict[str, Any], expected: str) -> None:
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    if len(text.encode()) > 512 * 1024:
        raise ValueError("Authoring metadata is full; archive resolved threads before adding more")
    atomic_write(path, text, expected)


def fingerprint(workspace: Workspace, pack: str) -> str:
    directory = workspace.pack(pack)
    paths = [p for p in directory.rglob("*") if p.is_file()]
    paths += [p for p in source.ref_dir(directory).rglob("*") if p.is_file()]
    paths += [p for p in source.migrations_dir(directory, pack).rglob("*") if p.is_file()]
    # Skill and scenario changes invalidate the report, even if content records did not change.
    for name in ("skills", "evals/scenarios", "evals/marking/studio", "evals/personas", "content/agent"):
        paths += [p for p in (workspace.root / name).rglob("*") if p.is_file()]
    digest = hashlib.sha256()
    for name in ("spec.lock", "uv.lock"):
        lock = workspace.root / name
        if lock.exists():
            digest.update(lock.read_bytes())
    if getattr(sys, "frozen", False):
        # Include the executable and static runtime resources in the evaluation identity.
        for directory in (Path(sys.executable).parent, Path(sys.executable).parent.parent / "tool-worker"):
            for resource in sorted(directory.rglob("*")):
                if resource.is_file():
                    digest.update(str(resource.relative_to(directory)).encode() + b"\0" + resource.read_bytes())
    # Results are tied to the evaluator and production runtime, as well as authored inputs.
    import agent_core.engine
    import tool_worker.functions

    import pack_builder

    for module in (agent_core.engine, tool_worker.functions, pack_builder):
        assert module.__file__ is not None
        module_root = Path(module.__file__).parent
        if module.__name__ != "pack_builder":
            module_root = module_root.parent
        for code in sorted(module_root.rglob("*.py")):
            digest.update(str(code.relative_to(module_root)).encode() + b"\0" + code.read_bytes())
    for path in sorted(set(paths)):
        if path.is_symlink() or not path.resolve().is_relative_to(workspace.root) or path.stat().st_size > 512 * 1024:
            raise ValueError("Unsafe evaluation input")
        digest.update(str(path.relative_to(workspace.root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


_ABSENT = object()


def field_diff(before: Any, after: Any, path: str = "$") -> list[dict[str, Any]]:
    if isinstance(before, dict) and isinstance(after, dict):
        absent = _ABSENT
        return [
            row
            for key in sorted(before.keys() | after.keys())
            for row in field_diff(before.get(key, absent), after.get(key, absent), f"{path}.{key}")
        ]
    if isinstance(before, list) and isinstance(after, list):
        absent = _ABSENT
        return [
            row
            for index in range(max(len(before), len(after)))
            for row in field_diff(
                before[index] if index < len(before) else absent,
                after[index] if index < len(after) else absent,
                f"{path}[{index}]",
            )
        ]
    if before == after:
        return []
    return [
        {
            "field": path,
            "before": {"$absent": True} if before is _ABSENT else before,
            "after": {"$absent": True} if after is _ABSENT else after,
        }
    ]


def published_record(
    workspace: Workspace, pack: str, table: str, identity: str
) -> tuple[dict[str, Any] | None, str | None]:
    item = workspace.locate(pack, table, identity)
    relative = str(item.path.relative_to(workspace.root))
    history = subprocess.run(
        ["git", "log", "-100", "--format=%H", "--", relative],
        cwd=workspace.root,
        capture_output=True,
        text=True,
        check=False,
    )
    if history.returncode:
        return None, None
    for commit in history.stdout.splitlines():
        result = subprocess.run(
            ["git", "show", f"{commit}:{relative}"], cwd=workspace.root, capture_output=True, text=True, check=False
        )
        if result.returncode or len(result.stdout.encode()) > 512 * 1024:
            continue
        value = yaml.safe_load(result.stdout)
        rows = value if isinstance(value, list) else [value]
        row = next((r for r in rows if isinstance(r, dict) and r.get("id") == identity), None)
        if row and (
            (row.get("review") or {}).get("status") == "published"
            or (table == "diagrams" and (row.get("review") or {}).get("status") == "approved")
        ):
            return row, commit
    return None, None


def diff(workspace: Workspace, request: dict[str, Any]) -> dict[str, Any]:
    if request["table"] == "skills":
        from agent_core.assets.skills import split_front_matter
        from jana_tools.skills import _section_text, _sections, version_bump_errors

        from pack_builder.studio_skills import check as skill_check
        from pack_builder.studio_skills import locate

        path = locate(workspace, request["id"])
        relative = str(path.relative_to(workspace.root))
        commits = subprocess.run(
            ["git", "log", "-100", "--format=%H", "--", relative],
            cwd=workspace.root,
            capture_output=True,
            text=True,
            check=False,
        )
        for commit in commits.stdout.splitlines() if commits.returncode == 0 else []:
            baseline = subprocess.run(
                ["git", "show", f"{commit}:{relative}"], cwd=workspace.root, capture_output=True, text=True, check=False
            )
            if baseline.returncode:
                continue
            try:
                meta, body, _ = split_front_matter(baseline.stdout)
                if meta.get("status") != "reviewed":
                    continue
                current = str(request["record"]["text"])
                after_meta, after_body, _ = split_front_matter(current)
                sections = sorted({title for title, _ in [*_sections(body), *_sections(after_body)]})
                return {
                    "available": True,
                    "commit": commit,
                    "baseline_kind": "committed reviewed skill",
                    "fields": field_diff(meta, after_meta),
                    "sections": [
                        {
                            "section": title,
                            "before": (_section_text(body, title) or ("", 0))[0],
                            "after": (_section_text(after_body, title) or ("", 0))[0],
                        }
                        for title in sections
                        if _section_text(body, title) != _section_text(after_body, title)
                    ],
                    "lint_before": skill_check(workspace, request["id"], baseline.stdout)["findings"],
                    "lint_after": skill_check(workspace, request["id"], current)["findings"],
                    "version_problems": version_bump_errors(request["id"], baseline.stdout, current),
                }
            except (ValueError, KeyError, yaml.YAMLError):
                continue
        return {"available": False, "problems": ["No committed reviewed skill baseline is available."]}
    before, baseline_commit = published_record(workspace, request["pack"], request["table"], request["id"])
    after = request["record"]
    if before is None:
        return {"available": False, "problems": ["No committed published baseline is available for this record."]}
    if request["table"] == "diagrams":
        from pack_builder.studio_diagrams import check, drawing_path

        drawing = drawing_path(workspace, request["pack"], before)
        old_svg = subprocess.run(
            ["git", "show", f"{baseline_commit}:{drawing.relative_to(workspace.root)}"],
            cwd=workspace.root,
            capture_output=True,
            text=True,
            check=False,
        )
        current_svg = drawing_path(workspace, request["pack"], after).read_text()
        return {
            "available": True,
            "commit": baseline_commit,
            "fields": field_diff(before, after),
            "before_preview": check(workspace, request["pack"], before, old_svg.stdout)["preview"]
            if old_svg.returncode == 0
            else None,
            "after_preview": check(workspace, request["pack"], after, current_svg)["preview"],
            "parts_before": before.get("parts", []),
            "parts_after": after.get("parts", []),
        }
    semantic = versioning.semantic_diff({request["table"]: [before]}, {request["table"]: [after]})
    return {
        "available": True,
        "commit": baseline_commit,
        "before": before,
        "after": after,
        "fields": field_diff(before, after),
        **semantic.summary(),
        "before_preview": workspace.preview(request["pack"], request["table"], before),
        "after_preview": workspace.preview(request["pack"], request["table"], after),
    }


def comment(workspace: Workspace, request: dict[str, Any]) -> dict[str, Any]:
    pack = request["pack"]
    ctx = workspace.ctx(request.get("author"))
    account = ctx.need()
    authorise(account, "edit_draft", ctx.audit, subject=pack)
    path, data, expected = read(workspace, pack)
    if request.get("resolve"):
        row = next((c for c in data["comments"] if c["id"] == request["resolve"]), None)
        if row is None:
            raise ValueError("That comment does not exist")
        row.update(status="resolved", resolved_by=account.rev_id, resolved_at=now_iso())
    else:
        identity, table = request["id"], request["table"]
        if table == "skills":
            from pack_builder.studio_skills import locate

            skill_path = locate(workspace, identity)
            record = {"id": identity, "text": skill_path.read_text()}
        else:
            record = workspace.locate(pack, table, identity).record
        anchor = request.get("anchor", {"kind": "record", "value": identity})
        kind, value = anchor.get("kind"), str(anchor.get("value", ""))
        if kind not in ("record", "field", "step", "part", "skill_line") or not value or len(value) > 200:
            raise ValueError("Choose a record, field, step, part or skill-line anchor")
        if kind == "part" and value not in {p["id"] for p in record.get("parts", [])}:
            raise ValueError("That part is not in the record")
        if kind == "step" and value not in {
            str(p["n"]) for p in record.get("steps", record.get("solution", {}).get("steps", []))
        }:
            raise ValueError("That step is not in the record")
        if kind == "skill_line" and (
            table != "skills" or not value.isdigit() or not 1 <= int(value) <= len(record["text"].splitlines())
        ):
            raise ValueError("Choose an existing line in this skill")
        if kind == "field":
            field = value if value.startswith("$") else "$." + value
            if not re.fullmatch(r"\$(?:\.[a-zA-Z_][a-zA-Z0-9_]*|\[\d+\])*", field):
                raise ValueError("Use an existing field path, such as $.parts[0].answer")
            current: Any = record
            try:
                for key, number in re.findall(r"\.([a-zA-Z_][a-zA-Z0-9_]*)|\[(\d+)\]", field):
                    current = current[key] if key else current[int(number)]
            except (KeyError, IndexError, TypeError):
                raise ValueError("That field does not exist in this record") from None
        text = str(request["text"]).strip()
        if not text or len(text) > 2000:
            raise ValueError("Comments need 1-2000 characters")
        # Comments are author instructions, never a place to paste a learner transcript.
        if re.search(r"(?m)^>|\bstudent\s+(said|wrote|says)|[\w.+-]+@[\w.-]+", text, re.I):
            raise ValueError("Remove quoted student text and personal/contact details")
        quotes = re.findall(r'["“]([^"”]+)["”]', text)
        if any(len(q.split()) >= 5 and re.search(r"\b(I|my|me|I'm)\b", q) for q in quotes) or any(
            len(re.sub(r"\D", "", phone)) >= 9 for phone in re.findall(r"\+?\d[\d -]{8,}\d", text)
        ):
            raise ValueError("Remove quoted student text and personal/contact details")
        from jana_tools.skills import FICTIONAL_NAMES

        names = re.findall(r"\b(?i:Mr|Mrs|Ms|Dr|student|learner|pupil|name)\.?\s+([A-Z][a-z]+)", text)
        if any(name not in FICTIONAL_NAMES for name in names):
            raise ValueError("Use a name from the fictional-names list")
        parent = request.get("parent")
        if parent and not any(c["id"] == parent and c["record_id"] == identity for c in data["comments"]):
            raise ValueError("Reply threads must belong to this record")
        row = {
            "id": "comment_" + hashlib.sha256((text + now_iso()).encode()).hexdigest()[:20],
            "record_id": identity,
            "table": table,
            "anchor": anchor,
            "parent": parent,
            "text": text,
            "blocking": bool(request.get("blocking")),
            "status": "open",
            "author": account.rev_id,
            "created_at": now_iso(),
        }
        data["comments"].append(row)
    store(path, data, expected)
    ctx.audit.append(account.account_id, "studio_comment", "ok", pack, row["id"])
    return row


def evaluate(workspace: Workspace, request: dict[str, Any]) -> dict[str, Any]:
    pack = request["pack"]
    directory = workspace.pack(pack)
    ctx = workspace.ctx(request.get("author"))
    account = ctx.need()
    authorise(account, "edit_draft", ctx.audit, subject=pack)
    before = fingerprint(workspace, pack)
    grouped, _ = categorised_problems(load_source(directory), Settings())
    oracle = api.eval_pack(directory)
    golden = workspace.goldens(pack)
    problems = [p for category in ("validate", "verify", "lint", "qa", "scan") for p in grouped[category]]
    if not oracle["ok"]:
        problems.append("Pack oracle evaluation failed")
    if not golden["ok"]:
        problems.append("A saved marking case failed or names stale content")
    needs_models = any((workspace.root / "skills").rglob("SKILL.md")) or any(
        (workspace.root / "evals/scenarios").rglob("*.yaml")
    )
    previous = read(workspace, pack)[1]["evaluations"]
    models_green = any(r.get("kind") == "local_model" and r["ok"] and r["sha256"] == before for r in previous)
    if needs_models and not models_green:
        problems.append("Current skill/scenario local-model evaluation evidence is required")
    after = fingerprint(workspace, pack)
    if before != after:
        problems.append("Inputs changed during evaluation; run again")
    report = {
        "sha256": before,
        "ok": not problems,
        "problems": problems,
        "oracle": oracle,
        "goldens": golden,
        "completed_at": now_iso(),
        "network": "off",
        "kind": "local_pack_tools",
    }
    path, data, expected = read(workspace, pack)
    data["evaluations"] = [*data["evaluations"][-4:], report]
    store(path, data, expected)
    if needs_models and not models_green:
        from pack_builder import studio_jobs

        queued = studio_jobs.queue(workspace, {**request, "kind": "local_evaluation"})
        report["job"] = queued["job"]
        report["runtime"] = studio_jobs.start(
            workspace, {"job_id": queued["job"]["id"], "author": request.get("author")}
        )
    return report


def publication(workspace: Workspace, request: dict[str, Any]) -> dict[str, Any]:
    pack = request["pack"]
    _path, data, _ = read(workspace, pack)
    ctx = workspace.ctx(request.get("author"))
    account = ctx.need()
    authorise(account, "request_publication", ctx.audit, subject=pack)
    blockers = [
        f"Open blocking comment: {c['id']}" for c in data["comments"] if c["blocking"] and c["status"] == "open"
    ]
    current = fingerprint(workspace, pack)
    if not any(r["ok"] and r["sha256"] == current for r in data["evaluations"]):
        blockers.append("Run green evaluations on the current content before publishing")
    from pack_builder.studio_changes import version_plan

    plan = version_plan(workspace, pack)
    if plan["available"]:
        blockers += plan["problems"]
    directory = workspace.pack(pack)
    # Requests name HEAD, so refuse unsaved/uncommitted sources and comments instead of queuing another version.
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=workspace.root,
        capture_output=True,
        text=True,
        check=False,
    )
    if dirty.returncode or dirty.stdout.strip():
        blockers.append("Commit the evaluated working copy and authoring records before queuing publication")
    if blockers:
        return {"ok": False, "problems": blockers}
    result = api.publish(ctx, directory, request.get("channel", "dev"))
    return {"ok": True, **result, "signed": False, "message": "Request queued for the signing service"}
