"""Accounting ratios and break-even (VER-25): `acc_ratios` and `acc_break_even`.

Ratios: gross and net margin, mark-up, return on capital employed, current and acid-test ratios, inventory turnover
(times and days), receivable and payable days and gearing, each with its formula, the figures it used and the
definitions applied. Definitions that differ between syllabuses (what capital employed is, a year of 365 or 360 days,
average or closing inventory, credit or total sales, ...) come from the call, then from the pack's definition set sent
with the call (`acc.definition_set.<name>`), and are recorded with their source. A definition a ratio needs and
nobody stated has no tool default: that ratio is `unknown` with `convention_missing`, never a guessed board rule.

Break-even: contribution per unit, the break-even point in units (exact and rounded up to whole units) and in
revenue, the margin of safety (units, revenue and percentage), the units for a target profit, and the fixed-cost,
total-cost and revenue lines for the chart. Values are exact; the places shown follow the call, the pack, then the
configured default."""

from __future__ import annotations

from collections.abc import Callable
from fractions import Fraction
from math import ceil
from typing import Any

from tool_worker.functions.money import Extras, amount, convention, figure, money, places, positive
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

TIMES = "\u00d7"  # the multiplication sign in a formula as written
ORDER = ("gross_margin", "net_margin", "mark_up", "roce", "current_ratio", "acid_test", "inventory_turnover_times",
         "inventory_turnover_days", "receivable_days", "payable_days", "gearing")  # fmt: skip
# The statement figures a call may give, each a top-level argument; gross profit and cost of sales may be derived.
FIGURES = ("revenue", "credit_sales", "cost_of_sales", "gross_profit", "profit_for_the_year", "interest",
           "opening_inventory", "closing_inventory", "purchases", "credit_purchases", "current_assets",
           "current_liabilities", "prepayments", "trade_receivables", "trade_payables", "opening_capital",
           "closing_capital", "non_current_liabilities", "total_assets")  # fmt: skip
DEFINITIONS = {
    "year_days": (360, 365),
    "capital_employed": ("capital_and_non_current_liabilities", "capital", "total_assets_less_current_liabilities"),
    "capital_basis": ("closing", "opening", "average"),
    "roce_profit": ("profit_for_the_year", "profit_before_interest"),
    "inventory_basis": ("average", "closing"),
    "receivables_sales": ("credit_sales", "revenue"),
    "payables_purchases": ("credit_purchases", "purchases"),
    "quick_assets": ("less_inventory", "less_inventory_and_prepayments"),
    "gearing": ("debt_to_capital_employed", "debt_to_equity"),
}


class Missing(Exception):
    """A figure or a definition the ratio needs was not given."""

    def __init__(self, what: str, definition: bool = False) -> None:
        super().__init__(what)
        self.what = what
        self.definition = definition


class Book:
    """The figures given, with derived ones (gross profit, cost of sales), and the definitions in force."""

    def __init__(self, args: dict[str, Any], context: CallContext) -> None:
        self.given = {k: amount(args[k], k) for k in FIGURES if k in args}
        self.args, self.context = args.get("definitions", {}), context
        self.used_figures: dict[str, Fraction] = {}
        self.used_definitions: dict[str, dict[str, Any]] = {}

    def __getitem__(self, name: str) -> Fraction:
        value = self._figure(name)
        self.used_figures[name] = value
        return value

    def _figure(self, name: str) -> Fraction:
        if name in self.given:
            return self.given[name]
        g = self.given
        if name == "gross_profit" and {"revenue", "cost_of_sales"} <= set(g):
            return g["revenue"] - g["cost_of_sales"]
        if name == "cost_of_sales":
            if {"revenue", "gross_profit"} <= set(g):
                return g["revenue"] - g["gross_profit"]
            if {"opening_inventory", "purchases", "closing_inventory"} <= set(g):
                return g["opening_inventory"] + g["purchases"] - g["closing_inventory"]
        raise Missing(name)

    def definition(self, name: str) -> Any:
        if name in self.args:
            value, source = self.args[name], "item"
        else:
            held = convention(self.context, f"acc.definition_set.{name}")
            if held is None:
                raise Missing(name, definition=True)
            value, source = held
            if name == "year_days" and str(value).isdigit():
                value = int(value)
            if value not in DEFINITIONS[name]:
                raise Missing(name, definition=True)
        self.used_definitions[name] = {"value": value, "source": source}
        return value


