"""Statements of cash flows by the indirect method (VER-34): `acc_cash_flow`.

Operating activities start from the profit from operations (or the profit before tax with the finance costs added
back), add back non-cash items, adjust for the changes in working capital (an increase in an asset is a cash outflow,
an increase in a liability an inflow) and deduct the interest and tax paid. Investing and financing activities follow;
their sum is the net change in cash and cash equivalents, from the opening to the closing balance.

Figures the question leaves to be found are derived, never assumed: the tax paid from the opening and closing tax
liabilities and the charge; purchases of non-current assets from the opening and closing cost and the cost of
disposals (less any revaluation); a disposal's profit or loss from its cost, accumulated depreciation and proceeds.
Each derivation is in the working. Inflows are positive and outflows negative, as the statement shows them in
brackets. A closing balance stated by the question that the flows do not reach is `unknown` (`does_not_reconcile`,
with the difference): a flow is missing, and no figure is invented to close it. A student's statement is checked line
by line with the own figure rule of `acc_statements`. Amounts are exact; the places shown come from the call, then the
pack, then the configured default."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from tool_worker.functions.money import Extras, amount, money, places, positive
from tool_worker.functions.statements import Statement, check, statement_out
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

NON_CASH = {"depreciation": ("Depreciation", 1), "amortisation": ("Amortisation", 1),
            "impairment": ("Impairment", 1), "loss_on_disposal": ("Loss on disposal", 1),
            "profit_on_disposal": ("Profit on disposal", -1), "increase_in_provisions": ("Increase in provisions", 1),
            "decrease_in_provisions": ("Decrease in provisions", -1)}  # fmt: skip
# Working capital items: +1 for an asset (an increase uses cash), -1 for a liability (an increase saves cash).
WORKING_CAPITAL = {"inventory": ("inventory", 1), "trade_receivables": ("trade receivables", 1),
                   "other_receivables": ("other receivables", 1), "prepayments": ("prepayments", 1),
                   "trade_payables": ("trade payables", -1), "other_payables": ("other payables", -1),
                   "accruals": ("accruals", -1)}  # fmt: skip
INVESTING = {
    "purchase_of_non_current_assets": ("Purchase of non-current assets", -1),
    "proceeds_from_disposals": ("Proceeds from sale of non-current assets", 1),
    "purchase_of_investments": ("Purchase of investments", -1),
    "interest_received": ("Interest received", 1),
    "dividends_received": ("Dividends received", 1),
}
FINANCING = {"share_issue": ("Proceeds from issue of shares", 1), "loans_raised": ("Loans raised", 1),
             "loans_repaid": ("Loans repaid", -1), "debentures_issued": ("Debentures issued", 1),
             "debentures_redeemed": ("Debentures redeemed", -1), "lease_payments": ("Lease payments", -1),
             "dividends_paid": ("Dividends paid", -1), "capital_introduced": ("Capital introduced", 1),
             "drawings": ("Drawings", -1)}  # fmt: skip
TITLE = "Statement of cash flows"


def _domain(message: str) -> WorkerError:
    return WorkerError(Code.DOMAIN_ERROR, message)


def _flows(raw: list[dict[str, Any]], kinds: dict[str, tuple[str, int]], field: str) -> dict[str, Fraction]:
    """Positive amounts by kind, each kind once."""
    seen: dict[str, Fraction] = {}
    for i, item in enumerate(raw):
        if item["kind"] in seen:
            raise _domain(f"{field}: {item['kind']} appears more than once; give the total")
        seen[item["kind"]] = positive(item["amount"], f"{field}[{i}].amount", allow_zero=True)
    return seen


def _derived(args: dict[str, Any], working: list[dict[str, Any]], check_id: str, dp: int) -> dict[str, Fraction]:
    """Figures derived from balances: the disposal's profit or loss and proceeds, purchases, the tax paid."""
    out: dict[str, Fraction] = {}

    def note(label: str, text: str) -> None:
        working.append({"label": label, "value": text, "check_id": check_id})

    disposal = args.get("disposal")
    if disposal is not None:
        cost = positive(disposal["cost"], "disposal.cost")
        depreciation = positive(disposal.get("accumulated_depreciation", 0), "disposal.accumulated_depreciation",
                                allow_zero=True)  # fmt: skip
        if depreciation > cost:
            raise _domain("disposal: accumulated depreciation cannot exceed cost")
        proceeds = positive(disposal["proceeds"], "disposal.proceeds", allow_zero=True)
        carrying = cost - depreciation
        out["proceeds_from_disposals"] = proceeds
        gain = proceeds - carrying
        out["profit_on_disposal" if gain >= 0 else "loss_on_disposal"] = abs(gain)
        note("disposal", f"proceeds {money(proceeds, dp)} - carrying amount ({money(cost, dp)} - "
             f"{money(depreciation, dp)}) = {'profit' if gain >= 0 else 'loss'} {money(abs(gain), dp)}")  # fmt: skip
    balances = args.get("non_current_asset_balances")
    if balances is not None:
        opening = positive(balances["opening_cost"], "non_current_asset_balances.opening_cost", allow_zero=True)
        closing = positive(balances["closing_cost"], "non_current_asset_balances.closing_cost", allow_zero=True)
        default_disposed = disposal["cost"] if disposal is not None else 0
        disposed = positive(balances.get("disposals_cost", default_disposed), "disposals_cost", allow_zero=True)
        revalued = positive(balances.get("revaluation_surplus", 0), "revaluation_surplus", allow_zero=True)
        bought = closing - opening + disposed - revalued
        if bought < 0:
            raise _domain("non_current_asset_balances: the balances imply negative purchases; check the disposals")
        out["purchase_of_non_current_assets"] = bought
        note("purchases", f"closing cost {money(closing, dp)} - opening cost {money(opening, dp)} + cost of disposals "
             f"{money(disposed, dp)} - revaluation {money(revalued, dp)} = {money(bought, dp)}")  # fmt: skip
    tax = args.get("tax_balances")
    if tax is not None:
        opening = amount(tax["opening_liability"], "tax_balances.opening_liability")
        charge = amount(tax["charge"], "tax_balances.charge")
        closing = amount(tax["closing_liability"], "tax_balances.closing_liability")
        out["tax_paid"] = opening + charge - closing
        if out["tax_paid"] < 0:
            raise _domain("tax_balances: the balances imply a negative payment")
        note("tax paid", f"opening liability {money(opening, dp)} + charge {money(charge, dp)} - closing liability "
             f"{money(closing, dp)} = {money(out['tax_paid'], dp)}")  # fmt: skip
    return out


