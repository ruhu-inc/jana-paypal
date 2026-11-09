"""Reply normalisation, so the engine never sees provider quirks (MDL-13, MDL-14, MDL-09).

- Think scrubbing: reasoning blocks are removed from visible text across stream deltas, including tags split
  between chunks and blocks left open at the end of the stream; the removed text becomes reasoning.
- Tool-call salvage: calls written as text (`<tool_call>` blocks or bare JSON) become tool calls only when tools
  were offered and only for offered names; anything else stays text and counts as an unparsed tool call.
- Call ids are made unique per turn; empty replies are detected for the empty-twice guard."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from agent_core.providers import model_json
from agent_core.providers.base import AssistantTurn, ToolCall

TAGS = (
    ("<think>", "</think>"),
    ("<thinking>", "</thinking>"),
    ("<reasoning>", "</reasoning>"),
    ("<thought>", "</thought>"),
)
OPENS = tuple(o for o, _ in TAGS)
CLOSES = tuple(c for _, c in TAGS)
ALL_TAGS = OPENS + CLOSES


def _held_suffix(text: str, tags: tuple[str, ...]) -> int:
    """Length of the longest suffix of `text` that is a proper prefix of one of `tags` (case-insensitive)."""
    lower = text.lower()
    best = 0
    for tag in tags:
        for n in range(min(len(tag) - 1, len(lower)), 0, -1):
            if lower.endswith(tag[:n]):
                best = max(best, n)
                break
    return best


@dataclass
class ThinkScrubber:
    """Feed text deltas; get back (visible, reasoning) pieces safe to emit now."""

    buffer: str = ""
    closing: str | None = None  # the close tag of the open block, when inside one

    def feed(self, delta: str) -> tuple[str, str]:
        self.buffer += delta
        visible: list[str] = []
        reasoning: list[str] = []
        while self.buffer:
            lower = self.buffer.lower()
            if self.closing is not None:
                at = lower.find(self.closing)
                if at >= 0:
                    reasoning.append(self.buffer[:at])
                    self.buffer = self.buffer[at + len(self.closing) :]
                    self.closing = None
                    continue
                keep = _held_suffix(self.buffer, (self.closing,))
                reasoning.append(self.buffer[: len(self.buffer) - keep])
                self.buffer = self.buffer[len(self.buffer) - keep :]
                break
            hits = [(lower.find(tag), tag) for tag in ALL_TAGS if lower.find(tag) >= 0]
            if hits:
                at, tag = min(hits)
                visible.append(self.buffer[:at])
                self.buffer = self.buffer[at + len(tag) :]
                if tag in OPENS:
                    self.closing = CLOSES[OPENS.index(tag)]
                continue  # a stray close tag is dropped
            keep = _held_suffix(self.buffer, ALL_TAGS)
            visible.append(self.buffer[: len(self.buffer) - keep])
            self.buffer = self.buffer[len(self.buffer) - keep :]
            break
        return "".join(visible), "".join(reasoning)

    def flush(self) -> tuple[str, str]:
        """End of stream: an unclosed block is reasoning; a dangling partial tag is dropped."""
        rest, self.buffer = self.buffer, ""
        if self.closing is not None:
            self.closing = None
            return "", rest
        return "", ""


def scrub(text: str) -> tuple[str, str]:
    scrubber = ThinkScrubber()
    visible, reasoning = scrubber.feed(text)
    tail_visible, tail_reasoning = scrubber.flush()
    return visible + tail_visible, reasoning + tail_reasoning


# --- tool-call salvage ----------------------------------------------------------------------------------------

_BLOCK = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _call_from_json(text: str) -> tuple[str, dict[str, Any]] | None:
    try:
        data = model_json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        return None
    arguments = data.get("arguments", data.get("parameters", {}))
    if isinstance(arguments, str):
        try:
            arguments = model_json.loads(arguments)
        except ValueError:
            return None
    return (data["name"], arguments) if isinstance(arguments, dict) else None


def looks_like_tool_call(text: str) -> bool:
    stripped = text.strip()
    return bool(_BLOCK.search(stripped)) or (
        stripped.startswith("{") and '"name"' in stripped and ('"arguments"' in stripped or '"parameters"' in stripped)
    )


@dataclass
class Salvage:
    text: str
    calls: list[ToolCall] = field(default_factory=list)
    unparsed: bool = False  # a tool call was written but could not be used


def salvage(text: str, offered: set[str]) -> Salvage:
    """Move text-written calls to offered tools into tool calls; leave everything else as text."""
    if not offered or not looks_like_tool_call(text):
        return Salvage(text, unparsed=bool(offered) and looks_like_tool_call(text))
    calls: list[ToolCall] = []
    unparsed = False
    blocks = _BLOCK.findall(text)
    if blocks:
        remaining = text
        for block in blocks:
            parsed = _call_from_json(block)
            if parsed and parsed[0] in offered:
                calls.append(ToolCall("", parsed[0], parsed[1]))
                remaining = _BLOCK.sub("", remaining, count=1)
            else:
                unparsed = True
        return Salvage(remaining.strip() if calls else text, calls, unparsed)
    body = text.strip()
    fenced = _FENCE.match(body)
    parsed = _call_from_json(fenced.group(1) if fenced else body)
    if parsed and parsed[0] in offered:
        return Salvage("", [ToolCall("", parsed[0], parsed[1])])
    return Salvage(text, unparsed=True)


def unique_ids(calls: list[ToolCall], prefix: str) -> list[ToolCall]:
    """Ids unique within the turn: missing or repeated ids are replaced, in order."""
    seen: set[str] = set()
    out = []
    for n, call in enumerate(calls):
        call_id = call.id if call.id and call.id not in seen else f"{prefix}_{n}"
        seen.add(call_id)
        out.append(replace(call, id=call_id))
    return out


@dataclass(frozen=True)
class Normalised:
    turn: AssistantTurn
    empty: bool
    unparsed_tool_call: bool


def normalise(turn: AssistantTurn, offered: set[str], id_prefix: str) -> Normalised:
    """A provider turn with reasoning moved out of text, text-written calls salvaged and ids made unique.

    Native tool calls pass through even when their name is misspelt or their arguments did not parse: the engine's
    repair pipeline resolves them or answers them through the hidden `invalid` tool (AGT-06, TOL-08, TOL-09). Only
    text that looks like a tool call and cannot be salvaged counts as an unparsed call (MDL-14, MDL-09)."""
    visible, extracted = scrub(turn.text)
    reasoning = "\n".join(r for r in (turn.reasoning, extracted.strip() or None) if r) or None
    found = salvage(visible, offered) if not turn.tool_calls else Salvage(visible)
    calls = unique_ids(list(turn.tool_calls) + found.calls, id_prefix) if offered else []
    text = found.text.strip() if found.calls else visible.strip()
    finish = "tool_calls" if calls else ("stop" if turn.finish_reason == "tool_calls" else turn.finish_reason)
    normal = replace(turn, text=text, tool_calls=calls, reasoning=reasoning, finish_reason=finish)
    unparsed = found.unparsed or (bool(turn.tool_calls) and not offered)
    empty = not text and not calls and turn.structured is None and not unparsed
    return Normalised(normal, empty, unparsed)
