"""The authoring library behind the `aut` CLI and, from M22, the studio (AUT-05): one function per command, each
returning a plain result the CLI prints (`--json`) and the studio renders, so both give identical results.

Every function that changes state checks the actor's role first (AUT-01) and appends to the audit log (AUT-02).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_core.config import Settings

from pack_builder import coverage as cov
from pack_builder import drafting, proposals
from pack_builder import quantities as qty
from pack_builder import source as tree
from pack_builder.accounts import Account, Accounts, AuditLog, authorise, home, now_iso, request_mac
from pack_builder.build import BuildError, build, categorised_problems, default_held_out, load_source
from pack_builder.preview import build_preview
from pack_builder.roles import Forbidden

REPO_ROOT = Path(__file__).resolve().parents[3]
CURRICULUM = REPO_ROOT / "content" / "curriculum"


class EnvironmentProblem(RuntimeError):
    """Something the environment lacks (exit code 3)."""


@dataclass
class Context:
    """Who is acting, where the authoring home is, and the second-factor code if one was given."""

    account: Account | None = None
    otp: str | None = None
    root: Path | None = None  # the authoring home; defaults to JANA_AUTHORING_HOME

    @property
    def audit(self) -> AuditLog:
        return AuditLog(self.root)

    def need(self) -> Account:
        if self.account is None:
            raise Forbidden("no authoring account: set JANA_AUTHOR to your account id (AUT-02)")
        return self.account


def pack_dirs(root: Path | None = None) -> list[Path]:
    root = root if root is not None else CURRICULUM
    return sorted(p.parent for p in root.glob("*/*/pack.yaml"))


def resolve_pack(name: str | Path, root: Path | None = None) -> Path:
    """A pack by directory, by `phy/o` or by pack id (`pack.phy.o`)."""
    root = root if root is not None else CURRICULUM
    path = Path(name)
    if (path / "pack.yaml").is_file():
        return path
    if (root / path / "pack.yaml").is_file():
        return root / path
    for directory in pack_dirs(root):
        if tree.read_pack(directory)[0].get("pack_id") == str(name):
            return directory
    raise FileNotFoundError(f"no pack {name}")


def find_record(record_id: str, root: Path | None = None) -> tuple[Path, str, dict[str, Any]]:
    root = root if root is not None else CURRICULUM
    for directory in pack_dirs(root):
        _, found = tree.read_pack(directory)
        for table, located in found.items():
            for item in located:
                if item.record.get("id") == record_id:
                    return directory, table, item.record
    raise KeyError(f"no record {record_id}")


def _filter(problems: list[str], ids: set[str] | None) -> list[str]:
    if ids is None:
        return problems
    return [p for p in problems if any(p.startswith(f"{i}:") or p.startswith(f"{i} ") for i in ids)]


def _ids_in(paths: list[Path]) -> set[str] | None:
    if not paths:
        return None
    ids: set[str] = set()
    for path in paths:
        data = tree._load(path) if path.suffix == ".yaml" else None
        for record in data if isinstance(data, list) else [data]:
            if isinstance(record, dict) and "id" in record:
                ids.add(str(record["id"]))
    return ids


def check(category: str, pack: Path, paths: list[Path] | None = None) -> dict[str, Any]:
    """validate, verify, lint, qa or scan one pack (optionally only the records in `paths`)."""
    try:
        source = load_source(pack)
    except BuildError as error:
        return {"pack": str(pack), "category": category, "problems": error.problems, "ok": False}
    grouped, verifier = categorised_problems(source, Settings())
    problems = _filter(grouped[category], _ids_in(paths or []))
    result: dict[str, Any] = {"pack": source.pack["pack_id"], "category": category, "problems": problems,
                              "ok": not problems}  # fmt: skip
    if category == "verify":
        result["checks"] = len(verifier.results)
    return result


def coverage(pack: Path, stage: str | None = None) -> dict[str, Any]:
    """The coverage report (CUR-31) for a stage; by default the first stage the subject-level is produced in."""
    source = load_source(pack)
    return cov.report(source.pack, source.tables, Settings(), stage)


def table_hashes(built_manifest: dict[str, Any]) -> dict[str, str]:
    return {t["name"]: t["sha256"] for t in built_manifest["tables"]}


def build_pack(pack: Path, out_dir: Path) -> dict[str, Any]:
    """The deterministic, unsigned build (signing is the signing service's alone, AUT-33)."""
    built = build(pack, out_dir)
    return {"pack": built.manifest["pack_id"], "version": built.manifest["version"], "path": str(built.pack_path),
            "table_hashes": table_hashes(built.manifest), "signed": False}  # fmt: skip


def preview(record_id: str, out_dir: Path) -> dict[str, Any]:
    directory, table, _ = find_record(record_id)
    built = build_preview(directory, out_dir)
    return {
        "record": record_id,
        "table": table,
        "pack": built.manifest["pack_id"],
        "preview_pack": str(built.pack_path),
        "signed": False,
        "note": "a preview pack loads only in the authoring preview; student builds refuse it",
    }


def eval_pack(pack: Path) -> dict[str, Any]:
    """Pack oracles: every item has a programmatic oracle and its key verifies (EVL-02, AUT-32)."""
    source = load_source(pack)
    grouped, _ = categorised_problems(source, Settings())
    cases = []
    for item in source.tables["items"]:
        mine = [p for p in grouped["verify"] if p.startswith(f"{item['id']}:") or p.startswith(f"{item['id']} ")]
        missing = [p["id"] for p in item.get("parts", []) if not p.get("answer")]
        cases.append({"item": item["id"], "passed": not mine and not missing,
                      "problems": mine + [f"part {m} has no oracle" for m in missing]})  # fmt: skip
    return {"pack": source.pack["pack_id"], "cases": cases, "ok": all(c["passed"] for c in cases)}


# --- edits, drafting and the pipeline -----------------------------------------------------------------------------


def _save(directory: Path, table: str, record: dict[str, Any]) -> None:
    tree.save_record(directory, table, record)


BANKED = ("in_eval_bank", "published")


def touch(ctx: Context, record_id: str) -> dict[str, Any]:
    """Register an edit made in the files: the editor becomes an author of the version, and the record returns to
    `drafted` (CUR-19); a banked or published record starts a new version, the published one staying live. A
    published mock paper that uses an edited item is registered as edited with it (`carry_papers`, ASM-15)."""
    account = ctx.need()
    directory, table, record = find_record(record_id)
    authorise(account, "edit_draft", ctx.audit, subject=record_id)
    updated = edited_record(record, account)
    _save(directory, table, updated)
    ctx.audit.append(account.account_id, "edit", "ok", record_id, f"version {updated.get('version')}")
    papers = carry_papers(ctx, directory, {record_id}) if table == "items" else []
    return {
        "record": record_id,
        "version": updated.get("version"),
        "state": "drafted",
        "authors": updated["authoring"]["authors"],
        "papers": papers,
    }


def carry_papers(ctx: Context, directory: Path, item_ids: set[str]) -> list[str]:
    """A published mock paper uses published items only (ASM-15): when an item it uses starts a new version in the
    pipeline, the paper is registered as edited with it, so the paper and its items travel to the next release
    together (CUR-19). Returns the papers edited."""
    _, found = tree.read_pack(directory)
    pending = {str(i.record["id"]) for i in found["items"] if str(i.record["id"]) in item_ids
               and (i.record.get("review") or {}).get("status") != "published"}  # fmt: skip
    papers: list[str] = []
    for located in found["mock_papers"]:
        paper = located.record
        uses = {str(q["item_id"]) for s in paper.get("sections") or [] for q in s.get("questions") or []}
        if (paper.get("review") or {}).get("status") == "published" and uses & pending:
            updated = _edit(ctx, paper, "its items have new versions")
            _save(directory, "mock_papers", updated)
            papers.append(str(updated["id"]))
    return papers


def edited_record(record: dict[str, Any], account: Account) -> dict[str, Any]:
    """Common edit transition for CLI and studio (CUR-19); no file I/O."""
    updated = dict(record)
    status = (record.get("review") or {}).get("status", "drafted")
    authoring = dict(record.get("authoring") or {})
    history = list(authoring.get("history") or ["drafted"])
    if status in BANKED:
        major, minor, *rest = (record.get("version") or "0.1.0").split(".")
        updated["version"] = f"{major}.{minor}.{int(rest[0]) + 1 if rest else 1}"
        authoring["authors"] = [account.rev_id]
    else:
        authoring["authors"] = sorted(set(drafting.authors(record)) | {account.rev_id})
    if status != "drafted":
        history.append("drafted")
    authoring["history"] = history
    updated["review"] = {**(record.get("review") or {}), "status": "drafted", "reviewers": []}
    updated["authoring"] = authoring
    return updated


def draft(ctx: Context, task: str, record_id: str, instruction: str, route: drafting.Route) -> dict[str, Any]:
    account = ctx.need()
    directory, table, record = find_record(record_id)
    authorise(account, "ai_draft", ctx.audit, subject=record_id)
    source = load_source(directory)
    spec = drafting.TASKS.get(task)
    if spec is None:
        raise drafting.DraftUnavailable(f"{task}: not a drafting task (TABLE AUT-17a)")
    related = {str(k) for k in record.get("kc_ids", [])} | {str(record.get("id"))}
    inputs: dict[str, list[Mapping[str, Any]]] = {
        t: [r for r in source.tables[t] if t == "conventions" or str(r.get("id")) in related
            or set(r.get("kc_ids", [])) & related] for t in spec["inputs"]}  # fmt: skip
    updated, used = drafting.run_draft(task, record, instruction, inputs, route,
                                       drafting.forbidden_hashes(REPO_ROOT), ctx.audit, account.account_id,
                                       home() / "egress.jsonl")  # fmt: skip
    if used != "template":
        _save(directory, table, updated)
        updated = _verify_ai(directory, table, record_id)
    else:
        _save(directory, table, updated)
    fields = (updated.get("authoring") or {}).get("fields") or {}
    return {"record": record_id, "task": task, "route": used, "fields": {k: {"status": v.get("status"),
            "accepted": v.get("accepted")} for k, v in fields.items()}}  # fmt: skip


def _verify_ai(directory: Path, table: str, record_id: str) -> dict[str, Any]:
    source = load_source(directory)
    grouped, _ = categorised_problems(source, Settings())
    _, _, record = find_record(record_id, directory.parent.parent)
    updated = drafting.verify_fields(record, grouped["verify"] + grouped["validate"])
    _save(directory, table, updated)
    return updated


def quantities(ctx: Context, pack: Path | None = None, write: bool = False,
               cache: proposals.Cache | None = None) -> dict[str, Any]:  # fmt: skip
    """Item quantities for misconception signatures (CUR-18, ASM-06, VER-12, D90): extracted from each stem and
    verified by tool (`pack_builder.quantities`). With `write`, every item that verifies and does not already carry
    them gets them as a registered edit (CUR-19): it returns to `drafted` (a banked or published record starts a new
    version) and the author then runs `promote` and `bank` as for any edit. A published mock paper that uses such an
    item while its new version is in the pipeline is registered as edited with it (`carry_papers`, ASM-15). Re-running
    changes nothing that already verifies. The misconception library is every pack's, as the marker's is every
    installed pack's.

    The `cache` holds a model's proposals by item id (`pack_builder.proposals`), read only where the item's own
    computation proves them. An item given a quantity a proposal bound records the field's AI provenance (AUT-19):
    the model and prompt, the tools' verification and the account that wrote it."""
    library = {str(r.record["id"]): r.record for d in pack_dirs() for r in tree.read_pack(d)[1]["misconceptions"]}
    proposed = cache.proposals() if cache is not None else {}
    results = []
    for directory in [pack] if pack is not None else pack_dirs():
        manifest, found = tree.read_pack(directory)
        planned = qty.plan([located.record for located in found["items"]], library, proposals=proposed)
        outcomes = {o.item_id: o for o in planned.outcomes}
        written: list[str] = []
        papers: list[str] = []
        if write:
            for located in found["items"]:
                given = planned.write.get(str(located.record["id"]))
                if given is not None:
                    record = {**located.record, "quantities": given}
                    sources = outcomes[str(record["id"])].sources
                    if cache is not None and any("model" in sources.get(n, []) for n in given):
                        entry = cache.data["entries"][str(record["id"])]
                        record = _with_ai_field(record, "quantities", proposals.field_record(
                            entry, str(record["id"]), ctx.need().rev_id))  # fmt: skip
                    updated = _edit(ctx, record, "quantities")
                    _save(directory, "items", updated)
                    written.append(str(updated["id"]))
            papers = carry_papers(ctx, directory, planned.carried)
        results.append({"pack": manifest.get("pack_id"), "written": written, "papers": papers, **planned.report()})
    return {"results": results}


def propose_quantities(
    ctx: Context,
    cache: proposals.Cache,
    transport: proposals.Transport,
    model: proposals.Model,
    pack: Path | None = None,
) -> dict[str, Any]:
    """Ask a model for the quantities no reading of an item binds (`pack_builder.proposals`), within the budget, for
    every item whose listed signatures wait only for them; the answers go to the cache, and `quantities` then reads
    them. Nothing is written to any record here."""
    account = ctx.need()
    authorise(account, "ai_draft", ctx.audit, subject=str(pack or "all packs"))
    library = {str(r.record["id"]): r.record for d in pack_dirs() for r in tree.read_pack(d)[1]["misconceptions"]}
    settings = Settings()
    wanted: list[tuple[Mapping[str, Any], list[str]]] = []
    for directory in [pack] if pack is not None else pack_dirs():
        items = [located.record for located in tree.read_pack(directory)[1]["items"]]
        planned = qty.plan(items, library, settings, proposals=cache.proposals())
        by_id = {str(i["id"]): i for i in items}
        for outcome in planned.outcomes:
            if outcome.item_id in planned.carried or outcome.item_id in planned.blocked:
                continue
            symbols = qty.unbound(outcome)
            if symbols:
                wanted.append((by_id[outcome.item_id], symbols))
    run = proposals.propose(wanted, cache, model, transport, drafting.forbidden_hashes(REPO_ROOT), ctx.audit,
                            account.account_id, settings, workers=settings.quantity_proposal_workers)  # fmt: skip
    return {"candidates": len(wanted), "model": model.model_id,
            **run.report(settings.quantity_proposal_budget_usd)}  # fmt: skip


def _with_ai_field(record: dict[str, Any], key: str, field: Mapping[str, Any]) -> dict[str, Any]:
    """The record with `key` recorded as AI content in its authoring fields (AUT-19), and its pack provenance saying
    it was drafted with AI and naming a model when it did not already (the field record names this one)."""
    authoring = dict(record.get("authoring") or {})
    authoring["fields"] = {**(authoring.get("fields") or {}), key: dict(field)}
    updated = {**record, "authoring": authoring}
    provenance = dict(record.get("provenance") or {})
    if not (provenance.get("drafted_with_ai") and provenance.get("ai_model")):
        updated["provenance"] = {**provenance, "drafted_with_ai": True, "ai_model": field.get("model")}
    return updated


def _edit(ctx: Context, record: dict[str, Any], why: str) -> dict[str, Any]:
    """The registered edit of `touch` for a record changed in memory, audited with why."""
    account = ctx.need()
    authorise(account, "edit_draft", ctx.audit, subject=str(record["id"]))
    updated = edited_record(record, account)
    ctx.audit.append(account.account_id, "edit", "ok", str(updated["id"]), f"version {updated.get('version')}: {why}")
    return updated


def accept(ctx: Context, record_id: str, fields: list[str]) -> dict[str, Any]:
    account = ctx.need()
    directory, table, record = find_record(record_id)
    authorise(account, "edit_draft", ctx.audit, subject=record_id)
    record = _verify_ai(directory, table, record_id)
    for key in fields:
        record = drafting.accept(record, key, account.rev_id)
    _save(directory, table, record)
    ctx.audit.append(account.account_id, "accept_ai", "ok", record_id, ",".join(fields))
    return {"record": record_id, "accepted": fields, "blockers": drafting.submit_blockers(record)}


# Every table whose records pass through the pipeline, mock papers included (ASM-15): the signing service publishes a
# pack only when all of them are banked (CUR-22), so promote, bank and record-release move all of them.
REVIEWED_TABLES = ("items", "worked_examples", "misconceptions", "lessons", "item_templates", "mock_papers")


def promote(ctx: Context, pack: Path, record_ids: list[str] | None = None) -> dict[str, Any]:
    """Move drafted records that pass every automated gate to `verified` (CUR-19 as amended by D65): validation, tool
    verification, the licence gate and lint, and no AI field left failed or unaccepted. There is no approval step."""
    account = ctx.need()
    authorise(account, "edit_draft", ctx.audit, subject=str(pack))
    grouped, _ = categorised_problems(load_source(pack), Settings())
    problems = grouped["validate"] + grouped["verify"] + grouped["lint"] + grouped["qa"] + grouped["scan"]
    _, found = tree.read_pack(pack)
    promoted: list[str] = []
    held: list[dict[str, Any]] = []
    for table in REVIEWED_TABLES:
        for located in found[table]:
            record = located.record
            if record_ids is not None and record["id"] not in record_ids:
                continue
            if (record.get("review") or {}).get("status", "drafted") != "drafted":
                continue
            blockers = drafting.submit_blockers(record) + _filter(problems, {record["id"]})
            if blockers:
                held.append({"record": record["id"], "problems": blockers})
                continue
            authoring = dict(record.get("authoring") or {})
            authoring["history"] = [*(authoring.get("history") or ["drafted"]), "verified"]
            _save(pack, table, {**record, "review": {**(record.get("review") or {}), "status": "verified",
                                                      "reviewers": []}, "authoring": authoring})  # fmt: skip
            promoted.append(record["id"])
    ctx.audit.append(account.account_id, "promote", "ok", str(pack), f"{len(promoted)} verified, {len(held)} held")
    return {"pack": str(pack), "verified": promoted, "held": held, "ok": not held}


# --- publishing ---------------------------------------------------------------------------------------------------


def inbox(root: Path | None = None) -> Path:
    return (root or home()) / "signing" / "inbox"


def publish(ctx: Context, pack: Path, channel: str, commit: str | None = None) -> dict[str, Any]:
    """A publish request to the signing service (AUT-33): it names a commit, pack id, version and channel. The CLI
    never signs; it queues the request in the service's inbox."""
    account = ctx.need()
    source = load_source(pack)
    authorise(account, "request_publication", ctx.audit, code=ctx.otp, subject=source.pack["pack_id"])
    from pack_builder.studio_workflow import comment_blockers

    # The CLI shares Studio's comment gate; comments are exported to the signing service, never into a pack.
    blockers = comment_blockers(pack.resolve().parents[3], str(source.pack["pack_id"]))
    if blockers:
        raise ValueError("; ".join(blockers))
    commit = commit or git_head(pack)
    request = {"request_id": f"pubreq_{secrets.token_hex(8)}", "commit": commit, "pack_id": source.pack["pack_id"],
               "pack_dir": git_relative(pack),
               "version": source.pack["version"], "channel": channel, "requested_at": now_iso(),
               "requested_by": {"role_id": account.rev_id, "pub_id": account.pub_id}}  # fmt: skip
    request["mac"] = request_mac(account.totp_secret, request)  # the service accepts only its requester's (AUT-33)
    path = inbox(ctx.root) / f"{request['request_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(request, indent=1, sort_keys=True) + "\n")
    ctx.audit.append(account.account_id, "publish_request", "queued", source.pack["pack_id"], request["request_id"])
    return {"request": request, "queued": str(path)}


def record_release(ctx: Context, release_path: Path) -> dict[str, Any]:
    """Carry a signed release back into the source tree (AUT-03, CUR-19): the records the signing service published
    move from in_eval_bank to published. The release must be of a commit in this repository's history, and only the
    records it names change."""
    account = ctx.need()
    release = json.loads(release_path.read_text())
    authorise(account, "request_publication", ctx.audit, code=ctx.otp, subject=str(release["pack_id"]))
    known = subprocess.run(["git", "merge-base", "--is-ancestor", str(release["commit"]), "HEAD"], cwd=REPO_ROOT,
                           capture_output=True, check=False)  # fmt: skip
    if known.returncode != 0:
        raise EnvironmentProblem("the release's commit is not in this repository's history")
    names = set(release["published_records"])
    marked: list[str] = []
    for pack in pack_dirs():
        _, found = tree.read_pack(pack)
        for table in REVIEWED_TABLES:
            for located in found.get(table, []):
                record = located.record
                if record["id"] in names and (record.get("review") or {}).get("status") == "in_eval_bank":
                    authoring = dict(record.get("authoring") or {})
                    authoring["history"] = [*(authoring.get("history") or []), "published"]
                    _save(pack, table, {**record, "review": {**record["review"], "status": "published"},
                                        "authoring": authoring})  # fmt: skip
                    marked.append(str(record["id"]))
    ctx.audit.append(account.account_id, "record_release", "ok", str(release["pack_id"]),
                     f"{release['version']} {release['channel']} {len(marked)} records")  # fmt: skip
    return {"published": sorted(marked), "release": release["request_id"]}


def git_relative(path: Path) -> str:
    """`path` relative to the top of its git repository, as the signing service finds it in the exported commit
    (never an absolute path, which would point outside the export, AUT-33)."""
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True,
                             check=True).stdout.strip()  # fmt: skip
    except (OSError, subprocess.CalledProcessError) as error:
        raise EnvironmentProblem("git is needed to name the pack folder of a publish request") from error
    return path.resolve().relative_to(Path(top).resolve()).as_posix()


