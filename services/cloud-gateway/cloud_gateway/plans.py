"""The checkout's side of TABLE BIZ-18a (BIZ-18, D121): the band from the billing country, the band's price in its
currency, and the entitlement terms the checkout signs (BIZ-07, D70).

The band is chosen from the billing country the payer gives at checkout, never from the device (BIZ-18): Nigeria, the
other purchasing-power band, or international. A quote is the band's price for a plan and a paid period in the band's
own currency, never converted for the payer. The entitlement grants exactly the table's features and fair-use limits,
with the one monthly allowance every paid plan shares (`cloud_gateway.tier`): no model picker and no premium quota.

This module never takes a payment or a payer's detail: the payment partner does that (BIZ-10), and the entitlement
names no name, e-mail address or payment detail (the licence contract). The gateway checks a presented entitlement
against the table's seat limits (`check_terms`), never against figures that may have changed since
it was signed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from cloud_gateway.config import Band, Billing, GatewayConfig, Plan, PlanId
from cloud_gateway.tier import add_months, billing_months, monthly_allowance_tokens

KIND = "cloud_tier_subscription"
SYMBOL = {"USD": "US$"}  # how the table writes a currency before its amount; others are written as their code
WORD = {"month": "a month", "term": "a term", "year": "a year"}


class PlanRefused(ValueError):
    """The table does not offer this: an unknown plan, a paid period the band does not sell, or too many students."""


@dataclass(frozen=True)
class Quote:
    """The band's price for one plan and paid period, in the band's currency (whole units)."""

    plan: PlanId
    band: Band
    billing: Billing
    country: str
    currency: str
    amount: int  # the total for the period and its students
    seats: int
    months: int

    def label(self) -> str:
        """As the checkout shows it: "NGN 9,000 a month", "US$96 a year"."""
        symbol = SYMBOL.get(self.currency)
        price = f"{symbol}{self.amount:,}" if symbol else f"{self.currency} {self.amount:,}"
        return f"{price} {WORD[self.billing]}"


def band_for(config: GatewayConfig, country: str) -> Band:
    """The price band of a billing country (ISO 3166 alpha-2): Nigeria, the purchasing-power band or international."""
    code = country.strip().upper()
    if len(code) != 2 or not code.isalpha():
        raise PlanRefused(f"{country!r} is not a billing country code")
    if code in config.plans.nigeria_countries:
        return "nigeria"
    if code in config.plans.purchasing_power_countries:
        return "purchasing_power"
    return "international"


def offers(config: GatewayConfig, country: str) -> list[Quote]:
    """Every plan and paid period the billing country's band sells, one student each, in TABLE BIZ-18a's order."""
    band = band_for(config, country)
    return [quote(config, country, plan_id, billing)
            for plan_id, plan in config.plans.paid.items() for billing in plan.prices[band]]  # fmt: skip


def _plan(config: GatewayConfig, plan: str) -> Plan | None:
    return config.plans.paid.get(cast(PlanId, plan))


def quote(config: GatewayConfig, country: str, plan: str, billing: str, seats: int = 1) -> Quote:
    """The band's price for `plan` over `billing` for `seats` students; PlanRefused if the table does not sell it."""
    band = band_for(config, country)
    chosen = _plan(config, plan)
    if chosen is None:
        raise PlanRefused(f"{plan!r} is not a plan (TABLE BIZ-18a)")
    offer = chosen.prices[band].get(cast(Billing, billing))
    if offer is None:
        raise PlanRefused(f"{chosen.name} is not sold {WORD.get(billing, billing)} in the {band} band")
    if not 1 <= seats <= chosen.max_students:
        raise PlanRefused(f"{chosen.name} covers 1 to {chosen.max_students} students")
    amount = offer.amount * (seats if chosen.priced_per == "student" else 1)
    period = cast(Billing, billing)
    return Quote(cast(PlanId, plan), band, period, country.strip().upper(), offer.currency, amount, seats,
                 billing_months(config, period))  # fmt: skip


def entitlement_terms(config: GatewayConfig, bought: Quote, *, subscription_id: str, licence_id: str,
                      period_start: str, created_at: str,
                      profile_ids: list[str] | None = None) -> dict[str, Any]:  # fmt: skip
    """The unsigned `CloudTierEntitlement` for a paid quote: the table's features and limits for its plan, the one
    allowance, and a paid period of the quote's months. The checkout signs it with a licence key (SEC-12)."""
    plan = config.plans.paid[bought.plan]
    start = _moment(period_start)
    document: dict[str, Any] = {
        "kind": KIND, "subscription_id": subscription_id, "licence_id": licence_id,
        "period_start": period_start, "period_end": _iso(add_months(start, bought.months)),
        "plan": bought.plan, "band": bought.band, "billing": bought.billing, "seats": bought.seats,
        "allowance_tokens": monthly_allowance_tokens(config), "features": list(plan.features),
        "limits": plan.limits.granted(), "created_at": created_at,
    }  # fmt: skip
    if profile_ids is not None:
        if len(profile_ids) > bought.seats:
            raise PlanRefused("more profiles than the students paid for")
        document["profile_ids"] = list(profile_ids)
    return document


def check_terms(config: GatewayConfig, plan: str, seats: int) -> str | None:
    """Why a signed entitlement covers more students than its plan sells (TABLE BIZ-18a), else None. Prices and
    limits are never rechecked: the table may have changed since the checkout signed it."""
    chosen = _plan(config, plan)
    if chosen is None:
        return "the entitlement names no plan of TABLE BIZ-18a"
    if seats > chosen.max_students:
        return f"{chosen.name} covers at most {chosen.max_students} students"
    return None


def _moment(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
