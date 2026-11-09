"""The licence gate and the content-credits page (CUR-23, CUR-24).

A record's own licence and every source's licence must be on the allowed list; share-alike sources are allowed only
for diagrams, kept as separate assets. Every external source needs its title, URL, licence, attribution text,
modifications and retrieval date; the credits page is generated from those fields, and a missing one fails the build.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ALLOWED = frozenset({"LicenseRef-Own", "CC0-1.0", "LicenseRef-PublicDomain", "CC-BY-3.0", "CC-BY-4.0"})
SHARE_ALIKE = frozenset({"CC-BY-SA-3.0", "CC-BY-SA-4.0"})
SOURCE_FIELDS = ("title", "url", "licence", "attribution", "modifications", "retrieved")


def _sources(table: str, record: Mapping[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    if table == "diagrams":
        return record.get("licence"), list(record.get("sources", []))
    provenance = record.get("provenance") or {}
    return provenance.get("licence"), list(provenance.get("sources", []))


def gate_problems(table: str, record: Mapping[str, Any], rid: str) -> list[str]:
    problems = []
    own, sources = _sources(table, record)
    allowed = ALLOWED | SHARE_ALIKE if table == "diagrams" else ALLOWED
    if own is not None and own not in allowed:
        problems.append(f"{rid}: licence {own} is not allowed for {table}")
    for source in sources:
        licence = source.get("licence")
        if licence in SHARE_ALIKE and table != "diagrams":
            problems.append(f"{rid}: share-alike source {source.get('title')!r} is allowed only for diagrams")
        elif licence not in allowed:
            problems.append(f"{rid}: source {source.get('title')!r} has licence {licence}, which is not allowed")
    if table == "diagrams" and own != "LicenseRef-Own" and not str(record.get("attribution") or "").strip():
        problems.append(f"{rid}: an imported diagram needs its attribution")
    return problems


def attribution_problems(table: str, record: Mapping[str, Any], rid: str) -> list[str]:
    _, sources = _sources(table, record)
    problems = []
    for n, source in enumerate(sources, 1):
        for name in SOURCE_FIELDS:
            if not str(source.get(name) or "").strip():
                problems.append(f"{rid}: source {n} has no {name}")
    return problems


def credits(tables: Mapping[str, list[dict[str, Any]]], row_id: Any) -> list[dict[str, Any]]:
    """One entry per external source, with the records that use it (Settings → About → Content credits)."""
    by_source: dict[tuple[str, str], dict[str, Any]] = {}
    for table, records in tables.items():
        for record in records:
            _, sources = _sources(table, record)
            for source in sources:
                key = (source["url"], source["title"])
                entry = by_source.setdefault(key, {**{f: source[f] for f in SOURCE_FIELDS}, "used_by": []})
                entry["used_by"].append(row_id(table, record))
            if table == "diagrams" and record.get("licence") != "LicenseRef-Own" and not record.get("sources"):
                entry = {"title": record["title"], "licence": record["licence"],
                         "attribution": record["attribution"], "used_by": [record["id"]]}  # fmt: skip
                by_source.setdefault((record["id"], record["title"]), entry)
    return [by_source[k] for k in sorted(by_source)]


def credits_html(entries: list[dict[str, Any]], pack_title: str) -> str:
    """A static page the app shows offline."""
    from html import escape

    rows = []
    for e in entries:
        link = f'<a href="{escape(e["url"])}">{escape(e["title"])}</a>' if e.get("url") else escape(e["title"])
        details = [escape(e["attribution"]), f"Licence: {escape(e['licence'])}"]
        if e.get("modifications"):
            details.append(f"Changes: {escape(e['modifications'])}")
        if e.get("retrieved"):
            details.append(f"Retrieved {escape(e['retrieved'])}")
        rows.append(f"<li>{link}<br>{'<br>'.join(details)}</li>")
    body = "<ul>" + "".join(rows) + "</ul>" if rows else "<p>All content in this pack is our own original work.</p>"
    return (f"<!doctype html><meta charset=\"utf-8\"><title>Content credits: {escape(pack_title)}</title>"
            f"<h1>Content credits</h1><h2>{escape(pack_title)}</h2>{body}\n")  # fmt: skip
