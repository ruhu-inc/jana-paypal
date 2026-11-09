"""Authoring accounts, the second sign-in factor and the hash-chained audit log (AUT-02).

Accounts and the audit log live in the authoring home (`JANA_AUTHORING_HOME`, by default `~/.jana/authoring`), never in
the content repository: names and e-mail addresses stay in the authoring system, while records, review records and
packs carry only pseudonymous ids (`rev_…`, `pub_…`). Each person has exactly one account. A time-based one-time
code (RFC 6238) is checked for the actions in `roles.SECOND_FACTOR`: none while one person authors (D67). Every
state change, permission refusal, AI drafting call and publish request is appended to the audit log; each entry
carries the sha256 of the one before, so deleting or reordering an entry breaks the chain.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pack_builder.roles import ROLES, SECOND_FACTOR, Forbidden, require
from pack_builder.studio_io import locked

HOME_ENV = "JANA_AUTHORING_HOME"
AUTHOR_ENV = "JANA_AUTHOR"  # the account id of the person running the CLI
OTP_ENV = "JANA_AUTHOR_OTP"
TOTP_STEP_S = 30


def home() -> Path:
    return Path(os.environ.get(HOME_ENV) or Path.home() / ".jana" / "authoring")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- second factor --------------------------------------------------------------------------------------------------


def totp(secret_b32: str, at: float | None = None, step: int = TOTP_STEP_S) -> str:
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(base64.b32decode(secret_b32), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def totp_ok(secret_b32: str, code: str | None, at: float | None = None) -> bool:
    if not code:
        return False
    moment = time.time() if at is None else at
    return any(hmac.compare_digest(totp(secret_b32, moment + d * TOTP_STEP_S), code) for d in (-1, 0, 1))


# --- publish request authentication (AUT-33) ---------------------------------------------------------------------

REQUEST_MAC_CONTEXT = b"jana-publish-request\n"


def request_mac(totp_secret: str, request: Mapping[str, Any]) -> str:
    """The MAC a publish request carries (`mac`), keyed from the requesting account's own secret with a separate
    context, over the canonical request without its `mac`: the signing service accepts only a request its named
    requester made (AUT-33)."""
    key = hmac.new(base64.b32decode(totp_secret), REQUEST_MAC_CONTEXT, hashlib.sha256).digest()
    body = json.dumps({k: v for k, v in request.items() if k != "mac"}, sort_keys=True, separators=(",", ":"))
    return hmac.new(key, body.encode(), hashlib.sha256).hexdigest()


# --- accounts -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Account:
    account_id: str
    rev_id: str
    roles: tuple[str, ...]
    pub_id: str | None = None
    display_name: str = field(default="", repr=False)  # personal: never leaves the authoring home
    email: str = field(default="", repr=False)
    totp_secret: str = field(default="", repr=False)


class Accounts:
    def __init__(self, root: Path | None = None) -> None:
        self.path = (root or home()) / "accounts.json"

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        data: dict[str, dict[str, Any]] = json.loads(self.path.read_text())["accounts"]
        return data

    def all(self) -> dict[str, Account]:
        return {k: Account(k, v["rev_id"], tuple(v["roles"]), v.get("pub_id"), v.get("display_name", ""),
                           v.get("email", ""), v.get("totp_secret", "")) for k, v in self._read().items()}  # fmt: skip

    def get(self, account_id: str) -> Account:
        found = self.all().get(account_id)
        if found is None:
            raise Forbidden(f"no authoring account {account_id!r}")
        return found

    def by_rev_id(self, rev_id: str) -> Account | None:
        return next((a for a in self.all().values() if a.rev_id == rev_id), None)

    def create(self, account_id: str, display_name: str, email: str, roles: list[str], pub: bool = False) -> Account:
        """One account per person: a second account for the same e-mail address is refused."""
        accounts = self._read()
        if account_id in accounts or any(a.get("email", "").lower() == email.lower() for a in accounts.values()):
            raise ValueError("each person has exactly one authoring account (AUT-02)")
        if not roles or set(roles) - set(ROLES):
            raise ValueError(f"roles must be from {', '.join(ROLES)}")
        suffix = secrets.token_hex(6)
        accounts[account_id] = {"rev_id": f"rev_{suffix}", "roles": sorted(roles), "display_name": display_name,
                                "email": email, "totp_secret": base64.b32encode(secrets.token_bytes(20)).decode(),
                                **({"pub_id": f"pub_{suffix}"} if pub else {})}  # fmt: skip
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"accounts": accounts}, indent=1, sort_keys=True) + "\n")
        os.chmod(self.path, 0o600)
        return self.get(account_id)


def authorise(account: Account, action: str, audit: AuditLog, *, code: str | None = None, subject: str = "") -> None:
    """Check the role and, for the actions that need it, the second factor; every refusal is audited."""
    try:
        require(account.roles, action)
        if action in SECOND_FACTOR and not totp_ok(account.totp_secret, code):
            raise Forbidden(f"{action}: a second sign-in factor is required (AUT-02)")
    except Forbidden as refusal:
        audit.append(account.account_id, action, "forbidden", subject, str(refusal))
        raise


# --- audit log ------------------------------------------------------------------------------------------------------


def _entry_sha(entry: dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class AuditLog:
    def __init__(self, root: Path | None = None) -> None:
        self.path = (root or home()) / "audit.jsonl"

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def append(self, actor: str, action: str, outcome: str, subject: str = "", detail: str = "") -> dict[str, Any]:
        """Read the last entry and append the next one under one cross-process lock (an OS file lock, so the studio
        and the CLI on the same home cannot both chain to the same entry), flushed to disk before the lock is let go."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with locked(self.path):
            entries = self.entries()
            last = entries[-1] if entries else None
            entry = {"seq": (last["seq"] + 1) if last else 1, "at": now_iso(), "actor": actor, "action": action,
                     "outcome": outcome, "subject": subject, "detail": detail[:500],
                     "prev_sha256": last["sha256"] if last else None}  # fmt: skip
            entry["sha256"] = _entry_sha(entry)
            with self.path.open("a") as out:
                out.write(json.dumps(entry, sort_keys=True) + "\n")
                out.flush()
                os.fsync(out.fileno())
        return entry


def chain_errors(entries: list[dict[str, Any]]) -> list[str]:
    """Why the audit chain does not hold: an edited, deleted or reordered entry."""
    errors = []
    previous: dict[str, Any] | None = None
    for index, entry in enumerate(entries):
        if entry.get("sha256") != _entry_sha(entry):
            errors.append(f"entry {entry.get('seq')}: its content does not match its hash")
        if entry.get("prev_sha256") != (previous["sha256"] if previous else None):
            errors.append(f"entry {entry.get('seq')}: does not follow entry {previous['seq'] if previous else 'none'}")
        if entry.get("seq") != index + 1:
            errors.append(f"entry at position {index + 1} has sequence {entry.get('seq')}")
        previous = entry
    return errors
