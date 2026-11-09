"""Signing the revocation list with a root key (SEC-16, TABLE SEC-12a).

The list is signed by the root itself, so only a key-ceremony quorum can revoke or un-revoke a key. The test root signs
the development list the app bundles; the release root's list is signed in the offline ceremony through the HSM's
signing command (`hierarchy.CommandSigner`), never on a build machine. Each new list carries every earlier revocation
and a version one higher, so a device that sees lists out of order still ends with the newest.

The key-compromise runbook (SEC-16) uses this module for its first step: revoke the intermediate here, then re-sign
the affected artefacts with a new intermediate (`hierarchy.issue_certificate`), and publish the new list inside the
next app build, every content pack, update pack and USB kit.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from agent_core.config.revocation import BUNDLED, KIND
from agent_core.config.trust import unsigned_bytes

from signing_service.hierarchy import now_iso
from signing_service.keys import DEV_ROOT_KEY_ID, dev_root_key


def revocation_list(version: int, revoked: Iterable[Mapping[str, str]], created_at: str | None = None
                    ) -> dict[str, Any]:  # fmt: skip
    """An unsigned list: each entry names a `key_id`, when it was revoked and why (a short code, no detail)."""
    entries = sorted(({"key_id": str(e["key_id"]), "revoked_at": str(e["revoked_at"]), "reason": str(e["reason"])}
                      for e in revoked), key=lambda e: e["key_id"])  # fmt: skip
    return {"kind": KIND, "version": int(version), "created_at": created_at or now_iso(), "revoked": entries}


def sign_with_root(document: Mapping[str, Any], sign: Callable[[bytes], bytes], root_key_id: str) -> dict[str, Any]:
    unsigned = {k: v for k, v in document.items() if k != "signature"}
    value = base64.b64encode(sign(unsigned_bytes(unsigned))).decode()
    return {**unsigned, "signature": {"alg": "ed25519", "key_id": root_key_id, "value": value}}


def dev_revocation_list(version: int = 1, revoked: Iterable[Mapping[str, str]] = (),
                        created_at: str = "2026-10-13T00:00:00.000Z") -> dict[str, Any]:  # fmt: skip
    """The test root's list (development builds and tests only; release builds refuse the test root)."""
    return sign_with_root(revocation_list(version, revoked, created_at), dev_root_key().sign, DEV_ROOT_KEY_ID)


def write_bundled(path: Path = BUNDLED) -> Path:
    """The development list the app bundles: version 1, nothing revoked."""
    import json

    path.write_text(json.dumps(dev_revocation_list(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path
