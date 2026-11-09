"""Offline help and metadata-only support bundles (OPS-22..25, UXS-46)."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent_core.config import Settings
from agent_core.contracts import errors as contract_errors
from agent_core.contracts.ops.support_bundle import SupportBundleMetadata

HELP = Path(__file__).with_name("offline_help.json")
GUIDES = frozenset({"parent", "installation", "quick_start", "escalation", "troubleshooting", "champion"})
TOPICS = frozenset(
    {"usb", "subject", "slow", "unavailable", "online_data", "rollback", "disk", "licence", "migration", "report"}
)


def intake_fields() -> tuple[str, ...]:
    return "ticket_id", "licence_id", "device_id", "category", "bundle_id"


# The ticket metadata is the `ops/support-bundle` contract (OPS-22, OPS-24, D92).
SupportBundle = SupportBundleMetadata


def bundle_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if contract_errors("ops/support-bundle", payload):
        raise ValueError("OPS-22: unsupported support bundle fields or metadata")
    parsed = SupportBundle.model_validate(payload)
    if any(not re.fullmatch(r"[a-z][a-z0-9_]*", code) for code in parsed.error_codes):
        raise ValueError("OPS-22: error codes only")
    return parsed.model_dump(exclude_unset=True)


class BundleVault:
    def __init__(self, root: Path, encryption_key: bytes, cfg: Settings | None = None) -> None:
        self.root, self.cipher, self.cfg = root, AESGCM(encryption_key), cfg or Settings()
        root.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bundles (id TEXT PRIMARY KEY, ticket TEXT, created REAL, hold TEXT,
                    ticket_closed INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS assignees (ticket TEXT, actor TEXT, PRIMARY KEY(ticket, actor));
                CREATE TABLE IF NOT EXISTS access (id TEXT, actor TEXT, at REAL, allowed INTEGER);
            """)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.root / "tickets.sqlite")
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(
        self,
        ticket: str,
        payload: dict[str, Any],
        assignees: set[str],
        now: datetime,
        *,
        deliberately_shared: bool,
        incident_hold: str | None = None,
    ) -> str:
        if not deliberately_shared or not ticket.strip() or not assignees or not all(a.strip() for a in assignees):
            raise ValueError("OPS-24: deliberate ticket share and assignees required")
        body = json.dumps(bundle_payload(payload), sort_keys=True).encode()
        identifier = uuid.uuid4().hex
        nonce = secrets.token_bytes(12)  # AES-GCM nonce size, not a policy threshold.
        data = nonce + self.cipher.encrypt(nonce, body, identifier.encode())
        path = self.root / f"{identifier}.enc"
        try:
            with path.open("xb") as handle:
                path.chmod(0o600)
                handle.write(data)
            with self._db() as db:
                db.execute(
                    "INSERT INTO bundles(id,ticket,created,hold) VALUES (?,?,?,?)",
                    (identifier, ticket, now.timestamp(), incident_hold),
                )
                for actor in assignees:
                    db.execute("INSERT OR IGNORE INTO assignees VALUES (?,?)", (ticket, actor))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return identifier

    def open(self, identifier: str, actor: str, now: datetime) -> dict[str, Any]:
        # Expiration is enforced at access, even if the scheduled purge has not run.
        self.purge(now)
        with self._db() as db:
            row = db.execute("SELECT ticket,deleted FROM bundles WHERE id=?", (identifier,)).fetchone()
            allowed = bool(
                row
                and not row[1]
                and db.execute("SELECT 1 FROM assignees WHERE ticket=? AND actor=?", (row[0], actor)).fetchone()
            )
            db.execute("INSERT INTO access VALUES (?,?,?,?)", (identifier, actor, now.timestamp(), allowed))
        if row is None or row[1]:
            raise FileNotFoundError("OPS-24: bundle unavailable")
        if not allowed:
            raise PermissionError("OPS-24: ticket assignment required")
        data = (self.root / f"{identifier}.enc").read_bytes()
        payload: dict[str, Any] = json.loads(self.cipher.decrypt(data[:12], data[12:], identifier.encode()))
        return bundle_payload(payload)

    def access_log(self, identifier: str) -> list[dict[str, Any]]:
        with self._db() as db:
            return [
                {"actor": actor, "at": at, "allowed": bool(allowed)}
                for actor, at, allowed in db.execute(
                    "SELECT actor,at,allowed FROM access WHERE id=? ORDER BY rowid", (identifier,)
                )
            ]

    def assign(self, ticket: str, actors: set[str]) -> None:
        if not actors or not all(a.strip() for a in actors):
            raise ValueError("OPS-24: ticket assignees required")
        with self._db() as db:
            db.execute("DELETE FROM assignees WHERE ticket=?", (ticket,))
            db.executemany("INSERT INTO assignees VALUES (?,?)", ((ticket, actor) for actor in actors))

    def erase(self, identifier: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("OPS-24: invalid bundle id")
        with self._db() as db:
            db.execute("UPDATE bundles SET deleted=1 WHERE id=?", (identifier,))
        (self.root / f"{identifier}.enc").unlink(missing_ok=True)

    def purge(self, now: datetime) -> list[str]:
        cutoff = (now - timedelta(days=self.cfg.ops_bundle_retention_days)).timestamp()
        with self._db() as db:
            identifiers = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM bundles WHERE deleted=0 AND hold IS NULL AND (ticket_closed=1 OR created<=?)",
                    (cutoff,),
                )
            ]
        for identifier in identifiers:
            self.erase(identifier)
        return identifiers

    def close_ticket(self, ticket: str, now: datetime) -> None:
        with self._db() as db:
            db.execute("UPDATE bundles SET ticket_closed=1 WHERE ticket=?", (ticket,))
        self.purge(now)

    def close_incident(self, incident: str, now: datetime) -> None:
        with self._db() as db:
            identifiers = [r[0] for r in db.execute("SELECT id FROM bundles WHERE hold=? AND deleted=0", (incident,))]
        for identifier in identifiers:
            self.erase(identifier)


