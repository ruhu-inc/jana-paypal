"""Rich accounting tables become exact VER-22 lines; displayed totals confer no correctness."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from agent_core.engine import slice as flows
from agent_core.engine.board_input import publish
from agent_core.engine.input import one_line
from agent_core.engine.state import BoardStep
from agent_core.engine.ui import open_board

COLUMNS = {
    "journal": ["Date", "Particulars", "Folio", "Dr", "Cr"],
    "t_account": ["Dr Date", "Dr Details", "Dr Amount", "Cr Date", "Cr Details", "Cr Amount"],
    "trial_balance": ["Account", "Dr", "Cr"],
}
MONEY = re.compile(r"^(\()?\s*(-)?\s*([$£€₦¥]|[A-Z]{3})?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,8}))?\s*(\))?$")


def response(blocks: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Every amount of the tables as one ledger line (account, side, amount), in the order written."""
    return [line for entry in entries(blocks) for line in entry["lines"]]


def entries(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tables' lines grouped into the journal entries they belong to (VER-22, ASM-07): in a journal, a row with a
    new date, or a debit row after the entry's credits, starts the next entry; a trial balance or a T-account is one
    entry. Each entry keeps its date and the accounts as written."""
    grouped: list[dict[str, Any]] = []
    lines: list[dict[str, str]] = []
    currencies = set()
    for table in blocks:
        if table["type"] != "ledger_table":
            continue
        template = table["template"]
        if table["columns"] != COLUMNS[template] or any(len(row) != len(table["columns"]) for row in table["rows"]):
            raise ValueError("invalid accounting columns")
        if template != "journal" or not grouped:
            grouped.append({"date": None, "lines": []})
        for row in table["rows"]:
            date = row[0].strip() if template == "journal" else ""
            current = grouped[-1]
            row_debit = template == "journal" and bool(row[3].strip())
            credited = any(line["side"] == "credit" for line in current["lines"])
            if template == "journal" and current["lines"] and ((date and date != current["date"]) or
                                                               (row_debit and credited)):  # fmt: skip
                current = {"date": None, "lines": []}
                grouped.append(current)
            if template == "journal" and date and current["date"] is None:
                current["date"] = date
            for index, account_index, side in (
                ((3, 1, "debit"), (4, 1, "credit"))
                if template == "journal"
                else ((1, 0, "debit"), (2, 0, "credit"))
                if template == "trial_balance"
                else ((2, 1, "debit"), (5, 4, "credit"))
            ):
                raw = row[index].strip()
                if not raw:
                    continue
                match = MONEY.fullmatch(raw)
                if not match or len(raw) > 30 or bool(match[1]) != bool(match[6]) or not row[account_index].strip():
                    raise ValueError("invalid accounting amount or account")
                currency = {"$": "USD", "£": "GBP", "€": "EUR", "₦": "NGN", "¥": "JPY"}.get(match[3] or "$", match[3])
                if match[3]:
                    currencies.add(currency)
                amount = Decimal(match[4].replace(",", "") + ("." + match[5] if match[5] else ""))
                if not amount:
                    continue
                if match[1] or match[2]:
                    side = "credit" if side == "debit" else "debit"
                line = {"account": row[account_index].strip(), "label": row[account_index].strip(), "side": side,
                        "amount": str(amount)}  # fmt: skip
                lines.append(line)
                current["lines"].append(line)
    if len(currencies) > 1 or len(lines) > 120:
        raise ValueError("mixed currencies or too many ledger lines")
    if not lines:
        raise ValueError("enter at least one amount")
    return [entry for entry in grouped if entry["lines"]]


async def apply(engine: Any, turn: Any, text: str, blocks: list[dict[str, Any]]) -> Any:
    model = await engine._guard_model(text)  # the input-guard role on the whole table text (AGT-02)
    stopped, text = engine._intake(turn, text, model=model)  # the guard sees the whole table text (PRV-13, PRM-18)
    if stopped is not None:
        return stopped
    lines = response(blocks)
    if engine.state.practice:
        status = await engine._mark_practice(turn, text, lines)
        if status == "unchecked":
            return engine._finish_template(
                turn, "tpl.honest_blocker", "completed", degraded_reason="accounting_unverified"
            )
        return await engine._coach_turn(
            turn,
            intent_note=(
                f"Accounting answer checked against the item key: {status}; "
                "discuss its verdict without supplying a corrected entry."
            ),
        )
    grouped = entries(blocks)
    calls: list[dict[str, Any]] = []
    for n, entry in enumerate(grouped, 1):  # each journal entry checked on its own (VER-22)
        call: dict[str, Any] = {"ref": f"e{n}", "lines": [{k: v for k, v in line.items() if k != "label"}
                                                          for line in entry["lines"]]}  # fmt: skip
        if entry["date"]:
            call["date"] = entry["date"][:24]
        calls.append(call)
    value, check_id = await flows.run_tool(engine, turn, "acc_double_entry", {"entries": calls})
    if check_id is None:
        return engine._finish_template(turn, "tpl.honest_blocker", "completed", degraded_reason="worker_unavailable")
    if engine.cancelled.is_set():
        return engine._finish_interrupted(turn)
    opened = open_board(engine.state)
    from agent_core.engine.events import new_id

    reason = ledger_reason(value or {}, grouped)
    # One board line: a cell's line break never becomes a line of its own anywhere the board is shown (PRM-15).
    step = BoardStep(new_id("stp"), len(engine.state.board.steps), one_line(text), reason, "student", "unchecked",
                     check_id)  # fmt: skip
    engine.state.board.steps.append(step)
    publish(engine, turn, [step], opened)
    engine.state.transcript.append({"role": "user", "content": text, "kind": "attempt"})
    return await engine._coach_turn(
        turn, intent_note=reason + " Ask one question; do not mark unkeyed accounts right or provide corrected entries."
    )


def ledger_reason(value: dict[str, Any], grouped: list[dict[str, Any]]) -> str:
    """The checked journal's one-line reason (VER-22, ASM-07): balanced, or the amount debits and credits differ by
    in total and the first entry that does not balance, with its own totals. Every figure is the tool's."""
    if value.get("all_balanced"):
        return "Every entry balances; account correctness has not been checked against a key."
    described = value.get("entries") or []
    first = next((n for n, entry in enumerate(described) if not entry.get("balanced")), None)
    total = (value.get("trial_balance") or {}).get("difference")
    reason = "Does not balance" + (f": debits and credits differ by {total} in total." if total else ".")
    if first is not None:
        entry = described[first]
        accounts = ", ".join(dict.fromkeys(line["account"] for line in grouped[first]["lines"]))
        named = f"the entry dated {grouped[first]['date']}" if grouped[first]["date"] else f"entry {first + 1}"
        reason += (f" The first entry that does not balance is {named} ({accounts}): debits "
                   f"{entry.get('total_debit')}, credits {entry.get('total_credit')}.")  # fmt: skip
    return one_line(reason)[:300]
