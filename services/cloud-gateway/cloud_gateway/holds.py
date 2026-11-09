"""Show the inference ledger's holds, and release those of calls that are over once provider billing shows they were
never charged.

    uv run python -m cloud_gateway.holds --config /operator/gateway.toml
    uv run python -m cloud_gateway.holds --config ... --release failed --older-than 10m            # dry run
    uv run python -m cloud_gateway.holds --config ... --release failed --status 400 --older-than 10m --apply

A call that fails, times out or ends without usage keeps its full upper bound so an unknown charge can never be
overspent; each hold records how its call ended (a call whose caller went away is settled at its input estimate,
`client_disconnected`, and holds nothing). Releasing is the operator's reconciliation step: check the provider's usage
page first. A release is a dry run unless --apply is given, and it never touches a call still in flight. Holds made
before outcomes were recorded, or left in flight by a gateway that stopped (marked when the next one starts), show as
`unknown` and are released only with `--release unknown`. Settled spend is
never changed here, and a call that settles after its hold was released still records what it was billed.
"""

from __future__ import annotations

import argparse
import re
import time
from collections.abc import Callable
from pathlib import Path

from cloud_gateway.config import load_config
from cloud_gateway.inference_budget import RELEASABLE, InferenceBudget
from cloud_gateway.store import GatewayStore

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def duration(text: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", text.strip())
    if not match:
        raise argparse.ArgumentTypeError("a duration such as 30s, 10m, 2h or 1d")
    return float(match[1]) * UNITS[match[2]]


def age(seconds: float) -> str:
    for unit in ("d", "h", "m"):
        if seconds >= UNITS[unit]:
            return f"{seconds / UNITS[unit]:.1f}{unit}"
    return f"{seconds:.0f}s"


def main(argv: list[str] | None = None, clock: Callable[[], float] = time.time) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--release", choices=list(RELEASABLE), help="release holds of calls that ended this way")
    parser.add_argument("--older-than", type=duration, default=duration("10m"), help="only holds at least this old")
    parser.add_argument("--status", type=int, help="only holds whose call ended with this upstream status")
    parser.add_argument("--apply", action="store_true", help="release them (otherwise a dry run)")
    parser.add_argument("--release-all", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.release_all:
        parser.error(
            "--release-all is gone: it also dropped holds of calls still in flight. Use --release (failed, ended, "
            "unknown, ...) with --older-than; it is a dry run unless --apply is given."
        )
    config = load_config(args.config)
    store = GatewayStore(config.storage_dir)
    try:
        budget = InferenceBudget(config, store, clock)
        now = clock()
        for group in budget.holds():
            status = f" status={group['status']}" if group["status"] is not None else ""
            dated = "migrated" if group["migrated"] else "oldest"
            since = f"{dated} {age(now - group['oldest_at'])} ago" if group["oldest_at"] is not None else "not dated"
            print(f"  {group['outcome']:<24} {group['role']:<12} {group['model']:<16}{status} "
                  f"{group['count']:>4} holds ${group['held_usd']:.4f} ({since})")  # fmt: skip
        if args.release:
            count, held = budget.release_held(args.release, args.older_than, status=args.status, apply=args.apply)
            verb = "released" if args.apply else "would release (dry run; add --apply)"
            print(f"{verb} {count} holds (${held:.4f}): {args.release}, older than {age(args.older_than)}"
                  + (f", status {args.status}" if args.status is not None else ""))  # fmt: skip
        snapshot = budget.snapshot()
        print(f"spent ${snapshot['spent_usd']:.4f} in total, ${snapshot['today_usd']:.4f} today (UTC); "
              f"held ${snapshot['held_usd']:.4f}")  # fmt: skip
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
