"""Resolution tools per visual kind (VSL-13): a valid request becomes `computed` data from its kind's tools in the
sandboxed worker, and every check id those results carry joins the resolved spec's `numbers_from`.

A resolver gets the request (already through validator stages 1 to 6), `call`, which runs one worker tool and returns
its `fn` result (value and check record), and the pack conventions in force. A kind with no resolver here resolves to
nothing; a tool that fails, answers `unknown` or returns no check record raises, and the validator reports
`visual_resolution_failed`. Every number a renderer shows from a resolution is formatted here, once, into
`computed.labels` (part id -> display text), so the client never formats or computes a claim number itself.

Tools by kind (registers/visual-catalogue.json): function_graph and motion_graph -> math_plot_points (VER-14);
geometry_figure, bearing_diagram and transformation_grid -> geom_figure (VER-15); unit_circle, free_body components,
probability_tree and motion_diagram -> math_evaluate; circuit and circuit_sim -> phys_circuit and ray_diagram ->
phys_optics (VER-16); projectile_sim -> phys_projectile (VER-17); balanced_equation and particle_diagram ->
chem_balance (VER-19); titration_curve and titration_sim -> chem_acid_base (VER-21); gas_laws_sim -> math_evaluate
(VSL-46: ideal gas, p = nRT/V, with the model's R and the pack's kelvin offset); function_transform_sim ->
math_plot_points and math_evaluate (the tracked point); electrolysis_sim -> chem_balance (each half-equation) and
math_evaluate (Q = It and the amounts, with the pack's Faraday constant and molar volume); rates_sim -> math_evaluate
(k = A exp(-Ea/RT), a simplified first-order model); equilibrium_sim -> math_evaluate (Kc at the temperature, van't
Hoff) and math_solve (the equilibrium extent: chem_equilibrium is A level, VER-32, and not built); population_sim ->
math_evaluate (the logistic curve's closed form at the end of the run; the Lotka-Volterra equilibrium and conserved
quantity); exponential_curve -> math_plot_points and math_evaluate (the half-life or time constant); distribution_curve
-> math_plot_points and math_integrate (normal) or math_evaluate and math_simplify (binomial, Poisson):
stats_distribution is A level, VER-30, and not built; data_plot -> stats_compute (means and variances, so the
least-squares line) and math_evaluate (the gradient, intercept and triangle); journal_entry, t_account,
trial_balance and bank_reconciliation -> acc_double_entry, financial_statements -> acc_statements and break_even_chart
-> acc_break_even (VER-22); punnett_square and microscope_view -> math_evaluate (the class probabilities and the
magnification: bio_genetics and bio_calc, VER-35, are not built in the worker yet), while food_web, pedigree_chart and
gel_electrophoresis are checked and laid out with no tool (VSL-46: pack-supported facts and the question's givens);
organic_structure -> chem_organic (VER-33), energy_levels and born_haber_cycle ->
math_evaluate (the photon energies; the closing sum of the cycle, as chem_energetics has no cycle task yet),
maxwell_boltzmann -> math_plot_points and math_integrate (the curve and its tail above Ea), concentration_time ->
math_plot_points and math_evaluate (first-order decay, half-life and initial rate) or chem_balance (the reaction of an
illustrative equilibrium response) (VSL-46)."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from decimal import Decimal
from fractions import Fraction
from itertools import pairwise
from math import gcd
from typing import Any

from agent_core.assets.chemistry_kinds import expected_moves, part_id, unique_part_ids
from agent_core.assets.generated.sim_presets import SIM_PRESETS
from agent_core.assets.visual_kinds import (
    data_point_part,
    food_web_organisms,
    gel_bands,
    safe_id,
    say,
    surface_kind,
    tree_paths,
)

Call = Callable[[str, dict[str, Any]], dict[str, Any]]
Computed = dict[str, Any]
PLAIN = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
FRACTION = re.compile(r"^-?[0-9]+/[0-9]+$")
QUANTITY = re.compile(r"^\s*(-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)\s*(.*)$")
OPTICS_CONVENTION = "phy.optics_sign_convention"
TIMES = "\u00d7"  # the multiplication sign before a magnification


class ResolutionError(Exception):
    """A resolution tool gave no traceable result."""


class Session:
    """One resolution: the tool calls it made, their check ids and the labels it formatted."""

    def __init__(self, call: Call, conventions: Mapping[str, str]) -> None:
        self._call = call
        self.conventions = conventions
        self.check_ids: list[str] = []
        self.labels: dict[str, str] = {}

    def run(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self._call(tool, args)
        check = result.get("check") or {}
        if check.get("function") != tool or not str(check.get("check_id", "")).startswith("chk_"):
            raise ResolutionError(f"{tool} returned no check record")
        value = result.get("value")
        if not isinstance(value, dict) or value.get("status") == "unknown":
            raise ResolutionError(f"{tool} could not decide ({(value or {}).get('reason', 'no value')})")
        if check["check_id"] not in self.check_ids:
            self.check_ids.append(str(check["check_id"]))
        return value

    def done(self, computed: Computed) -> Computed:
        out = {**computed, "check_ids": list(self.check_ids)}
        if self.labels:
            out["labels"] = dict(sorted(self.labels.items()))
        return out


def shown(value: Any, sf: int = 3) -> str:
    """How a tool's figure is displayed: its rounding when it states one, its exact text when that is a plain number or
    a simple fraction, else its decimal to `sf` significant figures."""
    if isinstance(value, dict):
        for key in ("rounded", "shown"):
            if isinstance(value.get(key), str):
                return str(value[key])
        if isinstance(value.get("exact"), dict):  # math_evaluate: {status, exact: {text, decimal}}
            return shown(value["exact"], sf)
        text = str(value.get("text", ""))
        if PLAIN.match(text) or FRACTION.match(text):
            return text
        return significant(str(value.get("decimal", text)), sf)
    return str(value)


def significant(decimal: str, sf: int = 3) -> str:
    number = float(decimal)
    if number == 0:
        return "0"
    text = f"{number:.{sf}g}"
    if "e" in text:
        text = f"{number:.{max(sf - 1, 0)}e}"
    return text


def magnitude(text: str) -> tuple[str, str]:
    """A quantity string split into its number and unit ("49 N" -> ("49", "N"))."""
    match = QUANTITY.match(str(text))
    if not match:
        raise ResolutionError(f"not a quantity: {text!r}")
    return match.group(1), match.group(2).strip()


# --- graphs ------------------------------------------------------------------------------------------------------


def _feature_label(x: dict[str, Any], y: dict[str, Any]) -> str:
    return f"({shown(x)}, {shown(y)})"


def function_graph(spec: dict[str, Any], s: Session) -> Computed:
    params = spec["params"]
    marks = [
        m
        for m in params.get("marks", [])
        if m in ("roots", "turning_points", "y_intercept", "asymptotes", "intersections")
    ]
    curves = [
        {
            "id": c["id"],
            "expr": re.sub(r"^\s*y\s*=\s*", "", c["expr"]),
            **({"domain": c["domain"]} if "domain" in c else {}),
        }
        for c in params["curves"]
    ]
    args: dict[str, Any] = {"curves": curves, "x_range": params["x_range"], "max_points": 200}
    if marks:
        args["marks"] = marks
    value = s.run("math_plot_points", args)
    out_curves, features = {}, {}
    for curve in value["curves"]:
        cid = curve["id"]
        out_curves[cid] = curve["segments"]
        found = []
        for n, root in enumerate(curve.get("roots", []), 1):
            found.append(
                {
                    "id": f"root{n}",
                    "x": float(root["x"]["decimal"]),
                    "y": 0.0,
                    "label": _feature_label(root["x"], root["y"]),
                    "say": f"It crosses the x-axis at x = {shown(root['x'])}.",
                }
            )
        for n, tp in enumerate(curve.get("turning_points", []), 1):
            nature = tp.get("nature", "turning_point")
            fid = {"minimum": "min", "maximum": "max"}.get(nature, "tp") + ("" if n == 1 else str(n))
            found.append(
                {
                    "id": fid,
                    "x": float(tp["x"]["decimal"]),
                    "y": float(tp["y"]["decimal"]),
                    "label": _feature_label(tp["x"], tp["y"]),
                    "say": f"It has a {nature} at {_feature_label(tp['x'], tp['y'])}.",
                }
            )
        if curve.get("y_intercept"):
            yi = curve["y_intercept"]
            found.append(
                {
                    "id": "yint",
                    "x": 0.0,
                    "y": float(yi["y"]["decimal"]),
                    "label": _feature_label(yi["x"], yi["y"]),
                    "say": f"It meets the y-axis at y = {shown(yi['y'])}.",
                }
            )
        for f in found:
            s.labels[f"{cid}.{f['id']}"] = str(f["label"])
        if found:
            features[cid] = found
    out: Computed = {"curves": out_curves}
    if features:
        out["features"] = features
    return s.done(out)


def motion_graph(spec: dict[str, Any], s: Session) -> Computed:
    curve = spec["params"].get("curve")
    if not curve:
        return {}
    value = s.run(
        "math_plot_points",
        {"curves": [{"id": "curve", "expr": curve["expr"]}], "x_range": curve["domain"], "max_points": 100},
    )
    return s.done({"curve": value["curves"][0]["segments"]})


def titration_curve(spec: dict[str, Any], s: Session) -> Computed:
    """The kind's params as `chem_acid_base` arguments; points, equivalence and start pH from its one result."""
    params = spec["params"]
    analyte, titrant = params["analyte"], params["titrant"]
    if analyte["kind"] == titrant["kind"]:
        raise ResolutionError("a titration needs an acid and a base")
    args: dict[str, Any] = {
        "task": "titration_curve",
        "analyte": analyte["kind"],
        f"{analyte['kind']}_volume": params["analyte_volume"],
    }
    for solution in (analyte, titrant):
        kind = solution["kind"]
        args[kind] = solution["formula"]
        args[f"{kind}_concentration"] = solution["concentration"]
        args[f"{kind}_strength"] = solution["strength"]
        if "basicity" in solution:
            args["basicity" if kind == "acid" else "acidity"] = solution["basicity"]
    if "volume_max" in params:
        args["volume_max"] = params["volume_max"]
    value = s.run("chem_acid_base", args)
    s.labels["equivalence"] = shown(value["equivalence"]["ph"])
    s.labels["equivalence_volume"] = (
        f"{shown(value['equivalence']['volume'])} {value['equivalence']['volume'].get('unit', '')}".strip()
    )
    s.labels["start_ph"] = shown(value["start_ph"])
    half = value.get("half_equivalence")
    if half:
        s.labels["half_equivalence"] = shown(half["ph"])
    return s.done(
        {
            **({"half_equivalence": half} if half else {}),
            "points": value["points"],
            "equivalence": value["equivalence"],
            "equivalence_index": value["equivalence_index"],
            "start_ph": value["start_ph"],
            "axes": value["axes"],
        }
    )


def break_even_chart(spec: dict[str, Any], s: Session) -> Computed:
    params = spec["params"]
    args = {
        k: params[k]
        for k in ("fixed_costs", "variable_cost_per_unit", "selling_price", "output_units", "chart_max_units")
        if k in params
    }
    value = s.run("acc_break_even", args)
    chart = value["chart"]
    s.labels["fixed_cost"] = chart["fixed_cost"][0][1]
    s.labels["break_even_units"] = shown(value["break_even_units"]["exact"]["exact"])
    s.labels["break_even_revenue"] = value["break_even_revenue"]["value"]
    s.labels["break_even"] = f"{s.labels['break_even_units']}, {s.labels['break_even_revenue']}"
    if value.get("margin_of_safety"):
        s.labels["margin_units"] = shown(value["margin_of_safety"]["units"]["exact"])
        s.labels["margin_of_safety"] = s.labels["margin_units"]
    lines = {k: [[float(x), float(y)] for x, y in chart[k]] for k in ("fixed_cost", "total_cost", "revenue")}
    bep = chart["break_even_point"]
    return s.done(
        {
            "lines": lines,
            "max_units": chart["max_units"],
            "break_even": [float(bep[0]["value"]), float(bep[1]["value"])],
        }
    )


def _plain(value: float) -> str:
    """A request's number as plain decimal text for a tool expression (0.0001, never 1e-04)."""
    return format(Decimal(repr(value)) if isinstance(value, float) else Decimal(value), "f")