def git_head(path: Path) -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True,
                              check=True).stdout.strip()  # fmt: skip
    except (OSError, subprocess.CalledProcessError) as error:
        raise EnvironmentProblem("git is needed to name the commit of a publish request") from error


# --- CI ------------------------------------------------------------------------------------------------------------


def reproducible(pack: Path) -> dict[str, Any]:
    """Build twice in separate places and compare every table hash (AUT-03)."""
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        copy = Path(b) / "src" / pack.parent.name / pack.name
        shutil.copytree(pack, copy)
        if tree.ref_dir(pack).is_dir():
            shutil.copytree(tree.ref_dir(pack), tree.ref_dir(copy))
        try:
            first = build(pack, Path(a) / "out", held_out=default_held_out(pack))
            second = build(copy, Path(b) / "out", held_out=default_held_out(pack))
        except BuildError as error:
            return {"pack": str(pack), "identical_table_hashes": False, "identical_pack_bytes": False,
                    "problems": error.problems}  # fmt: skip
        same = table_hashes(first.manifest) == table_hashes(second.manifest)
        bytes_same = hashlib.sha256(first.pack_path.read_bytes()).hexdigest() == hashlib.sha256(
            second.pack_path.read_bytes()).hexdigest()  # fmt: skip
    return {"pack": first.manifest["pack_id"], "identical_table_hashes": same, "identical_pack_bytes": bytes_same}


