"""Optional single-password protection for the API and WebSocket."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, status
from pydantic import BaseModel

COOKIE_NAME = "mtrtk_session"
_TOKEN_MESSAGE = b"mtrtk-session"


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
    return hmac.compare_digest(presented, session_token(password))


async def require_auth(request: Request) -> None:
    password = request.app.state.ctx.settings.web_password
    presented = _presented_token(
        {k.lower(): v for k, v in request.headers.items()}, request.cookies
    )
    if not token_ok(password, presented):
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


router = APIRouter(prefix="/api", tags=["auth"])


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response) -> dict[str, str]:
    password = request.app.state.ctx.settings.web_password
    if not password:
        return {"token": ""}
    if not hmac.compare_digest(body.password.encode(), password.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong password")
    token = session_token(password)
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=30 * 86400)
    return {"token": token}


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


AuthDep = Depends(require_auth)
