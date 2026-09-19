"""FastAPI application factory."""

from __future__ import annotations

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

from mtrtk import __version__
from mtrtk.web import auth
from mtrtk.web.api.logs import LogIndexMirror
from mtrtk.web.api.system import SystemCache
from mtrtk.web.context import AppContext
from mtrtk.web.ws import WsHub, websocket_endpoint

# Routers land one task at a time; each import stays optional until its module exists.
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


def default_static_dir() -> Path:
    return Path(str(resources.files("mtrtk.web") / "static"))


def is_spa_path(path: str) -> bool:
    """True when a 404 on `path` should hand the SPA its own index to route from."""
    return not any(path == prefix or path.startswith(prefix + "/") for prefix in SERVER_PREFIXES)


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
    static = static_dir if static_dir is not None else default_static_dir()

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        controller = ctx.controller
        return {
            "status": "ok",
            "role": ctx.settings.role.value,
            "connected": bool(getattr(controller, "connected", False)),
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
        index = static / "index.html"
        if index.exists():
            return FileResponse(index)  # SPA client-side route
        return JSONResponse({"detail": NO_UI_DETAIL}, status_code=503)

    if (static / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    # response_model=None: the union of two Response classes is not a pydantic field, and
    # FastAPI would otherwise try to build a response model from the return annotation.
    @app.get("/", include_in_schema=False, response_model=None)
    async def index() -> FileResponse | JSONResponse:
        index_file = static / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return JSONResponse({"detail": NO_UI_DETAIL}, status_code=503)

    return app


def _include_api_routers(app: FastAPI) -> None:
    """Routers are added in later tasks; each import is optional until its task lands."""
    for name in API_MODULES:
        module_name = f"mtrtk.web.api.{name}"
        try:
            module = import_module(module_name)
        except ModuleNotFoundError as exc:
            # Only "that router does not exist yet" is optional. A router that *is* there but
            # whose own imports are broken must not vanish silently into a 404.
            if exc.name != module_name:
                raise
            continue
        app.include_router(module.router, dependencies=[auth.AuthDep])
