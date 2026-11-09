"""A level statistics (VER-30): `stats_distribution` — binomial, Poisson and normal probabilities, hypothesis tests,
correlation (PMCC and Spearman's rank) and least-squares regression lines.

Exactness. Given numbers stay exact rationals ("0.35", "1/6"). Binomial probabilities are exact fractions summed from
the definition C(n, r) p^r (1 - p)^(n - r); Poisson probabilities are e^(-lambda) times an exact rational sum, so
their exact form is printed and the decimal is evaluated from it. Normal probabilities are exact in erf
(Phi(z) = (1 + erf(z / sqrt 2)) / 2) and inverse values exact in erfinv, evaluated to 40 digits by SymPy: the same
digits on every machine (VER-01). PMCC, Spearman's coefficient and regression coefficients are exact (r is a surd).

Every number is `{decimal, unrounded}` plus its exact `text` and `latex` when they are short: `decimal` is rounded half
away from zero to the significant figures in force, `unrounded` keeps 15 for working that carries on.

Conventions, each from the call, then the pack's conventions sent with the call, then the tool default with warning
`convention_default_used` (the source is in the result's `conventions`):
- `sf` (pack `numeric.sf`, default 4): significant figures of every `decimal`.
- `normal_values` (pack `stats.normal_values`, default `calculator`): `calculator` uses exact Phi and its inverse;
  `tables` reads the printed tables: z rounded to 2 decimal places and Phi(z) to 4, and an inverse z to 4 places for a
  percentage point of the table (upper tail 0.5, 0.4, 0.3, 0.2, 0.15, 0.1, 0.05, 0.025, 0.02, 0.01, 0.005, 0.0025,
  0.001, 0.0005) or the nearest 2-place z otherwise.
- `tie_method` (pack `stats.spearman_ties`, default `pmcc_of_ranks`, asked only when the data have ties): tied values
  take the mean of their ranks; then either the PMCC of the ranks (exact with ties) or the formula
  1 - 6 sum d^2 / (n (n^2 - 1)) as many textbooks do. Without ties the two agree and the formula is used.

Hypothesis tests follow the A level convention: a one-tailed test compares the tail probability with the significance
level; a two-tailed test compares the tail in the direction of the observation with half of it, and each tail of the
critical region has probability at most half the level. A discrete critical region is the largest such region; its
actual significance level is the probability of the whole region under H0. A PMCC test uses the call's critical value
(from the exam board's table) or computes it from the t distribution on n - 2 degrees of freedom; a Spearman test
needs the table's critical value, which differs between boards, and is `unknown` without it.

Regression is least squares: y on x (b = Sxy / Sxx, a = mean y - b mean x) or x on y. An estimate outside the range
of the data is extrapolation and is flagged; one inside is interpolation.

Contradictory input (a probability outside 0 to 1, a sample whose sums give |r| > 1, a standard deviation that would
be negative) is a DOMAIN_ERROR with no number; input that leaves the answer open (any standard deviation fits) is
`unknown` with its reason (VER-27)."""

from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from math import comb
from typing import Any

import mpmath
import sympy as sp

from tool_worker.functions.common import as_expr, convention
from tool_worker.functions.maths import significant
from tool_worker.functions.money import choice
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]
Value = dict[str, Any]

DEFAULT_SF = 4
NORMAL_VALUES = ("calculator", "tables")
TIE_METHODS = ("pmcc_of_ranks", "formula")
# Upper-tail probabilities printed in the percentage points table of the normal distribution.
PERCENTAGE_POINTS = frozenset(
    Fraction(p) for p in ("0.5", "0.4", "0.3", "0.2", "0.15", "0.1", "0.05", "0.025", "0.02", "0.01", "0.005",
                          "0.0025", "0.001", "0.0005")
)  # fmt: skip
MAX_TRIALS = 1000
MAX_LAMBDA = 500
MAX_COUNT = 5000
MAX_EXACT_TEXT = 80
PRECISION = 50  # digits for comparisons of transcendental values
SYMBOLS = {"eq": "=", "le": "<=", "lt": "<", "ge": ">=", "gt": ">"}


def _domain(message: str) -> WorkerError:
    return WorkerError(Code.DOMAIN_ERROR, message)


class Undetermined(Exception):
    """The input does not determine the answer: the result is `unknown` with this reason."""

    def __init__(self, reason: str, needs: str, code: str = "domain_restricted") -> None:
        super().__init__(needs)
        self.reason, self.needs, self.code = reason, needs, code


# --- numbers ----------------------------------------------------------------------------------------------------------


def rational(raw: Any, field: str) -> Fraction:
    """A given number (integer, decimal or fraction, JSON number or string) as an exact fraction."""
    if isinstance(raw, bool):
        raise _domain(f"{field}: not a number")
    text = repr(raw) if isinstance(raw, float) else str(raw).strip()
    try:
        if "/" in text:
            top, bottom = text.split("/")
            return Fraction(Decimal(top.strip())) / Fraction(Decimal(bottom.strip()))
        return Fraction(Decimal(text))
    except ZeroDivisionError:
        raise _domain(f"{field}: division by zero") from None
    except (ArithmeticError, ValueError):
        raise _domain(f"{field}: not a number") from None


def probability(raw: Any, field: str, *, open_interval: bool = False) -> Fraction:
    value = rational(raw, field)
    if open_interval and not 0 < value < 1:
        raise _domain(f"{field} must be strictly between 0 and 1")
    if not 0 <= value <= 1:
        raise _domain(f"{field} is a probability, from 0 to 1")
    return value


