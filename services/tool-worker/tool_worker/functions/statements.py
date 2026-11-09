"""Financial statements of a sole trader from a trial balance and typed adjustments (VER-23): `acc_statements`.

Each trial balance line carries its role (revenue, purchases, an expense, a non-current asset, ...); adjustments are
typed: accrued and prepaid expenses, accrued income and income in advance, depreciation (an amount, or a straight-line
or reducing-balance rate), irrecoverable debts, the allowance for irrecoverable debts (a new amount or a percentage of
the receivables), closing inventory and goods taken as drawings. The income statement and the statement of financial
position are built line by line; every figure carries a trace to the trial balance lines and adjustments it came
from, and every subtotal its formula over earlier lines.

A student's statement is checked line by line in the statement's order: the first line that differs is the first
divergent line, and each later subtotal is also recomputed from the student's own earlier figures, so a subtotal that
follows correctly from a wrong earlier figure is `own_figure` (the own figure rule) rather than wrong. Line names and
statement titles come from the pack's terms (`terms`: a line id or title to its names, the first shown); without them
one plain default name is used. Amounts are exact; the places shown come from the call, then the pack, then the
configured default. An upstream check (a depreciation schedule, an inventory valuation) is cited by its check id on
the adjustment and listed in the check record's `inputs_from` (VER-02)."""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from tool_worker.functions.money import Extras, amount, choice, money, normal, places, positive
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

TITLES = {"income_statement": "Income statement", "financial_position": "Statement of financial position"}
# One plain default name per fixed line; synonyms (balance sheet, sales, turnover, ...) come from the pack's terms.
LABELS = {
    "revenue": "Revenue", "sales_returns": "Sales returns", "net_revenue": "Net revenue",
    "opening_inventory": "Opening inventory", "purchases": "Purchases", "purchases_returns": "Purchases returns",
    "goods_for_own_use": "Goods for own use", "carriage_inwards": "Carriage inwards",
    "cost_of_goods_available": "Cost of goods available for sale", "closing_inventory": "Closing inventory",
    "cost_of_sales": "Cost of sales", "gross_profit": "Gross profit", "total_other_income": "Total other income",
    "total_expenses": "Total expenses", "profit_for_the_year": "Profit for the year",
    "total_non_current_assets": "Total non-current assets", "inventory": "Inventory",
    "trade_receivables": "Trade receivables", "allowance": "Allowance for irrecoverable debts",
    "net_trade_receivables": "Net trade receivables", "other_receivables": "Other receivables", "bank": "Bank",
    "cash": "Cash", "total_current_assets": "Total current assets", "total_assets": "Total assets",
    "capital": "Capital", "capital_profit": "Profit for the year", "drawings": "Drawings",
    "closing_capital": "Closing capital", "total_non_current_liabilities": "Total non-current liabilities",
    "trade_payables": "Trade payables", "other_payables": "Other payables", "bank_overdraft": "Bank overdraft",
    "total_current_liabilities": "Total current liabilities",
    "total_capital_and_liabilities": "Total capital and liabilities", "net_current_assets": "Net current assets",
    "net_assets": "Net assets", "irrecoverable_debts": "Irrecoverable debts",
    "allowance_increase": "Increase in allowance for irrecoverable debts",
    "allowance_decrease": "Decrease in allowance for irrecoverable debts",
}  # fmt: skip
SINGLE_ROLES = ("revenue", "sales_returns", "purchases", "purchases_returns", "carriage_inwards", "opening_inventory",
                "trade_receivables", "allowance_for_irrecoverable_debts", "other_receivables", "bank", "cash",
                "trade_payables", "other_payables", "bank_overdraft", "capital", "drawings")  # fmt: skip
CREDIT_ROLES = {"revenue", "purchases_returns", "other_income", "accumulated_depreciation", "trade_payables",
                "other_payables", "bank_overdraft", "non_current_liability", "capital",
                "allowance_for_irrecoverable_debts"}  # fmt: skip


