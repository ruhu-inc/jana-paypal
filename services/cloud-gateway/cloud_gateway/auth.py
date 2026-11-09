"""Device credentials (SEC-21): Ed25519 public keys bound to a licence, challenge-signed short-lived access tokens,
and revocation from the licence portal that also ends every outstanding token at once.

Registration presents the subscription entitlement the checkout signed with a licence key (BIZ-07, D70): the
credential is bound to that subscription, works only until the subscription's period ends, and a subscription holds
at most `devices_per_subscription` live credentials per covered student, so a leaked licence id mints nothing and a
lapsed subscription gets no new token. Quotas are kept per subscription (`Session.account`), so minting credentials
buys no headroom.

The device signs `b"jana-gateway-auth\\n" + nonce + b"\\n" + audience_host` with its service-credential key; the
audience is this gateway's configured host, so a signature cannot be replayed against another service.

Registration and the entitlement it carries are read through the gateway contract (D39): the signature is checked
first, so a forged or foreign entitlement is refused as such (403) whatever its shape, and a body the
`DeviceRegistration` contract does not accept is then refused as malformed (400)."""

from __future__ import annotations

import base64
import binascii
import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from cloud_gateway.config import GatewayConfig
from cloud_gateway.contracts import errors as contract_errors
from cloud_gateway.contracts.ops.licence import CloudTierEntitlement
from cloud_gateway.plans import check_terms
from cloud_gateway.store import GatewayStore

DOMAIN = b"jana-gateway-auth\n"
CREDENTIAL_PREFIX = "dcr_"


class AuthError(Exception):
    """A refused credential, challenge or token. The message is safe to return to the device."""