def whole(raw: Any, field: str, *, low: int = 0, high: int | None = None) -> int:
    value = rational(raw, field)
    if value.denominator != 1:
        raise _domain(f"{field} must be a whole number")
    if value < low or (high is not None and value > high):
        raise _domain(f"{field} must be from {low}" + (f" to {high}" if high is not None else " upwards"))
    return int(value)


def level(raw: Any) -> Fraction:
    """A significance level as a fraction: 0.05 or "5%"; above 0 and at most one half."""
    text = str(raw).strip()
    value = (
        rational(text[:-1], "significance_level") / 100 if text.endswith("%") else rational(raw, "significance_level")
    )
    if value >= 1 and not text.endswith("%"):
        raise _domain('significance_level: give it as a fraction such as 0.05, or as a percentage such as "5%"')
    if not 0 < value <= Fraction(1, 2):
        raise _domain("significance_level must be above 0 and at most 0.5")
    return value


def _text(value: Fraction | int | sp.Expr) -> str:
    if isinstance(value, sp.Basic):
        return str(sp.sstr(value))
    value = Fraction(value)
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def _sym(value: Fraction | sp.Expr) -> sp.Expr:
    return as_expr(value)


def _float(value: Fraction | sp.Expr) -> sp.Float:
    return sp.Float(sp.N(_sym(value), PRECISION), PRECISION)


def _round_dp(value: Fraction | sp.Expr, dp: int) -> Fraction:
    """Half away from zero to `dp` decimal places, as an exact fraction."""
    exact = Decimal(str(sp.N(_sym(value), PRECISION)))
    return Fraction(exact.quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP))


# --- the call -------------------------------------------------------------------------------------------------------


class Call:
    """One call's arguments, conventions, working lines and warnings."""

    def __init__(self, args: dict[str, Any], context: CallContext) -> None:
        self.args, self.context, self.check = args, context, context.check_id
        self.working: list[dict[str, Any]] = []
        self.warnings: list[dict[str, str]] = []
        self.conventions: dict[str, dict[str, Any]] = {}
        self.method = "exact"
        self.sf = self._sf()

    def _sf(self) -> int:
        if "sf" in self.args:
            sf, source = int(self.args["sf"]), "item"
        else:
            held = convention(self.context, "numeric.sf")
            if held is not None and str(held[0]).isdigit() and 1 <= int(held[0]) <= 15:
                sf, source = int(held[0]), held[1]
            else:
                sf, source = DEFAULT_SF, "default"
                self.warn("convention_default_used", f"no numeric.sf convention was stated; the tool default "
                          f"{DEFAULT_SF} significant figures was used")  # fmt: skip
        self.conventions["sf"] = {"value": sf, "source": source}
        return sf

    def choose(self, field: str, key: str, allowed: tuple[str, ...], default: str) -> str:
        value, source, warnings = choice(self.args, self.context, field, key, allowed, default)
        self.conventions[field] = {"value": value, "source": source}
        self.warnings.extend(warnings)
        return value

    def warn(self, code: str, message: str) -> None:
        self.warnings.append({"code": code, "message": message[:500]})

    def line(self, label: str, value: str) -> None:
        self.working.append({"label": label, "value": value, "check_id": self.check})

    def num(self, value: Fraction | int | sp.Expr) -> dict[str, Any]:
        """A number with its decimal to the significant figures in force, 15 unrounded, and its exact form when
        short."""
        expr = _sym(Fraction(value)) if isinstance(value, int) else _sym(value)
        out: dict[str, Any] = {"decimal": significant(expr, self.sf), "unrounded": significant(expr, 15)}
        text = sp.sstr(expr)
        if len(text) <= MAX_EXACT_TEXT and not expr.atoms(sp.Float):  # a computed decimal has no exact form
            out["text"], out["latex"] = text, sp.latex(expr)
        return out

    def extras(self) -> Extras:
        extras: Extras = {"method": self.method, "confidence": "proved", "working": self.working}
        if self.warnings:
            extras["warnings"] = self.warnings
        return extras


# --- discrete distributions ------------------------------------------------------------------------------------------


def binomial_pmf(n: int, p: Fraction) -> Callable[[int], Fraction]:
    q = 1 - p
    return lambda r: comb(n, r) * p**r * q ** (n - r) if 0 <= r <= n else Fraction(0)


def poisson_terms(lam: Fraction, top: int) -> list[Fraction]:
    """lambda^r / r! for r = 0..top, exactly (the probabilities without the factor e^(-lambda))."""
    terms, term = [], Fraction(1)
    for r in range(top + 1):
        if r:
            term = term * lam / r
        terms.append(term)
    return terms


def event(args: dict[str, Any], top: int | None) -> tuple[int, int | None, str]:
    """The event's whole-number interval [low, high] (high None: no upper end) and how it reads."""
    relation = args["relation"]
    k = whole(args["k"], "k")
    if relation == "between":
        if "k2" not in args:
            raise _domain("between needs k2, the upper end (both ends included)")
        k2 = whole(args["k2"], "k2")
        if k2 < k:
            raise _domain("between: k2 must be at least k")
        return k, k2, f"P({k} <= X <= {k2})"
    if "k2" in args:
        raise _domain("k2 is only for relation between")
    reading = f"P(X {SYMBOLS[relation]} {k})"
    if relation == "eq":
        return k, k, reading
    if relation == "le":
        return 0, k, reading
    if relation == "lt":
        return 0, k - 1, reading
    if relation == "ge":
        return k, top, reading
    return k + 1, top, reading