def _x(expression: str, at: str) -> str:
    """The expression with x replaced by `at` (bracketed); `exp` and `max` keep their x."""
    return re.sub(r"(?<![a-z])x(?![a-z])", f"({at})", expression)


# --- A level graphs (VSL-07, VSL-46) -------------------------------------------------------------------------------

# exponential_curve: c0 e^(-kt) or c0 (1 - e^(-kt)), k > 0 per time unit; the half-life is ln 2 / k and the time
# constant 1 / k, in the request's time unit.
EXPONENTIAL_MARKS = {"half_life": "ln(2)/({k})", "time_constant": "1/({k})"}


def exponential_curve(spec: dict[str, Any], s: Session) -> Computed:
    """The curve from math_plot_points and the marked half-life or time constant (with the value there) from
    math_evaluate; a mark outside the time range is refused, never drawn off the axes."""
    params = spec["params"]
    t0, t1 = params["time_range"]
    if not t0 < t1:
        raise ResolutionError("the time range runs the wrong way")
    if not params["initial"] > 0:
        raise ResolutionError("an initial value of zero draws no curve")
    c0, k = _plain(params["initial"]), _plain(params["constant"])
    expression = f"{c0}*exp(-{k}*x)" if params["mode"] == "decay" else f"{c0}*(1-exp(-{k}*x))"
    value = s.run(
        "math_plot_points",
        {"curves": [{"id": "curve", "expr": expression}], "x_range": [t0, t1], "max_points": 121, "marks": []},
    )
    out: Computed = {"curve": [p for segment in value["curves"][0]["segments"] for p in segment]}
    mark = params["mark"]
    if mark in EXPONENTIAL_MARKS:
        at = EXPONENTIAL_MARKS[mark].format(k=k)
        time = s.run("math_evaluate", {"expression": at, "sf": 3})
        where = float(time["exact"]["decimal"])
        if not t0 <= where <= t1:
            raise ResolutionError(
                f"the {mark.replace('_', ' ')} {shown(time)} {params['time_unit']} lies outside the time range"
            )
        there = s.run("math_evaluate", {"expression": _x(expression, at), "sf": 3})
        s.labels[mark] = shown(time)
        s.labels[f"{mark}.value"] = shown(there)
        out["mark"] = {"t": where, "value": float(there["exact"]["decimal"])}
    return s.done(out)


# distribution_curve: a normal curve sampled over the mean ± 4 sd (and out to the interval), its probability the
# integral of the density; a binomial or Poisson distribution as bars over the mean ± 4 sd, each P(X = k) and the
# probability from math_evaluate. Discrete intervals are whole outcomes, with both ends included.
NORMAL_SPAN = 4
NORMAL_TAIL = 40  # an open tail is integrated out to 40 sd: what lies beyond is below 1e-340, far under 4 sf
DISCRETE_BARS = 41  # the most outcomes one chart draws
DISCRETE_TERMS = 40  # the most terms one probability sums (within the tool's expression length and nesting limits)
PROBABILITY_SF = 4


def _bounds(params: dict[str, Any]) -> tuple[float | None, float | None]:
    """The event's lower and upper bounds: between [a, b]; less_than X <= b; greater_than X >= a."""
    a, b = params["interval"]
    if a > b:
        raise ResolutionError("the interval runs the wrong way")
    tail = params["tail"]
    return (None if tail == "less_than" else a), (None if tail == "greater_than" else b)


def _event(lo: float | None, hi: float | None) -> str:
    if lo is not None and hi is not None:
        return f"P({lo:g} ≤ X ≤ {hi:g})"
    return f"P(X ≤ {hi:g})" if hi is not None else f"P(X ≥ {lo:g})"


def _normal(params: dict[str, Any], lo: float | None, hi: float | None, s: Session) -> Computed:
    mean, sd = Decimal(_plain(params["parameters"]["mean"])), Decimal(_plain(params["parameters"]["sd"]))
    pdf = f"exp(-(x-({mean}))^2/(2*({sd})^2))/(({sd})*sqrt(2*pi))"
    ends = [Decimal(_plain(v)) for v in (lo, hi) if v is not None]
    w0, w1 = min([mean - NORMAL_SPAN * sd, *ends]), max([mean + NORMAL_SPAN * sd, *ends])
    plot = s.run(
        "math_plot_points",
        {"curves": [{"id": "pdf", "expr": pdf}], "x_range": [str(w0), str(w1)], "max_points": 161, "marks": []},
    )
    low = ends[0] if lo is not None else mean - NORMAL_TAIL * sd
    high = ends[-1] if hi is not None else mean + NORMAL_TAIL * sd
    area = s.run("math_integrate", {"expression": pdf, "variable": "x", "lower": str(low), "upper": str(high)})
    s.labels["shade"] = shown(area["definite"], PROBABILITY_SF)
    return {
        "points": [p for segment in plot["curves"][0]["segments"] for p in segment],
        "window": [float(w0), float(w1)],
        "shade": [float(max(low, w0)), float(min(high, w1))],
    }


def _term(params: dict[str, Any], k: int) -> str:
    """P(X = k) as a tool expression: nCk p^k (1 - p)^(n - k), or e^(-λ) λ^k / k!."""
    if params["distribution"] == "binomial":
        n, p = params["parameters"]["n"], _plain(params["parameters"]["p"])
        choose = "*".join(f"{n - k + i}/{i}" for i in range(1, k + 1)) or "1"
        return f"{choose}*({p})^{k}*(1-({p}))^{n - k}"
    lam = _plain(params["parameters"]["lambda"])
    factorial = "*".join(str(i) for i in range(1, k + 1)) or "1"
    return f"exp(-({lam}))*({lam})^{k}/({factorial})"


def _sum(params: dict[str, Any], first: int, last: int) -> str:
    """P(first <= X <= last), nested so each term is the one before times its ratio: (n - k + 1) p / (k (1 - p)), or
    λ / k."""
    inner = "1"
    for k in range(last, first, -1):
        if params["distribution"] == "binomial":
            n, p = params["parameters"]["n"], _plain(params["parameters"]["p"])
            ratio = f"{n - k + 1}/{k}*({p})/(1-({p}))"
        else:
            ratio = f"({_plain(params['parameters']['lambda'])})/{k}"
        inner = f"1+{ratio}*({inner})"
    return f"{_term(params, first)}*({inner})"


def _discrete(params: dict[str, Any], lo: float | None, hi: float | None, s: Session) -> Computed:
    binomial = params["distribution"] == "binomial"
    pars = params["parameters"]
    if binomial and not 0 < pars["p"] < 1:
        raise ResolutionError("a binomial distribution with p = 0 or 1 has one outcome; there is nothing to shade")
    top = pars["n"] if binomial else None
    for end in (lo, hi):
        if end is None:
            continue
        if not float(end).is_integer():
            raise ResolutionError(f"{end:g} is not a whole number of outcomes")
        if end < 0 or (top is not None and end > top):
            raise ResolutionError(f"{end:g} is not an outcome of the distribution")
    first, last = (int(lo) if lo is not None else None), (int(hi) if hi is not None else None)
    # The bars drawn: the mean ± 4 sd (presentation), widened to take in the interval.
    mean = pars["n"] * pars["p"] if binomial else pars["lambda"]
    sd = math.sqrt(pars["n"] * pars["p"] * (1 - pars["p"]) if binomial else pars["lambda"])
    k0, k1 = max(0, math.floor(mean - NORMAL_SPAN * sd)), math.ceil(mean + NORMAL_SPAN * sd)
    k1 = min(k1, top) if top is not None else max(k1, 1)
    k0, k1 = (
        min([k0, *(e for e in (first, last) if e is not None)]),
        max([k1, *(e for e in (first, last) if e is not None)]),
    )
    if k1 - k0 + 1 > DISCRETE_BARS:
        raise ResolutionError(f"the distribution has {k1 - k0 + 1} outcomes to draw, more than {DISCRETE_BARS}")
    # The probability: the event's terms, or one minus the rest where that sums fewer.
    lowest = first if first is not None else 0
    options: list[tuple[int, str]] = []
    if last is not None or top is not None:
        highest = last if last is not None else int(top or 0)
        options.append((highest - lowest + 1, _sum(params, lowest, highest)))
    if first is None and last is not None and top is not None:
        options.append((top - last, f"1-({_sum(params, last + 1, top)})" if last < top else "1"))
    if last is None:
        options.append((lowest, f"1-({_sum(params, 0, lowest - 1)})" if lowest > 0 else "1"))
    count, expression = min(options, key=lambda o: o[0])
    if count > DISCRETE_TERMS:
        raise ResolutionError(f"the probability sums {count} outcomes, more than {DISCRETE_TERMS}")
    s.labels["shade"] = shown(s.run("math_evaluate", {"expression": expression, "sf": PROBABILITY_SF}))
    # Each bar's height, P(X = k), exactly as one fraction: math_simplify skips math_evaluate's search for a closed
    # form, which takes a large share of a second for each of up to 41 bars.
    bars = []
    for k in range(k0, k1 + 1):
        height = s.run("math_simplify", {"expression": _term(params, k), "form": "single_fraction"})
        bars.append([k, float(height["result"]["decimal"])])
    shaded = [k for k in range(k0, k1 + 1) if (first is None or k >= first) and (last is None or k <= last)]
    return {"bars": bars, "shade": [shaded[0], shaded[-1]] if shaded else []}


def distribution_curve(spec: dict[str, Any], s: Session) -> Computed:
    """The shaded probability of the interval and the curve or bars it is shaded on (VSL-46: parameters of their own
    distribution; discrete intervals whole outcomes)."""
    params = spec["params"]
    lo, hi = _bounds(params)
    drawn = _normal(params, lo, hi, s) if params["distribution"] == "normal" else _discrete(params, lo, hi, s)
    return s.done({**drawn, "event": _event(lo, hi)})


def data_plot(spec: dict[str, Any], s: Session) -> Computed:
    """A linear fit by least squares from stats_compute: the means and the variances of x, y and x + y, so
    cov(x, y) = (var(x + y) - var x - var y) / 2 and the gradient cov / var x, with math_evaluate for the gradient, the
    intercept and the triangle across the data's x range. Points are unique; a constant x refuses a fit (VSL-46)."""
    params = spec["params"]
    points = params["points"]
    ids = [data_point_part(p["id"]) for p in points]
    if len(set(ids)) != len(ids):
        raise ResolutionError("point ids repeat")
    if len({(p["x"], p["y"]) for p in points}) != len(points):
        raise ResolutionError("two points are the same")
    if params["fit"] != "linear":
        return {}
    xs, ys = [_plain(p["x"]) for p in points], [_plain(p["y"]) for p in points]
    both = [str(Decimal(x) + Decimal(y)) for x, y in zip(xs, ys, strict=True)]
    sx, sy, sxy = (s.run("stats_compute", {"data": data}) for data in (xs, ys, both))
    if Decimal(sx["variance"]["decimal"]) == 0:
        raise ResolutionError("every point has the same x, so no line can be fitted")
    vx, vy, vxy = (f"({v['variance']['text']})" for v in (sx, sy, sxy))
    gradient = s.run("math_evaluate", {"expression": f"({vxy}-{vx}-{vy})/(2*{vx})", "sf": 3})
    m = f"({gradient['exact']['text']})"
    intercept = s.run("math_evaluate", {"expression": f"({sy['mean']['text']})-{m}*({sx['mean']['text']})", "sf": 3})
    c = f"({intercept['exact']['text']})"
    x_lo, x_hi = sx["min"]["text"], sx["max"]["text"]
    y_lo = s.run("math_evaluate", {"expression": f"{c}+{m}*({x_lo})"})
    y_hi = s.run("math_evaluate", {"expression": f"{c}+{m}*({x_hi})"})
    dx = s.run("math_evaluate", {"expression": f"({x_hi})-({x_lo})", "sf": 3})
    dy = s.run("math_evaluate", {"expression": f"({y_hi['exact']['text']})-({y_lo['exact']['text']})", "sf": 3})
    s.labels.update(
        {
            "fit.gradient": shown(gradient),
            "fit.intercept": shown(intercept),
            "gradient.dx": shown(dx),
            "gradient.dy": shown(dy),
        }
    )
    ends = [
        [float(sx["min"]["decimal"]), float(y_lo["exact"]["decimal"])],
        [float(sx["max"]["decimal"]), float(y_hi["exact"]["decimal"])],
    ]
    out = {"line": ends, "intercept": float(intercept["exact"]["decimal"])}
    return s.done(out)


