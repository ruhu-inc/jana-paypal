"""Double entry, ledgers, the trial balance, error correction and bank reconciliation (VER-22): `acc_double_entry`.

Tasks:
- `post`: checks that each journal entry balances, classifies each account from the chart of accounts sent with the
  call (the pack's chart, with synonyms), posts the entries to ledger accounts, balances them off (balance c/d, then
  b/d on the opposite side) and extracts the trial balance. With a key it also marks the entries (`mark`).
- `mark`: each student line against the key: `ok`, `wrong_side`, `wrong_account`, `wrong_amount`, `missing_entry` or
  `extra_entry`; an entry whose debits and credits differ is `unbalanced`.
- `correct`: compares each entry as recorded with the entry it should have been, names the error (omission,
  commission, principle, original entry, complete reversal, compensating, or one the trial balance reveals) and
  gives the correcting journal entry, through the suspense account when the recorded entry did not balance.
- `reconcile`: matches the cash book with the bank statement, updates the cash book for items first seen on the
  statement, and reconciles the updated balance with the statement balance through unpresented cheques and
  outstanding lodgements; a student's statement is checked line by line for its first divergent line.
- `control`: a sales or purchases ledger control account from the period's totals, balanced and reconciled with the
  total of the individual ledger balances (VER-34; `control.py`).

Every amount is exact; the places shown come from the call, then the pack, then the configured default."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from tool_worker.functions.control import control_account
from tool_worker.functions.money import (
    AMOUNT_PATTERN,
    Extras,
    amount,
    choice,
    money,
    normal,
    ordered_unique,
    places,
    positive,
)
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

CLASSES = ("asset", "liability", "capital", "income", "expense", "drawings", "contra")
# The side on which each class of account normally has its balance; a contra account names its own.
NORMAL_SIDE = {"asset": "debit", "expense": "debit", "drawings": "debit", "liability": "credit", "capital": "credit",
               "income": "credit"}  # fmt: skip
OPPOSITE = {"debit": "credit", "credit": "debit"}
_TEXT_LINE = re.compile(r"^\s*(?P<side>dr|cr|debit|credit)\b\.?\s*(?P<account>.+?)\s+(?P<amount>\S+)\s*$", re.I)
_AMOUNT = re.compile(AMOUNT_PATTERN)


# --- reading entries --------------------------------------------------------------------------------------------------


@dataclass
class Line:
    account: str
    side: str
    amount: Fraction


@dataclass
class Entry:
    ref: str
    lines: list[Line]
    date: str | None = None

    def total(self, side: str) -> Fraction:
        return sum((line.amount for line in self.lines if line.side == side), Fraction(0))

    @property
    def balanced(self) -> bool:
        return self.total("debit") == self.total("credit")


@dataclass
class KeyLine:
    account: str
    names: set[str]
    side: str
    amount: Fraction
    account_id: str | None = None


def _side(text: str) -> str:
    return "debit" if text.lower().startswith(("dr", "debit")) else "credit"


def parse_text(text: str, field_name: str) -> list[Entry]:
    """Entries written as "Dr Equipment 5000; Cr Cash 2500; Cr Payables 2500", one entry per line (or between |)."""
    entries = []
    for e, chunk in enumerate(part for part in re.split(r"[\n|]", text) if part.strip()):
        lines = []
        for written in (part for part in chunk.split(";") if part.strip()):
            match = _TEXT_LINE.match(written)
            if not match or not _AMOUNT.match(match["amount"]):
                raise WorkerError(
                    Code.PARSE_REJECTED, f"{field_name}: {written.strip()[:60]!r} is not like 'Dr Equipment 5000'"
                )
            lines.append(Line(match["account"].strip(), _side(match["side"]), amount(match["amount"], field_name)))
        entries.append(Entry(f"e{e + 1}", lines))
    return entries


def _entries(raw: list[dict[str, Any]], field_name: str, allow_empty: bool = False) -> list[Entry]:
    out = []
    for e, entry in enumerate(raw):
        lines = [Line(line["account"], line["side"], positive(line["amount"], f"{field_name}[{e}]"))
                 for line in entry["lines"]]  # fmt: skip
        if not lines and not allow_empty:
            raise WorkerError(Code.DOMAIN_ERROR, f"{field_name}[{e}] has no lines")
        out.append(Entry(str(entry.get("ref", f"e{e + 1}")), lines, entry.get("date")))
    return out


def read_entries(args: dict[str, Any], structured: str, text: str) -> list[Entry]:
    if structured in args and text in args:
        raise WorkerError(Code.DOMAIN_ERROR, f"give {structured} or {text}, not both")
    if structured in args:
        return _entries(args[structured], structured)
    if text in args:
        return parse_text(args[text], text)
    return []


# --- the chart of accounts --------------------------------------------------------------------------------------------


@dataclass
class Chart:
    by_name: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    given: bool = False

    @classmethod
    def read(cls, raw: dict[str, Any] | None) -> Chart:
        chart = cls(given=raw is not None)
        for account in (raw or {}).get("accounts", []):
            chart.by_id[account["account_id"]] = account
            for label in [account["account_name"], *account.get("accepted_names", [])]:
                chart.by_name.setdefault(normal(label), account)
        return chart

    def find(self, label: str) -> dict[str, Any] | None:
        return self.by_name.get(normal(label))

    def names(self, account_id: str | None) -> set[str]:
        account = self.by_id.get(account_id or "")
        if account is None:
            return set()
        return {normal(n) for n in [account["account_name"], *account.get("accepted_names", [])]}

    def classify(self, label: str) -> dict[str, Any]:
        account = self.find(label)
        if account is None:
            return {"class": None}
        out: dict[str, Any] = {"account_id": account["account_id"], "class": account["class"]}
        out["normal_side"] = account.get("normal_side", NORMAL_SIDE.get(account["class"], "credit"))
        return out


def _unclassified_warning(chart: Chart, labels: list[str]) -> list[dict[str, str]]:
    if not chart.given:
        return [{"code": "convention_missing", "message": "no chart of accounts was sent, so no account is classified"}]
    missing = [label for label in labels if chart.find(label) is None]
    if not missing:
        return []
    shown = ", ".join(missing[:5]) + (" and others" if len(missing) > 5 else "")
    return [{"code": "convention_missing", "message": f"not in the chart of accounts, so not classified: {shown}"}]


# --- posting, balancing off and the trial balance ---------------------------------------------------------------------


def post(entries: list[Entry], opening: list[dict[str, Any]], chart: Chart, dp: int) -> dict[str, Any]:
    """Ledger accounts in order of first appearance, balanced off, and the trial balance extracted from them."""
    accounts: dict[str, dict[str, Any]] = {}

    def ledger(label: str) -> dict[str, Any]:
        account = chart.find(label)
        name = account["account_name"] if account else label.strip()
        return accounts.setdefault(normal(name), {"account": name, "debit": [], "credit": []})

    for i, held in enumerate(opening):
        ledger(held["account"])[held["side"]].append(
            {"ref": "b/d", "details": ["Balance b/d"], "amount": positive(held["amount"], f"opening_balances[{i}]")}
        )
    for entry in entries:
        for line in entry.lines:  # accounts open in the order their lines are written
            ledger(line.account)
        for line in entry.lines:
            contra = ordered_unique([ledger(o.account)["account"] for o in entry.lines if o.side != line.side])
            ledger(line.account)[line.side].append({"ref": entry.ref, "details": contra, "amount": line.amount})

    out_accounts, trial = [], []
    for book in accounts.values():
        debit = sum((p["amount"] for p in book["debit"]), Fraction(0))
        credit = sum((p["amount"] for p in book["credit"]), Fraction(0))
        info = chart.classify(book["account"])
        row: dict[str, Any] = {"account": book["account"], **info}
        row["debit"] = [{**p, "amount": money(p["amount"], dp)} for p in book["debit"]]
        row["credit"] = [{**p, "amount": money(p["amount"], dp)} for p in book["credit"]]
        if debit == credit:
            row |= {"balance_cd": None, "total": money(debit, dp), "balance_bd": None}
        else:
            bd_side = "debit" if debit > credit else "credit"
            difference = abs(debit - credit)
            row |= {"balance_cd": {"side": OPPOSITE[bd_side], "amount": money(difference, dp)},
                    "total": money(max(debit, credit), dp),
                    "balance_bd": {"side": bd_side, "amount": money(difference, dp)}}  # fmt: skip
            trial.append({"account": book["account"], "side": bd_side, "amount": difference})
            if info.get("normal_side") and info["normal_side"] != bd_side:
                row["unusual_balance"] = True  # e.g. an asset with a credit balance
        out_accounts.append(row)
    debits = sum((t["amount"] for t in trial if t["side"] == "debit"), Fraction(0))
    credits = sum((t["amount"] for t in trial if t["side"] == "credit"), Fraction(0))
    return {
        "ledger": out_accounts,
        "trial_balance": {
            "rows": [{**t, "amount": money(t["amount"], dp)} for t in trial],
            "total_debit": money(debits, dp),
            "total_credit": money(credits, dp),
            "agrees": debits == credits,
            "difference": money(abs(debits - credits), dp),
        },
    }


def describe_entry(entry: Entry, chart: Chart, dp: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ref": entry.ref,
        "balanced": entry.balanced,
        "total_debit": money(entry.total("debit"), dp),
        "total_credit": money(entry.total("credit"), dp),
        "lines": [
            {"account": line.account, "side": line.side, "amount": money(line.amount, dp),
             **chart.classify(line.account)} for line in entry.lines
        ],
    }  # fmt: skip
    if entry.date:
        out["date"] = entry.date
    return out


# --- marking against a key --------------------------------------------------------------------------------------------


def read_key(args: dict[str, Any], chart: Chart) -> list[list[KeyLine]]:
    if "key" in args and "key_text" in args:
        raise WorkerError(Code.DOMAIN_ERROR, "give key or key_text, not both")
    key: list[list[KeyLine]] = []
    for e, entry in enumerate(args.get("key", [])):
        lines = []
        for line in entry["lines"]:
            debit, credit = amount(line["debit"], f"key[{e}]"), amount(line["credit"], f"key[{e}]")
            if (debit == 0) == (credit == 0) or debit < 0 or credit < 0:
                raise WorkerError(Code.DOMAIN_ERROR, f"key[{e}]: each key line has a debit or a credit, not both")
            names = {normal(n) for n in [line["account_name"], *line.get("accepted_names", [])]}
            names |= chart.names(line.get("account_id"))
            side = "debit" if debit else "credit"
            lines.append(KeyLine(line["account_name"], names, side, debit or credit, line.get("account_id")))
        key.append(lines)
    for entry in parse_text(args["key_text"], "key_text") if "key_text" in args else []:
        key.append([KeyLine(line.account, {normal(line.account)} | _chart_names(chart, line.account), line.side,
                            line.amount) for line in entry.lines])  # fmt: skip
    return [lines for lines in key if lines]


def _chart_names(chart: Chart, label: str) -> set[str]:
    account = chart.find(label)
    return chart.names(account["account_id"]) if account else set()


def mark_entry(student: Entry | None, key: list[KeyLine], dp: int) -> dict[str, Any]:
    """Each student line against the key entry, in passes from the closest match to the loosest."""
    lines = student.lines if student else []
    verdicts: list[dict[str, Any] | None] = [None] * len(lines)
    used: set[int] = set()

    def match(test: Any, verdict: str) -> None:
        for s, line in enumerate(lines):
            if verdicts[s] is not None:
                continue
            for k, expected in enumerate(key):
                if k not in used and test(line, expected):
                    used.add(k)
                    verdicts[s] = {"verdict": verdict, "expected": _key_out(expected, dp)}
                    break

    def named(line: Line, expected: KeyLine) -> bool:
        return normal(line.account) in expected.names

    match(lambda ln, ex: named(ln, ex) and ln.side == ex.side and ln.amount == ex.amount, "ok")
    match(lambda ln, ex: named(ln, ex) and ln.side != ex.side and ln.amount == ex.amount, "wrong_side")
    match(lambda ln, ex: named(ln, ex) and ln.side == ex.side, "wrong_amount")
    match(lambda ln, ex: named(ln, ex), "wrong_side")  # side and amount both wrong: the side is named first
    match(lambda ln, ex: ln.side == ex.side and ln.amount == ex.amount, "wrong_account")
    out_lines = []
    for s, line in enumerate(lines):
        written = {"account": line.account, "side": line.side, "amount": money(line.amount, dp)}
        out_lines.append({**written, **(verdicts[s] or {"verdict": "extra_entry"})})
    for k, expected in enumerate(key):
        if k not in used:
            out_lines.append({"verdict": "missing_entry", "expected": _key_out(expected, dp)})
    balanced = student.balanced if student else True
    correct = balanced and student is not None and all(line["verdict"] == "ok" for line in out_lines)
    return {
        "ref": student.ref if student else None,
        "verdict": "correct" if correct else ("unbalanced" if not balanced else "incorrect"),
        "balanced": balanced,
        "lines": out_lines,
    }


def _key_out(line: KeyLine, dp: int) -> dict[str, Any]:
    out = {"account": line.account, "side": line.side, "amount": money(line.amount, dp)}
    return out | ({"account_id": line.account_id} if line.account_id else {})


# --- errors and their correction --------------------------------------------------------------------------------------


def _net(entry: Entry, chart: Chart) -> dict[str, Fraction]:
    """Signed effect per account, debit positive, keyed by the chart's name when the account is in it."""
    out: dict[str, Fraction] = {}
    for line in entry.lines:
        found = chart.find(line.account)
        name = found["account_name"] if found else line.account.strip()
        out[name] = out.get(name, Fraction(0)) + (line.amount if line.side == "debit" else -line.amount)
    return out