def _binomial(call: Call) -> Value:
    args = call.args
    n = whole(args["n"], "n", low=1, high=MAX_TRIALS)
    p = probability(args["p"], "p")
    pmf = binomial_pmf(n, p)
    value: Value = {"task": "binomial", "n": n, "p": _text(p)}
    if "relation" in args:
        low, high, reading = event(args, n)
        top = n if high is None else min(high, n)
        start = max(low, 0)
        total = sum((pmf(r) for r in range(start, top + 1)), Fraction(0))
        value |= {"event": reading, "interval": [start, top] if start <= top else None, "probability": call.num(total)}
        call.line(reading, f"sum over r = {start} to {top} of C({n}, r) ({_text(p)})^r ({_text(1 - p)})^({n} - r)")
    mean, variance = n * p, n * p * (1 - p)
    value |= {"mean": call.num(mean), "variance": call.num(variance), "sd": call.num(sp.sqrt(_sym(variance)))}
    call.line("mean", f"np = {_text(mean)}")
    call.line("variance", f"np(1 - p) = {_text(variance)}")
    return value


def _poisson(call: Call) -> Value:
    args = call.args
    lam = rational(args["lambda"], "lambda")
    if not 0 < lam <= MAX_LAMBDA:
        raise _domain(f"lambda must be above 0 and at most {MAX_LAMBDA}")
    value: Value = {"task": "poisson", "lambda": _text(lam)}
    if "relation" in args:
        low, high, reading = event(args, None)
        if max(low, high or 0) > MAX_COUNT:
            raise WorkerError(Code.LIMIT_EXCEEDED, f"k is above {MAX_COUNT}", limit="k")
        e = sp.exp(-_sym(lam))
        if high is None:  # P(X >= low) = 1 - P(X <= low - 1)
            below = sum(poisson_terms(lam, low - 1), Fraction(0)) if low > 0 else Fraction(0)
            total: sp.Expr = 1 - e * _sym(below)
            call.line(reading, f"1 - e^(-{_text(lam)}) x (sum over r = 0 to {low - 1} of {_text(lam)}^r / r!)")
        elif low > high:
            total = sp.Integer(0)
        else:
            inside = sum(poisson_terms(lam, high)[low:], Fraction(0))
            total = e * _sym(inside)
            call.line(reading, f"e^(-{_text(lam)}) x (sum over r = {low} to {high} of {_text(lam)}^r / r!)")
        value |= {"event": reading, "interval": [low, high] if high is None or low <= high else None,
                  "probability": call.num(total)}  # fmt: skip
    value |= {"mean": call.num(lam), "variance": call.num(lam), "sd": call.num(sp.sqrt(_sym(lam)))}
    call.line("mean and variance", f"lambda = {_text(lam)}")
    return value


# --- the normal distribution -----------------------------------------------------------------------------------------

SQRT2 = sp.sqrt(2)


def phi(z: sp.Expr) -> sp.Expr:
    return (1 + sp.erf(z / SQRT2)) / 2


class Normal:
    """Phi and its inverse, by calculator (exact erf) or by the printed tables."""

    def __init__(self, call: Call) -> None:
        self.call = call
        self.tables = call.choose("normal_values", "stats.normal_values", NORMAL_VALUES, "calculator") == "tables"
        call.method = "table_lookup" if self.tables else "symbolic"

    def z(self, z: sp.Expr) -> sp.Expr:
        """z as it is used: exact, or rounded to 2 decimal places to read the table."""
        return _sym(_round_dp(z, 2)) if self.tables else z

    def cdf(self, z: sp.Expr) -> sp.Expr:
        if not self.tables:
            return phi(z)
        z2 = _round_dp(z, 2)
        read = _round_dp(phi(_sym(abs(z2))), 4)
        return _sym(read if z2 >= 0 else 1 - read)

    def inverse(self, q: Fraction) -> sp.Expr:
        """z with Phi(z) = q, 0 < q < 1."""
        z = SQRT2 * sp.erfinv(_sym(2 * q - 1))
        if not self.tables:
            return z
        upper = min(q, 1 - q)
        places = 4 if upper in PERCENTAGE_POINTS else 2
        return _sym(_round_dp(z, places))


def _parameters(call: Call, need_mean: bool = True, need_sd: bool = True) -> tuple[Fraction | None, sp.Expr | None]:
    """The mean and the standard deviation (exact; a surd when a variance that is not a square is given)."""
    args = call.args
    mean = rational(args["mean"], "mean") if "mean" in args else None
    if need_mean and mean is None:
        raise _domain("give the mean")
    sd: sp.Expr | None = None
    if "sd" in args and "variance" in args:
        raise _domain("give sd or variance, not both")
    if "sd" in args:
        given = rational(args["sd"], "sd")
        if given <= 0:
            raise _domain("sd must be positive")
        sd = _sym(given)
    elif "variance" in args:
        variance = rational(args["variance"], "variance")
        if variance <= 0:
            raise _domain("variance must be positive")
        sd = sp.sqrt(_sym(variance))
    if need_sd and sd is None:
        raise _domain("give sd (or variance)")
    return mean, sd


def _tail(raw: str, p: Fraction) -> Fraction:
    """Phi(z) for the condition P(X < x) = p (below) or P(X > x) = p (above)."""
    return p if raw == "below" else 1 - p


