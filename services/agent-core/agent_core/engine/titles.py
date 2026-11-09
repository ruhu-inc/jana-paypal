"""A chat's automatic title (UXS-68, D120): from its first exchange, at most six words, renameable afterwards.

The title is the first line of the student's first message with maths delimiters and Markdown marks dropped, so it
reads as words in the sidebar; the student renames it with `PATCH /v1/sessions/{id}` (`title`), which is guarded
like any other text the student writes."""

from __future__ import annotations

import re

TITLE_WORDS = 6
_DELIMITERS = re.compile(r"\\[()\[\]]|\$")
_MARKS = re.compile(r"[*_`#>]")


def chat_title(text: str, max_words: int = TITLE_WORDS) -> str:
    """At most six words from the first non-blank line of `text`, each with a letter and no digit (a title never
    carries a value from the question or its answer, as the title role's `clean_title`); empty when none is left.
    The title role replaces it with its own once the first problem is diagnosed (`team.name_session`)."""
    from agent_core.engine.pedagogy import social_kind

    if social_kind(text) is not None:
        return ""  # "hello" never names a chat; the first real question does
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    words = [
        w
        for w in _MARKS.sub("", _DELIMITERS.sub("", line)).split()
        if re.search(r"[^\W\d_]", w) and not re.search(r"\d", w)
    ]
    return " ".join(words[:max_words]).rstrip(",;:")[:80]
