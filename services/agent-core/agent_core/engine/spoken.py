"""Reading maths written or spoken in words back into symbols (AGT-09, AGT-10; the inverse of the VOC-08 conventions).

The leak gate and the claim check read every surface a student can read or hear. A value can be written as digits,
as LaTeX (`\\frac{49}{2}`, `2.45 \\times 10^{1}`) or in words ("twenty-four point five", "forty-nine over two", "two
minus two is four"). `to_written` turns the words into symbols in place, so the same numeric checks apply to all three:
number words become digits, and operator words between numbers become operators ("is" and "equals" become `=`).
Everything else in the text is left as it was. `latex_to_plain` turns the LaTeX forms into plain arithmetic."""

from __future__ import annotations

import functools
import re
from decimal import Decimal
from fractions import Fraction

ONES = {
    w: i
    for i, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen".split()
    )
}
TENS = {w: 10 * (i + 2) for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())}
SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000}
DENOMINATORS = {"half": 2, "halves": 2, "third": 3, "thirds": 3, "quarter": 4, "quarters": 4, "fourth": 4,
                "fourths": 4, "fifth": 5, "fifths": 5, "sixth": 6, "sixths": 6, "eighth": 8, "eighths": 8,
                "tenth": 10, "tenths": 10, "hundredth": 100, "hundredths": 100}  # fmt: skip
# Operator phrases said between two numbers, longest first (VOC-08a read backwards).
BETWEEN = (
    ("is equal to", "="),
    ("multiplied by", "*"),
    ("divided by", "/"),
    ("to the power of", "^"),
    ("take away", "-"),
    ("equals", "="),
    ("gives", "="),
    ("makes", "="),
    ("plus", "+"),
    ("minus", "-"),
    ("times", "*"),
    ("over", "/"),
    ("is", "="),
)
# "the next one", "this one": "one" as a pronoun, not a number.
PRONOUN_BEFORE = frozenset({"the", "this", "that", "which", "each", "every", "no", "next", "last", "any", "another",
                            "other", "same", "right", "wrong", "first", "a", "an"})  # fmt: skip
_WORD = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)*")
_NUMBERISH = r"-?\d+(?:\.\d+)?(?:/\d+)?"


def _number_at(words: list[str], i: int) -> tuple[Fraction, int] | None:
    """The number said from word i ("twenty four point five", "one hundred and two", "three quarters", "seven and a
    half") and the index after it."""
    if words[i] not in ONES and words[i] not in TENS:
        return None
    start = i
    whole = 0
    current = 0
    while i < len(words):
        word = words[i]
        if word in ONES:
            current += ONES[word]
        elif word in TENS:
            current += TENS[word]
        elif word in SCALES and i > start:
            if SCALES[word] == 100:
                current *= 100
            else:
                whole += current * SCALES[word]
                current = 0
        elif word == "and" and i + 1 < len(words) and (words[i + 1] in ONES or words[i + 1] in TENS) and \
                i > start and words[i - 1] in SCALES:  # fmt: skip
            pass
        else:
            break
        i += 1
    value = Fraction(whole + current)
    if i + 1 < len(words) and words[i] == "point" and words[i + 1] in ONES and ONES[words[i + 1]] < 10:
        digits = ""
        i += 1
        while i < len(words) and words[i] in ONES and ONES[words[i]] < 10:
            digits += str(ONES[words[i]])
            i += 1
        value += Fraction(int(digits), 10 ** len(digits))
    if i < len(words) and words[i] in DENOMINATORS and words[i] not in ("half",):  # "three quarters"
        return value / DENOMINATORS[words[i]], i + 1
    if i + 2 < len(words) and words[i] == "and" and words[i + 1] in ("a", "an", "one") \
            and words[i + 2] in DENOMINATORS:  # fmt: skip
        return value + Fraction(1, DENOMINATORS[words[i + 2]]), i + 3
    return value, i


def render(value: Fraction) -> str:
    """A value as written: a terminating decimal when it has one, else an exact fraction."""
    if value.denominator == 1:
        return str(value.numerator)
    denominator = value.denominator
    for prime in (2, 5):
        while denominator % prime == 0:
            denominator //= prime
    if denominator == 1:
        places = 0
        while (value * 10**places).denominator != 1:
            places += 1
        exact = Decimal(value.numerator) / Decimal(value.denominator)
        return f"{exact:.{places}f}"
    return f"{value.numerator}/{value.denominator}"


