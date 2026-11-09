"""School licence files on the signing side (PLT-19, BIZ-04, BIZ-05, BIZ-10, D84): one signed JSON file per school, by
a licence-purpose key (SEC-12).

Families need nothing from here: the offline product is free and needs no activation, and online features are bought
through the subscription (D30, D70, D84). A school installs the one file on each lab computer; it names no computer
and nobody types a code (D84). `seats` states how many computers were bought.

This is the vendor's tool, not a shop: no payment, price or buyer detail passes through it, and nothing about a student
(BIZ-10: payment is taken from adults, outside the app, by a channel the owner chooses).

School seats are set aside (D10, decided by the product owner in D100): no school seat is defined or sold, so only the
test hierarchy may sign a `school_seat` file (the tests of BIZ-04, which applies once the owner defines a seat). A lab
bundle file still makes a school's lab computers `school_lab` installs (BIZ-05, JRN-11), for the pilot's labs too.

    python -m signing_service.licences --kind lab_bundle --seats 30 [--expires 2027-12-31] [--channel pilot]

Without a production signer this uses the test hierarchy, which pilot and production builds refuse (OPS-35)."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from collections.abc import Sequence
from datetime import date
from typing import Any

from agent_core.config.trust import ARTEFACT_PURPOSE, Purpose
from agent_core.contracts import errors as contract_errors
from agent_core.store.licence import CHANNELS, CONTRACT, KINDS

from signing_service.hierarchy import Signer, SigningRefused, dev_signer, now_iso
from signing_service.keys import DEV_KEY_IDS

SET_ASIDE = ("school_seat",)  # D10 (D100): not defined and not sold


def issue_licence_file(kind: str, seats: int, *, channel: str = "stable", features: Sequence[str] = (),
                       expires_at: str | None = None, signer: Signer | None = None,
                       now: str | None = None) -> dict[str, Any]:  # fmt: skip
    """One signed licence file for a school's computers, matching the `SchoolLicenceFile` contract (D39)."""
    signer = signer or dev_signer(Purpose.LICENCE)
    if kind not in KINDS:
        raise SigningRefused(f"{kind!r} is not a licence kind ({', '.join(KINDS)})")
    if channel not in CHANNELS:
        raise SigningRefused(f"{channel!r} is not a release channel ({', '.join(CHANNELS)})")
    if signer.purpose is not ARTEFACT_PURPOSE["licence_file"]:
        raise SigningRefused(f"{signer.key_id} is a {signer.purpose.value} key and may not sign a licence file")
    if kind in SET_ASIDE and signer.key_id not in DEV_KEY_IDS.values():
        raise SigningRefused(f"{kind} licences are set aside: no school seat is defined or sold (D10, D100)")
    if seats < 1:
        raise SigningRefused("a licence covers at least one computer")
    created = now or now_iso()
    document = {"licence_id": f"lic_{secrets.token_hex(5)}", "kind": kind, "seats": seats, "features": list(features),
                "channel": channel, "policy": {}, "expires_at": expires_at, "created_at": created}  # fmt: skip
    signed = signer.sign("licence_file", document, created)
    if problems := contract_errors(CONTRACT, signed):
        raise SigningRefused(f"the licence file would not match its contract: {problems[0]}")
    return signed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="signing_service.licences")
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--seats", type=int, required=True)
    p.add_argument("--channel", default="stable", choices=CHANNELS)
    p.add_argument("--expires", type=date.fromisoformat)
    args = p.parse_args(argv)
    end = f"{args.expires.isoformat()}T23:59:59.999Z" if args.expires else None
    print(json.dumps(issue_licence_file(args.kind, args.seats, channel=args.channel, expires_at=end), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
