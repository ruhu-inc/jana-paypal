"""Tone in code (PED-20, PED-21): the banned lexicon as a check over any tutor text, and the word cap by level.

The critic (H10) repairs a reply with `apply_lexicon` before it is shown; `banned_phrases` finds what is left, for the
persona and skill-eval corpora. The cap is per level (O level, A level, the reading support set) from configuration;
a reply over it keeps whole sentences from the start and always its closing question, so a long reply is shortened,
never cut mid-sentence or stripped of the one thing it asks."""

from __future__ import annotations

import re

from agent_core.config import Settings
from agent_core.engine.pedagogy import BANNED, word_count

_SENTENCE = re.compile(r"[^.?!]+[.?!]+[\"')\]]*|[^.?!]+$")


def banned_phrases(text: str) -> list[str]:
    """Every banned phrase (PED-20) present in `text`, case-insensitive, at a word start."""
    lowered = text.replace(chr(0x2019), "'")  # a typographic apostrophe reads as a plain one
    return [phrase for phrase in BANNED if re.search(rf"\b{re.escape(phrase)}", lowered, re.I)]


def word_cap(settings: Settings, level: str, reading_level: str = "standard") -> int:
    """TABLE PED-21a: the reading support set, else the level's cap."""
    if reading_level == "support":
        return int(settings.coach_words_per_turn_support)
    return int(settings.coach_words_per_turn_a if level.upper() == "A" else settings.coach_words_per_turn)


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.findall(text) if s.strip()]


def cap_words(text: str, cap: int) -> str:
    """Within `cap` words: whole sentences from the start, then the last question if one was cut."""
    if word_count(text) <= cap:
        return text
    parts = sentences(text)
    question = next((s for s in reversed(parts) if s.endswith("?")), None)
    room = cap - (word_count(question) if question else 0)
    kept: list[str] = []
    for sentence in parts:
        if sentence is question:
            break
        if word_count(" ".join([*kept, sentence])) > room:
            break
        kept.append(sentence)
    if question is not None and word_count(question) <= cap:
        kept.append(question)
    out = " ".join(kept)
    if not out:  # one sentence longer than the cap: its first words, closed
        out = " ".join(text.split()[:cap]).rstrip(",;:") + "…"
    return out


# --- the spoken form of a voice turn (PRM-26, VOC-07, VOC-37) --------------------------------------------------------

# Cue markers (VOC-10; D103 write cues): stripped before the sidecar, so allowed here and not counted as length.
POINTER = re.compile(r'<point\s+at="[^"<>]{1,80}"\s*/>|<write\s+at="[^"<>]{1,80}"\s*>|</write\s*>')
SPOKEN_MARKUP = re.compile(r"[\\$^_{}<>`*#|~\[\]]|^\s*[-+]\s", re.M)  # LaTeX and markdown marks


def fit_spoken(text: str, max_chars: int, max_sentences: int) -> str | None:
    """The spoken form within its limits: whole sentences from the start, at most `max_sentences` and `max_chars`.
    None when it carries LaTeX or markdown, or when its first sentence alone is over the limit: the adapter then speaks
    the verbaliser's reading of `say` (VOC-07, VOC-08)."""
    if SPOKEN_MARKUP.search(POINTER.sub("", text)):
        return None
    kept: list[str] = []
    for sentence in (s.strip() for s in _SENTENCE.findall(text.strip())):
        if not sentence or len(kept) >= max_sentences or len(POINTER.sub("", " ".join([*kept, sentence]))) > max_chars:
            break
        kept.append(sentence)
    return " ".join(kept) or None