def _capital(book: Book) -> Fraction:
    basis = book.definition("capital_basis")
    if basis == "average":
        return (book["opening_capital"] + book["closing_capital"]) / 2
    return book[f"{basis}_capital"]


def _capital_employed(book: Book) -> tuple[Fraction, str]:
    kind = book.definition("capital_employed")
    if kind == "total_assets_less_current_liabilities":
        return book["total_assets"] - book["current_liabilities"], "total assets - current liabilities"
    capital = _capital(book)
    if kind == "capital":
        return capital, "capital"
    return capital + book["non_current_liabilities"], "capital + non-current liabilities"


def _inventory(book: Book) -> tuple[Fraction, str]:
    if book.definition("inventory_basis") == "average":
        return (book["opening_inventory"] + book["closing_inventory"]) / 2, "average inventory"
    return book["closing_inventory"], "closing inventory"


Ratio = tuple[Fraction, Fraction, str, str]  # numerator, denominator (with the factor applied), unit, formula


def _gross_margin(b: Book) -> Ratio:
    return b["gross_profit"] * 100, b["revenue"], "%", f"gross profit / revenue {TIMES} 100"


def _net_margin(b: Book) -> Ratio:
    return b["profit_for_the_year"] * 100, b["revenue"], "%", f"profit for the year / revenue {TIMES} 100"


def _mark_up(b: Book) -> Ratio:
    return b["gross_profit"] * 100, b["cost_of_sales"], "%", f"gross profit / cost of sales {TIMES} 100"


def _roce(b: Book) -> Ratio:
    if b.definition("roce_profit") == "profit_before_interest":
        profit, named = b["profit_for_the_year"] + b["interest"], "profit before interest"
    else:
        profit, named = b["profit_for_the_year"], "profit for the year"
    employed, employed_text = _capital_employed(b)
    return profit * 100, employed, "%", f"{named} / ({employed_text}) {TIMES} 100"


def _current(b: Book) -> Ratio:
    return b["current_assets"], b["current_liabilities"], ":1", "current assets : current liabilities"


def _acid(b: Book) -> Ratio:
    quick = b["current_assets"] - b["closing_inventory"]
    text = "(current assets - inventory) : current liabilities"
    if b.definition("quick_assets") == "less_inventory_and_prepayments":
        quick -= b["prepayments"]
        text = "(current assets - inventory - prepayments) : current liabilities"
    return quick, b["current_liabilities"], ":1", text


def _turnover_times(b: Book) -> Ratio:
    inventory, named = _inventory(b)
    return b["cost_of_sales"], inventory, "times", f"cost of sales / {named}"


def _turnover_days(b: Book) -> Ratio:
    inventory, named = _inventory(b)
    days = b.definition("year_days")
    return inventory * days, b["cost_of_sales"], "days", f"{named} / cost of sales {TIMES} {days}"


def _receivable_days(b: Book) -> Ratio:
    sales = b.definition("receivables_sales")
    days = b.definition("year_days")
    named = sales.replace("_", " ")
    return b["trade_receivables"] * days, b[sales], "days", f"trade receivables / {named} {TIMES} {days}"


def _payable_days(b: Book) -> Ratio:
    purchases = b.definition("payables_purchases")
    days = b.definition("year_days")
    named = purchases.replace("_", " ")
    return b["trade_payables"] * days, b[purchases], "days", f"trade payables / {named} {TIMES} {days}"


def _gearing(b: Book) -> Ratio:
    debt = b["non_current_liabilities"]
    if b.definition("gearing") == "debt_to_equity":
        return debt * 100, b["closing_capital"], "%", f"non-current liabilities / capital {TIMES} 100"
    total = b["closing_capital"] + debt
    return debt * 100, total, "%", f"non-current liabilities / (capital + non-current liabilities) {TIMES} 100"


