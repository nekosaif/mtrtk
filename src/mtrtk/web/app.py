"""FastAPI application factory."""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import import_module, resources
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mtrtk import __version__
from mtrtk.web import auth
from mtrtk.web.api.logs import LogIndexMirror
from mtrtk.web.api.system import SystemCache
from mtrtk.web.context import AppContext
from mtrtk.web.ws import WsHub, websocket_endpoint

# Every router the API serves, in the order they are mounted. All ten exist: a module that will
# not import is a bug to be seen, not a panel to be quietly 404ed.
API_MODULES = (
    "status",
    "system",
    "config",
    "receiver",
    "base",
    "ntrip",
    "logs",
    "history",
    "events",
    "jobs",
)
# Paths the SPA must never answer for: an unknown one under these is a real 404, not a client-side
# route. `/assets` is here too - a missing bundle has to look missing, not like the index page.
SERVER_PREFIXES = ("/api", "/ws", "/healthz", "/assets")
NO_UI_DETAIL = "UI not built; run `pnpm --dir web build` or use the Docker image"

# Every `/api` body is a small JSON object - the largest is `PUT /api/config`, one value per
# `Settings` field. A megabyte of it is not a request, and the free-text settings end up in `.env`,
# which the daemon re-reads on every `GET /api/config` and on every restart.
API_BODY_LIMIT = 256 * 1024
TOO_LARGE_DETAIL = f"request body too large: /api accepts at most {API_BODY_LIMIT} bytes"
# How stale the "is the SPA bundle there?" answer may be. The bundle can appear after the process
# starts - a volume mounted late, a build that finished - so "absent" is not remembered for ever;
# but a 404 storm must not become one stat of the card per request either.
INDEX_RECHECK_S = 5.0

_REPEATED_SLASHES = re.compile(r"/{2,}")


def default_static_dir() -> Path:
    return Path(str(resources.files("mtrtk.web") / "static"))


def normalized_path(path: str) -> str:
    """`//api/status` and `/api/status` are one route. Case is left alone: paths are sensitive.

    Proxies and hand-written clients produce doubled slashes, and Starlette routes on the raw
    path - so without this `//api/status` was a miss that fell through to the SPA, and a JSON
    client asking a mistyped URL got `text/html` and a 200 back.
    """
    return _REPEATED_SLASHES.sub("/", path)


def is_api_path(path: str) -> bool:
    """True for the routes the body limit and the JSON 404 apply to."""
    path = normalized_path(path)
    return path == "/api" or path.startswith("/api/")


def is_spa_path(path: str) -> bool:
    """True when a 404 on `path` should hand the SPA its own index to route from."""
    path = normalized_path(path)
    return not any(path == prefix or path.startswith(prefix + "/") for prefix in SERVER_PREFIXES)


class IndexFile:
    """Whether the SPA's `index.html` is on disk, stat'ed at most once every `INDEX_RECHECK_S`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._exists = path.exists()  # once, at `create_app`: the common answer is the first one
        self._checked = time.monotonic()

    def exists(self) -> bool:
        now = time.monotonic()
        if now - self._checked >= INDEX_RECHECK_S:
            self._exists = self.path.exists()
            self._checked = now
        return self._exists


class BodyLimitMiddleware:
    """Refuse an `/api` request body over *limit* bytes with a 413, before anything reads it.

    Pure ASGI rather than `BaseHTTPMiddleware`: a declared `Content-Length` is refused without
    reading a byte, and a chunked body - which declares no length at all - is counted as it
    arrives and cut off the moment it goes over, rather than being buffered whole first.
    """

    def __init__(self, app: ASGIApp, limit: int = API_BODY_LIMIT) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_api_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        if _declared_length(scope) > self.limit:
            await self._refuse(send)
            return
        seen = 0
        refused = False

        async def limited_receive() -> Message:
            nonlocal seen, refused
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                # `not refused`: a reader that keeps pulling past the disconnect must not make us
                # write a second response over the first.
                if seen > self.limit and not refused:
                    refused = True
                    await self._refuse(send)
                    # The app is told the client hung up. Whatever it makes of that - FastAPI
                    # turns the disconnect into its own 400 - is dropped by `guarded_send`.
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            if not refused:
                await send(message)

        await self.app(scope, limited_receive, guarded_send)

    @staticmethod
    async def _refuse(send: Send) -> None:
        body = json.dumps({"detail": TOO_LARGE_DETAIL}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _declared_length(scope: Scope) -> int:
    """The request's `Content-Length`, or 0 when it declares none (or declares nonsense)."""
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return 0
    return 0


