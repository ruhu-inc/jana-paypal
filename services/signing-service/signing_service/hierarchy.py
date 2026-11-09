"""The signing key hierarchy on the signing side (SEC-12, TABLE SEC-12a).

A root key signs intermediate certificates; each intermediate signs one purpose's artefacts. This module issues
certificates (refusing a validity longer than the configured rotation for the purpose), signs documents with an
intermediate (refusing an artefact of another purpose, or a certificate not valid now) and writes the public trust
anchors the app bundles.

Two signers exist:

- `dev_signer`: the test hierarchy (`keys.py`), derived from published phrases and refused by pilot and production.
- `CommandSigner`: production. The private key stays in its HSM; the service runs the HSM's signing command by
  reference (no key material on the build machine or in CI variables), hands it the bytes to sign and checks the
  returned signature against the intermediate's certificate before using it.
"""

from __future__ import annotations

import base64
import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_core.config import Settings
from agent_core.config.trust import (
    ANCHORS,
    ARTEFACT_PURPOSE,
    Certificate,
    Purpose,
    RootKey,
    TrustStore,
    canonical_bytes,
    unsigned_bytes,
)
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from signing_service.keys import DEV_KEY_IDS, DEV_ROOT_KEY_ID, dev_key, dev_root_key

ROTATION_KEY = {
    Purpose.CONTENT: "signing_content_rotation_years",
    Purpose.MODEL: "signing_model_rotation_years",
    Purpose.AGENT_BUNDLE: "signing_agent_bundle_rotation_years",
    Purpose.UPDATER: "signing_updater_rotation_years",
    Purpose.LICENCE: "signing_licence_rotation_years",
}
# The test hierarchy's validity: it confers no authority, so it is not rotated (OPS-35 refuses it outside dev).
DEV_NOT_BEFORE = "2026-01-01T00:00:00.000Z"
DEV_NOT_AFTER = "2046-01-01T00:00:00.000Z"
TEST_ROOT = "test"


class SigningRefused(ValueError):
    """The signer may not sign this artefact now, or a certificate's validity breaks the rotation policy."""