def held_out_guard() -> list[str]:
    """CI development jobs never hold the release key (EVL-18, AUT-06)."""
    return ["the release key for held-out items is set in a development CI job"] if os.environ.get(
        "JANA_HELD_OUT_KEY") else []  # fmt: skip


GATES = ("validate", "verify", "lint", "qa", "scan")  # the record gates CI and the build run (AUT-06, CUR-25, PRV-17)


def ci(base: str | None) -> dict[str, Any]:
    """The content CI gates of AUT-06, for every pack."""
    results: dict[str, Any] = {"packs": [], "held_out_guard": held_out_guard(), "base": base}
    for directory in pack_dirs():
        entry = {category: check(category, directory)["problems"] for category in GATES}
        entry["pack"] = str(directory.relative_to(REPO_ROOT))
        entry["coverage"] = coverage(directory)["gaps"]
        entry["reproducible"] = reproducible(directory)
        results["packs"].append(entry)
    failed = bool(results["held_out_guard"]) or any(
        any(p[g] for g in GATES) or not p["reproducible"]["identical_table_hashes"]
        for p in results["packs"])  # fmt: skip
    results["ok"] = not failed
    return results


def lint_skills() -> dict[str, Any]:
    """The skill linter and injection scan (SKL-06, SKL-22) of the repository tooling, run over skills/."""
    try:
        from jana_tools.skills import check as skill_check
    except ImportError as error:  # the authoring toolchain without the repository tooling
        raise EnvironmentProblem("the skill linter (jana_tools) is not installed") from error
    problems = skill_check()
    return {"pack": "skills", "category": "lint", "problems": problems, "ok": not problems}


