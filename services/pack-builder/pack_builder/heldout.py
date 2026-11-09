"""Held-out items never ship (CUR-22, EVL-18, EVL-24).

Held-out items live only in the encrypted store under `evals/held-out/`, which only the release pipeline decrypts.
Beside each sealed suite sits a list of hashes: the sha256 of every item id (`id:` prefix) and of every content string
of 24 or more characters, normalised (whitespace collapsed, lower case). A student pack build fails when any of its
record ids or texts matches one, so neither the item nor a near copy of its wording can ship. Drafting requests are
checked against the same list before they leave the machine (AUT-20).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MIN_TEXT = 24  # shorter strings (units, enum values) are not item content
SHINGLE = 8  # words: a request that repeats any 8 consecutive words of a held-out text is blocked (AUT-20)


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def id_hash(record_id: str) -> str:
    return hashlib.sha256(f"id:{record_id}".encode()).hexdigest()


def text_hashes(value: Any) -> set[str]:
    """sha256 of every item-content string (normalised) inside a value. Identifiers (KC and misconception ids, check
    ids, enum values) have no spaces and are not wording: records share them by design, and record ids are matched
    by `id_hash` instead."""
    found: set[str] = set()
    if isinstance(value, str):
        text = _normalise(value)
        if len(text) >= MIN_TEXT and " " in text:
            found.add(hashlib.sha256(text.encode()).hexdigest())
    elif isinstance(value, Mapping):
        for v in value.values():
            found |= text_hashes(v)
    elif isinstance(value, list):
        for v in value:
            found |= text_hashes(v)
    return found


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def shingle_hashes(text: str) -> set[str]:
    words = _normalise(text).split(" ")
    return {hashlib.sha256(("sh:" + " ".join(words[i : i + SHINGLE])).encode()).hexdigest()
            for i in range(len(words) - SHINGLE + 1)}  # fmt: skip


def item_hashes(items: Iterable[Mapping[str, Any]]) -> set[str]:
    """Ids, whole texts and 8-word shingles of the texts of held-out items."""
    found: set[str] = set()
    for item in items:
        if "id" in item:
            found.add(id_hash(str(item["id"])))
        found |= text_hashes(item)
        for text in _strings(item):
            found |= shingle_hashes(text)
    return found


@dataclass(frozen=True)
class HeldOut:
    hashes: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def load(cls, directory: Path) -> HeldOut:
        found: set[str] = set()
        for path in sorted(directory.glob("*.hashes.json")) if directory.is_dir() else []:
            found.update(json.loads(path.read_text()))
        return cls(frozenset(found))

    def conflicts(self, tables: Mapping[str, list[dict[str, Any]]]) -> list[str]:
        """Problems for every record whose id or wording matches a held-out item (CUR-22)."""
        if not self.hashes:
            return []
        problems = []
        for table in ("items", "worked_examples", "lessons", "item_templates"):
            for record in tables.get(table, []):
                if id_hash(str(record.get("id"))) in self.hashes:
                    problems.append(f"{record['id']}: a held-out item never ships in a student pack (CUR-22)")
                elif text_hashes(record) & self.hashes:
                    problems.append(f"{record['id']}: repeats the wording of a held-out item (CUR-22)")
        return problems

    def blocks(self, text: str) -> bool:
        """Whether an outgoing text carries a held-out item's id or wording (AUT-20)."""
        if not self.hashes:
            return False
        if text_hashes(text) & self.hashes or shingle_hashes(text) & self.hashes:
            return True
        lines = {line.strip(" -:'\"") for line in text.splitlines()}
        if any(text_hashes(line) & self.hashes for line in lines):
            return True
        ids = re.findall(r"[a-z]{2,4}\.[a-z]{3}\.[a-z0-9]+(?:\.[a-z0-9_]+)+", text)
        return any(id_hash(i) in self.hashes for i in ids)


# --- the encrypted store (EVL-18) ---------------------------------------------------------------------------------

KEY_ENV = "JANA_HELD_OUT_KEY"  # base64 of a 256-bit key; held by the release pipeline only


def seal(items: list[dict[str, Any]], suite: str, key_b64: str, out_dir: Path) -> Path:
    """Encrypt a suite's held-out items (AES-256-GCM, the suite name as associated data) and write their id and
    text hashes beside them."""
    import base64
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    out_dir.mkdir(parents=True, exist_ok=True)
    nonce = os.urandom(12)
    plain = json.dumps(items, sort_keys=True).encode()
    path = out_dir / f"{suite}.enc"
    path.write_bytes(nonce + AESGCM(base64.b64decode(key_b64)).encrypt(nonce, plain, suite.encode()))
    (out_dir / f"{suite}.hashes.json").write_text(json.dumps(sorted(item_hashes(items)), indent=1) + "\n")
    return path


def open_sealed(suite: str, key_b64: str, out_dir: Path) -> list[dict[str, Any]]:
    import base64

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    path = out_dir / f"{suite}.enc"
    if not path.is_file():
        return []
    sealed = path.read_bytes()
    items: list[dict[str, Any]] = json.loads(AESGCM(base64.b64decode(key_b64)).decrypt(sealed[:12], sealed[12:],
                                                                                       suite.encode()))  # fmt: skip
    return items
