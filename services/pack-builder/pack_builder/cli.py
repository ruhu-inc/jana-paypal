"""`aut`: the headless authoring command line (AUT-05), a thin front end over `pack_builder.api`.

It never prompts. `--json` prints the library's result as JSON. Exit codes: 0 success; 1 a validation, verification
or permission failure (`forbidden`); 2 a usage error; 3 an environment error; 4 a network needed and unavailable
(only `draft --cloud` and `publish --process` can need one).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agent_core.config import Settings
from agent_core.store.catalogue import STAGES

from pack_builder import api, drafting, proposals
from pack_builder.accounts import AUTHOR_ENV, OTP_ENV, Accounts, authorise, chain_errors
from pack_builder.analytics import IntakeRefused
from pack_builder.problem_inbox import ReportRefused
from pack_builder.roles import Forbidden

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_ENV, EXIT_NETWORK = 0, 1, 2, 3, 4
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _context(args: argparse.Namespace) -> api.Context:
    accounts = Accounts()
    author = os.environ.get(AUTHOR_ENV)
    account = accounts.get(author) if author else None
    return api.Context(account=account, otp=getattr(args, "otp", None) or os.environ.get(OTP_ENV))


def _route(args: argparse.Namespace) -> drafting.Route:
    """AUT-17: cloud only with online drafting on and a decided provider (D7, open: none in S1); else the local
    endpoint the author set (loopback only); else the template."""
    if getattr(args, "cloud", False):
        raise ConnectionError("cloud drafting needs a decided provider (D7); none is configured")
    url = os.environ.get("JANA_DRAFT_URL")
    model = os.environ.get("JANA_DRAFT_MODEL")
    local = None
    if url and model:
        if (urlparse(url).hostname or "") not in LOOPBACK:
            raise RuntimeError("JANA_DRAFT_URL must be a model on this machine (loopback); cloud drafting is D7")
        local = drafting.local_route(url, model)
    return drafting.choose_route(online_drafting=False, cloud=None, local=local)


def _paths(values: list[str]) -> list[Path]:
    return [Path(v) for v in values]


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    command = args.command
    if command == "studio":
        from pack_builder.studio import Workspace, handle

        result = handle(Workspace(Path(args.workspace)), json.load(sys.stdin))
        return result, EXIT_FAIL if "error" in result else EXIT_OK
    if command in api.GATES:
        results = [api.check(command, d, _paths(args.paths)) for d in _packs(args)]
        if command == "lint" and not args.paths and not args.pack:
            results.append(api.lint_skills())
        return {"command": command, "results": results}, EXIT_OK if all(r["ok"] for r in results) else EXIT_FAIL
    if command == "coverage":
        reports = [api.coverage(d, args.stage) for d in _packs(args)]
        return {"command": command, "results": reports}, EXIT_OK
    if command == "build":
        out = Path(args.out or tempfile.mkdtemp(prefix="aut-build-"))
        return {"command": command, "result": api.build_pack(api.resolve_pack(args.pack), out)}, EXIT_OK
    if command == "preview":
        out = Path(args.out or tempfile.mkdtemp(prefix="aut-preview-"))
        return {"command": command, "result": api.preview(args.record, out)}, EXIT_OK
    if command == "eval":
        results = [api.eval_pack(d) for d in _packs(args)]
        return {"command": command, "results": results}, EXIT_OK if all(r["ok"] for r in results) else EXIT_FAIL
    ctx = _context(args)
    if command == "draft":
        return {"command": command, "result": api.draft(ctx, args.task, args.record, args.instruction or "",
                                                         _route(args))}, EXIT_OK  # fmt: skip
    if command == "accept":
        result = api.accept(ctx, args.record, args.fields)
        return {"command": command, "result": result}, EXIT_OK
    if command == "edit":
        return {"command": command, "result": api.touch(ctx, args.record)}, EXIT_OK
    if command == "quantities":
        pack = api.resolve_pack(args.pack) if args.pack else None
        cache = proposals.Cache(Path(args.proposals))
        proposed: dict[str, Any] | None = None
        if args.propose:
            model = (
                proposals.gateway_model(Path(args.gateway_config)) if args.gateway_config else proposals.gateway_model()
            )
            key = proposals.api_key(Path(args.env_file) if args.env_file else None)
            transport = proposals.gemini_transport(model, key, Settings().quantity_proposal_timeout_s)
            proposed = api.propose_quantities(ctx, cache, transport, model, pack)
        found = api.quantities(ctx, pack, args.write, cache)
        if args.report:
            Path(args.report).write_text(json.dumps(found, indent=1, sort_keys=True, default=str) + "\n")
        brief = [{k: v for k, v in r.items() if k not in ("not_live", "evidence", "write")} for r in found["results"]]
        return {"command": command, "result": brief, **({"proposals": proposed} if proposed else {})}, EXIT_OK
    if command == "promote":
        result = api.promote(ctx, api.resolve_pack(args.pack), args.records or None)
        return {"command": command, "result": result}, EXIT_OK if result["ok"] else EXIT_FAIL
    if command == "bank":
        result = api.bank(ctx, api.resolve_pack(args.pack), held_ids=args.held or None)
        return {"command": command, "result": result}, EXIT_OK
    if command == "publish":
        return {"command": command, "result": api.publish(ctx, api.resolve_pack(args.pack), args.channel,
                                                           args.commit)}, EXIT_OK  # fmt: skip
    if command == "record-release":
        return {"command": command, "result": api.record_release(ctx, Path(args.release))}, EXIT_OK
    if command == "ci":
        result = api.ci(args.base)
        return {"command": command, "result": result}, EXIT_OK if result["ok"] else EXIT_FAIL
    if command == "account":
        result = api.create_account(ctx, args.id, args.name, args.email, args.roles.split(","), args.publisher)
        return {"command": command, "result": result}, EXIT_OK
    if command in ("analytics", "inbox"):
        return {"command": command, "result": _intake(ctx, command, Path(args.payload))}, EXIT_OK
    if command == "audit":
        errors = chain_errors(ctx.audit.entries())
        return {"command": command, "result": {"entries": len(ctx.audit.entries()), "problems": errors}}, (
            EXIT_OK if not errors else EXIT_FAIL)  # fmt: skip
    raise ValueError(command)


def _intake(ctx: api.Context, command: str, path: Path) -> dict[str, Any]:
    """Take in an evaluation run or a consented research export (AUT-37), or content problem report counts (AUT-38):
    a refused payload changes nothing and exits 1."""
    from pack_builder.analytics import Analytics
    from pack_builder.problem_inbox import Inbox

    account = ctx.need()
    authorise(account, "see_analytics", ctx.audit, subject=command)
    payload = json.loads(path.read_text())
    result = (Analytics(ctx.root) if command == "analytics" else Inbox(ctx.root)).intake(payload)
    ctx.audit.append(account.account_id, f"{command}_intake", "ok", str(result["kind"]), json.dumps(result))
    return result


def _packs(args: argparse.Namespace) -> list[Path]:
    return [api.resolve_pack(args.pack)] if getattr(args, "pack", None) else api.pack_dirs()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aut", description="Headless authoring commands (AUT-05).")
    p.add_argument("--json", action="store_true", help="print machine-readable results")
    sub = p.add_subparsers(dest="command", required=True)
    studio = sub.add_parser("studio", help="run one offline studio JSON command from stdin")
    studio.add_argument("--workspace", required=True)
    for name in api.GATES:
        s = sub.add_parser(name)
        s.add_argument("paths", nargs="*")
        s.add_argument("--pack")
    s = sub.add_parser("coverage")
    s.add_argument("--pack")
    s.add_argument("--stage", choices=STAGES, help="the stage's target (default: the first stage of the pack)")
    s = sub.add_parser("eval")
    s.add_argument("--pack")
    s = sub.add_parser("build")
    s.add_argument("pack")
    s.add_argument("--out")
    s = sub.add_parser("preview")
    s.add_argument("record")
    s.add_argument("--out")
    s = sub.add_parser("draft")
    s.add_argument("task", choices=sorted(drafting.TASKS) + sorted(drafting.S4_TASKS))
    s.add_argument("record")
    s.add_argument("--instruction")
    s.add_argument("--cloud", action="store_true")
    s = sub.add_parser("accept")
    s.add_argument("record")
    s.add_argument("fields", nargs="+")
    s = sub.add_parser("edit", help="register an edit you made to a record's file")
    s.add_argument("record")
    s = sub.add_parser("quantities", help="give items the quantities their misconception signatures use (CUR-18, D90)")
    s.add_argument("--pack")
    s.add_argument("--write", action="store_true", help="save each verified item as a registered edit (CUR-19)")
    s.add_argument("--report", help="write the full report (every signature left dead, with its reason) as JSON")
    s.add_argument(
        "--propose",
        choices=["gemini"],
        help="first ask the cloud gateway's fast model for the mappings no "
        "reading binds, within the budget (CUR-18); the tools still decide",
    )
    s.add_argument(
        "--proposals",
        default=str(api.CURRICULUM.parent / "agent" / "drafting" / "quantity-proposals.json"),
        help="the proposal cache: a rerun reads it and asks again only for what changed",
    )
    s.add_argument("--env-file", help="a dotenv file holding the developer key when it is not in the environment")
    s.add_argument(
        "--gateway-config", help="deployment file selecting the proposal model, price and service permission"
    )
    s = sub.add_parser("promote", help="move drafted records that pass every gate to verified (CUR-19, D65)")
    s.add_argument("pack")
    s.add_argument("records", nargs="*")
    s = sub.add_parser("bank", help="move verified records into the evaluation bank (CUR-22)")
    s.add_argument("pack")
    s.add_argument("--held", nargs="+", help="the verified items to seal as held out (default: a share of each KC)")
    s.add_argument("--otp")
    s = sub.add_parser("record-release", help="mark the records of a signed release published in the source tree")
    s.add_argument("release")
    s = sub.add_parser("publish")
    s.add_argument("pack")
    s.add_argument("--channel", default="dev", choices=["dev", "pilot", "stable"])
    s.add_argument("--commit")
    s.add_argument("--otp")
    s = sub.add_parser("ci")
    s.add_argument("--base")
    s = sub.add_parser("account")
    s.add_argument("id")
    s.add_argument("--name", required=True)
    s.add_argument("--email", required=True)
    s.add_argument("--roles", required=True)
    s.add_argument("--publisher", action="store_true")
    s.add_argument("--otp")
    s = sub.add_parser("analytics", help="take in an evaluation run or consented research export (AUT-37)")
    s.add_argument("action", choices=["intake"])
    s.add_argument("payload")
    s = sub.add_parser("inbox", help="take in content problem report counts or an authorized support export (AUT-38)")
    s.add_argument("action", choices=["intake"])
    s.add_argument("payload")
    sub.add_parser("audit")
    return p


def _print(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=1, sort_keys=True, default=str))
        return
    print(json.dumps(result, indent=1, sort_keys=True, default=str))


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
    except SystemExit as exit_:
        return EXIT_USAGE if exit_.code else EXIT_OK
    try:
        result, code = run(args)
    except Forbidden as refusal:
        result, code = {"command": args.command, "error": "forbidden", "message": str(refusal)}, EXIT_FAIL
    except (ConnectionError, TimeoutError) as error:
        result, code = {"command": args.command, "error": "network_unavailable", "message": str(error)}, EXIT_NETWORK
    except (IntakeRefused, ReportRefused) as error:
        result, code = {"command": args.command, "error": "refused", "message": str(error)}, EXIT_FAIL
    except (drafting.DraftBlocked, drafting.DraftUnavailable) as error:
        result, code = {"command": args.command, "error": "blocked", "message": str(error)}, EXIT_FAIL
    except (FileNotFoundError, KeyError, ValueError) as error:
        result, code = {"command": args.command, "error": "usage", "message": str(error)}, EXIT_USAGE
    except (api.EnvironmentProblem, RuntimeError, OSError) as error:
        result, code = {"command": args.command, "error": "environment", "message": str(error)}, EXIT_ENV
    _print(result, args.json)
    return code


if __name__ == "__main__":
    sys.exit(main())
