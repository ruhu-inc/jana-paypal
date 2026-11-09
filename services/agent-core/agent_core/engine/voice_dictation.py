"""Small, deterministic English spoken-maths grammar (INP-39): the inverse of the VOC-08 verbaliser.

Only a fully recognised expression (with an optional question prefix or "for x" suffix) is converted; anything else
stays verbatim and editable, with uncertain speech flagged for underlining. Nothing is guessed by a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ONES = dict(
    zip(
        (
            "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
            "sixteen seventeen eighteen nineteen"
        ).split(),
        range(20),
        strict=True,
    )
)
_ONES["nought"] = 0
_TENS = dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10), strict=True))
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000}
_DIGIT_WORDS = {w: v for w, v in _ONES.items() if v < 10}
# Denominator words of named fractions ("three quarters", "a half", "two fifths").
_DENOMINATORS = {
    "half": 2,
    "halves": 2,
    "third": 3,
    "thirds": 3,
    "quarter": 4,
    "quarters": 4,
    "fourth": 4,
    "fourths": 4,
    "fifth": 5,
    "fifths": 5,
    "sixth": 6,
    "sixths": 6,
    "seventh": 7,
    "sevenths": 7,
    "eighth": 8,
    "eighths": 8,
    "ninth": 9,
    "ninths": 9,
    "tenth": 10,
    "tenths": 10,
}
_ORDINAL_POWERS = {"fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}
_GREEK = {
    name: "\\" + name
    for name in ("alpha", "beta", "gamma", "delta", "theta", "lambda", "mu", "pi", "rho", "sigma", "omega", "phi")
}
_FUNCTIONS = {
    ("natural", "log", "of"): r"\ln",
    ("natural", "log"): r"\ln",
    ("sine", "of"): r"\sin",
    ("cosine", "of"): r"\cos",
    ("tangent", "of"): r"\tan",
    ("log", "of"): r"\log",
    ("sine",): r"\sin",
    ("sin",): r"\sin",
    ("cosine",): r"\cos",
    ("cos",): r"\cos",
    ("tangent",): r"\tan",
    ("tan",): r"\tan",
    ("log",): r"\log",
    ("ln",): r"\ln",
}
# Units read by the verbaliser (VOC-08 `UNITS`): spoken word -> symbol.
_UNITS = {
    "metre": "m",
    "metres": "m",
    "meter": "m",
    "meters": "m",
    "second": "s",
    "seconds": "s",
    "kilogram": "kg",
    "kilograms": "kg",
    "gram": "g",
    "grams": "g",
    "centimetre": "cm",
    "centimetres": "cm",
    "millimetre": "mm",
    "millimetres": "mm",
    "kilometre": "km",
    "kilometres": "km",
    "millisecond": "ms",
    "milliseconds": "ms",
    "newton": "N",
    "newtons": "N",
    "joule": "J",
    "joules": "J",
    "watt": "W",
    "watts": "W",
    "pascal": "Pa",
    "pascals": "Pa",
    "hertz": "Hz",
    "volt": "V",
    "volts": "V",
    "ampere": "A",
    "amperes": "A",
    "amp": "A",
    "amps": "A",
    "mole": "mol",
    "moles": "mol",
    "litre": "L",
    "litres": "L",
}
_PHRASES: dict[tuple[str, ...], tuple[str, str]] = {
    ("plus", "or", "minus"): ("op", r"\pm"),
    ("plus",): ("op", "+"),
    ("add",): ("op", "+"),
    ("minus",): ("minus", "-"),
    ("take", "away"): ("minus", "-"),
    ("negative",): ("neg", "-"),
    ("times",): ("mul", r"\times"),
    ("multiplied", "by"): ("mul", r"\times"),
    ("divided", "by"): ("mul", r"\div"),
    ("over",): ("over", "/"),
    ("equals",): ("rel", "="),
    ("equal", "to"): ("rel", "="),
    ("is", "equal", "to"): ("rel", "="),
    ("is",): ("rel", "="),
    ("is", "less", "than", "or", "equal", "to"): ("rel", r"\le"),
    ("less", "than", "or", "equal", "to"): ("rel", r"\le"),
    ("is", "greater", "than", "or", "equal", "to"): ("rel", r"\ge"),
    ("greater", "than", "or", "equal", "to"): ("rel", r"\ge"),
    ("is", "less", "than"): ("rel", "<"),
    ("less", "than"): ("rel", "<"),
    ("is", "greater", "than"): ("rel", ">"),
    ("greater", "than"): ("rel", ">"),
    ("is", "not", "equal", "to"): ("rel", r"\ne"),
    ("does", "not", "equal"): ("rel", r"\ne"),
    ("is", "approximately", "equal", "to"): ("rel", r"\approx"),
    ("approximately", "equals"): ("rel", r"\approx"),
    ("squared",): ("pow", "2"),
    ("cubed",): ("pow", "3"),
    ("to", "the", "power", "of"): ("power", ""),
    ("to", "the", "power"): ("power", ""),
    ("raised", "to", "the", "power", "of"): ("power", ""),
    ("end", "power"): ("end_power", ""),
    ("open", "bracket"): ("lp", "("),
    ("open", "brackets"): ("lp", "("),
    ("open", "parenthesis"): ("lp", "("),
    ("close", "bracket"): ("rp", ")"),
    ("close", "brackets"): ("rp", ")"),
    ("close", "parenthesis"): ("rp", ")"),
    ("the", "square", "root", "of"): ("root", "2"),
    ("square", "root", "of"): ("root", "2"),
    ("root", "of"): ("root", "2"),
    ("the", "square", "root"): ("root", "2"),
    ("square", "root"): ("root", "2"),
    ("the", "cube", "root", "of"): ("root", "3"),
    ("cube", "root", "of"): ("root", "3"),
    ("end", "root"): ("end_root", ""),
    ("the", "fraction"): ("fraction", ""),
    ("fraction",): ("fraction", ""),
    ("end", "fraction"): ("end_fraction", ""),
    ("sub",): ("sub", ""),
    ("subscript",): ("sub", ""),
    ("all",): ("all", ""),
    ("percent",): ("percent", r"\%"),
    ("per", "cent"): ("percent", r"\%"),
    ("per",): ("per", ""),
    **{(word,): ("greek", command) for word, command in _GREEK.items()},
    **{words: ("func", command) for words, command in _FUNCTIONS.items()},
}
_LONGEST = max(len(words) for words in _PHRASES)
_SYMBOLS = {
    "+": ("op", "+"),
    "-": ("minus", "-"),
    "\u2212": ("minus", "-"),
    "=": ("rel", "="),
    "<": ("rel", "<"),
    ">": ("rel", ">"),
    "\u2264": ("rel", r"\le"),
    "\u2265": ("rel", r"\ge"),
    "\u2260": ("rel", r"\ne"),
    "\u00d7": ("mul", r"\times"),
    "*": ("mul", r"\times"),
    "\u00f7": ("mul", r"\div"),
    "/": ("over", "/"),
    "(": ("lp", "("),
    ")": ("rp", ")"),
    ",": ("comma", ""),
}
# Element symbols a dictated formula may name (case-sensitive, as speech-to-text writes them).
_ELEMENTS = frozenset(
    ("H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Ti Cr Mn Fe Co Ni Cu Zn Br Ag Sn I Ba Pt Au Hg Pb U").split()
)
_ARROWS = {("gives",), ("yields",), ("forms",), ("produces",), ("reacts", "to", "give"), ("\u2192",), ("->",)}
_PREFIX = re.compile(
    r"(?i)^(what is|what's|solve|simplify|calculate|find|work out|expand|factorise|factorize|evaluate|"
    r"how do i solve|how do i simplify|can you solve)\s+"
)
_SUFFIX = re.compile(r"\s+(for [A-Za-z])$")
_HINT = re.compile(r"(?i)\b(squared|cubed|root|equals|over|plus|minus|times|power|fraction|divided)\b")
_COMMAND_END = re.compile(r"\\[a-zA-Z]+$")
MAX_INPUT = 4000


@dataclass(frozen=True)
class DictatedText:
    text: str
    uncertain: bool = False


class _NotMaths(ValueError):
    pass


def _cat(left: str, right: str) -> str:
    """Join LaTeX pieces; a control word followed by a letter or digit needs a space (`3\\times 4`, `2\\pi r`)."""
    if _COMMAND_END.search(left) and right[:1].isalnum():
        return left + " " + right
    return left + right


def _brace(text: str) -> str:
    return text if len(text) == 1 else "{" + text + "}"


def _integer(words: list[str]) -> int | None:
    """A cardinal from number words: "three hundred and five", "twelve thousand five hundred", "forty-two"."""
    if not words:
        return None
    for scale in ("million", "thousand"):
        if scale in words:
            if words.count(scale) != 1:
                return None
            index = words.index(scale)
            high = _integer(words[:index])
            tail = words[index + 1 :]
            if tail[:1] == ["and"]:
                tail = tail[1:]
            low = _integer(tail) if tail else 0
            if high is None or low is None or not 0 < high < 1000 or low >= _SCALES[scale]:
                return None
            return high * _SCALES[scale] + low
    if "hundred" in words:
        if words.count("hundred") != 1 or words.index("hundred") != 1 or not 0 < _ONES.get(words[0], 0) < 10:
            return None
        tail = words[2:]
        if tail[:1] == ["and"]:
            tail = tail[1:]
            if not tail:
                return None
        low = _integer(tail) if tail else 0
        return _ONES[words[0]] * 100 + low if low is not None and low < 100 else None
    if len(words) == 1:
        return _ONES.get(words[0], _TENS.get(words[0]))
    if len(words) == 2 and words[0] in _TENS and 0 < _ONES.get(words[1], 0) < 10:
        return _TENS[words[0]] + _ONES[words[1]]
    return None


def _is_number_word(word: str) -> bool:
    return word in _ONES or word in _TENS or word in _SCALES


class _Lexer:
    """Spoken words to maths tokens: (kind, value). Raises _NotMaths on any word outside the grammar."""

    def __init__(self, words: list[str]) -> None:
        self.words = words
        self.lower = [w.lower() for w in words]
        self.index = 0
        self.tokens: list[tuple[str, str]] = []

    def run(self) -> list[tuple[str, str]]:
        while self.index < len(self.words):
            self.step()
        # A comma is a pause: allowed only before an operator, a relation or a closing phrase.
        kept = []
        for position, token in enumerate(self.tokens):
            if token[0] == "comma":
                following = self.tokens[position + 1][0] if position + 1 < len(self.tokens) else "end"
                if following in ("end", "end_root", "end_fraction", "end_power", "op", "minus", "mul", "rel", "over"):
                    continue
                raise _NotMaths("pause_inside_expression")
            kept.append(token)
        return kept

    def step(self) -> None:
        word, lower = self.words[self.index], self.lower[self.index]
        if lower in _SYMBOLS:
            self.tokens.append(_SYMBOLS[lower])
            self.index += 1
            return
        if re.fullmatch(r"\d+(?:\.\d+)?", word):
            self.index += 1
            self.tokens.append(("num", word))
            self.fraction_after(word)
            return
        if _is_number_word(lower) or (lower == "a" and self.peek(1) in _DENOMINATORS):
            self.number()
            return
        if lower == "point" and self.peek(1) in _DIGIT_WORDS:  # "point five" reads as 0.5
            self.index += 1
            self.tokens.append(("num", "0." + self.decimals()))
            return
        if lower == "to" and self.peek(1) == "the" and self.peek(2) in _ORDINAL_POWERS:
            self.tokens.append(("pow", str(_ORDINAL_POWERS[self.peek(2)])))
            self.index += 3
            return
        for size in range(min(_LONGEST, len(self.words) - self.index), 0, -1):
            phrase = tuple(self.lower[self.index : self.index + size])
            if phrase in _PHRASES:
                self.tokens.append(_PHRASES[phrase])
                self.index += size
                return
        if lower in _UNITS:
            self.tokens.append(("unit", _UNITS[lower]))
            self.index += 1
            return
        if re.fullmatch(r"[A-Za-z]", word):
            self.tokens.append(("var", word))
            self.index += 1
            return
        raise _NotMaths("unknown_word")

    def peek(self, offset: int) -> str:
        position = self.index + offset
        return self.lower[position] if position < len(self.lower) else ""

    def decimals(self) -> str:
        digits = ""
        while self.peek(0) in _DIGIT_WORDS:
            digits += str(_DIGIT_WORDS[self.peek(0)])
            self.index += 1
        return digits

    def number(self) -> None:
        if self.lower[self.index] == "a":
            self.index += 1
            self.tokens.append(("num", "1"))
            self.fraction_after("1")
            return
        words: list[str] = []
        while self.index < len(self.lower):
            lower = self.lower[self.index]
            if _is_number_word(lower):
                # Two plain counts in a row ("three four") are two numbers, never one.
                if words and lower in _ONES and words[-1] in _ONES:
                    break
                if words and lower in _TENS and (words[-1] in _ONES or words[-1] in _TENS):
                    break
                words.append(lower)
                self.index += 1
            elif lower == "and" and words and words[-1] in _SCALES and _is_number_word(self.peek(1)):
                words.append(lower)
                self.index += 1
            else:
                break
        value = _integer(words)
        if value is None:
            raise _NotMaths("unreadable_number")
        text = str(value)
        if self.peek(0) == "point" and self.peek(1) in _DIGIT_WORDS:
            self.index += 1
            text += "." + self.decimals()
        self.tokens.append(("num", text))
        self.fraction_after(text)

    def fraction_after(self, numerator: str) -> None:
        denominator = _DENOMINATORS.get(self.peek(0))
        if denominator is None or not numerator.isdigit():
            return
        plural = self.peek(0).endswith("s")
        if plural == (numerator == "1"):  # "one thirds" and "two third" are not fractions
            raise _NotMaths("fraction_number")
        self.index += 1
        self.tokens[-1] = ("frac", r"\frac{" + numerator + "}{" + str(denominator) + "}")


class _Parser:
    """Recursive descent over the tokens: relation > sum > term > factor > primary."""

    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.index = 0
        self.stop_over = 0

    def peek(self, offset: int = 0) -> str:
        position = self.index + offset
        return self.tokens[position][0] if position < len(self.tokens) else "end"

    def take(self, kind: str | None = None) -> str:
        if kind is not None and self.peek() != kind:
            raise _NotMaths("expected_" + kind)
        value = self.tokens[self.index][1]
        self.index += 1
        return value

    def ahead(self, kind: str) -> int:
        return sum(1 for k, _ in self.tokens[self.index :] if k == kind)

    def expression(self) -> str:
        result = self.relation()
        if self.peek() != "end":
            raise _NotMaths("trailing_" + self.peek())
        return result

    def relation(self) -> str:
        result = self.sum()
        while self.peek() == "rel":
            result = _cat(_cat(result, self.take()), self.sum())
        return result

    def sum(self) -> str:
        result = self.term()
        while self.peek() in ("op", "minus"):
            operator = self.take()
            result = _cat(_cat(result, operator), self.term())
        return result

    def term(self) -> str:
        result, simple, ends_in_unit = self.factor()
        while True:
            kind = self.peek()
            if kind == "mul":
                operator = self.take()
                right, _, ends_in_unit = self.factor()
                result, simple = _cat(_cat(result, operator), right), False
            elif kind == "over" and not self.stop_over:
                self.take()
                right, right_simple, ends_in_unit = self.factor()
                if simple and right_simple:
                    result = r"\frac{" + result + "}{" + right + "}"
                else:
                    result = _cat(result + "/", right)
                simple = False
            elif kind in ("var", "greek", "lp", "root", "func", "fraction") and not ends_in_unit:
                right, _, ends_in_unit = self.factor()  # implied product: "three x", "two open bracket ..."
                result, simple = _cat(result, right), False
            else:
                return result

    def factor(self) -> tuple[str, bool, bool]:
        """(latex, is a plain number or letter, ends in a unit or percent)."""
        if self.peek() in ("neg", "minus"):
            self.take()
            inner, _, unit = self.factor()
            return "-" + inner, False, unit
        base, simple, closes_group = self.primary()
        powered = False
        while True:
            kind = self.peek()
            if kind == "all" and closes_group and self.peek(1) in ("pow", "power"):
                self.take()
            elif kind in ("pow", "power"):
                if powered:
                    raise _NotMaths("double_power")
                exponent = self.take()
                if kind == "power":
                    exponent = self.exponent()
                base, simple, powered = base + "^" + _brace(exponent), False, True
            elif kind == "sub" and simple:
                self.take()
                index = self.take() if self.peek() in ("num", "var") else ""
                if not index:
                    raise _NotMaths("missing_subscript")
                base, simple = base + "_" + _brace(index), False
            elif kind == "percent" and re.fullmatch(r"[\d.]+", base):
                return base + self.take(), False, True
            elif kind == "unit" and re.fullmatch(r"[\d.]+", base):
                return base + r"\," + self.units(), False, True
            else:
                return base, simple, False

    def exponent(self) -> str:
        if self.ahead("end_power") > 0:
            inner = self.sum()
            self.take("end_power")
            return inner
        sign = ""
        if self.peek() in ("neg", "minus"):
            self.take()
            sign = "-"
        if self.peek() not in ("num", "var", "greek", "frac"):
            raise _NotMaths("missing_exponent")
        return sign + self.take()

    def units(self) -> str:
        symbols = []
        while self.peek() == "unit":
            symbols.append(self.take())
        power = ""
        if self.peek() == "per":
            self.take()
            symbols.append(self.take("unit"))
            exponent = self.take() if self.peek() == "pow" else "1"
            power = "^{-" + exponent + "}"
        elif self.peek() == "pow":
            power = "^{" + self.take() + "}"
        return r"\text{" + " ".join(symbols) + "}" + power

    def primary(self) -> tuple[str, bool, bool]:
        """(latex, is a plain number or letter, ends with a closing bracket)."""
        kind = self.peek()
        if kind in ("num", "var", "greek"):
            return self.take(), True, False
        if kind == "frac":
            return self.take(), False, False
        if kind == "lp":
            self.take()
            saved, self.stop_over = self.stop_over, 0
            inner = self.relation()
            self.stop_over = saved
            self.take("rp")
            return "(" + inner + ")", False, True
        if kind == "root":
            degree = self.take()
            opening = r"\sqrt{" if degree == "2" else r"\sqrt[" + degree + "]{"
            if self.ahead("end_root") > self.ahead("root"):
                saved, self.stop_over = self.stop_over, 0
                inner = self.relation()
                self.stop_over = saved
                self.take("end_root")
            else:
                # Without "end root" the root takes one factor and must end the expression or one side of it.
                inner, _, _ = self.factor()
                if self.peek() not in ("end", "rel", "rp"):
                    raise _NotMaths("open_root")
            return opening + inner + "}", False, False
        if kind == "fraction":
            self.take()
            self.stop_over += 1
            numerator = self.sum()
            self.stop_over -= 1
            self.take("over")
            saved, self.stop_over = self.stop_over, 0
            denominator = self.sum()
            self.stop_over = saved
            self.take("end_fraction")
            return r"\frac{" + numerator + "}{" + denominator + "}", False, False
        if kind == "func":
            name = self.take()
            argument, _, _ = self.factor()
            return _cat(name, argument), False, False
        raise _NotMaths("unexpected_" + kind)


def _words(text: str) -> list[str]:
    text = re.sub(r"(?<=[A-Za-z])-(?=[A-Za-z])", " ", text)  # "twenty-one", "x-squared"
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)  # 12,500
    text = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", text)  # 3x
    return re.findall(r"\d+(?:\.\d+)?|[A-Za-z]+|->|[^\sA-Za-z\d]", text)


def _formula(words: list[str]) -> str | None:
    """A dictated chemical formula or equation ("H two S O four", "C H four plus two O two gives ...")."""
    pieces: list[str] = []
    species = ""
    subscripted = arrow = False
    elements = 0
    index = 0
    while index < len(words):
        word = words[index]
        glued = re.fullmatch(r"(?:[A-Z][a-z]?\d*)+", word)
        if word in _ELEMENTS:
            species += word
            elements += 1
            index += 1
            count = _ONES.get(words[index]) if index < len(words) else None
            if count is not None and count > 1:
                species += "_{" + str(count) + "}"
                subscripted = True
                index += 1
            continue
        if glued and any(c.isdigit() for c in word):
            parts = re.findall(r"([A-Z][a-z]?)(\d*)", word)
            if not all(symbol in _ELEMENTS for symbol, _ in parts):
                return None
            species += "".join(s + ("_{" + n + "}" if n else "") for s, n in parts)
            elements += len(parts)
            subscripted = True
            index += 1
            continue
        if word in _ONES and not species and 1 < _ONES[word] < 20:  # a coefficient before a species
            species = str(_ONES[word])
            index += 1
            continue
        separator = None
        if word.lower() == "plus":
            separator, size = "+", 1
        else:
            for phrase in _ARROWS:
                if tuple(w.lower() for w in words[index : index + len(phrase)]) == phrase:
                    separator, size = r"\rightarrow", len(phrase)
                    arrow = True
                    break
        if separator is None or not species or species.isdigit():
            return None
        pieces += [species, separator]
        species = ""
        index += size
    if not species or species.isdigit():
        return None
    pieces.append(species)
    if not (subscripted or arrow or (elements >= 2 and len(pieces) == 1)):
        return None
    result = ""
    for piece in pieces:
        result = _cat(result, piece)
    return r"\(\mathrm{" + result + r"}\)"


def spoken_maths(text: str) -> DictatedText:
    """Convert only a fully recognised expression (with an optional question prefix); preserve prose verbatim."""
    text = text.strip()
    if not text or len(text) > MAX_INPUT:
        return DictatedText(text[:MAX_INPUT], True)
    body = text.rstrip("?.!").rstrip()
    formula = _formula(body.replace(",", " ").split())
    if formula is not None:
        return DictatedText(formula)
    prefix = suffix = ""
    match = _PREFIX.match(body)
    if match:
        prefix, body = body[: match.end()], body[match.end() :]
    found = _SUFFIX.search(body)
    if found and prefix:
        suffix, body = " " + found[1], body[: found.start()]
    try:
        tokens = _Lexer(_words(body)).run()
        if not tokens:
            raise _NotMaths("empty")
        latex = _Parser(tokens).expression()
    except (_NotMaths, IndexError):
        return DictatedText(text, bool(_HINT.search(text)))
    if not re.search(r"[\d+=<>^_\\/-]", latex):  # a lone letter or word product is prose, not maths
        return DictatedText(text)
    return DictatedText(prefix + r"\(" + latex + r"\)" + suffix)
