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
# How often a `tailscale` bind checks that tailscale0 still carries the address it listens on.
REBIND_CHECK_S = 5.0


def tailscale_ipv4() -> str | None:
    """IPv4 address of the tailscale0 interface, or None when Tailscale is not up."""
    addrs = psutil.net_if_addrs().get(TAILSCALE_IFACE, [])
    for addr in addrs:
        if addr.family == socket.AF_INET:
            return str(addr.address)
    return None


def url_host(host: str) -> str:
    """A host as it must appear in a URL: an IPv6 literal is bracketed, everything else is not.

    `http://fd7a:115c:a1e0::1:8080/healthz` has no port in it as far as any URL parser is
    concerned - the colons run together - so the healthcheck would ask for the wrong thing and
    the bind log line would print an address nobody can paste into a browser.
    """
    if host.startswith("["):
        return host  # already bracketed
    try:
        ipaddress.IPv6Address(host)
    except ValueError:
        return host  # an IPv4 address or a name: nothing to bracket
    return f"[{host}]"


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


async def wait_for_rebind(
    mode: str, host: str, stop: asyncio.Event, check_s: float = REBIND_CHECK_S
) -> str | None:
    """Wait until *mode* resolves to an address other than *host*, and return that address.

    Only `tailscale` can move. tailscaled restores its cached state at boot, so a daemon that
    starts before the new netmap arrives binds the node's *old* tailnet address - a socket that
    stays in LISTEN on an address nothing routes to any more. The caller re-binds on what this
    returns. An interface that is briefly empty (tailscaled restarting) is no address to move
    to: the wait goes on, and the same address coming back needs nothing, because a socket bound
    to it works again as soon as it is reassigned. Never 0.0.0.0: `resolve_bind("tailscale")`
    only ever answers with tailscale0's own address. None means `stop` was set.
    """
    if mode != "tailscale":
        await stop.wait()  # lan/all/an explicit IP: the same address for the life of the process
        return None
    while not stop.is_set():
        await sleep_or_stop(stop, check_s)
        if stop.is_set():
            break
        try:
            # In a thread: psutil walks every interface and every address on it (dozens of veths
            # on a Docker host), every few seconds for the life of the process, and the loop it
            # would block also carries the RTCM fan-out.
            new = await asyncio.to_thread(resolve_bind, mode)
        except OSError as exc:  # psutil reads netlink; one bad read is not a moved address
            log.debug("could not read %s: %s", TAILSCALE_IFACE, exc)
            continue
        if new is not None and new != host and new != BIND_ANY:
            return new
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