def _merge(given: dict[str, Fraction], derived: dict[str, Fraction], kinds: tuple[str, ...]) -> None:
    for kind in kinds:
        if kind in derived:
            if kind in given and given[kind] != derived[kind]:
                raise _domain(f"{kind} is given and also derived from the balances, with different amounts")
            given[kind] = derived[kind]


def build(args: dict[str, Any], working: list[dict[str, Any]], check_id: str, dp: int) -> Statement:
    s = Statement(args.get("terms", {}))
    derived = _derived(args, working, check_id, dp)
    operating: list[tuple[int, str]] = []
    if "profit_from_operations" in args:
        if "profit_before_tax" in args:
            raise _domain("give profit_from_operations or profit_before_tax, not both")
        s.add("profit_from_operations", amount(args["profit_from_operations"], "profit_from_operations"),
              label="Profit from operations")  # fmt: skip
        operating.append((1, "profit_from_operations"))
    elif "profit_before_tax" in args:
        s.add("profit_before_tax", amount(args["profit_before_tax"], "profit_before_tax"), label="Profit before tax")
        operating.append((1, "profit_before_tax"))
        if "finance_costs" in args:
            s.add("finance_costs", positive(args["finance_costs"], "finance_costs", allow_zero=True),
                  label="Finance costs added back")  # fmt: skip
            operating.append((1, "finance_costs"))
    else:
        raise _domain("give profit_from_operations or profit_before_tax")
    non_cash = _flows(args.get("non_cash", []), NON_CASH, "non_cash")
    _merge(non_cash, derived, ("profit_on_disposal", "loss_on_disposal"))
    for kind, (label, sign) in NON_CASH.items():
        if kind in non_cash:
            s.add(kind, sign * non_cash[kind], label=label)
            operating.append((1, kind))
    seen: set[str] = set()
    for i, item in enumerate(args.get("working_capital", [])):
        kind = item["item"]
        if kind in seen:
            raise _domain(f"working_capital: {kind} appears more than once")
        seen.add(kind)
        name, sign = WORKING_CAPITAL[kind]
        opening = positive(item["opening"], f"working_capital[{i}].opening", allow_zero=True)
        closing = positive(item["closing"], f"working_capital[{i}].closing", allow_zero=True)
        change = closing - opening
        word = "Increase" if change > 0 else "Decrease"
        s.add(f"change_in_{kind}", -sign * change, label=f"{word} in {name}" if change else f"No change in {name}")
        operating.append((1, f"change_in_{kind}"))
    s.total("cash_generated_from_operations", operating)
    s.lines[-1]["label"] = s.label("cash_generated_from_operations", "Cash generated from operations")
    net_operating = [(1, "cash_generated_from_operations")]
    for kind, label in (("interest_paid", "Interest paid"), ("tax_paid", "Tax paid")):
        given = {kind: positive(args[kind], kind, allow_zero=True)} if kind in args else {}
        _merge(given, derived, (kind,))
        if kind in given:
            s.add(kind, -given[kind], label=label)
            net_operating.append((1, kind))
    _section(s, "net_cash_from_operating_activities", "Net cash from operating activities", net_operating)
    investing = _flows(args.get("investing", []), INVESTING, "investing")
    _merge(investing, derived, ("purchase_of_non_current_assets", "proceeds_from_disposals"))
    _section(s, "net_cash_from_investing_activities", "Net cash from investing activities",
             _lines(s, investing, INVESTING))  # fmt: skip
    financing = _flows(args.get("financing", []), FINANCING, "financing")
    _section(s, "net_cash_from_financing_activities", "Net cash from financing activities",
             _lines(s, financing, FINANCING))  # fmt: skip
    _section(s, "net_change_in_cash", "Net increase (decrease) in cash and cash equivalents",
             [(1, "net_cash_from_operating_activities"), (1, "net_cash_from_investing_activities"),
              (1, "net_cash_from_financing_activities")])  # fmt: skip
    s.add("opening_cash", amount(args["opening_cash"], "opening_cash"),
          label="Cash and cash equivalents at the start of the year")  # fmt: skip
    _section(s, "closing_cash", "Cash and cash equivalents at the end of the year",
             [(1, "opening_cash"), (1, "net_change_in_cash")])  # fmt: skip
    return s


