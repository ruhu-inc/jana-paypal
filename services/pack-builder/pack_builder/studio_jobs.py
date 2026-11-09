"""Durable evaluation jobs. Only a configured loopback model endpoint is used; no cloud fallback."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import yaml
from agent_core.assets.skills import KINDS, discover, parse
from agent_core.config import Settings
from agent_core.providers.llama import LlamaServerProvider
from jana_tools import skill_evals
from jana_tools.harness import simulator

from pack_builder import api
from pack_builder.accounts import authorise, now_iso
from pack_builder.build import categorised_problems, load_source
from pack_builder.studio_io import atomic_changes, atomic_write, locked
from pack_builder.studio_workflow import fingerprint, read

if TYPE_CHECKING:
    from pack_builder.studio import Workspace

ID = re.compile(r"job_[0-9a-f]{24}\Z")


def path_for(workspace: Workspace, identity: str, category: str = "requests") -> Path:
    if not ID.fullmatch(identity):
        raise ValueError("Invalid evaluation request id")
    path = workspace.root / "reviews/studio" / category / (identity + ".json")
    if any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(workspace.root)):
        raise ValueError("Evaluation records must not follow symbolic links")
    return path


def load(workspace: Workspace, identity: str) -> dict[str, Any]:
    path = path_for(workspace, identity)
    if path.stat().st_size > 512 * 1024:
        raise ValueError("Evaluation request is too large")
    return dict(json.loads(path.read_text()))


def local_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Studio evaluations require an explicitly configured loopback HTTP model endpoint")
    return url


def personas() -> dict[str, Any]:
    return simulator.load_personas()


def queue(workspace: Workspace, request: dict[str, Any]) -> dict[str, Any]:
    pack = request["pack"]
    ctx = workspace.ctx(request.get("author"))
    account = ctx.need()
    authorise(account, "edit_draft", ctx.audit, subject=pack)
    kind = request.get("kind", "simulation")
    if kind not in ("simulation", "local_evaluation"):
        raise ValueError("Choose simulation or local evaluation")
    loaded = load_source(workspace.pack(pack))
    items = {r["id"]: r for r in loaded.tables["items"] if r.get("review", {}).get("status") != "held_out"}
    table, target = request.get("table"), request.get("id")
    if kind == "simulation":
        if not isinstance(target, str):
            raise ValueError("Choose a simulation target")
        if table == "skills":
            from pack_builder.studio_skills import locate

            skill = parse(locate(workspace, target))
            kcs = set(skill.meta.get("kcs", []))
            selected = request.get("item_ids") or [
                identity for identity, item in items.items() if not kcs or kcs & set(item["kc_ids"])
            ]
        elif table == "items":
            selected = [target]
        elif table == "lessons":
            lesson = workspace.locate(pack, table, target).record
            selected = sorted(set(re.findall(r"itm\.[a-z0-9_.]+", json.dumps(lesson))))
        else:
            raise ValueError("Simulate a draft lesson, item set or skill")
        if not selected or len(selected) > 50 or any(identity not in items for identity in selected):
            raise ValueError("Choose 1-50 draft items from this pack; held-out items are unavailable")
        chosen = request.get("personas") or list(personas()["personas"])
        count = request.get("count", 5)
        if (
            not chosen
            or any(p not in personas()["personas"] for p in chosen)
            or not isinstance(count, int)
            or not 1 <= count <= 20
        ):
            raise ValueError("Choose known personas and 1-20 runs per persona")
    else:
        selected, chosen, count = [], [], 0
    identity = "job_" + secrets.token_hex(12)
    job = {
        "id": identity,
        "pack": pack,
        "kind": kind,
        "table": table,
        "target": target,
        "items": selected,
        "personas": chosen,
        "count": count,
        "sha256": fingerprint(workspace, pack),
        "status": "queued",
        "requested_at": now_iso(),
        "requested_by": account.rev_id,
        "development_data_only": True,
        "candidate_acceptance": False,
        "network": "loopback_model_only",
    }
    path = path_for(workspace, identity)
    meta, data, expected = read(workspace, pack)
    data.setdefault("jobs", []).append({"id": identity, "kind": kind, "target": target})
    atomic_changes(
        workspace.root,
        [
            {
                "path": str(path.relative_to(workspace.root)),
                "text": json.dumps(job, indent=2) + "\n",
                "expected": "missing",
            },
            {
                "path": str(meta.relative_to(workspace.root)),
                "text": json.dumps(data, indent=2) + "\n",
                "expected": expected,
            },
        ],
    )
    ctx.audit.append(account.account_id, "studio_eval_queued", "ok", pack, identity)
    return {
        "ok": True,
        "job": job,
        "message": "Simulated students need the evaluation service. Your request is queued."
        if kind == "simulation"
        else "Local model evaluation queued. Configure the local evaluation runtime to run it.",
    }


def start(workspace: Workspace, request: dict[str, Any]) -> dict[str, Any]:
    job = load(workspace, request["job_id"])
    ctx = workspace.ctx(request.get("author"))
    account = ctx.need()
    authorise(account, "edit_draft", ctx.audit, subject=job["pack"])
    url, model = os.environ.get("JANA_STUDIO_EVAL_URL"), os.environ.get("JANA_STUDIO_EVAL_MODEL")
    if not url or not model:
        return {
            "ok": False,
            "queued": True,
            "problems": ["Configure the trusted local evaluation endpoint and model. The request remains queued."],
        }
    local_url(url)
    path = path_for(workspace, job["id"])
    with locked(path.with_suffix(".start")):
        job = load(workspace, job["id"])
        active = False
        if job.get("pid"):
            try:
                os.kill(int(job["pid"]), 0)
                active = True
            except ProcessLookupError:
                pass
            except PermissionError:
                active = True
        if job["status"] in ("starting", "running") and (active or time.time() - job.get("started_epoch", 0) < 60):
            return {"ok": True, "job": job, "message": "Evaluation already running"}
        if job["sha256"] != fingerprint(workspace, job["pack"]):
            raise ValueError("The draft changed; queue a new evaluation request")
        job["status"] = "starting"
        job["started_epoch"] = time.time()
        job.pop("pid", None)
        atomic_write(path, json.dumps(job, indent=2) + "\n")
        args = [sys.executable] + ([] if getattr(sys, "frozen", False) else ["-m", "pack_builder.studio"])
        log = path.with_suffix(".log")
        try:
            with log.open("ab") as stream:
                child = subprocess.Popen(
                    [*args, "--workspace", str(workspace.root), "--run-job", job["id"]],
                    cwd=workspace.root,
                    stdin=subprocess.DEVNULL,
                    stdout=stream,
                    stderr=stream,
                    start_new_session=True,
                )
            job["pid"] = child.pid
            atomic_write(path, json.dumps(job, indent=2) + "\n")
        except OSError:
            job["status"] = "queued"
            atomic_write(path, json.dumps(job, indent=2) + "\n")
            raise
    ctx.audit.append(account.account_id, "studio_eval_started", "ok", job["pack"], job["id"])
    return {"ok": True, "job_id": job["id"], "message": "Local evaluation started"}


def result(workspace: Workspace, request: dict[str, Any]) -> dict[str, Any]:
    job = load(workspace, request["job_id"])
    path = path_for(workspace, job["id"], "results")
    if not path.exists():
        return {"job": job, "result": None}
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Evaluation result is too large")
    value = json.loads(path.read_text())
    return {"job": job, "result": value, "current": job["sha256"] == fingerprint(workspace, job["pack"])}


async def execute(workspace: Workspace, job: dict[str, Any], url: str, model: str, sim_model: str) -> dict[str, Any]:
    local_url(url)
    pack = job["pack"]
    loaded = load_source(workspace.pack(pack))
    key = os.environ.get("JANA_STUDIO_EVAL_KEY", "")
    out = path_for(workspace, job["id"], "runs").with_suffix("")
    out.mkdir(parents=True, exist_ok=True)
    if job["kind"] == "simulation":
        config = personas()
        rows = []
        items = {i["id"]: i for i in loaded.tables["items"]}
        sim = LlamaServerProvider(url, key)
        try:
            for persona in job["personas"]:
                for identity in job["items"]:
                    for n in range(job["count"]):
                        row = await simulator.run_one(
                            url,
                            key,
                            model,
                            sim,
                            sim_model,
                            config,
                            persona,
                            identity,
                            101 + n,
                            out,
                            workspace=workspace.root,
                            item_record=items[identity],
                            engine_stage="S2",
                        )
                        # Calibration and judge observations are independently supplied; never invent rubric scores.
                        row["rubric"] = {"status": "calibrated_judge_required", "scores": None}
                        rows.append(row)
        finally:
            await sim.close()
        return {
            "ok": all(
                r["outcome"] == "complete" and not r["broke_character"] and all(r["checks"].values()) for r in rows
            ),
            "kind": "simulation",
            "rows": rows,
            "tutor_model": model,
            "simulator_model": sim_model,
            "rubric_acceptance": False,
            "external_requirements": ["Calibrated rubric judges and independent candidate review"],
        }
    checks, _ = categorised_problems(loaded, Settings())
    problems = [p for category in ("validate", "verify", "lint", "qa", "scan") for p in checks[category]]
    provider = LlamaServerProvider(url, key)
    records = {}
    rows = []
    current_kcs = {k["id"] for k in loaded.tables["kcs"]}
    try:
        for path in discover(workspace.root / "skills", KINDS):
            skill = parse(path)
            if skill.kind == "teaching" and not current_kcs & set(skill.meta.get("kcs", [])):
                continue
            from pack_builder.studio_skills import check as check_skill

            lint_report = check_skill(workspace, skill.name, path.read_text())
            if not lint_report["ok"]:
                problems.append(f"{skill.name}: lint, injection, references or index budget failed")
            cases, issues = skill_evals.load_cases(skill.folder)
            problems += issues
            if len(cases) < skill_evals.minimum(skill.kind, skill.body, Settings()):
                problems.append(f"{skill.name}: too few colocated evals")
            found = []
            for case in cases:
                value = await skill_evals.run_case(
                    case,
                    skill,
                    provider,
                    model,
                    out / skill.name / case["id"],
                    workspace=workspace.root,
                    engine_stage="S2",
                )
                found.append(value)
                rows.append({"skill": skill.name, **asdict(value), "passed": value.passed})
            records[skill.name] = {
                "version": skill.version,
                "sha256": skill.sha256,
                "source_sha256": skill_evals.source_sha256(skill),
                "evals_sha256": skill_evals.evals_sha256(skill.folder),
                "model": model,
                "source": "studio_local_model",
                "engine_stage": "S2",
                "network": "loopback_only",
                "cases_total": len(cases),
                "cases": len(found),
                "hard_total": sum(r.hard_total for r in found),
                "hard_passed": sum(r.hard_passed for r in found),
                "cases_passed": sum(r.passed for r in found),
            }
            if not skill_evals.passing(records[skill.name], skill, Settings()) or not all(r.passed for r in found):
                problems.append(f"{skill.name}: a colocated evaluation failed")
        # KC-tagged regression scenarios use the same local model and production engine assertions.
        from agent_core.assets.skills import Skill

        for path in sorted((workspace.root / "evals/scenarios").glob("*.yaml")):
            if path.name.endswith(".meta.yaml"):
                continue
            suite = yaml.safe_load(path.read_text())
            from agent_core.contracts import errors

            issues = errors("evals/scenario", suite)
            if issues:
                problems += issues
                continue
            for case in suite["scenarios"]:
                if not any(kc in json.dumps(case) for kc in current_kcs):
                    continue
                context = Skill("studio-scenario", "agent", {"version": "0.1", "status": "draft"}, "", path, 1)
                value = await skill_evals.run_case(
                    case,
                    context,
                    provider,
                    model,
                    out / "scenarios" / case["id"],
                    workspace=workspace.root,
                    engine_stage="S2",
                )
                rows.append({"scenario": case["id"], **asdict(value), "passed": value.passed})
                if not value.passed:
                    problems.append(f"{case['id']}: a KC scenario failed")
    finally:
        await provider.close()
    results_path = workspace.root / "evals/skill-evals/results.json"
    if any(p.is_symlink() for p in (results_path, results_path.parent)):
        raise ValueError("Unsafe skill evaluation results path")
    existing = skill_evals.read_results(results_path)
    existing.update(records)
    atomic_write(results_path, json.dumps({"skills": existing}, indent=2) + "\n")
    oracle, goldens = api.eval_pack(workspace.pack(pack)), workspace.goldens(pack)
    if not oracle["ok"] or not goldens["ok"]:
        problems.append("Pack oracle or saved marker evaluation failed")
    return {
        "ok": not problems,
        "kind": "local_model",
        "rows": rows,
        "problems": problems,
        "oracle": oracle,
        "goldens": goldens,
        "model": model,
        "network": "loopback_only",
        "engine_stage": "S2",
    }


def run(workspace: Workspace, identity: str) -> int:
    job = load(workspace, identity)
    path = path_for(workspace, identity)
    try:
        url = local_url(os.environ["JANA_STUDIO_EVAL_URL"])
        model = os.environ["JANA_STUDIO_EVAL_MODEL"]
        if job["sha256"] != fingerprint(workspace, job["pack"]):
            raise ValueError("Inputs changed before evaluation started")
        with locked(path.with_suffix(".start")):
            job["status"] = "running"
            job["pid"] = os.getpid()
            atomic_write(path, json.dumps(job, indent=2) + "\n")
        value = asyncio.run(execute(workspace, job, url, model, os.environ.get("JANA_STUDIO_SIMULATOR_MODEL", model)))
        if job["sha256"] != fingerprint(workspace, job["pack"]):
            value.update(ok=False, problems=[*value.get("problems", []), "Inputs changed during evaluation"])
        value.update(sha256=job["sha256"], job_id=identity, completed_at=now_iso(), candidate_acceptance=False)
        output = path_for(workspace, identity, "results")
        text = json.dumps(value, indent=2) + "\n"
        if len(text.encode()) > 4 * 1024 * 1024:
            raise ValueError("Split this evaluation into smaller requests")
        atomic_write(output, text)
        meta, data, expected = read(workspace, job["pack"])
        if value["kind"] == "local_model":
            data["evaluations"] = [*data["evaluations"][-4:], {k: v for k, v in value.items() if k != "rows"}]
        else:
            data["simulations"].append(
                {
                    "job_id": identity,
                    "result_path": str(output.relative_to(workspace.root)),
                    "sha256": value["sha256"],
                    "ok": value["ok"],
                }
            )
        from pack_builder.studio_workflow import store

        store(meta, data, expected)
        job.update(status="complete" if value["ok"] else "failed", result_path=str(output.relative_to(workspace.root)))
    except Exception as error:
        job.update(status="failed", error=f"{type(error).__name__}: {str(error)[:500]}")
    atomic_write(path, json.dumps(job, indent=2) + "\n")
    return 0 if job["status"] == "complete" else 1