# --- geometry ----------------------------------------------------------------------------------------------------


def geometry_figure(spec: dict[str, Any], s: Session) -> Computed:
    params = spec["params"]
    figure = params["figure"]
    givens = dict(params.get("givens") or {})
    if figure == "parallel_lines":
        return {}
    if figure == "triangle":
        if params.get("right_angle"):
            givens["right_angle"] = params["right_angle"]
        value = s.run("geom_figure", {"template": "triangle", "givens": givens})
        solution = value["solutions"][0]
        for side in "abc":
            s.labels[f"side_{side}"] = shown(solution["sides"][side])
        for angle in "ABC":
            s.labels[f"angle_{angle}"] = shown(solution["angles"][angle])
        return s.done({"figure": solution["figure"], "ambiguous": value["ambiguous"]})
    if figure == "circle":
        value = s.run("geom_figure", {"template": "circle", "givens": givens})
        s.labels["radius"] = shown(value["radius"])
        for key in ("chord", "arc"):
            if key in value:
                s.labels[key] = shown(value[key])
        if "angle" in value:
            s.labels["sector"] = shown(value["angle"])
        return s.done({"figure": value.get("figure", {})})
    value = s.run("geom_figure", {"template": "circle_theorem", "givens": {"theorem": params["theorem"], **givens}})
    s.labels["angle_given"] = shown(value["given"])
    s.labels["angle_derived"] = shown(value["derived"])
    return s.done(
        {
            "meaning": value["meaning"],
            "given": float(value["given"]["decimal"]),
            "derived": float(value["derived"]["decimal"]),
        }
    )


def bearing_diagram(spec: dict[str, Any], s: Session) -> Computed:
    params = spec["params"]
    distance, unit = magnitude(params["distance"])
    value = s.run(
        "geom_figure", {"template": "bearing", "givens": {"bearing": params["bearing"], "distance": distance}}
    )
    s.labels["bearing"] = value["bearing"]["three_figure"]
    s.labels["back_bearing"] = value["back_bearing"]["three_figure"]
    s.labels["east"] = f"{shown(value['east'])} {unit}".strip()
    s.labels["north"] = f"{shown(value['north'])} {unit}".strip()
    return s.done({"figure": value["figure"]})


def transformation_grid(spec: dict[str, Any], s: Session) -> Computed:
    params = spec["params"]
    points = [[p["x"], p["y"]] for p in params["object"]]
    value = s.run(
        "geom_figure",
        {"template": "transformation", "givens": {"points": points, "transformation": params["transformation"]}},
    )
    return s.done({"image": value["figure"]["image"]})


def unit_circle(spec: dict[str, Any], s: Session) -> Computed:
    angle = spec["params"]["angle_deg"]
    show = spec["params"].get("show") or ["sin", "cos"]
    values = {}
    for ratio in ("sin", "cos", "tan"):
        if ratio not in show and ratio != "tan":
            continue
        if ratio == "tan" and ("tan" not in show or (float(angle) - 90) % 180 == 0):
            continue
        value = s.run("math_evaluate", {"expression": f"{ratio}({angle})", "angle_unit": "degrees", "sf": 3})
        s.labels[f"leg_{ratio}"] = shown(value)
        values[ratio] = float(value["exact"]["decimal"])
    return s.done({"values": values})


def probability_tree(spec: dict[str, Any], s: Session) -> Computed:
    branches = spec["params"]["branches"]
    leaves = {}
    chain: dict[tuple[str, ...], str] = {}
    for path, branch in tree_paths(branches):
        chain[path] = f"({branch['p']})" if len(path) == 1 else f"{chain[path[:-1]]}*({branch['p']})"
        if not branch.get("then"):
            value = s.run("math_evaluate", {"expression": chain[path]})
            leaves["_".join(path)] = shown(value)
            s.labels["o_" + "_".join(path)] = shown(value)
    return s.done({"outcomes": leaves})


def motion_diagram(spec: dict[str, Any], s: Session) -> Computed:
    params = spec["params"]
    u, _ = magnitude(params["initial_velocity"])
    a, _ = magnitude(params["acceleration"])
    dt, _ = magnitude(params["interval"])
    positions, velocities = [], []
    for i in range(int(params["marks"])):
        t = f"{i}*({dt})"
        position = s.run("math_evaluate", {"expression": f"({u})*{t} + ({a})*({t})^2/2"})
        velocity = s.run("math_evaluate", {"expression": f"({u}) + ({a})*{t}"})
        positions.append(float(position["exact"]["decimal"]))
        velocities.append(float(velocity["exact"]["decimal"]))
    return s.done({"positions": positions, "velocities": velocities})


# --- physics -----------------------------------------------------------------------------------------------------


def _acute(delta: float) -> float:
    """The angle between a line and a direction, folded into 0 to 90 degrees (presentation arithmetic on the given
    angle; the component magnitudes themselves come from the tool)."""
    delta %= 180
    return delta if delta <= 90 else 180 - delta


def free_body(spec: dict[str, Any], s: Session) -> Computed:
    """Components of forces asked for by an `add: components_of` step, each magnitude computed by math_evaluate."""
    params = spec["params"]
    forces = {f["id"]: f for f in params["forces"]}
    surface = params.get("surface") or {}
    incline = surface_kind(params) == "incline"
    theta = float(surface.get("incline_deg", 0) or 0) if incline else 0.0
    first, second = ("parallel", "perpendicular") if incline else ("x", "y")
    components = {}
    for step in spec.get("steps", []):
        for add in step.get("add", []):
            if add.get("element") != "components_of" or add.get("of") not in forces:
                continue
            force = forces[add["of"]]
            amount, unit = magnitude(force["magnitude"])
            along = _acute(float(force["direction_deg"]) - theta)
            for part, trig in ((first, "cos"), (second, "sin")):
                value = s.run(
                    "math_evaluate", {"expression": f"{amount}*{trig}({along:g})", "angle_unit": "degrees", "sf": 3}
                )
                label = f"{shown(value)} {unit}".strip()
                s.labels[f"{force['id']}.{part}"] = label
                components[f"{force['id']}.{part}"] = label
    return s.done({"components": components}) if components else {}


TOOL_KINDS = {
    "cell": "cell",
    "battery": "battery",
    "resistor": "resistor",
    "lamp": "lamp",
    "ammeter": "ammeter",
    "voltmeter": "voltmeter",
    "switch": "switch",
}


def circuit(spec: dict[str, Any], s: Session) -> Computed:
    params = spec["params"]
    components = []
    for c in params["components"]:
        if c["type"] not in TOOL_KINDS:
            return {}  # phys_circuit cannot solve it: the circuit is drawn with no computed readings
        entry: dict[str, Any] = {"id": c["id"], "kind": TOOL_KINDS[c["type"]]}
        if c.get("value") is not None:
            number, _ = magnitude(c["value"])
            entry["emf" if c["type"] in ("cell", "battery") else "resistance"] = number
        if c["type"] == "switch":
            entry["state"] = c.get("state", "closed")
        components.append(entry)
    if any(c["to"].endswith(".wiper") or c["from"].endswith(".wiper") for c in params["connections"]):
        return {}
    if any(c["kind"] in ("cell", "battery") and "emf" not in c for c in components):
        return {}  # no source value: a drawing of the circuit with nothing to compute
    value = s.run("phys_circuit", {"components": components, "connections": params["connections"]})
    units = value["units"]
    readings = {}
    wanted = params.get("show") or ["meter_readings"]
    meters = any(k in wanted for k in ("meter_readings", "currents", "voltages"))
    for c in params["components"] if meters else []:
        result = value["components"].get(c["id"], {})
        if c["type"] == "ammeter" and "current" in result:
            readings[f"{c['id']}.reading"] = f"{shown(result['current'])} {units['current']}"
        if c["type"] == "voltmeter" and "pd" in result:
            readings[f"{c['id']}.reading"] = f"{shown(result['pd'])} {units['pd']}"
    s.labels.update(readings)
    if "equivalent_resistance" in (params.get("show") or []) and value.get("equivalent_resistance"):
        s.labels["equivalent_resistance"] = f"{shown(value['equivalent_resistance'])} {units['resistance']}"
    currents = {cid: float(r["current"]["decimal"]) for cid, r in value["components"].items() if "current" in r}
    return s.done({"currents": currents, "open_circuit": value.get("open_circuit", False)})


def ray_diagram(spec: dict[str, Any], s: Session) -> Computed:
    params = spec["params"]
    convention = s.conventions.get(OPTICS_CONVENTION)
    if convention is None:
        raise ResolutionError("the pack states no optics sign convention")
    args: dict[str, Any] = {
        "task": "image",
        "optic": params["optic"],
        "sign_convention": convention,
        "sign_convention_source": "pack",
    }
    for key in ("focal_length", "object_distance", "object_height"):
        if params.get(key) is not None:
            args[key], unit = magnitude(params[key])
            if unit and key == "object_distance":
                args["distance_unit"] = unit
    value = s.run("phys_optics", args)
    unit = magnitude(params["object_distance"])[1]
    wanted = params.get("rays") or ["parallel", "focal", "centre"]
    rays = {r["ray"]: r["path"] for r in value.get("rays", []) if r["ray"] in wanted}
    frame = value.get("frame", {})
    nature = value.get("nature") or {}
    if value.get("image") == "formed" and value.get("image_distance"):
        s.labels["image_distance"] = f"{shown(value['image_distance']).lstrip('-')} {unit}".strip()
        s.labels["magnification"] = shown(value["magnification"]).lstrip("-")
    return s.done(
        {
            "rays": [r for r in ("parallel", "focal", "centre") if r in rays],
            "paths": rays,
            "object_top": frame.get("object_top"),
            "image_top": frame.get("image_top") if value.get("image") == "formed" else None,
            "focal_points": frame.get("focal_points", []),
            "nature": nature,
            "nature_text": " ".join(str(nature.get(k, "")) for k in ("type", "orientation", "size")).strip(),
        }
    )


def projectile_sim(spec: dict[str, Any], s: Session) -> Computed:
    """The readouts at the starting values, from phys_projectile; the client model takes over as the sliders move."""
    params = spec["params"]
    g = s.conventions.get("phy.g")
    if g is None:
        raise ResolutionError("the pack states no value of g")
    args: dict[str, Any] = {
        "motion": params["mode"],
        "speed": params["speed"],
        "g": g,
        "g_source": "pack",
        "points": 41,
    }
    if params["mode"] == "vertical":
        args["direction"] = "up"
    if params["mode"] == "angled":
        args["angle"] = params["angle"]  # degrees above the horizontal
    if params.get("height") is not None:
        args["height"] = params["height"]
    value = s.run("phys_projectile", args)
    readouts = {
        "out.time": shown(value["time_of_flight"]),
        "out.range": shown(value["range"]),
        "out.max_height": shown(value["max_height"]),
    }
    s.labels.update(readouts)
    controls = ("mode", "speed", "angle", "height") if params["mode"] == "angled" else ("mode", "speed", "height")
    parts = [{"id": f"ctrl.{p}", "role": "control", "label": p} for p in controls]
    parts += [{"id": k, "role": "readout", "label": k.split(".")[1].replace("_", " ")} for k in readouts]
    alt = {
        "mode": f"angled ({params['angle']} degrees)" if params["mode"] == "angled" else params["mode"],
        "speed": params["speed"],
        "height": params.get("height", 0),
        "range": readouts["out.range"],
        "time": readouts["out.time"],
    }
    return s.done({"g": float(g), "path": [[p["x"], p["y"]] for p in value["points"]], "parts": parts, "alt": alt})


