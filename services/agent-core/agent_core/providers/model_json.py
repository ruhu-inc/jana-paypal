r"""LaTeX inside a model's JSON (structured replies and tool arguments).

Models write LaTeX in JSON strings with one backslash: `"s = 4 \\times 6"` is meant, `"s = 4 \times 6"` is written.
JSON reads `\t`, `\f`, `\b`, `\n` and `\r` as control characters, so `\times` becomes a tab and "imes", `\frac` a form
feed and "rac", `\nu` a newline and "u" — the answer is right and the reading corrupts it. A backslash before any
other letter (`\alpha`, `\cdot`, `\sqrt`) is not a JSON escape at all, so the whole reply fails to parse. Grammar-
constrained decoding allows the first kind and forbids the second; cloud models produce both.

`repair_latex_escapes` doubles such backslashes inside JSON string literals before the text is read: a backslash
followed by a known LaTeX command whose first letter is a JSON escape letter, a `\\u` not followed by four hex digits,
and a backslash before any other letter, which JSON never allows. Real escapes (`\n` before a space or a non-command
word, `\"`, `\\`, `\uXXXX`) are left alone, and the result of reading is unchanged for text without LaTeX.
"""

from __future__ import annotations

import json
import re
from typing import Any

# LaTeX commands a student or tutor writes in O and A level work whose names start with a JSON escape letter.
LATEX_ON_ESCAPE_LETTERS = frozenset({
    "times", "text", "textbf", "textit", "textrm", "texttt", "theta", "tau", "tan", "tanh", "to", "top", "triangle",
    "tilde", "therefore", "tfrac", "tbinom", "frac", "forall", "flat", "frown", "beta", "bar", "bf", "boldsymbol",
    "binom", "big", "bigl", "bigr", "Big", "bigg", "bullet", "because", "bmod", "bot", "boxed", "nu", "neq", "ne",
    "nabla", "not", "neg", "nmid", "nexists", "ni", "newline", "nleq", "ngeq", "notin", "rho", "right", "rangle",
    "rightarrow", "rm", "rfloor", "rceil", "rbrace", "rvert", "rVert", "root", "underline", "upsilon", "uparrow",
    "underbrace", "union", "u",
})  # fmt: skip
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
_ESCAPE = re.compile(r"\\(.)([A-Za-z]*)", re.DOTALL)
_UNICODE = re.compile(r"\\u[0-9a-fA-F]{4}")


def _fix_literal(literal: str) -> str:
    def fix(match: re.Match[str]) -> str:
        first, rest = match.group(1), match.group(2)
        word = first + rest
        if first in '"\\/':
            return match.group(0)
        if first == "u":  # \uXXXX is a real escape (its hex digits are not all letters); anything else is LaTeX
            if _UNICODE.match(literal, match.start()):
                return match.group(0)
            return "\\\\" + word
        if first in "bfnrt":
            return ("\\\\" + word) if word in LATEX_ON_ESCAPE_LETTERS else match.group(0)
        if first.isalpha():  # \alpha, \cdot, \sqrt: never a JSON escape
            return "\\\\" + word
        return match.group(0)

    return _ESCAPE.sub(fix, literal)


def repair_latex_escapes(text: str) -> str:
    """`text` with the backslashes of LaTeX commands inside JSON strings doubled (see the module docstring)."""
    if "\\" not in text:
        return text
    return _STRING.sub(lambda m: _fix_literal(m.group(0)), text)


def loads(text: str) -> Any:
    """`json.loads` after `repair_latex_escapes`."""
    return json.loads(repair_latex_escapes(text))
