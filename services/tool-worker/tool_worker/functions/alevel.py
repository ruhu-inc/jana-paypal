"""The A level forms of `acc_statements` (VER-34): partnership appropriation and current accounts, a limited company's
income statement tail and statement of changes in equity, manufacturing accounts and incomplete records.

Each form is built line by line on the same `Statement` as the sole trader's statements, so every subtotal carries its
formula over earlier lines and a student's statement is checked with the same own figure rule (VER-23).

- Partnership: interest on drawings is added to the profit; salaries and interest on capital (a rate on each
  partner's capital, or a stated amount) are appropriated first, and the residual profit or loss is shared in the
  profit-sharing ratio, exactly. Current accounts run from the opening balance (credit positive) through the
  appropriations and drawings to the closing balance and its side.
- Limited company: profit from operations less finance costs is the profit before tax, less tax the profit for the
  year. Changes in equity run column by column (share capital, share premium, revaluation reserve, general reserve,
  retained earnings): issues at a premium split nominal value from premium; a bonus issue is capitalised from the
  reserve it names and may not exceed it; the profit, dividends paid and transfers move retained earnings.
- Manufacturing: raw materials consumed, prime cost, factory overheads, the work in progress adjustment and the cost
  of production, with a factory profit when goods are transferred at a mark-up, and the cost of a unit.
- Incomplete records: profit from the change in net assets (statement of affairs) adjusted for drawings and capital
  introduced; credit sales and purchases from the control account totals; and with a gross margin or mark-up, the
  missing revenue or cost of sales and any inventory lost."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from tool_worker.functions import statements
from tool_worker.functions.money import Extras, amount, money, places, positive
from tool_worker.functions.statements import Statement, check, statement_out
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Built = tuple[dict[str, Statement], dict[str, Any], list[dict[str, str]]]
COLUMNS = {"share_capital": "Share capital", "share_premium": "Share premium",
           "revaluation_reserve": "Revaluation reserve", "general_reserve": "General reserve",
           "retained_earnings": "Retained earnings"}  # fmt: skip


def _domain(message: str) -> WorkerError:
    return WorkerError(Code.DOMAIN_ERROR, message)


def _money_in(data: dict[str, Any], key: str, where: str) -> Fraction:
    return positive(data.get(key, 0), f"{where}.{key}", allow_zero=True)


def _total(s: Statement, line_id: str, label: str, parts: list[tuple[int, str]]) -> Fraction:
    value = s.total(line_id, parts)
    s.lines[-1]["label"] = s.label(line_id, label)
    return value


# --- partnership ------------------------------------------------------------------------------------------------------


def partnership(data: dict[str, Any], terms: dict[str, list[str]], dp: int) -> Built:
    partners = data["partners"]
    ids = [p["id"] for p in partners]
    if len(set(ids)) != len(ids):
        raise _domain("partnership: each partner id appears once")
    rate = Fraction(amount(data.get("interest_on_capital_percent", 0), "interest_on_capital_percent"))
    shares = sum(int(p["profit_share"]) for p in partners)
    figures: dict[str, dict[str, Fraction]] = {}
    for i, p in enumerate(partners):
        where = f"partnership.partners[{i}]"
        capital = _money_in(p, "capital", where)
        interest = _money_in(p, "interest_on_capital", where) if "interest_on_capital" in p else capital * rate / 100
        figures[p["id"]] = {
            "salary": _money_in(p, "salary", where),
            "interest_on_capital": interest,
            "drawings": _money_in(p, "drawings", where),
            "interest_on_drawings": _money_in(p, "interest_on_drawings", where),
            "opening": amount(p.get("current_account_opening", 0), f"{where}.current_account_opening"),
        }
    a = Statement(terms)
    a.add("profit_for_the_year", amount(data["profit_for_the_year"], "partnership.profit_for_the_year"),
          label="Profit for the year")  # fmt: skip
    parts = [(1, "profit_for_the_year")]
    names = {p["id"]: p.get("name", p["id"]) for p in partners}
    for key, label, sign in (("interest_on_drawings", "Interest on drawings", 1), ("salary", "Salary", -1),
                             ("interest_on_capital", "Interest on capital", -1)):  # fmt: skip
        for pid in ids:
            if figures[pid][key]:
                a.add(f"{pid}:{key}", figures[pid][key], "add" if sign > 0 else "less", label=f"{label}: {names[pid]}")
                parts.append((sign, f"{pid}:{key}"))
    residual = _total(a, "residual_profit", "Residual profit to share", parts)
    for p in partners:
        a.add(f"{p['id']}:share", residual * int(p["profit_share"]) / shares,
              label=f"Share of {'profit' if residual >= 0 else 'loss'}: {names[p['id']]}")  # fmt: skip
    c = Statement(terms)
    balances = {}
    for pid in ids:
        f = figures[pid]
        c.add(f"{pid}:opening", f["opening"], label=f"{names[pid]}: balance b/d")
        cparts = [(1, f"{pid}:opening")]
        for key, label in (("salary", "salary"), ("interest_on_capital", "interest on capital")):
            if f[key]:
                c.add(f"{pid}:{key}", f[key], label=f"{names[pid]}: {label}")
                cparts.append((1, f"{pid}:{key}"))
        c.add(f"{pid}:share", a.values[f"{pid}:share"], label=f"{names[pid]}: share of profit")
        cparts.append((1, f"{pid}:share"))
        for key, label in (("drawings", "drawings"), ("interest_on_drawings", "interest on drawings")):
            if f[key]:
                c.add(f"{pid}:{key}", f[key], "less", label=f"{names[pid]}: {label}")
                cparts.append((-1, f"{pid}:{key}"))
        closing = _total(c, f"{pid}:closing", f"{names[pid]}: balance c/d", cparts)
        balances[pid] = {"amount": money(abs(closing), dp), "side": "credit" if closing >= 0 else "debit"}
    summary = {"residual_profit": money(residual, dp),
               "shares": {pid: money(a.values[f"{pid}:share"], dp) for pid in ids},
               "current_accounts": balances}  # fmt: skip
    warnings = []
    if any(b["side"] == "debit" for b in balances.values()):
        warnings.append({"code": "implausible", "message": "a partner's current account has a debit "
                         "balance: the partner has drawn more than their appropriations"})  # fmt: skip
    return {"appropriation_account": a, "current_accounts": c}, summary, warnings


# --- limited company --------------------------------------------------------------------------------------------------


def limited_company(data: dict[str, Any], terms: dict[str, list[str]], dp: int) -> Built:
    built: dict[str, Statement] = {}
    if "profit_from_operations" in data:
        income = Statement(terms)
        income.add("profit_from_operations", amount(data["profit_from_operations"], "company.profit_from_operations"),
                   label="Profit from operations")  # fmt: skip
        income.add("finance_costs", _money_in(data, "finance_costs", "company"), "less", label="Finance costs")
        _total(income, "profit_before_tax", "Profit before tax", [(1, "profit_from_operations"), (-1, "finance_costs")])
        income.add("tax", _money_in(data, "tax", "company"), "less", label="Tax")
        profit = _total(income, "profit_for_the_year", "Profit for the year", [(1, "profit_before_tax"), (-1, "tax")])
        built["income_statement"] = income
        if (
            "profit_for_the_year" in data
            and amount(data["profit_for_the_year"], "company.profit_for_the_year") != profit
        ):
            raise _domain("company: profit_for_the_year does not follow from the profit from operations, finance "
                          "costs and tax")  # fmt: skip
    elif "profit_for_the_year" in data:
        profit = amount(data["profit_for_the_year"], "company.profit_for_the_year")
    else:
        raise _domain("company: give profit_from_operations (with finance_costs and tax) or profit_for_the_year")
    opening = data.get("equity", {})
    moves: dict[str, list[tuple[str, str, Fraction]]] = {col: [] for col in COLUMNS}
    issued_shares = 0
    for i, issue in enumerate(data.get("share_issues", [])):
        where = f"company.share_issues[{i}]"
        shares, nominal = int(issue["shares"]), positive(issue["nominal_value"], f"{where}.nominal_value")
        capital = shares * nominal
        issued_shares += shares
        if issue["kind"] == "bonus":
            source = issue.get("from_reserve", "share_premium")
            moves["share_capital"].append((f"bonus_{i + 1}", "Bonus issue", capital))
            moves[source].append((f"bonus_{i + 1}", "Bonus issue", -capital))
        else:
            price = positive(issue["price"], f"{where}.price")
            if price < nominal:
                raise _domain(f"{where}: shares may not be issued at a discount to nominal value")
            moves["share_capital"].append((f"issue_{i + 1}", "Issue of shares", capital))
            if price > nominal:
                moves["share_premium"].append((f"issue_{i + 1}", "Issue of shares", shares * (price - nominal)))
    if data.get("revaluation_surplus"):
        moves["revaluation_reserve"].append(("revaluation", "Revaluation surplus",
                                             _money_in(data, "revaluation_surplus", "company")))  # fmt: skip
    moves["retained_earnings"].append(("profit", "Profit for the year", profit))
    if data.get("dividends_paid"):
        moves["retained_earnings"].append(
            ("dividends", "Dividends paid", -_money_in(data, "dividends_paid", "company"))
        )
    transfer = _money_in(data, "transfer_to_general_reserve", "company")
    if transfer:
        moves["retained_earnings"].append(("transfer", "Transfer to general reserve", -transfer))
        moves["general_reserve"].append(("transfer", "Transfer from retained earnings", transfer))
    e = Statement(terms)
    closing: dict[str, Fraction] = {}
    warnings = []
    for col, title in COLUMNS.items():
        start = amount(opening.get(col, 0), f"company.equity.{col}")
        if not start and not moves[col]:
            continue
        e.add(f"{col}:opening", start, label=f"{title}: at the start of the year")
        parts = [(1, f"{col}:opening")]
        running = start
        for key, label, change in moves[col]:
            e.add(f"{col}:{key}", change, label=f"{title}: {label.lower()}")
            parts.append((1, f"{col}:{key}"))
            running += change
            if key.startswith("bonus") and change < 0 and running < 0:
                raise _domain(f"company: the bonus issue exceeds the {title.lower()}")
        closing[col] = _total(e, f"{col}:closing", f"{title}: at the end of the year", parts)
        if col == "retained_earnings" and closing[col] < 0 and data.get("dividends_paid"):
            warnings.append({"code": "implausible", "message": "dividends paid exceed the retained "
                             "earnings available for distribution"})  # fmt: skip
    _total(e, "total_equity", "Total equity", [(1, f"{col}:closing") for col in closing])
    built["changes_in_equity"] = e
    summary = {"profit_for_the_year": money(profit, dp), "total_equity": money(e.values["total_equity"], dp),
               "closing": {col: money(v, dp) for col, v in closing.items()}}  # fmt: skip
    return built, summary, warnings


# --- manufacturing ----------------------------------------------------------------------------------------------------


def manufacturing(data: dict[str, Any], terms: dict[str, list[str]], dp: int) -> Built:
    raw, where = data["raw_materials"], "manufacturing.raw_materials"
    s = Statement(terms)
    s.add(
        "opening_raw_materials", _money_in(raw, "opening_inventory", where), label="Opening inventory of raw materials"
    )
    s.add("raw_material_purchases", _money_in(raw, "purchases", where), label="Purchases of raw materials")
    parts = [(1, "opening_raw_materials"), (1, "raw_material_purchases")]
    for key, line_id, label, sign in (("carriage_inwards", "raw_carriage_inwards", "Carriage inwards", 1),
                                      ("returns_outwards", "raw_returns_outwards", "Purchases returns", -1),
                                      ("closing_inventory", "closing_raw_materials",
                                       "Closing inventory of raw materials", -1)):  # fmt: skip
        if key in raw or key == "closing_inventory":
            s.add(line_id, _money_in(raw, key, where), "add" if sign > 0 else "less", label=label)
            parts.append((sign, line_id))
    _total(s, "raw_materials_consumed", "Cost of raw materials consumed", parts)
    s.add("direct_labour", _money_in(data, "direct_labour", "manufacturing"), label="Direct labour")
    parts = [(1, "raw_materials_consumed"), (1, "direct_labour")]
    if "direct_expenses" in data:
        s.add("direct_expenses", _money_in(data, "direct_expenses", "manufacturing"), label="Direct expenses")
        parts.append((1, "direct_expenses"))
    _total(s, "prime_cost", "Prime cost", parts)
    overheads = []
    for i, item in enumerate(data.get("factory_overheads", [])):
        s.add(f"overhead_{i + 1}", positive(item["amount"], f"manufacturing.factory_overheads[{i}].amount",
                                            allow_zero=True), label=item["name"])  # fmt: skip
        overheads.append((1, f"overhead_{i + 1}"))
    _total(s, "factory_overheads", "Total factory overheads", overheads)
    _total(s, "total_production_cost", "Total production cost", [(1, "prime_cost"), (1, "factory_overheads")])
    parts = [(1, "total_production_cost")]
    wip = data.get("work_in_progress")
    if wip is not None:
        s.add("opening_wip", _money_in(wip, "opening", "manufacturing.work_in_progress"),
              label="Opening work in progress")  # fmt: skip
        s.add("closing_wip", _money_in(wip, "closing", "manufacturing.work_in_progress"), "less",
              label="Closing work in progress")  # fmt: skip
        parts += [(1, "opening_wip"), (-1, "closing_wip")]
    cost = _total(s, "cost_of_production", "Cost of production", parts)
    summary: dict[str, Any] = {"cost_of_production": money(cost, dp), "prime_cost": money(s.values["prime_cost"], dp)}
    if "factory_profit_percent" in data:
        markup = amount(data["factory_profit_percent"], "factory_profit_percent")
        s.add("factory_profit", cost * markup / 100, label="Factory profit")
        transfer = _total(s, "transfer_price", "Production transferred at factory cost plus profit",
                          [(1, "cost_of_production"), (1, "factory_profit")])  # fmt: skip
        summary["transfer_price"] = money(transfer, dp)
    if data.get("units_produced"):
        units = int(data["units_produced"])
        summary["unit_cost"] = {"exact": str(cost / units), "value": money(cost / units, dp)}
    return {"manufacturing_account": s}, summary, []


# --- incomplete records -----------------------------------------------------------------------------------------------


def _ledger_total(s: Statement, data: dict[str, Any], where: str, kind: str) -> Fraction:
    """Credit sales (or purchases) as the missing figure of the control account."""
    others: tuple[str, ...]
    if kind == "sales":
        opening, closing, cash = "opening_receivables", "closing_receivables", "receipts"
        others = ("discounts_allowed", "irrecoverable_debts", "returns_inwards", "contra")
        result, label = "credit_sales", "Credit sales"
    else:
        opening, closing, cash = "opening_payables", "closing_payables", "payments"
        others = ("discounts_received", "returns_outwards", "contra")
        result, label = "credit_purchases", "Credit purchases"
    s.add(f"{kind}:{cash}", _money_in(data, cash, where), label=cash.capitalize())
    parts = [(1, f"{kind}:{cash}")]
    for key in others:
        if key in data:
            s.add(f"{kind}:{key}", _money_in(data, key, where), label=key.replace("_", " ").capitalize())
            parts.append((1, f"{kind}:{key}"))
    s.add(f"{kind}:{closing}", _money_in(data, closing, where), label=closing.replace("_", " ").capitalize())
    s.add(f"{kind}:{opening}", _money_in(data, opening, where), "less", label=opening.replace("_", " ").capitalize())
    parts += [(1, f"{kind}:{closing}"), (-1, f"{kind}:{opening}")]
    credit = _total(s, result, label, parts)
    if credit < 0:
        raise _domain(f"{where}: the control account totals imply negative {label.lower()}")
    cash_key = "cash_sales" if kind == "sales" else "cash_purchases"
    if cash_key in data:
        s.add(cash_key, _money_in(data, cash_key, where), label=cash_key.replace("_", " ").capitalize())
        return _total(s, f"total_{kind}", f"Total {kind}", [(1, result), (1, cash_key)])
    return credit


def incomplete_records(data: dict[str, Any], terms: dict[str, list[str]], dp: int) -> Built:
    built: dict[str, Statement] = {}
    summary: dict[str, Any] = {}
    if "affairs" in data:
        affairs, s = data["affairs"], Statement(terms)
        for when in ("opening", "closing"):
            where = f"incomplete_records.affairs.{when}"
            s.add(f"{when}_assets", _money_in(affairs[when], "assets", where), label=f"{when.capitalize()} assets")
            s.add(f"{when}_liabilities", _money_in(affairs[when], "liabilities", where), "less",
                  label=f"{when.capitalize()} liabilities")  # fmt: skip
            _total(s, f"{when}_capital", f"{when.capitalize()} capital (net assets)",
                   [(1, f"{when}_assets"), (-1, f"{when}_liabilities")])  # fmt: skip
        _total(s, "change_in_capital", "Increase in capital", [(1, "closing_capital"), (-1, "opening_capital")])
        s.add("drawings", _money_in(affairs, "drawings", "incomplete_records.affairs"), "add", label="Add drawings")
        s.add("capital_introduced", _money_in(affairs, "capital_introduced", "incomplete_records.affairs"), "less",
              label="Less capital introduced")  # fmt: skip
        profit = _total(s, "profit_for_the_year", "Profit for the year",
                        [(1, "change_in_capital"), (1, "drawings"), (-1, "capital_introduced")])  # fmt: skip
        built["statement_of_affairs"] = s
        summary["profit_for_the_year"] = money(profit, dp)
    totals: dict[str, Fraction] = {}
    if "sales" in data or "purchases" in data:
        s = Statement(terms)
        for kind in ("sales", "purchases"):
            if kind in data:
                totals[kind] = _ledger_total(s, data[kind], f"incomplete_records.{kind}", kind)
                summary[f"total_{kind}"] = money(totals[kind], dp)
        built["control_totals"] = s
    if "margin" in data:
        built["trading"], found = _margin(data["margin"], totals, terms, dp)
        summary.update(found)
    if not built:
        raise _domain("incomplete_records: give affairs, sales, purchases or margin")
    return built, summary, []


def _margin(data: dict[str, Any], totals: dict[str, Fraction], terms: dict[str, list[str]], dp: int) -> tuple[
        Statement, dict[str, Any]]:  # fmt: skip
    """The missing revenue, cost of sales or inventory from a gross margin (on revenue) or mark-up (on cost)."""
    where = "incomplete_records.margin"
    rate = amount(data["percent"], f"{where}.percent") / 100
    on_cost = data["basis"] == "mark_up"
    if not on_cost and rate >= 1:
        raise _domain(f"{where}: a gross margin is under 100% of revenue")
    cost_share = 1 / (1 + rate) if on_cost else 1 - rate  # cost of sales as a share of revenue
    revenue = amount(data["revenue"], f"{where}.revenue") if "revenue" in data else totals.get("sales")
    purchases = amount(data["purchases"], f"{where}.purchases") if "purchases" in data else totals.get("purchases")
    opening = _money_in(data, "opening_inventory", where)
    s = Statement(terms)
    s.add("opening_inventory", opening, label="Opening inventory")
    if purchases is None:
        raise _domain(f"{where}: give purchases (or the purchases control totals)")
    s.add("purchases", purchases, label="Purchases")
    _total(s, "goods_available", "Cost of goods available for sale", [(1, "opening_inventory"), (1, "purchases")])
    found: dict[str, Any] = {}
    if revenue is not None:
        cost_of_sales = revenue * cost_share
        s.add("revenue", revenue, label="Revenue")
        s.add("gross_profit", revenue - cost_of_sales, "less", label="Gross profit at the stated margin")
        _total(s, "cost_of_sales", "Cost of sales", [(1, "revenue"), (-1, "gross_profit")])
        expected = _total(s, "expected_closing_inventory", "Closing inventory that should remain",
                          [(1, "goods_available"), (-1, "cost_of_sales")])  # fmt: skip
        found.update(cost_of_sales=money(cost_of_sales, dp), expected_closing_inventory=money(expected, dp))
        if expected < 0:
            raise _domain(f"{where}: the cost of sales at this margin exceeds the goods available")
        if "closing_inventory" in data:
            s.add("actual_closing_inventory", _money_in(data, "closing_inventory", where), "less",
                  label="Closing inventory counted")  # fmt: skip
            lost = _total(s, "inventory_lost", "Inventory lost",
                          [(1, "expected_closing_inventory"), (-1, "actual_closing_inventory")])  # fmt: skip
            if lost < 0:
                raise _domain(f"{where}: the inventory counted exceeds what should remain at this margin")
            found["inventory_lost"] = money(lost, dp)
    elif "closing_inventory" in data:
        s.add("closing_inventory", _money_in(data, "closing_inventory", where), "less", label="Closing inventory")
        cost_of_sales = _total(s, "cost_of_sales", "Cost of sales", [(1, "goods_available"), (-1, "closing_inventory")])
        s.add("gross_profit", cost_of_sales * (1 / cost_share - 1), label="Gross profit at the stated margin")
        found["revenue"] = money(_total(s, "revenue", "Revenue", [(1, "cost_of_sales"), (1, "gross_profit")]), dp)
        found["cost_of_sales"] = money(cost_of_sales, dp)
    else:
        raise _domain(f"{where}: give revenue (or the sales control totals) or the closing inventory")
    return s, found


FORMS = {"partnership": partnership, "limited_company": limited_company, "manufacturing": manufacturing,
         "incomplete_records": incomplete_records}  # fmt: skip
FIELDS = {"partnership": "partnership", "limited_company": "company", "manufacturing": "manufacturing",
          "incomplete_records": "incomplete_records"}  # fmt: skip
TITLES = {"appropriation_account": "Appropriation account", "current_accounts": "Partners' current accounts",
          "income_statement": "Income statement", "changes_in_equity": "Statement of changes in equity",
          "manufacturing_account": "Manufacturing account", "statement_of_affairs": "Statement of affairs",
          "control_totals": "Control account totals", "trading": "Trading figures from the margin"}  # fmt: skip


def acc_statements(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """A sole trader's statements (VER-23), or an A level form (VER-34) when `form` names one."""
    form = args.get("form", "sole_trader")
    if form == "sole_trader":
        return statements.acc_statements(args, context)
    dp = places(args, context)
    terms = {key: list(names) for key, names in args.get("terms", {}).items()}
    if FIELDS[form] not in args:
        raise _domain(f"form {form} needs {FIELDS[form]}")
    built, summary, warnings = FORMS[form](args[FIELDS[form]], terms, dp)
    value: dict[str, Any] = {"form": form, **summary,
                             "statements": {name: {"title": terms.get(name, [TITLES[name]])[0],
                                                   "lines": statement_out(statement, dp)}
                                            for name, statement in built.items()}}  # fmt: skip
    if args.get("period"):
        value["period"] = args["period"]
    if "student" in args:
        name = args["student"]["statement"]
        if name not in built:
            raise _domain(f"student.statement: {name} is not a statement of form {form}")
        value["student"] = {"statement": name, **check(built[name], args["student"]["lines"], dp)}
    return value, {"warnings": warnings, "method": "exact", "confidence": "proved"}