def _signature(entry: Entry, chart: Chart) -> list[tuple[str, str, Fraction]]:
    return sorted((normal(k), "debit" if v > 0 else "credit", abs(v)) for k, v in _net(entry, chart).items() if v)


def error_kind(recorded: Entry, intended: Entry, chart: Chart) -> tuple[str, str | None]:
    """The kind of error for one recorded entry, and a reason when it cannot be decided."""
    if not recorded.lines:
        return "omission", None
    if not recorded.balanced:
        return "revealed_by_trial_balance", None
    got, want = _signature(recorded, chart), _signature(intended, chart)
    if got == want:
        return "none", None
    if sorted((a, OPPOSITE[s], v) for a, s, v in got) == want:
        return "complete_reversal", None
    if [(a, s) for a, s, _ in got] == [(a, s) for a, s, _ in want]:
        return "original_entry", None
    got_amounts = sorted((s, v) for _, s, v in got)
    if got_amounts == sorted((s, v) for _, s, v in want):
        wrong = [a for a, _, _ in got if a not in {w for w, _, _ in want}]
        meant = [a for a, _, _ in want if a not in {g for g, _, _ in got}]
        if len(wrong) == 1 and len(meant) == 1:
            classes = [chart.classify(name).get("class") for name in (wrong[0], meant[0])]
            if None in classes:
                return "unknown", "the chart of accounts is needed to tell commission from principle"
            return ("commission" if classes[0] == classes[1] else "principle"), None
    return "unknown", "more than one thing differs in the entry"


