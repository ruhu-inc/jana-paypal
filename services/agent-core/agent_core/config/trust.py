"""The signing key hierarchy as the device sees it (SEC-12, TABLE SEC-12a; OPS-35 signing roots).

Every product signature is Ed25519. A root key, kept offline, signs intermediate key certificates; each intermediate
has one purpose (content, model, agent bundle, updater, licence) and signs only the artefacts of that purpose. The
app bundles the root public keys and the intermediate certificates (`trust_anchors.json`: public data only). An
artefact verifies when its signing key has a certificate that

- chains to a bundled root whose signing root this environment trusts (OPS-35),
- names the purpose of this artefact type (a content key cannot sign a model manifest),
- is not revoked, and
- was valid when the artefact says it was signed (its `created_at`), so a key's rotation never invalidates the packs
  it signed while it was current.

Private keys never come here. The test root's keys are derived from published phrases by the signing service and
give no authority: pilot and production builds refuse the test root.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agent_core.config.environments import Environment, signing_root_allowed

ANCHORS = Path(__file__).with_name("trust_anchors.json")


class Purpose(StrEnum):
    """What an intermediate key may sign (TABLE SEC-12a)."""

    CONTENT = "content"
    MODEL = "model"
    AGENT_BUNDLE = "agent_bundle"
    UPDATER = "updater"
    LICENCE = "licence"


# Artefact types and the one purpose that may sign each (TABLE SEC-12a, column "Signs").
ARTEFACT_PURPOSE: dict[str, Purpose] = {
    "content_pack": Purpose.CONTENT,
    "content_diff": Purpose.CONTENT,
    "usb_kit": Purpose.CONTENT,
    "safety_configuration": Purpose.CONTENT,
    "pilot_country": Purpose.CONTENT,
    "compatibility_matrix": Purpose.CONTENT,
    "dpia_list": Purpose.CONTENT,
    "model_manifest": Purpose.MODEL,
    "agent_bundle": Purpose.AGENT_BUNDLE,
    "updater_artefact": Purpose.UPDATER,
    "perception_pack": Purpose.UPDATER,
    "speech_pack": Purpose.UPDATER,  # ships the native voice sidecar: executable, so updater-signed (D88)
    "offline_update_pack": Purpose.UPDATER,
    "licence_file": Purpose.LICENCE,
    "activation_reply": Purpose.LICENCE,
}


class SignatureRefused(ValueError):
    """The signature does not verify, or its key is unknown, revoked, out of date or of the wrong purpose."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def unsigned_bytes(document: Mapping[str, Any]) -> bytes:
    """What a signature covers: the canonical document without its `signature` field."""
    return canonical_bytes({k: v for k, v in document.items() if k != "signature"})


def signed_body(document: Mapping[str, Any]) -> bytes:
    """`unsigned_bytes` for a document to verify: one that cannot be encoded (a lone surrogate, a value JSON cannot
    hold) is refused as unsigned rather than escaping as another error (SEC-13)."""
    try:
        return unsigned_bytes(document)
    except (UnicodeError, TypeError, ValueError) as error:
        raise SignatureRefused(f"the document cannot be read as signed JSON ({type(error).__name__})") from None