def _normal(call: Call) -> Value:
    args = call.args
    find = args.get("find", "probability")
    normal = Normal(call)
    value: Value = {"task": "normal", "find": find}
    if find in ("probability", "z", "value"):
        mean, sd = _parameters(call)
        assert mean is not None and sd is not None
        value |= {"mean": _text(mean), "sd": _text(sd)}
        if find == "z":
            x = rational(args["x"], "x")
            z = _sym(x - mean) / sd
            call.line("z", f"(x - mu) / sigma = ({_text(x)} - {_text(mean)}) / {_text(sd)} = {_text(z)}")
            return value | {"z": call.num(z)}
        if find == "probability":
            return value | _normal_probability(call, normal, mean, sd)
        return value | _normal_value(call, normal, mean, sd)
    if find == "mean":
        _, sd = _parameters(call, need_mean=False)
        assert sd is not None
        x, p = rational(args["x"], "x"), probability(args["probability"], "probability", open_interval=True)
        z = normal.inverse(_tail(args["tail"], p))
        mean = _sym(x) - z * _sym(sd)
        call.line("z", f"Phi(z) = {_text(_tail(args['tail'], p))}, z = {significant(z, 15)}")
        call.line("mean", "mu = x - z sigma")
        return value | {"sd": _text(sd), "z": call.num(z), "mean": call.num(mean)}
    if find == "sd":
        mean, _ = _parameters(call, need_sd=False)
        assert mean is not None
        x, p = rational(args["x"], "x"), probability(args["probability"], "probability", open_interval=True)
        z = normal.inverse(_tail(args["tail"], p))
        call.line("z", f"Phi(z) = {_text(_tail(args['tail'], p))}, z = {significant(z, 15)}")
        if z == 0:
            if x == mean:
                raise Undetermined("underdetermined", "x is the mean and the probability is 0.5: every sd fits")
            raise _domain("a probability of 0.5 puts x at the mean, but x is not the mean")
        sd = (_sym(x) - _sym(mean)) / z
        if not _float(sd) > 0:
            raise _domain("the standard deviation would be negative: x and the probability are on opposite sides of "
                          "the mean")  # fmt: skip
        call.line("sd", "sigma = (x - mu) / z")
        return value | {"mean": _text(mean), "z": call.num(z), "sd": call.num(sd)}
    # mean_and_sd: two conditions x_i = mu + z_i sigma
    conditions = args["conditions"]
    xs, zs = [], []
    for i, c in enumerate(conditions):
        p = probability(c["probability"], f"conditions[{i}].probability", open_interval=True)
        xs.append(_sym(rational(c["x"], f"conditions[{i}].x")))
        zs.append(normal.inverse(_tail(c["tail"], p)))
        call.line(f"z{i + 1}", f"Phi(z) = {_text(_tail(c['tail'], p))}, z = {significant(zs[-1], 15)}")
    if zs[0] == zs[1]:
        if xs[0] == xs[1]:
            raise Undetermined("underdetermined", "the two conditions are the same: give a second, different one")
        raise _domain("the two conditions give the same z for different x: no normal distribution fits")
    sd = (xs[1] - xs[0]) / (zs[1] - zs[0])
    if not _float(sd) > 0:
        raise _domain("the conditions need a negative standard deviation: no normal distribution fits")
    mean = xs[0] - zs[0] * sd
    call.line("solve", "x1 = mu + z1 sigma and x2 = mu + z2 sigma simultaneously")
    return value | {"z": [call.num(z) for z in zs], "mean": call.num(mean), "sd": call.num(sd)}


def _normal_probability(call: Call, normal: Normal, mean: Fraction, sd: sp.Expr) -> Value:
    args = call.args
    relation = args["relation"]
    x = rational(args["x"], "x")
    z = _sym(x - mean) / sd
    if relation == "eq":
        call.line("continuous", "P(X = x) = 0 for a continuous distribution")
        return {"event": f"P(X = {_text(x)})", "z": call.num(z), "probability": call.num(0)}
    if relation == "between":
        if "x2" not in args:
            raise _domain("between needs x2, the upper end")
        x2 = rational(args["x2"], "x2")
        if x2 < x:
            raise _domain("between: x2 must be at least x")
        z2 = _sym(x2 - mean) / sd
        total = sp.expand(normal.cdf(_sym(z2)) - normal.cdf(_sym(z)))
        call.line("z", f"z1 = {_text(z)}, z2 = {_text(z2)}" + (" (2 d.p. for the table)" if normal.tables else ""))
        call.line("probability", "Phi(z2) - Phi(z1)")
        return {"event": f"P({_text(x)} < X < {_text(x2)})", "z": [call.num(normal.z(_sym(z))),
                call.num(normal.z(_sym(z2)))], "probability": call.num(total)}  # fmt: skip
    if "x2" in args:
        raise _domain("x2 is only for relation between")
    below = relation in ("le", "lt")
    total = normal.cdf(_sym(z)) if below else sp.expand(1 - normal.cdf(_sym(z)))
    call.line("z", f"(x - mu) / sigma = {_text(z)}" + (" (2 d.p. for the table)" if normal.tables else ""))
    call.line("probability", "Phi(z)" if below else "1 - Phi(z)")
    return {"event": f"P(X {SYMBOLS[relation]} {_text(x)})", "z": call.num(normal.z(_sym(z))),
            "probability": call.num(total)}  # fmt: skip


