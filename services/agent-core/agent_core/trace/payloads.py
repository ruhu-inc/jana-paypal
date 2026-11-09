"""Payload capture (OBS-07): `hashes` by default; `full` only in support mode, for one profile, for 24 hours.

Payload text (prompts, completions, tool arguments and results) is never in the trace store: in `hashes` mode only
its sha256 is kept; in `full` mode the text goes to this separate store, deleted at expiry, after which capture is back
to `hashes`. Enabling support mode on a release build needs the adult PIN and the student's confirmation (the UI and
settings flows arrive with M5; here the store enforces the expiry)."""

from __future__ import annotations

import datetime
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

UTC = datetime.UTC


@dataclass
class PayloadStore:
    root: Path
    full_hours: float = 24.0
    clock: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(UTC)
    expires_at: dict[str, datetime.datetime] = field(default_factory=dict)  # profile -> end of support mode

    def mode(self, profile: str) -> str:
        expiry = self.expires_at.get(profile)
        return "full" if expiry is not None and self.clock() < expiry else "hashes"

    def enable_full(self, profile: str) -> datetime.datetime:
        expiry = self.clock() + datetime.timedelta(hours=self.full_hours)
        self.expires_at[profile] = expiry
        return expiry

    def put(self, profile: str, trace_id: str, kind: str, text: str) -> str:
        """Returns the payload's sha256; the text is stored only while support mode is on for the profile."""
        digest = hashlib.sha256(text.encode()).hexdigest()
        if self.mode(profile) == "full":
            folder = self.root / profile
            folder.mkdir(parents=True, exist_ok=True)
            entry = {"trace_id": trace_id, "kind": kind, "sha256": digest, "text": text}
            with (folder / f"{trace_id}.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return digest

    def purge(self) -> int:
        """Delete every profile's payloads whose support mode has expired; capture returns to `hashes`."""
        removed = 0
        now = self.clock()
        for profile, expiry in list(self.expires_at.items()):
            if now >= expiry:
                for path in (self.root / profile).glob("*.jsonl") if (self.root / profile).is_dir() else []:
                    path.unlink()
                    removed += 1
                del self.expires_at[profile]
        return removed

    def files(self) -> list[Path]:
        return sorted(self.root.rglob("*.jsonl")) if self.root.is_dir() else []