@dataclass
class Figure:
    """A running figure with the trial balance lines and adjustments that make it up."""

    label: str
    value: Fraction = Fraction(0)
    tb: list[str] = field(default_factory=list)
    adjustments: list[int] = field(default_factory=list)

    def add(self, value: Fraction, tb: str | None = None, adjustment: int | None = None) -> None:
        self.value += value
        if tb is not None:
            self.tb.append(tb)
        if adjustment is not None:
            self.adjustments.append(adjustment)

    def trace(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.tb:
            out["trial_balance"] = self.tb
        if self.adjustments:
            out["adjustments"] = self.adjustments
        return out


@dataclass
class Books:
    single: dict[str, Figure]
    expenses: dict[str, Figure]
    income: dict[str, Figure]
    assets: dict[str, Figure]
    depreciation: dict[str, Figure]
    loans: dict[str, Figure]
    closing_inventory: Figure
    own_use: Figure
    prepayments: Figure
    accruals: Figure
    allowance_new: Figure | None = None


def _read_trial_balance(rows: list[dict[str, Any]]) -> Books:
    single = {role: Figure(LABELS.get(role, "Allowance for irrecoverable debts")) for role in SINGLE_ROLES}
    books = Books(single, {}, {}, {}, {}, {}, Figure("Closing inventory"), Figure("Goods for own use"),
                  Figure("Prepayments"), Figure("Accruals"))  # fmt: skip
    pending_depreciation = []
    total = Fraction(0)
    for i, row in enumerate(rows):
        debit = amount(row.get("debit", 0), f"trial_balance[{i}].debit")
        credit = amount(row.get("credit", 0), f"trial_balance[{i}].credit")
        if debit < 0 or credit < 0 or (debit and credit):
            raise WorkerError(Code.DOMAIN_ERROR, f"trial_balance[{i}]: give a debit or a credit amount, not both")
        total += debit - credit
        role, account = row["role"], row["account"].strip()
        value = credit - debit if role in CREDIT_ROLES else debit - credit
        if role in SINGLE_ROLES:
            books.single[role].add(value, account)
        elif role == "expense":
            books.expenses.setdefault(normal(account), Figure(account)).add(value, account)
        elif role == "other_income":
            books.income.setdefault(normal(account), Figure(account)).add(value, account)
        elif role == "non_current_asset":
            books.assets.setdefault(normal(account), Figure(account)).add(value, account)
        elif role == "non_current_liability":
            books.loans.setdefault(normal(account), Figure(account)).add(value, account)
        else:  # accumulated depreciation: matched to its asset once every asset is read
            pending_depreciation.append((row, value))
    if total:
        raise WorkerError(Code.DOMAIN_ERROR, f"the trial balance does not agree: debits exceed credits by {total}")
    for row, value in pending_depreciation:
        asset = _asset(books, row.get("asset") or _asset_named_in(books, row["account"]), "trial_balance")
        books.depreciation.setdefault(asset, Figure(row["account"].strip())).add(value, row["account"].strip())
    for asset in books.assets:
        books.depreciation.setdefault(asset, Figure(f"Accumulated depreciation: {books.assets[asset].label}"))
    return books


def _asset_named_in(books: Books, account: str) -> str | None:
    """The asset whose name appears in an accumulated depreciation account's name, or the only asset there is."""
    named = [key for key in books.assets if key and key in normal(account)]
    if len(named) == 1:
        return named[0]
    return next(iter(books.assets)) if len(books.assets) == 1 else None


def _asset(books: Books, label: str | None, where: str) -> str:
    key = normal(label) if label else None
    if key in books.assets:
        return key
    held = [k for k in books.assets if key and (key in k or k in key)]
    if len(held) == 1:
        return held[0]
    raise WorkerError(
        Code.DOMAIN_ERROR,
        f"{where}: name the non-current asset (one of: "
        f"{', '.join(f.label for f in books.assets.values()) or 'none in the trial balance'})",
    )


def _rate(raw: Any, field_name: str) -> Fraction:
    text = str(raw).strip().rstrip("%").strip()
    return positive(text, field_name) / 100


def _apply(books: Books, adjustments: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, str]]]:
    """Every adjustment applied in a fixed order (irrecoverable debts before the allowance on what remains)."""
    upstream: list[str] = []
    warnings: list[dict[str, str]] = []
    order = sorted(range(len(adjustments)), key=lambda i: adjustments[i]["kind"] == "allowance")
    closing_given = False
    for i in order:
        adj = adjustments[i]
        kind, where = adj["kind"], f"adjustments[{i}]"
        if adj.get("check_id"):
            upstream.append(adj["check_id"])
        if kind == "depreciation":
            asset = _asset(books, adj.get("account"), where)
            if "amount" in adj:
                charge = positive(adj["amount"], where, allow_zero=True)
            elif "method" in adj and "rate_percent" in adj:
                base = books.assets[asset].value
                if adj["method"] == "reducing_balance":
                    base -= books.depreciation[asset].value
                charge = base * _rate(adj["rate_percent"], where)
            else:
                raise WorkerError(Code.DOMAIN_ERROR, f"{where}: give the amount, or the method and rate_percent")
            label = f"Depreciation: {books.assets[asset].label}"
            books.expenses.setdefault(f"depreciation:{asset}", Figure(label)).add(charge, adjustment=i)
            books.depreciation[asset].add(charge, adjustment=i)
            continue
        if kind == "allowance":
            receivables = books.single["trade_receivables"].value
            if "amount" in adj:
                new = positive(adj["amount"], where, allow_zero=True)
            elif "percent" in adj:
                new = receivables * _rate(adj["percent"], where)
            else:
                raise WorkerError(Code.DOMAIN_ERROR, f"{where}: give the new allowance as amount or percent")
            books.allowance_new = Figure("Allowance for irrecoverable debts", new, adjustments=[i])
            continue
        value = positive(adj.get("amount", ""), where) if "amount" in adj else None
        if value is None:
            raise WorkerError(Code.DOMAIN_ERROR, f"{where}: {kind} needs an amount")
        if kind == "closing_inventory":
            books.closing_inventory.add(value, adjustment=i)
            closing_given = True
        elif kind == "goods_for_own_use":
            books.own_use.add(value, adjustment=i)
            books.single["drawings"].add(value, adjustment=i)
        elif kind == "irrecoverable_debt":
            label = adj.get("account", LABELS["irrecoverable_debts"])
            books.expenses.setdefault(normal(label), Figure(label)).add(value, adjustment=i)
            books.single["trade_receivables"].add(-value, adjustment=i)
        elif kind in ("accrued_expense", "prepaid_expense"):
            label = _named(adj, where)
            sign = 1 if kind == "accrued_expense" else -1
            books.expenses.setdefault(normal(label), Figure(label)).add(sign * value, adjustment=i)
            (books.accruals if sign > 0 else books.prepayments).add(value, adjustment=i)
        else:  # accrued_income, income_in_advance
            label = _named(adj, where)
            sign = 1 if kind == "accrued_income" else -1
            books.income.setdefault(normal(label), Figure(label)).add(sign * value, adjustment=i)
            (books.prepayments if sign > 0 else books.accruals).add(value, adjustment=i)
    if not closing_given:
        message = "no closing inventory was given; it is taken as nil"
        warnings.append({"code": "assumption_added", "message": message})
    return upstream, warnings


