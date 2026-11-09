"""Provider adapters behind one interface, chosen by config (MDL-26)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx

from cloud_gateway.config import GatewayConfig, ModelSpec, ProviderSpec, Sam, SearchEndpoint, Segmentation
from cloud_gateway.custody import Secret, SecretSource
from cloud_gateway.providers.base import ProviderAdapter
from cloud_gateway.providers.gemini import GeminiAdapter
from cloud_gateway.providers.meta import MetaAdapter
from cloud_gateway.providers.openai_compat import OpenAICompatAdapter
from cloud_gateway.providers.policy import PermittedAdapter
from cloud_gateway.providers.vertex import VertexAdapter

if TYPE_CHECKING:
    from cloud_gateway.inference_budget import InferenceBudget

ClientFactory = Callable[[str, ProviderSpec | Sam | SearchEndpoint | Segmentation], httpx.AsyncClient]
ADAPTERS: dict[str, Callable[[ProviderSpec, Secret | None, httpx.AsyncClient], ProviderAdapter]] = {
    "openai_compat": OpenAICompatAdapter,
    "meta": MetaAdapter,
    "gemini": GeminiAdapter,
    "vertex": VertexAdapter,
}


def default_client(name: str, spec: ProviderSpec | Sam | SearchEndpoint | Segmentation) -> httpx.AsyncClient:
    # TLS with certificate validation, no redirects, no proxy or netrc credentials from the environment (SEC-31).
    return httpx.AsyncClient(follow_redirects=False, trust_env=False)


class Providers:
    """The configured adapters by provider name, and slot/alias resolution for device model ids."""

    def __init__(
        self,
        config: GatewayConfig,
        secrets: SecretSource,
        client_factory: ClientFactory = default_client,
        *,
        budget: InferenceBudget | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.clock = clock
        self.adapters: dict[str, ProviderAdapter] = {
            name: ADAPTERS[spec.adapter](spec, secrets.get(spec.secret), client_factory(name, spec))
            for name, spec in config.providers.items()
            if (
                name in config.slots.values()
                and spec.permission is not None
                and spec.permission.permits(clock())
                and any(model.id in config.prices for model in spec.models.values())
            )
        }
        if budget is not None:
            from cloud_gateway.inference_budget import BudgetedAdapter

            self.adapters = {name: BudgetedAdapter(adapter, budget) for name, adapter in self.adapters.items()}
        self.adapters = {
            name: PermittedAdapter(adapter, config.providers[name], clock, priced_models=frozenset(config.prices))
            for name, adapter in self.adapters.items()
        }

    def has_model(self, model: object) -> bool:
        if not isinstance(model, str) or model.count("/") != 1:
            return False
        slot, alias = model.split("/")
        for candidate in (slot, "fallback") if slot == "primary" else (slot,):
            declaration = self.config.providers.get(self.config.slots.get(candidate, ""))
            if declaration is not None and alias in declaration.models:
                return True
        return False

    def resolve(
        self, model: object, *, required: frozenset[str] = frozenset({"text"})
    ) -> tuple[str, ProviderAdapter, ModelSpec] | None:
        """`<slot>/<alias>` to the provider name, its adapter and the concrete model, or None if not configured."""
        if not isinstance(model, str) or model.count("/") != 1:
            return None
        slot, alias = model.split("/")
        # Only an explicitly configured fallback is considered; no request is sent to a guessed provider/model.
        for candidate in (slot, "fallback") if slot == "primary" else (slot,):
            provider = self.config.slots.get(candidate)
            if provider is None or provider not in self.adapters:
                continue
            declaration = self.config.providers[provider]
            if declaration.permission is None or not declaration.permission.permits(self.clock()):
                continue
            spec = declaration.models.get(alias)
            if spec is not None and spec.id in self.config.prices and required <= spec.capabilities:
                return provider, self.adapters[provider], spec
        return None

    async def aclose(self) -> None:
        for adapter in self.adapters.values():
            await adapter.aclose()