def _normal_value(call: Call, normal: Normal, mean: Fraction, sd: sp.Expr) -> Value:
    args = call.args
    p = probability(args["probability"], "probability", open_interval=True)
    tail = args["tail"]
    if tail == "central":
        z = normal.inverse((1 + p) / 2)
        call.line("z", f"Phi(z) = {_text((1 + p) / 2)}, z = {significant(z, 15)}")
        call.line("values", "mu - z sigma and mu + z sigma")
        return {"z": call.num(z), "lower": call.num(_sym(mean) - z * _sym(sd)),
                "upper": call.num(_sym(mean) + z * _sym(sd))}  # fmt: skip
    q = _tail(tail, p)
    z = normal.inverse(q)
    call.line("z", f"Phi(z) = {_text(q)}, z = {significant(z, 15)}")
    call.line("value", "x = mu + z sigma")
    return {"z": call.num(z), "x": call.num(_sym(mean) + z * _sym(sd))}


# --- correlation and regression --------------------------------------------------------------------------------------


class Sums:
    """n and the sums of squares and products, from paired data or from the summary statistics given."""

    def __init__(self, call: Call, *, need_y2: bool = True) -> None:
        args = call.args
        self.x: list[Fraction] | None = None
        self.y: list[Fraction] | None = None
        if "x_values" in args or "y_values" in args:
            if "sums" in args:
                raise _domain("give the data (x_values and y_values) or the sums, not both")
            if "x_values" not in args or "y_values" not in args:
                raise _domain("give both x_values and y_values")
            limit = int(call.context.setting("stats_max_points", 500))
            if max(len(args["x_values"]), len(args["y_values"])) > limit:
                raise WorkerError(Code.LIMIT_EXCEEDED, f"more than {limit} pairs", limit="data_points")
            self.x = [rational(v, f"x_values[{i}]") for i, v in enumerate(args["x_values"])]
            self.y = [rational(v, f"y_values[{i}]") for i, v in enumerate(args["y_values"])]
            if len(self.x) != len(self.y):
                raise _domain(f"x_values has {len(self.x)} values and y_values {len(self.y)}: give them in pairs")
            n = len(self.x)
            sx, sy = sum(self.x, Fraction(0)), sum(self.y, Fraction(0))
            sxx = sum((v * v for v in self.x), Fraction(0)) - sx * sx / n
            syy = sum((v * v for v in self.y), Fraction(0)) - sy * sy / n
            sxy = sum((a * b for a, b in zip(self.x, self.y, strict=True)), Fraction(0)) - sx * sy / n
        elif "sums" in args:
            given = args["sums"]
            n = whole(given["n"], "sums.n", low=2)
            sx, sy = rational(given["sum_x"], "sums.sum_x"), rational(given["sum_y"], "sums.sum_y")
            sxx = rational(given["sum_x2"], "sums.sum_x2") - sx * sx / n
            sxy = rational(given["sum_xy"], "sums.sum_xy") - sx * sy / n
            if "sum_y2" in given:
                syy = rational(given["sum_y2"], "sums.sum_y2") - sy * sy / n
            elif need_y2:
                raise _domain("sums: this needs sum_y2")
            else:
                syy = Fraction(0)
        else:
            raise _domain("give the data (x_values and y_values) or the sums")
        if n < 2:
            raise _domain("at least two pairs are needed")
        if sxx < 0 or syy < 0:
            raise _domain("the sums are inconsistent: a sum of squares about the mean would be negative")
        self.has_y2 = need_y2 or self.y is not None or "sum_y2" in args.get("sums", {})
        if self.has_y2 and sxy * sxy > sxx * syy:
            raise _domain("the sums are inconsistent: they would give |r| > 1")
        self.n, self.mean_x, self.mean_y = n, sx / n, sy / n
        self.sxx, self.syy, self.sxy = sxx, syy, sxy

    def r(self) -> sp.Expr:
        if self.sxx == 0 or self.syy == 0:
            which = "x" if self.sxx == 0 else "y"
            raise _domain(f"every {which} value is the same, so r is undefined")
        return sp.radsimp(_sym(self.sxy) / sp.sqrt(_sym(self.sxx * self.syy)))


def ranks(values: list[Fraction]) -> list[Fraction]:
    """Ranks from 1 for the smallest; tied values share the mean of their ranks."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [Fraction(0)] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = Fraction(i + j + 2, 2)
        i = j + 1
    return out


def _spearman(call: Call, x: list[Fraction], y: list[Fraction]) -> tuple[sp.Expr, Value]:
    """Spearman's coefficient and how it was found."""
    n = len(x)
    rx, ry = ranks(x), ranks(y)
    ties = len(set(x)) < n or len(set(y)) < n
    d2 = sum(((a - b) ** 2 for a, b in zip(rx, ry, strict=True)), Fraction(0))
    method = call.choose("tie_method", "stats.spearman_ties", TIE_METHODS, "pmcc_of_ranks") if ties else "formula"
    if method == "formula":
        rs: sp.Expr = _sym(1 - 6 * d2 / (n * (n * n - 1)))
        call.line("Spearman", f"1 - 6 x {_text(d2)} / ({n} x ({n}^2 - 1))")
    else:
        mean = Fraction(n + 1, 2)
        sab = sum(((a - mean) * (b - mean) for a, b in zip(rx, ry, strict=True)), Fraction(0))
        saa = sum(((a - mean) ** 2 for a in rx), Fraction(0))
        sbb = sum(((b - mean) ** 2 for b in ry), Fraction(0))
        if saa == 0 or sbb == 0:
            raise _domain("every value is tied, so Spearman's coefficient is undefined")
        rs = sp.radsimp(_sym(sab) / sp.sqrt(_sym(saa * sbb)))
        call.line("Spearman", "the PMCC of the ranks (tied values share the mean rank)")
    return rs, {"rs": call.num(rs), "ranks_x": [_text(r) for r in rx], "ranks_y": [_text(r) for r in ry],
                "sum_d2": _text(d2), "ties": ties, "tie_method": method,
                "ranking": "1 for the smallest; tied values share the mean of their ranks"}  # fmt: skip