def _named(adj: dict[str, Any], where: str) -> str:
    if not adj.get("account"):
        raise WorkerError(Code.DOMAIN_ERROR, f"{where}: {adj['kind']} needs the account it adjusts")
    return str(adj["account"]).strip()


# --- building the statements ------------------------------------------------------------------------------------------


Formula = list[tuple[int, str]]  # (sign, line id) over earlier lines


class Statement:
    """Lines in order: id -> value, its trace, and for a subtotal its formula over earlier lines."""

    def __init__(self, terms: dict[str, list[str]]) -> None:
        self.lines: list[dict[str, Any]] = []
        self.values: dict[str, Fraction] = {}
        self.terms = terms

    def label(self, line_id: str, fallback: str) -> str:
        return self.terms.get(line_id, [fallback])[0]

    def add(
        self, line_id: str, figure: Figure | Fraction, op: str = "add", formula: Formula | None = None,
        label: str | None = None,
    ) -> None:  # fmt: skip
        value = figure.value if isinstance(figure, Figure) else figure
        if label is None:
            label = LABELS.get(line_id) or (figure.label if isinstance(figure, Figure) else line_id)
        line: dict[str, Any] = {"id": line_id, "label": self.label(line_id, label), "op": op, "value": value}
        if isinstance(figure, Figure) and figure.trace():
            line["trace"] = figure.trace()
        if formula:
            line["formula"] = formula
        self.lines.append(line)
        self.values[line_id] = value

    def total(self, line_id: str, formula: list[tuple[int, str]], op: str = "total") -> Fraction:
        value = sum((sign * self.values[part] for sign, part in formula), Fraction(0))
        self.add(line_id, value, op, formula)
        return value


