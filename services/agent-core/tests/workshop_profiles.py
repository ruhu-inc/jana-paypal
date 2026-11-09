import json
from api_harness import cli


def student(name: str, age_band: str = "18_plus") -> dict:
    return {"display_name": name, "avatar": "disc-teal", "level": "O", "subjects": ["physics"], "age_band": age_band}


class Api:
    """A thin client: the launch token always, plus a profile token when given. There is no other token (D86)."""

    def __init__(self, r) -> None:
        self.r = r

    async def call(self, method: str, path: str, body=None, profile: str | None = None, headers: dict | None = None):
        sent = self.r.headers(mutating=method != "GET")
        if method != "GET":
            sent["idempotency-key"] = cli()
        if profile:
            sent["x-profile-token"] = profile
        sent.update(headers or {})
        return await self.r.client.request(method, f"{self.r.base}{path}", headers=sent,
                                           content=None if body is None else json.dumps(body))  # fmt: skip

    async def add(self, body: dict) -> str:
        """Add a student (launch token only, D86)."""
        reply = await self.call("POST", "/v1/profiles", body)
        assert reply.status_code == 200, reply.text
        return reply.json()["profile_id"]

    async def unlock(self, profile_id: str):
        """A tap in the picker (UXS-09): no PIN."""
        return await self.call("POST", f"/v1/profiles/{profile_id}/open", {})

    async def token(self, profile_id: str) -> str:
        reply = await self.unlock(profile_id)
        assert reply.status_code == 200, reply.text
        return str(reply.json()["profile_token"])