def correct_entries(recorded: list[Entry], intended: list[Entry], chart: Chart, dp: int) -> dict[str, Any]:
    if len(recorded) != len(intended):
        raise WorkerError(Code.DOMAIN_ERROR, "give one recorded entry for each intended entry, in the same order")
    corrections: list[dict[str, Any]] = []
    suspense: list[dict[str, Any]] = []
    kinds = []
    for wrong, right in zip(recorded, intended, strict=True):
        kind, reason = error_kind(wrong, right, chart)
        kinds.append(kind)
        net = _net(right, chart)
        for name, value in _net(wrong, chart).items():
            net[name] = net.get(name, Fraction(0)) - value
        lines: list[dict[str, Any]] = [{"account": name, "side": "debit" if v > 0 else "credit", "amount": abs(v)}
                 for name, v in net.items() if v]  # fmt: skip
        imbalance = sum((v for v in net.values()), Fraction(0))
        if imbalance:  # the recorded entry did not balance: the difference sits in suspense until corrected
            side = "credit" if imbalance > 0 else "debit"
            lines.append({"account": "Suspense", "side": side, "amount": abs(imbalance)})
            suspense.append({"ref": right.ref, "side": side, "amount": abs(imbalance)})
        out: dict[str, Any] = {"ref": right.ref, "error": kind,
                               "revealed_by_trial_balance": kind == "revealed_by_trial_balance",
                               "correction": [{**ln, "amount": money(ln["amount"], dp)} for ln in lines]}  # fmt: skip
        if reason:
            out["reason"] = reason
        corrections.append(out)
    # Errors that each upset the trial balance but cancel one another are compensating: together they hide.
    unbalanced = [i for i, kind in enumerate(kinds) if kind == "revealed_by_trial_balance"]
    total = sum((s["amount"] if s["side"] == "debit" else -s["amount"] for s in suspense), Fraction(0))
    if len(unbalanced) >= 2 and total == 0:
        for i in unbalanced:
            corrections[i] |= {"error": "compensating", "revealed_by_trial_balance": False}
    opening = -total  # the suspense balance the recorded entries left: credit when debits were short
    return {
        "corrections": corrections,
        "suspense": {
            "opening": {"side": "credit" if opening < 0 else "debit", "amount": money(abs(opening), dp)}
            if opening
            else None,
            "postings": [{**s, "amount": money(s["amount"], dp)} for s in suspense],
        },
    }