def income_statement(books: Books, terms: dict[str, list[str]]) -> Statement:
    s = Statement(terms)
    single = books.single
    s.add("revenue", single["revenue"])
    revenue_id = "revenue"
    if single["sales_returns"].value:
        s.add("sales_returns", single["sales_returns"], "less")
        s.total("net_revenue", [(1, "revenue"), (-1, "sales_returns")])
        revenue_id = "net_revenue"
    s.add("opening_inventory", single["opening_inventory"])
    s.add("purchases", single["purchases"])
    available = [(1, "opening_inventory"), (1, "purchases")]
    for line_id, figure, op in (("purchases_returns", single["purchases_returns"], "less"),
                                ("goods_for_own_use", books.own_use, "less"),
                                ("carriage_inwards", single["carriage_inwards"], "add")):  # fmt: skip
        if figure.value:
            s.add(line_id, figure, op)
            available.append((-1 if op == "less" else 1, line_id))
    s.total("cost_of_goods_available", available, "subtotal")
    s.add("closing_inventory", books.closing_inventory, "less")
    s.total("cost_of_sales", [(1, "cost_of_goods_available"), (-1, "closing_inventory")])
    s.total("gross_profit", [(1, revenue_id), (-1, "cost_of_sales")])
    profit = [(1, "gross_profit")]
    income = dict(books.income)
    expenses = dict(books.expenses)
    if books.allowance_new is not None:
        change = books.allowance_new.value - single["allowance_for_irrecoverable_debts"].value
        figure = Figure("", abs(change), single["allowance_for_irrecoverable_debts"].tb,
                        books.allowance_new.adjustments)  # fmt: skip
        if change > 0:
            figure.label = LABELS["allowance_increase"]
            expenses["allowance_increase"] = figure
        elif change < 0:
            figure.label = LABELS["allowance_decrease"]
            income["allowance_decrease"] = figure
    for key, figure in income.items():
        line_id = key if key.startswith("allowance") else f"income:{key}"
        s.add(line_id, figure)
        profit.append((1, line_id))
    parts = []
    for key, figure in expenses.items():
        line_id = key if key.startswith(("allowance", "depreciation:")) else f"expense:{key}"
        s.add(line_id, figure, "less")
        parts.append((1, line_id))
    if parts:
        s.total("total_expenses", parts, "subtotal")
        profit.append((-1, "total_expenses"))
    s.total("profit_for_the_year", profit)
    return s


