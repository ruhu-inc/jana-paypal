"""Ed25519 signing of pack manifests (CUR-08): the algorithm only.

The pack builder, the `aut` CLI, CI and the studio never hold a signing key (D34, AUT-33). Keys live with the signing
service (`services/signing-service`), which rebuilds a pack from a commit, re-runs every gate and signs with the key
handed to `sign` here.
"""

from __future__ import annotations

import base64
from typing import Any

from agent_core.store.packs import signing_bytes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def public_key_b64(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def sign(manifest: dict[str, Any], key: Ed25519PrivateKey, key_id: str) -> dict[str, Any]:
    unsigned = {k: v for k, v in manifest.items() if k != "signature"}
    value = base64.b64encode(key.sign(signing_bytes(unsigned))).decode()
    return {**unsigned, "signature": {"alg": "ed25519", "key_id": key_id, "value": value}}