def circuit_sim(spec: dict[str, Any], s: Session) -> Computed:
    params = spec["params"]
    resistors = params["resistors"]
    components = [
        {"id": "B", "kind": "battery", "emf": params["emf"]},
        {"id": "A1", "kind": "ammeter"},
        {"id": "S", "kind": "switch", "state": "closed" if params.get("switch_closed", True) else "open"},
    ]
    components += [{"id": r["id"], "kind": "resistor", "resistance": r["resistance"]} for r in resistors]
    group = [r["id"] for r in resistors]
    network = {
        "series": [
            "B",
            "A1",
            "S",
            {"parallel": group} if params["arrangement"] == "parallel" and len(group) > 1 else {"series": group},
        ]
    }
    value = s.run("phys_circuit", {"components": components, "network": network})
    units = value["units"]
    readouts = {
        "out.current": f"{shown(value['components']['A1']['current'])}",
        "out.resistance": shown(value["equivalent_resistance"]) if value.get("equivalent_resistance") else "∞",
    }
    for r in resistors:
        readouts[f"out.pd_{r['id']}"] = shown(value["components"][r["id"]]["pd"])
    s.labels.update(readouts)
    parts = [
        {"id": "ctrl.emf", "role": "control", "label": "emf"},
        {"id": "ctrl.arrangement", "role": "control", "label": "arrangement"},
        {"id": "ctrl.switch", "role": "control", "label": "switch"},
    ]
    parts += [{"id": f"ctrl.{r['id']}", "role": "control", "label": r["id"]} for r in resistors]
    parts += [{"id": k, "role": "readout", "label": k.split(".")[1]} for k in readouts]
    alt = {
        "emf": params["emf"],
        "count": len(resistors),
        "arrangement": params["arrangement"],
        "current": readouts["out.current"],
    }
    return s.done({"units": units, "parts": parts, "alt": alt})


def titration_sim(spec: dict[str, Any], s: Session) -> Computed:
    params = spec["params"]
    acid, base = params["acid"], params["base"]
    volume = params["acid_volume"]
    end = volume * acid["concentration"] / base["concentration"]
    at = [0, end / 2, end, end * 1.5]
    args = {
        "task": "titration_curve",
        "analyte": "acid",
        "acid": acid["formula"],
        "acid_strength": "strong",
        "acid_concentration": f"{acid['concentration']} mol dm^-3",
        "acid_volume": f"{volume} cm^3",
        "base": base["formula"],
        "base_strength": "strong",
        "base_concentration": f"{base['concentration']} mol dm^-3",
        "at_volumes": [f"{v:g} cm^3" for v in at],
        "volume_max": f"{2 * end:g} cm^3",
    }
    value = s.run("chem_acid_base", args)
    readouts = {
        "out.ph": shown(value["start_ph"]),
        "out.volume": "0",
        "out.end_point": shown(value["equivalence"]["volume"]),
    }
    s.labels.update(readouts)
    parts = [
        {"id": "ctrl.volume", "role": "control", "label": "volume added"},
        {"id": "ctrl.tap", "role": "control", "label": "tap"},
        {"id": "ctrl.indicator", "role": "control", "label": "indicator"},
    ]
    parts += [
        {"id": k, "role": "readout", "label": k.split(".")[1].replace("_", " ")}
        for k in ("out.volume", "out.ph", "out.end_point")
    ]
    alt = {"acid": acid["formula"], "base": base["formula"], "volume": volume, "end": readouts["out.end_point"]}
    samples = [{"volume": float(a["volume"]["decimal"]), "ph": float(a["ph"]["decimal"])} for a in value.get("at", [])]
    return s.done({"points": value["points"], "samples": samples, "parts": parts, "alt": alt})


# The molar gas constant of the simplified S3 simulation models (J/(mol K)); the client models use the same figure.
GAS_CONSTANT = "8.314"
KELVIN_OFFSET = "phy.kelvin_offset"
# The client models' bounded slider ranges (VSL-30): a starting value outside them is refused, never clamped.
GAS_TEMPERATURE_K = (100, 600)
GAS_VOLUME_CM3 = (20, 500)


def _sim_preset(kind: str, preset_id: str) -> dict[str, Any]:
    preset: dict[str, Any] | None = SIM_PRESETS.get(preset_id)
    if preset is None or preset["kind"] != kind:
        ours = ", ".join(sorted(i for i, p in SIM_PRESETS.items() if p["kind"] == kind))
        raise ResolutionError(f"no {kind} preset {preset_id} (the presets are {ours})")
    return preset


def _within(name: str, value: float, bounds: tuple[float, float]) -> None:
    if not bounds[0] <= value <= bounds[1]:
        raise ResolutionError(f"{name} {value:g} is outside the simulation's range {bounds[0]:g} to {bounds[1]:g}")


def gas_laws_sim(spec: dict[str, Any], s: Session) -> Computed:
    """The starting pressure from math_evaluate (p = nRT/V); the client model takes over as the slider moves."""
    params = spec["params"]
    preset = _sim_preset("gas_laws_sim", params["preset_id"])
    if params["control"] not in preset["controls"]:
        raise ResolutionError(f"{preset['id']} holds the {params['control']} fixed; it cannot be the control")
    volume_cm3 = params["volume_m3"] * 1e6
    _within("temperature", params["temperature_k"], GAS_TEMPERATURE_K)
    _within("volume", volume_cm3, GAS_VOLUME_CM3)
    expression = f"{params['amount_mol']}*{GAS_CONSTANT}*{params['temperature_k']}/({params['volume_m3']})/1000"
    value = s.run("math_evaluate", {"expression": expression, "sf": 3})
    readouts = {"out.pressure": shown(value), "out.temperature": f"{params['temperature_k']:g}"}
    out: Computed = {"container": preset["container"]}
    offset = s.conventions.get(KELVIN_OFFSET)
    if offset is not None:
        out["kelvin_offset"] = float(offset)
        readouts["out.celsius"] = f"{params['temperature_k'] - float(offset):g}"
    s.labels.update(readouts)
    parts = [{"id": f"ctrl.{c}", "role": "control", "label": c} for c in ("temperature", "volume")]
    parts += [{"id": k, "role": "readout", "label": k.split(".")[1]} for k in readouts]
    alt = {
        "container": preset["container"],
        "control": params["control"],
        "temperature": readouts["out.temperature"],
        "volume": f"{volume_cm3:g}",
        "pressure": readouts["out.pressure"],
    }
    return s.done({**out, "parts": parts, "alt": alt})


# The client's deterministic parser (packages/renderers/src/expr.ts): numbers, x, + - * / ^, brackets and these names.
FUNCTION_NAMES = frozenset({"sin", "cos", "tan", "asin", "acos", "atan", "sqrt", "abs", "exp", "ln", "log", "pi", "e"})
EXPRESSION = re.compile(r"^[0-9x.+\-*/^() a-z]+$")


def _transformed(expression: str, p: dict[str, float]) -> str:
    inner = f"(({p['b']})*(x+({p['c']})))"
    return f"({p['a']})*({re.sub(r'(?<![a-z])x(?![a-z])', inner, expression)})+({p['d']})"


def function_transform_sim(spec: dict[str, Any], s: Session) -> Computed:
    """y = a f(b(x + c)) + d: the starting curve from math_plot_points and the tracked point, where the parent graph's
    point at x0 goes, from math_evaluate. Slider ranges, steps and values are checked; an expression the client's
    parser cannot read fails closed."""
    params = spec["params"]
    expression = params["expression"].replace("**", "^")
    names = set(re.findall(r"[a-z]+", expression)) - {"x"}
    if not EXPRESSION.match(expression) or not names <= FUNCTION_NAMES:
        raise ResolutionError(f"the expression {expression!r} is not one the simulation can draw")
    lo, hi = params["x_range"]
    if not lo < hi:
        raise ResolutionError("x_range runs the wrong way")
    values: dict[str, float] = {}
    for name in ("a", "b", "c", "d"):
        slider = params["parameters"][name]
        if not slider["min"] < slider["max"] or not slider["min"] <= slider["value"] <= slider["max"]:
            raise ResolutionError(f"the {name} slider's range or value is invalid")
        steps = (slider["value"] - slider["min"]) / slider["step"]
        if abs(steps - round(steps)) > 1e-9:
            raise ResolutionError(f"{name} = {slider['value']:g} is not on its slider's steps")
        values[name] = slider["value"]
    if values["b"] == 0:
        raise ResolutionError("b = 0 flattens the graph; the tracked point is undefined")
    # The tracked point: the parent's point at x0 = 0, or at x0 = 1 where f(0) is undefined (ln x, 1/x).
    parent: dict[str, Any] | None = None
    for x0 in (0, 1):
        try:
            parent = s.run("math_evaluate", {"expression": re.sub(r"(?<![a-z])x(?![a-z])", f"({x0})", expression)})
            break
        except Exception:  # undefined there (a domain error or `unknown`): the next point is tried
            parent = None
    if parent is None:
        raise ResolutionError("the parent graph has no point at x = 0 or x = 1 to track")
    curve = _transformed(expression, values)
    s.run("math_plot_points", {"curves": [{"id": "f", "expr": curve}], "x_range": [lo, hi], "max_points": 121})
    point_x = s.run("math_evaluate", {"expression": f"{x0}/({values['b']})-({values['c']})"})
    point_y = s.run("math_evaluate", {"expression": f"({values['a']})*({parent['exact']['text']})+({values['d']})"})
    readouts = {"out.point_x": shown(point_x), "out.point_y": shown(point_y)}
    s.labels.update(readouts)
    parts = [{"id": f"ctrl.{k}", "role": "control", "label": k} for k in ("a", "b", "c", "d")]
    parts += [{"id": k, "role": "readout", "label": k.split(".")[1].replace("_", " ")} for k in readouts]
    alt = {
        "expression": params["expression"],
        "a": f"{values['a']:g}",
        "b": f"{values['b']:g}",
        "c": f"{values['c']:g}",
        "d": f"{values['d']:g}",
        "x": readouts["out.point_x"],
        "y": readouts["out.point_y"],
    }
    return s.done({"x0": x0, "parts": parts, "alt": alt})


ELECTROLYSIS_CURRENT_A = (0.1, 5)
ELECTROLYSIS_DURATION_S = (1, 7200)
FORMULA_PART = re.compile(r"([A-Z][a-z]?)(\d*)")


def _molar_mass(formula: str, conventions: Mapping[str, str]) -> str:
    """A product's molar mass as an expression of the pack's relative atomic masses (chem.ar_<element>)."""
    terms = []
    for element, count in FORMULA_PART.findall(formula):
        ar = conventions.get(f"chem.ar_{element.lower()}")
        if ar is None:
            raise ResolutionError(f"the pack states no relative atomic mass for {element}")
        terms.append(f"{count or 1}*{ar}")
    return "(" + "+".join(terms) + ")"


def _half_equation(product: dict[str, Any], s: Session) -> None:
    """chem_balance checks the preset's half-equation is balanced in atoms and charge, with the stated electrons."""
    value = s.run("chem_balance", {"equation": product["half_equation"]})
    if not value.get("student", {}).get("balanced"):
        raise ResolutionError(f"{product['half_equation']} is not balanced")
    by_formula = {sp["formula"]: sp["coefficient"] for sp in value["species"]}
    if by_formula.get("e-", 0) != product["electrons"] * by_formula.get(product["formula"], 0):
        raise ResolutionError(
            f"{product['half_equation']} does not move {product['electrons']} electrons per {product['formula']}"
        )