# --- bank reconciliation ----------------------------------------------------------------------------------------------


def _balance(book: dict[str, Any], field_name: str) -> Fraction:
    value = positive(book["balance"], f"{field_name}.balance", allow_zero=True)
    return -value if book.get("overdrawn") else value


Items = list[dict[str, Any]]


def _match(cash: Items, bank: Items) -> tuple[list[int], list[int], list[tuple[int, int]]]:
    """Cash book and statement items paired by reference, then by direction and amount, in order. Returns the
    unmatched cash book items, the unmatched statement items and the pairs whose amounts differ."""
    paired_cash: dict[int, int] = {}
    for c, item in enumerate(cash):
        if not item.get("ref"):
            continue
        for b, line in enumerate(bank):
            if b not in paired_cash.values() and line.get("ref") == item["ref"] and line["direction"] == item[
                    "direction"]:  # fmt: skip
                paired_cash[c] = b
                break
    for c, item in enumerate(cash):
        if c in paired_cash:
            continue
        for b, line in enumerate(bank):
            if (b not in paired_cash.values() and line["direction"] == item["direction"]
                    and line["value"] == item["value"] and not (line.get("ref") and item.get("ref"))):  # fmt: skip
                paired_cash[c] = b
                break
    differ = [(c, b) for c, b in paired_cash.items() if cash[c]["value"] != bank[b]["value"]]
    return ([c for c in range(len(cash)) if c not in paired_cash],
            [b for b in range(len(bank)) if b not in paired_cash.values()], differ)  # fmt: skip


