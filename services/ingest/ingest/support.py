"""Support bundle handling (OPS-24, OBS-18, OPS-22, PRV-10, OPS-31).

A bundle reaches us only by a person's deliberate upload with the ticket reference support gave them (the support
page). Support opens a ticket first, naming its assignees; the ticket reference is long and random, so it cannot be
guessed. The upload is one of the app's two report files:

- a plain report (`.zip`): re-scanned with the redaction scanner (OBS-19) over every file, and its manifest's
  hashes checked; a finding refuses the upload and nothing is kept;
- a passphrase-sealed report (`.janadiag`, it includes a conversation): it cannot be read here, so it is stored as
  it came and opened only by an L3 assignee naming the defect; the passphrase travels by another channel and this
  service never accepts it.

Either is stored encrypted under the storage key. Every opening, refusal and deletion is in the access log. A
bundle is deleted when its ticket closes or after the retention period, whichever is first, unless a child-safety
incident holds the ticket, in which case it goes when the incident closes. A deletion request (PRV-10) deletes held
bundles too."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import secrets
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ingest import contracts, redaction
from ingest.config import IngestConfig, SupportLevel
from ingest.store import Bundle, IngestStore

SEALED_MAGIC = b"JANA-ARCHIVE-1\n"  # the app's passphrase-sealed export (SEC-18)
ZIP_MAGIC = b"PK\x03\x04"
MANIFEST = "manifest.json"
TICKET_PATTERN = re.compile(r"^JT-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$")
REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")  # incident and defect references
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class SupportRefused(Exception):
    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Staff:
    actor: str
    level: SupportLevel


def new_ticket_ref() -> str:
    raw = "".join(CROCKFORD[b % 32] for b in secrets.token_bytes(12))  # 60 bits
    return f"JT-{raw[:4]}-{raw[4:8]}-{raw[8:12]}"


def bundle_problems(data: bytes, max_files: int, max_unzipped: int) -> list[str]:
    """Why a plain report may not be kept: not a zip, too big unzipped, a manifest that does not match, or a
    redaction finding in any file (by kind only)."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return ["not a report file"]
    with archive:
        infos = archive.infolist()
        if len(infos) > max_files:
            return ["too many files"]
        if sum(info.file_size for info in infos) > max_unzipped:
            return ["too large once unzipped"]
        files: dict[str, bytes] = {}
        budget = max_unzipped
        for info in infos:
            if info.is_dir():
                continue
            with archive.open(info) as handle:
                content = handle.read(budget + 1)
            budget -= len(content)
            if budget < 0:
                return ["too large once unzipped"]  # the declared sizes understated the content
            files[info.filename] = content
    problems = []
    if MANIFEST not in files:
        problems.append("no manifest")
    else:
        try:
            listed = json.loads(files[MANIFEST])["files"]
        except (ValueError, KeyError, TypeError):
            listed = None
        if (
            not isinstance(listed, dict)
            or set(listed) != set(files) - {MANIFEST}
            or any(hashlib.sha256(files[name]).hexdigest() != digest for name, digest in listed.items())
        ):
            problems.append("the manifest does not match the files")
    for name, content in sorted(files.items()):
        found = redaction.findings(name + "\n" + content.decode("utf-8", errors="replace"), redaction.BUNDLE_ALLOWED)
        problems += [f"{name}: {kind}" for kind in found]
    return problems