def electrolysis_sim(spec: dict[str, Any], s: Session) -> Computed:
    """The cell from its preset (each half-equation checked by chem_balance), and the amounts at the end of the run from
    math_evaluate: Q = It, n = Q / (zF), a gas's volume from the pack's molar volume, a metal's mass from its Ar."""
    params = spec["params"]
    preset = _sim_preset("electrolysis_sim", params["preset_id"])
    for field in ("phase", "electrodes"):
        if params[field] != preset[field]:
            raise ResolutionError(f"{preset['id']} is {field} {preset[field]}, not {params[field]}")
    if params["electrolyte"].strip().lower() != preset["electrolyte"].lower():
        raise ResolutionError(f"{preset['id']} is {preset['electrolyte']}, not {params['electrolyte']}")
    _within("current", params["current_a"], ELECTROLYSIS_CURRENT_A)
    _within("duration", params["duration_s"], ELECTROLYSIS_DURATION_S)
    faraday, vm = s.conventions.get("chem.faraday"), s.conventions.get("chem.vm")
    if faraday is None or vm is None:
        raise ResolutionError("the pack states no Faraday constant or molar gas volume")
    current, duration = params["current_a"], params["duration_s"]
    molar_mass: dict[str, float] = {}
    readouts = {"out.charge": shown(s.run("math_evaluate", {"expression": f"{current}*{duration}", "sf": 3}))}
    for side in ("cathode", "anode"):
        product = preset[side]
        _half_equation(product, s)
        moles = f"{current}*{duration}/({product['electrons']}*{faraday})"
        if product["measure"] == "gas":
            gas = s.run("math_evaluate", {"expression": f"{moles}*{vm}*1000", "sf": 3})
            readouts[f"out.{side}_gas"], readouts[f"out.{side}_mass"] = shown(gas), "0"
            continue
        mass_expr = _molar_mass(product["formula"], s.conventions)
        molar_mass[product["formula"]] = float(s.run("math_evaluate", {"expression": mass_expr})["exact"]["decimal"])
        sign = "-" if product["measure"] == "dissolves" else ""
        mass = s.run("math_evaluate", {"expression": f"{sign}{moles}*{mass_expr}", "sf": 3})
        readouts[f"out.{side}_mass"], readouts[f"out.{side}_gas"] = shown(mass), "0"
    s.labels.update(readouts)
    constants = {"faraday": float(faraday), "vm_cm3": float(vm) * 1000, "molar_mass": molar_mass}
    parts = [
        {"id": "ctrl.current", "role": "control", "label": "current"},
        {"id": "ctrl.ions", "role": "control", "label": "ions"},
    ]
    order = ("out.charge", "out.cathode_mass", "out.cathode_gas", "out.anode_mass", "out.anode_gas")  # the model's
    parts += [{"id": k, "role": "readout", "label": k.split(".")[1].replace("_", " ")} for k in order]
    alt = {
        "electrolyte": preset["electrolyte"],
        "phase": preset["phase"],
        "electrodes": preset["electrodes"],
        "current": f"{current:g}",
        "duration": f"{duration:g}",
        "cathode": preset["cathode"]["id"],
        "anode": preset["anode"]["id"],
    }
    return s.done({"constants": constants, "parts": parts, "alt": alt})


RATES_TEMPERATURE_K = (273, 373)
RATES_CONCENTRATION = (0.1, 2)
# A run ends at kt = ln(10^6): the product is then within one part in a million of all it can become (the client's).
RATES_END_KT = "ln(1000000)"


def rates_sim(spec: dict[str, Any], s: Session) -> Computed:
    """The rate constant and the starting rate from math_evaluate (k = A e^(-Ea/RT), rate = k c0), and how long the run
    takes; the catalyst lowers the activation energy and so must give a lower one (VSL-46)."""
    params = spec["params"]
    _within("temperature", params["temperature_k"], RATES_TEMPERATURE_K)
    _within("concentration", params["concentration_mol_l"], RATES_CONCENTRATION)
    if not params["catalysed_activation_energy_j_mol"] < params["activation_energy_j_mol"]:
        raise ResolutionError("a catalyst gives a lower activation energy than the uncatalysed reaction")
    temperature, c0 = params["temperature_k"], params["concentration_mol_l"]

    def k(ea: float) -> str:
        return f"{params['pre_exponential_s']}*exp(-{ea}/({GAS_CONSTANT}*{temperature}))"

    uncatalysed = k(params["activation_energy_j_mol"])
    readouts = {
        "out.rate_constant": shown(s.run("math_evaluate", {"expression": uncatalysed, "sf": 3})),
        "out.rate": shown(s.run("math_evaluate", {"expression": f"{uncatalysed}*{c0}", "sf": 3})),
        "out.product": "0",
    }
    end = s.run("math_evaluate", {"expression": f"{RATES_END_KT}/({uncatalysed})", "sf": 3})
    s.labels.update(readouts)
    s.labels["end.time"] = shown(end)
    parts = [{"id": f"ctrl.{c}", "role": "control", "label": c} for c in ("temperature", "concentration", "catalyst")]
    parts += [{"id": k, "role": "readout", "label": k.split(".")[1].replace("_", " ")} for k in readouts]
    alt = {
        "temperature": f"{temperature:g}",
        "concentration": f"{c0:g}",
        "k": readouts["out.rate_constant"],
        "time": shown(end),
    }
    return s.done({"parts": parts, "alt": alt})


EQUILIBRIUM_SPAN_K = 200  # the client's temperature slider runs this far either side of the reference


def equilibrium_sim(spec: dict[str, Any], s: Session) -> Computed:
    """Kc at the starting temperature from the reference value (van't Hoff, constant enthalpy) with math_evaluate, and
    the equilibrium the starting mixture reaches with math_solve: one admissible extent keeping every species
    non-negative, or the request is refused. Pure solids and liquids take no part (activity 1)."""
    params = spec["params"]
    species = params["reaction"]["species"]
    ids = [sp["id"] for sp in species]
    if len(set(ids)) != len(ids):
        raise ResolutionError("species ids repeat")
    sides = {sp["side"] for sp in species}
    if sides != {"reactant", "product"}:
        raise ResolutionError("a reversible reaction needs a reactant and a product")
    active = [sp for sp in species if sp["state"] in ("gas", "aqueous")]
    initial = {c["species_id"]: c["value"] for c in params["initial_concentrations"]}
    unknown = sorted(set(initial) - set(ids))
    if unknown:
        raise ResolutionError(f"initial concentrations name species not in the reaction: {', '.join(unknown)}")
    reference = params["reference_temperature_k"]
    _within(
        "temperature", params["temperature_k"], (max(1, reference - EQUILIBRIUM_SPAN_K), reference + EQUILIBRIUM_SPAN_K)
    )
    kc_expr = (
        f"{params['reference_kc']}*exp(-({params['forward_enthalpy_j_mol']})/{GAS_CONSTANT}"
        f"*(1/{params['temperature_k']}-1/{reference}))"
    )
    kc = s.run("math_evaluate", {"expression": kc_expr, "sf": 3})
    # The extent x from the starting mixture: products (c + v x)^v over reactants (c - v x)^v equals Kc.
    lo, hi = float("-inf"), float("inf")
    top, bottom = [], []
    for sp in active:
        c, v = initial.get(sp["id"], 0), sp["coefficient"]
        if sp["side"] == "reactant":
            hi = min(hi, c / v)
            bottom.append(f"({c}-{v}*x)^{v}")
        else:
            lo = max(lo, -c / v)
            top.append(f"({c}+{v}*x)^{v}")
    equation = f"{'*'.join(top) or '1'} = ({kc['exact']['decimal']})*{'*'.join(bottom) or '1'}"
    solved = s.run("math_solve", {"equations": [equation], "unknowns": ["x"], "domain": "real"})
    roots = [float(r["x"]["decimal"]) for r in solved.get("solutions", []) if "x" in r]
    admissible = sorted({round(x, 12) for x in roots if lo - 1e-12 <= x <= hi + 1e-12})
    if len(admissible) != 1:
        raise ResolutionError(f"the equilibrium has {len(admissible)} admissible extents, not one")
    extent = admissible[0]
    readouts = {"out.kc": shown(kc)}
    for sp in active:
        c, v = initial.get(sp["id"], 0), sp["coefficient"]
        sign = 1 if sp["side"] == "product" else -1
        readouts[f"eq.c_{sp['id']}"] = significant(str(c + sign * v * extent))
    s.labels.update(readouts)
    order = ("temperature", "volume", "add_reactant", "add_product", "catalyst")  # the client model's control order
    controls = [c for c in order if c in params["controls"]]
    parts = [{"id": f"ctrl.{c}", "role": "control", "label": c.replace("_", " ")} for c in controls]
    parts += [{"id": f"out.c_{sp['id']}", "role": "readout", "label": sp["formula"]} for sp in active]
    parts += [{"id": "out.kc", "role": "readout", "label": "Kc"}]
    equation_text = " ⇌ ".join(
        " + ".join(
            f"{sp['coefficient'] if sp['coefficient'] > 1 else ''}{sp['formula']}"
            for sp in species
            if sp["side"] == side
        )
        for side in ("reactant", "product")
    )
    at_equilibrium = ", ".join(f"{sp['formula']} {readouts['eq.c_' + sp['id']]}" for sp in active)
    alt = {"equation": equation_text, "temperature": f"{params['temperature_k']:g}", "kc": readouts["out.kc"],
           "equilibrium": at_equilibrium}  # fmt: skip
    return s.done({"extent": extent, "parts": parts, "alt": alt})


# population_sim: the client's sliders run from a tenth of each starting rate to twice it, in tenths (VSL-30), so a
# rate too small for the slider's steps is refused.
POPULATION_SMALLEST_RATE = 0.0001
POPULATION_CONTROLS = {
    "logistic": ("growth_rate", "carrying_capacity"),
    "predator_prey": ("growth_rate", "predation_rate", "predator_death_rate", "conversion_rate"),
}


def population_sim(spec: dict[str, Any], s: Session) -> Computed:
    """Logistic growth, N' = rN(1 - N/K): the population at the end of the run from the closed form with math_evaluate.
    Predator and prey (Lotka-Volterra: x' = ax - bxy, y' = dxy - cy, every coefficient positive): the equilibrium
    (c/d, a/b) and the conserved quantity dx - c ln x + by - a ln y from math_evaluate. The populations start above
    zero; no ecological prediction is claimed (VSL-46)."""
    params = spec["params"]
    model = params["model"]
    for name in POPULATION_CONTROLS[model]:
        if params[name] < POPULATION_SMALLEST_RATE:
            raise ResolutionError(f"{name.replace('_', ' ')} {params[name]:g} is below {POPULATION_SMALLEST_RATE:g}")
    prey, duration = _plain(params["initial_prey"]), _plain(params["duration"])
    if not params["initial_prey"] > 0:
        raise ResolutionError("the population starts at zero, so nothing grows")
    readouts = {"out.time": "0", "out.prey": f"{params['initial_prey']:g}"}
    if model == "logistic":
        r, k = _plain(params["growth_rate"]), _plain(params["carrying_capacity"])
        end = s.run("math_evaluate", {"expression": f"{k}*{prey}/({prey}+({k}-{prey})*exp(-{r}*{duration}))", "sf": 3})
        s.labels["end.prey"] = shown(end)
        alt = {"prey": f"{params['initial_prey']:g}", "rate": f"{params['growth_rate']:g}",
               "capacity": f"{params['carrying_capacity']:g}", "duration": f"{params['duration']:g}",
               "end": shown(end)}  # fmt: skip
    else:
        if not params["initial_predator"] > 0:
            raise ResolutionError("with no predators the prey grow without limit; start with some predators")
        a, b = _plain(params["growth_rate"]), _plain(params["predation_rate"])
        c, d = _plain(params["predator_death_rate"]), _plain(params["conversion_rate"])
        predators = _plain(params["initial_predator"])
        readouts["out.predator"] = f"{params['initial_predator']:g}"
        s.labels["eq.prey"] = shown(s.run("math_evaluate", {"expression": f"{c}/{d}", "sf": 3}))
        s.labels["eq.predator"] = shown(s.run("math_evaluate", {"expression": f"{a}/{b}", "sf": 3}))
        conserved = f"{d}*{prey}-{c}*ln({prey})+{b}*{predators}-{a}*ln({predators})"
        s.labels["conserved"] = shown(s.run("math_evaluate", {"expression": conserved, "sf": 3}))
        alt = {"prey": f"{params['initial_prey']:g}", "predators": f"{params['initial_predator']:g}",
               "prey_eq": s.labels["eq.prey"], "predator_eq": s.labels["eq.predator"],
               "duration": f"{params['duration']:g}"}  # fmt: skip
    s.labels.update(readouts)
    parts = [{"id": f"ctrl.{c}", "role": "control", "label": c.replace("_", " ")} for c in POPULATION_CONTROLS[model]]
    parts += [{"id": k, "role": "readout", "label": k.split(".")[1]} for k in readouts]
    return s.done({"parts": parts, "alt": alt})


# --- chemistry -----------------------------------------------------------------------------------------------------


def _equation(spec: dict[str, Any]) -> str:
    params = spec["params"]
    arrow = " ⇌ " if params.get("arrow") == "equilibrium" else " -> "
    return (
        " + ".join(s["formula"] for s in params["reactants"])
        + arrow
        + " + ".join(s["formula"] for s in params["products"])
    )