def financial_position(books: Books, profit: Fraction, layout: str, terms: dict[str, list[str]]) -> Statement:
    s = Statement(terms)
    single = books.single
    nca = []
    for key, cost in books.assets.items():
        depreciation = books.depreciation[key]
        s.add(f"asset:{key}:cost", cost, label=cost.label)
        s.add(f"asset:{key}:depreciation", depreciation, "less", label=f"Accumulated depreciation: {cost.label}")
        s.total(f"asset:{key}:net_book_value", [(1, f"asset:{key}:cost"), (-1, f"asset:{key}:depreciation")],
                "subtotal")  # fmt: skip
        s.lines[-1]["label"] = s.label(f"asset:{key}:net_book_value", f"Net book value: {cost.label}")
        nca.append((1, f"asset:{key}:net_book_value"))
    s.total("total_non_current_assets", nca)
    s.add("inventory", books.closing_inventory)
    current = [(1, "inventory")]
    if single["trade_receivables"].value or books.allowance_new or single["allowance_for_irrecoverable_debts"].value:
        s.add("trade_receivables", single["trade_receivables"])
        allowance = books.allowance_new or single["allowance_for_irrecoverable_debts"]
        if allowance.value:
            s.add("allowance", allowance, "less")
            s.total("net_trade_receivables", [(1, "trade_receivables"), (-1, "allowance")], "subtotal")
            current.append((1, "net_trade_receivables"))
        else:
            current.append((1, "trade_receivables"))
    other = Figure("Other receivables", single["other_receivables"].value + books.prepayments.value,
                   single["other_receivables"].tb, books.prepayments.adjustments)  # fmt: skip
    overdraft = Figure("Bank overdraft", single["bank_overdraft"].value, list(single["bank_overdraft"].tb))
    bank = single["bank"]
    if bank.value < 0:  # a bank account with a credit balance is an overdraft
        overdraft.add(-bank.value)
        overdraft.tb += bank.tb
        bank = Figure("Bank")
    for line_id, figure in (("other_receivables", other), ("bank", bank), ("cash", single["cash"])):
        if figure.value:
            s.add(line_id, figure)
            current.append((1, line_id))
    s.total("total_current_assets", current)
    liabilities = []
    payables = Figure("Other payables", single["other_payables"].value + books.accruals.value,
                      single["other_payables"].tb, books.accruals.adjustments)  # fmt: skip
    current_liabilities = []

    def current_section() -> None:
        for line_id, figure in (("trade_payables", single["trade_payables"]), ("other_payables", payables),
                                ("bank_overdraft", overdraft)):  # fmt: skip
            if figure.value:
                s.add(line_id, figure)
                current_liabilities.append((1, line_id))
        s.total("total_current_liabilities", current_liabilities)

    def loans_section() -> None:
        for key, loan in books.loans.items():
            s.add(f"loan:{key}", loan)
            liabilities.append((1, f"loan:{key}"))
        if liabilities:
            s.total("total_non_current_liabilities", liabilities)

    if layout == "net_assets":
        current_section()
        s.total("net_current_assets", [(1, "total_current_assets"), (-1, "total_current_liabilities")])
        loans_section()
        net = [(1, "total_non_current_assets"), (1, "net_current_assets")]
        s.total("net_assets", net + ([(-1, "total_non_current_liabilities")] if liabilities else []))
    else:
        s.total("total_assets", [(1, "total_non_current_assets"), (1, "total_current_assets")])
    s.add("capital", single["capital"])
    s.add("capital_profit", profit, "add" if profit >= 0 else "less")  # a loss is a negative figure
    s.lines[-1]["trace"] = {"income_statement": ["profit_for_the_year"]}
    s.add("drawings", single["drawings"], "less")
    s.total("closing_capital", [(1, "capital"), (1, "capital_profit"), (-1, "drawings")])
    if layout != "net_assets":
        loans_section()
        current_section()
        parts = [(1, "closing_capital"), (1, "total_current_liabilities")]
        s.total(
            "total_capital_and_liabilities", parts + ([(1, "total_non_current_liabilities")] if liabilities else [])
        )
    return s


# --- checking a student's statement -----------------------------------------------------------------------------------


def _names(line: dict[str, Any], terms: dict[str, list[str]]) -> set[str]:
    names = {normal(line["label"]), normal(LABELS.get(line["id"], line["label"]))}
    return names | {normal(n) for n in terms.get(line["id"], [])}