def _moment(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _public(b64: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(b64))


@dataclass(frozen=True)
class RootKey:
    key_id: str
    public_key: str  # base64 raw Ed25519
    root: str  # the signing root name environments trust (OPS-35): test, staging or release
    not_before: str
    not_after: str


def root_valid_at(root: RootKey, moment: str) -> bool:
    """A root key signs only inside its own validity window, at both ends (SEC-12)."""
    return _moment(root.not_before) <= _moment(moment) < _moment(root.not_after)


@dataclass(frozen=True)
class Certificate:
    """An intermediate key certificate: the body below, signed by a root key (`signature.key_id`)."""

    key_id: str
    purpose: Purpose
    public_key: str
    root_key_id: str
    not_before: str
    not_after: str
    signature: str  # base64 Ed25519 by the root key over `body()`

    def body(self) -> dict[str, Any]:
        return {"key_id": self.key_id, "purpose": self.purpose.value, "public_key": self.public_key,
                "root_key_id": self.root_key_id, "not_before": self.not_before,
                "not_after": self.not_after}  # fmt: skip

    def record(self) -> dict[str, Any]:
        return {**self.body(), "signature": self.signature}

    @staticmethod
    def from_record(record: Mapping[str, Any]) -> Certificate:
        return Certificate(str(record["key_id"]), Purpose(str(record["purpose"])), str(record["public_key"]),
                           str(record["root_key_id"]), str(record["not_before"]), str(record["not_after"]),
                           str(record["signature"]))  # fmt: skip

    def valid_at(self, moment: str) -> bool:
        return _moment(self.not_before) <= _moment(moment) < _moment(self.not_after)


@dataclass(frozen=True)
class TrustStore:
    """Root keys, intermediate certificates and revoked key ids, as bundled with the app."""

    roots: Mapping[str, RootKey]
    certificates: Mapping[str, Certificate]
    revoked: frozenset[str] = field(default_factory=frozenset)

    @staticmethod
    def from_document(document: Mapping[str, Any], revoked: Iterable[str] = ()) -> TrustStore:
        roots = {str(r["key_id"]): RootKey(str(r["key_id"]), str(r["public_key"]), str(r["root"]),
                                           str(r["not_before"]), str(r["not_after"]))
                 for r in document.get("roots", [])}  # fmt: skip
        certificates = {str(c["key_id"]): Certificate.from_record(c) for c in document.get("certificates", [])}
        return TrustStore(roots, certificates, frozenset(revoked))

    def document(self) -> dict[str, Any]:
        roots = [{"key_id": r.key_id, "public_key": r.public_key, "root": r.root, "not_before": r.not_before,
                  "not_after": r.not_after} for r in sorted(self.roots.values(), key=lambda r: r.key_id)]  # fmt: skip
        certificates = [c.record() for c in sorted(self.certificates.values(), key=lambda c: c.key_id)]
        return {"roots": roots, "certificates": certificates}

    def with_revoked(self, key_ids: Iterable[str]) -> TrustStore:
        return TrustStore(self.roots, self.certificates, self.revoked | frozenset(key_ids))

    def key_for(self, key_id: str, artefact: str, environment: Environment, signed_at: str | None = None
                ) -> Ed25519PublicKey:  # fmt: skip
        """The public key that may verify `artefact` signed by `key_id`, or `SignatureRefused` saying why not."""
        purpose = ARTEFACT_PURPOSE.get(artefact)
        if purpose is None:
            raise SignatureRefused(f"{artefact} is not a signed artefact type")
        certificate = self.certificates.get(key_id)
        if certificate is None:
            raise SignatureRefused(f"signed by an unknown key ({key_id})")
        root = self.roots.get(certificate.root_key_id)
        if root is None:
            raise SignatureRefused(f"{key_id} does not chain to a bundled root")
        if not signing_root_allowed(environment, root.root):
            raise SignatureRefused(f"the {root.root} signing root is not trusted in {environment.value} builds")
        if key_id in self.revoked or root.key_id in self.revoked:
            raise SignatureRefused(f"{key_id} is revoked")
        try:
            _public(root.public_key).verify(base64.b64decode(certificate.signature),
                                            canonical_bytes(certificate.body()))  # fmt: skip
        except (InvalidSignature, ValueError) as error:
            raise SignatureRefused(f"the certificate of {key_id} does not verify") from error
        if certificate.purpose is not purpose:
            raise SignatureRefused(f"{key_id} is a {certificate.purpose.value} key; {artefact} needs a "
                                   f"{purpose.value} key")  # fmt: skip
        if signed_at is not None:
            try:
                current = certificate.valid_at(signed_at) and root_valid_at(root, signed_at)
            except ValueError as error:
                raise SignatureRefused(f"the signing time {signed_at!r} is not a date-time") from error
            if not current:
                raise SignatureRefused(f"{key_id} was not valid at {signed_at}")
        return _public(certificate.public_key)

    def verify(self, artefact: str, document: Mapping[str, Any], environment: Environment,
               signed_at: str | None = None) -> None:  # fmt: skip
        """Verify a document's embedded `signature` ({alg, key_id, value}) over the rest of it."""
        signature = document.get("signature")
        if not isinstance(signature, Mapping) or signature.get("alg") != "ed25519":
            raise SignatureRefused("not signed with ed25519")
        self.verify_bytes(artefact, signed_body(document), signature, environment, signed_at)

    def verify_bytes(self, artefact: str, body: bytes, signature: Mapping[str, Any], environment: Environment,
                     signed_at: str | None = None) -> None:  # fmt: skip
        public = self.key_for(str(signature.get("key_id")), artefact, environment, signed_at)
        try:
            public.verify(base64.b64decode(str(signature.get("value"))), body)
        except (InvalidSignature, ValueError) as error:
            raise SignatureRefused("the signature does not verify") from error


@cache
def bundled_trust() -> TrustStore:
    """The roots and intermediate certificates this build carries."""
    return TrustStore.from_document(json.loads(ANCHORS.read_text()))


# Where a store reads its trust from: a fixed store, a callable read at each use (the device's revocation store, so a
# list adopted later applies at once, SEC-16), or None for the bundled anchors.
TrustSource = TrustStore | Callable[[], TrustStore] | None


def current_trust(source: TrustSource) -> TrustStore:
    if source is None:
        return bundled_trust()
    if isinstance(source, TrustStore):
        return source
    return source()