class HelpCatalogue:
    """The offline FAQ and troubleshooting articles (OPS-23) as an index of ids and catalogue keys (LOC-01, D93): the
    words live in the per-locale catalogues shipped with the app (`help.article.<id>.title`, `.body`, `.step_<n>`), so
    the client shows and searches them in the student's locale and the core holds no user-visible text."""

    required_guides = GUIDES

    def __init__(self, app_version: str, source: Path = HELP, cfg: Settings | None = None) -> None:
        if not re.fullmatch(r"\d+\.\d+\.\d+", app_version):
            raise ValueError("OPS-25: invalid app version")
        self.app_version, self.cfg = app_version, cfg or Settings()
        self.index: list[dict[str, Any]] = json.loads(source.read_text(encoding="utf-8"))

    def articles(self) -> list[dict[str, Any]]:
        """Every article for this app version, as the `HelpReply` lists it."""
        major_minor = ".".join(self.app_version.split(".")[:2])
        return [
            {
                "id": entry["id"],
                "title_key": f"help.article.{entry['id']}.title",
                "body_key": f"help.article.{entry['id']}.body",
                "step_keys": [f"help.article.{entry['id']}.step_{n}" for n in range(1, int(entry["steps"]) + 1)],
                "app_major_minor": major_minor,
            }
            for entry in self.index
        ]

    def errors(self, catalogue: dict[str, str] | None = None) -> list[str]:
        """Why the help is incomplete: a topic of OPS-23 with no article, too few articles, an article with no steps,
        or (given a locale catalogue) a key the catalogue has no words for."""
        ids = [entry.get("id") for entry in self.index]
        if (
            len(ids) != len(set(ids))
            or not TOPICS <= set(ids)
            or len(ids) < self.cfg.ops_min_help_topics
            or any(int(entry.get("steps") or 0) < 1 for entry in self.index)
        ):
            return ["OPS-23: offline help incomplete"]
        if catalogue is not None:
            keys = [k for a in self.articles() for k in (a["title_key"], a["body_key"], *a["step_keys"])]
            missing = [k for k in keys if not str(catalogue.get(k) or "").strip()]
            if missing:
                return [f"OPS-23: no words for {k} (LOC-01)" for k in missing]
        return []

    def docs_errors(self, manifest: dict[str, Any]) -> list[str]:
        if manifest.get("app_major_minor") != ".".join(self.app_version.split(".")[:2]):
            return ["OPS-25: documentation version mismatch"]
        errors = []
        for guide in self.required_guides:
            entry = manifest.get("files", {}).get(guide, {})
            path = Path(entry.get("path", ""))
            if not path.is_file() or not path.read_bytes().startswith(b"%PDF-"):
                errors.append(f"OPS-25: missing offline PDF {guide}")
            elif hashlib.sha256(path.read_bytes()).hexdigest() != entry.get("sha256"):
                errors.append(f"OPS-25: document digest mismatch {guide}")
        return errors


SECTIONS = (
    "online_features",
    "profiles",
    "tutor_models",
    "content",
    "egress",
    "accessibility_language",
    "study",
    "tutor",
    "data_export",
    "licence",
    "help",
    "about",
)


def settings_sections(stage: str, *, school: bool = False) -> list[dict[str, Any]]:
    if stage not in {"S0", "S1", "S2", "S3", "S4", "S5"}:
        raise ValueError("UXS-46: unknown stage")
    names = list(SECTIONS) if stage >= "S2" else []
    if stage >= "S3":
        names += ["voice"]
        if school:
            names += ["parent_summary", "alerts"]
    return [{"id": name, "gate": "open", "offline": True, "states": ["ready", "empty", "error"]} for name in names]