def balanced_equation(spec: dict[str, Any], s: Session) -> Computed:
    value = s.run("chem_balance", {"equation": _equation(spec).replace(" ⇌ ", " -> ")})
    species = value["species"]
    counter = {"reactants": 0, "products": 0}
    for sp in species:
        counter[sp["side"]] += 1
        part = f"{'r' if sp['side'] == 'reactants' else 'p'}{counter[sp['side']]}"
        s.labels[part] = f"{sp['coefficient'] if sp['coefficient'] != 1 else ''}{sp['formula']}"
    for row in value["atom_counts"]:
        s.labels[f"atoms.{row['element']}"] = f"{row['reactants']} = {row['products']}"
    return s.done(
        {
            "species": species,
            "coefficients": value["coefficients"],
            "equation": value["equation"],
            "atom_counts": value["atom_counts"],
        }
    )


def particle_diagram(spec: dict[str, Any], s: Session) -> Computed:
    params = spec["params"]
    if params["mode"] != "reaction":
        return {}
    value = s.run("chem_balance", {"equation": params["equation"]})
    for i, sp in enumerate(value["species"], 1):
        s.labels[f"sp{i}"] = f"{sp['coefficient'] if sp['coefficient'] != 1 else ''}{sp['formula']}"
    return s.done({"species": value["species"], "equation": value["equation"]})


# --- organic and A level chemistry (VSL-06, VSL-07, VSL-46) ---------------------------------------------------------


def _unique(ids: list[str], what: str) -> dict[str, str]:
    try:
        return unique_part_ids(ids, what)
    except ValueError as error:
        raise ResolutionError(str(error)) from None


def organic_structure(spec: dict[str, Any], s: Session) -> Computed:
    """Each structure from chem_organic (VER-33: canonicalised and checked by RDKit, names from the pack's curated
    table): its formula, functional groups, atom and bond ids and its drawing, which the engine's tool call has
    already sanitised as library SVG is (VSL-23, engine.tool_drawings); a comparison also gets the tool's
    relationship. Groups to highlight must be ones the tool found."""
    params = spec["params"]
    inputs = params["inputs"]
    value = s.run(
        "chem_organic", {"task": params["task"], "representation": params["representation"], "inputs": inputs}
    )
    structures = value.get("structures") or []
    if len(structures) != len(inputs):
        raise ResolutionError(f"chem_organic gave {len(structures)} structures for {len(inputs)} inputs")
    wanted = list(params.get("highlight_groups") or [])
    found = {g.lower() for st in structures for g in st["functional_groups"]}
    missing = [g for g in wanted if g.lower() not in found]
    if missing:
        raise ResolutionError(f"chem_organic found no {', '.join(missing)} group")
    out = []
    for n, (given, st) in enumerate(zip(inputs, structures, strict=True), 1):
        s.labels[f"mol_{n}"] = given["value"] if given["kind"] == "name" else st["canonical_smiles"]
        s.labels[f"mol_{n}.formula"] = st["formula"]
        highlighted = [g for g in wanted if g.lower() in {f.lower() for f in st["functional_groups"]}]
        out.append(
            {
                "canonical_smiles": st["canonical_smiles"],
                "formula": st["formula"],
                "functional_groups": st["functional_groups"],
                "highlighted": highlighted,
                "svg": st["svg"],
                "atom_ids": st["atom_ids"],
                "bond_ids": st["bond_ids"],
            }
        )
    computed: Computed = {"structures": out}
    if value.get("relationship"):
        computed["relationship"] = value["relationship"]
    return s.done(computed)


def energy_levels(spec: dict[str, Any], s: Session) -> Computed:
    """Each transition's photon energy from math_evaluate, |E(to) - E(from)| in eV, with its direction kept: emission
    when the electron falls to a lower level, absorption when it rises (VSL-46)."""
    params = spec["params"]
    levels = {lv["id"]: lv for lv in params["levels"]}
    _unique([lv["id"] for lv in params["levels"]], "level")
    transitions, seen = [], set()
    for tr in params["transitions"]:
        if tr["from"] not in levels or tr["to"] not in levels:
            raise ResolutionError(f"a transition names a level that is not drawn ({tr['from']} to {tr['to']})")
        if tr["from"] == tr["to"] or (tr["from"], tr["to"]) in seen:
            raise ResolutionError(f"the transition {tr['from']} to {tr['to']} is empty or repeated")
        seen.add((tr["from"], tr["to"]))
        start, end = levels[tr["from"]]["energy_ev"], levels[tr["to"]]["energy_ev"]
        if start == end:
            raise ResolutionError(f"{tr['from']} and {tr['to']} have the same energy; no photon is involved")
        tid = f"tr_{part_id(tr['from'])}_{part_id(tr['to'])}"
        entry: dict[str, Any] = {"id": tid, "direction": "emission" if end < start else "absorption"}
        if params["show_photon_energy"]:
            value = s.run("math_evaluate", {"expression": f"abs(({end})-({start}))", "sf": 3})
            entry["energy_ev"] = float(value["exact"]["decimal"])
            s.labels[tid] = f"{shown(value)} eV"
        transitions.append(entry)
    for lv in params["levels"]:
        s.labels[f"lvl_{part_id(lv['id'])}"] = f"{lv['label']}: {lv['energy_ev']:g} eV"
    return s.done({"transitions": transitions})


# A tail beyond this many RT above the activation energy holds less than e^-80 of the molecules (presentation bound
# of the integral, never a displayed number).
MB_TAIL_RT = 80


def maxwell_boltzmann(spec: dict[str, Any], s: Session) -> Computed:
    """The energy distribution f(E) = 2 sqrt(E/pi) (RT)^(-3/2) exp(-E/RT) for each temperature, sampled by
    math_plot_points over 0 to energy_max, and the fraction of molecules with at least Ea from math_integrate over
    the same function, so the shaded tail and the curve are one distribution (VSL-46)."""
    params = spec["params"]
    temperatures = params["temperatures_k"]
    ea, top = params["activation_energy_j_mol"], params["energy_max_j_mol"]
    if len(set(temperatures)) != len(temperatures):
        raise ResolutionError("a temperature repeats")
    if not ea < top:
        raise ResolutionError("the activation energy must lie inside the energy axis")
    curves, fractions = {}, {}
    for n, t in enumerate(temperatures, 1):
        rt = f"({GAS_CONSTANT}*{t})"
        density = f"2*sqrt(x/pi)*(1/{rt})^(3/2)*exp(-x/{rt})"
        plotted = s.run(
            "math_plot_points",
            {"curves": [{"id": f"curve_{n}", "expr": density}], "x_range": [0, top], "max_points": 120},
        )
        curves[f"curve_{n}"] = plotted["curves"][0]["segments"]
        upper = f"{ea + MB_TAIL_RT * float(GAS_CONSTANT) * t:.6g}"
        tail = s.run("math_integrate", {"expression": density, "variable": "x", "lower": f"{ea:g}", "upper": upper})
        fractions[f"tail_{n}"] = float(tail["definite"]["decimal"])
        s.labels[f"tail_{n}"] = shown(tail["definite"])
        s.labels[f"curve_{n}"] = f"{t:g} K"
    s.labels["ea"] = f"Ea = {ea:g} J/mol"
    return s.done({"curves": curves, "fractions": fractions})


BORN_HABER_ONE = ("formation", "lattice")


def born_haber_cycle(spec: dict[str, Any], s: Session) -> Computed:
    """The unknown step from math_evaluate so that the cycle closes: the sum of orientation x coefficient x enthalpy
    round the cycle is zero, checked again with the found value (VSL-46). chem_energetics has no cycle task until the
    A level checkers (M26.a), so the closing sum goes through math_evaluate as the other A level figures do (VSL-07)."""
    params = spec["params"]
    steps = params["steps"]
    ids = _unique([st["id"] for st in steps], "step")
    unknown = params["unknown_step"]
    if unknown not in ids:
        raise ResolutionError(f"unknown_step {unknown} is not a step of the cycle")
    for st in steps:
        if (st["id"] == unknown) == ("enthalpy_kj_mol" in st):
            raise ResolutionError(f"exactly the unknown step omits its enthalpy ({st['id']})")
    for kind in BORN_HABER_ONE:
        if sum(1 for st in steps if st["kind"] == kind) != 1:
            raise ResolutionError(f"a Born-Haber cycle has exactly one {kind} step")

    def term(st: dict[str, Any], enthalpy: str) -> str:
        return f"({st['orientation']})*({st['coefficient']})*({enthalpy})"

    target = next(st for st in steps if st["id"] == unknown)
    known = " + ".join(term(st, str(st["enthalpy_kj_mol"])) for st in steps if st["id"] != unknown)
    value = s.run("math_evaluate", {"expression": f"-({known})/(({target['orientation']})*({target['coefficient']}))"})
    found = value["exact"]["text"]
    closure = s.run("math_evaluate", {"expression": f"{known} + {term(target, found)}"})
    if closure["exact"]["text"] != "0":
        raise ResolutionError("the cycle does not close")
    contributions = {}
    for st in steps:
        enthalpy = float(value["exact"]["decimal"]) if st["id"] == unknown else float(st["enthalpy_kj_mol"])
        pid = f"step_{ids[st['id']]}"
        contributions[pid] = st["orientation"] * st["coefficient"] * enthalpy
        text = shown(value) if st["id"] == unknown else f"{st['enthalpy_kj_mol']:g}"
        sign = "+" if not text.startswith("-") and float(text) != 0 else ""
        times = f"{st['coefficient']:g} \u00d7 " if st["coefficient"] != 1 else ""
        s.labels[pid] = f"{times}{sign}{text}"
    # Each step's contribution round the cycle, for the drawing's levels (presentation geometry of traced values).
    return s.done({"contributions": contributions, "unknown": f"step_{ids[unknown]}"})


# A qualitative response settles over a fifth of the time after the event (illustrative, never a measured rate).
SETTLE_FRACTION = 5


def concentration_time(spec: dict[str, Any], s: Session) -> Computed:
    """Measured data as given; a first-order decay c = c0 exp(-kt) sampled by math_plot_points with its half-life and
    initial-rate tangent from math_evaluate; or an illustrative equilibrium response whose reaction chem_balance
    checks, whose plateaus move the way Le Chatelier says, and whose time scale is never claimed (VSL-46: no kinetics
    from Kc, no fitted claim from data)."""
    params = spec["params"]
    species = params["species"]
    ids = _unique([sp["id"] for sp in species], "species")
    lo, hi = params["time_range"]
    if not lo < hi:
        raise ResolutionError("time_range runs the wrong way")
    mode = params["mode"]
    if mode != "first_order" and params.get("show_initial_tangent"):
        raise ResolutionError("an initial-rate tangent needs a declared rate law (first_order)")
    if mode == "data":
        curves = {}
        for sp in species:
            times = [pt["time"] for pt in sp["points"]]
            if any(b <= a for a, b in pairwise(times)) or times[0] < lo or times[-1] > hi:
                raise ResolutionError(f"{sp['id']}: the points must run forwards in time inside time_range")
            curves[f"sp_{ids[sp['id']]}"] = [[[pt["time"], pt["concentration"]] for pt in sp["points"]]]
        return s.done({"curves": curves})
    if mode == "first_order":
        return _first_order(params, ids, s)
    return _qualitative_equilibrium(params, ids, s)


def _first_order(params: dict[str, Any], ids: dict[str, str], s: Session) -> Computed:
    lo, hi = params["time_range"]
    unit, curves, tangents, halves = params["time_unit"], {}, {}, {}
    for sp in params["species"]:
        if sp.get("role") == "product":
            raise ResolutionError("first_order draws the decay of a reactant, c = c0 exp(-kt)")
        pid, c0, k = f"sp_{ids[sp['id']]}", sp["initial"], sp["rate_constant"]
        plotted = s.run(
            "math_plot_points",
            {"curves": [{"id": pid, "expr": f"{c0}*exp(-{k}*x)"}], "x_range": [lo, hi], "max_points": 80},
        )
        curves[pid] = plotted["curves"][0]["segments"]
        half = s.run("math_evaluate", {"expression": f"ln(2)/{k}", "sf": 3})
        if lo <= float(half["exact"]["decimal"]) <= hi:
            s.labels[f"{pid}.half_life"] = f"t½ = {shown(half)} {unit}"
            halves[pid] = float(half["exact"]["decimal"])
        if params.get("show_initial_tangent"):
            rate = s.run("math_evaluate", {"expression": f"{k}*{c0}*exp(-{k}*{lo})", "sf": 3})
            end = min(hi, lo + 1 / k)
            line = s.run(
                "math_plot_points",
                {
                    "curves": [{"id": f"{pid}.tangent", "expr": f"{c0}*exp(-{k}*{lo})*(1-{k}*(x-{lo}))"}],
                    "x_range": [lo, end],
                    "max_points": 20,
                },
            )
            tangents[pid] = line["curves"][0]["segments"]
            s.labels[f"{pid}.tangent"] = f"{shown(rate)} {params['concentration_unit']}/{unit}"
    return s.done({"curves": curves, "half_lives": halves, "tangents": tangents})


