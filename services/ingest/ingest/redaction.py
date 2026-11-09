"""The ingest's redaction scanner (OBS-19, OPS-24): what must never be stored here, found by pattern.

The device already applies TABLE OBS-19a at the writer and refuses a crash report its own scanner flags (OBS-17);
this is the service's second look, so a report or bundle that slipped through is refused instead of stored. It runs
over normalised text (NFKC, invisible format characters removed) so hidden characters cannot split a match. A
finding names its kind, never the matched text."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+"),
    # International (+ country code) or national (leading 0) numbers of at least eight digits, so a year, a version
    # or a list of small numbers is never one.
    "phone": re.compile(
        r"(?<![\w.])\+\d[\d ().-]{7,}\d(?!\w)"
        r"|(?<![\w.])\(?0\d{1,3}\)?[ .-]?\d{3,4}[ .-]?\d{3,4}(?!\w)"
    ),
    "profile_id": re.compile(r"(?<![^\W_])prf_?[0-9A-HJKMNP-TV-Z]{26}(?![^\W_])", re.I),
    # Device and licence ids in the support metadata's form; a digit is required so setting names do not match.
    "device_id": re.compile(r"(?<![\w-])dev_(?=[\w-]*\d)[\w-]{8,}"),
    "licence_id": re.compile(r"(?<![\w-])lic_(?=[\w-]*\d)[\w-]{6,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "provider_key": re.compile(r"AIza[0-9A-Za-z_\-]{35}|sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"),
    "bearer_token": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    "credential_value": re.compile(
        r"(?i)(?<![\w])(?:password|passphrase|secret|api[_-]?key|access[_-]?token|token|pin)[\"']?\s*[:=]\s*[\"']?"
        r"(?!\[(?:removed|redacted)\])([^\s\"',}]{6,})"
    ),
    # A home directory that still names its user (OBS-19a: the home directory is replaced by `~`).
    "home_path": re.compile(
        r"(?:/Users/|/home/|[A-Za-z]:(?:\\\\|\\|/)Users(?:\\\\|\\|/))(?!Shared\b|Public\b)[^\\/\s\"']+"
    ),
}
# Support bundles legitimately carry the device and licence ids of their ticket metadata (OPS-22).
BUNDLE_ALLOWED = frozenset({"device_id", "licence_id"})


def normalise(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text)
    return "".join(c for c in folded if unicodedata.category(c) != "Cf")


def findings(value: Any, allowed: frozenset[str] = frozenset()) -> list[str]:
    """The kinds of forbidden content in `value` (text or JSON-shaped data), in pattern order."""
    text = normalise(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    found = []
    for kind, pattern in PATTERNS.items():
        if kind in allowed:
            continue
        if kind == "credential_value":
            # A number after a setting's name is a setting, not a secret.
            if any(any(c.isalpha() for c in m.group(1)) for m in pattern.finditer(text)):
                found.append(kind)
        elif pattern.search(text):
            found.append(kind)
    return found
