"""Deterministic English speech text (VOC-08), shared by the future TTS and OS read-aloud paths.

This module renders text; it does not accept tutor candidates, grant speech release, or start a turn. Callers must
first obtain accepted text through Finalise (VOC-33). Unknown or malformed maths is replaced as a whole, rather
than partially read. Diagnostics contain reason codes only: the voice service can log them without student text.
No model, symbolic evaluator, file access, audio, or network is involved.
"""

# ruff: noqa: S105 -- `token` throughout this parser is a maths lexeme, never a credential.

from __future__ import annotations

import re
from dataclasses import dataclass

FALLBACK = "the expression on the board"
MAX_TEXT = 32_768
MAX_DEPTH = 32
ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen"
).split()
TENS = "zero ten twenty thirty forty fifty sixty seventy eighty ninety".split()
OPERATORS = {
    "+": "plus",
    "=": "equals",
    "*": "times",
    "/": "over",
    ":": "to",
    "%": "percent",
    "<": "is less than",
    ">": "is greater than",
    "<=": "is less than or equal to",
    ">=": "is greater than or equal to",
    "!=": "is not equal to",
}
COMMANDS = {
    "times": "times",
    "cdot": "times",
    "div": "divided by",
    "pm": "plus or minus",
    "le": "is less than or equal to",
    "leq": "is less than or equal to",
    "ge": "is greater than or equal to",
    "geq": "is greater than or equal to",
    "ne": "is not equal to",
    "neq": "is not equal to",
    "approx": "is approximately equal to",
    "rightarrow": "gives",
    "to": "gives",
    "Rightarrow": "implies",
    "infty": "infinity",
    "alpha": "alpha",
    "beta": "beta",
    "gamma": "gamma",
    "delta": "delta",
    "theta": "theta",
    "lambda": "lambda",
    "mu": "mu",
    "pi": "pi",
    "rho": "rho",
    "sigma": "sigma",
    "omega": "omega",
    "Delta": "delta",
    "Omega": "omega",
}
CONSTANTS = frozenset(
    {
        "infty",
        "alpha",
        "beta",
        "gamma",
        "delta",
        "theta",
        "lambda",
        "mu",
        "pi",
        "rho",
        "sigma",
        "omega",
        "Delta",
        "Omega",
    }
)
UNITS = {
    "m": ("metre", "metres"),
    "s": ("second", "seconds"),
    "kg": ("kilogram", "kilograms"),
    "g": ("gram", "grams"),
    "cm": ("centimetre", "centimetres"),
    "mm": ("millimetre", "millimetres"),
    "km": ("kilometre", "kilometres"),
    "ms": ("millisecond", "milliseconds"),
    "N": ("newton", "newtons"),
    "J": ("joule", "joules"),
    "W": ("watt", "watts"),
    "Pa": ("pascal", "pascals"),
    "Hz": ("hertz", "hertz"),
    "V": ("volt", "volts"),
    "A": ("ampere", "amperes"),
    "mol": ("mole", "moles"),
    "L": ("litre", "litres"),
}
# A quantity's unit is spoken in words (VOC-08): every unit the Maths and Physics packs answer in, by its symbol.
SPOKEN_UNITS = {
    **UNITS,
    "nm": ("nanometre", "nanometres"),
    "dm": ("decimetre", "decimetres"),
    "min": ("minute", "minutes"),
    "h": ("hour", "hours"),
    "kN": ("kilonewton", "kilonewtons"),
    "kJ": ("kilojoule", "kilojoules"),
    "MJ": ("megajoule", "megajoules"),
    "kW": ("kilowatt", "kilowatts"),
    "MW": ("megawatt", "megawatts"),
    "kWh": ("kilowatt hour", "kilowatt hours"),
    "kPa": ("kilopascal", "kilopascals"),
    "mmHg": ("millimetre of mercury", "millimetres of mercury"),
    "kHz": ("kilohertz", "kilohertz"),
    "mV": ("millivolt", "millivolts"),
    "kV": ("kilovolt", "kilovolts"),
    "mA": ("milliampere", "milliamperes"),
    "C": ("coulomb", "coulombs"),
    "nC": ("nanocoulomb", "nanocoulombs"),
    "Ω": ("ohm", "ohms"),
    "kΩ": ("kilohm", "kilohms"),
    "K": ("kelvin", "kelvin"),
    "°C": ("degree Celsius", "degrees Celsius"),
    "°": ("degree", "degrees"),
    "Bq": ("becquerel", "becquerels"),
    "kBq": ("kilobecquerel", "kilobecquerels"),
    "MBq": ("megabecquerel", "megabecquerels"),
    "counts": ("count", "counts"),
}
_SYMBOL = (
    r"(?:\\Omega|" + "|".join(re.escape(s) for s in sorted(SPOKEN_UNITS, key=len, reverse=True)) + r")(?![A-Za-z])"
)
_POWER = r"(?:\^\{-?[123]\}|\^-?[123](?![0-9])|[²³]|⁻[¹²³])"
_TERM = rf"{_SYMBOL}{_POWER}?"
_JOIN = r"(?:\s*(?:\\[,;]|\\cdot|·)\s*|\s+)"
_PRODUCT = rf"{_TERM}(?:{_JOIN}{_TERM})*"
_UNIT = rf"{_PRODUCT}(?:\s*/\s*(?:{_TERM}|\(\s*{_PRODUCT}\s*\)))?"
_WRAPPED = rf"\\(?:text|mathrm)\{{\s*{_UNIT}\s*\}}{_POWER}?"
# A number, then a unit: spaced from it (TeX spaces count), or glued when the unit is more than one letter or a sign.
_QUANTITY = re.compile(
    rf"(?:(?<![A-Za-z_\\])|(?<=\d[eE])|(?<=\d[eE][-+]))(?P<number>(?:\d{{1,3}}(?:,\d{{3}})+|\d+)(?:\.\d+)?)"
    rf"(?P<close>\}}?)(?P<space>(?:\s|~|\\[,;: ]|\\quad)*)(?P<unit>{_WRAPPED}|{_UNIT})(?![\^⁻²³0-9A-Za-z])"
)
_ALONE = re.compile(rf"{_WRAPPED}(?![\^⁻²³0-9A-Za-z])")
_PIECE = re.compile(rf"/|\(|\)|(?P<symbol>{_SYMBOL})(?P<power>{_POWER})?")
_POWERS = {"²": 2, "³": 3, "⁻¹": -1, "⁻²": -2, "⁻³": -3}
_NUMBER = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_TOKEN = re.compile(
    r"\\(?:[a-zA-Z]+|[,;:! ])|(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|"
    r"[a-zA-Z]+(?:['\u2019][a-zA-Z]+)*|<=|>=|!=|[^\s]"
)
_WORD = r"[a-zA-Z]+(?:['\u2019][a-zA-Z]+)*"
_FORMULA = re.compile(r"(?:[A-Z][a-z]?)+")  # element symbols as written in a formula: CO, NH, Fe, Mg
_CHARGE = re.compile(r"[1-9]?[+-]")  # an ion's charge as a superscript: 2+, 3-, +, -
# Lowercase runs a maths span reads as named functions (TeX writes the rest as products of single-letter variables).
FUNCTIONS = frozenset({"sin", "cos", "tan", "log", "ln", "exp", "lim", "max", "min", "mod", "det"})
ORDINALS = frozenset({"st", "nd", "rd", "th"})
_RATIO_SIDE = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|[a-zA-Z]|[)\]}]")
# Cue markers of a spoken form (VOC-10, D103 write cues): never spoken. `engine.pointing` resolves them first.
_POINTER = re.compile(r'<point\s+at="[^"<>]{1,80}"\s*/>|<write\s+at="[^"<>]{1,80}"\s*>|</write\s*>')
_FORBIDDEN = re.compile(r"[\\$^_{}<>*`#]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TRANSLATE = str.maketrans(
    {
        "\u2212": "-",
        "\u00d7": "*",
        "\u00b7": "*",
        "\u00f7": "/",
        "\u2264": "<=",
        "\u2265": ">=",
        "\u2260": "!=",
        "\u2192": r"\rightarrow",
    }
)