def _qualitative_equilibrium(params: dict[str, Any], ids: dict[str, str], s: Session) -> Computed:
    lo, hi = params["time_range"]
    event = params["event"]
    at = event["at"]
    if not lo < at < hi:
        raise ResolutionError("the change must happen inside the time shown")
    reaction = event["reaction"]["species"]
    by_id = {sp["id"]: sp for sp in reaction}
    if len(by_id) != len(reaction):
        raise ResolutionError("the reaction repeats a species id")
    for sp in params["species"]:
        if sp["id"] not in by_id or by_id[sp["id"]]["side"] != sp["role"]:
            raise ResolutionError(f"{sp['id']} is not a {sp['role']} of the reaction")
    sides = [
        " + ".join(
            f"{x['coefficient'] if x['coefficient'] > 1 else ''}{x['formula']}" for x in reaction if x["side"] == side
        )
        for side in ("reactant", "product")
    ]
    balance = s.run("chem_balance", {"equation": " -> ".join(sides)})
    if not balance.get("student", {}).get("balanced"):
        raise ResolutionError(f"{' -> '.join(sides)} is not balanced")
    try:
        moves = expected_moves(params)
    except ValueError as error:
        raise ResolutionError(str(error)) from None
    added = event.get("species_id") if event["change"] in ("add_species", "remove_species") else None
    tau = (hi - at) / SETTLE_FRACTION
    curves = {}
    for sp in params["species"]:
        before, after = sp["initial_plateau"], sp["final_plateau"]
        moved = "unchanged" if after == before else ("rises" if after > before else "falls")
        if moved != moves[sp["id"]]:
            raise ResolutionError(f"{sp['id']} should be {moves[sp['id']]} after the change, not {moved}")
        # The species added or removed jumps at the change and then partly recovers; the rest move smoothly.
        start = before + 2 * (after - before) if sp["id"] == added else before
        settle = f"({after})+(({start})-({after}))*exp(-(x-({at}))/({tau}))"
        plotted = s.run(
            "math_plot_points", {"curves": [{"id": "after", "expr": settle}], "x_range": [at, hi], "max_points": 60}
        )
        curves[f"sp_{ids[sp['id']]}"] = [[[lo, before], [at, before]], *plotted["curves"][0]["segments"]]
    equation = " ⇌ ".join(sides)
    return s.done({"curves": curves, "moves": moves, "equation": equation, "illustrative": True})


# --- accounting ----------------------------------------------------------------------------------------------------


def journal_entry(spec: dict[str, Any], s: Session) -> Computed:
    lines = [{"account": x["account"], "side": x["side"], "amount": x["amount"]} for x in spec["params"]["lines"]]
    value = s.run("acc_double_entry", {"task": "post", "entries": [{"ref": "j1", "lines": lines}]})
    entry = value["entries"][0]
    s.labels["total_debit"] = entry["total_debit"]
    s.labels["total_credit"] = entry["total_credit"]
    return s.done({"balanced": entry["balanced"]})


def t_account(spec: dict[str, Any], s: Session) -> Computed:
    params = spec["params"]
    names = {a["id"]: a["name"] for a in params["accounts"]}
    entries = [
        {
            "ref": e["id"],
            "lines": [
                {"account": names.get(e["debit"], e["debit"]), "side": "debit", "amount": e["amount"]},
                {"account": names.get(e["credit"], e["credit"]), "side": "credit", "amount": e["amount"]},
            ],
        }
        for e in params["entries"]
    ]
    args: dict[str, Any] = {"task": "post", "entries": entries}
    opening = [
        {"account": a["name"], "side": a.get("opening_side", "debit"), "amount": a["opening_balance"]}
        for a in params["accounts"]
        if a.get("opening_balance")
    ]
    if opening:
        args["opening_balances"] = opening
    value = s.run("acc_double_entry", args)
    by_name = {acc["account"]: acc for acc in value["ledger"]}
    balances, totals = {}, {}
    for aid, name in names.items():
        ledger = by_name.get(name)
        if ledger is None:
            continue
        if ledger.get("balance_cd"):
            balances[aid] = ledger["balance_cd"]
            s.labels[f"{aid}.balance_cd"] = ledger["balance_cd"]["amount"]
        totals[aid] = ledger.get("total")
    return s.done({"balances": balances, "totals": totals})


def trial_balance(spec: dict[str, Any], s: Session) -> Computed:
    params = spec["params"]
    balances = params.get("balances") or []
    opening = [{"account": b["account"], "side": b["side"], "amount": b["amount"]} for b in balances]
    # Posting nothing on top of the balances gives the trial balance of the balances themselves.
    value = s.run("acc_double_entry", {"task": "post", "opening_balances": opening})
    tb = value["trial_balance"]
    rows = tb["rows"]
    for i, row in enumerate(rows, 1):
        s.labels[f"row_{i}"] = row["amount"]
    s.labels["total_debit"] = tb["total_debit"]
    s.labels["total_credit"] = tb["total_credit"]
    if not tb["agrees"]:
        s.labels["mismatch"] = tb["difference"]
    return s.done({"rows": rows, "agrees": tb["agrees"], "difference": tb["difference"]})


def financial_statements(spec: dict[str, Any], s: Session) -> Computed:
    params = spec["params"]
    args: dict[str, Any] = {"trial_balance": params["trial_balance"]}
    if params.get("adjustments"):
        args["adjustments"] = params["adjustments"]
    value = s.run("acc_statements", args)
    statement = value[params["statement"]]
    lines = []
    for line in statement["lines"]:
        lid = "line_" + re.sub(r"[^A-Za-z0-9_]", "_", str(line["id"]))
        lines.append({"id": lid, "label": line["label"], "op": line["op"]})
        s.labels[lid] = line["amount"]
    return s.done({"title": statement["title"], "lines": lines})


def bank_reconciliation(spec: dict[str, Any], s: Session) -> Computed:
    params = spec["params"]

    def items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: r[k] for k in ("direction", "amount", "details", "ref") if k in r} for r in rows]

    value = s.run(
        "acc_double_entry",
        {
            "task": "reconcile",
            "cash_book": {"balance": params["cash_book_balance"], "entries": items(params.get("cash_book", []))},
            "bank_statement": {
                "balance": params["bank_statement_balance"],
                "entries": items(params.get("bank_statement", [])),
            },
        },
    )
    lines = []
    for line in value["statement"]["lines"]:
        lid = "line_" + re.sub(r"[^A-Za-z0-9_]", "_", str(line["id"]))
        lines.append({"id": lid, "label": line["label"], "op": line["op"]})
        s.labels[lid] = line["amount"]
    return s.done(
        {"lines": lines, "updated_balance": value["cash_book"]["updated_balance"], "reconciled": value["reconciled"]}
    )


# --- biology (VSL-05, VSL-46) ---------------------------------------------------------------------------------------

DOMINANCE = {"recessive": 0, "codominant": 1, "dominant": 2}
SEX_LINKED = re.compile(r"^X([A-Za-z])(?:X([A-Za-z])|(Y))$")
BLOOD_GROUP = re.compile(r"^I([ABO])I([ABO])$")
Gamete = tuple[str, ...]
Y_CHROMOSOME = "/Y"  # a father's Y in a sex-linked cross; never an allele symbol, which is letters only


class _Cross:
    """A cross as declared: the allele legend (rank = its place in the legend), and each parent's genes. The cells,
    classes and phenotypes follow from the declarations alone, never from how a symbol is spelt (VER-35)."""

    def __init__(self, params: dict[str, Any]) -> None:
        self.kind = str(params["cross"])
        self.alleles: dict[str, dict[str, Any]] = {}
        for rank, allele in enumerate(params["alleles"]):
            if allele["symbol"] in self.alleles:
                raise ResolutionError(f"allele {allele['symbol']} is declared twice")
            self.alleles[allele["symbol"]] = {**allele, "rank": rank}
        self.parents = [self.genes(str(params["parent1"])), self.genes(str(params["parent2"]))]
        if self.kind == "sex_linked" and sorted(len(p[0]) for p in self.parents) != [1, 2]:
            raise ResolutionError("a sex-linked cross needs a mother (XX) and a father (XY)")
        if len(self.parents[0]) != len(self.parents[1]):
            raise ResolutionError("the parents do not carry the same genes")

    def symbol(self, token: str) -> str:
        found = f"I{token}" if self.kind == "multiple_alleles" and f"I{token}" in self.alleles else token
        if found not in self.alleles:
            raise ResolutionError(f"{token} is not in the allele legend")
        return found

    def genes(self, genotype: str) -> list[list[str]]:
        """The parent's genes, each its alleles in legend order; a sex-linked father's gene holds his one X allele."""
        if self.kind == "sex_linked":
            match = SEX_LINKED.match(genotype)
            if not match:
                raise ResolutionError(f"{genotype} is not a sex-linked genotype")
            return [self.ordered([self.symbol(a) for a in match.groups()[:2] if a])]
        if self.kind == "multiple_alleles":
            match = BLOOD_GROUP.match(genotype)
            if not match:
                raise ResolutionError(f"{genotype} is not a multiple-allele genotype")
            return [self.ordered([self.symbol(a) for a in match.groups()])]
        tokens, rest = [], genotype
        while rest:  # the longest declared symbol first
            found = next((s for s in sorted(self.alleles, key=len, reverse=True) if rest.startswith(s)), None)
            if found is None:
                raise ResolutionError(f"{genotype} has an allele that is not in the legend")
            tokens.append(found)
            rest = rest[len(found) :]
        count = 4 if self.kind == "dihybrid" else 2
        if len(tokens) != count:
            raise ResolutionError(f"{genotype} does not have {count} alleles for a {self.kind} cross")
        genes = [self.ordered(tokens[i : i + 2]) for i in range(0, count, 2)]
        if self.kind == "dihybrid" and any(g[0][0].lower() != g[1][0].lower() for g in genes):
            raise ResolutionError(f"{genotype} does not pair the alleles of each gene")
        return genes

    def ordered(self, alleles: list[str]) -> list[str]:
        return sorted(alleles, key=lambda a: self.alleles[a]["rank"])

    def gametes(self, parent: list[list[str]]) -> list[Gamete]:
        """Each different gamete once (all equally likely), in the order the parent's alleles are written."""
        out: list[Gamete] = [()]
        for gene in parent:
            out = [(*g, a) for g in out for a in dict.fromkeys(gene)]
        if self.kind == "sex_linked" and len(parent[0]) == 1:
            out.append((Y_CHROMOSOME,))
        return list(dict.fromkeys(out))

    def offspring(self, a: Gamete, b: Gamete) -> list[list[str]]:
        if self.kind == "sex_linked":
            return [self.ordered([x for x in (*a, *b) if x != Y_CHROMOSOME])]
        return [self.ordered([x, y]) for x, y in zip(a, b, strict=True)]

    def allele_text(self, allele: str) -> str:
        """An allele as written in a genotype: on its X for a sex-linked gene, on I for the blood groups."""
        if allele == Y_CHROMOSOME:
            return "Y"
        if self.kind == "sex_linked":
            return f"X{allele}"
        return f"I{allele}" if self.kind == "multiple_alleles" and not allele.startswith("I") else allele

    def written(self, genes: list[list[str]]) -> str:
        text = "".join(self.allele_text(a) for gene in genes for a in gene)
        return text + "Y" if self.kind == "sex_linked" and len(genes[0]) == 1 else text

    def gamete_text(self, gamete: Gamete) -> str:
        return "".join(self.allele_text(a) for a in gamete)

    def phenotype(self, genes: list[list[str]]) -> str:
        words = []
        for gene in genes:
            records = [self.alleles[a] for a in dict.fromkeys(gene)]
            strongest = max(DOMINANCE[r["dominance"]] for r in records)
            shown_by = [r for r in records if DOMINANCE[r["dominance"]] == strongest]
            if len(shown_by) == 1 or len(gene) == 1:
                words.append(str(shown_by[0]["phenotype"]))
            elif all(r["dominance"] == "codominant" for r in shown_by) and len(shown_by) == 2:
                words.append(say("part", "punnett_both", a=shown_by[0]["phenotype"], b=shown_by[1]["phenotype"]))
            else:
                raise ResolutionError(f"{' and '.join(r['symbol'] for r in shown_by)} are both declared dominant")
        text = ", ".join(words)
        if self.kind == "sex_linked":
            sex = say("part", "sex_male" if len(genes[0]) == 1 else "sex_female")
            return say("part", "punnett_sexed", phenotype=text, sex=sex)
        return text


