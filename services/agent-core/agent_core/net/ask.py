"""Ask anything's transport (STU-20, D117): one request to the gateway's `/v1/ask`, through the pinned,
consent-gated egress client.

Ask anything is part of the paid online plan (D121): it rides the cloud tier's egress scope (`cloud_tier`, payload
class `cloud_prompt`) and its gates, so it runs only when the cloud tier is in effect for the profile (the switch,
an active subscription naming the profile, the profile's `cloud_tier` consent and its first-use notice). The free
plan's small online taste and the fair-use limits are the entitlement's (BIZ-18, M45.e); the gateway answers
`allowance_exhausted` when either is used up."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_core.net.client import NetError
from agent_core.net.policy import EgressRefused
from agent_core.net.redaction import Redactor

PATH = "/v1/ask"
# Why Ask anything cannot run now, as the app shows it: offline, no online plan, or the profile's consent missing.
REASONS = ("offline", "needs_online_plan", "consent_required", "notice_required", "allowance_exhausted",
           "model_unavailable", "safety_blocked", "insufficient_evidence", "processing_failed")  # fmt: skip


class AskCloudError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class AskCloud:
    def __init__(self, layer: Any, profile: Callable[[], str | None], connection: Callable[[], Any]):
        self.layer, self.profile, self.connection = layer, profile, connection

    def unavailable(self) -> str | None:
        """Why a question cannot go now, or None when it can (checked before anything is sent)."""
        pid, conn = self.profile(), self.connection()
        if pid is None or conn is None:
            return "consent_required"
        if "cloud_tier" not in self.layer.features_on() or not self.layer.cloud_effective(pid, conn, "ask"):
            return "needs_online_plan"
        if self.layer.notice_answer(pid) != "accepted":
            return "notice_required"
        if not self.layer.usable():
            return "offline"
        return None

    async def ask(self, body: dict[str, Any], identifying: tuple[str, ...]) -> Any:
        reason = self.unavailable()
        if reason is not None:
            raise AskCloudError(reason)
        if Redactor(identifying).blocks(body):
            raise AskCloudError("safety_blocked")
        pid = self.profile()
        try:
            token = await self.layer.credential.token()
            if self.unavailable() is not None:  # token renewal may have yielded through a profile or consent change
                raise AskCloudError("consent_required")
            reply = await self.layer.client.send(
                "cloud_tier",
                "POST",
                "https://" + self.layer.host_for("cloud_tier") + PATH,
                "cloud_prompt",
                body=body,
                headers={"authorization": "Bearer " + token},
                profile_id=pid,
                timeout_s=90,
            )
        except EgressRefused as error:
            raise AskCloudError("offline" if error.reason == "offline" else "consent_required") from None
        except NetError:
            raise AskCloudError("offline") from None
        if not 200 <= reply.status < 300:
            code = reply.body.get("error", {}).get("code", "") if isinstance(reply.body, dict) else ""
            raise AskCloudError(code if code in REASONS else "processing_failed")
        return reply.body