def _items(raw: list[dict[str, Any]], field_name: str) -> list[dict[str, Any]]:
    return [{**item, "value": positive(item["amount"], f"{field_name}[{i}]")} for i, item in enumerate(raw)]


def _label(item: dict[str, Any]) -> str:
    details = str(item.get("details") or "").strip()
    ref = str(item.get("ref") or "").strip()
    return " ".join(part for part in (details, ref) if part) or "item"


def reconcile(args: dict[str, Any], context: CallContext, dp: int) -> tuple[dict[str, Any], list[dict[str, str]]]:
    cash_book, statement = args["cash_book"], args["bank_statement"]
    cash = _items(cash_book.get("entries", []), "cash_book.entries")
    bank = _items(statement.get("entries", []), "bank_statement.entries")
    if "balance" in cash_book:
        cash_balance = _balance(cash_book, "cash_book")
    elif "opening_balance" in cash_book:
        opening = positive(cash_book["opening_balance"], "cash_book.opening_balance", allow_zero=True)
        cash_balance = (-opening if cash_book.get("opening_overdrawn") else opening) + sum(
            (i["value"] if i["direction"] == "in" else -i["value"] for i in cash if not i.get("earlier")), Fraction(0)
        )
    else:
        raise WorkerError(Code.DOMAIN_ERROR, "cash_book needs its balance or its opening balance")
    bank_balance = _balance(statement, "bank_statement")
    only_cash, only_bank, differ = _match(cash, bank)

    # 1. The cash book updated for items first seen on the bank statement, and for amounts entered wrongly.
    updates = []
    for b in only_bank:
        item = bank[b]
        updates.append({"id": f"update:{b + 1}", "label": _label(item), "direction": item["direction"],
                        "amount": item["value"]})  # fmt: skip
    for c, b in differ:
        signed = (bank[b]["value"] - cash[c]["value"]) * (1 if cash[c]["direction"] == "in" else -1)
        updates.append({"id": f"error:{c + 1}", "label": f"correction of {_label(cash[c])}",
                        "direction": "in" if signed > 0 else "out", "amount": abs(signed)})  # fmt: skip
    adjusted = cash_balance + sum((u["amount"] if u["direction"] == "in" else -u["amount"] for u in updates),
                                  Fraction(0))  # fmt: skip

    # 2. The reconciliation statement, in the layout in force.
    start, start_source, warnings = choice(args, context, "start_from", "acc.reconciliation_start",
                                           ("bank_statement", "cash_book"), "bank_statement")  # fmt: skip
    lodgements = [c for c in only_cash if cash[c]["direction"] == "in"]
    cheques = [c for c in only_cash if cash[c]["direction"] == "out"]
    lines: list[dict[str, Any]] = []
    first_sign = 1 if start == "bank_statement" else -1  # from the cash book the reconciling items reverse
    opening_line, closing_line = (("bank_statement_balance", "balance as per bank statement", bank_balance),
                                  ("cash_book_balance", "balance as per cash book (updated)", adjusted))  # fmt: skip
    if start == "cash_book":
        opening_line, closing_line = closing_line, opening_line
    lines.append({"id": opening_line[0], "label": opening_line[1], "op": "start", "amount": opening_line[2]})
    for c in lodgements:
        lines.append({"id": f"lodgement:{c + 1}", "label": f"outstanding lodgement {_label(cash[c])}",
                      "op": "add" if first_sign > 0 else "less", "amount": cash[c]["value"]})  # fmt: skip
    for c in cheques:
        lines.append({"id": f"unpresented:{c + 1}", "label": f"unpresented cheque {_label(cash[c])}",
                      "op": "less" if first_sign > 0 else "add", "amount": cash[c]["value"]})  # fmt: skip
    running = opening_line[2] + sum((ln["amount"] if ln["op"] == "add" else -ln["amount"] for ln in lines[1:]),
                                    Fraction(0))  # fmt: skip
    lines.append({"id": closing_line[0], "label": closing_line[1], "op": "total", "amount": closing_line[2]})
    value: dict[str, Any] = {
        "task": "reconcile",
        "cash_book": {
            "balance": money(cash_balance, dp),
            "updates": [{**u, "amount": money(u["amount"], dp)} for u in updates],
            "updated_balance": money(adjusted, dp),
            "overdrawn": adjusted < 0,
        },
        "statement": {
            "start_from": {"value": start, "source": start_source},
            "lines": [{**ln, "amount": money(ln["amount"], dp)} for ln in lines],
        },
        "reconciled": running == closing_line[2],
        "difference": money(running - closing_line[2], dp),
    }
    if "student" in args:
        value["student"] = compare_in_order(lines, args["student"], dp)
    return value, warnings


