"""Vertex Gemini using the attached Compute identity; no exported key or disk token cache.

Only Google's fixed metadata endpoint supplies short-lived access tokens. Provider destinations are validated
before credentials are obtained. The Gemini wire translation and inline-only material handling stay shared.
"""

from __future__ import annotations

import asyncio
import math
import re
import time

import httpx

from cloud_gateway.config import ProviderSpec
from cloud_gateway.custody import Secret
from cloud_gateway.providers.base import UpstreamError
from cloud_gateway.providers.gemini import GeminiAdapter

METADATA_URL = "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"
ENDPOINT = re.compile(
    r"https://(?P<host>[^/]+)/v1/"
    r"projects/(?P<project>[a-z][a-z0-9-]{4,61}[a-z0-9])/locations/(?P<location>global|eu|us|[a-z]+-[a-z]+[0-9])/publishers/google"
)


def vertex_scope(base_url: str) -> tuple[str, str]:
    """Validate the credential destination and return its project/location for retention binding."""
    match = ENDPOINT.fullmatch(base_url)
    location = match["location"] if match else ""
    host = (
        "aiplatform.googleapis.com"
        if location == "global"
        else f"aiplatform.{location}.rep.googleapis.com"
        if location in ("eu", "us")
        else f"{location}-aiplatform.googleapis.com"
    )
    if match is None or match["host"] != host:
        raise ValueError("Vertex endpoint must be a Google publisher endpoint")
    return match["project"], location


class VertexAdapter(GeminiAdapter):
    def __init__(self, spec: ProviderSpec, key: Secret | None, client: httpx.AsyncClient) -> None:
        vertex_scope(spec.base_url)
        super().__init__(spec, None, client)
        self._token: Secret | None = None
        self._expires = 0.0
        self._refresh = asyncio.Lock()

    async def _request_headers(self) -> dict[str, str]:
        async with self._refresh:
            if self._token is None or time.monotonic() >= self._expires:
                try:
                    response = await self.client.get(
                        METADATA_URL,
                        headers={"Metadata-Flavor": "Google"},
                        timeout=5,
                    )
                    response.raise_for_status()
                    data = response.json()
                    token, lifetime = data["access_token"], float(data["expires_in"])
                    if (
                        not isinstance(token, str)
                        or not token
                        or not math.isfinite(lifetime)
                        or not 60 < lifetime <= 86400
                    ):
                        raise ValueError("invalid metadata token")
                    self._token = Secret(token)
                    self._expires = time.monotonic() + lifetime - 60
                except (httpx.HTTPError, ValueError, TypeError, KeyError):
                    # Metadata response bodies and credentials never enter exceptions or logs.
                    raise UpstreamError("upstream_auth", "runtime identity unavailable") from None
            return {"Authorization": "Bearer " + self._token.reveal(), "content-type": "application/json"}
