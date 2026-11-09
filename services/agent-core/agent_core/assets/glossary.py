"""The course definitions the Coach is given and held to (PRM-09 tier 5, AGT-10).

Tier 5 carries "a notation glossary that overrides the model's own definitions". The pack knowledge components name
their glossary terms; `content/agent/glossary/<lang>.yaml` defines them. The engine pushes the entries in play for
the open problem (its KCs, or a cue in the question) and checks each reply for a phrase an entry in play marks wrong
("Average Cost Value" for AVCO, "independent" for a draw without replacement) before the student sees it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

MAX_PUSHED = 8  # entries in tier 5 per call: the topic's key terms, never the whole glossary
DEFINITION_MAX = 240
# A wrong phrase right after a negation is the right claim ("these are not independent").
_NEGATION = re.compile(r"\b(?:not|never|no|isn't|aren't|wasn't|weren't|cannot|can't)\s+(?:\w+\s+){0,1}$", re.I)


class GlossaryError(ValueError):
    pass


@lru_cache(maxsize=4096)
def _phrase(text: str) -> re.Pattern[str]:
    """A phrase as whole words, any case, with an optional plural."""
    words = [re.escape(w) for w in text.split()]
    return re.compile(r"(?<![\w.])" + r"\s+".join(words) + r"(?:s|es)?(?![\w])", re.I)


@dataclass(frozen=True)
class Entry:
    term: str
    definition: str
    kcs: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    cues: tuple[str, ...] = ()
    wrong: tuple[str, ...] = ()

    def cued(self, statement: str) -> bool:
        return any(_phrase(c).search(statement) for c in self.cues)

    def named(self, statement: str) -> bool:
        return any(_phrase(n).search(statement) for n in (self.term, *self.aliases))

    def contradiction(self, text: str, statement: str) -> str | None:
        """The first wrong phrase the text names outside a negation, or None. An entry with cues checks only when a
        cue is in the question."""
        if self.cues and not self.cued(statement):
            return None
        for phrase in self.wrong:
            for match in _phrase(phrase).finditer(text):
                if not _NEGATION.search(text[: match.start()]):
                    return match.group(0)
        return None


@dataclass(frozen=True)
class Glossary:
    entries: tuple[Entry, ...] = ()
    version: str = ""

    @classmethod
    def load(cls, path: Path) -> Glossary:
        """The glossary file, or an empty glossary when the language has none."""
        if not path.is_file():
            return cls()
        data = yaml.safe_load(path.read_text()) or {}
        entries = []
        for raw in data.get("terms") or []:
            entry = Entry(
                term=str(raw["term"]),
                definition=" ".join(str(raw["definition"]).split()),
                kcs=tuple(str(k) for k in raw.get("kcs") or ()),
                aliases=tuple(str(a) for a in raw.get("aliases") or ()),
                cues=tuple(str(c) for c in raw.get("cues") or ()),
                wrong=tuple(str(w) for w in raw.get("wrong") or ()),
            )
            if not entry.kcs or not entry.definition or len(entry.definition) > DEFINITION_MAX:
                raise GlossaryError(f"{path.name}: {entry.term}: needs kcs and a definition of at most "
                                    f"{DEFINITION_MAX} characters")  # fmt: skip
            entries.append(entry)
        return cls(tuple(entries), str(data.get("version", "")))

    def in_play(self, kc_ids: Iterable[str], statement: str) -> tuple[Entry, ...]:
        """The entries of the KCs in play and those a cue in the question brings in, at most MAX_PUSHED: first the
        terms the question names or cues, then those holding a known wrong phrase, then file order."""
        kcs = set(kc_ids)
        found = [e for e in self.entries if kcs.intersection(e.kcs) or (e.cues and e.cued(statement))]
        found.sort(key=lambda e: (not (e.named(statement) or (bool(e.cues) and e.cued(statement))), not e.wrong))
        return tuple(found[:MAX_PUSHED])


def lines(entries: Iterable[Entry]) -> tuple[str, ...]:
    """Tier-5 glossary lines (PRM-09)."""
    return tuple(f"- {e.definition}" for e in entries)


def contradictions(texts: Iterable[str], entries: Iterable[Entry], statement: str) -> list[str]:
    """Each named phrase the course definitions in play contradict, as "phrase: the definition" for the nudge."""
    found: list[str] = []
    for entry in entries:
        for text in texts:
            phrase = entry.contradiction(text, statement) if text else None
            if phrase is not None:
                found.append(f'"{phrase}" contradicts the course definition. {entry.definition}')
                break
    return found