def _correlation(call: Call) -> Value:
    measure = call.args.get("measure", "both" if "x_values" in call.args else "pmcc")
    sums = Sums(call)
    value: Value = {"task": "correlation", "n": sums.n}
    if measure in ("pmcc", "both"):
        r = sums.r()
        call.line("Sxx, Syy, Sxy", f"{_text(sums.sxx)}, {_text(sums.syy)}, {_text(sums.sxy)}")
        call.line("r", "Sxy / sqrt(Sxx Syy)")
        value |= {"sxx": call.num(sums.sxx), "syy": call.num(sums.syy), "sxy": call.num(sums.sxy), "r": call.num(r)}
    if measure in ("spearman", "both"):
        if sums.x is None or sums.y is None:
            raise _domain("Spearman's rank needs the data (x_values and y_values), not sums")
        value["spearman"] = _spearman(call, sums.x, sums.y)[1]
    return value


def _regression(call: Call) -> Value:
    args = call.args
    line = args.get("line", "y_on_x")
    on_x = line == "y_on_x"
    sums = Sums(call, need_y2=not on_x)
    spread, mean_in, mean_out = (sums.sxx, sums.mean_x, sums.mean_y) if on_x else (sums.syy, sums.mean_y, sums.mean_x)
    if spread == 0:
        raise _domain(f"every {'x' if on_x else 'y'} value is the same: there is no least-squares line")
    b = sums.sxy / spread
    a = mean_out - b * mean_in
    names = ("y", "x") if on_x else ("x", "y")
    var = sp.Symbol(names[1])
    equation = f"{names[0]} = {sp.sstr(_sym(a) + _sym(b) * var)}"
    call.line("gradient", f"b = Sxy / S{names[1]}{names[1]} = {_text(sums.sxy)} / {_text(spread)} = {_text(b)}")
    call.line("intercept", f"a = mean {names[0]} - b x mean {names[1]} = {_text(a)}")
    value: Value = {"task": "regression", "line": line, "n": sums.n, "equation": equation, "a": call.num(a),
                    "b": call.num(b), "mean_point": [call.num(sums.mean_x), call.num(sums.mean_y)]}  # fmt: skip
    if sums.has_y2 and sums.sxx > 0 and sums.syy > 0:
        value["r"] = call.num(sums.r())
    if "predict" in args:
        data = sums.x if on_x else sums.y
        low: Fraction | None
        high: Fraction | None
        if data is not None:
            low, high = min(data), max(data)
        elif "explanatory_range" in args:
            low, high = (rational(v, "explanatory_range") for v in args["explanatory_range"])
            if high < low:
                raise _domain("explanatory_range: give the smallest value first")
        else:
            low = high = None
        estimates = []
        for i, raw in enumerate(args["predict"]):
            at = rational(raw, f"predict[{i}]")
            estimate: dict[str, Any] = {"at": _text(at), "estimate": call.num(a + b * at)}
            if low is None or high is None:
                estimate["reliability"] = "unknown_range"
            elif low <= at <= high:
                estimate["reliability"] = "interpolation"
            else:
                estimate["reliability"] = "extrapolation"
                call.warn("domain_restricted", f"{names[1]} = {_text(at)} is outside the data ({_text(low)} to "
                          f"{_text(high)}): extrapolation, the estimate is unreliable")  # fmt: skip
            estimates.append(estimate)
        if low is None:
            call.warn("domain_restricted", "the range of the data is not known (give explanatory_range): whether an "
                      "estimate is interpolation or extrapolation is not checked")  # fmt: skip
        value["predictions"] = estimates
    return value


# --- hypothesis tests ------------------------------------------------------------------------------------------------

ALTERNATIVES = {"less": "<", "greater": ">", "two_sided": "!="}


def _le(a: sp.Expr, b: Fraction) -> bool:
    """a <= b: exactly for rationals, at 50 digits for a Poisson probability (never equal to a rational)."""
    if a.is_Rational:
        return bool(a <= _sym(b))
    return bool(_float(a) <= _float(b))