def public_b64(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def now_iso() -> str:
    moment = datetime.now(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _moment(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _years_between(start: str, end: str) -> float:
    return (_moment(end) - _moment(start)).total_seconds() / (365.25 * 86_400)


def root_record(key_id: str, public_key: str, root: str, not_before: str, not_after: str,
                settings: Settings | None = None) -> RootKey:  # fmt: skip
    """A root's public record; its validity may not exceed the root rotation (SEC-12)."""
    settings = settings or Settings()
    if _moment(not_after) <= _moment(not_before):
        raise SigningRefused(f"{key_id}: not_after is not after not_before")
    if root != TEST_ROOT and _years_between(not_before, not_after) > settings.signing_root_rotation_years:
        raise SigningRefused(f"{key_id}: a root is valid for at most {settings.signing_root_rotation_years} years")
    return RootKey(key_id, public_key, root, not_before, not_after)


def issue_certificate(root: RootKey, sign_with_root: Callable[[bytes], bytes], key_id: str, purpose: Purpose,
                      public_key: str, not_before: str, not_after: str, settings: Settings | None = None
                      ) -> Certificate:  # fmt: skip
    """An intermediate certificate signed by the root (offline, two-person ceremony for the release root)."""
    settings = settings or Settings()
    if _moment(not_after) <= _moment(not_before):
        raise SigningRefused(f"{key_id}: not_after is not after not_before")
    if _moment(not_before) < _moment(root.not_before) or _moment(not_after) > _moment(root.not_after):
        raise SigningRefused(f"{key_id}: the certificate outlives its root {root.key_id}")
    limit = int(getattr(settings, ROTATION_KEY[purpose]))
    if root.root != TEST_ROOT and _years_between(not_before, not_after) > limit:
        raise SigningRefused(f"{key_id}: a {purpose.value} key is valid for at most {limit} year(s)")
    unsigned = Certificate(key_id, purpose, public_key, root.key_id, not_before, not_after, "")
    value = base64.b64encode(sign_with_root(canonical_bytes(unsigned.body()))).decode()
    return Certificate(key_id, purpose, public_key, root.key_id, not_before, not_after, value)


@dataclass(frozen=True)
class Signer:
    """An intermediate key that signs one purpose's artefacts, by its certificate."""

    certificate: Certificate
    sign_bytes: Callable[[bytes], bytes]

    @property
    def key_id(self) -> str:
        return self.certificate.key_id

    @property
    def purpose(self) -> Purpose:
        return self.certificate.purpose

    def sign(self, artefact: str, document: Mapping[str, Any], now: str | None = None) -> dict[str, Any]:
        """`document` with its `signature` ({alg, key_id, value}) over the canonical rest of it."""
        wanted = ARTEFACT_PURPOSE.get(artefact)
        if wanted is not self.purpose:
            raise SigningRefused(f"{self.key_id} is a {self.purpose.value} key and may not sign a {artefact}")
        moment = now or now_iso()
        if not self.certificate.valid_at(moment):
            raise SigningRefused(f"{self.key_id} is not valid at {moment}; rotate to the current key")
        unsigned = {k: v for k, v in document.items() if k != "signature"}
        signature = self.sign_bytes(unsigned_bytes(unsigned))
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(self.certificate.public_key)).verify(
                signature, unsigned_bytes(unsigned)
            )
        except (InvalidSignature, ValueError) as error:
            raise SigningRefused(f"the signature from {self.key_id} does not match its certificate") from error
        value = base64.b64encode(signature).decode()
        return {**unsigned, "signature": {"alg": "ed25519", "key_id": self.key_id, "value": value}}


def dev_root() -> RootKey:
    return root_record(DEV_ROOT_KEY_ID, public_b64(dev_root_key()), TEST_ROOT, DEV_NOT_BEFORE, DEV_NOT_AFTER)


def dev_certificate(purpose: Purpose) -> Certificate:
    return issue_certificate(dev_root(), dev_root_key().sign, DEV_KEY_IDS[purpose], purpose,
                             public_b64(dev_key(purpose)), DEV_NOT_BEFORE, DEV_NOT_AFTER)  # fmt: skip


def dev_signer(purpose: Purpose = Purpose.CONTENT) -> Signer:
    return Signer(dev_certificate(purpose), dev_key(purpose).sign)


def dev_trust() -> TrustStore:
    root = dev_root()
    return TrustStore({root.key_id: root}, {DEV_KEY_IDS[p]: dev_certificate(p) for p in Purpose})


def anchors_document(stores: Sequence[TrustStore]) -> dict[str, Any]:
    roots: dict[str, Any] = {}
    certificates: dict[str, Any] = {}
    for store in stores:
        document = store.document()
        roots.update({r["key_id"]: r for r in document["roots"]})
        certificates.update({c["key_id"]: c for c in document["certificates"]})
    return {"$comment": "Public trust anchors (SEC-12): root public keys and intermediate certificates. No private "
                        "key material. Regenerate with: uv run python -m signing_service anchors --write",
            "roots": [roots[k] for k in sorted(roots)],
            "certificates": [certificates[k] for k in sorted(certificates)]}  # fmt: skip


def anchors_text(extra: Sequence[TrustStore] = ()) -> str:
    """The bundled anchors: the test hierarchy plus any production roots and certificates already issued."""
    return json.dumps(anchors_document([dev_trust(), *extra]), indent=1, sort_keys=True) + "\n"


def production_anchors(path: Path = ANCHORS) -> TrustStore:
    """The non-test roots and certificates already in the bundled anchors (kept when the file is rewritten)."""
    if not path.is_file():
        return TrustStore({}, {})
    store = TrustStore.from_document(json.loads(path.read_text()))
    roots = {k: r for k, r in store.roots.items() if r.root != TEST_ROOT}
    return TrustStore(roots, {k: c for k, c in store.certificates.items() if c.root_key_id in roots})


@dataclass(frozen=True)
class CommandSigner:
    """Production signing by reference: `argv` is the HSM's signing command (a PKCS#11 or KMS client) for the key
    named by the certificate. It reads the bytes on stdin and writes the base64 signature on stdout."""

    argv: tuple[str, ...]
    timeout_s: float = 60.0

    def __call__(self, body: bytes) -> bytes:
        done = subprocess.run(list(self.argv), input=body, capture_output=True, timeout=self.timeout_s, check=False)
        if done.returncode != 0:
            raise SigningRefused(f"the signing command failed ({done.returncode})")
        try:
            return base64.b64decode(done.stdout.strip(), validate=True)
        except ValueError as error:
            raise SigningRefused("the signing command did not return a base64 signature") from error


def production_signer(certificate_key_id: str, argv: Sequence[str], anchors: TrustStore | None = None) -> Signer:
    """The production intermediate named `certificate_key_id`, signing through its HSM command."""
    store = anchors or production_anchors()
    certificate = store.certificates.get(certificate_key_id)
    if certificate is None:
        raise SigningRefused(f"{certificate_key_id} has no certificate in the bundled anchors; run the key ceremony")
    if store.roots[certificate.root_key_id].root == TEST_ROOT:
        raise SigningRefused(f"{certificate_key_id} belongs to the test root")
    return Signer(certificate, CommandSigner(tuple(argv)))


def verify_issued(store: TrustStore) -> list[str]:
    """Problems with a set of anchors: a certificate that does not chain to its root."""
    problems = []
    for certificate in store.certificates.values():
        root = store.roots.get(certificate.root_key_id)
        if root is None:
            problems.append(f"{certificate.key_id}: no root {certificate.root_key_id}")
            continue
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(root.public_key)).verify(
                base64.b64decode(certificate.signature), canonical_bytes(certificate.body())
            )
        except (InvalidSignature, ValueError):
            problems.append(f"{certificate.key_id}: the certificate does not verify with {root.key_id}")
    return problems