def _numbers_in_words(text: str) -> str:
    """Runs of number words replaced, in place, by their digits."""
    # Every word with its span; a hyphenated word ("twenty-four") is several words sharing one span.
    words: list[str] = []
    spans: list[tuple[int, int, bool, bool]] = []  # start, end, first and last part of its token
    tokens = list(_WORD.finditer(text))
    for k, token in enumerate(tokens):
        parts = token.group(0).lower().split("-")
        for n, part in enumerate(parts):
            words.append(part)
            spans.append((token.start(), token.end(), n == 0, n == len(parts) - 1))
        if k + 1 < len(tokens) and text[token.end() : tokens[k + 1].start()].strip():
            words.append("|")  # punctuation between words breaks a run
            spans.append((token.end(), token.end(), True, True))
    pieces: list[str] = []
    cursor = 0
    i = 0
    while i < len(words):
        if words[i] == "one" and i > 0 and words[i - 1] in PRONOUN_BEFORE:
            i += 1
            continue
        said = _number_at(words, i) if spans[i][2] else None  # a run starts at the start of a token
        if said is None or not spans[said[1] - 1][3]:  # and ends at the end of one
            i += 1
            continue
        value, end = said
        start = spans[i][0]
        pieces.append(text[cursor:start])
        pieces.append(render(value))
        cursor = spans[end - 1][1]
        i = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def to_written(text: str) -> str:
    """Number words as digits and operator words between numbers as symbols; other words are kept as they were."""
    if not text:
        return text
    written = _numbers_in_words(text.replace("\u2212", "-"))
    if written == text.replace("\u2212", "-") and not re.search(r"\d", written):
        return text
    written = re.sub(rf"\bnegative\s+(?={_NUMBERISH})", "-", written, flags=re.I)
    written = re.sub(r"(?<=\d)\s+squared\b", "^2", written, flags=re.I)
    written = re.sub(r"(?<=\d)\s+cubed\b", "^3", written, flags=re.I)
    for phrase, symbol in BETWEEN:
        pattern = re.compile(rf"(?<=[\d)])\s+{re.escape(phrase)}\s+(?=[-(\d])", re.I)
        if symbol == "=":
            written = pattern.sub(functools.partial(_relation, text=written), written)
        else:
            written = pattern.sub(f" {symbol} ", written)
    return written


_CLAUSE_START = re.compile(r"[.,;:!?=\n](?!\d)")
_OPERATION = re.compile(r"[\d)]\s*[-+*/^]\s*[-(\d]")


def _relation(match: re.Match[str], text: str) -> str:
    """ "is", "equals", "gives" or "makes" become `=` only after a calculation in the same clause ("two minus two is
    four"), never after a bare number ("step 1 is 2 marks" is not a claim)."""
    before = text[: match.start()]
    starts = [m.end() for m in _CLAUSE_START.finditer(before)]
    clause = before[starts[-1] if starts else 0 :]
    return " = " if _OPERATION.search(clause) else match.group(0)


_FRAC = re.compile(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_SQRT = re.compile(r"\\sqrt\s*\{([^{}]*)\}")


def latex_to_plain(text: str) -> str:
    """LaTeX arithmetic as plain arithmetic: fractions, products, powers in braces, thin spaces and `\\text{}` units."""
    if "\\" not in text and "{" not in text and "\u00d7" not in text and "\u00b7" not in text:
        return text
    plain = text.replace("\u2212", "-").replace("\u00d7", "*").replace("\u00b7", "*")
    for _ in range(4):  # nested fractions, innermost first
        changed = _FRAC.sub(r"((\1)/(\2))", plain)
        if changed == plain:
            break
        plain = changed
    plain = _SQRT.sub(r"((\1)^(1/2))", plain)
    plain = re.sub(r"\\(?:times|cdot|ast)", "*", plain)
    plain = re.sub(r"\\div", "/", plain)
    plain = re.sub(r"\\(?:left|right|,|;|:|!| |quad|qquad)", " ", plain)
    plain = re.sub(r"\\(?:text|mathrm|mathit|operatorname)\s*\{([^{}]*)\}", r" \1 ", plain)
    plain = re.sub(r"\^\s*\{([^{}]*)\}", r"^(\1)", plain)
    plain = plain.replace("{", "(").replace("}", ")")
    return plain