@dataclass(frozen=True)
class SpeechText:
    text: str
    fallbacks: tuple[str, ...] = ()


class _Unreadable(ValueError):
    pass


def _integer(value: int) -> str:
    if value < 20:
        return ONES[value]
    if value < 100:
        return TENS[value // 10] + (" " + ONES[value % 10] if value % 10 else "")
    for scale, name in (
        (10**12, "trillion"),
        (10**9, "billion"),
        (10**6, "million"),
        (1000, "thousand"),
        (100, "hundred"),
    ):
        if value >= scale:
            return _integer(value // scale) + " " + name + (" " + _integer(value % scale) if value % scale else "")
    raise AssertionError("unreachable")


def _number(token: str) -> str:
    whole, dot, decimal = token.replace(",", "").partition(".")
    if len(whole) > 15:
        raise _Unreadable("number_too_large")
    result = _integer(int(whole))
    return result + (" point " + " ".join(ONES[int(d)] for d in decimal) if dot else "")


def _power(text: str | None) -> int:
    if not text:
        return 0
    return _POWERS[text] if text in _POWERS else int(text.strip("^{}"))


def _unit_words(unit: str, singular: bool) -> str:
    """A unit in words: "m/s^2" is "metres per second squared", "J/(kg °C)" is "joules per kilogram per degree
    Celsius". Only the last unit above the line is plural, as in "newton metres"; one of anything is singular."""
    outer = 0
    wrapped = re.fullmatch(rf"\\(?:text|mathrm)\{{\s*(?P<inner>.*?)\s*\}}(?P<power>{_POWER})?", unit, re.S)
    if wrapped:
        unit, outer = wrapped["inner"], _power(wrapped["power"])  # a power after the group is its last unit's
    terms: list[tuple[str, int]] = []
    below = False
    for piece in _PIECE.finditer(unit):
        if piece[0] == "/":
            below = True
        elif piece["symbol"]:
            power = _power(piece["power"]) or 1
            terms.append(("Ω" if piece["symbol"] == r"\Omega" else piece["symbol"], -power if below else power))
    if outer:
        symbol, power = terms[-1]
        terms[-1] = (symbol, outer if power > 0 else -outer)
    ending = {1: "", 2: " squared", 3: " cubed"}
    above = [(s, p) for s, p in terms if p > 0]
    words = [SPOKEN_UNITS[s][0 if singular or i < len(above) - 1 else 1] + ending[p] for i, (s, p) in enumerate(above)]
    words += ["per " + SPOKEN_UNITS[s][0] + ending[-p] for s, p in terms if p < 0]
    return " ".join(words)


def _say_units(text: str, maths: bool) -> str:
    """Each quantity's unit as words in a `\\text{}` group, which the reader speaks as it stands (VOC-08). A letter
    glued to a number is a unit only outside maths and when it cannot be a variable: 5cm and 30° are units, the m of
    2m and the s of 1990s are not."""

    def quantity(match: re.Match[str]) -> str:
        unit = match["unit"]
        wrapped = unit.startswith(("\\text", "\\mathrm"))
        if not match["space"] and not wrapped:
            first = re.match(_SYMBOL, unit)
            sign = first is not None and first[0] in ("°", "°C", "Ω", r"\Omega")
            if not sign and (maths or first is None or len(first[0]) == 1):
                return match[0]
        before = text[match.start() - 1] if match.start() else ""
        singular = match["number"] == "1" and not match["close"] and before not in "^{-.eE"
        return f"{match['number']}{match['close']} \\text{{{_unit_words(unit, singular)}}}"

    def alone(match: re.Match[str]) -> str:
        symbols = [p["symbol"] for p in _PIECE.finditer(match[0]) if p["symbol"]]
        if len(symbols) == 1 and symbols[0] not in UNITS and len(symbols[0]) == 1:
            return match[0]  # a lone \text{C} or \text{K} may be a label, not a unit
        return f"\\text{{{_unit_words(match[0], False)}}}"

    return _ALONE.sub(alone, _QUANTITY.sub(quantity, text))


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([.,;:!?])", r"\1", text)


class _Math:
    def __init__(self, text: str, maths: bool = False) -> None:
        found = list(_TOKEN.finditer(text.translate(_TRANSLATE)))
        self.tokens = [m.group() for m in found]
        # Whether each token touches the one before it with no space (the "as" of 2as, the "nd" of 2nd).
        self.glued = [i > 0 and m.start() == found[i - 1].end() for i, m in enumerate(found)]
        self.index = 0
        self.maths = maths  # a delimited TeX span, where a lowercase run is a product of variables
        self.words = 0  # inside \text or \mathrm: words stay words

    def spelled(self, token: str) -> bool:
        """VOC-08: a lowercase run read letter by letter, as the product of variables it is: the 2as of
        v^2 = u^2 + 2as is "two a s", never the word "as". Units, ordinals and named functions stay whole."""
        if not re.fullmatch(r"[a-z]{2,}", token) or self.words:
            return False
        position = self.index - 1
        coefficient = (
            0 < position < len(self.tokens)
            and self.glued[position]
            and _NUMBER.fullmatch(self.tokens[position - 1]) is not None
            and len(token) <= 4
            and token not in UNITS
            and token not in ORDINALS
        )
        return coefficient or (self.maths and token not in FUNCTIONS)

    def ratio(self) -> bool:
        """A colon between two quantities is a ratio ("three to four"); a colon in prose is punctuation, a pause
        ("two accounts: when cash comes in"), never the word "to"."""
        position = self.index - 1
        before = self.tokens[position - 1] if position > 0 else ""
        after = self.peek()
        return bool(_RATIO_SIDE.fullmatch(before)) and bool(_RATIO_SIDE.fullmatch(after) or after in ("(", "{", "["))

    def hyphen(self) -> bool:
        """A hyphen glued between words (well-known, light-years, x-axis, a 20-minute break) belongs to the word and
        is never read as a sign. Single letters on both sides (x-y) and anything in a maths span stay a minus."""
        position = self.index - 1
        if (self.maths and not self.words) or position < 1 or position + 1 >= len(self.tokens):
            return False
        before, after = self.tokens[position - 1], self.peek()
        if not (self.glued[position] and self.glued[position + 1]) or not re.fullmatch(_WORD, after):
            return False
        if _NUMBER.fullmatch(before):
            return len(after) > 2 and after not in FUNCTIONS
        return re.fullmatch(_WORD, before) is not None and max(len(before), len(after)) > 1

    def peek(self) -> str:
        return self.tokens[self.index] if self.index < len(self.tokens) else ""

    def take(self) -> str:
        token = self.peek()
        self.index += 1
        return token

    def group(self, depth: int) -> tuple[str, list[str]]:
        if self.take() != "{":
            raise _Unreadable("missing_group")
        start = self.index
        spoken = self.read(depth + 1, "}")
        return spoken, self.tokens[start : self.index - 1]

    def script(self, depth: int) -> tuple[str, list[str]]:
        if self.peek() == "{":
            return self.group(depth)
        token = self.take()
        if not token or not (token.isalnum() or token.startswith("\\")):
            raise _Unreadable("missing_script")
        # An unbraced TeX script is exactly one character (x^23 means x squared, then three).
        if len(token) > 1 and token.isalnum():
            self.tokens[self.index - 1 : self.index] = [token[0], token[1:]]
            self.glued[self.index - 1 : self.index] = [self.glued[self.index - 1], True]
            self.index -= 1
            token = self.take()
        return self.atom(token, depth + 1), [token]

    def atom(self, token: str, depth: int) -> str:
        if depth > MAX_DEPTH:
            raise _Unreadable("nesting_limit")
        if _NUMBER.fullmatch(token):
            return _number(token)
        if token == "-":
            return "negative"  # read() distinguishes unary from binary minus
        if token in OPERATORS:
            return OPERATORS[token]
        if token in ("(", "["):
            close = ")" if token == "(" else "]"
            return "open bracket " + self.read(depth + 1, close) + " close bracket"
        if token in (r"\frac", r"\dfrac", r"\tfrac"):
            numerator, ns = self.group(depth)
            denominator, ds = self.group(depth)
            if not numerator or not denominator:
                raise _Unreadable("empty_fraction")
            if len(ns) == len(ds) == 1 and ns[0].isdigit() and ds[0].isdigit():
                n, d = int(ns[0]), int(ds[0])
                if d in (2, 3, 4) and 0 < n < d:
                    unit = {2: "half", 3: "third", 4: "quarter"}[d]
                    return numerator + " " + unit + ("s" if n != 1 else "")
            if len(ns) == len(ds) == 1:
                return numerator + " over " + denominator
            return "the fraction " + numerator + " over " + denominator + ", end fraction"
        if token == r"\sqrt":
            radicand, _ = self.group(depth)
            if not radicand:
                raise _Unreadable("empty_root")
            return "the square root of " + radicand + ", end root"
        if token in (r"\text", r"\mathrm", r"\operatorname"):
            self.words += 1
            try:
                spoken, tokens = self.group(depth)
            finally:
                self.words -= 1
            # Unit groups can carry a power on their final unit: \\text{m s}^{-2}.
            if tokens and all(t in UNITS for t in tokens):
                suffix = ""
                per = False
                if self.peek() == "^":
                    self.take()
                    power, raw = self.script(depth)
                    per = bool(raw and raw[0] == "-")
                    absolute = raw[1:] if per else raw
                    endings: dict[tuple[str, ...], str] = {("2",): " squared", ("3",): " cubed", ("1",): ""}
                    if per and tuple(absolute) not in endings:
                        raise _Unreadable("unsupported_unit_power")
                    suffix = endings.get(tuple(absolute), " to the power " + power)
                units = [UNITS[t][1] for t in tokens]
                if per:
                    units[-1] = "per " + UNITS[tokens[-1]][0]
                units[-1] += suffix
                return " ".join(units)
            return spoken
        if token in (r"\,", r"\;", r"\:", r"\!", "\\ ", r"\quad", r"\qquad"):
            return ""
        if token.startswith("\\"):
            command = token[1:]
            if command in COMMANDS:
                return COMMANDS[command]
            raise _Unreadable("unsupported_command")
        if re.fullmatch(r"[a-zA-Z]+(?:['\u2019][a-zA-Z]+)*", token):
            # Formula symbols keep their case and are separated from atom counts by the lexer.
            if re.fullmatch(r"(?:[A-Z][a-z]?)+", token):
                return " ".join(re.findall(r"[A-Z][a-z]?", token))
            return token
        if token in (".", ",", ";", "!", "?", "'", '"'):
            return token
        raise _Unreadable("unsupported_notation")

    def read(self, depth: int = 0, close: str = "") -> str:
        if depth > MAX_DEPTH:
            raise _Unreadable("nesting_limit")
        pieces: list[str] = []
        operand = False
        chemical = False  # the last piece ends a formula: an element symbol, its count or its charge
        while self.peek():
            token = self.take()
            if token == close:
                return _clean(" ".join(pieces))
            if token in ("}", ")", "]"):
                raise _Unreadable("unbalanced_group")
            if token in (r"\left", r"\right"):
                continue
            if token in ("^", "_"):
                if not operand:
                    raise _Unreadable("missing_base")
                spoken, raw = self.script(depth)
                if not spoken:
                    raise _Unreadable("empty_script")
                if token == "_":
                    # C_3H_8, CO_2 and Fe_2O_3 follow the chemistry convention; x_i remains an explicit subscript.
                    chemical = chemical or bool(pieces and re.fullmatch(r"[A-Z][a-z]?", pieces[-1]))
                    pieces.append(spoken if chemical else "sub " + spoken)
                elif chemical and _CHARGE.fullmatch("".join(raw)):
                    # An ion's charge: Mg^{2+} is "Mg two plus", Cl^{-} is "Cl minus".
                    charge = "".join(raw)
                    sign = "plus" if charge.endswith("+") else "minus"
                    pieces.append((_number(charge[:-1]) + " " + sign) if len(charge) > 1 else sign)
                elif raw == ["2"]:
                    pieces.append("squared")
                elif raw == ["3"]:
                    pieces.append("cubed")
                else:
                    pieces.append("to the power " + spoken + (", end power" if len(raw) > 1 else ""))
                continue
            if token == "{":
                self.index -= 1
                spoken, _ = self.group(depth)
            elif token == "-" and pieces and self.hyphen():
                pieces[-1] += "-" + self.take()
                operand = chemical = False
                continue
            elif token == "-":
                spoken = "minus" if operand else "negative"
            elif token == ":" and not self.ratio():
                pieces.append(":")
                operand = chemical = False
                continue
            elif self.spelled(token):
                pieces.append(" ".join(token))
                operand, chemical = True, False
                continue
            elif token == "$":
                amount = self.take()
                if not _NUMBER.fullmatch(amount):
                    raise _Unreadable("invalid_currency")
                spoken = _number(amount) + (" dollar" if amount.replace(",", "") == "1" else " dollars")
            else:
                spoken = self.atom(token, depth)
            pieces.append(spoken)
            chemical = bool(_FORMULA.fullmatch(token))
            if spoken:
                operand = token not in OPERATORS and token != "-" and token[1:] not in COMMANDS
                if token.isalpha() and len(token) > 1 and not chemical:
                    operand = False
                if token.startswith("\\") and token[1:] in ("sqrt", "frac", "dfrac", "tfrac", "text", "mathrm"):
                    operand = True
                if token.startswith("\\") and token[1:] in CONSTANTS:
                    operand = True
        if close:
            raise _Unreadable("unbalanced_group")
        return _clean(" ".join(pieces))


def _render(text: str, failures: list[str], maths: bool = False) -> str:
    try:
        if "**" in text:
            raise _Unreadable("unsupported_notation")
        if re.search(r"<\s*/?\s*[A-Za-z][^>]*>", text):
            raise _Unreadable("unsupported_markup")
        rendered = _Math(_say_units(text, maths), maths).read()
        if _FORBIDDEN.search(rendered):
            raise _Unreadable("unsafe_speech_text")
        return rendered
    except _Unreadable as error:
        failures.append(str(error))
        return FALLBACK


def verbalise(text: str) -> SpeechText:
    """Render accepted English text, with reason-only fallback diagnostics.

    Supports inline/display TeX delimiters and bare maths. Unsupported delimited expressions are replaced without
    losing surrounding prose; unsupported bare notation replaces the entire passage. No rejected draft should be
    passed here: rendering is deliberately separate from the engine's acceptance and release decisions.
    """
    if len(text) > MAX_TEXT:
        return SpeechText(FALLBACK, ("text_limit",))
    text = _POINTER.sub(" ", text)
    # Visible markdown labels survive; destinations and formatting never reach TTS.
    text = re.sub(r"!?\[([^\]\n]+)\]\([^()\n]*\)", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}(?:#{1,6}\s+|[-*+]\s+|>\s+)", "", text)
    text = re.sub(r"(?<!\w)\*\*(?=\S)(.+?)(?<=\S)\*\*(?!\w)", r"\1", text)
    text = re.sub(r"(?<![\w*])\*(?=\S)([^*\n]*?\S)\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?<!`)`([^`\n]+)`(?!`)", r"\1", text)
    failures: list[str] = []
    pieces: list[str] = []
    cursor = 0
    plain = 0
    while cursor < len(text):
        opener = next((op for op in (r"\(", r"\[", "$$", "$") if text.startswith(op, cursor)), None)
        if opener is None:
            cursor += 1
            continue
        # Currency may occur several times in one sentence. A numeric TeX span instead continues with an
        # operator or its closing delimiter; never pair the dollar signs of two currency amounts.
        amount = _NUMBER.match(text, cursor + 1) if opener == "$" else None
        if amount:
            after = text[amount.end() :].lstrip()
            # A coefficient glued to letters ($2as = v^2 - u^2$) opens a maths span when a closing dollar follows.
            glued = text[amount.end() : amount.end() + 1].isalpha() and "$" in text[amount.end() :]
            if not glued and (not after or after[0].isalpha() or after[0] in ".,;!?)"):
                cursor += 1
                continue
        pieces.append(_render(text[plain:cursor], failures))
        closer = {r"\(": r"\)", r"\[": r"\]", "$$": "$$", "$": "$"}[opener]
        end = text.find(closer, cursor + len(opener))
        if end < 0:
            failures.append("unclosed_math")
            pieces.append(FALLBACK)
            plain = cursor = len(text)
            break
        pieces.append(_render(text[cursor + len(opener) : end], failures, maths=True))
        plain = cursor = end + len(closer)
    pieces.append(_render(text[plain:], failures))
    return SpeechText(_clean(" ".join(pieces)), tuple(failures))
