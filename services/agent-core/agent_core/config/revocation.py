"""The revocation list (SEC-16): key ids the root has withdrawn, carried by every artefact a device receives.

The list is signed by a root key itself (TABLE SEC-12a: the root signs intermediate certificates and the revocation
list), not by an intermediate, so a compromised intermediate cannot un-revoke itself. It ships in every app build
(`revocation_list.json` beside the trust anchors), inside content packs and update packs as an embedded signed file,
and in the USB kit. The device keeps the newest valid list it has seen (`version` grows by one per list) and adopts a
newer one from whichever source brings it, so a revocation reaches an offline device through any ordinary update.

A list whose signature does not verify, whose root this environment does not trust, or whose version is not newer
than the one held is ignored: nothing a pack carries can shrink what is revoked.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agent_core.config.environments import Environment, signing_root_allowed
from agent_core.config.trust import SignatureRefused, TrustStore, bundled_trust, unsigned_bytes

ARTEFACT = "revocation_list"
KIND = "revocation_list"
BUNDLED = Path(__file__).with_name("revocation_list.json")
HELD = "config/revocations.json"  # the adopted list, in the device's data folder (TABLE DAT-01a, SEC-16)
LEGACY_HELD = "revocation_list.json"  # where builds before 2026-10-15 kept it


def verify_list(document: Mapping[str, Any], environment: Environment, base: TrustStore | None = None) -> None:
    """A revocation list signed by a bundled root this environment trusts; raises `SignatureRefused` otherwise."""
    if document.get("kind") != KIND or not isinstance(document.get("version"), int):
        raise SignatureRefused("not a revocation list")
    signature = document.get("signature")
    if not isinstance(signature, Mapping) or signature.get("alg") != "ed25519":
        raise SignatureRefused("the revocation list is not signed with ed25519")
    store = base or bundled_trust()
    root = store.roots.get(str(signature.get("key_id")))
    if root is None:
        raise SignatureRefused("the revocation list is not signed by a root key")
    if not signing_root_allowed(environment, root.root):
        raise SignatureRefused(f"the {root.root} signing root is not trusted in {environment.value} builds")
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(root.public_key)).verify(
            base64.b64decode(str(signature.get("value"))), unsigned_bytes(document)
        )
    except (InvalidSignature, ValueError) as error:
        raise SignatureRefused("the revocation list's signature does not verify") from error


def revoked_ids(document: Mapping[str, Any] | None) -> frozenset[str]:
    if not document:
        return frozenset()
    return frozenset(str(entry["key_id"]) for entry in document.get("revoked", []))


class RevocationStore:
    """The newest valid revocation list this device has seen, persisted in `folder`."""

    def __init__(self, folder: Path, environment: Environment, base: TrustStore | None = None,
                 bundled: Path | None = BUNDLED) -> None:  # fmt: skip
        self.folder = folder
        self.environment = environment
        self.base = base or bundled_trust()
        folder.mkdir(parents=True, exist_ok=True)
        legacy = folder / LEGACY_HELD
        if legacy.is_file():  # a list held where earlier builds kept it moves to its place, once
            try:
                self.adopt(json.loads(legacy.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
            legacy.unlink(missing_ok=True)
        if bundled is not None and bundled.is_file():
            self.adopt(json.loads(bundled.read_text(encoding="utf-8")))  # the list this build shipped with

    @property
    def path(self) -> Path:
        return self.folder / HELD

    def current(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            held: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
            verify_list(held, self.environment, self.base)  # a held list edited on disk counts for nothing
        except (OSError, ValueError, SignatureRefused):
            return None
        return held

    def version(self) -> int:
        held = self.current()
        return int(held["version"]) if held else 0

    def adopt(self, document: Mapping[str, Any]) -> bool:
        """Keep `document` if it verifies and is newer than the held list; returns whether it was adopted."""
        try:
            verify_list(document, self.environment, self.base)
        except SignatureRefused:
            return False
        if int(document["version"]) <= self.version():
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(dict(document), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, self.path)
        return True

    def revoked(self) -> frozenset[str]:
        return revoked_ids(self.current())

    def trust(self) -> TrustStore:
        """The bundled anchors with every key the held list revokes refused."""
        return self.base.with_revoked(self.revoked())