class Discrete:
    """A discrete distribution's tails under H0, exact (binomial) or e^(-lambda) times exact sums (Poisson)."""

    def __init__(self, test: str, args: dict[str, Any]) -> None:
        self.test = test
        self.partial: list[Fraction] = []
        if test == "binomial":
            self.n = whole(args["n"], "n", low=1, high=MAX_TRIALS)
            self.p = probability(args["p"], "p", open_interval=True)
            pmf = binomial_pmf(self.n, self.p)
            running = Fraction(0)
            for r in range(self.n + 1):
                running += pmf(r)
                self.partial.append(running)
            self.mean = self.n * self.p
            self.parameter = f"p = {_text(self.p)}"
            self.top = self.n
        else:
            self.lam = rational(args["lambda"], "lambda")
            if not 0 < self.lam <= MAX_LAMBDA:
                raise _domain(f"lambda must be above 0 and at most {MAX_LAMBDA}")
            self.e = sp.exp(-_sym(self.lam))
            self.term = Fraction(1)
            self.mean = self.lam
            self.parameter = f"lambda = {_text(self.lam)}"
            self.top = MAX_COUNT

    def _cumulative(self, c: int) -> Fraction:
        """The sum of lambda^r / r! for r = 0..c, extended as needed (binomial: P(X <= c) itself)."""
        while len(self.partial) <= c:
            r = len(self.partial)
            if r:
                self.term = self.term * self.lam / r
            self.partial.append((self.partial[-1] if r else Fraction(0)) + self.term)
        return self.partial[c]

    def at_most(self, c: int) -> sp.Expr:
        """P(X <= c) under H0."""
        if c < 0:
            return sp.Integer(0)
        if self.test == "binomial":
            return _sym(self.partial[min(c, self.n)])
        return self.e * _sym(self._cumulative(c))

    def at_least(self, c: int) -> sp.Expr:
        if c <= 0:
            return sp.Integer(1)
        if self.test == "binomial" and c > self.n:
            return sp.Integer(0)
        return 1 - self.at_most(c - 1)

    def lower_region(self, tail: Fraction) -> int | None:
        """The largest c with P(X <= c) <= tail, or None when even P(X = 0) is above it."""
        found, c = None, 0
        while c <= self.top and _le(self.at_most(c), tail):
            found, c = c, c + 1
        return found

    def upper_region(self, tail: Fraction) -> int | None:
        """The smallest c with P(X >= c) <= tail, or None when even P(X = n) is above it (binomial)."""
        if self.test == "binomial":
            found, c = None, self.n
            while c >= 0 and _le(self.at_least(c), tail):
                found, c = c, c - 1
            return found
        c = int(self.lam)
        while c <= self.top:
            if _le(self.at_least(c), tail):
                return c
            c += 1
        raise WorkerError(Code.LIMIT_EXCEEDED, f"the critical region starts beyond {MAX_COUNT}", limit="k")


def _discrete_test(call: Call, test: str) -> Value:
    args = call.args
    model = Discrete(test, args)
    alternative = args["alternative"]
    alpha = level(args["significance_level"])
    tail = alpha / 2 if alternative == "two_sided" else alpha
    symbol = "p" if test == "binomial" else "lambda"
    value: Value = {"task": "hypothesis_test", "test": test, "h0": model.parameter,
                    "h1": f"{symbol} {ALTERNATIVES[alternative]} {model.parameter.split(' = ')[1]}",
                    "significance_level": _text(alpha)}  # fmt: skip
    region: dict[str, Any] = {}
    actual = sp.Integer(0)
    if alternative in ("less", "two_sided"):
        c = model.lower_region(tail)
        if c is None:
            region["lower"] = None
        else:
            actual += model.at_most(c)
            region["lower"] = {"x_max": c, "probability": call.num(model.at_most(c))}
    if alternative in ("greater", "two_sided"):
        c = model.upper_region(tail)
        if c is None:
            region["upper"] = None
        else:
            actual += model.at_least(c)
            region["upper"] = {"x_min": c, "probability": call.num(model.at_least(c))}
    value |= {"critical_region": region, "actual_significance_level": call.num(actual)}
    call.line("critical region", f"each tail with probability at most {_text(tail)} under H0 ({model.parameter})")
    if "observed" in args:
        x = whole(args["observed"], "observed", high=model.top)
        if alternative == "less" or (alternative == "two_sided" and x < model.mean):
            direction, p_value, reading = "lower", model.at_most(x), f"P(X <= {x})"
        elif alternative == "greater" or x > model.mean:
            direction, p_value, reading = "upper", model.at_least(x), f"P(X >= {x})"
        else:  # at the mean: neither tail is small
            direction, p_value, reading = "upper", model.at_least(x), f"P(X >= {x})"
        reject = _le(p_value, tail)
        call.line("tail probability", f"{reading} under H0, compared with {_text(tail)}")
        value |= {"observed": x, "tail": direction, "tail_event": reading, "tail_probability": call.num(p_value),
                  "compare_with": _text(tail), "in_critical_region": reject,
                  "conclusion": "reject_h0" if reject else "do_not_reject_h0"}  # fmt: skip
    return value


def _normal_mean_test(call: Call) -> Value:
    args = call.args
    normal = Normal(call)
    mu0 = rational(args["mean"], "mean")
    _, sd = _parameters(call, need_mean=False)
    assert sd is not None
    n = whole(args.get("n", 1), "n", low=1)
    alternative = args["alternative"]
    alpha = level(args["significance_level"])
    tail = alpha / 2 if alternative == "two_sided" else alpha
    se = _sym(sd) / sp.sqrt(n)
    critical = normal.inverse(1 - tail)
    value: Value = {"task": "hypothesis_test", "test": "normal_mean", "h0": f"mu = {_text(mu0)}",
                    "h1": f"mu {ALTERNATIVES[alternative]} {_text(mu0)}", "significance_level": _text(alpha),
                    "standard_error": call.num(se), "critical_z": call.num(critical)}  # fmt: skip
    region: dict[str, Any] = {}
    if alternative in ("less", "two_sided"):
        region["lower"] = {"sample_mean_max": call.num(_sym(mu0) - critical * se)}
    if alternative in ("greater", "two_sided"):
        region["upper"] = {"sample_mean_min": call.num(_sym(mu0) + critical * se)}
    value["critical_region"] = region
    call.line("standard error", f"sigma / sqrt(n) = {_text(sd)} / sqrt({n})")
    if "sample_mean" in args:
        xbar = rational(args["sample_mean"], "sample_mean")
        z = sp.radsimp((_sym(xbar) - _sym(mu0)) / se)
        lower = alternative == "less" or (alternative == "two_sided" and xbar < mu0)
        p_value = normal.cdf(z) if lower else sp.expand(1 - normal.cdf(z))
        zf, cf = _float(z), _float(critical)
        reject = bool(zf <= -cf) if lower else bool(zf >= cf)
        call.line("z", f"(x bar - mu) / (sigma / sqrt(n)) = {significant(z, 15)}")
        call.line("tail probability", f"{'Phi(z)' if lower else '1 - Phi(z)'}, compared with {_text(tail)}")
        value |= {"sample_mean": _text(xbar), "z": call.num(z), "tail": "lower" if lower else "upper",
                  "tail_probability": call.num(p_value), "compare_with": _text(tail), "in_critical_region": reject,
                  "conclusion": "reject_h0" if reject else "do_not_reject_h0"}  # fmt: skip
    return value


