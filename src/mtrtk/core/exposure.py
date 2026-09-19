"""Network exposure helpers: which local IP the caster and web UI should bind to."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket

import psutil

log = logging.getLogger(__name__)

TAILSCALE_IFACE = "tailscale0"
BIND_ANY = "0.0.0.0"  # every interface: only ever chosen by the lan/all modes


def tailscale_ipv4() -> str | None:
    """IPv4 address of the tailscale0 interface, or None when Tailscale is not up."""
    addrs = psutil.net_if_addrs().get(TAILSCALE_IFACE, [])
    for addr in addrs:
        if addr.family == socket.AF_INET:
            return str(addr.address)
    return None


def resolve_bind(mode: str) -> str | None:
    """Turn a bind mode from settings into a host to listen on. None = not available yet."""
    if mode == "tailscale":
        return tailscale_ipv4()
    if mode in ("lan", "all"):
        return BIND_ANY
    try:
        ipaddress.ip_address(mode)
    except ValueError as exc:  # Settings validates this; a caller that bypasses it should know
        raise ValueError(f"bind mode {mode!r} is neither a mode name nor an IP address") from exc
    return mode


async def wait_for_bind(mode: str, stop: asyncio.Event, retry_s: float = 5.0) -> str | None:
    """Retry until the bind address exists. Never falls back to 0.0.0.0 for tailscale."""
    attempts = 0
    while not stop.is_set():
        host = resolve_bind(mode)
        if host is not None:
            return host
        if attempts % max(1, int(60 / retry_s)) == 0:
            log.warning(
                "bind mode %r not available yet (is tailscaled running?); retrying every %.0fs",
                mode,
                retry_s,
            )
        attempts += 1
        await sleep_or_stop(stop, retry_s)
    return None


async def sleep_or_stop(stop: asyncio.Event, delay: float) -> None:
    """Wait *delay* seconds, but wake at once when *stop* is set, so shutdown is not held up."""
    sleeper = asyncio.ensure_future(asyncio.sleep(delay))
    stopped = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({sleeper, stopped}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        sleeper.cancel()
        stopped.cancel()
        await asyncio.gather(sleeper, stopped, return_exceptions=True)
