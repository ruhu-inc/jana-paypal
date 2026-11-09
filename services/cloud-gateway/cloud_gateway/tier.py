"""The online tier's one monthly allowance per student, from TABLE BIZ-18a's prices and the BIZ-08 margin rule (BIZ-07,
BIZ-08, BIZ-18, OPS-19; D33 and D121, which supersedes D10's single price).

A subscription is paid for a month, a term (`prepaid_term_months` months paid at once) or a year, in its band's
currency (`cloud_gateway.plans`). Whatever the plan, band or period, the allowance is monthly and the same for every
covered student (D121: no premium quota): allowance month `k` of a paid period runs from `k` calendar months after
the period's start to `k + 1` months after it (a start on the 31st falls on the last day of a shorter month), so a
prepaid term or year resets its allowance every month. The device meters the same months
(`agent_core.store.subscription`).

LIST BIZ-08a formula 5: `Allowance_tokens = P_cloud · (1 - g) / p_blend`. `P_cloud` is the lowest price one covered
student's month brings in anywhere in the table (a Family subscription's price shared by its four students, a year's
price over twelve months, in US dollars at the table's proposal rates), so every plan covers its cost. `P_cloud ·
(1 - g)` is what each student's month may cost us, which the gateway enforces as spend; `p_blend` is the price per
token of this gateway's price table at TABLE BIZ-08b's usage mix (`Tier.calls_per_turn`, tokens per call and cached
share), which turns it into the token figure the checkout signs into the entitlement. Recompute whenever a price,
model or prompt budget changes (BIZ-08): `python -m cloud_gateway.tier [deployment.toml]` prints every figure."""

from __future__ import annotations

import calendar
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cloud_gateway.config import Billing, GatewayConfig, load_config

PER_MILLION = 1_000_000


def add_months(moment: float, months: int) -> float:
    """`moment` (a UTC timestamp) moved by whole calendar months, the day of month kept where the month has it."""
    at = datetime.fromtimestamp(moment, UTC)
    index = at.month - 1 + months
    year, month = at.year + index // 12, index % 12 + 1
    return at.replace(year=year, month=month, day=min(at.day, calendar.monthrange(year, month)[1])).timestamp()


def allowance_window(period_start: float, now: float) -> tuple[float, float]:
    """The allowance month of a paid period that holds `now`: (start, end)."""
    at, start = datetime.fromtimestamp(now, UTC), datetime.fromtimestamp(period_start, UTC)
    months = max(0, (at.year - start.year) * 12 + at.month - start.month)
    if months and add_months(period_start, months) > now:
        months -= 1
    return add_months(period_start, months), add_months(period_start, months + 1)


def billing_months(config: GatewayConfig, billing: Billing) -> int:
    """The calendar months a price in TABLE BIZ-18a pays for: a month, a term (D33) or a year."""
    return {"month": 1, "term": config.tier.prepaid_term_months, "year": 12}[billing]


def student_months_usd(config: GatewayConfig) -> dict[str, float]:
    """What one covered student's month brings in at each offer of TABLE BIZ-18a, in US dollars: keyed
    `plan/band/billing`. A plan priced per subscription shares its price among its most students."""
    plans, figures = config.plans, {}
    for plan_id, plan in plans.paid.items():
        students = plan.max_students if plan.priced_per == "subscription" else 1
        for band, offers in plan.prices.items():
            for billing, offer in offers.items():
                usd = offer.amount / plans.per_usd[offer.currency]
                figures[f"{plan_id}/{band}/{billing}"] = usd / billing_months(config, billing) / students
    return figures


def allowance_basis_usd(config: GatewayConfig) -> float:
    """`P_cloud` for the one allowance: the lowest a covered student's month brings in across TABLE BIZ-18a."""
    return min(student_months_usd(config).values(), default=0.0)


def monthly_allowance_usd(config: GatewayConfig) -> float:
    """What one student's month of the online tier may cost us: `P_cloud · (1 - g)`, or the allowance set directly
    when BIZ-08 shows a plan cannot cover its cost (the allowance changes before the price does, BIZ-18)."""
    tier = config.tier
    if tier.allowance_usd_per_student_month is not None:
        return tier.allowance_usd_per_student_month
    return allowance_basis_usd(config) * (1 - tier.gross_margin)


def usage_per_turn(config: GatewayConfig) -> tuple[float, float, float]:
    """(US$, tokens, model calls) of one tutor turn at TABLE BIZ-08b's mix over the primary provider's prices."""
    tier = config.tier
    provider = config.providers.get(config.slots.get("primary", ""))
    if provider is None:
        return 0.0, 0.0, 0.0
    models = provider.models
    usd = tokens = calls = 0.0
    for alias, count in tier.calls_per_turn.items():
        price = config.prices[models[alias].id]
        tokens_in, tokens_out, cached = tier.input_tokens_per_call, tier.output_tokens_per_call, tier.cache_hit_share
        input_usd = tokens_in * ((1 - cached) * price.input + cached * price.cached_input)
        usd += count * (input_usd + tokens_out * price.output) / PER_MILLION
        tokens += count * (tokens_in + tokens_out)
        calls += count
    return usd, tokens, calls


def blended_usd_per_token(config: GatewayConfig) -> float:
    """`p_blend`: US$ per token at the usage mix, the cached share of the input at its cached price."""
    usd, tokens, _ = usage_per_turn(config)
    return usd / tokens if tokens else 0.0


def monthly_allowance_tokens(config: GatewayConfig) -> int:
    """LIST BIZ-08a formula 5: the tokens one student may use in a month, for the entitlement's `allowance_tokens`."""
    blended = blended_usd_per_token(config)
    return int(monthly_allowance_usd(config) / blended) if blended > 0 else 0


def summary(config: GatewayConfig) -> dict[str, Any]:
    """Every figure of the tier's allowance, for the finance review and the checkout (BIZ-08, OPS-19)."""
    tier = config.tier
    usd, tokens, calls = usage_per_turn(config)
    allowance = monthly_allowance_tokens(config)
    turns = allowance / tokens if tokens else 0.0
    per_student = student_months_usd(config)
    return {
        "usd_per_student_month_by_offer": per_student,
        "allowance_basis_usd_per_student_month": allowance_basis_usd(config),
        "allowance_basis_offer": min(per_student, key=per_student.__getitem__) if per_student else None,
        "prepaid_term_months": tier.prepaid_term_months,
        "gross_margin": tier.gross_margin,
        "allowance_usd_per_student_month": monthly_allowance_usd(config),
        "blended_usd_per_million_tokens": blended_usd_per_token(config) * PER_MILLION,
        "usd_per_turn": usd,
        "allowance_tokens_per_student_month": allowance,
        "allowance_turns_per_student_month": int(turns),
        "allowance_requests_per_student_month": int(turns * calls),
        "models": {alias: config.providers[config.slots["primary"]].models[alias].id for alias in tier.calls_per_turn},
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    print(json.dumps(summary(load_config(Path(args[0]) if args else None)), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
