"""Provision a dedicated development-only licence signer for one PayPal Sandbox deployment."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_core.config.trust import TrustStore
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from signing_service.hierarchy import dev_root, issue_certificate, public_b64
from signing_service.keys import Purpose, dev_root_key


def initialise(directory: Path):
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    key = Ed25519PrivateKey.generate()
    with os.fdopen(os.open(directory / "licence.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
        f.write(key.private_bytes_raw())
    root, now = dev_root(), datetime.now(UTC)
    cert = issue_certificate(
        root,
        dev_root_key().sign,
        "licence.prepaid-sandbox",
        Purpose.LICENCE,
        public_b64(key),
        (now - timedelta(minutes=1)).isoformat(),
        (now + timedelta(days=365)).isoformat(),
    )
    (directory / "anchors.json").write_text(json.dumps(TrustStore({root.key_id: root}, {cert.key_id: cert}).document()))
    return cert


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("action", choices=["init", "sign"])
    args.add_argument("directory", type=Path)
    cmd = args.parse_args()
    if cmd.action == "init":
        cert = initialise(cmd.directory)
        print(f'[entitlement_keys]\n"{cert.key_id}" = "{cert.public_key}"')
    else:
        payload = sys.stdin.buffer.read(16385)
        document = json.loads(payload)
        if (
            len(payload) > 16384
            or document.get("kind") != "cloud_tier_subscription"
            or document.get("plan") != "jana_online"
            or document.get("seats") != 1
        ):
            raise ValueError("Expected a single-seat Sandbox licence.")
        key = Ed25519PrivateKey.from_private_bytes((cmd.directory / "licence.key").read_bytes())
        print(base64.b64encode(key.sign(payload)).decode())


if __name__ == "__main__":
    main()
