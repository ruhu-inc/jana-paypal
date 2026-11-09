"""agent_core.config: typed, layered, validated configuration (ARC-27, ARC-28) and environments (OPS-35)."""

from agent_core.config.environments import Environment, environment_policy
from agent_core.config.keys import Settings
from agent_core.config.layers import Layer
from agent_core.config.resolve import (
    ConfigError,
    Resolved,
    Source,
    WriteResult,
    check_write,
    environment_source,
    resolve,
    toml_source,
)

__all__ = [
    "ConfigError",
    "Environment",
    "Layer",
    "Resolved",
    "Settings",
    "Source",
    "WriteResult",
    "check_write",
    "environment_policy",
    "environment_source",
    "resolve",
    "toml_source",
]
