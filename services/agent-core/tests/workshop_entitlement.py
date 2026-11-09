from typing import Any
from signing_service.hierarchy import dev_signer
from signing_service.keys import Purpose


def entitlement(profiles: list[str], *, allowance: int = 1_800_000, seats: int = 2) -> dict[str, Any]:
    document = {"kind": "cloud_tier_subscription", "subscription_id": "sub_test_0001", "licence_id": "lic_test",
                "period_start": "2026-09-01T00:00:00.000Z", "period_end": "2099-01-01T00:00:00.000Z",
                "seats": seats, "allowance_tokens": allowance, "profile_ids": profiles,
                "plan": "jana_online", "band": "nigeria", "billing": "month", "features": ["cloud_tier"], "limits": {},
                "created_at": "2026-10-01T00:00:00.000Z"}  # fmt: skip
    return dev_signer(Purpose.LICENCE).sign("licence_file", document, now="2026-10-01T00:00:00.000Z")
