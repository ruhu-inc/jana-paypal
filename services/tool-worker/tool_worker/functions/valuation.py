"""Depreciation schedules and inventory valuation (VER-24): `acc_depreciation` and `acc_inventory`.

Depreciation: straight line ((cost - residual) / life, or a percentage of cost) or reducing balance (a percentage of
the opening net book value), year by year. Each year's charge is rounded to the places in force (the item's, then
the pack's, then the configured default) and the net book value carries the rounded charges, as a schedule is
written. The part-year policy is either a full year's charge in the year of purchase and none in the year of
disposal, or charges time-apportioned by the months held. A straight-line charge never takes the net book value
below the residual value. A disposal gives the net book value, the gain or loss and the disposal account.

Inventory: FIFO, or AVCO (perpetual, the average recomputed after each receipt; or the periodic weighted average),
as a stores ledger table with receipts, issues and the balance after each movement, the cost of sales and the closing
inventory at the lower of cost and net realisable value when the NRV is given. Values are exact; an AVCO unit cost is
exact unless the call or the pack rounds it to the places in force at each receipt."""

from __future__ import annotations

from fractions import Fraction
from math import ceil
from typing import Any

from tool_worker.functions.money import Extras, convention, figure, money, places, positive, round_half_up
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext


def _rate(raw: Any, field_name: str) -> Fraction:
    text = str(raw).strip().rstrip("%").strip()
    rate = positive(text, field_name) / 100
    if rate > 1:
        raise WorkerError(Code.DOMAIN_ERROR, f"{field_name} cannot be more than 100%")
    return rate


# --- acc_depreciation -------------------------------------------------------------------------------------------------


def _policy(args: dict[str, Any], context: CallContext) -> tuple[str, str, list[dict[str, str]]]:
    if "part_year_policy" in args:
        return str(args["part_year_policy"]), "item", []
    held = convention(context, "acc.part_year_policy")
    if held is not None and held[0] in ("full_year", "time_apportioned"):
        return str(held[0]), held[1], []
    message = "no part-year policy was stated; a full year in the year of purchase and none in the year of disposal"
    return "full_year", "default", [{"code": "convention_default_used", "message": message}]


