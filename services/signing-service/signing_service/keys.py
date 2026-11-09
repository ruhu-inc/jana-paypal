"""Ephemeral development keys for this process. Never shared between installations."""
from __future__ import annotations

from functools import cache

from agent_core.config.trust import Purpose
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DEV_KEY_ID = "key_dev_test"  # the content intermediate of the test root
DEV_ROOT_KEY_ID = "key_root_test"
DEV_KEY_IDS: dict[Purpose, str] = {
    Purpose.CONTENT: DEV_KEY_ID,
    Purpose.MODEL: "key_dev_model",
    Purpose.AGENT_BUNDLE: "key_dev_agent_bundle",
    Purpose.UPDATER: "key_dev_updater",
    Purpose.LICENCE: "key_dev_licence",
}

@cache
def dev_key(purpose: Purpose = Purpose.CONTENT) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@cache
def dev_root_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()