RATIOS: dict[str, Callable[[Book], Ratio]] = {
    "gross_margin": _gross_margin, "net_margin": _net_margin, "mark_up": _mark_up, "roce": _roce,
    "current_ratio": _current, "acid_test": _acid, "inventory_turnover_times": _turnover_times,
    "inventory_turnover_days": _turnover_days, "receivable_days": _receivable_days, "payable_days": _payable_days,
    "gearing": _gearing,
}  # fmt: skip


def acc_ratios(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """Each requested ratio (all of them by default) with its formula, figures and definitions (VER-25)."""
    dp = places(args, context)
    if not any(name in args for name in FIGURES):
        raise WorkerError(Code.DOMAIN_ERROR, "give the statement figures the ratios need, such as revenue")
    wanted = list(args.get("ratios", ORDER))
    out: dict[str, Any] = {}
    missing_definitions: list[str] = []
    for name in wanted:
        book = Book(args, context)
        try:
            numerator, denominator, unit, formula = RATIOS[name](book)
        except Missing as gap:
            if gap.definition:
                missing_definitions.append(gap.what)
                out[name] = {"status": "unknown", "reason": "convention_missing", "definition": gap.what}
            else:
                out[name] = {"status": "missing_figures", "missing": gap.what}
            continue
        entry: dict[str, Any] = {"formula": formula, "unit": unit,
                                 "figures": {k: money(v, dp) for k, v in book.used_figures.items()}}  # fmt: skip
        if book.used_definitions:
            entry["definitions"] = book.used_definitions
        if denominator == 0:
            out[name] = {**entry, "status": "undefined", "reason": "the denominator is zero"}
            continue
        out[name] = {**entry, "status": "ok", "value": figure(numerator / denominator, dp)}
    warnings = [{"code": "convention_missing",
                 "message": f"the pack's definition set does not state {d}, so ratios that need it are unknown"}
                for d in sorted(set(missing_definitions))]  # fmt: skip
    return {"ratios": out}, {"warnings": warnings, "method": "exact", "confidence": "proved"}


def acc_break_even(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """Contribution, break-even, margin of safety, target profit units and the chart's lines (VER-25)."""
    dp = places(args, context)
    fixed = positive(args["fixed_costs"], "fixed_costs", allow_zero=True)
    variable = positive(args["variable_cost_per_unit"], "variable_cost_per_unit", allow_zero=True)
    price = positive(args["selling_price"], "selling_price")
    contribution = price - variable
    if contribution <= 0:
        raise WorkerError(Code.DOMAIN_ERROR, "the selling price must exceed the variable cost per unit to break even")
    units = fixed / contribution
    value: dict[str, Any] = {
        "contribution_per_unit": money(contribution, dp),
        "contribution_to_sales_ratio": figure(contribution / price, 4),
        "break_even_units": {"exact": figure(units, dp), "whole_units": ceil(units)},
        "break_even_revenue": figure(units * price, dp),
        "formulas": {
            "break_even_units": "fixed costs / contribution per unit",
            "margin_of_safety": "output - break-even output",
            "target_units": "(fixed costs + target profit) / contribution per unit",
        },
    }
    if "output_units" in args:
        output = Fraction(int(args["output_units"]))
        safety = output - units
        value["margin_of_safety"] = {
            "units": figure(safety, dp),
            "revenue": figure(safety * price, dp),
            "percent": figure(safety / output * 100, dp) if output else None,
        }
        value["profit_at_output"] = money(output * contribution - fixed, dp)
    if "target_profit" in args:
        target = amount(args["target_profit"], "target_profit")
        needed = (fixed + target) / contribution
        value["target_profit"] = {"profit": money(target, dp), "exact": figure(needed, dp), "whole_units": ceil(needed)}
    top = int(args.get("chart_max_units", max(int(args.get("output_units", 0)), 2 * ceil(units), 1)))
    value["chart"] = {
        "max_units": top,
        "fixed_cost": [[0, money(fixed, dp)], [top, money(fixed, dp)]],
        "total_cost": [[0, money(fixed, dp)], [top, money(fixed + variable * top, dp)]],
        "revenue": [[0, money(Fraction(0), dp)], [top, money(price * top, dp)]],
        "break_even_point": [figure(units, dp), figure(units * price, dp)],
    }
    return value, {"method": "exact", "confidence": "proved"}