def acc_depreciation(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """A depreciation schedule by straight line or reducing balance, with a disposal when given (VER-24)."""
    dp = places(args, context)
    method = args["method"]
    cost = positive(args["cost"], "cost")
    residual = positive(args.get("residual", 0), "residual", allow_zero=True)
    if residual >= cost:
        raise WorkerError(Code.DOMAIN_ERROR, "the residual value must be less than the cost")
    policy, policy_source, warnings = _policy(args, context)
    if method == "straight_line":
        if ("life_years" in args) == ("rate_percent" in args):
            raise WorkerError(Code.DOMAIN_ERROR, "straight line needs life_years or rate_percent (of cost), not both")
        if "life_years" in args:
            annual = (cost - residual) / int(args["life_years"])
            basis = f"(cost - residual) / {args['life_years']} years"
        else:
            annual = cost * _rate(args["rate_percent"], "rate_percent")
            basis = f"{args['rate_percent']}% of cost".replace("%%", "%")
        rate = Fraction(0)
    else:
        if "rate_percent" not in args:
            raise WorkerError(Code.DOMAIN_ERROR, "reducing balance needs rate_percent")
        rate = _rate(args["rate_percent"], "rate_percent")
        annual, basis = Fraction(0), f"{args['rate_percent']}% of the opening net book value".replace("%%", "%")
    first_months = int(args.get("first_year_months", 12)) if policy == "time_apportioned" else 12
    disposal = args.get("disposal")
    if disposal is not None:
        years = int(disposal["year"])
    elif "years" in args:
        years = int(args["years"])
    elif method == "straight_line":
        years = ceil((cost - residual) / annual) + (1 if first_months < 12 else 0)
    else:
        raise WorkerError(Code.DOMAIN_ERROR, "reducing balance needs years or a disposal to end the schedule")

    rows, nbv, accumulated = [], cost, Fraction(0)
    for year in range(1, years + 1):
        opening = nbv
        full = annual if method == "straight_line" else opening * rate
        if disposal is not None and year == years:
            share = Fraction(int(disposal.get("months", 0)), 12) if policy == "time_apportioned" else Fraction(0)
        elif year == 1:
            share = Fraction(first_months, 12)
        else:
            share = Fraction(1)
        charge = round_half_up(full * share, dp)
        if method == "straight_line":
            charge = max(min(charge, opening - residual), Fraction(0))
        accumulated += charge
        nbv = opening - charge
        rows.append({"year": year, "opening_nbv": money(opening, dp), "charge": money(charge, dp),
                     "accumulated": money(accumulated, dp), "closing_nbv": money(nbv, dp)})  # fmt: skip
    value: dict[str, Any] = {
        "method": method,
        "basis": basis,
        "part_year_policy": {"value": policy, "source": policy_source},
        "cost": money(cost, dp),
        "residual": money(residual, dp),
        "schedule": rows,
    }
    if method == "straight_line":
        value["annual_charge"] = figure(annual, dp)
    if disposal is not None:
        proceeds = positive(disposal["proceeds"], "disposal.proceeds", allow_zero=True)
        difference = proceeds - nbv
        result = "gain" if difference > 0 else ("loss" if difference < 0 else "none")
        account = [
            {"side": "debit", "details": "cost", "amount": money(cost, dp)},
            {"side": "credit", "details": "accumulated depreciation", "amount": money(accumulated, dp)},
            {"side": "credit", "details": "proceeds", "amount": money(proceeds, dp)},
        ]
        if difference:
            side = "debit" if difference > 0 else "credit"  # a gain is debited to close the disposal account
            account.append({"side": side, "details": result, "amount": money(abs(difference), dp)})
        value["disposal"] = {"year": years, "net_book_value": money(nbv, dp), "proceeds": money(proceeds, dp),
                             "result": result, "amount": money(abs(difference), dp),
                             "disposal_account": account}  # fmt: skip
    working = [{"label": f"year {r['year']} charge", "value": r["charge"]} for r in rows[:50]]
    return value, {"warnings": warnings, "working": working, "method": "exact", "confidence": "proved"}


# --- acc_inventory ----------------------------------------------------------------------------------------------------


def _avco_rounding(args: dict[str, Any], context: CallContext) -> tuple[bool, list[dict[str, str]]]:
    if "round_unit_cost" in args:
        return bool(args["round_unit_cost"]), []
    held = convention(context, "acc.avco_unit_cost")
    if held is not None and held[0] in ("exact", "rounded"):
        return held[0] == "rounded", []
    message = "no convention for rounding the AVCO unit cost was stated; it is kept exact"
    return False, [{"code": "convention_default_used", "message": message}]


def _layers(layers: list[list[Fraction]], dp: int) -> list[dict[str, Any]]:
    return [{"quantity": int(q), "unit_cost": figure(c, dp), "value": money(q * c, dp)} for q, c in layers]


def acc_inventory(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """The stores ledger by FIFO or AVCO, cost of sales and closing inventory (VER-24)."""
    dp = places(args, context)
    method = args["method"]
    basis = str(args.get("avco_basis", "perpetual"))
    warnings: list[dict[str, str]] = []
    rounded = False
    if method == "avco":
        rounded, warnings = _avco_rounding(args, context)
    movements = args["movements"]
    for i, move in enumerate(movements):
        if move["kind"] == "opening" and i:
            raise WorkerError(Code.DOMAIN_ERROR, f"movements[{i}]: the opening balance comes first")
        if move["kind"] != "issue" and "unit_cost" not in move:
            raise WorkerError(Code.DOMAIN_ERROR, f"movements[{i}]: a {move['kind']} needs its unit_cost")
    periodic_cost: Fraction | None = None
    if method == "avco" and basis == "periodic":
        bought = [(m["quantity"], positive(m["unit_cost"], "unit_cost")) for m in movements if m["kind"] != "issue"]
        periodic_cost = sum((q * c for q, c in bought), Fraction(0)) / sum(q for q, _ in bought)
        if rounded:
            periodic_cost = round_half_up(periodic_cost, dp)

    layers: list[list[Fraction]] = []  # FIFO: [quantity, unit cost], oldest first
    quantity, value_held, unit = 0, Fraction(0), Fraction(0)
    rows, cost_of_sales = [], Fraction(0)
    for i, move in enumerate(movements):
        q = int(move["quantity"])
        row: dict[str, Any] = {"kind": move["kind"], "receipt": None, "issue": None}
        if move.get("date"):
            row["date"] = move["date"]
        if move["kind"] in ("opening", "receipt"):
            cost = positive(move["unit_cost"], f"movements[{i}].unit_cost")
            row["receipt"] = {"quantity": q, "unit_cost": figure(cost, dp), "value": money(q * cost, dp)}
            layers.append([Fraction(q), cost])
            quantity += q
            value_held += q * cost
            if method == "avco":
                unit = periodic_cost if periodic_cost is not None else value_held / quantity
                if rounded and periodic_cost is None:
                    unit = round_half_up(unit, dp)
        else:
            if q > quantity:
                raise WorkerError(Code.DOMAIN_ERROR, f"movements[{i}]: issuing {q} when only {quantity} are held")
            if method == "fifo":
                taken, left = [], Fraction(q)
                while left:
                    layer = layers[0]
                    use = min(left, layer[0])
                    taken.append([use, layer[1]])
                    layer[0] -= use
                    left -= use
                    if not layer[0]:
                        layers.pop(0)
                issued = sum((a * c for a, c in taken), Fraction(0))
                row["issue"] = {"quantity": q, "layers": _layers(taken, dp), "value": money(issued, dp)}
            else:
                issued = q * unit
                row["issue"] = {"quantity": q, "unit_cost": figure(unit, dp), "value": money(issued, dp)}
            quantity -= q
            value_held -= issued
            cost_of_sales += issued
        if periodic_cost is not None:  # the periodic average values every issue and balance at one cost
            value_held = quantity * periodic_cost
        elif method == "avco" and rounded and quantity:
            value_held = quantity * unit  # a rounded unit cost values the whole balance, as the ledger is written
        balance: dict[str, Any] = {"quantity": quantity, "value": money(value_held, dp)}
        if method == "fifo":
            balance["layers"] = _layers(layers, dp)
        elif quantity:
            balance["unit_cost"] = figure(unit, dp)
        row["balance"] = balance
        rows.append(row)
    closing: dict[str, Any] = {"quantity": quantity, "cost": money(value_held, dp)}
    carried = value_held
    if "nrv" in args:
        nrv_args = args["nrv"]
        if ("per_unit" in nrv_args) == ("total" in nrv_args):
            raise WorkerError(Code.DOMAIN_ERROR, "nrv needs per_unit or total, not both")
        nrv = (quantity * positive(nrv_args["per_unit"], "nrv.per_unit", allow_zero=True) if "per_unit" in nrv_args
               else positive(nrv_args["total"], "nrv.total", allow_zero=True))  # fmt: skip
        closing["nrv"] = money(nrv, dp)
        carried = min(value_held, nrv)
        closing["basis"] = "nrv" if nrv < value_held else "cost"
    closing["value"] = money(carried, dp)
    out: dict[str, Any] = {
        "method": method,
        "stores_ledger": rows,
        "cost_of_sales": money(cost_of_sales, dp),
        "closing_inventory": closing,
    }
    if method == "avco":
        out["avco_basis"] = basis
        out["unit_cost_rounded"] = rounded
        if periodic_cost is not None:
            out["periodic_unit_cost"] = figure(periodic_cost, dp)
    return out, {"warnings": warnings, "method": "exact", "confidence": "proved"}
