"""Control accounts (VER-34), the `control` task of `acc_double_entry`.

A sales ledger control account (receivables) or a purchases ledger control account (payables) is built from the
period's totals, each item on its fixed side: credit sales, dishonoured cheques, refunds and interest charged on the
debit of the sales ledger control account, receipts, discounts allowed, returns, irrecoverable debts and contras on
its credit; the mirror for the purchases ledger control account. Opening balances on both sides are allowed (a credit
balance on a sales ledger account is a customer who overpaid). The balance c/d is the difference of the sides. When
the total of the individual ledger balances is given, the control account is reconciled with it and any difference
reported, never absorbed. A student's balance (and side) is compared with the computed one."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from tool_worker.functions.money import money, positive
from tool_worker.protocol import Code, WorkerError

# item -> (label, side) for each control account
ITEMS = {
    "sales_ledger": {
        "credit_sales": ("Credit sales", "debit"), "dishonoured_cheques": ("Dishonoured cheques", "debit"),
        "refunds": ("Refunds to customers", "debit"), "interest_charged": ("Interest charged", "debit"),
        "receipts": ("Receipts from customers", "credit"), "discounts_allowed": ("Discounts allowed", "credit"),
        "returns": ("Sales returns", "credit"), "irrecoverable_debts": ("Irrecoverable debts", "credit"),
        "contra": ("Contra: purchases ledger", "credit"),
    },
    "purchases_ledger": {
        "credit_purchases": ("Credit purchases", "credit"), "refunds": ("Refunds from suppliers", "credit"),
        "interest_charged": ("Interest charged by suppliers", "credit"),
        "payments": ("Payments to suppliers", "debit"), "discounts_received": ("Discounts received", "debit"),
        "returns": ("Purchases returns", "debit"), "contra": ("Contra: sales ledger", "debit"),
    },
}  # fmt: skip
NORMAL = {"sales_ledger": "debit", "purchases_ledger": "credit"}
TITLES = {"sales_ledger": "Sales ledger control account", "purchases_ledger": "Purchases ledger control account"}


def control_account(data: dict[str, Any], dp: int) -> dict[str, Any]:
    kind = data["kind"]
    sides: dict[str, list[dict[str, str]]] = {"debit": [], "credit": []}
    totals = {"debit": Fraction(0), "credit": Fraction(0)}
    for side in ("debit", "credit"):
        opening = positive(data.get(f"opening_{side}", 0), f"control_account.opening_{side}", allow_zero=True)
        if opening:
            sides[side].append({"item": "balance_bd", "label": "Balance b/d", "amount": money(opening, dp)})
            totals[side] += opening
    seen = set()
    for i, entry in enumerate(data["entries"]):
        item = entry["item"]
        if item not in ITEMS[kind]:
            raise WorkerError(Code.DOMAIN_ERROR, f"control_account.entries[{i}]: {item} is not an item of the {kind} "
                              "control account")  # fmt: skip
        if item in seen:
            raise WorkerError(Code.DOMAIN_ERROR, f"control_account.entries: {item} appears more than once; give the "
                              "total")  # fmt: skip
        seen.add(item)
        label, side = ITEMS[kind][item]
        posted = positive(entry["amount"], f"control_account.entries[{i}].amount", allow_zero=True)
        sides[side].append({"item": item, "label": label, "amount": money(posted, dp)})
        totals[side] += posted
    net = totals["debit"] - totals["credit"]  # positive: a debit balance b/d
    carried = "credit" if net >= 0 else "debit"  # the balance c/d sits on the lighter side
    sides[carried].append({"item": "balance_cd", "label": "Balance c/d", "amount": money(abs(net), dp)})
    balance_side = "debit" if net >= 0 else "credit"
    value: dict[str, Any] = {
        "task": "control", "kind": kind, "title": TITLES[kind], "debit": sides["debit"], "credit": sides["credit"],
        "total": money(max(totals["debit"], totals["credit"]), dp),
        "balance_bd": {"side": balance_side if net else NORMAL[kind], "amount": money(abs(net), dp)},
    }  # fmt: skip
    if "ledger_total" in data:
        ledger = positive(data["ledger_total"], "control_account.ledger_total", allow_zero=True)
        signed = net if NORMAL[kind] == "debit" else -net  # the control balance on its normal side
        value["reconciliation"] = {"ledger_total": money(ledger, dp), "agrees": signed == ledger,
                                   "difference": money(signed - ledger, dp)}  # fmt: skip
    if "student_balance" in data:
        student = positive(data["student_balance"], "control_account.student_balance", allow_zero=True)
        side = data.get("student_side", balance_side)
        value["student"] = {"correct": student == abs(net) and (side == balance_side or not net),
                            "expected": money(abs(net), dp), "student": money(student, dp), "side": side}  # fmt: skip
    return value
