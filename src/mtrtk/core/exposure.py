"""Network exposure helpers: which local IP the caster and web UI should bind to."""

from __future__ import annotations

import socket

import psutil

TAILSCALE_IFACE = "tailscale0"


def tailscale_ipv4() -> str | None:
    """IPv4 address of the tailscale0 interface, or None when Tailscale is not up."""
    addrs = psutil.net_if_addrs().get(TAILSCALE_IFACE, [])
    for addr in addrs:
        if addr.family == socket.AF_INET:
            return str(addr.address)
    return None
