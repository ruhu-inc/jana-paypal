"""INP-14: normalise untrusted text before persistence, redaction and injection scanning."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import Any

REMOVED = re.compile(r"[\u202a-\u202e\u2066-\u2069\u200b\u2060\ufeff\u00ad\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def normalise_input(text: str) -> str:
    text = REMOVED.sub("", unicodedata.normalize("NFC", text))
    return "".join(
        ch
        for i, ch in enumerate(text)
        if ch not in "\u200c\u200d" or (i > 0 and i + 1 < len(text) and text[i - 1].isalnum() and text[i + 1].isalnum())
    )


LINE_BREAKS = re.compile(r"[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]+")


def one_line(text: str) -> str:
    """The text on one line: every line or paragraph break (U+2028 and U+2029 included) becomes a space, so student
    text placed in a line-based engine block can never start a line of its own (PRM-15, PRM-19)."""
    return LINE_BREAKS.sub(" ", text)


def normalise_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def walk(value: Any) -> Any:
        if isinstance(value, str):
            return normalise_input(value)
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        return value

    return [walk(block) for block in blocks]


def photo_text(
    text: str, learning: Callable[[list[Any]], None] | None = None, session_id: str | None = None
) -> tuple[str, int]:
    from agent_core.learner.service import NewEvent
    from agent_core.prompts.untrusted import screened

    clean, hits = screened(normalise_input(text), "ocr_text")
    if hits and learning is not None:
        learning(
            [
                NewEvent(
                    "injection_flagged",
                    {"source": "photo", "pattern_id": pattern},
                    source="engine",
                    session_id=session_id.removeprefix("ses_") if session_id else None,
                )
                for pattern in dict.fromkeys(hit.pattern_id for hit in hits)
            ]
        )
    return normalise_input(clean), len(hits)
