"""The DNS-rebinding and cross-site WebSocket guard for a UI that has no password.

Without `WEB_PASSWORD` nothing but the network stands between a browser and the API. A page
from any site can point a name it owns at 127.0.0.1 or a tailnet address (DNS rebinding) and then
drive the API as same-origin, and a WebSocket is not subject to CORS at all, so a cross-site page
can read the live state with no rebinding. Both carry the attacker's name: the rebound request in
its `Host`, the WebSocket in its `Origin`. So, with no password:

- a request whose `Host` is a *name* must be one this daemon was given: localhost, this host's
  own name, its MagicDNS name (any `*.ts.net` name when `WEB_BIND=tailscale`), `PUBLIC_DOMAIN`
  or `WEB_ALLOWED_HOSTS`. An IP literal is always accepted: rebinding needs a name the attacker
  controls, never an address.
- a `/ws` handshake that carries an `Origin` must come from the host it is addressed to (or from
  an allowed name, for a proxy that rewrites `Host`). Scripts and the ROS bridge send no Origin.

With a password the session cookie (SameSite=Lax, on this host only) does that job, so the
guard stands aside. `/healthz` answers any name: a monitor may know the box by any of them.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import subprocess
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import anyio
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from mtrtk.config import Settings

POLICY_VIOLATION = 1008
MAGICDNS_TTL_S = 60.0
TAILSCALE_TIMEOUT_S = 2.0
EXEMPT_PATHS = frozenset({"/healthz"})


def host_name(value: str) -> str:
    """The name part of a `Host` header or URL netloc: `[::1]:8080` -> `::1`, `A.b.:80` -> `a.b`."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    if value.count(":") == 1:  # name:port (a bare IPv6 address has more than one colon)
        value = value.split(":", 1)[0]
    return value.rstrip(".")


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def tailscale_dns_names() -> frozenset[str]:
    """This node's MagicDNS name and its short form; empty without a reachable `tailscale`."""
    try:
        out = subprocess.run(
            ["tailscale", "status", "--json", "--peers=false"],
            capture_output=True,
            text=True,
            timeout=TAILSCALE_TIMEOUT_S,
            check=False,
        ).stdout
        name = str(json.loads(out).get("Self", {}).get("DNSName") or "")
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return frozenset()
    name = name.lower().rstrip(".")
    return frozenset({name, name.split(".", 1)[0]}) if name else frozenset()


class AllowedHosts:
    """Which names a password-less daemon answers to. The MagicDNS lookup is cached."""

    def __init__(self, magicdns: Callable[[], frozenset[str]] | None = None) -> None:
        # Looked up at call time by default, so a test can patch the module's function.
        self._magicdns = magicdns or (lambda: tailscale_dns_names())
        self._dns: frozenset[str] = frozenset()
        self._dns_at = -MAGICDNS_TTL_S

    def static_names(self, settings: Settings) -> set[str]:
        names = {"localhost"}
        local = socket.gethostname().lower().rstrip(".")
        if local:
            names |= {local, local.split(".", 1)[0], f"{local.split('.', 1)[0]}.local"}
        if settings.public_domain:
            names.add(settings.public_domain.strip().lower().rstrip("."))
        names.update(settings.web_allowed_hosts)
        if str(settings.web_bind).strip().lower() == "tailscale":
            # Bound only to the tailnet: serve any MagicDNS name. The image has no tailscale CLI
            # to name this node, and public DNS never points a ts.net name at a tailnet address,
            # so a rebinding page cannot use one.
            names.add(".ts.net")
        return names

    @staticmethod
    def _matches(name: str, names: set[str]) -> bool:
        if "*" in names or name in names or name.endswith(".localhost"):
            return True
        return any(n.startswith(".") and (name.endswith(n) or name == n[1:]) for n in names)

    async def allows(self, settings: Settings, host: str) -> bool:
        name = host_name(host)
        if not name or _is_ip(name) or self._matches(name, self.static_names(settings)):
            return True
        # Only a miss pays for the lookup, and it is cached whatever it found.
        now = time.monotonic()
        if now - self._dns_at >= MAGICDNS_TTL_S:
            self._dns = await anyio.to_thread.run_sync(self._magicdns)
            self._dns_at = now
        return name in self._dns

    async def origin_ok(self, settings: Settings, origin: str | None, host: str) -> bool:
        """A WebSocket's `Origin` is the host it addresses, or a name the daemon was given."""
        if origin is None:
            return True  # not a browser: a browser always sends one on a WebSocket
        try:
            netloc = urlsplit(origin).netloc
        except ValueError:
            return False
        name = host_name(netloc)
        if not name:  # `Origin: null` (a sandboxed frame, a file:// page) names no host
            return False
        if name == host_name(host):
            return True
        return not _is_ip(name) and self._matches(name, self.static_names(settings))


def refused_host_detail(host: str) -> str:
    return (
        f"Host {host_name(host)!r} is not a name this daemon answers to without WEB_PASSWORD: "
        "add it to WEB_ALLOWED_HOSTS, or set WEB_PASSWORD"
    )


class HostGuardMiddleware:
    """Refuse, while the UI has no password, a `Host` it was never given (see the module)."""

    def __init__(
        self, app: ASGIApp, settings: Callable[[], Settings], hosts: AllowedHosts | None = None
    ) -> None:
        self.app = app
        self.settings = settings
        self.hosts = hosts or AllowedHosts()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        settings = self.settings()
        if (
            scope["type"] not in ("http", "websocket")
            or settings.web_password
            or scope.get("path", "") in EXEMPT_PATHS
        ):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", ())}
        host = headers.get("host", "")
        allowed = await self.hosts.allows(settings, host)
        if allowed and scope["type"] == "websocket":
            allowed = await self.hosts.origin_ok(settings, headers.get("origin"), host)
        if allowed:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # Before accept: the handshake fails with an HTTP 403.
            await WebSocket(scope, receive, send).close(code=POLICY_VIOLATION)
            return
        body = json.dumps({"detail": refused_host_detail(host)}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
