"""Neutral research search results and service contract; language-model selection is independent."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from cloud_gateway.config import Price, SearchEndpoint
from cloud_gateway.custody import Secret
from cloud_gateway.errors import classify_upstream
from cloud_gateway.providers.base import UpstreamError, Usage
from cloud_gateway.providers.http import upstream_error
from cloud_gateway.quotas import cost_usd


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""


@dataclass(frozen=True)
class SearchReply:
    text: str
    queries: tuple[str, ...]
    results: tuple[SearchResult, ...]
    searches: int
    usage: Usage | None
    status: str


class SearchBackend(Protocol):
    @property
    def configured(self) -> bool: ...

    async def search(self, instructions: str, query: str) -> SearchReply: ...

    async def aclose(self) -> None: ...


def search_cost(reply: SearchReply, price: Price | None, search_usd_per_1000: float) -> float | None:
    if reply.usage is None:
        return None
    return cost_usd(price, reply.usage) + reply.searches * search_usd_per_1000 / 1000


class JsonSearch:
    """POST {query, limit} -> {results:[{url,title,snippet}]} to a configured search service.

    One request is one priced search. Credentials go in a header. Only approved sub-questions are sent; the
    research brief, learning history and photos stay out. Result URLs still pass the gateway's source/SSRF policy.
    """

    def __init__(
        self,
        spec: SearchEndpoint | None,
        key: Secret | None,
        client: httpx.AsyncClient,
        clock: Callable[[], float],
        timeout_s: float,
    ) -> None:
        self.spec, self._key, self.client, self.clock = spec, key, client, clock
        self.timeout = httpx.Timeout(timeout_s, connect=5)

    @property
    def configured(self) -> bool:
        spec = self.spec
        return bool(
            spec
            and spec.permission
            and spec.permission.permits(self.clock())
            and (spec.secret is None or self._key is not None)
        )

    async def search(self, instructions: str, query: str) -> SearchReply:
        try:
            async with asyncio.timeout(self.timeout.read):
                return await self._search(query)
        except TimeoutError:
            raise UpstreamError("timeout", "search deadline exceeded") from None

    async def _search(self, query: str) -> SearchReply:
        spec = self.spec
        if spec is None or not self.configured:
            raise UpstreamError(
                "overloaded", "search permission or configuration unavailable", extra={"unbilled": True}
            )
        headers = {"content-type": "application/json"}
        if self._key is not None:
            headers["authorization"] = "Bearer " + self._key.reveal()
        try:
            async with self.client.stream(
                "POST",
                spec.url,
                headers=headers,
                json={"query": query[:2000], "limit": spec.max_results},
                timeout=self.timeout,
            ) as response:
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > spec.max_response_bytes:
                        raise UpstreamError("bad_response", "search response exceeds its limit")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                if response.status_code >= 400:
                    raise upstream_error(response.status_code, raw, response.headers, classify_upstream)
        except httpx.HTTPError as exc:
            reason = "timeout" if isinstance(exc, httpx.TimeoutException) else "network_unavailable"
            raise UpstreamError(reason, "search transport failed") from None
        import json

        try:
            data = json.loads(raw)
            rows = data["results"]
            if not isinstance(rows, list):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise UpstreamError("bad_response", "invalid search response") from None
        found = []
        for row in rows[: spec.max_results]:
            if not isinstance(row, dict) or not isinstance(row.get("url"), str):
                continue
            try:
                parts = urlsplit(row["url"])
            except ValueError:
                continue
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username
                or parts.password
                or len(row["url"]) > 2048
            ):
                continue
            found.append(SearchResult(str(row.get("title", ""))[:300], row["url"], str(row.get("snippet", ""))[:2000]))
        return SearchReply("", (query[:2000],), tuple(found), 1, Usage(), "completed")

    async def aclose(self) -> None:
        await self.client.aclose()
