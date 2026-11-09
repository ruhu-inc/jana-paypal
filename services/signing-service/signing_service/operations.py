"""Service-side operations publication (OPS-02, OPS-06, OPS-10).

The deployment supplies its server-owned gate runner. Client claims never substitute for that runner.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from agent_core.config import Environment
from agent_core.config.operations import ACTIONS, SafetyEntry
from agent_core.config.trust import TrustSource, current_trust, unsigned_bytes
from agent_core.contracts import validate

from signing_service.hierarchy import Signer

GateRunner = Callable[[str, Mapping[str, Any]], list[str]]


class OperationsPublisher:
    def __init__(
        self, root: Path, signer: Signer, trust: TrustSource, environment: Environment, recheck: GateRunner
    ) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "operations-publications.sqlite"
        self.signer, self.trust, self.environment, self.recheck = signer, trust, environment, recheck
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS publications (
                kind TEXT, channel TEXT, sequence INTEGER, body BLOB, document TEXT,
                PRIMARY KEY(kind,channel,sequence))""")

    def publish(self, kind: str, document: Mapping[str, Any], *, at: str) -> dict[str, Any]:
        if kind not in {"compatibility_matrix", "safety_configuration"}:
            raise ValueError("OPS-02: unsupported operations artefact")
        body = {k: v for k, v in document.items() if k != "signature"}
        channel = body.get("channel")
        field = "matrix_version" if kind == "compatibility_matrix" else "sequence"
        sequence = body.get(field)
        if channel not in {"dev", "pilot", "stable"} or type(sequence) is not int or sequence <= 0:
            raise ValueError("OPS-02: channel and monotonic sequence required")
        # Schema shape only; this placeholder is never published or treated as a signature.
        shaped = {**body, "signature": {"alg": "ed25519", "key_id": self.signer.key_id, "value": "AA=="}}
        if kind == "compatibility_matrix":
            validate("ops/compatibility-matrix", shaped)
        else:
            # The publisher refuses what a device would ignore: every entry must be a TABLE OPS-11a entry (D92).
            validate("ops/safety-configuration", shaped)
            identifiers = set()
            for raw in shaped["entries"]:
                validate("ops/safety-configuration#/$defs/Entry", raw)
                entry = SafetyEntry.model_validate(raw)
                if entry.action not in ACTIONS.get(entry.target_kind, set()) or entry.entry_id in identifiers:
                    raise ValueError("OPS-10: unknown, duplicate or nonrestrictive entry")
                identifiers.add(entry.entry_id)
        if errors := self.recheck(kind, body):
            raise ValueError("OPS-06: " + "; ".join(errors))
        canonical = unsigned_bytes(body)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT sequence,body,document FROM publications WHERE kind=? AND channel=? "
                "ORDER BY sequence DESC LIMIT 1",
                (kind, channel),
            ).fetchone()
            if previous and sequence <= previous[0]:
                if sequence != previous[0] or canonical != previous[1]:
                    raise ValueError("OPS-02: immutable version or sequence regression")
                held: dict[str, Any] = json.loads(previous[2])
                current_trust(self.trust).verify(kind, held, self.environment, str(held["issued_at"]))
                return held
            signed = self.signer.sign(kind, body, now=at)
            current_trust(self.trust).verify(kind, signed, self.environment, str(body["issued_at"]))
            db.execute(
                "INSERT INTO publications VALUES (?,?,?,?,?)",
                (kind, channel, sequence, canonical, json.dumps(signed, sort_keys=True)),
            )
            return signed
