"""Personal-data detection shared by every check that decides what may leave the device (MDL-25, PRV-20, PRV-25).

One place for the text normalisation and the contact-detail patterns, so the egress check (`net.redaction`) and the
engine's volunteered-data redaction can match the same things. Text is normalised before any pattern runs: NFKC
(fullwidth letters, digits and at signs become plain ones), invisible format characters such as zero-width spaces
removed, and the Cyrillic and Greek letters that look like Latin ones folded to Latin, so a name or an id cannot slip
past a check by being spelled with lookalike or hidden characters. Patterns never return the matched text."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# Cyrillic and Greek letters drawn like Latin ones (a small, fixed confusables table), by code point.
_LOOKALIKES = str.maketrans({
    0x0410: "A", 0x0412: "B", 0x0415: "E", 0x041A: "K", 0x041C: "M", 0x041D: "H", 0x041E: "O", 0x0420: "P",
    0x0421: "C", 0x0422: "T", 0x0425: "X", 0x0430: "a", 0x0435: "e", 0x043E: "o", 0x0440: "p", 0x0441: "c",
    0x0443: "y", 0x0445: "x", 0x0456: "i", 0x0458: "j", 0x0455: "s", 0x0391: "A", 0x0392: "B", 0x0395: "E",
    0x0396: "Z", 0x0397: "H", 0x0399: "I", 0x039A: "K", 0x039C: "M", 0x039D: "N", 0x039F: "O", 0x03A1: "P",
    0x03A4: "T", 0x03A5: "Y", 0x03A7: "X", 0x03BF: "o", 0x03BD: "v",
})  # fmt: skip
_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def normalise(text: str) -> str:
    """The text as the checks read it: `\\uXXXX` escapes decoded, NFKC, format characters (category Cf: zero-width
    spaces and joiners, soft hyphens, direction marks) removed, and Latin lookalikes folded."""
    decoded = _ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
    folded = unicodedata.normalize("NFKC", decoded)
    visible = "".join(c for c in folded if unicodedata.category(c) != "Cf")
    return visible.translate(_LOOKALIKES)


# Patterns over normalised text. `\d` and `\w` are Unicode-aware, so other scripts' digits and letters count.
CONTACT: dict[str, re.Pattern[str]] = {
    # The domain ends in a label of letters (a real top-level domain), so a versioned reference such as
    # `pack.mth.o@1.0.8` in a cited passage is never taken for an address.
    "email": re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{2,}(?![\w-])"),
    # International (+country code) or national (a leading 0, the area code optionally in brackets), in groups split
    # by spaces, dots or hyphens, or none: at least eight digits, so a year or a list of numbers is never one.
    "phone": re.compile(
        r"(?<![\w.])\+\d[\d ().-]{7,}\d(?![\w])"
        r"|(?<![\w.])\(?0\d{1,3}\)?[ .-]?\d{3,4}[ .-]?\d{3,4}(?![\w])"
    ),
    # A GhanaPostGPS digital address: region and district letters, then two groups of digits (GA-123-4567).
    "digital_address": re.compile(r"(?<![\w-])[A-Za-z]{2}[ -]\d{3,4}[ -]\d{4}(?![\w-])|(?<!\w)[A-Z]{2}\d{7,8}(?!\w)"),
    # A self-introduction with any name after it, in any case and any script.
    "name": re.compile(
        r"\b(?:my\s+(?:full\s+)?name\s+is|my\s+name's|i\s+am\s+called|i'm\s+called)\s+[^\W\d_]{2,}", re.I
    ),
}


def contact_kinds(text: str) -> list[str]:
    """The kinds of contact detail in `text` (after normalising it), never the matched text."""
    clean = normalise(text)
    return [kind for kind, pattern in CONTACT.items() if pattern.search(clean)]


def term_pattern(terms: list[str]) -> re.Pattern[str] | None:
    """Whole-term matches for identifying strings over normalised text, case-insensitive, with the term's words
    joined by any run of spaces, underscores, dots or hyphens (`Ama Owusu`, `ama_owusu`, `AMA  OWUSU`)."""
    parts = []
    for term in terms:
        words = [w for w in re.split(r"[\s_.\-]+", normalise(term).strip()) if w]
        if words and len("".join(words)) >= 2:
            parts.append(r"[\s_.\-]*".join(re.escape(w) for w in words))
    if not parts:
        return None
    parts.sort(key=len, reverse=True)
    return re.compile(r"(?<![^\W_])(?:" + "|".join(parts) + r")(?![^\W_])", re.I)


PATTERNS: dict[str, re.Pattern[str]] = {
    "profile_id": re.compile(r"(?<![^\W_])prf_?[0-9A-HJKMNP-TV-Z]{26}(?![^\W_])", re.I),
    **CONTACT,
}


@dataclass(frozen=True)
class Finding:
    kind: str  # "term" or a pattern name; never the matched text


class Redactor:
    """The same identifying-term and contact checks for API inputs and outgoing cloud requests."""

    def __init__(self, terms: Iterable[str] = ()) -> None:
        self._terms = term_pattern([t for t in terms if t])

    def findings(self, body: Any) -> list[Finding]:
        text = normalise(body if isinstance(body, str) else json.dumps(body, ensure_ascii=False))
        found = [Finding("term")] if self._terms is not None and self._terms.search(text) else []
        found += [Finding(name) for name, pattern in PATTERNS.items() if pattern.search(text)]
        return found

    def blocks(self, body: Any) -> bool:
        return bool(self.findings(body))
