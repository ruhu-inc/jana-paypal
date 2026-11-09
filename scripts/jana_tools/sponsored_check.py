"""Run the two sponsored Study checks against PayPal Sandbox using synthetic adult enrollments."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import tomllib
import uuid
from contextlib import closing
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from agent_core.config.environments import Environment
from agent_core.store.subscription import verify
from cloud_gateway.config import load_config
from agent_core.config.trust import TrustStore
from signing_service.hierarchy import CommandSigner, Signer
from jana_tools.prepaid_signer import initialise

from jana_tools.sponsored_places import PRICE, FundingError, Places, Sandbox, matches_amount


def fixture_members(count):
    return [(f"prf_{count * 10 + n:026d}", f"lic_study_fixture_{n}") for n in range(3)]


def serve_return(db: Path):
    class ReturnPage(BaseHTTPRequestHandler):
        def do_GET(self):
            uri = urlsplit(self.path)
            if uri.path not in ("/sponsor/return", "/sponsor/cancel"):
                self.send_error(404)
                return
            order = parse_qs(uri.query).get("token", [""])[0]
            with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                row = conn.execute("SELECT state,frozen FROM workshops WHERE order_id=?", (order,)).fetchone()
            if row is None:
                self.send_error(404)
                return
            state, count = row
            message = "Check the workshop status after Jana verifies the order with PayPal."
            if state == "captured":
                message = f"{count} places funded. USD {count * PRICE / 100:.2f} captured."
            elif state == "voided":
                message = "No places claimed. Authorization voided. USD 0.00 captured."
            body = (
                "<!doctype html><html lang=en><meta charset=utf-8><title>Jana Sandbox workshop</title>"
                "<main><h1>Jana Sandbox workshop</h1><p>" + message + "</p></main></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass  # PayPal includes buyer identifiers in the return query.

    with ThreadingHTTPServer(("127.0.0.1", 8783), ReturnPage) as server:
        server.serve_forever()


def evidence(places, row, case, count):
    auth, captures = places.remote(row)
    records = []
    for entry in captures:
        if entry.get("id") != row["capture_id"]:
            raise FundingError("Unexpected capture in the verified order.")
        capture = places.paypal.call("GET", "/v2/payments/captures/" + row["capture_id"])
        if (
            capture.get("status") != "COMPLETED"
            or capture.get("final_capture") is not True
            or not matches_amount(capture.get("amount"), count * PRICE)
        ):
            raise FundingError("The final payment has changed since settlement.")
        records.append(
            {
                "status": capture["status"],
                "amount": capture["amount"]["value"],
                "final_capture": True,
                "provider_time": capture["update_time"],
                "receipt_suffix": capture["id"][-4:],
            }
        )
    if len(records) != int(count > 0) or auth["status"] != ("CAPTURED" if count else "VOIDED"):
        raise FundingError("The authorization or capture count differs from the expected outcome.")
    with closing(places.connect()) as conn:
        grants = conn.execute(
            "SELECT grant FROM members WHERE workshop=? AND grant IS NOT NULL", (row["id"],)
        ).fetchall()
    if len(grants) != count:
        raise FundingError("The signed grant count differs from the verified claim count.")
    return {
        "case": case,
        "environment": "PayPal Sandbox",
        "verified_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "authorized_usd": auth["amount"]["value"],
        "authorization_status": auth["status"],
        "authorization_suffix": auth["id"][-4:],
        "order_suffix": row["order_id"][-4:],
        "eligible_claims": count,
        "fixture_allowance_tokens": [json.loads(g[0])["allowance_tokens"] for g in grants],
        "model_calls": 0,
        "captures": records,
        "test_profile_grant_sha256": [hashlib.sha256(g[0].encode()).hexdigest() for g in grants],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=["start", "finish", "serve"])
    parser.add_argument("--gateway", type=Path, help="Use the gateway allowance terms for new fixture grants")
    parser.add_argument("--pilot", type=Path, default=Path.home() / ".config/jana/pilot.toml")
    parser.add_argument("--run", type=Path, required=True, help="Private directory for this pair of workshops")
    args = parser.parse_args()
    args.run.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.run.stat().st_mode & 0o077:
        parser.error("Use an owner-only run directory.")
    settings = tomllib.loads(args.pilot.read_text())
    db = args.run / "workshops.sqlite"
    if args.step == "finish" and not db.is_file():
        parser.error("Start this run first.")
    if args.step == "serve":
        serve_return(db)
        return
    pending = False
    traces = []
    enrollment = settings["enrollment"]
    places = Places(
        db,
        Sandbox(Path(settings["credentials_file"])),
        load_config(args.gateway),
        merchant=enrollment["merchant"],
        payer=enrollment["payer"],
        origin="http://localhost:8783",
    )
    if args.step == "finish":
        keys = args.run / "signer"
        if not keys.exists():
            try:
                initialise(keys)
            except FileExistsError:
                pass
        trust = TrustStore.from_document(json.loads((keys / "anchors.json").read_text()))
        certificate = trust.certificates["licence.prepaid-sandbox"]
        signer = Signer(
            certificate, CommandSigner((sys.executable, "-m", "jana_tools.prepaid_signer", "sign", str(keys)))
        )
    for case, count in (("two-claims", 2), ("no-claims", 0)):
        # A new run directory starts a separate pair. Re-running a step uses the existing orders.
        wid = str(uuid.uuid5(uuid.NAMESPACE_URL, str(args.run.resolve()) + "/" + case))
        members = fixture_members(count)
        places.register(wid, "sandbox-coordinator", members, window=15)
        if args.step == "start":
            row = places.fund(wid)
            print(case, row["approval"] if row["state"] == "approval" else row["state"], flush=True)
            continue
        row = places.authorize(wid)
        if row["state"] in ("approval", "authorizing"):
            pending = True
            print(case, "awaiting Sandbox buyer approval", flush=True)
            continue
        if row["state"] == "open" and places.clock() < row["deadline"]:
            for profile, _ in members[:count]:
                places.claim(wid, profile)
            time.sleep(max(0, row["deadline"] - places.clock()))
        eligible = {p for p, _ in members[:count]}
        row = places.settle(wid, eligible.__contains__)
        if row["state"] != ("captured" if count else "voided") or row["frozen"] != count:
            raise SystemExit(f"{case}: unexpected settlement; inspect the stored run before retrying.")
        for profile in eligible:
            signed = places.grant(wid, profile, signer)
            seat = verify(signed, Environment.DEV, trust)
            assert seat.profile_ids == (profile,) and seat.seats == 1
        traces.append(evidence(places, row, case, count))
        print(case, row["state"], f"USD {12 * count}.00", f"{count} funded places", flush=True)
    if pending:
        raise SystemExit(2)
    if len(traces) == 2:
        receipt = args.run / "verified.json"
        receipt.write_text(json.dumps(traces, indent=2) + "\n")
        print("Verified receipts:", receipt)


if __name__ == "__main__":
    main()
