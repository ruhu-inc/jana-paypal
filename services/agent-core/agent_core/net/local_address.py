"""The only addresses that name this device (MDL-01, ARC-18, VOC-23): `127.0.0.1` and `::1`, written as addresses.

A name such as `localhost` resolves through the hosts file and the resolver, which another program can point
elsewhere, and other 127/8 addresses are not where any Jana service binds; both are refused. No network import here,
so the speech sidecar and the voice transport can share the check."""

from __future__ import annotations

import ipaddress

LOOPBACK_ADDRESSES = frozenset({ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")})


def is_literal_loopback(host: str) -> bool:
    """`127.0.0.1` or `::1` written as an address; a name (even `localhost`) or a zone-scoped address is not."""
    try:
        return ipaddress.ip_address(host) in LOOPBACK_ADDRESSES
    except ValueError:
        return False
