"""Conservative quantity spacing (LOC-10). Numerals, TeX algebra and nuclide/chemical symbols are preserved."""

from __future__ import annotations

import re
from typing import Any

# Lowercase m/s/g can be algebra. Upright TeX makes their unit role explicit; bare joined symbols are conservative.
_PLAIN = r"m(?=/s)|kg|km|cm|mm|nm|ms|kJ|MJ|kW|MW|kPa|kHz|MHz|GHz|mA|kV|mV|mol|Pa|Hz|N|J|W|V|A|°C"
_EXPLICIT = rf"{_PLAIN}|m|s|g|K|L"
_JOINED = re.compile(rf"(?<![\w.^_{{/\\])(?P<number>[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(?P<unit>{_PLAIN})(?!\w)")
_TEX = re.compile(rf"(?P<previous>\d|}})(?P<command>\\(?:mathrm|text)\{{(?P<unit>{_EXPLICIT})(?=[\s}}/^\\]))")
_COMMA = re.compile(
    rf"(?<![\w,])(?P<number>\d+(?:(?:,|\{{,\}})\d+)+(?:[eE][-+]?\d+)?)(?:\s|\\[, ;])*"
    rf"(?:\\(?:mathrm|text)\{{)?(?:{_EXPLICIT})(?![A-Za-z])"
)
_GROUPED = re.compile(r"\d{1,3}(?:,\d{3})+")
_CHEMICAL_SYMBOLS = {"N", "V", "W", "Pa", "K"}
TEXT_FIELDS = {"say", "say_spoken", "latex", "prompt", "reason", "terms", "text", "body", "label", "title", "caption"}


def format_unit_spacing(text: str, subject: str | None = None, *, latex: bool = False) -> str:
    chemical = subject == "chemistry" or ".chm." in (subject or "")

    def plain(match: re.Match[str]) -> str:
        return match[0] if chemical and match["unit"] in _CHEMICAL_SYMBOLS else f"{match['number']} {match['unit']}"

    def tex(match: re.Match[str]) -> str:
        prefix = match.string[: match.start()] + match["previous"]
        if (chemical and match["unit"] in _CHEMICAL_SYMBOLS) or re.search(r"_\{[^{}]*\}$", prefix):
            return match[0]
        return f"{match['previous']}\\,{match['command']}"

    if not latex:
        # Model maths uses these delimiters. Bare 1000V in an equation is algebra, not a joined volt quantity.
        parts = re.split(r"(\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]|\$\$[\s\S]*?\$\$)", text)
        return "".join(
            format_unit_spacing(part, subject, latex=True) if index % 2 else _TEX.sub(tex, _JOINED.sub(plain, part))
            for index, part in enumerate(parts)
        )
    return _TEX.sub(tex, text)


def decimal_unit_comma(text: str) -> bool:
    """English grouping is allowed; a decimal comma attached to a quantity is not canonical tutor output."""
    return any(_GROUPED.fullmatch(match["number"].replace("{,}", ",")) is None for match in _COMMA.finditer(text))


def format_turn_units(draft: dict[str, Any], subject: str | None) -> dict[str, Any]:
    def walk(value: Any, key: str = "") -> Any:
        if isinstance(value, str):
            return format_unit_spacing(value, subject, latex=key == "latex") if key in TEXT_FIELDS else value
        if isinstance(value, dict):
            return {k: walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, key) for v in value]
        return value

    return walk(draft)  # type: ignore[no-any-return]
