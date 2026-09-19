"""FastAPI application factory."""

from __future__ import annotations

import logging
from importlib import import_module, resources
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from mtrtk import __version__
from mtrtk.web import auth
from mtrtk.web.context import AppContext

log = logging.getLogger(__name__)

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
NO_UI_DETAIL = "UI not built; run `pnpm --dir web build` or use the Docker image"


def default_static_dir() -> Path:
    return Path(str(resources.files("mtrtk.web") / "static"))


def create_app(ctx: AppContext, static_dir: Path | None = None) -> FastAPI:
    app = FastAPI(
        title="mtrtk",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
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
    _include_api_routers(app)

    @app.exception_handler(404)
    async def not_found(request: Request, exc: HTTPException) -> JSONResponse | FileResponse:
        if request.url.path.startswith(("/api", "/ws", "/healthz")):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
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
    try:
        ws_module = import_module("mtrtk.web.ws")
    except ModuleNotFoundError as exc:
        if exc.name != "mtrtk.web.ws":
            raise
        return
    app.add_api_websocket_route("/ws", ws_module.websocket_endpoint)
