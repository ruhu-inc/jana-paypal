"""The cloud gateway: the only way the device reaches cloud models (ARC-12).

It holds the provider keys (SEC-20), authenticates devices with challenge-signed short-lived tokens (SEC-21),
enforces quotas, refuses requests that carry profile data (MDL-25, PRV-25), translates the neutral OpenAI-shaped
request for the provider a config slot names (MDL-26) and stores per-request metadata only (OBS-22)."""