class RegistrationError(Exception):
    """A registration the gateway refuses (unknown licence or malformed key)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


ENTITLEMENT_KIND = "cloud_tier_subscription"
ENTITLEMENT_CONTRACT = "ops/licence#/$defs/CloudTierEntitlement"
REGISTRATION_CONTRACT = "cloud/gateway#/$defs/DeviceRegistration"
NO_ENTITLEMENT = "$: 'entitlement' is a required property"


@dataclass(frozen=True)
class Entitlement:
    licence_id: str
    subscription_id: str
    period_end: float
    period_start: float = 0.0
    seats: int = 1


def _moment(text: object) -> float:
    if not isinstance(text, str):
        raise ValueError("not a date-time")
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def verify_entitlement(document: object, keys: Mapping[str, str], now: float,
                       config: GatewayConfig | None = None) -> Entitlement:  # fmt: skip
    """The subscription entitlement, verified against the licence keys and current at `now`, and (with `config`) on
    terms TABLE BIZ-18a sells (BIZ-18); RegistrationError otherwise. The signature covers the canonical document
    without its `signature` field (SEC-12)."""
    if not isinstance(document, dict) or document.get("kind") != ENTITLEMENT_KIND:
        raise RegistrationError("entitlement is missing or not a cloud tier entitlement", 403)
    signature = document.get("signature")
    if not isinstance(signature, dict) or signature.get("alg") != "ed25519":
        raise RegistrationError("entitlement is not signed", 403)
    public = keys.get(str(signature.get("key_id")))
    if public is None:
        raise RegistrationError("entitlement is not signed by a licence key", 403)
    unsigned: dict[str, Any] = {k: v for k, v in document.items() if k != "signature"}
    try:
        body = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public)).verify(
            base64.b64decode(str(signature.get("value")), validate=True), body
        )
    except (InvalidSignature, ValueError, TypeError, binascii.Error) as exc:
        raise RegistrationError("entitlement does not verify", 403) from exc
    if contract_errors(ENTITLEMENT_CONTRACT, document):
        raise RegistrationError("entitlement does not match its contract", 403)
    granted = CloudTierEntitlement.model_validate(document)
    try:
        start, end = _moment(granted.period_start), _moment(granted.period_end)
    except ValueError as exc:
        raise RegistrationError("entitlement does not verify", 403) from exc
    if not start <= now < end:
        raise RegistrationError("the subscription is not current", 403)
    if config is not None and (why := check_terms(config, granted.plan, granted.seats)):
        raise RegistrationError(why, 403)
    return Entitlement(granted.licence_id, granted.subscription_id, end, start, granted.seats)


def signed_message(nonce: bytes, audience_host: str) -> bytes:
    return DOMAIN + nonce + b"\n" + audience_host.encode()


def _b64(value: object, what: str, length: int | None = None) -> bytes:
    if not isinstance(value, str):
        raise AuthError(f"{what} is missing")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AuthError(f"{what} is not base64") from exc
    if length is not None and len(raw) != length:
        raise AuthError(f"{what} has the wrong length")
    return raw


@dataclass(frozen=True)
class Session:
    """An authenticated request's credential and licence. Never logged or stored as such. `account` is what quotas
    and spend are kept for: the subscription, so every credential of one subscription shares them."""

    credential_id: str
    licence_id: str
    account: str = ""
    # The paid period's start and its seats, for the monthly allowance (BIZ-07, D33); None without an entitlement.
    period_start: float | None = None
    seats: int | None = None


class Authenticator:
    def __init__(self, config: GatewayConfig, store: GatewayStore, clock: Callable[[], float]) -> None:
        self.config = config
        self.store = store
        self.clock = clock

    def register(self, body: Mapping[str, Any]) -> str:
        """Bind the body's public key to the subscription its entitlement proves (a `DeviceRegistration`)."""
        public_key, licence_id, entitlement = body.get("public_key"), body.get("licence_id"), body.get("entitlement")
        try:
            raw = _b64(public_key, "public_key", 32)
            Ed25519PublicKey.from_public_bytes(raw)
        except (AuthError, ValueError) as exc:
            raise RegistrationError("public_key must be a base64 raw Ed25519 public key") from exc
        now = self.clock()
        if entitlement is not None:
            granted = verify_entitlement(entitlement, self.config.entitlement_keys, now, self.config)
            if licence_id not in (None, "", granted.licence_id):
                raise RegistrationError("licence_id does not match the entitlement", 403)
            if self.config.licences and granted.licence_id not in self.config.licences:
                raise RegistrationError("unknown licence", 403)
            subscription, licence, expires = granted.subscription_id, granted.licence_id, granted.period_end
            start, seats = granted.period_start, granted.seats
        elif self.config.open_registration:  # a developer environment only (config refuses it elsewhere)
            if not isinstance(licence_id, str) or not licence_id or len(licence_id) > 128:
                raise RegistrationError("licence_id is missing")
            subscription, licence, expires = f"dev:{licence_id}", licence_id, None
            start, seats = None, None
        else:
            raise RegistrationError("unknown licence", 403)
        problems = contract_errors(REGISTRATION_CONTRACT, dict(body))
        if entitlement is None:  # the developer environment's open registration carries none
            problems = [p for p in problems if p != NO_ENTITLEMENT]
        if problems:
            raise RegistrationError("the registration does not match its contract")
        outcome, credential_id = self.store.bind_credential(
            CREDENTIAL_PREFIX + secrets.token_urlsafe(18), raw, licence, subscription, expires, now,
            self.config.devices_per_subscription * (seats or 1), period_start=start, seats=seats,
        )  # fmt: skip
        if outcome == "revoked":
            raise RegistrationError("this device's credential was revoked", 403)
        if outcome == "full":
            raise RegistrationError("the subscription's device limit is reached", 409)
        return credential_id

    def _live(self, credential_id: object) -> str:
        if not isinstance(credential_id, str):
            raise AuthError("credential_id is missing")
        found = self.store.credential(credential_id)
        if found is None or found.revoked:
            raise AuthError("unknown or revoked credential")
        if found.lapsed(self.clock()):
            raise AuthError("the subscription has lapsed")
        return credential_id

    def challenge(self, credential_id: object) -> tuple[str, int]:
        live = self._live(credential_id)
        nonce = base64.b64encode(secrets.token_bytes(32)).decode()
        self.store.add_challenge(nonce, live, self.clock() + self.config.challenge_ttl_s)
        return nonce, self.config.challenge_ttl_s

    def token(self, credential_id: object, nonce: object, signature: object) -> tuple[str, int]:
        live = self._live(credential_id)
        nonce_bytes = _b64(nonce, "nonce", 32)
        # Single-use, whatever the outcome below; keyed by the canonical encoding of the nonce bytes.
        taken = self.store.take_challenge(base64.b64encode(nonce_bytes).decode())
        if taken is None or taken[0] != live:
            raise AuthError("unknown or used challenge")
        if self.clock() > taken[1]:
            raise AuthError("challenge expired")
        found = self.store.credential(live)
        if found is None or found.revoked or found.lapsed(self.clock()):
            raise AuthError("unknown or revoked credential")
        key = Ed25519PublicKey.from_public_bytes(found.public_key)
        raw = _b64(signature, "signature", 64)
        # The voice service is this deployment under its own host name (VOC-28): a token may be asked for there too.
        audiences = [self.config.audience_host, *([self.config.voice_audience_host] if self.config.voice_audience_host
                                                  else [])]  # fmt: skip
        for audience in audiences:
            try:
                key.verify(raw, signed_message(nonce_bytes, audience))
                break
            except InvalidSignature:
                continue
        else:
            raise AuthError("bad signature")
        token = secrets.token_urlsafe(32)
        ttl = self.config.access_token_ttl_s
        if found.expires_at is not None:  # a token never outlives the subscription
            ttl = max(1, min(ttl, int(found.expires_at - self.clock())))
        self.store.add_token(token, live, self.clock() + ttl)
        return token, ttl

    def authenticate(self, authorization: str | None) -> Session:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthError("missing access token")
        found = self.store.token(authorization.removeprefix("Bearer ").strip())
        if found is None:
            raise AuthError("unknown or revoked access token")
        credential_id, expires_at = found
        if self.clock() > expires_at:
            raise AuthError("access token expired")
        credential = self.store.credential(credential_id)
        if credential is None or credential.revoked:
            raise AuthError("unknown or revoked credential")
        if credential.lapsed(self.clock()):
            raise AuthError("the subscription has lapsed")
        return Session(credential_id, credential.licence_id, credential.subscription_id or credential_id,
                       credential.period_start, credential.seats)  # fmt: skip

    def revoke(self, credential_id: str) -> bool:
        return self.store.revoke(credential_id, self.clock())
