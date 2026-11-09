"""The stored form of a timed transcript line (SPEC §20.3, W9): `[m:ss] text`.

A YouTube transcript's chunks are stored with each line keeping its own moment as a `[m:ss]` prefix (`h:mm:ss` from
one hour), so every citation and companion excerpt can name and open its moment. These helpers read and write that
prefix only; the paste parser that builds the chunks is `engine/study_links.py`. The memory queries (`study_search`'s
excerpts) and the engine both need them, so they live below both layers (ARC-18)."""

from __future__ import annotations

import re

_PREFIX = re.compile(r"^\[(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\]\s?")
_ANY_MOMENT = re.compile(r"\[((?:\d{1,2}:)?\d{1,2}:\d{2})\]")


def moment_label(ms: int) -> str:
    """A moment's label, as the panel writes it: `m:ss` under an hour, `h:mm:ss` from one hour."""
    seconds = max(0, int(ms)) // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def to_ms(hours: str | None, minutes: str, seconds: str) -> int:
    """A parsed `h:mm:ss` or `m:ss` timestamp in milliseconds."""
    return (int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)) * 1000


def line_moment(line: str) -> int | None:
    """The moment a chunk line carries as its `[m:ss]` prefix, or None."""
    found = _PREFIX.match(line)
    return to_ms(*found.groups()) if found else None


def moment_at(text: str, offset: int) -> int | None:
    """The moment of the line of `text` that holds code point `offset` (a citation's start), from its prefix."""
    if offset < 0 or offset > len(text):
        return None
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", start)
    return line_moment(text[start : end if end >= 0 else len(text)])


def first_moment(text: str) -> int | None:
    """The first `[m:ss]` moment anywhere in `text` (an excerpt that may start mid-line), or None."""
    found = _ANY_MOMENT.search(text)
    if found is None:
        return None
    parts = found.group(1).split(":")
    hours = parts[0] if len(parts) == 3 else None
    return to_ms(hours, parts[-2], parts[-1])


def line_text(line: str) -> str:
    """A chunk line without its moment prefix."""
    return _PREFIX.sub("", line, count=1)
