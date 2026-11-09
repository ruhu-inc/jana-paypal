"""The content scan for age-appropriate content (PRV-17) and the sensitivity lint (CUR-25, CUR-27).

Every text of every pack record is scanned before the pack builds: the input-guard categories of TABLE PRV-13a that
content can fall into (`distress`, `harmful`, `sexual_or_violent`, `personal_data`, `injection`) and the sensitivity
word lists of CUR-27 (stereotypes, unsafe procedures, brands, political content, real people). A record that trips
any of them does not build. The word lists live in the shared reference pack, `content/curriculum/ref/common/
scan.yaml` (`pack.ref`), so problem reports after release extend them without a code change.

Sensitive topics (reproduction, disease, drugs and the like) never fail a build: they mark the records the content
safety review must sample in full (CUR-27, `pack_builder.safety_review`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from agent_core.store.packs import row_id

COMMON = "common"  # content/curriculum/ref/common: the cross-subject part of pack.ref
SCAN_FILE = "scan.yaml"
CONTEXT_FILE = "context.yaml"
_ID = re.compile(r"^[a-z]{2,5}\.[a-z0-9_.:-]+$")  # record ids, check ids and the like are not wording


REPOSITORY_COMMON = Path(__file__).resolve().parents[3] / "content" / "curriculum" / "ref" / COMMON


def common_ref(pack_dir: Path | None) -> Path:
    """The shared reference lists beside a pack's own conventions (`content/curriculum/ref/common`); a pack built
    outside a content tree (a test copy) uses this repository's."""
    if pack_dir is not None:
        beside = pack_dir.parent.parent / "ref" / COMMON
        if beside.is_dir():
            return beside
    return REPOSITORY_COMMON


@dataclass(frozen=True)
class Category:
    group: str  # guard (TABLE PRV-13a) or sensitivity (CUR-27)
    name: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class Lists:
    categories: tuple[Category, ...]
    sensitive_topics: re.Pattern[str] | None
    allow: tuple[str, ...]

    def hits(self, text: str) -> list[tuple[str, str]]:
        """(category, matched words) for each category the text trips, allowed phrases removed first."""
        for phrase in self.allow:
            text = re.sub(re.escape(phrase), " ", text, flags=re.IGNORECASE)
        found = []
        for category in self.categories:
            match = category.pattern.search(text)
            if match is not None:
                found.append((category.name, match.group(0)))
        return found

    def sensitive(self, text: str) -> bool:
        return self.sensitive_topics is not None and self.sensitive_topics.search(text) is not None


def _compile(patterns: Iterable[str], sensitive: bool) -> re.Pattern[str]:
    joined = "|".join(f"(?:{p})" for p in patterns)
    return re.compile(rf"(?<![\w-])(?:{joined})(?![\w])", 0 if sensitive else re.IGNORECASE)


def parse_lists(data: Mapping[str, Any]) -> Lists:
    categories = []
    for group in ("guard", "sensitivity"):
        for name, entry in (data.get(group) or {}).items():
            patterns = entry["patterns"] if isinstance(entry, Mapping) else entry
            sensitive = isinstance(entry, Mapping) and entry.get("case") == "sensitive"
            if patterns:
                categories.append(Category(group, str(name), _compile(patterns, sensitive)))
    topics = data.get("sensitive_topics") or []
    allow = tuple(str(a["phrase"] if isinstance(a, Mapping) else a) for a in data.get("allow") or [])
    return Lists(tuple(categories), _compile(topics, False) if topics else None, allow)


@cache
def _load(path: Path, stamp: int) -> Lists:  # `stamp` (the file's mtime) keys the cache, so an edited list reloads
    return parse_lists(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def lists_for(pack_dir: Path | None) -> Lists | None:
    """The scan lists of the reference pack for `pack_dir`, or None when there are none (`scan_problems` then fails
    the pack)."""
    path = common_ref(pack_dir) / SCAN_FILE
    return _load(path.resolve(), path.stat().st_mtime_ns) if path.is_file() else None


# Where a source's licence metadata records its address (CUR-24): a URL there is required metadata, not wording.
LOCATORS = frozenset({"url", "svg_path"})


def texts(record: Any, path: str = "") -> Iterable[tuple[str, str]]:
    """(field path, text) for every string of a record that is wording, not an identifier or a source locator."""
    if isinstance(record, str):
        if not _ID.match(record):
            yield path or "text", record
    elif isinstance(record, Mapping):
        for key, value in record.items():
            if key not in LOCATORS:
                yield from texts(value, f"{path}.{key}" if path else str(key))
    elif isinstance(record, list):
        for index, value in enumerate(record):
            yield from texts(value, f"{path}[{index}]")


def scan_problems(tables: Mapping[str, list[dict[str, Any]]], lists: Lists | None) -> list[str]:
    """One problem per record field that trips the scan; a pack with no scan lists cannot be scanned and fails."""
    if lists is None:
        return [f"no content scan lists ({COMMON}/{SCAN_FILE} in the reference pack); the pack cannot be scanned "
                "(PRV-17)"]  # fmt: skip
    problems = []
    for table, records in tables.items():
        if table in ("search_fts", "search_vectors"):
            continue
        for record in records:
            try:
                rid = row_id(table, record)
            except (KeyError, TypeError):
                continue
            for where, text in texts(record):
                for category, words in lists.hits(text):
                    problems.append(f"{rid}: {where} trips the content scan ({category}: {words!r}) (PRV-17)")
    return problems


def sensitive_records(tables: Mapping[str, list[dict[str, Any]]], lists: Lists) -> set[str]:
    """Ids of the records whose text touches a sensitive topic (CUR-27)."""
    found = set()
    for table, records in tables.items():
        for record in records:
            try:
                rid = row_id(table, record)
            except (KeyError, TypeError):
                continue
            if any(lists.sensitive(text) for _, text in texts(record)):
                found.add(rid)
    return found
