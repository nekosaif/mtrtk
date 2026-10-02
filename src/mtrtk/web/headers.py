"""Defensive response headers, and whether a request reached us over HTTPS.

The daemon sets them itself rather than leaving them to Caddy, so the Cloudflare Tunnel path and
a direct tailnet bind get them too: nothing may frame the UI (clickjacking), no response is
content-sniffed, and no URL leaks in a Referer. HSTS goes only on a response to a request that
came in over HTTPS - through Caddy or cloudflared, both of which say `X-Forwarded-Proto: https`.
A client that spoofs that header only gets a stricter answer, so it is safe to believe.
"""

from __future__ import annotations

from collections.abc import Iterable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

HSTS = "max-age=31536000"
DEFENSIVE_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
    (b"x-frame-options", b"DENY"),  # for browsers that predate frame-ancestors
    (b"referrer-policy", b"no-referrer"),
)


def is_https(scheme: str, headers: Iterable[tuple[str, str]]) -> bool:
    """The request was HTTPS end to end as far as the browser is concerned."""
    if scheme in ("https", "wss"):
        return True
    for name, value in headers:
        if name.lower() == "x-forwarded-proto":
            return value.split(",", 1)[0].strip().lower() in ("https", "wss")
    return False


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        https = is_https(
            scope.get("scheme", "http"),
            ((k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers", ())),
        )

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                present = {name.lower() for name, _ in headers}
                extra = list(DEFENSIVE_HEADERS)
                if https:
                    extra.append((b"strict-transport-security", HSTS.encode()))
                headers += [(n, v) for n, v in extra if n not in present]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, with_headers)