def create_app(ctx: AppContext, static_dir: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Own the bus subscribers the API needs: one per process, released on shutdown."""
        cache = SystemCache(ctx.bus)
        cache.start()
        app.state.system_cache = cache
        hub = WsHub(ctx)
        hub.start()
        app.state.ws_hub = hub
        mirror = LogIndexMirror(ctx)
        mirror.start()
        app.state.log_index = mirror
        try:
            yield
        finally:
            # Clear before closing, so a request racing shutdown sees None rather than a
            # half-closed object, and release in the reverse order of creation. Nested, so a
            # close that raises cannot strand the subscriber it was released before.
            app.state.log_index = None
            try:
                await mirror.aclose()
            finally:
                app.state.ws_hub = None
                try:
                    await hub.aclose()
                finally:
                    app.state.system_cache = None
                    await cache.aclose()

    # The docs are built by hand below so that `require_auth` covers them: FastAPI's own
    # `docs_url` / `openapi_url` routes hang off the app, where a router dependency cannot reach.
    app = FastAPI(
        title="mtrtk",
        version=__version__,
        docs_url=None,
        openapi_url=None,
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
        lifespan=lifespan,
    )
    app.state.ctx = ctx
    # Outside the routers and the exception handlers: a body this large must never be buffered,
    # let alone parsed, and the refusal must not depend on which route it was aimed at.
    app.add_middleware(BodyLimitMiddleware, limit=API_BODY_LIMIT)
    static = static_dir if static_dir is not None else default_static_dir()
    index_file = IndexFile(static / "index.html")

    # HEAD as well as GET: a monitor that wants the status code and nothing else should not have
    # to ask for the body, and a 405 would read as "this daemon is broken". Two registrations
    # rather than `methods=["GET", "HEAD"]`, which would put one operation id in the schema twice.
    @app.head("/healthz", include_in_schema=False)
    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        controller = ctx.controller
        return {
            "status": "ok",
            "role": ctx.settings.role.value,
            "connected": bool(getattr(controller, "connected", False)),
            # A replay daemon answers every route a live one does, `connected` included, so
            # without this a monitor cannot tell a base from a file being played back.
            "passive": bool(getattr(controller, "passive", ctx.settings.source_is_file)),
        }

    app.include_router(auth.router)
    app.include_router(auth.protected_router)
    _include_api_routers(app)
    # Not a router: the hub the endpoint fans out from is built by the lifespan above.
    app.add_api_websocket_route("/ws", websocket_endpoint)

    @app.get("/api/openapi.json", include_in_schema=False, dependencies=[auth.AuthDep])
    async def openapi_schema() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = get_openapi(
                title=app.title, version=app.version, routes=app.routes
            )
        return app.openapi_schema

    @app.get("/api/docs", include_in_schema=False, dependencies=[auth.AuthDep])
    async def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url="/api/openapi.json", title=f"{app.title} API")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> Response:
        """FastAPI's own 422 echoes the offending body back in `input`, which may be a password.

        Under `/api` the detail is cut down to where and why, with nothing of the payload in it -
        the same shape `PUT /api/config` produces for a `Settings` validation error.
        """
        if not request.url.path.startswith("/api/"):
            return await request_validation_exception_handler(request, exc)
        detail = [
            {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]}
            for err in exc.errors()
        ]
        return JSONResponse({"detail": detail}, status_code=422)

    @app.exception_handler(404)
    async def not_found(request: Request, exc: HTTPException) -> JSONResponse | FileResponse:
        if not is_spa_path(request.url.path):
            # Keep whatever the route said ("job not found"); only a routing miss is "Not Found".
            return JSONResponse(
                {"detail": getattr(exc, "detail", None) or "Not Found"}, status_code=404
            )
        if index_file.exists():
            return FileResponse(index_file.path)  # SPA client-side route
        return JSONResponse({"detail": NO_UI_DETAIL}, status_code=503)

    if (static / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    # response_model=None: the union of two Response classes is not a pydantic field, and
    # FastAPI would otherwise try to build a response model from the return annotation.
    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False, response_model=None)
    async def index() -> FileResponse | JSONResponse:
        if index_file.exists():
            return FileResponse(index_file.path)
        return JSONResponse({"detail": NO_UI_DETAIL}, status_code=503)

    return app


def _include_api_routers(app: FastAPI) -> None:
    """Mount every `API_MODULES` router behind the auth dependency.

    Nothing here is optional any more. While the routers were landing one task at a time a
    missing module was skipped; now that all ten exist, that `except ModuleNotFoundError` could
    only ever hide a real breakage - and an unmounted router is not an error anywhere, it is a
    whole section of the UI getting 404s. `test_web_app.py` pins the resulting path inventory.
    """
    for name in API_MODULES:
        module = import_module(f"mtrtk.web.api.{name}")
        app.include_router(module.router, dependencies=[auth.AuthDep])
