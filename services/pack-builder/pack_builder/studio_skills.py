"""Bounded skill editing and the same line-aware gates used by CI."""

from __future__ import annotations

import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from agent_core.assets.skills import KINDS, Skill, SkillError, SkillSet, discover, parse, role_slice, split_front_matter
from agent_core.config import Settings
from jana_tools import skill_evals
from jana_tools.skills import SECTIONS, Catalogue, injection_scan, lint, version_bump_errors

from pack_builder.accounts import authorise
from pack_builder.form_schema import form_schema
from pack_builder.studio_io import Conflict, atomic_write, revision

if TYPE_CHECKING:
    from pack_builder.studio import Workspace

LIMIT = 512 * 1024


def inventory(workspace: Workspace) -> list[dict[str, str]]:
    root = workspace.root / "skills"
    if root.is_symlink():
        raise ValueError("Skills must not follow symbolic links")
    out = []
    for path in discover(root, KINDS):
        if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size > LIMIT:
            raise ValueError("Unsafe skill source")
        if any(p.is_symlink() for p in path.parents if p.is_relative_to(root)):
            raise ValueError("Unsafe skill directory")
        out.append(
            {
                "name": path.parent.name,
                "path": str(path.relative_to(workspace.root)),
                "kind": path.relative_to(root).parts[0],
            }
        )
    if len(out) > 4000:
        raise ValueError("Too many skills in the authoring workspace")
    return out


def locate(workspace: Workspace, name: str) -> Path:
    paths = [workspace.root / r["path"] for r in inventory(workspace) if r["name"] == name]
    if len(paths) != 1:
        raise ValueError("Choose one unambiguous skill from this workspace")
    return paths[0]


def check(workspace: Workspace, name: str, text: str) -> dict[str, Any]:
    path = locate(workspace, name)
    if len(text.encode()) > LIMIT:
        raise ValueError("Skill source is too large")
    catalogue = Catalogue.from_registers({r["name"] for r in inventory(workspace)}).with_packs(workspace.content)
    try:
        findings = lint(path, catalogue, text)
    except yaml.YAMLError as error:
        return {
            "ok": False,
            "findings": [{"rule": "schema", "line": 1, "message": str(error)}],
            "frontmatter": {},
            "body": "",
            "schema": form_schema("skills/skill-frontmatter"),
            "eval_cases": 0,
            "eval_minimum": 0,
            "current_evals_green": False,
        }
    meta: dict[str, Any] = {}
    body, start = "", 1
    slices: dict[str, str] = {}
    budget: dict[str, dict[str, Any]] = {}
    cases: list[dict[str, Any]] = []
    needed = 0
    try:
        meta, body, start = split_front_matter(text)
        findings += injection_scan(path, set(meta.get("requires_tools", [])), catalogue.tools, text)
        skill = Skill(name, str(meta["kind"]), meta, body, path, start)
        cases, case_problems = skill_evals.load_cases(path.parent)
        needed = skill_evals.minimum(skill.kind, body, Settings())
        others = {}
        for row in inventory(workspace):
            if row["name"] != name:
                try:
                    other = parse(workspace.root / row["path"])
                    others[other.name] = other
                except (SkillError, ValueError):
                    continue
        others[name] = skill
        skills = SkillSet(others, frozenset({"draft", "reviewed"}))
        for role in meta.get("roles", []):
            part = role_slice(skill, role)
            slices[role] = body if part is None else part
            try:
                lines = skills.index(role)
                budget[role] = {"ok": True, "lines": len(lines), "characters": len("\n".join(lines))}
            except SkillError as error:
                budget[role] = {"ok": False, "problem": str(error)}
        results = skill_evals.read_results(workspace.root / "evals/skill-evals/results.json")
        evidence = results.get(name)
        passing = bool(
            evidence
            and evidence.get("source") == "studio_local_model"
            and evidence.get("engine_stage") == "S2"
            and evidence.get("network") == "loopback_only"
            and skill_evals.passing(evidence, skill, Settings())
        )
    except (SkillError, ValueError, KeyError, TypeError) as error:
        case_problems, passing = [str(error)], False
    return {
        "ok": not findings and not case_problems and all(b["ok"] for b in budget.values()),
        "findings": [asdict(f) for f in findings],
        "frontmatter": meta,
        "body": body,
        "body_start_line": start,
        "schema": form_schema("skills/skill-frontmatter"),
        "sections": SECTIONS.get(str(meta.get("kind")), []),
        "eval_cases": len(cases),
        "eval_minimum": needed,
        "eval_problems": case_problems,
        "current_evals_green": passing,
        "role_slices": slices,
        "index_budget": budget,
    }


def dispatch(workspace: Workspace, request: dict[str, Any]) -> dict[str, Any]:
    op = request["op"]
    name = request["name"]
    path = locate(workspace, name)
    if op == "skill_compose":
        meta = request["frontmatter"]
        if not isinstance(meta, dict) or meta.get("name") != name:
            raise ValueError("Skill names are immutable")
        return {
            "text": "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n" + str(request["body"])
        }
    if op == "skill_open":
        text = path.read_text()
        return {"name": name, "text": text, "revision": revision(path), "report": check(workspace, name, text)}
    if op == "skill_check":
        return check(workspace, name, request["text"])
    ctx = workspace.ctx(request.get("author"))
    account = ctx.need()
    authorise(account, "edit_draft", ctx.audit, subject=name)
    text = str(request["text"])
    if len(text.encode()) > LIMIT:
        raise ValueError("Skill source is too large")
    if op == "skill_submit":
        report = check(workspace, name, path.read_text())
        if not report["ok"] or report["eval_cases"] < report["eval_minimum"] or not report["current_evals_green"]:
            return {
                "ok": False,
                "problems": ["Fix lint and run green colocated evaluations on the current skill before submission"],
                "report": report,
            }
        baseline = subprocess.run(
            ["git", "show", f"HEAD:{path.relative_to(workspace.root)}"],
            cwd=workspace.root,
            capture_output=True,
            text=True,
            check=False,
        )
        if baseline.returncode == 0:
            problems = version_bump_errors(name, baseline.stdout, path.read_text())
            if problems:
                return {"ok": False, "problems": problems}
        if revision(path) != request["revision"]:
            raise Conflict("The skill changed before submission; reload and evaluate it again")
        meta, body, _ = split_front_matter(path.read_text())
        meta["status"] = "reviewed"
        atomic_write(
            path,
            "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n" + body,
            request["revision"],
        )
        ctx.audit.append(account.account_id, "studio_skill_submit", "ok", name, revision(path))
        return {"ok": True, "name": name, "sha256": parse(path).sha256, "revision": revision(path)}
    if op != "skill_save":
        raise ValueError("Unknown skill command")
    try:
        meta, body, _ = split_front_matter(text)
        if meta.get("name") != name:
            raise ValueError("Skill names are immutable in this editor")
        # Saving drafts cannot grant reviewed status. Live eval gates remain authoritative.
        meta["status"] = "draft"
        text = "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n" + body
        version_problems = version_bump_errors(name, path.read_text(), text)
    except (SkillError, yaml.YAMLError):
        version_problems = ["Finish the YAML front matter before submitting"]
    atomic_write(path, text, request["revision"])
    ctx.audit.append(account.account_id, "studio_skill_save", "ok", name, revision(path))
    return {
        "name": name,
        "text": text,
        "revision": revision(path),
        "report": check(workspace, name, text),
        "version_problems": version_problems,
    }
