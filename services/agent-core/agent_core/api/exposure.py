"""Exposure policy for reasoning and hidden roles (API-11).

Student-scope streams never carry model reasoning text, Solver plans, raw tool outputs or other roles' drafts: they
routinely contain the answer. Reasoning shows only as a `reasoning` part's start and end with its duration; a
`tool.state` keeps its humanised title, a summary without withheld values and its check id. `reasoning.delta` and
tool outputs go only to `debug` scope tokens, which exist only in developer builds (none are issued today).

The one `output` a student-scope `tool.state` may keep is engine-built, not a tool's: the count of items a memory read
returned, from which the client draws the fixed memory-use line (MEM-28). It carries no text."""

from __future__ import annotations

from typing import Any

DEBUG_ONLY_EVENTS = frozenset({"reasoning.delta"})


def engine_count(output: Any) -> bool:
    """The memory-use count `{"items": n}` (MEM-28): the only output shape a student may receive."""
    return isinstance(output, dict) and set(output) == {"items"} and isinstance(output["items"], int)


def student_view(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """The envelope as a student-scope stream may carry it: None for a debug-only event, a tool state without its
    output, everything else unchanged."""
    event = envelope["event"]
    if event["type"] in DEBUG_ONLY_EVENTS:
        return None
    if event["type"] == "tool.state" and "output" in event.get("state", {}):
        if not engine_count(event["state"]["output"]):
            state = {k: v for k, v in event["state"].items() if k != "output"}
            return {**envelope, "event": {**event, "state": state}}
    return envelope