def compare_in_order(expected: list[dict[str, Any]], student: list[dict[str, Any]], dp: int) -> dict[str, Any]:
    """A student's lines against the expected lines in order, aligned on amounts: the first expected line that the
    student left out, wrote with another amount, or preceded by a line of their own, is the first divergent line."""
    written = [positive(line["amount"], f"student[{i}]", allow_zero=True) for i, line in enumerate(student)]
    want = [abs(line["amount"]) for line in expected]
    matcher = difflib.SequenceMatcher(None, [money(v, dp) for v in want], [money(v, dp) for v in written],
                                      autojunk=False)  # fmt: skip
    marks: list[dict[str, Any]] = []
    for op, e1, e2, s1, s2 in matcher.get_opcodes():
        if op == "equal":
            marks += [{"index": e, "id": expected[e]["id"], "verdict": "ok"} for e in range(e1, e2)]
            continue
        pairs = min(e2 - e1, s2 - s1) if op == "replace" else 0
        for k in range(pairs):
            marks.append({"index": e1 + k, "id": expected[e1 + k]["id"], "verdict": "wrong_amount",
                          "expected": money(want[e1 + k], dp), "student": money(written[s1 + k], dp)})  # fmt: skip
        for e in range(e1 + pairs, e2):
            marks.append({"index": e, "id": expected[e]["id"], "verdict": "missing_line",
                          "expected": money(want[e], dp)})  # fmt: skip
        for s in range(s1 + pairs, s2):
            marks.append({"index": e1 + pairs, "id": None, "verdict": "extra_line", "student": money(written[s], dp),
                          "student_line": s})  # fmt: skip
    first = next((m for m in marks if m["verdict"] != "ok"), None)
    if first is not None and first["id"]:
        first = {**first, "label": expected[first["index"]]["label"]}
    return {"correct": first is None, "first_divergent": first, "lines": marks}