def create_account(ctx: Context, account_id: str, name: str, email: str, roles: list[str],
                   publisher: bool = False) -> dict[str, Any]:  # fmt: skip
    """The first account may be created freely (bootstrap); every later one by a curriculum lead with a second
    factor."""
    accounts = Accounts(ctx.root)
    if accounts.all():
        actor = ctx.need()
        if "curriculum_lead" not in actor.roles:
            ctx.audit.append(actor.account_id, "create_account", "forbidden", account_id, "not a curriculum lead")
            raise Forbidden("only a curriculum lead creates authoring accounts")
        authorise(actor, "request_publication", ctx.audit, code=ctx.otp, subject=account_id)
    created = accounts.create(account_id, name, email, roles, pub=publisher)
    ctx.audit.append(ctx.account.account_id if ctx.account else account_id, "create_account", "ok", account_id,
                     ",".join(created.roles))  # fmt: skip
    return {"account_id": created.account_id, "rev_id": created.rev_id, "pub_id": created.pub_id,
            "roles": list(created.roles), "totp_secret": created.totp_secret,
            "note": "add the secret to an authenticator app; it is shown once"}  # fmt: skip


# --- evaluation bank first (CUR-22) --------------------------------------------------------------------------------


def public_evaluation_items(pack: Path) -> set[str]:
    """An item already named in a public eval cannot become an unseen held-out item."""
    root = pack.resolve().parents[3]
    identities: set[str] = set()
    pattern = re.compile(r"\bitm\.(?:mth|phy|chm|bio|acc)\.(?:o|a)\.[a-z0-9_]+\.[0-9]{4}\b")
    for folder in ("evals/marking", "evals/scenarios", "skills"):
        directory = root / folder
        if directory.is_symlink():
            raise ValueError("Public evaluation inputs must not follow symbolic links")
        for path in directory.rglob("*"):
            if path.suffix not in (".json", ".jsonl", ".yaml", ".md") or not path.is_file():
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("Unsafe public evaluation input")
            identities.update(pattern.findall(path.read_text()))
    return identities


