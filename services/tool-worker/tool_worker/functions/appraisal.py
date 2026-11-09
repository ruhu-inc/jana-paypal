"""Investment appraisal (VER-34): `acc_appraisal` — payback, accounting rate of return, net present value from a
discount table or the exact factors, and the internal rate of return by linear interpolation.

Cash flows are the net inflows of years 1 to n after an outlay at year 0; a residual value is received at the end of
year n. Values are exact; the places shown come from the call, then the pack, then the configured default.

- Payback: the whole years before the outlay is recovered plus the fraction of the next year, assuming the year's flow
  arrives evenly (stated in the working); also in years and months, the month rounded up as exam boards do unless the
  call asks for the nearest month. An outlay not recovered within the flows is `not_recovered`, never a projected year.
- ARR: average annual profit (average inflow less straight-line depreciation (outlay - residual) / n) over the average
  investment ((outlay + residual) / 2) or the initial investment. The basis differs between syllabuses: it comes from
  the call or the pack (`acc.arr_basis`) and, stated by nobody, ARR is `unknown` with `convention_missing`.
- NPV: each flow times its factor, less the outlay. Factors from the call's table (as printed, usually 3 places) are
  used as given; otherwise the exact factor 1 / (1 + r)^t is used, and which one is recorded.
- IRR: NPV at two rates (each from its table or exact factors) and IRR = r1 + NPV1 / (NPV1 - NPV2) x (r2 - r1). The
  two NPVs must have opposite signs (or one be zero): interpolation never extrapolates."""

from __future__ import annotations

from fractions import Fraction
from math import ceil
from typing import Any

from tool_worker.functions.money import Extras, amount, convention, figure, money, places, positive, round_half_up
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

BASES = ("average_investment", "initial_investment")


def _domain(message: str) -> WorkerError:
    return WorkerError(Code.DOMAIN_ERROR, message)


def _rate(raw: Any, field: str) -> Fraction:
    rate = amount(raw, field)
    if rate < 0 or rate >= 1000:
        raise _domain(f"{field}: a rate in percent from 0 to under 1000")
    return rate


def _factors(rate: Fraction, years: int, table: list[Any] | None, field: str) -> tuple[list[Fraction], str]:
    if table is not None:
        if len(table) != years:
            raise _domain(f"{field}: give one factor for each of the {years} years")
        factors = [amount(f, field) for f in table]
        if any(not 0 < f <= 1 for f in factors):
            raise _domain(f"{field}: a discount factor is more than 0 and at most 1")
        return factors, "table"
    base = 1 + rate / 100
    return [1 / base**t for t in range(1, years + 1)], "exact"


def _npv(outlay: Fraction, flows: list[Fraction], factors: list[Fraction]) -> tuple[Fraction, list[Fraction]]:
    present = [flow * factor for flow, factor in zip(flows, factors, strict=True)]
    return sum(present, Fraction(0)) - outlay, present


def acc_appraisal(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    dp = places(args, context)
    outlay = positive(args["initial_investment"], "initial_investment")
    flows = [amount(f, "cash_flows") for f in args["cash_flows"]]
    residual = positive(args.get("residual_value", 0), "residual_value", allow_zero=True)
    flows_in = [*flows[:-1], flows[-1] + residual]  # the residual arrives with the last year's flow
    years = len(flows)
    methods = list(args.get("methods") or ["payback", "arr", "npv", "irr"])
    value: dict[str, Any] = {}
    check = context.check_id
    working: list[dict[str, Any]] = []

    if "payback" in methods:
        cumulative, year = -outlay, 0
        while year < years and cumulative + flows_in[year] < 0:
            cumulative += flows_in[year]
            year += 1
        if year == years or flows_in[year] <= 0:
            value["payback"] = {"status": "unknown", "reason": "not_recovered"}
        else:
            exact = year + (-cumulative) / flows_in[year]
            whole = int(exact)
            part = (exact - whole) * 12
            months = ceil(part) if args.get("payback_months", "round_up") == "round_up" else int(round_half_up(part, 0))
            if months == 12:
                whole, months = whole + 1, 0
            value["payback"] = {**figure(exact, dp), "years": whole, "months": months}
            working.append({"label": "payback", "value": f"{year} years + {money(-cumulative, dp)} / "
                            f"{money(flows_in[year], dp)} of year {year + 1} (flows arrive evenly)",
                            "check_id": check})  # fmt: skip

    if "arr" in methods:
        held = (str(args["arr_basis"]), "item") if "arr_basis" in args else convention(context, "acc.arr_basis")
        if held is None or held[0] not in BASES:
            value["arr"] = {"status": "unknown", "reason": "convention_missing", "definition": "acc.arr_basis"}
        else:
            basis = held[0]
            profit = (sum(flows, Fraction(0)) - (outlay - residual)) / years
            invested = (outlay + residual) / 2 if basis == "average_investment" else outlay
            arr = profit / invested * 100
            value["arr"] = {"percent": figure(arr, dp), "average_annual_profit": money(profit, dp),
                            "investment": money(invested, dp), "basis": basis, "basis_source": held[1]}  # fmt: skip

    if "npv" in methods:
        if "discount_rate_percent" not in args:
            raise _domain("NPV needs discount_rate_percent")
        rate = _rate(args["discount_rate_percent"], "discount_rate_percent")
        factors, source = _factors(rate, years, args.get("discount_factors"), "discount_factors")
        npv, present = _npv(outlay, flows_in, factors)
        value["npv"] = {"value": money(npv, dp), "exact": figure(npv, dp)["exact"], "rate_percent": money(rate, 2),
                        "factors_source": source, "present_values": [money(p, dp) for p in present]}  # fmt: skip
        working.append({"label": "NPV", "value": "sum of flow x factor - outlay", "check_id": check})

    if "irr" in methods:
        rates = args.get("irr_rates")
        if not rates or len(rates) != 2:
            raise _domain("IRR needs two rates (irr_rates) to interpolate between")
        tables = args.get("irr_factors") or [None, None]
        low, high = (_rate(r, "irr_rates") for r in rates)
        if low == high:
            raise _domain("irr_rates: give two different rates")
        npvs = []
        for rate, table in ((low, tables[0]), (high, tables[1])):
            factors, _ = _factors(rate, years, table, "irr_factors")
            npvs.append(_npv(outlay, flows_in, factors)[0])
        if npvs[0] * npvs[1] > 0 or npvs[0] == npvs[1]:
            value["irr"] = {"status": "unknown", "reason": "not_bracketed",
                            "npvs": [money(n, dp) for n in npvs]}  # fmt: skip
        else:
            irr = low + npvs[0] / (npvs[0] - npvs[1]) * (high - low)
            value["irr"] = {"percent": figure(irr, dp), "rates": [money(low, 2), money(high, 2)],
                            "npvs": [money(n, dp) for n in npvs], "method": "linear interpolation"}  # fmt: skip
            working.append({"label": "IRR", "value": "r1 + NPV1 / (NPV1 - NPV2) x (r2 - r1)", "check_id": check})

    decided = any(part.get("status") != "unknown" for part in value.values())
    return value, {"method": "exact", "confidence": "proved" if decided else "undecided", "working": working}
