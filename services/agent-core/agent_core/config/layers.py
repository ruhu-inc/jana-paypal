"""Configuration layers, lowest to highest precedence (TABLE ARC-27a)."""

from __future__ import annotations

from enum import IntEnum


class Layer(IntEnum):
    DEFAULTS = 1  # built-in defaults, every proposal value (config/defaults.toml in the signed bundle)
    HARDWARE = 2  # hardware class profile (config/hardware/<class>.toml)
    ROUTER = 3  # router and roles (router YAML)
    LICENCE = 4  # device licence entitlements (signed licence file)
    ONLINE_FEATURES = 5  # online-feature switches with their consents only (ARC-11, D53, D86)
    PROFILE = 6  # profile settings: student preferences
    DEVELOPER = 7  # JANA_* environment and dev.toml; development builds only
    SAFETY = 8  # safety configuration: restrict-only (signed safety configuration file)