def r_critical(nu: int, tail: Fraction) -> sp.Float:
    """The PMCC critical value for a one-tail probability `tail` on nu = n - 2 degrees of freedom: the r with
    P(R >= r) = I_(1 - r^2)(nu / 2, 1 / 2) / 2 = tail, by bisection at fixed precision (a private mpmath context, so
    the digits never depend on another call's precision)."""
    mp = mpmath.MPContext()
    mp.dps = PRECISION
    target = mp.mpf(tail.numerator) / tail.denominator
    a, half = mp.mpf(nu) / 2, mp.mpf(1) / 2
    low, high = mp.mpf(0), mp.mpf(1)
    for _ in range(110):
        middle = (low + high) / 2
        if mp.betainc(a, half, 0, 1 - middle * middle, regularized=True) / 2 > target:
            low = middle
        else:
            high = middle
    return sp.Float(mp.nstr((low + high) / 2, 40), 40)


def _correlation_test(call: Call, test: str) -> Value:
    args = call.args
    alternative = args["alternative"]
    alpha = level(args["significance_level"])
    tail = alpha / 2 if alternative == "two_sided" else alpha
    name = "rho" if test == "pmcc" else "rho_s"
    if "r" in args:
        r: sp.Expr = _sym(rational(args["r"], "r"))
        if not -1 <= r <= 1:
            raise _domain("r must be from -1 to 1")
        n = whole(args["n"], "n", low=3) if "n" in args else None
        if n is None:
            raise _domain("give n, the number of pairs")
    else:
        sums = Sums(call)
        n = sums.n
        if test == "pmcc":
            r = sums.r()
        else:
            if sums.x is None or sums.y is None:
                raise _domain("a Spearman test needs the data or rs")
            r = _spearman(call, sums.x, sums.y)[0]
        if n < 3:
            raise _domain("a correlation test needs at least 3 pairs")
    h1 = {"less": f"{name} < 0", "greater": f"{name} > 0", "two_sided": f"{name} != 0"}[alternative]
    value: Value = {"task": "hypothesis_test", "test": test, "h0": f"{name} = 0", "h1": h1, "n": n,
                    "significance_level": _text(alpha), "r": call.num(r)}  # fmt: skip
    if "critical_value" in args:
        critical: sp.Expr = _sym(rational(args["critical_value"], "critical_value"))
        if not 0 < critical < 1:
            raise _domain("critical_value: the table's value, above 0 and below 1")
        call.method = "table_lookup"
        value["critical_value_source"] = "item"
    elif test == "spearman":
        raise Undetermined("critical_value_needed", "Spearman's critical values come from the exam board's table: "
                           "give critical_value for n and the level", "convention_missing")  # fmt: skip
    else:
        critical = r_critical(n - 2, tail)
        call.method = "symbolic"
        value["critical_value_source"] = "computed"
        call.warn("numeric_only", f"the critical value is computed from the t distribution on {n - 2} degrees of "
                  "freedom to 40 digits; a printed table rounds it to 4 decimal places")  # fmt: skip
    value["critical_value"] = call.num(critical)
    rf, cf = _float(r), _float(critical)
    if alternative == "greater":
        reject = bool(rf >= cf)
    elif alternative == "less":
        reject = bool(rf <= -cf)
    else:
        reject = bool(abs(rf) >= cf)
    call.line("compare", f"r = {significant(r, 15)} with the critical value {significant(critical, 15)}"
              f" ({'two-tailed, ' if alternative == 'two_sided' else ''}{_text(tail)} in each tail)")  # fmt: skip
    return value | {"in_critical_region": reject, "conclusion": "reject_h0" if reject else "do_not_reject_h0"}


def _hypothesis_test(call: Call) -> Value:
    test = call.args["test"]
    if test in ("binomial", "poisson"):
        return _discrete_test(call, test)
    if test == "normal_mean":
        return _normal_mean_test(call)
    return _correlation_test(call, test)


# --- stats_distribution -----------------------------------------------------------------------------------------------

TASKS: dict[str, Callable[[Call], Value]] = {
    "binomial": _binomial,
    "poisson": _poisson,
    "normal": _normal,
    "hypothesis_test": _hypothesis_test,
    "correlation": _correlation,
    "regression": _regression,
}


def stats_distribution(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """A level statistics by task (VER-30); see the module docstring for the conventions."""
    call = Call(args, context)
    try:
        value = TASKS[args["task"]](call)
    except Undetermined as undetermined:
        found = {"task": args["task"], "status": "unknown", "reason": undetermined.reason, "needs": undetermined.needs}
        return found, {"method": "rule_based", "confidence": "undecided",
                       "warnings": [{"code": undetermined.code, "message": undetermined.needs}]}  # fmt: skip
    value["conventions"] = call.conventions
    return value, call.extras()
