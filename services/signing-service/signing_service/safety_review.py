"""The content safety review gate of the signing service (CUR-27).

Before a pack is signed for the pilot or stable channel, the service recomputes the release's safety review sample
(`pack_builder.safety_review.sample`: every new or changed record on a sensitive topic and the configured share of the
rest) and requires a review record at `content/curriculum/safety-reviews/<pack id>/<version>.json` in the commit that
covers the whole sample, approves every sampled record in its current content, and is signed by a reviewer the
service trusts. The development channel does not need one; its release record says whether one was present.

Trusted reviewers are public keys in `safety_reviewers.json` beside this module, added by code review: a subject or
curriculum lead's own Ed25519 key, never held by the service. It ships empty: no pack reaches pilot or stable before
the owner adds their key and signs a review. The development reviewer key below is derived from a published phrase,
so it proves nothing about any content: tests use it to sign fixture reviews, and it is trusted only where a caller
passes `development_reviewers()` explicitly.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_core.config.trust import unsigned_bytes
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pack_builder.safety_review import Sample, coverage_problems

TRUSTED = Path(__file__).with_name("safety_reviewers.json")
DEV_REVIEWER_KEY_ID = "key_dev_safety_reviewer"
_DEV_REVIEW_PHRASE = b"jana development safety reviewer: a test fixture, never a real content review"

Reviewers = Mapping[str, str]  # key id -> base64 Ed25519 public key


def trusted_reviewers(path: Path = TRUSTED) -> dict[str, str]:
    """The reviewers whose signature the service accepts for pilot and stable (public keys only)."""
    data = json.loads(path.read_text()) if path.is_file() else {"reviewers": []}
    return {str(r["key_id"]): str(r["public_key"]) for r in data.get("reviewers", [])}


def dev_reviewer_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(_DEV_REVIEW_PHRASE).digest())


def _public_b64(key: Ed25519PrivateKey) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def development_reviewers() -> dict[str, str]:
    """The development reviewer only: for tests and development tooling, never the default trust."""
    return {DEV_REVIEWER_KEY_ID: _public_b64(dev_reviewer_key())}


def sign_review(review: Mapping[str, Any], key: Ed25519PrivateKey, key_id: str) -> dict[str, Any]:
    """A reviewer's signature over the canonical review record (run by the reviewer, with their own key)."""
    unsigned = {k: v for k, v in review.items() if k != "signature"}
    value = base64.b64encode(key.sign(unsigned_bytes(unsigned))).decode()
    return {**unsigned, "signature": {"alg": "ed25519", "key_id": key_id, "value": value}}


def signature_problems(review: Mapping[str, Any], reviewers: Reviewers) -> list[str]:
    signature = review.get("signature")
    if not isinstance(signature, Mapping) or signature.get("alg") != "ed25519":
        return ["the safety review is not signed (CUR-27)"]
    key_id = str(signature.get("key_id"))
    public = reviewers.get(key_id)
    if public is None:
        return [f"the safety review is signed by {key_id}, not a trusted reviewer (CUR-27)"]
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public)).verify(
            base64.b64decode(str(signature.get("value"))), unsigned_bytes(review)
        )
    except (InvalidSignature, ValueError):
        return [f"the safety review's signature does not verify against {key_id} (CUR-27)"]
    return []


def load_review(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def review_problems(review: Mapping[str, Any] | None, wanted: Sample, reviewers: Reviewers) -> list[str]:
    """Everything that keeps a release from publication under CUR-27: a missing, untrusted or broken signature, or a
    sample the review does not cover and approve."""
    if review is None:
        return coverage_problems(None, wanted)
    return signature_problems(review, reviewers) + coverage_problems(review, wanted)