def bank(
    ctx: Context,
    pack: Path,
    held_out_share: float | None = None,
    suite: str = "answer_oracles",
    held_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Move verified records into the evaluation bank (CUR-22). A share of each KC's verified items (key
    `eval_held_out_share`, stratified so every KC is represented) is sealed into the held-out store and leaves the
    source tree; the rest enter the bank and may then be published. Sealing needs the release key.

    By default the held-out items are a hash-ordered share of each KC. An author who wrote held-out items in their own
    wording names them instead (`held_ids`): each KC must still reach the share, and no banked record may repeat a
    held-out item's wording, or the bank refuses.

    A new version of a record that was banked or published before (an edit, CUR-19) goes back to the bank and is
    never held out: its earlier version has been in the bank and may have shipped, so it could not be an unseen
    evaluation item (CUR-22). The share is drawn from records entering the bank for the first time."""
    from pack_builder import heldout

    account = ctx.need()
    authorise(account, "request_publication", ctx.audit, code=ctx.otp, subject=str(pack))
    share = Settings().eval_held_out_share if held_out_share is None else held_out_share
    _, found = tree.read_pack(pack)
    approved = {t: [i.record for i in found[t] if (i.record.get("review") or {}).get("status") == "verified"]
                for t in REVIEWED_TABLES}  # fmt: skip
    seen = {str(i["id"]) for i in approved["items"]
            if set((i.get("authoring") or {}).get("history") or []) & set(BANKED)}  # fmt: skip
    by_kc: dict[str, list[dict[str, Any]]] = {}
    for item in approved["items"]:
        if str(item["id"]) not in seen:
            by_kc.setdefault(str((item.get("kc_ids") or ["none"])[0]), []).append(item)
    held: list[dict[str, Any]] = []
    exposed = (
        public_evaluation_items(pack)
        if held_ids or any(round(share * len(items)) for items in by_kc.values())
        else set()
    )
    if held_ids is None:
        for items in by_kc.values():
            ordered = sorted(items, key=lambda r: hashlib.sha256(str(r["id"]).encode()).hexdigest())
            needed = round(share * len(ordered))
            eligible = [item for item in ordered if item["id"] not in exposed]
            if len(eligible) < needed:
                raise ValueError(
                    "Not enough unseen items for the held-out share; public evaluation items cannot be sealed"
                )
            held += eligible[:needed]
    else:
        named = set(held_ids)
        unknown = named - {str(i["id"]) for i in approved["items"]}
        if unknown:
            raise ValueError(f"not verified items of this pack: {', '.join(sorted(unknown))}")
        if named & seen:
            raise ValueError(f"banked or published before, so never held out: {', '.join(sorted(named & seen))}")
        if named & exposed:
            raise ValueError("Items already used in public evaluations cannot become held out")
        held = [i for i in approved["items"] if i["id"] in named]
        problems = [f"{kc}: {sum(i['id'] in named for i in items)} held out of {len(items)}, needs "
                    f"{round(share * len(items))}" for kc, items in sorted(by_kc.items())
                    if sum(i["id"] in named for i in items) < round(share * len(items))]  # fmt: skip
        kept = {t: [r for r in records if r["id"] not in named] for t, records in approved.items()}
        problems += heldout.HeldOut(frozenset(heldout.item_hashes(held))).conflicts(kept)
        if problems:
            raise ValueError("; ".join(problems[:20]))
    sealed = {str(r["id"]) for r in held}
    if held:
        key = os.environ.get(heldout.KEY_ENV)
        if not key:
            raise EnvironmentProblem(f"sealing held-out items needs the release key ({heldout.KEY_ENV})")
        store = REPO_ROOT / "evals" / "held-out"
        existing = heldout.open_sealed(suite, key, store)
        heldout.seal(existing + held, suite, key, store)
    moved: dict[str, list[str]] = {"held_out": sorted(sealed), "in_eval_bank": []}
    for table, records in approved.items():
        for record in records:
            target = "held_out" if record["id"] in sealed else "in_eval_bank"
            authoring = dict(record.get("authoring") or {})
            authoring["history"] = [*(authoring.get("history") or []), target]
            updated = {**record, "review": {**record["review"], "status": target}, "authoring": authoring}
            if target == "held_out":
                tree.record_path(pack, table, record).unlink()  # the plaintext leaves the tree
            else:
                _save(pack, table, updated)
                moved["in_eval_bank"].append(record["id"])
    ctx.audit.append(account.account_id, "bank", "ok", str(pack), json.dumps(moved)[:500])
    return moved