class SupportDesk:
    def __init__(self, config: IngestConfig, store: IngestStore, storage_key: bytes,
                 clock: Callable[[], float]) -> None:  # fmt: skip
        self.config = config
        self.store = store
        self.cipher = AESGCM(storage_key)
        self.clock = clock

    # --- encryption at rest -----------------------------------------------------------------------------------------

    def _seal(self, bundle_id: str, label: bytes, data: bytes) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + self.cipher.encrypt(nonce, data, bundle_id.encode() + label)

    def _unseal(self, bundle_id: str, label: bytes, blob: bytes) -> bytes:
        return self.cipher.decrypt(blob[:12], blob[12:], bundle_id.encode() + label)

    # --- tickets ------------------------------------------------------------------------------------------------------

    def open_ticket(self, staff: Staff, assignees: list[str]) -> str:
        known = set(self.config.staff)
        if not assignees or not set(assignees) <= known:
            raise SupportRefused(400, "invalid_assignees", "Name one or more assignees from the staff list.")
        ticket_ref = new_ticket_ref()
        self.store.add_ticket(ticket_ref, frozenset(assignees), self.clock())
        self.store.log(self.clock(), staff.actor, "ticket.open", ticket_ref, True)
        return ticket_ref

    def close_ticket(self, staff: Staff, ticket_ref: str) -> list[str]:
        ticket = self.store.ticket(ticket_ref)
        if ticket is None or staff.actor not in ticket.assignees:
            self.store.log(self.clock(), staff.actor, "ticket.close", ticket_ref, False, "not an assignee")
            raise SupportRefused(404, "not_found", "No such ticket among yours.")
        self.store.close_ticket(ticket_ref, self.clock())
        self.store.log(self.clock(), staff.actor, "ticket.close", ticket_ref, True)
        return self.purge()

    def hold(self, staff: Staff, ticket_ref: str, incident: str) -> None:
        """A child-safety incident keeps the ticket's bundles as evidence until it closes (OPS-31)."""
        ticket = self.store.ticket(ticket_ref)
        if ticket is None or not REFERENCE_PATTERN.fullmatch(incident):
            raise SupportRefused(400, "invalid_request", "Name an existing ticket and an incident reference.")
        self.store.hold_ticket(ticket_ref, incident)
        self.store.log(self.clock(), staff.actor, "ticket.hold", ticket_ref, True, incident)

    def close_incident(self, staff: Staff, incident: str) -> list[str]:
        erased = []
        for ticket_ref in self.store.tickets_held_by(incident):
            self.store.hold_ticket(ticket_ref, None)
            self.store.log(self.clock(), staff.actor, "incident.close", ticket_ref, True, incident)
            for bundle in self.store.bundles(ticket_ref, live_only=True):
                self._erase(bundle.bundle_id, "incident closed")
                erased.append(bundle.bundle_id)
        return erased + self.purge()

    # --- upload (the support page) ------------------------------------------------------------------------------------

    def upload(self, ticket_ref: str, data: bytes, metadata_header: str | None) -> dict[str, Any]:
        now = self.clock()
        ticket = self.store.ticket(ticket_ref) if TICKET_PATTERN.fullmatch(ticket_ref) else None
        if ticket is None or ticket.closed_at is not None:
            raise SupportRefused(404, "ticket_not_found", "That ticket reference is not open. Check it with support.")
        if len(self.store.bundles(ticket_ref, live_only=True)) >= self.config.limits.max_bundles_per_ticket:
            raise SupportRefused(409, "ticket_full", "This ticket already holds the most reports it may.")
        metadata = self._metadata(metadata_header)
        if data.startswith(SEALED_MAGIC):
            sealed = True
        elif data.startswith(ZIP_MAGIC):
            sealed = False
            problems = bundle_problems(data, self.config.limits.max_bundle_files,
                                       self.config.limits.max_bundle_unzipped_bytes)  # fmt: skip
            if problems:
                kinds = sorted({p.rsplit(": ", 1)[-1] for p in problems})
                self.store.log(now, "uploader", "bundle.upload", ticket_ref, False, ", ".join(kinds))
                raise SupportRefused(422, "redaction_blocked" if any(k in redaction.PATTERNS for k in kinds)
                                     else "bundle_invalid", f"The report was not accepted ({kinds[0]}).")  # fmt: skip
        else:
            raise SupportRefused(415, "unsupported_file", "Upload the report file the app saved (.zip or .janadiag).")
        bundle_id = uuid.uuid4().hex
        path = self.store.bundle_path(bundle_id)
        with path.open("xb") as handle:
            path.chmod(0o600)
            handle.write(self._seal(bundle_id, b"file", data))
        sealed_metadata = None if metadata is None else self._seal(bundle_id, b"metadata", metadata)
        try:
            self.store.add_bundle(Bundle(bundle_id, ticket_ref, now, len(data), sealed,
                                         hashlib.sha256(data).hexdigest(), sealed_metadata, None))  # fmt: skip
        except Exception:
            path.unlink(missing_ok=True)
            raise
        self.store.log(now, "uploader", "bundle.upload", bundle_id, True, ticket_ref)
        return {"bundle_id": bundle_id, "ticket_ref": ticket_ref, "sealed": sealed, "scanned": not sealed}

    def _metadata(self, header: str | None) -> bytes | None:
        """The optional ticket metadata (`ops/support-bundle`: device and licence ids, version, channel, component
        health and error codes, never free text; OPS-22), sent base64url-encoded in a header."""
        if header is None:
            return None
        try:
            value = json.loads(base64.urlsafe_b64decode(header.encode() + b"=" * (-len(header) % 4)))
        except (ValueError, binascii.Error) as error:
            raise SupportRefused(400, "invalid_metadata", "The report details could not be read.") from error
        if contracts.errors("ops/support-bundle", value):
            raise SupportRefused(422, "invalid_metadata", "The report details break their contract.")
        return json.dumps(value, sort_keys=True).encode()

    # --- reading ----------------------------------------------------------------------------------------------------

    def _allowed(self, staff: Staff, bundle: Bundle | None, defect: str | None) -> str | None:
        """None when `staff` may open the bundle, else why not."""
        if bundle is None or bundle.deleted_at is not None:
            return "no such bundle"
        ticket = self.store.ticket(bundle.ticket_ref)
        if ticket is None or staff.actor not in ticket.assignees:
            return "not an assignee of the ticket"
        if bundle.sealed and staff.level != "L3":
            return "a report with conversation is opened by L3 only"
        if bundle.sealed and (defect is None or not REFERENCE_PATTERN.fullmatch(defect)):
            return "a report with conversation is opened for a named defect"
        return None

    def open(self, staff: Staff, bundle_id: str, defect: str | None = None) -> tuple[bytes, Bundle]:
        bundle = self.store.bundle(bundle_id)
        refusal = self._allowed(staff, bundle, defect)
        self.store.log(self.clock(), staff.actor, "bundle.open", bundle_id, refusal is None, refusal or defect)
        if refusal is not None or bundle is None:
            raise SupportRefused(404 if refusal == "no such bundle" else 403, "not_allowed", refusal or "")
        data = self._unseal(bundle_id, b"file", self.store.bundle_path(bundle_id).read_bytes())
        return data, bundle

    def metadata(self, staff: Staff, bundle_id: str) -> dict[str, Any] | None:
        """The ticket metadata of a bundle, for any assignee (it never holds conversation or free text)."""
        bundle = self.store.bundle(bundle_id)
        ticket = None if bundle is None or bundle.deleted_at is not None else self.store.ticket(bundle.ticket_ref)
        allowed = ticket is not None and staff.actor in ticket.assignees
        self.store.log(self.clock(), staff.actor, "bundle.metadata", bundle_id, allowed)
        if not allowed or bundle is None:
            raise SupportRefused(403, "not_allowed", "not an assignee of a live bundle")
        if bundle.metadata is None:
            return None
        loaded: dict[str, Any] = json.loads(self._unseal(bundle_id, b"metadata", bundle.metadata))
        return loaded

    def listing(self, staff: Staff, ticket_ref: str) -> list[dict[str, Any]]:
        ticket = self.store.ticket(ticket_ref)
        allowed = ticket is not None and staff.actor in ticket.assignees
        self.store.log(self.clock(), staff.actor, "ticket.list", ticket_ref, allowed)
        if not allowed:
            raise SupportRefused(404, "not_found", "No such ticket among yours.")
        return [{"bundle_id": b.bundle_id, "received_at": b.received_at, "bytes": b.size, "sealed": b.sealed,
                 "sha256": b.sha256, "deleted": b.deleted_at is not None}
                for b in self.store.bundles(ticket_ref)]  # fmt: skip

    # --- deletion and retention ---------------------------------------------------------------------------------------

    def _erase(self, bundle_id: str, reason: str) -> None:
        self.store.bundle_path(bundle_id).unlink(missing_ok=True)
        self.store.mark_erased(bundle_id, self.clock(), reason)
        self.store.log(self.clock(), "system", "bundle.erase", bundle_id, True, reason)

    def erase(self, staff: Staff, bundle_id: str) -> bool:
        """A deletion request (PRV-10): deletes the bundle even while an incident holds it."""
        bundle = self.store.bundle(bundle_id)
        if bundle is None or bundle.deleted_at is not None:
            self.store.log(self.clock(), staff.actor, "bundle.erase", bundle_id, False, "no such bundle")
            return False
        self.store.bundle_path(bundle_id).unlink(missing_ok=True)
        self.store.mark_erased(bundle_id, self.clock(), "deletion request")
        self.store.log(self.clock(), staff.actor, "bundle.erase", bundle_id, True, "deletion request")
        return True

    def purge(self) -> list[str]:
        expired = self.store.expired_bundles(self.clock(), self.config.retention.bundle_days)
        for bundle_id in expired:
            self._erase(bundle_id, "retention")
        return expired