def _lines(s: Statement, flows: dict[str, Fraction], kinds: dict[str, tuple[str, int]]) -> list[tuple[int, str]]:
    parts = []
    for kind, (label, sign) in kinds.items():
        if kind in flows:
            s.add(kind, sign * flows[kind], label=label)
            parts.append((1, kind))
    return parts


def _section(s: Statement, line_id: str, label: str, parts: list[tuple[int, str]]) -> None:
    s.total(line_id, parts)
    s.lines[-1]["label"] = s.label(line_id, label)


def acc_cash_flow(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    dp = places(args, context)
    working: list[dict[str, Any]] = []
    statement = build(args, working, context.check_id, dp)
    closing = statement.values["closing_cash"]
    if "closing_cash" in args:
        stated = amount(args["closing_cash"], "closing_cash")
        if stated != closing:
            unknown = {"status": "unknown", "reason": "does_not_reconcile", "computed_closing": money(closing, dp),
                     "stated_closing": money(stated, dp), "difference": money(stated - closing, dp)}  # fmt: skip
            return unknown, {"method": "exact", "confidence": "undecided", "working": working}
    value: dict[str, Any] = {
        "title": statement.label("cash_flow_statement", TITLE),
        "lines": statement_out(statement, dp),
        **{key: money(statement.values[key], dp) for key in (
            "net_cash_from_operating_activities", "net_cash_from_investing_activities",
            "net_cash_from_financing_activities", "net_change_in_cash", "closing_cash")},
    }  # fmt: skip
    if "student" in args:
        value["student"] = check(statement, args["student"]["lines"], dp)
    return value, {"method": "exact", "confidence": "proved", "working": working}
