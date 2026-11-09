"""Publishing through the signing service (AUT-33, D34).

A publish request names a commit, a pack id, a version and a channel. The service exports that commit's content into a
clean directory, rebuilds the pack there, and re-runs every gate itself, whatever CI said: the build's verification,
licence and held-out exclusion, every record's pipeline history (CUR-19), the evaluation bank (CUR-22), the coverage
target for the pilot and stable channels, and the requester (a curriculum lead). There is no human approval (D65).
Only then does it move the banked records to `published`, sign with the content key and return the signed pack, a
signed diff from the previous release and a release record. The studio, the CLI and CI never hold a key.

Against the previous release it also checks the version (CUR-09: the bump the change needs, never a smaller one) and
the KC migrations (CUR-10), signs a row-level diff, and records the pack and diff sizes against their budgets
(CUR-34). For the pilot and stable channels it also requires a signed content safety review covering the release's
sample (CUR-27, `signing_service.safety_review`). Every gate of the build is re-run here, so a pack whose worked
solutions or keys fail tool verification is never signed (EVL-25). It signs with a content intermediate of the key
hierarchy (SEC-12): the test hierarchy by default, the production key by reference to its HSM
(`hierarchy.production_signer`).
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from agent_core.config import Settings
from agent_core.config.trust import Purpose
from agent_core.contracts import errors as contract_errors
from agent_core.store import pack_diff
from agent_core.store.channels import CHANNELS
from agent_core.store.pack_channels import KIT_ARTEFACT, KIT_CONTRACT, KIT_FILE, KIT_FORMAT, kit_entry
from agent_core.store.pack_diff import diff_bytes, make_diff
from agent_core.store.packs import read_pack
from pack_builder import coverage as cov
from pack_builder import safety_review as review
from pack_builder import scan, versioning
from pack_builder import source as tree
from pack_builder.accounts import Accounts, AuditLog, now_iso, request_mac
from pack_builder.build import REVIEWED, BuildError, build, load_source
from pack_builder.heldout import HeldOut

from signing_service.hierarchy import Signer, dev_signer
from signing_service.safety_review import Reviewers, load_review, review_problems, trusted_reviewers

ACTOR = "signing_service"


class Refused(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class Release:
    record: dict[str, Any]
    pack_path: Path
    diff_path: Path | None  # None for a pack's first release


# What a publish request may hold (AUT-33): a full or abbreviated commit id only (never an option such as
# `--remote=`), ids and versions that are plain names, and a pack folder inside the content tree.
COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
VERSION = re.compile(r"^\d{1,6}\.\d{1,6}\.\d{1,6}$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CONTENT_ROOT = PurePosixPath("content/curriculum")


def request_problems(request: Any) -> list[str]:
    """Why a publish request is not well formed, before anything it names reaches git or a path."""
    if not isinstance(request, dict):
        return ["the publish request is not a JSON object"]
    problems = []
    if not COMMIT.fullmatch(str(request.get("commit", ""))):
        problems.append("commit must be a commit id (hexadecimal)")
    pack_id = str(request.get("pack_id", ""))
    if not NAME.fullmatch(pack_id) or ".." in pack_id:
        problems.append("pack_id is not a pack id")
    if not VERSION.fullmatch(str(request.get("version", ""))):
        problems.append("version is not a version number")
    if request.get("channel") not in CHANNELS:
        problems.append(f"channel must be one of {', '.join(CHANNELS)}")
    if not REQUEST_ID.fullmatch(str(request.get("request_id", ""))):
        problems.append("request_id is not a request id")
    pack_dir = request.get("pack_dir")
    folder = PurePosixPath(str(pack_dir)) if isinstance(pack_dir, str) and "\\" not in pack_dir else None
    if folder is None or folder.is_absolute() or ".." in folder.parts or not folder.is_relative_to(CONTENT_ROOT):
        problems.append("pack_dir must be a folder under content/curriculum")
    return problems


def export_commit(repo: Path, commit: str, target: Path) -> None:
    """The commit's content tree and held-out hashes, exported into a clean directory (no working-copy state)."""
    if not COMMIT.fullmatch(commit):
        raise Refused(["commit must be a commit id (hexadecimal)"])
    optional = subprocess.run(
        ["git", "ls-tree", "--name-only", commit, "--", "evals/held-out", "reviews/studio"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    archive = subprocess.run(
        ["git", "archive", "--format=tar", commit, "--", "content/curriculum", *optional],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
        bundle.extractall(target, filter="data")


def _pipeline_problems(pack_dir: Path) -> list[str]:
    """Every pack record has passed the automated gates and entered the evaluation bank (CUR-19, CUR-22)."""
    problems = []
    _, found = tree.read_pack(pack_dir)
    for table in REVIEWED:
        for located in found[table]:
            record = located.record
            status = (record.get("review") or {}).get("status")
            if status == "verified":
                problems.append(f"{record['id']}: verified but not yet in the evaluation bank (CUR-22)")
            elif status not in ("in_eval_bank", "published"):
                problems.append(f"{record['id']}: {status}; it has not passed the automated gates (CUR-19)")
    return problems


def _requester_problems(request: dict[str, Any], accounts: Accounts) -> list[str]:
    requester = accounts.by_rev_id(str(request.get("requested_by", {}).get("role_id")))
    if requester is None or "curriculum_lead" not in requester.roles:
        return ["the publish request is not from a curriculum lead (AUT-01)"]
    return []


def as_released(tables: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """The tables as this release publishes them: every banked record becomes published as the service signs it, a
    new version of an edited record (CUR-19) included."""
    return {table: [{**r, "review": {**r["review"], "status": "published"}}
                    if table in REVIEWED and (r.get("review") or {}).get("status") == "in_eval_bank" else r
                    for r in records] for table, records in tables.items()}  # fmt: skip


def coverage_problems(pack: dict[str, Any], tables: dict[str, list[dict[str, Any]]], settings: Settings) -> list[str]:
    """The coverage target (CUR-31) of the pack this release publishes: counted after its banked records are
    published, so re-releasing banked edits of published records is not read as lost coverage (AUT-33)."""
    return [f"coverage: {g}" for g in cov.report(pack, as_released(tables), settings)["gaps"]]


def _mark_published(pack_dir: Path) -> list[str]:
    """The banked records become published as the service signs them; returns their ids."""
    published = []
    _, found = tree.read_pack(pack_dir)
    for table in REVIEWED:
        for located in found[table]:
            record = located.record
            if (record.get("review") or {}).get("status") != "in_eval_bank":
                continue
            authoring = dict(record.get("authoring") or {})
            authoring["history"] = [*(authoring.get("history") or []), "published"]
            tree.save_record(pack_dir, table, {**record, "review": {**record["review"], "status": "published"},
                                               "authoring": authoring})  # fmt: skip
            published.append(record["id"])
    return published


def _released_tables(releases: Path, previous: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]] | None:
    """The previous release's tables, from the signed pack kept with its release record."""
    if previous is None or not previous.get("pack_file") or not (releases / previous["pack_file"]).is_file():
        return None
    return read_pack(releases / previous["pack_file"])[1]


def handle(request: dict[str, Any], repo: Path, out_dir: Path, *, authoring_home: Path | None = None,
           settings: Settings | None = None, signer: Signer | None = None,
           reviewers: Reviewers | None = None) -> Release:  # fmt: skip
    """`reviewers` are the safety reviewers whose signature counts (CUR-27); by default the trusted list."""
    settings = settings or Settings()
    reviewers = trusted_reviewers() if reviewers is None else reviewers
    signer = signer or dev_signer(Purpose.CONTENT)
    if signer.purpose is not Purpose.CONTENT:
        raise Refused([f"{signer.key_id} is a {signer.purpose.value} key; packs are signed with a content key"])
    audit = AuditLog(authoring_home)
    accounts = Accounts(authoring_home)
    malformed = request_problems(request)
    if malformed:
        audit.append(ACTOR, "publish", "refused", str(request.get("pack_id", ""))[:96], "; ".join(malformed)[:500])
        raise Refused(malformed)
    work = Path(tempfile.mkdtemp(prefix="signing-"))
    try:
        export_commit(repo, str(request["commit"]), work)
        pack_dir = (work / str(request["pack_dir"])).resolve()
        if not pack_dir.is_relative_to((work / CONTENT_ROOT).resolve()):  # a symlink in the tree leads out
            raise Refused(["pack_dir must be a folder under content/curriculum"])
        source = load_source(pack_dir)
        problems = []
        if source.pack["pack_id"] != request["pack_id"] or source.pack["version"] != request["version"]:
            problems.append(f"the commit holds {source.pack['pack_id']} {source.pack['version']}, not the requested "
                            f"{request['pack_id']} {request['version']}")  # fmt: skip
        problems += _requester_problems(request, accounts)
        problems += _pipeline_problems(pack_dir)
        from pack_builder.studio_workflow import comment_blockers

        problems += comment_blockers(work, str(source.pack["pack_id"]))
        if request["channel"] != "dev":
            problems += coverage_problems(source.pack, source.tables, settings)
        releases = out_dir / "releases" / request["pack_id"]
        previous_path = releases / "latest.json"
        previous = json.loads(previous_path.read_text()) if previous_path.is_file() else None
        old_tables = _released_tables(releases, previous)
        lists = scan.lists_for(pack_dir)
        safety: dict[str, Any] = {"required": request["channel"] != "dev"}
        if lists is not None:
            wanted = review.sample(source.pack, source.tables, old_tables, lists, settings)
            found = load_review(review.review_path(work / "content" / "curriculum", str(request["pack_id"]),
                                                   str(request["version"])))  # fmt: skip
            gaps = review_problems(found, wanted, reviewers)
            safety.update(sample=len(wanted.records), sensitive=len(wanted.sensitive), new_records=wanted.new,
                          present=found is not None, signed_off=not gaps)  # fmt: skip
            if safety["required"]:
                problems += gaps
        elif safety["required"]:
            problems.append("the safety review sample cannot be drawn: no content scan lists (CUR-27)")
        if problems:
            audit.append(ACTOR, "publish", "refused", request["pack_id"], "; ".join(problems)[:500])
            raise Refused(problems)
        published = _mark_published(pack_dir)
        if request["channel"] != source.pack["channel"]:
            text = (pack_dir / "pack.yaml").read_text().replace(f"channel: {source.pack['channel']}",
                                                                f"channel: {request['channel']}")  # fmt: skip
            (pack_dir / "pack.yaml").write_text(text)
        held = HeldOut.load(work / "evals" / "held-out") if (work / "evals" / "held-out").is_dir() else HeldOut()
        signed_at = now_iso()

        def sign_manifest(manifest: dict[str, Any]) -> dict[str, Any]:  # the signing time is the pack's created_at
            return signer.sign("content_pack", {**manifest, "created_at": signed_at}, now=signed_at)

        try:
            built = build(pack_dir, work / "build", held_out=held, settings=settings, sign_with=sign_manifest)
        except BuildError as error:
            audit.append(ACTOR, "publish", "refused", request["pack_id"], "; ".join(error.problems)[:500])
            raise Refused(error.problems) from error
        releases.mkdir(parents=True, exist_ok=True)
        _, tables = read_pack(built.pack_path)
        diff_path: Path | None = None
        diff_size: int | None = None
        changes: dict[str, Any] = {}
        if previous is not None and old_tables is not None:
            semantic = versioning.semantic_diff(old_tables, tables)
            changes = semantic.summary()
            problems = versioning.version_problems(previous["manifest"], built.manifest, semantic)
            problems += versioning.migration_problems(tables, str(built.manifest["version"]), old_tables)
            if problems:
                audit.append(ACTOR, "publish", "refused", request["pack_id"], "; ".join(problems)[:500])
                raise Refused(problems)
            diff = make_diff(previous["manifest"], old_tables, built.manifest, tables, signed_at)
            signed_diff = signer.sign(pack_diff.ARTEFACT, diff, now=signed_at)
            diff_path = releases / f"{request['version']}.diff.json"
            diff_path.write_bytes(diff_bytes(signed_diff))
            diff_size = diff_path.stat().st_size
        final = out_dir / "build"  # only a release that passed every check replaces earlier build output
        final.mkdir(parents=True, exist_ok=True)
        for made in (built.pack_path, built.json_path, built.credits_path, built.report_path):
            shutil.copyfile(made, final / made.name)
        pack_path = final / built.pack_path.name
        shutil.copyfile(built.pack_path, releases / f"{request['version']}.pack")
        size = versioning.size_report(built.pack_path.stat().st_size, diff_size, tables, settings)
        record = {"request_id": request["request_id"], "commit": request["commit"], "pack_id": request["pack_id"],
                  "version": request["version"], "channel": request["channel"], "key_id": signer.key_id,
                  "pack_sha256": hashlib.sha256(built.pack_path.read_bytes()).hexdigest(), "signed_at": signed_at,
                  "published_records": published, "manifest": built.manifest, "changes": changes,
                  "size": size, "pack_file": f"{request['version']}.pack", "safety_review": safety}  # fmt: skip
        (releases / f"{request['version']}.release.json").write_text(json.dumps(record, indent=1, sort_keys=True))
        previous_path.write_text(json.dumps(record, indent=1, sort_keys=True))
        if size["over_budget"]:  # CUR-34 is a "should": recorded for the release review, not refused
            audit.append(ACTOR, "publish", "over_budget", request["pack_id"], "; ".join(size["over_budget"])[:500])
        audit.append(ACTOR, "publish", "signed", request["pack_id"], f"{request['version']} {request['channel']}")
        return Release(record, pack_path, diff_path)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def authentication_problems(request: Any, accounts: Accounts, seen: set[str]) -> list[str]:
    """An inbox request must carry the MAC its named requester's account makes (AUT-33), and a request id is
    handled once: a copied or replayed request is refused."""
    if not isinstance(request, dict):
        return ["the publish request is not a JSON object"]
    requester = accounts.by_rev_id(str((request.get("requested_by") or {}).get("role_id")))
    mac = request.get("mac")
    if requester is None or not requester.totp_secret or not isinstance(mac, str):
        return ["the publish request is not authenticated (AUT-33)"]
    if not hmac.compare_digest(mac, request_mac(requester.totp_secret, request)):
        return ["the publish request is not authenticated (AUT-33)"]
    if str(request.get("request_id")) in seen:
        return ["this publish request was handled already"]
    return []


def _handled(inbox: Path) -> set[str]:
    ids = set()
    for folder in (inbox.parent / "done", inbox.parent / "refused"):
        for path in folder.glob("*.json") if folder.is_dir() else []:
            try:
                ids.add(str(json.loads(path.read_text()).get("request_id")))
            except (OSError, ValueError, AttributeError):
                continue
    return ids


def process_inbox(inbox: Path, repo: Path, out_dir: Path, authoring_home: Path | None = None) -> list[dict[str, Any]]:
    """Handle every queued request; each moves to done/ or refused/ with its outcome. Only a well-formed request
    its named requester authenticated, and not handled before, reaches the gates (AUT-33). A request that leaves the
    inbox after it was listed (withdrawn, or taken by another run) is skipped, not refused, and never stops the run."""
    outcomes = []
    accounts = Accounts(authoring_home)
    seen = _handled(inbox)
    for path in sorted(inbox.glob("*.json")):
        try:
            request: Any = json.loads(path.read_text())
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            request = None
        request_id = str(request.get("request_id")) if isinstance(request, dict) else path.stem
        outcome: dict[str, Any]
        try:
            problems = authentication_problems(request, accounts, seen) or request_problems(request)
            if problems:
                AuditLog(authoring_home).append(ACTOR, "publish", "refused", path.stem[:96], "; ".join(problems))
                raise Refused(problems)
            release = handle(request, repo, out_dir, authoring_home=authoring_home)
            outcome = {"request_id": request_id, "status": "signed", "pack": str(release.pack_path)}
            target = inbox.parent / "done"
        except Refused as refusal:
            outcome = {"request_id": request_id, "status": "refused", "problems": refusal.problems}
            target = inbox.parent / "refused"
        seen.add(request_id)
        if not isinstance(request, dict):
            request = {"request_id": request_id}
        target.mkdir(parents=True, exist_ok=True)
        (target / path.name).write_text(json.dumps({**request, "outcome": outcome}, indent=1, sort_keys=True))
        path.unlink(missing_ok=True)
        outcomes.append(outcome)
    return outcomes


def sign_kit(kit_dir: Path, files: list[tuple[str, str]], signer: Signer | None = None,
             revocation_list: dict[str, Any] | None = None, *, kit_id: str = "kit-2026.1.1",
             channel: str = "stable", candidate: bool = False, release_version: str = "1.0.0",
             matrix_version: int = 1) -> dict[str, Any]:  # fmt: skip
    """Write the USB kit's signed `kit.json` (PLT-12, OPS-15): each (path, kind) already in `kit_dir` with its size
    and sha256, the kit's identity (id, channel, candidate or not, release and matrix versions), signed by a content
    key like a pack (TABLE SEC-12a), carrying the current root-signed revocation list (SEC-16). The signed listing
    must match the `ops/usb-kit` contract (D39) or nothing is written."""
    signer = signer or dev_signer(Purpose.CONTENT)
    signed_at = now_iso()
    listing: dict[str, Any] = {"format": KIT_FORMAT, "kit_id": kit_id, "created_at": signed_at, "channel": channel,
                               "candidate": candidate, "release_version": release_version,
                               "matrix_version": matrix_version,
                               "files": [kit_entry(kit_dir, name, kind).model_dump(mode="json")
                                         for name, kind in files]}  # fmt: skip
    if revocation_list is not None:
        listing["revocation_list"] = revocation_list
    signed = signer.sign(KIT_ARTEFACT, listing, now=signed_at)
    problems = contract_errors(KIT_CONTRACT, signed)
    if problems:
        raise ValueError(f"the kit listing does not match its contract: {problems[0]}")
    (kit_dir / KIT_FILE).write_text(json.dumps(signed, indent=1, sort_keys=True) + "\n")
    return signed
