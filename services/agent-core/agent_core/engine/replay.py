"""Replay an explanation (UXS-59, D103): a board played again as its timed sequence from the recorded events and the
tutor's spoken text, with no model call, no network and no audio.

The engine records every part it emits in order (`Engine.persisted`): board parts hold the board events, text parts the
tutor's words. The replay is those board events for one board in emission order, each with the spoken text that came
before it (re-synthesised by read-aloud where a voice exists, VOC-18; captions only otherwise), timed at a teaching
pace: the words take their reading time and a written line its write-on time (UXS-55). Applied in order, the items
end in exactly the board's final state. The student's own speech is never part of it, and no audio is stored or read."""

from __future__ import annotations

from typing import Any

WORD_MS = 380  # one spoken word at a teaching pace, for the replay's timing only
WRITE_ON_MS_PER_CHAR = 40
WRITE_ON_MAX_MS = 1200
GAP_MS = 250
SPOKEN_LIMIT = 600
MAX_ITEMS = 2000
BOARD_EVENTS = ("board.reset", "board.knowns", "board.step", "board.highlight", "board.annotate")


def board_replay(persisted: dict[str, dict[str, Any]], board_id: str) -> dict[str, Any] | None:
    """The replay of one board, or None when no recorded part holds it."""
    items: list[dict[str, Any]] = []
    offset = 0
    spoken: str | None = None
    for part_id, record in persisted.items():
        if record["kind"] == "text":
            text = str(record["content"] or "").strip()
            spoken = text[:SPOKEN_LIMIT] if text else spoken
            continue
        if record["kind"] != "board":
            continue
        for event in record["content"] or []:
            if event.get("type") not in BOARD_EVENTS or event.get("board_id") != board_id:
                continue
            if spoken is not None:
                offset += WORD_MS * len(spoken.split())  # the words before this item are said first
            if "part_id" in event and not event["part_id"]:
                event = {**event, "part_id": part_id}  # a reset records its part before the part is framed
            items.append({"offset_ms": offset, "event": event, "spoken_text": spoken})
            spoken = None
            if event["type"] == "board.step" and event.get("owner") == "tutor":
                offset += min(WRITE_ON_MAX_MS, WRITE_ON_MS_PER_CHAR * len(event.get("latex") or ""))
            offset += GAP_MS
    if not items:
        return None
    return {"board_id": board_id, "duration_ms": offset, "items": items[:MAX_ITEMS]}


def final_state(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The board's steps after applying the items in order (last write of each step wins), for checking a replay."""
    steps: dict[str, dict[str, Any]] = {}
    for item in items:
        event = item["event"]
        if event["type"] == "board.reset":
            steps = {}
        elif event["type"] == "board.step":
            steps[event["step_id"]] = {k: event[k] for k in ("latex", "owner", "status")}
    return steps
