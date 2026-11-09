"""Our own expression grammar (VER-03): plain maths and a LaTeX subset, parsed by a tokenizer and a Pratt parser that
build SymPy trees node by node. No string ever reaches `eval`, `exec`, `compile`, `sympify` or `parse_expr`.

Accepted forms follow TABLE VER-03a: numbers (decimals kept exact, scientific notation, fractions, mixed numbers,
a decimal comma where the locale uses one), implicit products, powers and roots, the named functions, relations
and chains, and LaTeX commands from a fixed list; anything else is refused with the position of the first error.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

import sympy as sp

# Limits (proposals of VER-03).
MAX_DEPTH = 64
MAX_INTEGER = 10**30
MAX_POWER_EXPONENT = 10_000  # numeric powers beyond this stay unevaluated or are refused
MAX_POWER_BITS = 10_000  # a numeric power's result, in bits of its base times the exponent


class ParseRejected(Exception):
    def __init__(self, message: str, position: int) -> None:
        super().__init__(message)
        self.position = position


class TooLarge(Exception):
    def __init__(self, message: str, limit: str) -> None:
        super().__init__(message)
        self.limit = limit


# --- tokens -------------------------------------------------------------------------------------------------

FUNCTIONS = {
    "sin": sp.sin,
    "cos": sp.cos,
    "tan": sp.tan,
    "sec": sp.sec,
    "csc": sp.csc,
    "cot": sp.cot,
    "asin": sp.asin,
    "acos": sp.acos,
    "atan": sp.atan,
    "arcsin": sp.asin,
    "arccos": sp.acos,
    "arctan": sp.atan,
    "sinh": sp.sinh,
    "cosh": sp.cosh,
    "tanh": sp.tanh,
    "exp": sp.exp,
    "ln": sp.log,
    "abs": sp.Abs,
}
CONSTANTS = {"pi": sp.pi, "π": sp.pi, "e": sp.E, "∞": sp.oo, "inf": sp.oo}
GREEK = {
    "alpha",
    "beta",
    "gamma",
    "delta",
    "epsilon",
    "zeta",
    "eta",
    "theta",
    "iota",
    "kappa",
    "lambda",
    "mu",
    "nu",
    "xi",
    "rho",
    "sigma",
    "tau",
    "upsilon",
    "phi",
    "chi",
    "psi",
    "omega",
    "Delta",
    "Gamma",
    "Theta",
    "Lambda",
    "Sigma",
    "Phi",
    "Psi",
    "Omega",
}
LATEX_COMMANDS = {
    "frac",
    "dfrac",
    "tfrac",
    "sqrt",
    "cdot",
    "times",
    "div",
    "left",
    "right",
    "pi",
    "ln",
    "log",
    "exp",
    "le",
    "leq",
    "ge",
    "geq",
    "ne",
    "neq",
    "pm",
    "infty",
    *GREEK,
    *(n for n in FUNCTIONS if n != "abs"),
}
UNICODE_GREEK = {
    "α": "alpha",
    "β": "beta",
    "γ": "gamma",
    "δ": "delta",
    "θ": "theta",
    "λ": "lambda",
    "μ": "mu",
    "ρ": "rho",
    "σ": "sigma",
    "φ": "phi",
    "ω": "omega",
    "Δ": "Delta",
    "Ω": "Omega",
}
SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")
RELATIONS = {
    "=": sp.Eq,
    "<": sp.Lt,
    ">": sp.Gt,
    "<=": sp.Le,
    ">=": sp.Ge,
    "!=": sp.Ne,
    "≤": sp.Le,
    "≥": sp.Ge,
    "≠": sp.Ne,
}


@dataclass(frozen=True)
class Token:
    kind: str  # num, name, cmd, op, rel, lparen, rparen, lbrace, rbrace, lbracket, rbracket, bar, sup, sub, end
    text: str
    pos: int


_NUMBER = re.compile(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+")
_NUMBER_COMMA = re.compile(r"\d+(?:,\d+)?(?:[eE][+-]?\d+)?")


def tokenize(text: str, decimal_comma: bool = False) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    number = _NUMBER_COMMA if decimal_comma else _NUMBER
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "0123456789" or (ch == "." and i + 1 < len(text) and text[i + 1] in "0123456789"):
            m = number.match(text, i)
            assert m
            tokens.append(Token("num", m.group().replace(",", "."), i))
            i = m.end()
            continue
        if ch == "\\":
            m = re.compile(r"\\([A-Za-z]+|[{}|,;!])").match(text, i)
            if not m:
                raise ParseRejected("a backslash must start a LaTeX command", i)
            name = m.group(1)
            if name in (",", ";", "!"):  # LaTeX spacing
                i = m.end()
                continue
            if name in ("{", "}", "|"):
                tokens.append(Token({"{": "lparen", "}": "rparen", "|": "bar"}[name], name, i))
                i = m.end()
                continue
            if name not in LATEX_COMMANDS:
                raise ParseRejected(f"unknown LaTeX command \\{name}", i)
            tokens.append(Token("cmd", name, i))
            i = m.end()
            continue
        if ch in UNICODE_GREEK:
            tokens.append(Token("name", UNICODE_GREEK[ch], i))
            i += 1
            continue
        if ch.isascii() and ch.isalpha():
            m = re.compile(r"[A-Za-z]+").match(text, i)
            if not m:
                raise ParseRejected(f"unexpected character {ch!r}", i)
            tokens.append(Token("name", m.group(), i))
            i = m.end()
            continue
        if ch in "π∞":
            tokens.append(Token("name", ch, i))
            i += 1
            continue
        two = text[i : i + 2]
        if two in ("<=", ">=", "!=", "**"):
            tokens.append(Token("rel" if two != "**" else "op", "^" if two == "**" else two, i))
            i += 2
            continue
        if ch in "=<>≤≥≠":
            tokens.append(Token("rel", ch, i))
        elif ch in "+-*/^×·÷−":
            tokens.append(Token("op", {"×": "*", "·": "*", "÷": "/", "−": "-"}.get(ch, ch), i))
        elif ch in "([":
            tokens.append(Token("lparen", ch, i))
        elif ch in ")]":
            tokens.append(Token("rparen", ch, i))
        elif ch == "{":
            tokens.append(Token("lbrace", ch, i))
        elif ch == "}":
            tokens.append(Token("rbrace", ch, i))
        elif ch == "|":
            tokens.append(Token("bar", ch, i))
        elif ch == "_":
            tokens.append(Token("sub", ch, i))
        elif ch == ",":
            tokens.append(Token("comma", ch, i))
        elif ch == "√":
            tokens.append(Token("cmd", "sqrt", i))
        elif ch in "⁰¹²³⁴⁵⁶⁷⁸⁹⁻":
            j = i
            while j < len(text) and text[j] in "⁰¹²³⁴⁵⁶⁷⁸⁹⁻":
                j += 1
            tokens.append(Token("sup", text[i:j].translate(SUPERSCRIPTS), i))
            i = j
            continue
        else:
            raise ParseRejected(f"unexpected character {ch!r}", i)
        i += 1
    tokens.append(Token("end", "", len(text)))
    return tokens


# --- numbers and nodes --------------------------------------------------------------------------------------


def exact_number(text: str, pos: int) -> sp.Expr:
    """A decimal literal as an exact Rational (VER-03: decimals kept exact)."""
    mantissa, _, exponent = text.lower().partition("e")
    whole, _, frac = mantissa.partition(".")
    digits = int((whole or "0") + frac)
    if digits > MAX_INTEGER:
        raise TooLarge(f"the number at position {pos} is larger than 10^30", "components")
    value = sp.Rational(digits, 10 ** len(frac))
    if exponent:
        power = int(exponent)
        if abs(power) > 300:
            raise TooLarge(f"the exponent at position {pos} is too large", "components")
        value = value * sp.Rational(10) ** power
    return value


def safe_pow(base: sp.Expr, exponent: sp.Expr) -> sp.Expr:
    """A power that never evaluates a huge numeric result. Every rational exponent counts, not only integers:
    `4^(10^9+1/2)` is as large as `2^(2*10^9+1)` (SEC-08)."""
    if base.is_number and exponent.is_number and exponent.is_Rational:
        size = abs(sp.Rational(exponent))
        if size > MAX_POWER_EXPONENT:
            raise TooLarge("the power is too large to evaluate", "components")
        if base.is_Rational:
            bits = max(math.log2(abs(int(base.p)) + 1), math.log2(abs(int(base.q)) + 1), 1)
            if size * bits > MAX_POWER_BITS:
                raise TooLarge("the power is too large to evaluate", "components")
    if exponent.is_number and exponent.has(sp.Pow) and base.is_number:
        return sp.Pow(base, exponent, evaluate=False)
    return sp.Pow(base, exponent)


def huge_power(tree: sp.Basic) -> bool:
    """A power with a numeric exponent beyond the grammar's bound, such as (x+1)^(10^29): expanding, factorising,
    differentiating or solving it loops over binomial coefficients without a Python call, so the time slice never
    fires and the per-call budget is escaped (SEC-08). Such a tree can only be sampled."""
    return any(
        isinstance(node, sp.Pow) and node.exp.is_number and abs(node.exp) > MAX_POWER_EXPONENT
        for node in sp.preorder_traversal(tree)
    )


def bounded_subs(tree: sp.Basic, values: dict[sp.Basic, sp.Basic]) -> sp.Basic:
    """`tree` with `values` substituted, rebuilt bottom-up through `safe_pow`, so a power whose base or exponent
    becomes a number (x^(10^29) at x = 2, 2^x at x = 10^29) is refused with TooLarge before SymPy computes it: an exact
    power runs in C, where the per-call budget cannot stop it (SEC-08). As with `subs`, a subtree with nothing
    substituted is kept as it is, never re-evaluated."""
    if tree in values:
        return values[tree]
    if not tree.args:
        return tree
    args = [bounded_subs(a, values) for a in tree.args]
    if all(new is old for new, old in zip(args, tree.args, strict=True)):
        return tree
    return safe_pow(*args) if isinstance(tree, sp.Pow) else tree.func(*args)


# --- parser -------------------------------------------------------------------------------------------------


@dataclass
class Parser:
    tokens: list[Token]
    variables: frozenset[str]
    assumptions: dict[str, dict[str, bool]]
    log_base: int = 10
    evaluate: bool = True  # False keeps the form as written (2/4 stays 2/4) for form checks
    i: int = 0
    depth: int = 0
    abs_depth: int = 0
    symbols: dict[str, sp.Symbol] = field(default_factory=dict)
    # The product before a split run's last letter and that letter, while a power may still follow (VER-03).
    split_run: tuple[sp.Expr, sp.Expr] | None = None

    @property
    def tok(self) -> Token:
        return self.peek(0)

    def peek(self, ahead: int) -> Token:
        """The token `ahead` places on; past the end it is always the end token."""
        return self.tokens[min(self.i + ahead, len(self.tokens) - 1)]

    def take(self, kind: str | None = None, text: str | None = None) -> Token:
        tok = self.tok
        if (kind and tok.kind != kind) or (text is not None and tok.text != text):
            want = text or kind or "a token"
            raise ParseRejected(f"expected {want!r} but found {tok.text or 'the end'!r}", tok.pos)
        self.i += 1
        return tok

    def accept(self, kind: str, text: str | None = None) -> Token | None:
        if self.tok.kind == kind and (text is None or self.tok.text == text):
            return self.take()
        return None

    def symbol(self, name: str) -> sp.Symbol:
        if name not in self.symbols:
            flags = {"real": True, **self.assumptions.get(name, {})}
            self.symbols[name] = sp.Symbol(name, **flags)
        return self.symbols[name]

    def enter(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise TooLarge(f"the expression is nested deeper than {MAX_DEPTH} levels", "components")

    # --- arithmetic that respects `evaluate` -----------------------------------------------------------------

    def add(self, a: sp.Expr, b: sp.Expr) -> sp.Expr:
        return sp.Add(a, b, evaluate=self.evaluate)

    def sub(self, a: sp.Expr, b: sp.Expr) -> sp.Expr:
        return sp.Add(a, self.neg(b), evaluate=self.evaluate)

    def mul(self, a: sp.Expr, b: sp.Expr) -> sp.Expr:
        return sp.Mul(a, b, evaluate=self.evaluate)

    def div(self, a: sp.Expr, b: sp.Expr) -> sp.Expr:
        return sp.Mul(a, sp.Pow(b, -1, evaluate=self.evaluate), evaluate=self.evaluate)

    def neg(self, a: sp.Expr) -> sp.Expr:
        return sp.Mul(-1, a, evaluate=self.evaluate)

    def pow(self, a: sp.Expr, b: sp.Expr) -> sp.Expr:
        return safe_pow(a, b) if self.evaluate else sp.Pow(a, b, evaluate=False)

    def applies(self, *arguments: sp.Expr) -> bool:
        """Whether a function is evaluated on these arguments. The form as written evaluates one only on numbers and
        symbols: evaluating it on an unevaluated expression asks SymPy what it knows of that expression, and SymPy's
        answers for a node evaluation would have removed (the -1*0 of `7 - (0)`) depend on the order they are asked
        in and stay cached for every later call, so an unrelated later check meets a contradiction."""
        return self.evaluate or not any(a.args for a in arguments)

    # --- statements and relations ---------------------------------------------------------------------------

    def statement(self) -> sp.Basic:
        left = self.expression()
        if self.tok.kind != "rel" and not (
            self.tok.kind == "cmd" and self.tok.text in ("le", "leq", "ge", "geq", "ne", "neq")
        ):
            self.take("end")
            return left
        parts: list[sp.Basic] = []
        while self.tok.kind == "rel" or (
            self.tok.kind == "cmd" and self.tok.text in ("le", "leq", "ge", "geq", "ne", "neq")
        ):
            op = self.take().text
            op = {"le": "<=", "leq": "<=", "ge": ">=", "geq": ">=", "ne": "!=", "neq": "!="}.get(op, op)
            right = self.expression()
            parts.append(RELATIONS[op](left, right, evaluate=False))
            left = right
        self.take("end")
        return parts[0] if len(parts) == 1 else sp.And(*parts, evaluate=False)

    # --- expressions (Pratt) ---------------------------------------------------------------------------------

    def expression(self) -> sp.Expr:
        self.enter()
        try:
            negative = bool(self.accept("op", "-"))
            if not negative:
                self.accept("op", "+")
            value = self.term()
            if negative:
                value = self.neg(value)
            while self.tok.kind == "op" and self.tok.text in "+-":
                op = self.take().text
                right = self.term()
                value = self.add(value, right) if op == "+" else self.sub(value, right)
            return value
        finally:
            self.depth -= 1

    def term(self) -> sp.Expr:
        value = self.signed_power()
        while True:
            if self.tok.kind == "op" and self.tok.text in "*/":
                op = self.take().text
                right = self.signed_power()
                if op == "/":
                    if right == 0:
                        raise ParseRejected("division by zero", self.tokens[self.i - 1].pos)
                    value = self.div(value, right)
                else:
                    value = self.mul(value, right)
            elif self.tok.kind == "cmd" and self.tok.text in ("cdot", "times", "div"):
                op = self.take().text
                right = self.signed_power()
                value = self.div(value, right) if op == "div" else self.mul(value, right)
            elif self.starts_factor():
                value = self.mul(value, self.power())  # implicit product
            else:
                return value

    def signed_power(self) -> sp.Expr:
        if self.accept("op", "-"):
            return self.neg(self.signed_power())
        return self.power()

    def starts_factor(self) -> bool:
        tok = self.tok
        if tok.kind == "bar":
            return self.abs_depth == 0  # inside |...| a bar closes the absolute value
        return tok.kind in ("num", "name", "lparen") or (
            tok.kind == "cmd"
            and tok.text not in ("cdot", "times", "div", "left", "right", "le", "leq", "ge", "geq", "ne", "neq", "pm")
        )

    def power(self) -> sp.Expr:
        self.split_run = None
        base = self.primary()
        # A letter run split into a product (ab -> a*b): a following power belongs to its last letter only (VER-03).
        run, self.split_run = self.split_run, None
        if self.tok.kind == "sup":
            text = self.take().text
            exponent = exact_number(text.lstrip("-"), 0)
            exponent = -exponent if text.startswith("-") else exponent
        elif self.accept("op", "^"):
            exponent = self.braced() if self.tok.kind in ("lbrace",) else self.signed_power()
        else:
            return base
        if run is not None:
            head, last = run
            return self.mul(head, self.pow(last, exponent))
        return self.pow(base, exponent)

    def braced(self) -> sp.Expr:
        self.take("lbrace")
        value = self.expression()
        self.take("rbrace")
        return value

    def primary(self) -> sp.Expr:
        tok = self.tok
        if tok.kind == "num":
            return self.number()
        if tok.kind == "lparen":
            self.take()
            value = self.expression()
            self.take("rparen")
            return value
        if tok.kind == "lbrace":
            return self.braced()
        if tok.kind == "bar":
            self.take()
            self.abs_depth += 1
            value = self.expression()
            self.abs_depth -= 1
            self.take("bar")
            return sp.Abs(value, evaluate=self.evaluate)
        if tok.kind == "cmd":
            return self.command()
        if tok.kind == "name":
            return self.name()
        raise ParseRejected(f"unexpected {tok.text or 'end of input'!r}", tok.pos)

    def number(self) -> sp.Expr:
        tok = self.take("num")
        value = exact_number(tok.text, tok.pos) if self.evaluate or "." not in tok.text else sp.Float(tok.text)
        # A mixed number: an integer, a space, then integer/integer (2 1/2).
        if (
            "." not in tok.text
            and self.tok.kind == "num"
            and "." not in self.tok.text
            and self.peek(1).kind == "op"
            and self.peek(1).text == "/"
            and self.peek(2).kind == "num"
            and self.tok.pos > tok.pos + len(tok.text)
        ):
            numerator = exact_number(self.take().text, tok.pos)
            self.take("op", "/")
            denominator = exact_number(self.take("num").text, tok.pos)
            if denominator == 0:
                raise ParseRejected("division by zero", tok.pos)
            return self.add(value, self.div(numerator, denominator))
        return value

    def name(self) -> sp.Expr:
        tok = self.take("name")
        text = tok.text
        if text in ("cbrt", "root", "frac") and text not in self.variables:
            self.take("lparen", "(")
            first = self.expression()
            if text == "cbrt":
                self.take("rparen", ")")
                return self.pow(first, sp.Rational(1, 3))
            self.take("comma")
            second = self.expression()
            self.take("rparen", ")")
            if text == "frac":
                if second == 0:
                    raise ParseRejected("division by zero", tok.pos)
                return self.div(first, second)
            if first == 0:
                raise ParseRejected("a root's degree cannot be zero", tok.pos)
            return self.pow(second, self.div(sp.Integer(1), first))
        if text in FUNCTIONS or text in ("log", "sqrt"):
            return self.function(text, tok)
        if text in CONSTANTS and text not in self.variables:
            return CONSTANTS[text]
        if text in self.variables or text in GREEK:
            return self.subscripted(text)
        # Split undeclared letter runs into single-letter variables (xy -> x*y), keeping known names whole.
        value: sp.Expr = sp.Integer(1)
        rest = text
        while rest:
            known = next(
                (
                    n
                    for n in sorted((*FUNCTIONS, "log", "sqrt", "pi", *self.variables, *GREEK), key=len, reverse=True)
                    if rest.startswith(n) and len(n) > 1
                ),
                None,
            )
            if known and (known in FUNCTIONS or known in ("log", "sqrt")):
                # A function name glued to letters: 2xsin -> refuse rather than guess.
                raise ParseRejected(f"write the function {known} separately", tok.pos + len(text) - len(rest))
            if known:
                value = self.mul(value, CONSTANTS[known] if known == "pi" else self.symbol(known))
                rest = rest[len(known) :]
                continue
            letter, rest = rest[0], rest[1:]
            atom = self.subscripted(letter) if not rest else self.symbol(letter)
            if not rest and value != 1:
                self.split_run = (value, atom)
            value = atom if value == 1 else self.mul(value, atom)
        return value

    def subscripted(self, base: str) -> sp.Expr:
        if self.accept("sub"):
            if self.tok.kind == "lbrace" or (self.tok.kind == "lparen" and self.tok.text == "("):
                opening = self.take()
                parts = []
                while self.tok.kind in ("num", "name") or (self.tok.kind == "op" and self.tok.text in ("+", "-")):
                    parts.append(self.take().text)
                self.take("rbrace" if opening.kind == "lbrace" else "rparen")
                if not parts:
                    raise ParseRejected("the subscript is empty", opening.pos)
                index = "".join(parts)
            else:
                index = self.take().text
            return self.symbol(f"{base}_{index}")
        return self.symbol(base)

    def command(self) -> sp.Expr:
        tok = self.take("cmd")
        name = tok.text
        if name in ("frac", "dfrac", "tfrac"):
            numerator, denominator = self.braced(), self.braced()
            if denominator == 0:
                raise ParseRejected("division by zero", tok.pos)
            return self.div(numerator, denominator)
        if name == "sqrt":
            index: sp.Expr | None = None
            if self.accept("lparen", "["):
                index = self.expression()
                self.take("rparen", "]")
            if self.tok.kind == "lbrace":
                radicand = self.braced()
            elif self.tok.kind == "lparen":
                radicand = self.primary()
            else:
                radicand = self.power()
            if index is not None:
                return sp.root(radicand, index, evaluate=self.applies(radicand, index))
            return sp.sqrt(radicand, evaluate=self.applies(radicand))
        if name == "left":
            if self.accept("bar"):
                value = self.expression()
                self.take("cmd", "right")
                self.take("bar")
                return sp.Abs(value, evaluate=self.applies(value))
            self.take("lparen")
            value = self.expression()
            self.take("cmd", "right")
            self.take("rparen")
            return value
        if name in ("pi",):
            return sp.pi
        if name == "infty":
            return sp.oo
        if name in GREEK:
            return self.subscripted(name)
        if name in FUNCTIONS or name in ("log", "exp", "ln"):
            return self.function(name, tok)
        raise ParseRejected(f"\\{name} is not allowed here", tok.pos)

    def function(self, name: str, tok: Token) -> sp.Expr:
        base: sp.Expr | None = None
        if name == "log" and self.accept("sub"):
            base = self.braced() if self.tok.kind == "lbrace" else self.primary()
        # sin^2 x = (sin x)^2
        outer_power: sp.Expr | None = None
        if self.tok.kind == "sup":
            outer_power = exact_number(self.take().text, tok.pos)
        elif self.accept("op", "^"):
            outer_power = self.braced() if self.tok.kind == "lbrace" else self.primary()
        if self.tok.kind == "lparen" and self.tok.text == "(":
            self.take()
            argument = self.expression()
            self.take("rparen")
        elif self.tok.kind == "cmd" and self.tok.text == "left":
            argument = self.primary()
        else:
            # An argument without brackets: the following factors up to the next function or operator (sin 2x).
            argument = self.power()
            while self.starts_factor() and not (
                self.tok.kind in ("name", "cmd") and (self.tok.text in FUNCTIONS or self.tok.text in ("log", "sqrt"))
            ):
                argument = self.mul(argument, self.power())
        if name == "sqrt":
            value: sp.Expr = sp.sqrt(argument, evaluate=self.applies(argument))
        elif name == "log":
            base = base if base is not None else sp.Integer(self.log_base)
            if self.applies(argument, base):
                value = sp.log(argument, base)
            else:  # log(x)/log(b), the shape the evaluated two-argument log has
                value = self.div(sp.log(argument, evaluate=False), sp.log(base, evaluate=False))
        elif FUNCTIONS[name] in (sp.asin, sp.acos, sp.atan):
            # Kept unevaluated: asin(1/2) would otherwise become pi/6 here, before a degrees reading can apply.
            value = FUNCTIONS[name](argument, evaluate=False)
        else:
            value = FUNCTIONS[name](argument, evaluate=self.applies(argument))
        return self.pow(value, outer_power) if outer_power is not None else value


@dataclass(frozen=True)
class Parsed:
    tree: sp.Basic  # evaluated: canonical for computation
    as_written: sp.Basic  # unevaluated: the student's form, for form checks
    symbols: dict[str, sp.Symbol]


def parse(
    text: str,
    *,
    variables: frozenset[str] = frozenset(),
    assumptions: dict[str, dict[str, bool]] | None = None,
    log_base: int = 10,
    decimal_comma: bool = False,
) -> Parsed:
    """Parse one line into a SymPy expression or relation. Raises ParseRejected (with position) or TooLarge."""
    tokens = tokenize(text, decimal_comma)
    if tokens[0].kind == "end":
        raise ParseRejected("the input is empty", 0)
    parser = Parser(tokens, variables, assumptions or {}, log_base)
    tree = parser.statement()
    written = Parser(tokens, variables, assumptions or {}, log_base, evaluate=False, symbols=parser.symbols)
    return Parsed(tree, written.statement(), parser.symbols)


def describe(tree: sp.Basic) -> dict[str, Any]:
    """The canonical, deterministic description of a parsed tree (VER-01)."""
    return {
        "kind": "relation" if isinstance(tree, (sp.core.relational.Relational, sp.And)) else "expression",
        "srepr": sp.srepr(tree),
        "latex": sp.latex(tree),
        "text": sp.sstr(tree),
        "free_symbols": sorted(str(s) for s in tree.free_symbols),
    }
