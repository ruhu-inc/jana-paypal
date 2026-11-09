"""Device credentials for online services (SEC-21).

When an adult enables an authenticated online feature the device generates an Ed25519 service-credential key pair,
keeps the private key in the keystore under its own reference (never a database key or the backup key, SEC-32) and
registers the public key with the gateway, bound to the licence. Requests carry short-lived access tokens obtained
by signing a fresh challenge; the token, never the key, is sent. Anonymous features (downloads, update checks,
telemetry, crash reports) never carry the credential or any other device identifier: the policy refuses a request
that tries (`credential_on_anonymous_feature`). A credential revoked through the licence portal can obtain no new
token and its tokens stop working; the client then forgets it.

Every body the device sends to the gateway is built through the gateway contract's models and every reply read
through them (D39): a reply that does not match is a refusal (`CredentialRefused`), as is registering with no
subscription entitlement, which the `DeviceRegistration` contract requires (D70)."""

from __future__ import annotations

import base64
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import BaseModel, ValidationError

from agent_core.contracts.cloud.gateway import (
    AccessToken,
    Challenge,
    ChallengeRequest,
    DeviceRegistered,
    DeviceRegistration,
    TokenRequest,
)
from agent_core.net.client import EgressClient
from agent_core.store.keystore import KeyStore

KEY_REF = "jana-meta.service-credential.ed25519"  # the purpose-specific keystore entry (SEC-21)
ID_REF = "jana-meta.service-credential.id"
AUTH_CONTEXT = b"jana-gateway-auth\n"
FEATURE = "cloud_tier"
PAYLOAD = "service_auth"


class CredentialRefused(Exception):
    """The gateway refused the credential (unknown or revoked) or the token."""


def signed_message(nonce: bytes, audience: str) -> bytes:
    return AUTH_CONTEXT + nonce + b"\n" + audience.encode()


@dataclass
class ServiceCredential:
    keys: KeyStore
    client: EgressClient
    host: Callable[[], str]  # the gateway host (the audience of every token)
    licence_id: Callable[[], str | None]
    device: Any = None  # device.db: the registered public key is recorded in `device`
    clock: Callable[[], float] = time.time
    # The subscription's signed entitlement, the gateway's proof that this device may register (D70, BIZ-07).
    entitlement: Callable[[], Mapping[str, Any] | None] = lambda: None
    margin_s: float = 60.0  # renew this long before the token expires
    _token: tuple[str, float] | None = field(default=None, repr=False)

    def public_key(self) -> bytes:
        """The credential's public key, made on first use."""
        raw = self.keys.get(KEY_REF)
        if raw is None:
            raw = Ed25519PrivateKey.generate().private_bytes_raw()
            self.keys.put(KEY_REF, raw)
        return Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def _sign(self, message: bytes) -> bytes:
        raw = self.keys.get(KEY_REF)
        if raw is None:
            raise CredentialRefused("no service credential")
        return Ed25519PrivateKey.from_private_bytes(raw).sign(message)

    def credential_id(self) -> str | None:
        found = self.keys.get(ID_REF)
        return found.decode() if found else None

    async def register(self, feature: str = FEATURE, host: str | None = None) -> str:
        entitlement = self.entitlement()
        if entitlement is None:
            raise CredentialRefused("no subscription entitlement to register with")
        public = self.public_key()
        try:
            registration = DeviceRegistration.model_validate({
                "public_key": base64.b64encode(public).decode(), "licence_id": self.licence_id() or "",
                "entitlement": dict(entitlement),
            })  # fmt: skip
        except ValidationError:
            raise CredentialRefused("the registration does not match the gateway contract") from None
        body = registration.model_dump(mode="json", exclude_unset=True)
        reply = await self.client.send(feature, "POST", f"https://{host or self.host()}/v1/devices", PAYLOAD, body=body)
        if reply.status >= 300:
            raise CredentialRefused(f"registration refused ({reply.status})")
        credential_id = _reply(DeviceRegistered, reply.body, "registration").credential_id
        self.keys.put(ID_REF, credential_id.encode())
        if self.device is not None:
            self.device.execute("UPDATE device SET credential_public_key = ?, credential_registered_at = ?",
                                (public, int(self.clock() * 1000)))  # fmt: skip
        return credential_id

    async def token(self, feature: str = FEATURE, host: str | None = None) -> str:
        """A short-lived access token, renewed before it expires. Cloud voice asks through its own feature and host
        (VOC-28): the voice service is the gateway's deployment under its own name, so the token serves both."""
        now = self.clock()
        if self._token is not None and self._token[1] - self.margin_s > now:
            return self._token[0]
        self.forget_token()  # due for renewal: a refusal below leaves no token behind
        audience = host or self.host()
        credential_id = self.credential_id() or await self.register(feature, audience)
        base = f"https://{audience}/v1/auth"
        asked = _body(ChallengeRequest, {"credential_id": credential_id}, "challenge")
        challenge = await self.client.send(feature, "POST", f"{base}/challenge", PAYLOAD, body=asked)
        if challenge.status >= 300:
            self.forget_token()
            if challenge.status == 401:  # lapsed or revoked: the next attempt registers again with the current
                self.keys.delete(ID_REF)  # entitlement, which a revoked key can never do (SEC-21, D70)
            raise CredentialRefused(f"challenge refused ({challenge.status})")
        given = _reply(Challenge, challenge.body, "challenge")
        signature = self._sign(signed_message(base64.b64decode(given.nonce), audience))
        exchange = _body(TokenRequest, {"credential_id": credential_id, "nonce": given.nonce,
                                        "signature": base64.b64encode(signature).decode()}, "token")  # fmt: skip
        reply = await self.client.send(feature, "POST", f"{base}/token", PAYLOAD, body=exchange)
        if reply.status >= 300:
            self.forget_token()
            raise CredentialRefused(f"token refused ({reply.status})")
        token = _reply(AccessToken, reply.body, "token")
        self._token = (token.access_token, now + token.expires_in)
        return self._token[0]

    def forget_token(self) -> None:
        self._token = None


def _body[M: BaseModel](model: type[M], values: dict[str, Any], what: str) -> dict[str, Any]:
    """A request body built through its contract model."""
    try:
        return model.model_validate(values).model_dump(mode="json", exclude_unset=True)
    except ValidationError:
        raise CredentialRefused(f"the {what} request does not match the gateway contract") from None


def _reply[M: BaseModel](model: type[M], body: Any, what: str) -> M:
    """A gateway reply read through its contract model; one that does not match is a refusal."""
    try:
        return model.model_validate(body)
    except ValidationError:
        raise CredentialRefused(f"the gateway's {what} reply does not match its contract") from None