def _ratio(fractions: list[Fraction]) -> str:
    """Probabilities as the smallest whole-number ratio ("1 : 2 : 1")."""
    scale = 1
    for f in fractions:
        scale = scale * f.denominator // gcd(scale, f.denominator)
    whole = [int(f * scale) for f in fractions]
    common = 0
    for w in whole:
        common = gcd(common, w)
    return " : ".join(str(w // max(common, 1)) for w in whole)


def punnett_square(spec: dict[str, Any], s: Session) -> Computed:
    """The square from the declared alleles; each class's probability from math_evaluate (bio_genetics, VER-35, is
    not built in the worker yet), and the ratios from those probabilities. Every cell is equally likely: each parent's
    gametes are listed once each and are equally likely."""
    cross = _Cross(spec["params"])
    first, second = (cross.gametes(p) for p in cross.parents)
    grid: list[list[str]] = []
    genotypes: dict[str, dict[str, Any]] = {}
    for a in first:
        row = []
        for b in second:
            genes = cross.offspring(a, b)
            text = cross.written(genes)
            genotypes.setdefault(text, {"genes": genes, "male": Y_CHROMOSOME in a + b, "count": 0})["count"] += 1
            row.append(text)
        grid.append(row)
    total = len(first) * len(second)

    def rank(text: str) -> tuple[int, ...]:
        record = genotypes[text]
        return (int(record["male"]), *(cross.alleles[a]["rank"] for gene in record["genes"] for a in gene))

    genotype_classes: list[dict[str, Any]] = []
    phenotype_counts: dict[str, int] = {}
    for text in sorted(genotypes, key=rank):
        record = genotypes[text]
        probability = shown(s.run("math_evaluate", {"expression": f"{record['count']}/{total}"}))
        genotype_classes.append({"genotype": text, "probability": probability})
        phenotype = cross.phenotype(record["genes"])
        phenotype_counts[phenotype] = phenotype_counts.get(phenotype, 0) + record["count"]
    phenotype_classes = [
        {"phenotype": name, "probability": shown(s.run("math_evaluate", {"expression": f"{count}/{total}"}))}
        for name, count in phenotype_counts.items()
    ]
    for classes, key in ((genotype_classes, "genotype_ratio"), (phenotype_classes, "phenotype_ratio")):
        ratio = _ratio([Fraction(c["probability"]) for c in classes])
        for c, part in zip(classes, ratio.split(" : "), strict=True):
            c["ratio"] = int(part)
        s.labels[key] = ratio
    return s.done({
        "gametes": {"p1": [cross.gamete_text(g) for g in first], "p2": [cross.gamete_text(g) for g in second]},
        "grid": grid,
        "genotype_classes": genotype_classes,
        "phenotype_classes": phenotype_classes,
    })  # fmt: skip


def food_web(spec: dict[str, Any], s: Session) -> Computed:
    """VSL-46: unique organisms, links that name them, every arrow from food to consumer up the trophic levels the
    author gave, and every consumer eating something. Pack-supported facts, not numbers: no tool runs."""
    params = spec["params"]
    organisms = food_web_organisms(params)
    levels = {o["id"]: int(o["trophic_level"]) for o in organisms}
    parts = {safe_id(o["id"]) for o in organisms}
    if len(levels) != len(organisms) or len(parts) != len(organisms):
        raise ResolutionError("an organism id is used twice")
    seen, eats = set(), set()
    for link in params["links"]:
        a, b = link["from"], link["to"]
        if a not in levels or b not in levels:
            raise ResolutionError(f"the link {a} -> {b} names an organism the web does not have")
        if levels[b] <= levels[a]:
            raise ResolutionError(f"{b} eats {a}, so it must be at a higher trophic level")
        if (a, b) in seen:
            raise ResolutionError(f"the link {a} -> {b} is listed twice")
        seen.add((a, b))
        eats.add(b)
    hungry = [o for o in levels if levels[o] > 1 and o not in eats]
    if hungry:
        raise ResolutionError(f"{hungry[0]} is a consumer that eats nothing in this web")
    return s.done({
        "organisms": [{"id": o["id"], "part": f"org.{safe_id(o['id'])}", "label": o["label"], "level": levels[o["id"]]}
                      for o in organisms],
        "links": [{"from": x["from"], "to": x["to"], "part": f"link.{safe_id(x['from'])}.{safe_id(x['to'])}"}
                  for x in params["links"]],
        "levels": sorted(set(levels.values())),
    })  # fmt: skip


ROMAN = (("X", 10), ("IX", 9), ("V", 5), ("IV", 4), ("I", 1))


def roman(n: int) -> str:
    out = ""
    for letters, value in ROMAN:
        while n >= value:
            out, n = out + letters, n - value
    return out


def pedigree_chart(spec: dict[str, Any], s: Session) -> Computed:
    """VSL-46: parents exist and come from earlier generations (so no cycles), each child has one pair of parents, and
    a carrier status the author left unknown stays unknown: nothing is inferred. Each generation is laid out with a
    family's children together under their parents and each partner beside the person they partner."""
    params = spec["params"]
    people = {p["id"]: p for p in params["individuals"]}
    if len(people) != len(params["individuals"]):
        raise ResolutionError("an individual is listed twice")
    families: dict[tuple[str, str], list[str]] = {}
    has_parents: set[str] = set()
    for link in params["parents"]:
        child, a, b = link["child"], link["parent1"], link["parent2"]
        for who in (child, a, b):
            if who not in people:
                raise ResolutionError(f"{who} is not in the pedigree")
        if a == b:
            raise ResolutionError(f"{child} has the same person as both parents")
        if child in has_parents:
            raise ResolutionError(f"{child} has two pairs of parents")
        for parent in (a, b):
            if people[parent]["generation"] >= people[child]["generation"]:
                raise ResolutionError(f"{parent} is not in an earlier generation than {child}")
        has_parents.add(child)
        key = (b, a) if (b, a) in families else (a, b)
        families.setdefault(key, []).append(child)
    partners: dict[str, list[str]] = {}
    for a, b in families:
        partners.setdefault(a, []).append(b)
        partners.setdefault(b, []).append(a)
    placed: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for generation in sorted({p["generation"] for p in people.values()}):
        row: list[str] = []

        def place(who: str, gen: int = generation, row: list[str] = row) -> None:
            if who in placed or people[who]["generation"] != gen:
                return
            placed[who] = len(row)
            row.append(who)
            for partner in partners.get(who, []):
                if partner not in has_parents:
                    place(partner)

        couples = sorted(families.items(), key=lambda f: sum(placed.get(p, 0) for p in f[0]) / 2)
        for _, children in couples:
            for child in children:
                place(child)
        for who in people:
            place(who)
        rows.append({"generation": generation, "roman": roman(len(rows) + 1), "members": row})
    for r in rows:
        for n, who in enumerate(r["members"], 1):
            s.labels[f"ind.{who}"] = f"{r['roman']}-{n}"
    order = [who for r in rows for who in r["members"]]
    keep = ("id", "sex", "phenotype", "carrier")
    return s.done({
        "rows": rows,
        "people": [{k: people[who][k] for k in keep} for who in order],
        "couples": [{"parent1": a, "parent2": b, "children": c} for (a, b), c in families.items()],
    })  # fmt: skip


def gel_electrophoresis(spec: dict[str, Any], s: Session) -> Computed:
    """VSL-46: sizes are whole base pairs, each band once and largest first; a band's distance from the well is its
    place on a logarithmic schematic scale between the largest and smallest sizes shown (smaller fragments travel
    further). No migration distance is measured and nothing about the samples is inferred."""
    params = spec["params"]
    ids = [lane["id"] for lane in params["lanes"]]
    if len(set(ids)) != len(ids):
        raise ResolutionError("a lane id is used twice")
    ladder = gel_bands(params["ladder_bp"])
    sizes = set(ladder) | {b for lane in params["lanes"] for b in lane["fragments_bp"]}
    if len(sizes) < 2 or len(ladder) < 2:
        raise ResolutionError("a gel needs at least two different ladder sizes")
    top, bottom = math.log(max(sizes)), math.log(min(sizes))

    def at(bp: int) -> float:
        return round((top - math.log(bp)) / (top - bottom), 4)

    lanes = []
    for lane in params["lanes"]:
        lid = f"lane.{safe_id(lane['id'])}"
        bands = [{"bp": b, "part": f"{lid}.b{n}", "position": at(b)}
                 for n, b in enumerate(gel_bands(lane["fragments_bp"]), 1)]  # fmt: skip
        lanes.append({"id": lane["id"], "part": lid, "label": lane["label"], "bands": bands})
    return s.done({
        "ladder": [{"bp": b, "part": f"ladder.b{n}", "position": at(b)} for n, b in enumerate(ladder, 1)],
        "lanes": lanes,
        "migration": params["migration"],
    })  # fmt: skip


def microscope_view(spec: dict[str, Any], s: Session) -> Computed:
    """Magnification = image length / actual length with both in micrometres, from math_evaluate (bio_calc, VER-35,
    is not built in the worker yet); the scale bar comes from the library entry with the drawing (N3)."""
    params = spec["params"]
    value = s.run(
        "math_evaluate", {"expression": f"({params['image_length_mm']})*1000/({params['actual_length_um']})", "sf": 3}
    )
    magnification = shown(value)
    s.labels["magnification"] = f"{TIMES}{magnification}"
    return s.done({"magnification": magnification})


KIND_RESOLVERS: dict[str, Callable[[dict[str, Any], Session], Computed]] = {
    "function_graph": function_graph,
    "motion_graph": motion_graph,
    "titration_curve": titration_curve,
    "break_even_chart": break_even_chart,
    "exponential_curve": exponential_curve,
    "distribution_curve": distribution_curve,
    "data_plot": data_plot,
    "geometry_figure": geometry_figure,
    "bearing_diagram": bearing_diagram,
    "transformation_grid": transformation_grid,
    "unit_circle": unit_circle,
    "probability_tree": probability_tree,
    "motion_diagram": motion_diagram,
    "free_body": free_body,
    "circuit": circuit,
    "ray_diagram": ray_diagram,
    "balanced_equation": balanced_equation,
    "particle_diagram": particle_diagram,
    "organic_structure": organic_structure,
    "energy_levels": energy_levels,
    "maxwell_boltzmann": maxwell_boltzmann,
    "born_haber_cycle": born_haber_cycle,
    "concentration_time": concentration_time,
    "journal_entry": journal_entry,
    "t_account": t_account,
    "trial_balance": trial_balance,
    "financial_statements": financial_statements,
    "bank_reconciliation": bank_reconciliation,
    "projectile_sim": projectile_sim,
    "circuit_sim": circuit_sim,
    "titration_sim": titration_sim,
    "gas_laws_sim": gas_laws_sim,
    "function_transform_sim": function_transform_sim,
    "electrolysis_sim": electrolysis_sim,
    "rates_sim": rates_sim,
    "equilibrium_sim": equilibrium_sim,
    "population_sim": population_sim,
    "punnett_square": punnett_square,
    "food_web": food_web,
    "pedigree_chart": pedigree_chart,
    "gel_electrophoresis": gel_electrophoresis,
    "microscope_view": microscope_view,
}


def resolver(call: Call, conventions: Mapping[str, str] | None = None) -> Callable[[dict[str, Any]], Computed]:
    """The resolution step for `validate` / `Resolver`: the request's kind resolver, or nothing to compute."""

    def resolve(spec: dict[str, Any]) -> Computed:
        kind = KIND_RESOLVERS.get(spec["type"])
        if kind is None:
            return {}
        return kind(spec, Session(call, conventions or {}))

    return resolve
