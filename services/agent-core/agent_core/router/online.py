"""The cloud tier for one session (ARC-12, MDL-24 to MDL-26, MDL-29, BIZ-07).

`cloud_session` gives a session's router what the cloud tier needs: one gateway provider per configured slot
(`primary`, `fallback`), each with its own per-session pseudonym and the session's redaction terms; the connectivity
state the router reads before choosing a cloud candidate; and the cost budget metered on the device. The engine wires
these into its router; the connectivity layer itself (`agent_core.net.layer.OnlineLayer`) is the device's."""

from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from functools import partial

from agent_core.config import Settings
from agent_core.net.layer import OnlineLayer
from agent_core.net.redaction import Redactor
from agent_core.providers.gateway import GatewayProvider, cloud_capabilities
from agent_core.router.budget import CloudBudget, MemorySpend, SpendStore

SLOTS = ("primary", "fallback")
# While offline, a turn the cloud would have served asks for a probe once the last check is this old (W3, MDL-24).
RECHECK_AFTER_S = 10.0

__all__ = ["SLOTS", "CloudSession", "OnlineLayer", "cloud_session"]


@dataclass
class CloudSession:
    providers: dict[str, GatewayProvider]
    budget: CloudBudget
    network: Callable[[], str]
    recheck: Callable[[], Awaitable[str]] | None = None  # probe now and return the state (a stalled cloud call)
    recheck_soon: Callable[[], None] | None = None  # probe soon while offline (a turn the cloud would have served)


def today() -> str:
    return datetime.datetime.now(datetime.UTC).date().isoformat()


def cloud_session(
    layer: OnlineLayer,
    settings: Settings,
    *,
    profile_id: str | None,
    terms: Iterable[str] = (),
    learner: Callable[[], sqlite3.Connection | None] | None = None,
    spend: SpendStore | None = None,
    allowance_tokens: int | None = None,
    on_reminder: Callable[[str, float], None] | None = None,
) -> CloudSession:
    from agent_core.net.catalogue import models as deployment_models

    slots = deployment_models(layer.deployment(), layer.clock_ms())
    # Gateway fallback can use the other explicit slot. Hold the higher declared rate on the device;
    # gateway billing still settles actual usage, so a fallback cannot silently overrun the device budget.
    prices = {
        alias: (
            max(v["price_in_per_mtok"] for aliases in slots.values() if (v := aliases.get(alias))),
            max(v["price_out_per_mtok"] for aliases in slots.values() if (v := aliases.get(alias))),
        )
        for aliases in slots.values()
        for alias in aliases
    }

    def model_allowed(slot: str, alias: str) -> bool:
        return alias in deployment_models(layer.deployment(), layer.clock_ms()).get(slot, {})

    redactor = Redactor(terms)
    providers = {
        slot: GatewayProvider(
            layer.client,
            lambda: layer.host_for("cloud_tier"),
            slot,
            token=layer.credential.token,
            allowed=(lambda service: layer.service_allowed(learner(), service)) if learner else (lambda _: False),
            strict_models=True,
            model_allowed=partial(model_allowed, slot),
            caps={
                alias: cloud_capabilities(v["context"], v["max_output"], "vision" in v["capabilities"])
                for alias, v in aliases.items()
                if {"text", "structured_output", "tool_calls", "streaming"} <= set(v["capabilities"])
            },
            prices={alias: prices[alias] for alias in aliases},
            redactor=redactor,
            profile_id=profile_id,
            on_network_failure=layer.network_failure,
        )
        for slot, aliases in slots.items()
    }
    budget = CloudBudget(settings.cloud_budget_session_usd, settings.cloud_budget_student_day_usd,
                         tuple(settings.cloud_budget_reminders), spend or MemorySpend(), today,
                         allowance_tokens=allowance_tokens, on_reminder=on_reminder)  # fmt: skip
    connectivity = layer.connectivity
    return CloudSession(providers, budget, lambda: connectivity.state, connectivity.check,
                        lambda: connectivity.recheck_soon(RECHECK_AFTER_S))  # fmt: skip
