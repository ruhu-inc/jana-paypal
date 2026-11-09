"""Record identifiers: ULIDs (Crockford base32, time-ordered). The store owns them because every layer that
writes records may import the store, and the store imports no other agent-core module (TABLE ARC-18a)."""

from __future__ import annotations

import secrets
import time

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(now_ms: int | None = None) -> str:
    stamp = now_ms if now_ms is not None else int(time.time() * 1000)
    value = (stamp << 80) | secrets.randbits(80)
    return "".join(CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))
