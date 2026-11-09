"""The content problem report inbox (AUT-38): students' reports on content records (CUR-35) as counts by category and
record id, in the authoring home (`analytics/inbox.json`).

Two sources reach it. Opt-in telemetry carries only a category and a record id per report (OBS-20; the
`content_reports` counts are proposed in contract-additions-m33b.json until the telemetry contract adopts them), so
those arrive as counts. A support export the student authorized (OBS-18) may carry a report's free text: the text is
kept only where the report says the student allowed sending it, and otherwise dropped with only the count kept.
Anything else is refused: there is no teacher inbox or class transport (CUR-35) and no school feed (D52).

Each inbox row can open a draft change on its record (the studio opens the record; saving it is the ordinary edit
transition of CUR-19). A confirmed report enters the internal review queue (CUR-26) by the reviewer's choice; nothing
here edits a record.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pack_builder.accounts import home, now_iso
from pack_builder.studio_io import atomic_write, locked

CATEGORIES = ("wrong_answer", "unclear", "typo", "offensive", "other")
# The content records a student can report (CUR-35): ids of the content-pack contract, nothing else.
RECORD_ID = re.compile(
    r"(itm|wex|itpl)\.(mth|phy|chm|bio|acc)\.(o|a)\.[a-z0-9_]+\.[0-9]{4}"
    r"|(les|kc)\.(mth|phy|chm|bio|acc)\.(o|a)\.[a-z0-9_]+\.[a-z0-9_]+"
    r"|mc\.(mth|phy|chm|bio|acc)\.(o|a)\.[a-z0-9_]+"
    r"|dgm\.(mth|phy|chm|bio|acc)\.[a-z0-9_]+\.v[0-9]+"
    r"|mck\.(mth|phy|chm|bio|acc)\.(o|a)\.[0-9]{4}"
)
NOTE_MAX = 1000


class ReportRefused(ValueError):
    """A payload that is not content problem report counts or an authorized support export."""


def _record(value: Any) -> str:
    if not isinstance(value, str) or not RECORD_ID.fullmatch(value):
        raise ReportRefused("a report names a content record id")
    return value


def _category(value: Any) -> str:
    if value not in CATEGORIES:
        raise ReportRefused(f"a report's category is one of {', '.join(CATEGORIES)}")
    return str(value)


def reports(payload: Any) -> list[tuple[str, str, int, str | None]]:
    """(record id, category, count, allowed note) rows from one payload, or a refusal of the whole payload."""
    if not isinstance(payload, dict):
        raise ReportRefused("a report payload is a JSON object")
    kind = payload.get("kind")
    out: list[tuple[str, str, int, str | None]] = []
    if kind == "content_reports":
        if set(payload) != {"kind", "counts"} or not isinstance(payload["counts"], list):
            raise ReportRefused("telemetry report counts are {kind, counts}: a category and a record id, no text")
        for row in payload["counts"]:
            if not isinstance(row, dict) or set(row) != {"record_id", "category", "count"}:
                raise ReportRefused("a count is {record_id, category, count}")
            if type(row["count"]) is not int or row["count"] < 1:
                raise ReportRefused("a count is a positive whole number")
            out.append((_record(row["record_id"]), _category(row["category"]), row["count"], None))
        return out
    if kind == "support_export":
        if set(payload) != {"kind", "reports"} or not isinstance(payload["reports"], list):
            raise ReportRefused("a support export is {kind, reports}")
        for row in payload["reports"]:
            if not isinstance(row, dict) or not set(row) <= {"record_id", "category", "note", "allow_support_export"}:
                raise ReportRefused("a supported report is {record_id, category, note, allow_support_export}")
            note = row.get("note")
            allowed = row.get("allow_support_export") is True and isinstance(note, str) and note.strip() != ""
            out.append((_record(row.get("record_id")), _category(row.get("category")), 1,
                        note.strip()[:NOTE_MAX] if allowed and isinstance(note, str) else None))  # fmt: skip
        return out
    raise ReportRefused(f"{kind!r} is not a content problem report source: telemetry counts or a support export the "
                        "student authorized; there is no teacher inbox or school feed (CUR-35, D52)")  # fmt: skip


class Inbox:
    def __init__(self, root: Path | None = None) -> None:
        self.directory = (root or home()) / "analytics"
        self.path = self.directory / "inbox.json"

    def read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text()) if self.path.is_file() else {"records": {}, "accepted": []}

    def intake(self, payload: Any) -> dict[str, Any]:
        rows = reports(payload)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with locked(self.directory):
            data = self.read()
            if digest in data["accepted"]:
                raise ReportRefused("this payload was already taken in")
            for record_id, category, count, note in rows:
                entry = data["records"].setdefault(record_id, {"counts": {}, "notes": [], "drafts": []})
                entry["counts"][category] = entry["counts"].get(category, 0) + count
                if note is not None:
                    entry["notes"].append({"category": category, "note": note, "received_at": now_iso()})
            data["accepted"].append(digest)
            atomic_write(self.path, json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
        return {"kind": payload["kind"], "reports": sum(r[2] for r in rows), "records": len({r[0] for r in rows})}

    def rows(self) -> list[dict[str, Any]]:
        """The inbox: one row per record, most reported first."""
        records: Mapping[str, Any] = self.read()["records"]
        out = [{"record_id": k, "counts": dict(v["counts"]), "total": sum(v["counts"].values()),
                "notes": list(v["notes"]), "drafts": list(v["drafts"])} for k, v in records.items()]  # fmt: skip
        return sorted(out, key=lambda r: (-r["total"], r["record_id"]))

    def opened(self, record_id: str, version: Any, author: str) -> None:
        """Note that a draft change was opened on the record from its reports."""
        with locked(self.directory):
            data = self.read()
            if record_id not in data["records"]:
                raise KeyError("That record has no reports in the inbox.")
            data["records"][record_id]["drafts"].append({"version": version, "by": author, "at": now_iso()})
            atomic_write(self.path, json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