# --- acc_double_entry -------------------------------------------------------------------------------------------------


def _task(args: dict[str, Any]) -> str:
    if "task" in args:
        return str(args["task"])
    if "control_account" in args:
        return "control"
    if "cash_book" in args or "bank_statement" in args:
        return "reconcile"
    if "recorded" in args:
        return "correct"
    return "mark" if "key" in args or "key_text" in args else "post"


def acc_double_entry(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """Journal entries checked, posted and balanced, marked against a key, corrected, a bank reconciliation or a
    control account."""
    dp = places(args, context)
    task = _task(args)
    chart = Chart.read(args.get("chart"))
    if task == "control":
        if "control_account" not in args:
            raise WorkerError(Code.DOMAIN_ERROR, "a control account needs control_account")
        return control_account(args["control_account"], dp), {"method": "exact", "confidence": "proved"}
    if task == "reconcile":
        if "cash_book" not in args or "bank_statement" not in args:
            raise WorkerError(Code.DOMAIN_ERROR, "a reconciliation needs cash_book and bank_statement")
        value, warnings = reconcile(args, context, dp)
        return value, {"warnings": warnings, "method": "exact", "confidence": "proved"}
    if task == "correct":
        if "recorded" not in args or "entries" not in args:
            raise WorkerError(Code.DOMAIN_ERROR, "a correction needs recorded and entries (as they should have been)")
        recorded = _entries(args["recorded"], "recorded", allow_empty=True)
        intended = _entries(args["entries"], "entries")
        value = {"task": "correct", **correct_entries(recorded, intended, chart, dp)}
        labels = [line.account for entry in recorded + intended for line in entry.lines]
        return value, {"warnings": _unclassified_warning(chart, ordered_unique(labels)), "method": "rule_based",
                       "confidence": "proved"}  # fmt: skip
    entries = read_entries(args, "entries", "entries_text")
    if not entries and not args.get("opening_balances"):
        raise WorkerError(Code.DOMAIN_ERROR, "give entries or entries_text")
    value = {
        "task": task,
        "entries": [describe_entry(entry, chart, dp) for entry in entries],
        "all_balanced": all(entry.balanced for entry in entries),
        **post(entries, args.get("opening_balances", []), chart, dp),
    }
    if task == "mark":
        key = read_key(args, chart)
        if not key:
            raise WorkerError(Code.DOMAIN_ERROR, "marking needs key or key_text")
        marked = [mark_entry(entries[i] if i < len(entries) else None, key[i], dp) for i in range(len(key))]
        for extra in entries[len(key) :]:
            marked.append(mark_entry(extra, [], dp))
        value["marking"] = {"correct": all(m["verdict"] == "correct" for m in marked), "entries": marked}
    labels = ordered_unique([line.account for entry in entries for line in entry.lines]
                            + [line["account"] for line in args.get("opening_balances", [])])  # fmt: skip
    return value, {"warnings": _unclassified_warning(chart, labels), "method": "exact", "confidence": "proved"}
