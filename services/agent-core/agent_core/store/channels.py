"""Release channels (PLT-16, TABLE OPS-08a): every app, agent bundle, content pack and model pack release carries one.

A device's channel is set by its licence (`stable` by default; there is no adult channel setting, D53). A device
accepts artefacts of its own channel or a more stable one: a `stable` device takes only `stable`, a `pilot` device
takes `pilot` and `stable`, a team device on `dev` takes all three.

D84: the free offline product needs no licence. A device without one is a `stable` device and receives every stable
pack, model and update from download or USB; nothing in packaging, packs or updates refuses for want of a licence.
"""

from __future__ import annotations

CHANNELS = ("dev", "pilot", "stable")  # least to most stable
DEFAULT_CHANNEL = "stable"


def channel_accepts(device_channel: str, artefact_channel: str) -> bool:
    if device_channel not in CHANNELS or artefact_channel not in CHANNELS:
        return False
    return CHANNELS.index(artefact_channel) >= CHANNELS.index(device_channel)
