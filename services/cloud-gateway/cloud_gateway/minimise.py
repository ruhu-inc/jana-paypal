"""Server-side data minimisation (MDL-25, PRV-25), as defence in depth behind the device's own redaction.

The gateway refuses a request that carries a profile id or the profile token header, forwards only an allow-list of
request fields (so nothing the device did not mean to send reaches a provider, and a chat participant `name` never
does), and builds every provider request from scratch, so no device header is ever forwarded. The only per-user
value a provider may receive is the random per-session pseudonym."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

FORWARDED_FIELDS = frozenset({
    "messages", "tools", "tool_choice", "parallel_tool_calls", "temperature", "top_p", "max_tokens",
    "max_completion_tokens", "stop", "seed", "response_format", "reasoning_effort",
})  # fmt: skip
MESSAGE_FIELDS = frozenset({"role", "content", "tool_calls", "tool_call_id"})
PROFILE_ID = re.compile(r"(?<![A-Za-z0-9])prf[_-]?[A-Za-z0-9_-]{4,}", re.I)
TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
PROFILE_KEYS = frozenset({"profile_id", "profile_ids", "display_name", "profile_name", "profile_token"})
PROFILE_HEADER = "x-profile-token"
PSEUDONYM = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
# An inline image is base64, which spells `prf…` in about a quarter of photos: its bytes are not words, so the
# profile-id check reads past it (an image carries no profile field the device could have meant to send).
IMAGE_DATA = re.compile(r"^data:image/[a-z0-9.+-]{1,20};base64,[A-Za-z0-9+/=\s]*$")


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        found = {str(k).lower() for k in value}
        for item in value.values():
            found |= _keys(item)
        return found
    if isinstance(value, list):
        return set().union(*(_keys(v) for v in value)) if value else set()
    return set()


def normalised(text: str) -> str:
    """The text as the checks read it: `\\uXXXX` escapes decoded, NFKC (fullwidth forms become plain ones) and
    invisible format characters (zero-width spaces and joiners, direction marks) removed, so hidden or lookalike
    characters cannot carry an id past the check (MDL-25)."""
    decoded = _ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
    return "".join(c for c in unicodedata.normalize("NFKC", decoded) if unicodedata.category(c) != "Cf")


def _strings(value: Any) -> list[str]:
    """Every key and string value of the body, so each is normalised as the provider would read it."""
    if isinstance(value, str):
        return [] if IMAGE_DATA.match(value) else [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (str(k), *_strings(v))]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return [json.dumps(value)]


def refusal(headers: Mapping[str, str], body: dict[str, Any]) -> str | None:
    """Why a request must not leave the gateway, or None."""
    if any(name.lower() == PROFILE_HEADER for name in headers):
        return "the request carries a profile token"
    if PROFILE_ID.search("\n".join(normalised(text) for text in _strings(body))):
        return "the request carries a profile id"
    if _keys(body) & PROFILE_KEYS:
        return "the request carries a profile field"
    return None


def valid_pseudonym(value: str | None) -> bool:
    return bool(value) and PSEUDONYM.fullmatch(value or "") is not None and not (value or "").startswith("prf_")


def forwarded(body: dict[str, Any]) -> dict[str, Any]:
    """The allow-listed request a provider adapter receives."""
    out = {k: v for k, v in body.items() if k in FORWARDED_FIELDS}
    messages = []
    for message in out.get("messages") or []:
        if not isinstance(message, dict):
            continue
        kept = {k: v for k, v in message.items() if k in MESSAGE_FIELDS}
        name = message.get("name")
        if message.get("role") == "tool" and isinstance(name, str) and TOOL_NAME.fullmatch(name):
            kept["name"] = name  # a tool's identifier, never a free-text name
        messages.append(kept)
    out["messages"] = messages
    return out
