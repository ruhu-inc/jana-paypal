"""Operator-only settlement of uncertain Study sends, using independently obtained provider evidence.

Dry run by default. No provider call, learner content, credential or daily device identifier is accepted.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from pathlib import Path
from typing import Any

from cloud_gateway.store import GatewayStore


def reconcile(
    store: GatewayStore, identity: str, cost: float, evidence: str, *, apply: bool = False, now: float | None = None
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", identity) or not re.fullmatch(r"[0-9a-f]{64}", evidence):
        raise ValueError("Receipt and provider-evidence digest must be SHA-256 hex values")
    if not math.isfinite(cost) or not 0 <= cost <= 1000:
        raise ValueError("Actual cost must be finite and between 0 and 1000 USD")
    with store._tx():
        row = store._db.execute(
            "SELECT state,reserved_usd,account,month_start,actual_usd FROM study_receipts WHERE identity=?",
            (identity,),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown receipt")
        previous = store._db.execute(
            "SELECT actual_usd,evidence_sha256 FROM study_reconciliations WHERE identity=?", (identity,)
        ).fetchone()
        if previous is not None:
            if previous[0] != cost or previous[1] != evidence:
                raise ValueError("Receipt was already reconciled with different evidence or cost")
            return {"receipt_id": identity, "state": "reconciled", "actual_usd": cost, "changed": False}
        if row[0] != "reserved":
            raise ValueError("Only an uncertain reserved send can be reconciled")
        if apply:
            store._db.execute(
                "INSERT INTO study_reconciliations VALUES (?,?,?,?,?)",
                (identity, evidence, row[1], cost, time.time() if now is None else now),
            )
            store._db.execute(
                "UPDATE study_receipts SET state='reconciled',reserved_usd=0,actual_usd=? WHERE identity=?",
                (cost, identity),
            )
            if row[3] is not None and cost:
                store._db.execute(
                    "INSERT INTO allowance_spend VALUES (?,?,?) ON CONFLICT(account_sha256,month_start) "
                    "DO UPDATE SET cost_usd=cost_usd+excluded.cost_usd",
                    (row[2], row[3], cost),
                )
        return {
            "receipt_id": identity,
            "state": "reconciled" if apply else "dry_run",
            "reserved_usd": row[1],
            "actual_usd": cost,
            "changed": apply,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-dir", type=Path, required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--cost-usd", type=float, required=True)
    parser.add_argument("--evidence-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not (args.storage_dir / "gateway.sqlite3").is_file():
        parser.error("An existing gateway storage directory is required")
    store = GatewayStore(args.storage_dir)
    try:
        result = reconcile(store, args.receipt, args.cost_usd, args.evidence_sha256, apply=args.apply)
    except ValueError as error:
        parser.error(str(error))
    finally:
        store.close()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
