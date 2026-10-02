"""Optional single-password protection for the API and WebSocket."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, status
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from mtrtk.web.headers import is_https

COOKIE_NAME = "mtrtk_session"
_TOKEN_MESSAGE = b"mtrtk-session"
# Failed logins are throttled for the whole daemon, not per address: behind Caddy or the tunnel
# every caller is 127.0.0.1. Each failure answers only after a delay, and a burst of failures
# empties a small bucket that refills one attempt every LOGIN_REFILL_S; while it is empty a login
# is refused with 429 before the password is looked at, so a guess learns nothing. Sessions that
# already exist are not affected; a fresh login waits out the attack.
FAILED_LOGIN_DELAY_S = 1.0
LOGIN_BURST = 10
LOGIN_REFILL_S = 6.0
TOO_MANY_LOGINS = "too many failed logins: try again in {s} s"


def _now() -> float:
    return time.monotonic()


class LoginThrottle:
    """A token bucket of failed-login attempts (see `LOGIN_BURST`)."""

    def __init__(self) -> None:
        self.tokens = float(LOGIN_BURST)
        self.stamp = _now()

    def _refill(self) -> None:
        now = _now()
        self.tokens = min(LOGIN_BURST, self.tokens + (now - self.stamp) / LOGIN_REFILL_S)
        self.stamp = now

    def retry_after(self) -> int | None:
        """Seconds until an attempt is allowed again; None when one is allowed now."""
        self._refill()
        if self.tokens >= 1:
            return None
        return max(1, math.ceil((1 - self.tokens) * LOGIN_REFILL_S))

    def failed(self) -> None:
        self._refill()
        self.tokens = max(0.0, self.tokens - 1)


def _throttle(request: Request) -> LoginThrottle:
    throttle = getattr(request.app.state, "login_throttle", None)
    if throttle is None:
        throttle = request.app.state.login_throttle = LoginThrottle()
    return throttle


def session_token(password: str) -> str:
    key = hashlib.sha256(password.encode("utf-8")).digest()
    return hmac.new(key, _TOKEN_MESSAGE, hashlib.sha256).hexdigest()


def _presented_token(headers: dict[str, str], cookies: dict[str, str]) -> str | None:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return cookies.get(COOKIE_NAME)


def token_ok(password: str | None, presented: str | None) -> bool:
    if not password:
        return True
    if not presented:
        return False
    # Compare bytes, not str: Starlette hands headers and cookies over latin-1-decoded, and
    # `compare_digest` raises TypeError on a str holding non-ASCII characters - which would turn
    # `Authorization: Bearer <any high byte>` into an unauthenticated 500 with a traceback.
    # latin-1 maps every byte to U+0000..U+00FF, so it cannot produce a lone surrogate and
    # `surrogateescape` never fires on that path; it stays as belt and braces for a caller that
    # hands us a string from somewhere else, where a plain `.encode()` would raise instead.
    return hmac.compare_digest(
        presented.encode("utf-8", "surrogateescape"), session_token(password).encode("ascii")
    )


def connection_authorized(conn: HTTPConnection, password: str | None) -> bool:
    """The check `require_auth` makes, on anything with headers and cookies - so the body limit
    middleware can make it too, before a byte of the body is read."""
    presented = _presented_token({k.lower(): v for k, v in conn.headers.items()}, conn.cookies)
    return token_ok(password, presented)


async def require_auth(request: Request) -> None:
    if not connection_authorized(request, request.app.state.ctx.settings.web_password):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )


def websocket_authorized(ws: WebSocket) -> bool:
    password = ws.app.state.ctx.settings.web_password
    presented = _presented_token({k.lower(): v for k, v in ws.headers.items()}, ws.cookies)
    if presented is None:
        presented = ws.query_params.get("token")
    return token_ok(password, presented)


class LoginBody(BaseModel):
    password: str


AuthDep = Depends(require_auth)

# The one ungated `/api` router: it carries `/api/login` and nothing else may join it without
# being safe to serve to an anonymous caller. Everything else goes on `protected_router` (or on
# a resource router, which `create_app` gates on the way in).
router = APIRouter(prefix="/api", tags=["auth"])
protected_router = APIRouter(prefix="/api", tags=["auth"], dependencies=[AuthDep])


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response) -> dict[str, str]:
    password = request.app.state.ctx.settings.web_password
    if not password:
        return {"token": ""}
    throttle = _throttle(request)
    wait = throttle.retry_after()
    if wait is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            TOO_MANY_LOGINS.format(s=wait),
            headers={"Retry-After": str(wait)},
        )
    if not hmac.compare_digest(body.password.encode(), password.encode()):
        throttle.failed()  # counted before the delay, so parallel guesses all pay for it
        await asyncio.sleep(FAILED_LOGIN_DELAY_S)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong password")
    token = session_token(password)
    # Secure when the browser came over HTTPS (through Caddy or the tunnel), so the cookie is
    # never offered on a later http:// visit. A plain-HTTP tailnet or LAN bind cannot have it.
    secure = is_https(request.url.scheme, request.headers.items())
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, samesite="lax", secure=secure, max_age=30 * 86400
    )
    return {"token": token}


@protected_router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}