def check(statement: Statement, student: list[dict[str, Any]], dp: int) -> dict[str, Any]:
    """The student's lines matched to the statement's by id or by name; verdicts in the statement's order."""
    by_id = {line["id"]: line for line in statement.lines}
    written: dict[str, Fraction] = {}
    unrecognised = []
    for i, line in enumerate(student):
        value = amount(line["amount"], f"student.lines[{i}]")
        target: str | None = line.get("line")
        if target is None and line.get("label"):
            wanted = normal(line["label"])
            target = next((ln["id"] for ln in statement.lines if ln["id"] not in written
                           and wanted in _names(ln, statement.terms)), None)  # fmt: skip
        if target is not None and target in by_id and target not in written:
            written[target] = value
        else:
            unrecognised.append({"index": i, "label": line.get("label") or line.get("line"),
                                 "amount": money(value, dp)})  # fmt: skip
    marks = []
    for line in statement.lines:
        expected = line["value"]
        got = written.get(line["id"])
        mark: dict[str, Any] = {"id": line["id"], "label": line["label"], "expected": money(expected, dp)}
        if got is None:
            mark["verdict"] = "missing"
        else:
            mark["student"] = money(got, dp)
            if got == expected:
                mark["verdict"] = "ok"
            elif "formula" in line and got == sum((sign * written.get(part, statement.values[part])
                                                   for sign, part in line["formula"]), Fraction(0)):  # fmt: skip
                mark["verdict"] = "own_figure"
            else:
                mark["verdict"] = "wrong"
        marks.append(mark)
    first = next((m for m in marks if m["verdict"] not in ("ok", "own_figure")), None)
    if first is None:
        first = next((m for m in marks if m["verdict"] != "ok"), None)
    return {"correct": first is None and not unrecognised, "first_divergent": first, "lines": marks,
            "unrecognised": unrecognised}  # fmt: skip


def statement_out(statement: Statement, dp: int) -> list[dict[str, Any]]:
    out = []
    for line in statement.lines:
        row = {k: v for k, v in line.items() if k != "value"}
        row["amount"] = money(line["value"], dp)
        if "formula" in row:
            row["formula"] = [("+" if sign > 0 else "-") + part for sign, part in row["formula"]]
        out.append(row)
    return out


def acc_statements(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """The income statement and statement of financial position, and a student's statement checked (VER-23)."""
    dp = places(args, context)
    terms = {key: list(names) for key, names in args.get("terms", {}).items()}
    if "trial_balance" not in args:
        raise WorkerError(Code.DOMAIN_ERROR, "a sole trader's statements need trial_balance")
    books = _read_trial_balance(args["trial_balance"])
    upstream, warnings = _apply(books, args.get("adjustments", []))
    layouts = ("capital_and_liabilities", "net_assets")
    layout, source, layout_warnings = choice(args, context, "layout", "acc.sofp_layout", layouts, layouts[0])
    income = income_statement(books, terms)
    profit = income.values["profit_for_the_year"]
    position = financial_position(books, profit, layout, terms)
    if layout == "net_assets":
        balanced = position.values["net_assets"] == position.values["closing_capital"]
    else:
        balanced = position.values["total_assets"] == position.values["total_capital_and_liabilities"]
    value: dict[str, Any] = {
        "income_statement": {"title": terms.get("income_statement", [TITLES["income_statement"]])[0],
                             "lines": statement_out(income, dp)},
        "financial_position": {"title": terms.get("financial_position", [TITLES["financial_position"]])[0],
                               "layout": {"value": layout, "source": source}, "lines": statement_out(position, dp)},
        "profit_for_the_year": money(profit, dp),
        "result": "profit" if profit > 0 else ("loss" if profit < 0 else "break_even"),
        "balanced": balanced,
    }  # fmt: skip
    if args.get("period"):
        value["period"] = args["period"]
    if "student" in args:
        target = income if args["student"]["statement"] == "income_statement" else position
        value["student"] = {"statement": args["student"]["statement"], **check(target, args["student"]["lines"], dp)}
    extras: Extras = {"warnings": warnings + layout_warnings, "method": "exact", "confidence": "proved"}
    if upstream:
        extras["inputs_from"] = sorted(set(upstream))
    return value, extras
