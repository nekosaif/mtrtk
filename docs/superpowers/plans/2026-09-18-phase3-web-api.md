# mtrtk Phase 3: Web API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose everything the daemon knows over HTTP + WebSocket so the Phase 4 UI (and scripts, ROS bridge, PPK tooling) can read live state, history, logs, events and sites, and can drive the base station (mode, sites, receiver actions, configuration) — served by uvicorn inside the daemon process, bound like the caster (Tailscale by default), optionally password-protected.

**Architecture:** `web/app.py` builds a FastAPI app from an `AppContext` (settings, bus, store, db, daemon handles, job runner). REST routers live in `web/api/*.py`, one file per resource. `web/ws.py` is a hub that sends a full state snapshot on connect, then one bundled `epoch` message per receiver epoch plus event-driven messages for RF, spectrum, NTRIP clients, events, system stats, receiver lifecycle and jobs — filtered by the topics a client asked for. `jobs.py` is a small persistent job runner (SQLite `jobs` table + `DATA_DIR/jobs/<id>/` result dirs) reused by Phase 5 exports and Phase 8 PPK. `web/server.py` runs uvicorn programmatically as one more supervised daemon consumer. The built SPA (Phase 4/Docker) is served from `mtrtk/web/static` with SPA fallback.

**Tech Stack:** FastAPI, uvicorn (`uvicorn[standard]`, in-process `Server`), websockets, httpx (tests via `ASGITransport`), python-dotenv-free `.env` writer, pydantic v2.

**Spec:** `docs/superpowers/specs/2026-09-18-mtrtk-design.md` — sections *web API*, *Configuration*, *frontend* (topics), *Phase 3*. Prerequisites: Phase 1 and Phase 2 plans complete. Verify interface names against real code before starting.

## Global Constraints

- All routes under `/api`; WebSocket at `/ws`; health at `/healthz` (always unauthenticated, returns `{"status":"ok","role":...,"connected":bool}`).
- Bind via `exposure.wait_for_bind(settings.web_bind, stop)` (Tailscale IP by default; never silently `0.0.0.0` for `tailscale`). Port `WEB_PORT` (default 8080). Public binds without `WEB_PASSWORD` are refused by `Settings` unless `WEB_ALLOW_INSECURE=1` (Phase 1).
- Auth: when `WEB_PASSWORD` is set, every `/api/*` route except `/api/login` and every `/ws` connection requires either cookie `mtrtk_session=<token>` or header `Authorization: Bearer <token>`, where `token = HMAC-SHA256(key=SHA256(password), msg=b"mtrtk-session")` hex. Comparison uses `hmac.compare_digest`. Static files are never gated (the SPA shows its own login page).
- Secrets (`ntrip_password`, `web_password`, `alert_webhook_url`, `tunnel_token`) are returned masked as `"***"` by `GET /api/config`; a PUT containing `"***"` leaves the secret unchanged.
- `PUT /api/config` validates the merged settings with `Settings(**merged, _env_file=None)` before writing; the `.env` file is rewritten atomically (temp file + `os.replace`), preserving unrelated lines and comments. Live-applicable keys: `base_mode`, `svin_min_duration_s`, `svin_acc_limit_m`, `active_site`; every other change returns `restart_required: true`.
- WebSocket protocol: first message `{"type":"snapshot","state":<ReceiverState JSON>,"role":...,"topics":[...]}`; then `{"type":"epoch","t":<unix s or null>,"pvt":{position,accuracy,dops,fix,velocity,time},"sats":{sats,sat_summary},"rtcm":{...},"svin":{...}}` once per receiver epoch (only the requested sections), and `{"type":"update","topic":<name>,"data":...}` for `rf`, `span`, `ntrip`, `events`, `system`, `receiver`, `base`, `jobs`, `rawlog`. Available topic names: `pvt, sats, rtcm, svin, rf, span, ntrip, events, system, receiver, base, jobs, rawlog`. Default (no `topics` query) = all. `span` updates are throttled to 1 per second per client; a client that cannot keep up (send queue > 50 messages) is disconnected.
- JSON uses pydantic `model_dump(mode="json")`; datetimes are ISO-8601 UTC strings.
- Job runner: statuses `queued|running|done|failed`; results under `DATA_DIR/jobs/<id>/`; at most 1 running job at a time (RTKLIB is CPU-heavy on a Pi); `jobs.update` published on every status/progress change.
- Commit per task with a Conventional Commit message ending in `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File structure (this plan)

| Path | Responsibility |
|---|---|
| `src/mtrtk/web/__init__.py`, `context.py` | `AppContext` dataclass shared by routers |
| `src/mtrtk/web/app.py` | `create_app(ctx)`: routers, static SPA, healthz, error handlers |
| `src/mtrtk/web/auth.py` | token derivation, `require_auth` dependency, login route |
| `src/mtrtk/web/ws.py` | `WsHub` + `/ws` endpoint |
| `src/mtrtk/web/envfile.py` | `.env` read/update helpers |
| `src/mtrtk/web/api/__init__.py`, `status.py`, `config.py`, `receiver.py`, `base.py`, `ntrip.py`, `logs.py`, `history.py`, `events.py`, `system.py`, `jobs.py` | REST routers |
| `src/mtrtk/web/server.py` | `WebServer` (uvicorn in-process) |
| `src/mtrtk/jobs.py` | `JobRunner`, `Job`, `JobContext` |
| `src/mtrtk/core/receiver.py` (modify) | `ReceiverController.reset(kind)` |
| `src/mtrtk/daemon.py` (modify) | web consumer, `AppContext` construction, `web_port` |
| `src/mtrtk/cli.py` (modify) | `mtrtk healthcheck` |
| `tests/unit/test_web_*.py`, `tests/unit/test_jobs.py`, `tests/unit/test_envfile.py` | tests (httpx `ASGITransport`, fake websocket) |
| `docs/api.md` | endpoint reference |

---

### Task 1: App skeleton, context, auth, static SPA, healthz

**Files:**
- Modify: `pyproject.toml` (dependencies: `fastapi>=0.115`, `uvicorn[standard]>=0.34`, `websockets>=15`, `python-multipart>=0.0.20`)
- Create: `src/mtrtk/web/__init__.py` (empty), `src/mtrtk/web/context.py`, `src/mtrtk/web/auth.py`, `src/mtrtk/web/app.py`, `src/mtrtk/web/api/__init__.py` (empty), `tests/unit/webtest.py`, `tests/unit/test_web_app.py`

**Interfaces:**
- Produces: `AppContext(settings, bus, store, db, daemon, jobs=None, started_mono=...)` with `.uptime_s`, `.controller` / `.caster` / `.basemode` properties reading `daemon` attributes (may be `None`); `auth.session_token(password) -> str`, `auth.require_auth` (FastAPI dependency; no-op when no password), `POST /api/login {"password"}` → sets cookie + returns `{"token"}`, `POST /api/logout`; `create_app(ctx, static_dir: Path | None = None) -> FastAPI`; `GET /healthz`; static SPA mount with fallback to `index.html` for non-`/api` paths; JSON 404 for unknown `/api/*`.
- Test helper `tests/unit/webtest.py`: `make_ctx(tmp_path, **settings_overrides) -> AppContext` (real Bus/StateStore/Database, fake daemon namespace), `client(app) -> httpx.AsyncClient` (ASGITransport), `load_fixture_into(store)` feeding `f9p_hpg113_base_30s.ubx`.

- [ ] **Step 1: Add dependencies**

Append to `[project].dependencies`: `"fastapi>=0.115"`, `"uvicorn[standard]>=0.34"`, `"websockets>=15"`, `"python-multipart>=0.0.20"`. Run `uv sync`.

- [ ] **Step 2: Write the test helper and failing tests**

`tests/unit/webtest.py`:
```python
"""Shared helpers for web API tests."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.core.statestore import StateStore
from mtrtk.store.db import Database
from mtrtk.web.context import AppContext

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


async def make_ctx(tmp_path: Path, **overrides) -> AppContext:  # type: ignore[no-untyped-def]
    overrides.setdefault("ntrip_password", "pw")
    overrides.setdefault("data_dir", tmp_path)
    settings = Settings(_env_file=None, **overrides)
    bus = Bus()
    store = StateStore(bus)
    db = Database(tmp_path / "mtrtk.db")
    await db.open()
    daemon = SimpleNamespace(controller=None, caster=None, basemode=None, stop=None)
    return AppContext(settings=settings, bus=bus, store=store, db=db, daemon=daemon, started_mono=time.monotonic() - 5)


def load_fixture_into(store: StateStore, name: str = "f9p_hpg113_base_30s.ubx") -> int:
    frames = Framer().feed((FIXTURES / name).read_bytes())
    for f in frames:
        store.apply(f)
    return len(frames)


def client(app: FastAPI, **kwargs) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", **kwargs)
```

`tests/unit/test_web_app.py`:
```python
from pathlib import Path

import pytest
from webtest import client, make_ctx

from mtrtk.web.app import create_app
from mtrtk.web.auth import session_token


@pytest.fixture
async def ctx(tmp_path: Path):
    c = await make_ctx(tmp_path)
    try:
        yield c
    finally:
        await c.db.close()


async def test_healthz_is_open(ctx) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "role": "base", "connected": False}


async def test_unknown_api_route_is_json_404(ctx) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.get("/api/nope")
    assert r.status_code == 404 and r.json()["detail"] == "Not Found"


async def test_spa_fallback_and_missing_build(ctx, tmp_path: Path) -> None:
    async with client(create_app(ctx, static_dir=tmp_path / "nostatic")) as c:
        r = await c.get("/")
    assert r.status_code == 503 and "pnpm build" in r.json()["detail"]
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>mtrtk</html>")
    (static / "assets" / "app.js").write_text("console.log(1)")
    async with client(create_app(ctx, static_dir=static)) as c:
        assert (await c.get("/")).text == "<html>mtrtk</html>"
        assert (await c.get("/satellites")).text == "<html>mtrtk</html>"  # SPA route
        assert (await c.get("/assets/app.js")).text == "console.log(1)"
        assert (await c.get("/api/nope")).status_code == 404  # API never falls back to the SPA


async def test_no_password_means_open_api(ctx) -> None:
    async with client(create_app(ctx)) as c:
        assert (await c.get("/api/status")).status_code == 200


async def test_password_gates_api_cookie_and_bearer(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path, web_password="hunter2", web_bind="lan")
    app = create_app(ctx)
    try:
        async with client(app) as c:
            assert (await c.get("/api/status")).status_code == 401
            assert (await c.get("/healthz")).status_code == 200
            bad = await c.post("/api/login", json={"password": "nope"})
            assert bad.status_code == 401
            ok = await c.post("/api/login", json={"password": "hunter2"})
            assert ok.status_code == 200
            token = ok.json()["token"]
            assert token == session_token("hunter2") and "mtrtk_session" in ok.cookies
            assert (await c.get("/api/status")).status_code == 200  # cookie jar
            out = await c.post("/api/logout")
            assert out.status_code == 200
        async with client(app) as c2:
            assert (await c2.get("/api/status", headers={"Authorization": f"Bearer {token}"})).status_code == 200
            assert (await c2.get("/api/status", headers={"Authorization": "Bearer wrong"})).status_code == 401
    finally:
        await ctx.db.close()


def test_session_token_is_stable_and_password_bound() -> None:
    assert session_token("a") == session_token("a")
    assert session_token("a") != session_token("b")
    assert len(session_token("a")) == 64
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/unit/test_web_app.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.web.context'`.

- [ ] **Step 4: Write `src/mtrtk/web/context.py`**

```python
"""Objects the web layer needs from the running daemon."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.statestore import StateStore
from mtrtk.store.db import Database

if TYPE_CHECKING:
    from mtrtk.jobs import JobRunner


@dataclass
class AppContext:
    settings: Settings
    bus: Bus
    store: StateStore
    db: Database
    daemon: Any  # Daemon (or a stand-in in tests) exposing controller / caster / basemode / stop
    jobs: JobRunner | None = None
    started_mono: float = field(default_factory=time.monotonic)

    @property
    def uptime_s(self) -> float:
        return time.monotonic() - self.started_mono

    @property
    def controller(self) -> Any:
        return getattr(self.daemon, "controller", None)

    @property
    def caster(self) -> Any:
        return getattr(self.daemon, "caster", None)

    @property
    def basemode(self) -> Any:
        return getattr(self.daemon, "basemode", None)
```

- [ ] **Step 5: Write `src/mtrtk/web/auth.py`**

```python
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
    presented = _presented_token({k.lower(): v for k, v in request.headers.items()}, request.cookies)
    if not token_ok(password, presented):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required", headers={"WWW-Authenticate": "Bearer"})


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
```

- [ ] **Step 6: Write `src/mtrtk/web/app.py`**

```python
"""FastAPI application factory."""

from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from mtrtk import __version__
from mtrtk.web import auth
from mtrtk.web.context import AppContext

log = logging.getLogger(__name__)


def default_static_dir() -> Path:
    return Path(str(resources.files("mtrtk.web") / "static"))


def create_app(ctx: AppContext, static_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="mtrtk", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.ctx = ctx
    static = static_dir if static_dir is not None else default_static_dir()

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        controller = ctx.controller
        return {"status": "ok", "role": ctx.settings.role.value, "connected": bool(getattr(controller, "connected", False))}

    app.include_router(auth.router)
    _include_api_routers(app)

    @app.exception_handler(404)
    async def not_found(request: Request, exc: HTTPException) -> JSONResponse | FileResponse:
        if request.url.path.startswith(("/api", "/ws", "/healthz")):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        index = static / "index.html"
        if index.exists():
            return FileResponse(index)  # SPA client-side route
        return JSONResponse({"detail": "UI not built; run `pnpm --dir web build` or use the Docker image"}, status_code=503)

    if (static / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse | JSONResponse:
        index_file = static / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return JSONResponse({"detail": "UI not built; run `pnpm --dir web build` or use the Docker image"}, status_code=503)

    return app


def _include_api_routers(app: FastAPI) -> None:
    """Routers are added in later tasks; each import is optional until its task lands."""
    from importlib import import_module

    for name in ("status", "system", "config", "receiver", "base", "ntrip", "logs", "history", "events", "jobs"):
        try:
            module = import_module(f"mtrtk.web.api.{name}")
        except ModuleNotFoundError:
            continue
        app.include_router(module.router, dependencies=[auth.AuthDep])
    try:
        from mtrtk.web import ws as ws_module
    except ModuleNotFoundError:
        return
    app.add_api_websocket_route("/ws", ws_module.websocket_endpoint)
```

Then create a minimal `src/mtrtk/web/api/status.py` so the auth tests have a route to hit (Task 2 completes it):
```python
"""GET /api/status — one-screen summary."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/status")
async def status(request: Request) -> dict[str, object]:
    ctx = request.app.state.ctx
    return {"role": ctx.settings.role.value, "uptime_s": round(ctx.uptime_s, 1)}
```

- [ ] **Step 7: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_web_app.py -q` → `6 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

```bash
git add pyproject.toml uv.lock src/mtrtk/web tests/unit/webtest.py tests/unit/test_web_app.py
git commit -m "feat(web): FastAPI app factory with optional password auth, SPA serving and healthz

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Status, state and system endpoints

**Files:**
- Modify: `src/mtrtk/web/api/status.py`
- Create: `src/mtrtk/web/api/system.py`, `tests/unit/test_web_status.py`

**Interfaces:**
- Produces: `GET /api/status` → `{role, version, uptime_s, connected, source, firmware:{fw_version,protver,module}, fix:{fix_type_name,carr_soln_name,num_sv}, position:{lat,lon,height_m}, accuracy:{h_acc_m,v_acc_m}, survey_in:{active,valid,dur_s,mean_acc_m}, ntrip_clients:int, rtcm_bytes_per_s, epoch_count, capabilities:{supported,unsupported}}`; `GET /api/state` → full `ReceiverState` JSON; `GET /api/system` → `{hostname, tailscale_ip, data_dir, stats: SystemStats|null, versions:{mtrtk, python, pyubx2}}`. The latest `SystemStats` is cached by a tiny bus subscriber `SystemCache` started in `create_app` (kept on `app.state.system_cache`).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_web_status.py`:
```python
from pathlib import Path

import pytest
from webtest import client, load_fixture_into, make_ctx

from mtrtk.store.models import SystemStats
from mtrtk.web.app import create_app


@pytest.fixture
async def ctx(tmp_path: Path):
    c = await make_ctx(tmp_path)
    load_fixture_into(c.store)
    try:
        yield c
    finally:
        await c.db.close()


async def test_status_summary(ctx) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "base" and body["connected"] is False and body["version"]
    assert body["fix"]["fix_type_name"] == "3D" and body["fix"]["num_sv"] > 10
    assert 23 < body["position"]["lat"] < 24.5
    assert body["firmware"]["fw_version"] in ("", "HPG 1.13")  # MON-VER is only in the stream if polled
    assert body["ntrip_clients"] == 0 and body["epoch_count"] >= 25
    assert set(body["survey_in"]) == {"active", "valid", "dur_s", "mean_acc_m"}


async def test_full_state(ctx) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.get("/api/state")
    body = r.json()
    assert body["fix"]["fix_type"] == 3 and len(body["sats"]) > 20
    assert body["time"]["utc"].startswith("2026-")
    assert body["rtcm_out"]["total_count"] > 0


async def test_system_endpoint_reflects_latest_stats(ctx, monkeypatch) -> None:
    monkeypatch.setattr("mtrtk.web.api.system.tailscale_ipv4", lambda: "100.100.50.10")
    app = create_app(ctx)
    async with client(app) as c:
        r = await c.get("/api/system")
        assert r.status_code == 200 and r.json()["stats"] is None and r.json()["tailscale_ip"] == "100.100.50.10"
        ctx.bus.publish("system.stats", SystemStats(cpu_pct=1.5, mem_pct=2, disk_free_gb=3, disk_used_pct=4, uptime_s=5))
        await __import__("asyncio").sleep(0.01)
        r = await c.get("/api/system")
    assert r.json()["stats"]["cpu_pct"] == 1.5
    assert r.json()["versions"]["mtrtk"] and r.json()["versions"]["pyubx2"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_web_status.py -q`
Expected: failures (`/api/state` and `/api/system` 404, status body incomplete).

- [ ] **Step 3: Complete `src/mtrtk/web/api/status.py`**

```python
"""GET /api/status (summary) and GET /api/state (full ReceiverState)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from mtrtk import __version__

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    s = ctx.store.state
    controller = ctx.controller
    caps = getattr(controller, "capabilities", None)
    caster = ctx.caster
    return {
        "role": ctx.settings.role.value,
        "version": __version__,
        "uptime_s": round(ctx.uptime_s, 1),
        "connected": bool(getattr(controller, "connected", False)),
        "source": ctx.settings.mtrtk_source,
        "firmware": {"fw_version": s.firmware.fw_version, "protver": s.firmware.protver, "module": s.firmware.module},
        "fix": {"fix_type_name": s.fix.fix_type_name, "carr_soln_name": s.fix.carr_soln_name, "num_sv": s.fix.num_sv},
        "position": {"lat": s.position.lat, "lon": s.position.lon, "height_m": s.position.height_m},
        "accuracy": {"h_acc_m": s.accuracy.h_acc_m, "v_acc_m": s.accuracy.v_acc_m},
        "survey_in": {"active": s.survey_in.active, "valid": s.survey_in.valid, "dur_s": s.survey_in.dur_s, "mean_acc_m": s.survey_in.mean_acc_m},
        "ntrip_clients": len(getattr(caster, "clients", {}) or {}),
        "rtcm_bytes_per_s": s.rtcm_out.bytes_per_s,
        "epoch_count": s.epoch_count,
        "capabilities": {"supported": sorted(caps.supported), "unsupported": sorted(caps.unsupported)} if caps else None,
    }


@router.get("/state")
async def state(request: Request) -> dict[str, Any]:
    return request.app.state.ctx.store.state.model_dump(mode="json")
```

- [ ] **Step 4: Write `src/mtrtk/web/api/system.py`**

```python
"""GET /api/system — host information and the latest SystemStats."""

from __future__ import annotations

import asyncio
import platform
import socket
from typing import Any

import pyubx2
from fastapi import APIRouter, Request

from mtrtk import __version__
from mtrtk.core.bus import Bus
from mtrtk.core.exposure import tailscale_ipv4
from mtrtk.store.models import SystemStats

router = APIRouter(prefix="/api", tags=["system"])


class SystemCache:
    """Remembers the newest SystemStats published on the bus."""

    def __init__(self, bus: Bus) -> None:
        self.latest: SystemStats | None = None
        self._sub = bus.subscribe("system.stats", maxsize=5)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="web-system-cache")

    async def _run(self) -> None:
        async for _, item in self._sub:
            self.latest = item

    def close(self) -> None:
        self._sub.close()


def _cache(request: Request) -> SystemCache:
    cache = getattr(request.app.state, "system_cache", None)
    if cache is None:
        cache = SystemCache(request.app.state.ctx.bus)
        request.app.state.system_cache = cache
    cache.start()
    return cache


@router.get("/system")
async def system(request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    cache = _cache(request)
    return {
        "hostname": socket.gethostname(),
        "tailscale_ip": tailscale_ipv4(),
        "data_dir": str(ctx.settings.data_dir),
        "stats": cache.latest.model_dump(mode="json") if cache.latest else None,
        "versions": {"mtrtk": __version__, "python": platform.python_version(), "pyubx2": pyubx2.__version__},
    }
```

- [ ] **Step 5: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_web_status.py tests/unit/test_web_app.py -q` → all pass; `uv run ruff check . && uv run ruff format . && uv run mypy`.

```bash
git add src/mtrtk/web/api/status.py src/mtrtk/web/api/system.py tests/unit/test_web_status.py
git commit -m "feat(web): status, full state and system endpoints

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: WebSocket hub

**Files:**
- Create: `src/mtrtk/web/ws.py`, `tests/unit/test_web_ws.py`

**Interfaces:**
- Consumes: bus topics `state.epoch`, `state.hardware`, `state.rf`, `state.spectrum`, `ntrip.clients`, `events.new`, `system.stats`, `receiver.*`, `base.*`, `jobs.update`, `rawlog.*`; `auth.websocket_authorized`.
- Produces: `TOPICS = ("pvt","sats","rtcm","svin","rf","span","ntrip","events","system","receiver","base","jobs","rawlog")`; `WsHub(ctx)` with `async serve(socket: WsLike, topics: set[str])` where `WsLike` has `async send_json(obj)` and `async receive_text()`; `websocket_endpoint(ws: WebSocket)` (FastAPI route handler used by `create_app`); `snapshot_message(ctx, topics) -> dict`; `epoch_message(state, topics) -> dict`.
- Message format per Global Constraints. Span throttle 1 Hz per client; send queue limit 50 → disconnect.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_web_ws.py`:
```python
import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from webtest import load_fixture_into, make_ctx

from mtrtk.core.state import RfBlock, Spectrum
from mtrtk.store.models import SystemStats
from mtrtk.web.app import create_app
from mtrtk.web.ws import TOPICS, WsHub, epoch_message, snapshot_message


class FakeSocket:
    def __init__(self, hold_open: bool = True) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self._close_event = asyncio.Event()
        self.hold_open = hold_open

    async def send_json(self, obj: dict) -> None:
        self.sent.append(obj)

    async def receive_text(self) -> str:
        if not self.hold_open:
            raise ConnectionError("closed")
        await self._close_event.wait()
        raise ConnectionError("closed")

    async def close(self, code: int = 1000) -> None:
        self.closed = True
        self._close_event.set()

    def disconnect(self) -> None:
        self._close_event.set()


@pytest.fixture
async def ctx(tmp_path: Path):
    c = await make_ctx(tmp_path)
    load_fixture_into(c.store)
    try:
        yield c
    finally:
        await c.db.close()


def test_snapshot_and_epoch_shapes(ctx) -> None:
    snap = snapshot_message(ctx, set(TOPICS))
    assert snap["type"] == "snapshot" and snap["role"] == "base" and snap["state"]["fix"]["fix_type"] == 3
    assert sorted(snap["topics"]) == sorted(TOPICS)
    epoch = epoch_message(ctx.store.state, {"pvt", "sats"})
    assert epoch["type"] == "epoch" and set(epoch) == {"type", "t", "pvt", "sats"}
    assert set(epoch["pvt"]) == {"position", "accuracy", "dops", "fix", "velocity", "time"}
    assert set(epoch["sats"]) == {"sats", "sat_summary"}
    assert epoch["t"] is not None
    only_rtcm = epoch_message(ctx.store.state, {"rtcm", "svin"})
    assert set(only_rtcm) == {"type", "t", "rtcm", "svin"}


async def test_serve_sends_snapshot_then_epochs_and_updates(ctx) -> None:
    hub = WsHub(ctx)
    sock = FakeSocket()
    task = asyncio.create_task(hub.serve(sock, {"pvt", "rf", "system", "events"}))
    await asyncio.sleep(0.01)
    assert sock.sent[0]["type"] == "snapshot"
    ctx.bus.publish("state.epoch", ctx.store.state)
    ctx.bus.publish("state.rf", [RfBlock(block_id=0, jam_ind=5)])
    ctx.bus.publish("state.spectrum", [Spectrum(block_id=0, span_hz=1, res_hz=1, center_hz=1, pga_db=0, bins=[0] * 256)])  # not subscribed
    ctx.bus.publish("system.stats", SystemStats(cpu_pct=1, mem_pct=2, disk_free_gb=3, disk_used_pct=4, uptime_s=5))
    await asyncio.sleep(0.02)
    types = [(m["type"], m.get("topic")) for m in sock.sent[1:]]
    assert types == [("epoch", None), ("update", "rf"), ("update", "system")]
    assert "sats" not in sock.sent[1]
    assert sock.sent[2]["data"][0]["jam_ind"] == 5
    sock.disconnect()
    await asyncio.wait_for(task, 1.0)


async def test_span_is_throttled_to_one_per_second(ctx) -> None:
    hub = WsHub(ctx)
    sock = FakeSocket()
    task = asyncio.create_task(hub.serve(sock, {"span"}))
    await asyncio.sleep(0.01)
    spectrum = [Spectrum(block_id=0, span_hz=1, res_hz=1, center_hz=1, pga_db=0, bins=[0] * 256)]
    for _ in range(5):
        ctx.bus.publish("state.spectrum", spectrum)
    await asyncio.sleep(0.02)
    assert sum(1 for m in sock.sent if m.get("topic") == "span") == 1
    sock.disconnect()
    await asyncio.wait_for(task, 1.0)


async def test_slow_client_is_disconnected(ctx) -> None:
    hub = WsHub(ctx)
    sock = FakeSocket()

    async def slow_send(obj: dict) -> None:
        await asyncio.sleep(10)

    sock.send_json = slow_send  # type: ignore[method-assign]
    task = asyncio.create_task(hub.serve(sock, {"pvt"}))
    await asyncio.sleep(0.01)
    for _ in range(60):
        ctx.bus.publish("state.epoch", ctx.store.state)
    await asyncio.sleep(0.05)
    assert sock.closed is True
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_websocket_route_snapshot_via_testclient(ctx) -> None:
    app = create_app(ctx)
    with TestClient(app) as client, client.websocket_connect("/ws?topics=pvt,rtcm") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot" and sorted(first["topics"]) == ["pvt", "rtcm"]


def test_websocket_requires_token_when_password_set(tmp_path: Path) -> None:
    import asyncio as aio

    ctx = aio.run(make_ctx(tmp_path, web_password="pw", web_bind="lan"))
    app = create_app(ctx)
    from mtrtk.web.auth import session_token

    with TestClient(app) as client:
        with pytest.raises(Exception):  # policy violation close before accept
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()
        with client.websocket_connect(f"/ws?token={session_token('pw')}") as ws:
            assert ws.receive_json()["type"] == "snapshot"
    aio.run(ctx.db.close())
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_web_ws.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.web.ws'`.

- [ ] **Step 3: Write `src/mtrtk/web/ws.py`**

```python
"""WebSocket hub: snapshot on connect, one bundled message per receiver epoch, event-driven updates."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Protocol

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from mtrtk.core.state import ReceiverState
from mtrtk.web.auth import websocket_authorized
from mtrtk.web.context import AppContext

log = logging.getLogger(__name__)

TOPICS = ("pvt", "sats", "rtcm", "svin", "rf", "span", "ntrip", "events", "system", "receiver", "base", "jobs", "rawlog")
EPOCH_TOPICS = {"pvt", "sats", "rtcm", "svin"}
BUS_TO_TOPIC = {
    "state.hardware": "rf",
    "state.rf": "rf",
    "state.spectrum": "span",
    "ntrip.clients": "ntrip",
    "events.new": "events",
    "system.stats": "system",
    "jobs.update": "jobs",
}
PREFIX_TO_TOPIC = {"receiver.": "receiver", "base.": "base", "rawlog.": "rawlog"}
SPAN_MIN_INTERVAL_S = 1.0
SEND_QUEUE_LIMIT = 50


class WsLike(Protocol):
    async def send_json(self, obj: dict[str, Any]) -> None: ...
    async def receive_text(self) -> str: ...
    async def close(self, code: int = 1000) -> None: ...


def _json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if hasattr(value, "public"):
        return value.public()
    if hasattr(value, "__dataclass_fields__"):
        return {k: _json(getattr(value, k)) for k in value.__dataclass_fields__}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def snapshot_message(ctx: AppContext, topics: set[str]) -> dict[str, Any]:
    return {"type": "snapshot", "role": ctx.settings.role.value, "topics": sorted(topics), "state": ctx.store.state.model_dump(mode="json")}


def epoch_message(state: ReceiverState, topics: set[str]) -> dict[str, Any]:
    msg: dict[str, Any] = {"type": "epoch", "t": state.time.utc.timestamp() if state.time.utc else None}
    if "pvt" in topics:
        msg["pvt"] = {k: getattr(state, k).model_dump(mode="json") for k in ("position", "accuracy", "dops", "fix", "velocity", "time")}
    if "sats" in topics:
        msg["sats"] = {"sats": [s.model_dump(mode="json") for s in state.sats], "sat_summary": state.sat_summary.model_dump(mode="json")}
    if "rtcm" in topics:
        msg["rtcm"] = state.rtcm_out.model_dump(mode="json")
    if "svin" in topics:
        msg["svin"] = state.survey_in.model_dump(mode="json")
    return msg


class WsHub:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def serve(self, socket: WsLike, topics: set[str]) -> None:
        topics = set(topics) & set(TOPICS) or set(TOPICS)
        sub = self.ctx.bus.subscribe(
            "state.epoch", "state.hardware", "state.rf", "state.spectrum", "ntrip.clients", "events.new",
            "system.stats", "receiver.*", "base.*", "jobs.update", "rawlog.*", maxsize=SEND_QUEUE_LIMIT * 2,
        )
        outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SEND_QUEUE_LIMIT)
        last_span = 0.0

        async def producer() -> None:
            nonlocal last_span
            async for bus_topic, item in sub:
                msg = self._translate(bus_topic, item, topics)
                if msg is None:
                    continue
                if msg.get("topic") == "span":
                    now = time.monotonic()
                    if now - last_span < SPAN_MIN_INTERVAL_S:
                        continue
                    last_span = now
                try:
                    outbox.put_nowait(msg)
                except asyncio.QueueFull:
                    log.info("websocket client too slow; disconnecting")
                    await socket.close(code=1008)
                    return

        async def sender() -> None:
            while True:
                await socket.send_json(await outbox.get())

        async def reader() -> None:  # detects disconnects; client messages are ignored
            while True:
                await socket.receive_text()

        await socket.send_json(snapshot_message(self.ctx, topics))
        tasks = [asyncio.create_task(producer()), asyncio.create_task(sender()), asyncio.create_task(reader())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, (ConnectionError, WebSocketDisconnect, RuntimeError)):
                    log.debug("websocket task ended: %r", exc)
        finally:
            sub.close()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _translate(bus_topic: str, item: Any, topics: set[str]) -> dict[str, Any] | None:
        if bus_topic == "state.epoch":
            wanted = topics & EPOCH_TOPICS
            return epoch_message(item, wanted) if wanted else None
        topic = BUS_TO_TOPIC.get(bus_topic)
        if topic is None:
            for prefix, name in PREFIX_TO_TOPIC.items():
                if bus_topic.startswith(prefix):
                    topic = name
                    break
        if topic is None or topic not in topics:
            return None
        return {"type": "update", "topic": topic, "source": bus_topic, "data": _json(item)}


async def websocket_endpoint(ws: WebSocket) -> None:
    if not websocket_authorized(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    raw = ws.query_params.get("topics", "")
    topics = {t.strip() for t in raw.split(",") if t.strip()} or set(TOPICS)
    hub = WsHub(ws.app.state.ctx)
    try:
        await hub.serve(ws, topics)
    except WebSocketDisconnect:
        pass
```

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_web_ws.py -q` → `6 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

If `test_websocket_requires_token_when_password_set` does not raise on the unauthenticated connect (Starlette's TestClient may deliver the close frame differently), assert instead that `ws.receive()` yields a `websocket.close` message with code 1008.

```bash
git add src/mtrtk/web/ws.py tests/unit/test_web_ws.py
git commit -m "feat(web): WebSocket hub with snapshot, epoch bundles, topic filtering and slow-client protection

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Configuration endpoints and `.env` writer

**Files:**
- Create: `src/mtrtk/web/envfile.py`, `src/mtrtk/web/api/config.py`, `tests/unit/test_envfile.py`, `tests/unit/test_web_config.py`
- Modify: `src/mtrtk/config.py` (add `mtrtk_env_file: Path = Path(".env")`)

**Interfaces:**
- Produces: `envfile.read_env(path) -> dict[str, str]`; `envfile.update_env(path, updates: dict[str, str]) -> None` (atomic; preserves other lines and trailing `# comments`; appends missing keys); `envfile.to_env_value(value) -> str` (enums → `.value`, lists → comma-joined, bools → `1`/`0`, None → ``); `GET /api/config` → `{"values": {...masked...}, "env_file": str, "secret_keys": [...], "live_keys": [...]}`; `PUT /api/config {"values": {...}}` → `{"changed": [...], "restart_required": bool}` (422 on invalid); `POST /api/restart` → sets `daemon.stop` and returns `{"ok": true}`.
- Constants: `SECRET_KEYS = {"ntrip_password", "web_password", "alert_webhook_url", "tunnel_token"}`, `LIVE_KEYS = {"base_mode", "svin_min_duration_s", "svin_acc_limit_m", "active_site"}`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_envfile.py`:
```python
from pathlib import Path

from mtrtk.config import BaseMode
from mtrtk.web.envfile import read_env, to_env_value, update_env


def test_read_env_parses_values_and_ignores_comments(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("# header\nROLE=base   # trailing\nNTRIP_PASSWORD=\nLOG_MESSAGES=RXM-RAWX,RXM-SFRBX\nQUOTED=\"a # b\"\n\n")
    assert read_env(p) == {"ROLE": "base", "NTRIP_PASSWORD": "", "LOG_MESSAGES": "RXM-RAWX,RXM-SFRBX", "QUOTED": "a # b"}


def test_update_env_preserves_layout_and_appends(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("# header\nROLE=base   # role comment\nBAUD=115200\n")
    update_env(p, {"ROLE": "rover", "NEW_KEY": "x y"})
    text = p.read_text()
    assert text.splitlines() == ["# header", "ROLE=rover   # role comment", "BAUD=115200", "NEW_KEY=x y"]
    assert not (tmp_path / ".env.tmp").exists()


def test_update_env_creates_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "sub" / ".env"
    update_env(p, {"A": "1"})
    assert read_env(p) == {"A": "1"}


def test_to_env_value() -> None:
    assert to_env_value(BaseMode.FIXED) == "fixed"
    assert to_env_value(["a", "b"]) == "a,b"
    assert to_env_value(True) == "1" and to_env_value(False) == "0"
    assert to_env_value(None) == "" and to_env_value(5.5) == "5.5" and to_env_value(Path("/data")) == "/data"
```

`tests/unit/test_web_config.py`:
```python
from pathlib import Path
from types import SimpleNamespace

import pytest
from webtest import client, make_ctx

from mtrtk.web.app import create_app
from mtrtk.web.envfile import read_env


@pytest.fixture
async def ctx(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("ROLE=base\nNTRIP_PASSWORD=pw\nSVIN_MIN_DURATION_S=300\n")
    c = await make_ctx(tmp_path, mtrtk_env_file=env, alert_webhook_url="https://ntfy.sh/secret")
    try:
        yield c
    finally:
        await c.db.close()


async def test_get_config_masks_secrets(ctx) -> None:
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/config")).json()
    assert body["values"]["ntrip_password"] == "***" and body["values"]["alert_webhook_url"] == "***"
    assert body["values"]["web_password"] is None
    assert body["values"]["station_id"] == "MTRK" and body["values"]["base_mode"] == "survey-in"
    assert "ntrip_password" in body["secret_keys"] and "base_mode" in body["live_keys"]
    assert body["env_file"].endswith(".env")


async def test_put_config_writes_env_and_flags_restart(ctx) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"station_id": "BASE", "ntrip_password": "***", "rtcm_msm": 4}})
    assert r.status_code == 200
    assert r.json() == {"changed": ["rtcm_msm", "station_id"], "restart_required": True}
    env = read_env(ctx.settings.mtrtk_env_file)
    assert env["STATION_ID"] == "BASE" and env["RTCM_MSM"] == "4" and env["NTRIP_PASSWORD"] == "pw"  # secret untouched


async def test_put_invalid_config_is_422_and_writes_nothing(ctx) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"station_id": "toolong"}})
    assert r.status_code == 422 and "station_id" in r.text
    assert read_env(ctx.settings.mtrtk_env_file).get("STATION_ID") is None


async def test_live_keys_apply_without_restart(ctx) -> None:
    calls: list[str] = []

    class FakeBaseMode:
        mode = ctx.settings.base_mode
        svin_min_duration_s = 300
        svin_acc_limit_m = 2.0
        active_site_name = None

        async def apply_mode(self) -> None:
            calls.append(f"{self.mode.value}:{self.svin_min_duration_s}:{self.svin_acc_limit_m}")

    ctx.daemon.basemode = FakeBaseMode()
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"svin_min_duration_s": 600, "svin_acc_limit_m": 1.0}})
    assert r.json() == {"changed": ["svin_acc_limit_m", "svin_min_duration_s"], "restart_required": False}
    assert calls == ["survey-in:600:1.0"]
    assert ctx.settings.svin_min_duration_s == 600
    assert read_env(ctx.settings.mtrtk_env_file)["SVIN_MIN_DURATION_S"] == "600"


async def test_restart_sets_stop_event(ctx) -> None:
    import asyncio

    ctx.daemon = SimpleNamespace(controller=None, caster=None, basemode=None, stop=asyncio.Event())
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/restart")
    assert r.status_code == 200 and ctx.daemon.stop.is_set()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_envfile.py tests/unit/test_web_config.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.web.envfile'`.

- [ ] **Step 3: Add the env-file setting**

In `src/mtrtk/config.py`, in the identity/receiver block, add:
```python
    mtrtk_env_file: Path = Path(".env")  # where PUT /api/config persists changes
```

- [ ] **Step 4: Write `src/mtrtk/web/envfile.py`**

```python
"""Minimal .env reader/writer that preserves comments and unrelated lines."""

from __future__ import annotations

import os
import re
from enum import Enum
from pathlib import Path
from typing import Any

_LINE_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*?)(?P<comment>\s+#.*)?$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not Path(path).exists():
        return values
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith('"') or "=" not in line:
            continue
        key, rest = line.split("=", 1)
        rest = rest.strip()
        if rest.startswith(("'", '"')):
            values[key.strip()] = _unquote(rest.split(rest[0], 2)[1] if rest.count(rest[0]) >= 2 else rest)
            continue
        m = _LINE_RE.match(line)
        values[key.strip()] = _unquote(m["value"]) if m else _unquote(rest)
    return values


def to_env_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


def update_env(path: Path, updates: dict[str, str]) -> None:
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    out: list[str] = []
    for raw in lines:
        m = _LINE_RE.match(raw.strip())
        if m and m["key"] in pending:
            comment = m["comment"] or ""
            out.append(f"{m['key']}={pending.pop(m['key'])}{comment}")
        else:
            out.append(raw)
    for key, value in pending.items():
        out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)
```

- [ ] **Step 5: Write `src/mtrtk/web/api/config.py`**

```python
"""GET/PUT /api/config and POST /api/restart."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from mtrtk.config import Settings
from mtrtk.web.envfile import to_env_value, update_env

router = APIRouter(prefix="/api", tags=["config"])

SECRET_KEYS = {"ntrip_password", "web_password", "alert_webhook_url", "tunnel_token"}
LIVE_KEYS = {"base_mode", "svin_min_duration_s", "svin_acc_limit_m", "active_site"}
MASK = "***"


class ConfigBody(BaseModel):
    values: dict[str, Any]


def _masked(settings: Settings) -> dict[str, Any]:
    values = settings.model_dump(mode="json")
    for key in SECRET_KEYS:
        if values.get(key):
            values[key] = MASK
    return values


@router.get("/config")
async def get_config(request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    return {
        "values": _masked(ctx.settings),
        "env_file": str(ctx.settings.mtrtk_env_file),
        "secret_keys": sorted(SECRET_KEYS),
        "live_keys": sorted(LIVE_KEYS),
    }


@router.put("/config")
async def put_config(body: ConfigBody, request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    current = ctx.settings
    incoming = {k: v for k, v in body.values.items() if not (k in SECRET_KEYS and v == MASK)}
    unknown = sorted(set(incoming) - set(Settings.model_fields))
    if unknown:
        raise HTTPException(422, f"unknown settings: {unknown}")
    merged = current.model_dump() | incoming
    try:
        candidate = Settings(_env_file=None, **merged)
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    changed = sorted(k for k in incoming if getattr(candidate, k) != getattr(current, k))
    if not changed:
        return {"changed": [], "restart_required": False}
    update_env(current.mtrtk_env_file, {k.upper(): to_env_value(getattr(candidate, k)) for k in changed})
    live_only = set(changed) <= LIVE_KEYS
    basemode = ctx.basemode
    if live_only and basemode is not None:
        for key in changed:
            setattr(current, key, getattr(candidate, key))
        basemode.mode = current.base_mode
        basemode.svin_min_duration_s = current.svin_min_duration_s
        basemode.svin_acc_limit_m = current.svin_acc_limit_m
        basemode.active_site_name = current.active_site
        await basemode.apply_mode()
        return {"changed": changed, "restart_required": False}
    return {"changed": changed, "restart_required": True}


@router.post("/restart")
async def restart(request: Request) -> dict[str, bool]:
    ctx = request.app.state.ctx
    stop = getattr(ctx.daemon, "stop", None)
    if stop is None:
        raise HTTPException(409, "daemon not running")
    stop.set()  # the process exits; Docker/systemd restarts it with the new .env
    return {"ok": True}
```

`Settings` instances are mutable pydantic models (default `frozen=False`), so `setattr(current, key, ...)` works for live keys.

- [ ] **Step 6: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_envfile.py tests/unit/test_web_config.py -q` → `9 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

```bash
git add src/mtrtk/config.py src/mtrtk/web/envfile.py src/mtrtk/web/api/config.py tests/unit/test_envfile.py tests/unit/test_web_config.py
git commit -m "feat(web): configuration API with masked secrets, atomic .env updates and live re-apply

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Receiver actions (reapply, reset, poll)

**Files:**
- Modify: `src/mtrtk/core/receiver.py` (add `reapply()`, `reset(kind)`), `tests/unit/test_receiver.py` (add tests)
- Create: `src/mtrtk/web/api/receiver.py`, `tests/unit/test_web_receiver.py`

**Interfaces:**
- Produces: `ReceiverController.reapply() -> Capabilities` (re-runs `configure(link, first=True)` on the live link; `ReceiverError` if disconnected); `ReceiverController.reset(kind: Literal["hot","warm","cold","factory"]) -> None` (sends UBX-CFG-RST; factory first sends UBX-CFG-CFG clearing BBR+Flash and marks the next apply as first/flash); `GET /api/receiver` → `{connected, source, capabilities, firmware, passive}`; `POST /api/receiver/reapply`; `POST /api/receiver/reset {"kind"}`; `POST /api/receiver/poll {"msg_class","msg_id"}` → parsed message fields as JSON.
- Field names for UBX-CFG-RST / UBX-CFG-CFG **must be checked** before coding: `uv run python -c "from pyubx2.ubxtypes_set import UBX_PAYLOADS_SET as P; print(P['CFG-RST']); print(P['CFG-CFG'])"`. Expected: `CFG-RST` has `navBbrMask` (X002 bitfield), `resetMode` (U001); `CFG-CFG` has `clearMask`, `saveMask`, `loadMask` (X004) and a `deviceMask` bitfield with `devBBR`, `devFlash`, `devEEPROM`, `devSpiFlash`. Adapt the kwargs below to the printed names.

- [ ] **Step 1: Add controller tests to `tests/unit/test_receiver.py`**

```python
async def test_reapply_reconfigures_on_live_link(env) -> None:
    ctrl, link, rx, _ = env
    ctrl.link = link
    caps = await ctrl.reapply()
    assert caps.fw_version == "HPG 1.13" and rx.config["CFG_RATE_MEAS"] == 1000


async def test_reapply_without_link_raises(env) -> None:
    ctrl, *_ = env
    from mtrtk.core.receiver import ReceiverError

    with pytest.raises(ReceiverError):
        await ctrl.reapply()


async def test_reset_kinds_send_cfg_rst(env) -> None:
    ctrl, link, rx, _ = env
    ctrl.link = link
    ctrl._first_apply = False
    await ctrl.reset("cold")
    assert rx.writes[-1][2:4] == b"\x06\x04"  # CFG-RST
    await ctrl.reset("factory")
    assert rx.writes[-2][2:4] == b"\x06\x09" and rx.writes[-1][2:4] == b"\x06\x04"  # CFG-CFG then CFG-RST
    assert ctrl._first_apply is True
    with pytest.raises(ValueError):
        await ctrl.reset("nuke")  # type: ignore[arg-type]
```

- [ ] **Step 2: Write the failing API tests**

`tests/unit/test_web_receiver.py`:
```python
from pathlib import Path

import pytest
from webtest import client, make_ctx

from mtrtk.core.receiver import Capabilities, ReceiverError
from mtrtk.web.app import create_app


class FakeController:
    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.passive = False
        self.capabilities = Capabilities(protver="27.12", fw_version="HPG 1.13", module="ZED-F9P", supported={"MON-COMMS"}, unsupported={"MON-SPAN"})
        self.calls: list[str] = []

    async def reapply(self) -> Capabilities:
        if not self.connected:
            raise ReceiverError("receiver not connected")
        self.calls.append("reapply")
        return self.capabilities

    async def reset(self, kind: str) -> None:
        if kind not in ("hot", "warm", "cold", "factory"):
            raise ValueError(kind)
        self.calls.append(f"reset:{kind}")

    async def poll(self, msg_class: str, msg_id: str) -> dict:
        self.calls.append(f"poll:{msg_id}")
        return {"identity": msg_id, "swVersion": "EXT CORE 1.00 (f10c36)"}


@pytest.fixture
async def ctx(tmp_path: Path):
    c = await make_ctx(tmp_path)
    c.daemon.controller = FakeController()
    try:
        yield c
    finally:
        await c.db.close()


async def test_get_receiver(ctx) -> None:
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/receiver")).json()
    assert body["connected"] is True and body["capabilities"]["unsupported"] == ["MON-SPAN"]
    assert body["source"] == "auto" and body["passive"] is False


async def test_actions(ctx) -> None:
    async with client(create_app(ctx)) as c:
        assert (await c.post("/api/receiver/reapply")).json()["capabilities"]["fw_version"] == "HPG 1.13"
        assert (await c.post("/api/receiver/reset", json={"kind": "warm"})).json() == {"ok": True, "kind": "warm"}
        assert (await c.post("/api/receiver/reset", json={"kind": "bogus"})).status_code == 422
        r = await c.post("/api/receiver/poll", json={"msg_class": "MON", "msg_id": "MON-VER"})
    assert r.json()["swVersion"].startswith("EXT CORE")
    assert ctx.daemon.controller.calls == ["reapply", "reset:warm", "poll:MON-VER"]


async def test_actions_when_disconnected_are_409(ctx) -> None:
    ctx.daemon.controller.connected = False
    async with client(create_app(ctx)) as c:
        assert (await c.post("/api/receiver/reapply")).status_code == 409


async def test_no_controller_is_409(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path)
    try:
        async with client(create_app(ctx)) as c:
            assert (await c.post("/api/receiver/reapply")).status_code == 409
            assert (await c.get("/api/receiver")).json()["connected"] is False
    finally:
        await ctx.db.close()
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/unit/test_receiver.py tests/unit/test_web_receiver.py -q`
Expected: failures (`reapply`/`reset` missing; router missing).

- [ ] **Step 4: Extend `src/mtrtk/core/receiver.py`**

Add imports `from typing import Literal` and `from pyubx2 import SET, UBXMessage`; add to `ReceiverController`:
```python
    ResetKind = Literal["hot", "warm", "cold", "factory"]
    _RESET_MASKS = {"hot": 0x0000, "warm": 0x0001, "cold": 0xFFFF, "factory": 0xFFFF}

    async def reapply(self) -> Capabilities:
        if self.link is None or not self.connected:
            raise ReceiverError("receiver not connected")
        return await self.configure(self.link, first=True)

    async def reset(self, kind: ResetKind) -> None:
        if kind not in self._RESET_MASKS:
            raise ValueError(f"unknown reset kind {kind!r}")
        if self.link is None or not self.connected:
            raise ReceiverError("receiver not connected")
        if kind == "factory":
            # clear every configuration item in BBR and Flash, then re-apply our profile with flash on reconnect
            clear = UBXMessage("CFG", "CFG-CFG", SET, clearMask=0xFFFF, saveMask=0x0000, loadMask=0xFFFF, devBBR=1, devFlash=1)
            await self.link.write(clear.serialize())
            self._first_apply = True
        rst = UBXMessage("CFG", "CFG-RST", SET, navBbrMask=self._RESET_MASKS[kind], resetMode=0x01)
        await self.link.write(rst.serialize())
        log.warning("sent %s reset to the receiver; expect a reconnect", kind)

    async def poll(self, msg_class: str, msg_id: str) -> dict[str, Any]:
        if self.link is None or not self.connected:
            raise ReceiverError("receiver not connected")
        frame = await self.link.poll(msg_class, msg_id)
        parsed = frame.parsed()
        out = {k: (v.decode("ascii", "replace").rstrip("\x00") if isinstance(v, bytes) else v) for k, v in parsed.__dict__.items() if not k.startswith("_")}
        out["identity"] = frame.identity
        return out
```
`resetMode=0x01` is a hardware reset: the USB device re-enumerates and the controller's reconnect loop re-applies the profile (RAM layer, or all layers after a factory reset). Replace the CFG-CFG kwargs with the names printed in Step 0 if they differ.

- [ ] **Step 5: Write `src/mtrtk/web/api/receiver.py`**

```python
"""Receiver introspection and actions."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from mtrtk.core.receiver import ReceiverError

router = APIRouter(prefix="/api/receiver", tags=["receiver"])


class ResetBody(BaseModel):
    kind: Literal["hot", "warm", "cold", "factory"]


class PollBody(BaseModel):
    msg_class: str
    msg_id: str


def _controller(request: Request) -> Any:
    controller = request.app.state.ctx.controller
    if controller is None or not getattr(controller, "connected", False):
        raise HTTPException(409, "receiver not connected")
    return controller


def _caps_dict(caps: Any) -> dict[str, Any]:
    return {"protver": caps.protver, "fw_version": caps.fw_version, "module": caps.module, "supported": sorted(caps.supported), "unsupported": sorted(caps.unsupported)}


def _caps(controller: Any) -> dict[str, Any] | None:
    caps = getattr(controller, "capabilities", None)
    return _caps_dict(caps) if caps is not None else None


@router.get("")
async def get_receiver(request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    controller = ctx.controller
    return {
        "connected": bool(getattr(controller, "connected", False)),
        "passive": bool(getattr(controller, "passive", ctx.settings.source_is_file)),
        "source": ctx.settings.mtrtk_source,
        "capabilities": _caps(controller) if controller else None,
        "firmware": ctx.store.state.firmware.model_dump(mode="json"),
    }


@router.post("/reapply")
async def reapply(request: Request) -> dict[str, Any]:
    controller = _controller(request)
    try:
        caps = await controller.reapply()
    except ReceiverError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "capabilities": _caps_dict(caps)}


@router.post("/reset")
async def reset(body: ResetBody, request: Request) -> dict[str, Any]:
    controller = _controller(request)
    try:
        await controller.reset(body.kind)
    except (ReceiverError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "kind": body.kind}


@router.post("/poll")
async def poll(body: PollBody, request: Request) -> dict[str, Any]:
    controller = _controller(request)
    try:
        return await controller.poll(body.msg_class, body.msg_id)
    except ReceiverError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:  # unknown message names raise from pyubx2
        raise HTTPException(422, f"cannot poll {body.msg_id}: {exc}") from exc
```
- [ ] **Step 6: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_receiver.py tests/unit/test_web_receiver.py -q` → all pass; `uv run ruff check . && uv run ruff format . && uv run mypy`.

```bash
git add src/mtrtk/core/receiver.py src/mtrtk/web/api/receiver.py tests/unit/test_receiver.py tests/unit/test_web_receiver.py
git commit -m "feat(web): receiver endpoints (reapply, reset, poll) and controller reset support

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Base mode, survey-in and sites endpoints

**Files:**
- Create: `src/mtrtk/web/api/base.py`, `tests/unit/test_web_base.py`

**Interfaces:**
- Consumes: `BaseModeManager` (mode, applied_site, verified, last_1005, `apply_mode`, `freeze_survey_in`, `activate_site`), `SitesRepo`, `Site`, `envfile.update_env`.
- Produces: `GET /api/base/mode` → `{mode, site, verified, last_1005:{station_id,x,y,z}|null, svin:{min_duration_s, acc_limit_m}, available: bool}`; `PUT /api/base/mode {mode, svin_min_duration_s?, svin_acc_limit_m?, site?}`; `GET /api/base/survey` → SurveyIn JSON; `POST /api/base/survey/freeze {name, activate=false}` → Site; `GET /api/base/sites`; `POST /api/base/sites` body `{name, x,y,z}` or `{name, lat, lon, height_m}` plus optional `sigma_m, source, frame, epoch, notes`; `DELETE /api/base/sites/{name}`; `POST /api/base/sites/{name}/activate`. 409 with `"base mode manager not running"` when the daemon has no `basemode` (rover role or replay) for mode/freeze/activate-apply; sites CRUD works regardless.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_web_base.py`:
```python
from pathlib import Path

import pytest
from webtest import client, make_ctx

from mtrtk.base.rtcm1005 import Ecef1005
from mtrtk.config import BaseMode
from mtrtk.core.state import SurveyIn
from mtrtk.store.models import Site
from mtrtk.store.repos import SitesRepo
from mtrtk.web.app import create_app


class FakeBaseMode:
    def __init__(self, sites: SitesRepo, store) -> None:  # type: ignore[no-untyped-def]
        self.sites, self.store = sites, store
        self.mode = BaseMode.SURVEY_IN
        self.svin_min_duration_s, self.svin_acc_limit_m, self.active_site_name = 300, 2.0, None
        self.applied_site: Site | None = None
        self.verified = False
        self.last_1005 = Ecef1005(0, 1.0, 2.0, 3.0, True, True, True)
        self.applied: list[str] = []

    async def apply_mode(self) -> None:
        self.applied.append(self.mode.value)

    async def freeze_survey_in(self, name: str) -> Site:
        if not self.store.state.survey_in.valid:
            raise ValueError("survey-in is not valid yet")
        return await self.sites.add(Site.from_ecef(name, 10.0, 20.0, 30.0, sigma_m=1.1, source="survey-in"))

    async def activate_site(self, name: str) -> Site:
        site = await self.sites.activate(name)
        self.applied_site, self.mode = site, BaseMode.FIXED
        self.applied.append("fixed:" + name)
        return site


@pytest.fixture
async def ctx(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("BASE_MODE=survey-in\n")
    c = await make_ctx(tmp_path, mtrtk_env_file=env)
    c.daemon.basemode = FakeBaseMode(SitesRepo(c.db), c.store)
    try:
        yield c
    finally:
        await c.db.close()


async def test_get_mode(ctx) -> None:
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/base/mode")).json()
    assert body["mode"] == "survey-in" and body["site"] is None and body["available"] is True
    assert body["last_1005"] == {"station_id": 0, "x": 1.0, "y": 2.0, "z": 3.0}
    assert body["svin"] == {"min_duration_s": 300, "acc_limit_m": 2.0}


async def test_put_mode_survey_in_with_params_persists(ctx) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/base/mode", json={"mode": "survey-in", "svin_min_duration_s": 900, "svin_acc_limit_m": 0.5})
    assert r.status_code == 200 and r.json()["svin"] == {"min_duration_s": 900, "acc_limit_m": 0.5}
    assert ctx.daemon.basemode.applied == ["survey-in"]
    assert ctx.settings.svin_min_duration_s == 900
    assert "SVIN_MIN_DURATION_S=900" in ctx.settings.mtrtk_env_file.read_text()


async def test_put_mode_fixed_requires_site(ctx) -> None:
    async with client(create_app(ctx)) as c:
        assert (await c.put("/api/base/mode", json={"mode": "fixed"})).status_code == 409
        await c.post("/api/base/sites", json={"name": "roof", "x": 1.0, "y": 2.0, "z": 3.0, "source": "manual"})
        r = await c.put("/api/base/mode", json={"mode": "fixed", "site": "roof"})
    assert r.status_code == 200 and r.json()["mode"] == "fixed" and r.json()["site"] == "roof"
    assert ctx.daemon.basemode.applied == ["fixed:roof"]
    assert "BASE_MODE=fixed" in ctx.settings.mtrtk_env_file.read_text() and "ACTIVE_SITE=roof" in ctx.settings.mtrtk_env_file.read_text()


async def test_survey_and_freeze(ctx) -> None:
    async with client(create_app(ctx)) as c:
        assert (await c.get("/api/base/survey")).json()["valid"] is False
        assert (await c.post("/api/base/survey/freeze", json={"name": "roof"})).status_code == 409
        ctx.store.state.survey_in = SurveyIn(active=True, valid=True, mean_x_m=10.0, mean_y_m=20.0, mean_z_m=30.0, mean_acc_m=1.1, dur_s=300, obs=300)
        r = await c.post("/api/base/survey/freeze", json={"name": "roof", "activate": True})
        assert r.status_code == 200 and r.json()["name"] == "roof" and r.json()["active"] is True
        assert (await c.post("/api/base/survey/freeze", json={"name": "roof"})).status_code == 409  # duplicate name


async def test_sites_crud_and_activate(ctx) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/base/sites", json={"name": "llh", "lat": 23.8373506, "lon": 90.2625502, "height_m": -36.268, "sigma_m": 0.01})
        assert r.status_code == 200 and abs(r.json()["x"] - 1.0) > 1000
        r = await c.post("/api/base/sites", json={"name": "ecef", "x": 1234567.8912, "y": -987654.3234, "z": 5555555.0, "source": "csrs-ppp", "frame": "ITRF2020", "epoch": "2026.71"})
        assert r.status_code == 200 and r.json()["source"] == "csrs-ppp"
        assert (await c.post("/api/base/sites", json={"name": "bad"})).status_code == 422
        assert (await c.post("/api/base/sites", json={"name": "ecef", "x": 1, "y": 2, "z": 3})).status_code == 409
        names = [s["name"] for s in (await c.get("/api/base/sites")).json()]
        assert names == ["ecef", "llh"]
        r = await c.post("/api/base/sites/ecef/activate")
        assert r.status_code == 200 and r.json()["active"] is True
        assert ctx.daemon.basemode.applied[-1] == "fixed:ecef"
        assert (await c.post("/api/base/sites/nope/activate")).status_code == 404
        assert (await c.delete("/api/base/sites/llh")).status_code == 200
        assert [s["name"] for s in (await c.get("/api/base/sites")).json()] == ["ecef"]


async def test_without_basemode_sites_work_but_mode_is_409(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path)
    try:
        async with client(create_app(ctx)) as c:
            assert (await c.get("/api/base/mode")).json()["available"] is False
            assert (await c.put("/api/base/mode", json={"mode": "off"})).status_code == 409
            assert (await c.post("/api/base/sites", json={"name": "a", "x": 1, "y": 2, "z": 3})).status_code == 200
            r = await c.post("/api/base/sites/a/activate")
            assert r.status_code == 200 and r.json()["active"] is True  # DB only; applied when a base daemon runs
    finally:
        await ctx.db.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_web_base.py -q`
Expected: 404s (router missing).

- [ ] **Step 3: Write `src/mtrtk/web/api/base.py`**

```python
"""Base-station mode, survey-in and fixed sites."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, model_validator

from mtrtk.config import BaseMode
from mtrtk.core.geo import llh_to_ecef
from mtrtk.store.models import Site
from mtrtk.store.repos import SitesRepo
from mtrtk.web.envfile import to_env_value, update_env

router = APIRouter(prefix="/api/base", tags=["base"])


class ModeBody(BaseModel):
    mode: BaseMode
    svin_min_duration_s: int | None = None
    svin_acc_limit_m: float | None = None
    site: str | None = None


class FreezeBody(BaseModel):
    name: str
    activate: bool = False


class SiteBody(BaseModel):
    name: str
    x: float | None = None
    y: float | None = None
    z: float | None = None
    lat: float | None = None
    lon: float | None = None
    height_m: float | None = None
    sigma_m: float | None = None
    source: str = "manual"
    frame: str = "ITRF2020"
    epoch: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _coords(self) -> SiteBody:
        has_ecef = None not in (self.x, self.y, self.z)
        has_llh = None not in (self.lat, self.lon, self.height_m)
        if not has_ecef and not has_llh:
            raise ValueError("provide x,y,z (ECEF metres) or lat,lon,height_m")
        return self

    def to_site(self) -> Site:
        if None not in (self.x, self.y, self.z):
            x, y, z = float(self.x), float(self.y), float(self.z)  # type: ignore[arg-type]
        else:
            x, y, z = llh_to_ecef(float(self.lat), float(self.lon), float(self.height_m))  # type: ignore[arg-type]
        return Site.from_ecef(self.name, x, y, z, sigma_m=self.sigma_m, source=self.source, frame=self.frame, epoch=self.epoch, notes=self.notes)


def _basemode(request: Request) -> Any:
    bm = request.app.state.ctx.basemode
    if bm is None:
        raise HTTPException(409, "base mode manager not running (rover role or replay)")
    return bm


def _mode_view(request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    bm = ctx.basemode
    last = getattr(bm, "last_1005", None)
    return {
        "available": bm is not None,
        "mode": (bm.mode.value if bm else ctx.settings.base_mode.value),
        "site": (bm.applied_site.name if bm and bm.applied_site else None),
        "verified": bool(getattr(bm, "verified", False)),
        "last_1005": {"station_id": last.station_id, "x": last.x, "y": last.y, "z": last.z} if last else None,
        "svin": {
            "min_duration_s": bm.svin_min_duration_s if bm else ctx.settings.svin_min_duration_s,
            "acc_limit_m": bm.svin_acc_limit_m if bm else ctx.settings.svin_acc_limit_m,
        },
    }


@router.get("/mode")
async def get_mode(request: Request) -> dict[str, Any]:
    return _mode_view(request)


@router.put("/mode")
async def put_mode(body: ModeBody, request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    bm = _basemode(request)
    settings = ctx.settings
    env: dict[str, str] = {"BASE_MODE": body.mode.value}
    if body.svin_min_duration_s is not None:
        settings.svin_min_duration_s = bm.svin_min_duration_s = body.svin_min_duration_s
        env["SVIN_MIN_DURATION_S"] = str(body.svin_min_duration_s)
    if body.svin_acc_limit_m is not None:
        settings.svin_acc_limit_m = bm.svin_acc_limit_m = body.svin_acc_limit_m
        env["SVIN_ACC_LIMIT_M"] = to_env_value(body.svin_acc_limit_m)
    if body.mode is BaseMode.FIXED:
        repo = SitesRepo(ctx.db)
        active = await repo.active()
        name = body.site or (active.name if active else None)
        if not name or await repo.get(name) is None:
            raise HTTPException(409, "fixed mode needs an existing site: pass 'site' or activate one first")
        await bm.activate_site(name)
        settings.active_site = bm.active_site_name = name
        env["ACTIVE_SITE"] = name
    else:
        bm.mode = body.mode
        await bm.apply_mode()
    settings.base_mode = body.mode
    update_env(settings.mtrtk_env_file, env)
    return _mode_view(request)


@router.get("/survey")
async def get_survey(request: Request) -> dict[str, Any]:
    return request.app.state.ctx.store.state.survey_in.model_dump(mode="json")


@router.post("/survey/freeze")
async def freeze(body: FreezeBody, request: Request) -> dict[str, Any]:
    bm = _basemode(request)
    try:
        site = await bm.freeze_survey_in(body.name)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if body.activate:
        site = await bm.activate_site(site.name)
    return site.model_dump(mode="json")


@router.get("/sites")
async def list_sites(request: Request) -> list[dict[str, Any]]:
    return [s.model_dump(mode="json") for s in await SitesRepo(request.app.state.ctx.db).list()]


@router.post("/sites")
async def add_site(body: SiteBody, request: Request) -> dict[str, Any]:
    try:
        site = await SitesRepo(request.app.state.ctx.db).add(body.to_site())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return site.model_dump(mode="json")


@router.delete("/sites/{name}")
async def delete_site(name: str, request: Request) -> dict[str, bool]:
    await SitesRepo(request.app.state.ctx.db).delete(name)
    return {"ok": True}


@router.post("/sites/{name}/activate")
async def activate_site(name: str, request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    repo = SitesRepo(ctx.db)
    if await repo.get(name) is None:
        raise HTTPException(404, f"no site named {name!r}")
    bm = ctx.basemode
    site = await bm.activate_site(name) if bm is not None else await repo.activate(name)
    ctx.settings.active_site = name
    return site.model_dump(mode="json")
```

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_web_base.py -q` → `6 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

```bash
git add src/mtrtk/web/api/base.py tests/unit/test_web_base.py
git commit -m "feat(web): base mode, survey-in freeze and sites endpoints

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: NTRIP and raw-log endpoints

**Files:**
- Create: `src/mtrtk/web/api/ntrip.py`, `src/mtrtk/web/api/logs.py`, `tests/unit/test_web_ntrip_logs.py`
- Modify: `tests/unit/webtest.py` (add `make_log`)

**Interfaces:**
- Produces: `GET /api/ntrip` → `{running, host, port, mountpoint, anonymous, username, connection_url (password masked), sourcetable}`; `GET /api/ntrip/clients` → list of `ClientInfo.public()`; `GET /api/ntrip/history?limit=100` → `NtripClientRecord` JSON list. `GET /api/logs` → `{"files":[LogFile JSON...], "total_bytes", "hours", "disk_free_gb"}`; `GET /api/logs/availability?from=&to=` → `[{hour_utc, available, bytes, complete}]`; `GET /api/logs/{name}` → file download; `PATCH /api/logs/{name} {"keep": bool}` → updates sidecar + `log_files` row; `DELETE /api/logs/{name}` → 409 for the newest (open) file unless `?force=1`; `GET /api/logs/window?from=&to=` → streaming concatenation of the hourly files overlapping the window, `Content-Disposition: attachment; filename="MTRK_YYYYMMDDHH_YYYYMMDDHH.ubx"`.
- File names are validated with `parse_log_name`; any other name → 404.

- [ ] **Step 1: Extend `tests/unit/webtest.py`**

Append:
```python
from datetime import UTC, datetime, timedelta

from mtrtk.rawlog.writer import Sidecar, log_path, sidecar_path


def make_log(root: Path, hour: datetime, size: int = 1000, keep: bool = False, station: str = "MTRK") -> Path:
    path = log_path(root, station, hour)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes([hour.hour]) * size)
    Sidecar(station, "base", hour.isoformat(), end_utc=(hour + timedelta(hours=1)).isoformat(), hour_utc=hour.isoformat(),
            bytes=size, keep=keep, complete=True, msg_counts={"RXM-RAWX": 3600}).dump(sidecar_path(path))
    return path


H0 = datetime(2026, 9, 18, 10, tzinfo=UTC)
```

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_web_ntrip_logs.py`:
```python
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from webtest import H0, client, make_ctx, make_log

from mtrtk.base.ntrip_caster import ClientInfo
from mtrtk.rawlog.writer import Sidecar, sidecar_path
from mtrtk.store.repos import NtripLogRepo
from mtrtk.web.app import create_app


@pytest.fixture
async def ctx(tmp_path: Path):
    c = await make_ctx(tmp_path, ntrip_user="rover")
    try:
        yield c
    finally:
        await c.db.close()


async def test_ntrip_info_without_caster(ctx) -> None:
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/ntrip")).json()
    assert body["running"] is False and body["mountpoint"] == "MTRK" and body["anonymous"] is False
    assert body["connection_url"] == "ntrip://rover:***@<bind-address>:2101/MTRK"
    async with client(create_app(ctx)) as c:
        assert (await c.get("/api/ntrip/clients")).json() == []


async def test_ntrip_info_with_caster_and_history(ctx) -> None:
    from datetime import UTC, datetime

    info = ClientInfo(id=1, ip="100.100.50.12", port=5000, mountpoint="MTRK", user_agent="NTRIP SWMaps", username="rover", version=2, connected_utc=datetime.now(UTC), bytes_sent=123)
    ctx.daemon.caster = SimpleNamespace(host="100.100.50.10", port=2101, clients={1: info}, sourcetable_body=lambda: b"STR;MTRK;...\r\nENDSOURCETABLE\r\n", config=SimpleNamespace(anonymous=False))
    row = await NtripLogRepo(ctx.db).connected("100.100.50.12", "MTRK", "NTRIP SWMaps", "rover")
    await NtripLogRepo(ctx.db).disconnected(row, 555, 23.8, 90.2, "client closed")
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/ntrip")).json()
        assert body["running"] is True and body["connection_url"] == "ntrip://rover:***@100.100.50.10:2101/MTRK"
        assert body["sourcetable"].startswith("STR;MTRK")
        clients = (await c.get("/api/ntrip/clients")).json()
        assert clients[0]["ip"] == "100.100.50.12" and clients[0]["bytes_sent"] == 123
        history = (await c.get("/api/ntrip/history")).json()
    assert history[0]["bytes_sent"] == 555 and history[0]["reason"] == "client closed"


async def test_logs_list_availability_download_keep_delete(ctx, tmp_path: Path) -> None:
    for i in range(3):
        make_log(tmp_path, H0 + timedelta(hours=i), size=100 * (i + 1))
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/logs")).json()
        assert [f["hour_utc"] for f in body["files"]] == [(H0 + timedelta(hours=i)).isoformat() for i in range(3)]
        assert body["total_bytes"] == 600 and body["hours"] == 3 and body["disk_free_gb"] > 0
        avail = (await c.get("/api/logs/availability", params={"from": H0.isoformat(), "to": (H0 + timedelta(hours=5)).isoformat()})).json()
        assert [a["available"] for a in avail] == [True, True, True, False, False]
        name = "MTRK_20260918_10.ubx"
        r = await c.get(f"/api/logs/{name}")
        assert r.status_code == 200 and r.content == bytes([10]) * 100 and "attachment" in r.headers["content-disposition"]
        assert (await c.get("/api/logs/../../etc/passwd")).status_code in (404, 422)
        assert (await c.get("/api/logs/notalog.ubx")).status_code == 404
        r = await c.patch(f"/api/logs/{name}", json={"keep": True})
        assert r.status_code == 200 and r.json()["keep"] is True
        assert Sidecar.load(sidecar_path(tmp_path / "ubx" / "2026" / "261" / name)).keep is True
        newest = "MTRK_20260918_12.ubx"
        assert (await c.delete(f"/api/logs/{newest}")).status_code == 409  # newest is protected
        assert (await c.delete("/api/logs/MTRK_20260918_11.ubx")).status_code == 200  # older: fine
        assert (await c.delete(f"/api/logs/{newest}", params={"force": 1})).status_code == 200
        assert len((await c.get("/api/logs")).json()["files"]) == 1


async def test_logs_window_concatenates(ctx, tmp_path: Path) -> None:
    for i in range(3):
        make_log(tmp_path, H0 + timedelta(hours=i), size=10)
    async with client(create_app(ctx)) as c:
        r = await c.get("/api/logs/window", params={"from": (H0 + timedelta(minutes=30)).isoformat(), "to": (H0 + timedelta(hours=2, minutes=30)).isoformat()})
    assert r.status_code == 200
    assert r.content == bytes([10]) * 10 + bytes([11]) * 10 + bytes([12]) * 10
    assert r.headers["content-disposition"] == 'attachment; filename="MTRK_2026091810_2026091812.ubx"'
    async with client(create_app(ctx)) as c:
        assert (await c.get("/api/logs/window", params={"from": (H0 + timedelta(days=5)).isoformat(), "to": (H0 + timedelta(days=6)).isoformat()})).status_code == 404
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/unit/test_web_ntrip_logs.py -q`
Expected: 404s (routers missing).

- [ ] **Step 4: Write `src/mtrtk/web/api/ntrip.py`**

```python
"""NTRIP caster status, live clients and connection history."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from mtrtk.store.repos import NtripLogRepo

router = APIRouter(prefix="/api/ntrip", tags=["ntrip"])


@router.get("")
async def info(request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    s = ctx.settings
    caster = ctx.caster
    host = getattr(caster, "host", None) or "<bind-address>"
    port = getattr(caster, "port", None) or s.ntrip_port
    anonymous = s.ntrip_anonymous
    cred = "" if anonymous else f"{s.ntrip_user}:***@"
    return {
        "running": caster is not None,
        "host": host,
        "port": port,
        "mountpoint": s.mountpoint,
        "anonymous": anonymous,
        "username": None if anonymous else s.ntrip_user,
        "bind_mode": s.ntrip_bind,
        "connection_url": f"ntrip://{cred}{host}:{port}/{s.mountpoint}",
        "sourcetable": caster.sourcetable_body().decode("latin-1") if caster is not None else None,
    }


@router.get("/clients")
async def clients(request: Request) -> list[dict[str, Any]]:
    caster = request.app.state.ctx.caster
    if caster is None:
        return []
    return [c.public() for c in caster.clients.values()]


@router.get("/history")
async def history(request: Request, limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
    rows = await NtripLogRepo(request.app.state.ctx.db).recent(limit)
    return [r.model_dump(mode="json") for r in rows]
```

- [ ] **Step 5: Write `src/mtrtk/web/api/logs.py`**

```python
"""Raw UBX log files: list, availability, download, keep flag, delete, window export."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from mtrtk.rawlog.index import LogFile, files_for_window, hour_availability, list_logs, parse_log_name
from mtrtk.rawlog.writer import Sidecar, log_path, sidecar_path
from mtrtk.store.repos import LogFilesRepo

router = APIRouter(prefix="/api/logs", tags=["logs"])
CHUNK = 1 << 20


class KeepBody(BaseModel):
    keep: bool


def _logfile_json(lf: LogFile) -> dict[str, Any]:
    return {
        "name": lf.path.name, "hour_utc": lf.hour_utc.isoformat(), "bytes": lf.bytes, "complete": lf.complete,
        "keep": lf.keep, "msg_counts": lf.msg_counts, "start_utc": lf.start_utc, "end_utc": lf.end_utc,
    }


def _resolve(request: Request, name: str) -> tuple[Path, LogFile]:
    parsed = parse_log_name(Path(name))
    if parsed is None or "/" in name or name != Path(name).name:
        raise HTTPException(404, "not a log file name")
    station, hour = parsed
    root = request.app.state.ctx.settings.data_dir
    path = log_path(root, station, hour)
    if not path.exists():
        raise HTTPException(404, "log file not found")
    lf = next((x for x in list_logs(root) if x.path == path), None)
    if lf is None:
        raise HTTPException(404, "log file not indexed")
    return path, lf


def _parse_dt(value: str, label: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, f"{label} must be ISO-8601") from exc
    if dt.tzinfo is None:
        raise HTTPException(422, f"{label} must include a timezone (use Z or +00:00)")
    return dt


@router.get("")
async def list_files(request: Request) -> dict[str, Any]:
    root = request.app.state.ctx.settings.data_dir
    files = list_logs(root)
    target = root if root.exists() else root.parent
    return {
        "files": [_logfile_json(lf) for lf in files],
        "total_bytes": sum(lf.bytes for lf in files),
        "hours": len(files),
        "disk_free_gb": shutil.disk_usage(target).free / 1e9,
    }


@router.get("/availability")
async def availability(request: Request, from_: str = Query(alias="from"), to: str = Query()) -> list[dict[str, Any]]:
    root = request.app.state.ctx.settings.data_dir
    slots = hour_availability(root, _parse_dt(from_, "from"), _parse_dt(to, "to"))
    return [
        {"hour_utc": s.hour_utc.isoformat(), "available": s.file is not None, "bytes": s.file.bytes if s.file else 0, "complete": s.file.complete if s.file else False}
        for s in slots
    ]


@router.get("/window")
async def window(request: Request, from_: str = Query(alias="from"), to: str = Query()) -> StreamingResponse:
    root = request.app.state.ctx.settings.data_dir
    start, end = _parse_dt(from_, "from"), _parse_dt(to, "to")
    files = files_for_window(root, start, end)
    if not files:
        raise HTTPException(404, "no raw logs in that window")

    async def body() -> AsyncIterator[bytes]:
        for lf in files:
            with lf.path.open("rb") as fh:
                while chunk := fh.read(CHUNK):
                    yield chunk

    name = f"{files[0].station_id}_{files[0].hour_utc:%Y%m%d%H}_{files[-1].hour_utc:%Y%m%d%H}.ubx"
    return StreamingResponse(body(), media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/{name}")
async def download(name: str, request: Request) -> FileResponse:
    path, _ = _resolve(request, name)
    return FileResponse(path, media_type="application/octet-stream", filename=name)


@router.patch("/{name}")
async def set_keep(name: str, body: KeepBody, request: Request) -> dict[str, Any]:
    path, _ = _resolve(request, name)
    sc_path = sidecar_path(path)
    sidecar = Sidecar.load(sc_path) if sc_path.exists() else Sidecar(request.app.state.ctx.settings.station_id, "base", None)
    sidecar.keep = body.keep
    sidecar.dump(sc_path)
    await LogFilesRepo(request.app.state.ctx.db).upsert(path, sidecar)
    _, lf = _resolve(request, name)
    return _logfile_json(lf)


@router.delete("/{name}")
async def delete(name: str, request: Request, force: int = Query(0)) -> dict[str, bool]:
    path, lf = _resolve(request, name)
    newest = list_logs(request.app.state.ctx.settings.data_dir)[-1]
    if lf.path == newest.path and not force:
        raise HTTPException(409, "this is the newest log (probably being written); pass ?force=1 to delete anyway")
    path.unlink(missing_ok=True)
    sidecar_path(path).unlink(missing_ok=True)
    await LogFilesRepo(request.app.state.ctx.db).delete(path)
    return {"ok": True}
```

Route order matters: `/availability` and `/window` are declared before `/{name}` so they are not captured by the path parameter.

- [ ] **Step 6: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_web_ntrip_logs.py -q` → `4 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

```bash
git add src/mtrtk/web/api/ntrip.py src/mtrtk/web/api/logs.py tests/unit/webtest.py tests/unit/test_web_ntrip_logs.py
git commit -m "feat(web): NTRIP status/history and raw log list, download, keep, delete and window export

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: History and events endpoints

**Files:**
- Create: `src/mtrtk/web/api/history.py`, `src/mtrtk/web/api/events.py`, `tests/unit/test_web_history_events.py`

**Interfaces:**
- Consumes: `Sampler.history(table, start_ts, end_ts, columns)`, `SAMPLE_COLUMNS`, `EventsRepo`.
- Produces: `GET /api/history?metrics=h_acc_m,nsat_used&from=<iso>&to=<iso>&res=auto|1s|1m` → `{"res": "1s"|"1m", "columns": ["ts", ...], "rows": [[ts, v1, v2, ...], ...]}`; `res=auto` picks `1m` when the window exceeds 6 h. For `1m`, a bare metric name maps to `<metric>_avg` when that column exists (else the exact name must exist in `samples_1m`). Unknown metric → 422. `GET /api/history/metrics` → `{"1s": [...], "1m": [...]}`. `GET /api/events?limit=200&level=` → Event list (newest first); `POST /api/events/{id}/ack` → `{"ok": true}`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_web_history_events.py`:
```python
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from webtest import client, make_ctx

from mtrtk.core.state import ReceiverState
from mtrtk.store.repos import EventsRepo
from mtrtk.store.sampler import Sampler
from mtrtk.web.app import create_app

T0 = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)


def state_at(t: datetime, h_acc: float) -> ReceiverState:
    s = ReceiverState()
    s.time.utc = t
    s.accuracy.h_acc_m = h_acc
    s.sat_summary.used = 12
    s.fix.fix_type = 3
    return s


@pytest.fixture
async def ctx(tmp_path: Path):
    c = await make_ctx(tmp_path)
    sampler = Sampler(c.bus, c.db)
    for i in range(120):
        await sampler.insert(sampler.sample_row(state_at(T0 + timedelta(seconds=i), 1.0 + i / 100), None, 0))
    await sampler.rollup_minute(T0.timestamp())
    await sampler.rollup_minute(T0.timestamp() + 60)
    try:
        yield c
    finally:
        await c.db.close()


async def test_history_1s(ctx) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.get("/api/history", params={"metrics": "h_acc_m,nsat_used", "from": T0.isoformat(), "to": (T0 + timedelta(seconds=10)).isoformat(), "res": "1s"})
    body = r.json()
    assert r.status_code == 200 and body["res"] == "1s" and body["columns"] == ["ts", "h_acc_m", "nsat_used"]
    assert len(body["rows"]) == 10 and body["rows"][0][1] == 1.0 and body["rows"][0][2] == 12


async def test_history_1m_maps_avg_and_auto_res(ctx) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.get("/api/history", params={"metrics": "h_acc_m,h_acc_max,fix_type_min", "from": T0.isoformat(), "to": (T0 + timedelta(hours=7)).isoformat()})
    body = r.json()
    assert body["res"] == "1m" and body["columns"] == ["ts", "h_acc_avg", "h_acc_max", "fix_type_min"]
    assert len(body["rows"]) == 2 and abs(body["rows"][0][1] - 1.295) < 1e-9 and body["rows"][0][2] == 1.59


async def test_history_validation(ctx) -> None:
    async with client(create_app(ctx)) as c:
        assert (await c.get("/api/history", params={"metrics": "nope", "from": T0.isoformat(), "to": T0.isoformat()})).status_code == 422
        assert (await c.get("/api/history", params={"metrics": "h_acc_m", "from": "yesterday", "to": T0.isoformat()})).status_code == 422
        metrics = (await c.get("/api/history/metrics")).json()
    assert "h_acc_m" in metrics["1s"] and "h_acc_avg" in metrics["1m"] and "ts" not in metrics["1s"]


async def test_events_list_and_ack(ctx) -> None:
    repo = EventsRepo(ctx.db)
    e = await repo.add("warning", "jamming", "jam_ind 210", {"jam_ind": 210})
    await repo.add("info", "survey_in_valid", "done")
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/events")).json()
        assert [x["kind"] for x in body] == ["survey_in_valid", "jamming"] and body[1]["meta"] == {"jam_ind": 210}
        assert [x["kind"] for x in (await c.get("/api/events", params={"level": "warning"})).json()] == ["jamming"]
        assert (await c.post(f"/api/events/{e.id}/ack")).json() == {"ok": True}
        assert (await c.get("/api/events", params={"level": "warning"})).json()[0]["acked"] is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_web_history_events.py -q`
Expected: 404s.

- [ ] **Step 3: Write `src/mtrtk/web/api/history.py`**

```python
"""Time-series history for charts."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from mtrtk.store.sampler import SAMPLE_COLUMNS, Sampler

router = APIRouter(prefix="/api/history", tags=["history"])
AUTO_1M_THRESHOLD = timedelta(hours=6)


async def _columns_1m(request: Request) -> list[str]:
    cache = getattr(request.app.state, "columns_1m", None)
    if cache is None:
        rows = await request.app.state.ctx.db.fetchall("PRAGMA table_info(samples_1m)")
        cache = [r["name"] for r in rows if r["name"] != "ts"]
        request.app.state.columns_1m = cache
    return cache


def _parse(value: str, label: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, f"{label} must be ISO-8601") from exc
    if dt.tzinfo is None:
        raise HTTPException(422, f"{label} must include a timezone")
    return dt


@router.get("/metrics")
async def metrics(request: Request) -> dict[str, list[str]]:
    return {"1s": [c for c in SAMPLE_COLUMNS if c != "ts"], "1m": await _columns_1m(request)}


@router.get("")
async def history(
    request: Request,
    metrics_csv: str = Query(alias="metrics", description="comma-separated column names"),
    from_: str = Query(alias="from"),
    to: str = Query(),
    res: Literal["auto", "1s", "1m"] = "auto",
) -> dict[str, Any]:
    ctx = request.app.state.ctx
    start, end = _parse(from_, "from"), _parse(to, "to")
    if res == "auto":
        res = "1m" if end - start > AUTO_1M_THRESHOLD else "1s"
    wanted = [m.strip() for m in metrics_csv.split(",") if m.strip()]
    if not wanted:
        raise HTTPException(422, "metrics is empty")
    if res == "1s":
        valid = set(SAMPLE_COLUMNS) - {"ts"}
        columns = wanted
        table = "samples_1s"
    else:
        valid = set(await _columns_1m(request))
        columns = [m if m in valid else (f"{m}_avg" if f"{m}_avg" in valid else m) for m in wanted]
        table = "samples_1m"
    unknown = [c for c in columns if c not in valid]
    if unknown:
        raise HTTPException(422, f"unknown metrics for {res}: {unknown}")
    sampler = Sampler(ctx.bus, ctx.db)
    rows = await sampler.history(table, start.timestamp(), end.timestamp(), ["ts", *columns])
    sampler.stop()
    return {"res": res, "columns": ["ts", *columns], "rows": [[r["ts"], *[r[c] for c in columns]] for r in rows]}
```

- [ ] **Step 4: Write `src/mtrtk/web/api/events.py`**

```python
"""Event log."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query, Request

from mtrtk.store.repos import EventsRepo

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("")
async def list_events(request: Request, limit: int = Query(200, ge=1, le=2000), level: Literal["info", "warning", "error"] | None = None) -> list[dict[str, Any]]:
    events = await EventsRepo(request.app.state.ctx.db).list(limit=limit, level=level)
    return [e.model_dump(mode="json") for e in events]


@router.post("/{event_id}/ack")
async def ack(event_id: int, request: Request) -> dict[str, bool]:
    await EventsRepo(request.app.state.ctx.db).ack(event_id)
    return {"ok": True}
```

- [ ] **Step 5: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_web_history_events.py -q` → `4 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

```bash
git add src/mtrtk/web/api/history.py src/mtrtk/web/api/events.py tests/unit/test_web_history_events.py
git commit -m "feat(web): history query endpoint with 1s/1m resolution and events API

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Job runner and jobs endpoints

**Files:**
- Create: `src/mtrtk/jobs.py`, `src/mtrtk/store/schema/002_jobs_message.sql`, `src/mtrtk/web/api/jobs.py`, `tests/unit/test_jobs.py`, `tests/unit/test_web_jobs.py`
- Modify: `tests/unit/test_store.py` (expect `user_version == 2`)

**Interfaces:**
- Produces: `Job` (pydantic: `id, kind, status, created_utc, updated_utc, progress, message, params, result, error`); `JobContext(job, dir, progress: Callable)` with `await ctx.progress(fraction, message=None)`; `JobFn = Callable[[JobContext], Awaitable[dict[str, Any]]]`; `JobRunner(db, bus, root, max_concurrent=1)` with `await submit(kind, params, fn) -> Job`, `await get(job_id) -> Job | None`, `await list(kind=None, limit=50) -> list[Job]`, `await delete(job_id)`, `result_path(job_id, name) -> Path` (rejects path escapes), `await restore()` (marks `queued/running` rows as `failed: interrupted by restart`), `await shutdown()`; bus topic `jobs.update` (Job) on every change. Endpoints: `GET /api/jobs?kind&limit`, `GET /api/jobs/{id}`, `DELETE /api/jobs/{id}`, `GET /api/jobs/{id}/files` → `[{name, bytes}]`, `GET /api/jobs/{id}/files/{name}` → download. 409 if `ctx.jobs` is None.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_jobs.py`:
```python
import asyncio
from pathlib import Path

import pytest

from mtrtk.core.bus import Bus
from mtrtk.jobs import JobContext, JobRunner
from mtrtk.store.db import Database


@pytest.fixture
async def runner(tmp_path: Path):
    db = Database(tmp_path / "m.db")
    await db.open()
    bus = Bus()
    r = JobRunner(db, bus, tmp_path / "jobs")
    try:
        yield r, bus
    finally:
        await r.shutdown()
        await db.close()


async def test_job_lifecycle_and_result_files(runner) -> None:
    r, bus = runner
    updates = bus.subscribe("jobs.update")

    async def work(ctx: JobContext) -> dict:
        await ctx.progress(0.5, "halfway")
        (ctx.dir / "out.txt").write_text("hello")
        return {"files": ["out.txt"], "n": 1}

    job = await r.submit("demo", {"a": 1}, work)
    assert job.status == "queued" and job.params == {"a": 1} and len(job.id) == 12
    for _ in range(50):
        await asyncio.sleep(0.01)
        current = await r.get(job.id)
        if current and current.status == "done":
            break
    assert current.status == "done" and current.progress == 1.0 and current.result == {"files": ["out.txt"], "n": 1}
    assert r.result_path(job.id, "out.txt").read_text() == "hello"
    published = [item for _, item in [updates.queue.get_nowait() for _ in range(updates.queue.qsize())]]
    statuses = [item.status for item in published]
    assert statuses[0] == "queued" and "running" in statuses and statuses[-1] == "done"
    assert any(item.message == "halfway" and item.progress == 0.5 for item in published)
    listed = await r.list(kind="demo")
    assert [j.id for j in listed] == [job.id]


async def test_failed_job_records_error(runner) -> None:
    r, _ = runner

    async def boom(ctx: JobContext) -> dict:
        raise RuntimeError("convbin exited 1")

    job = await r.submit("export", {}, boom)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if (await r.get(job.id)).status == "failed":
            break
    failed = await r.get(job.id)
    assert failed.status == "failed" and failed.error == "RuntimeError: convbin exited 1"


async def test_jobs_run_one_at_a_time(runner) -> None:
    r, _ = runner
    running: list[int] = []
    peak = 0

    async def slow(ctx: JobContext) -> dict:
        nonlocal peak
        running.append(1)
        peak = max(peak, len(running))
        await asyncio.sleep(0.02)
        running.pop()
        return {}

    jobs = [await r.submit("slow", {}, slow) for _ in range(3)]
    for _ in range(100):
        await asyncio.sleep(0.01)
        if all((await r.get(j.id)).status == "done" for j in jobs):
            break
    assert peak == 1


async def test_delete_removes_dir_and_row(runner) -> None:
    r, _ = runner

    async def work(ctx: JobContext) -> dict:
        (ctx.dir / "x").write_text("x")
        return {}

    job = await r.submit("demo", {}, work)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if (await r.get(job.id)).status == "done":
            break
    assert (r.root / job.id).exists()
    await r.delete(job.id)
    assert not (r.root / job.id).exists() and await r.get(job.id) is None
    with pytest.raises(ValueError):
        r.result_path(job.id, "../../etc/passwd")


async def test_restore_marks_interrupted(tmp_path: Path) -> None:
    db = Database(tmp_path / "m.db")
    await db.open()
    await db.execute("INSERT INTO jobs (id, kind, status, created_utc, progress, params) VALUES ('abc', 'export', 'running', '2026-09-18T00:00:00+00:00', 0.3, '{}')")
    await db.commit()
    r = JobRunner(db, Bus(), tmp_path / "jobs")
    await r.restore()
    job = await r.get("abc")
    assert job.status == "failed" and job.error == "interrupted by restart"
    await db.close()
```

`tests/unit/test_web_jobs.py`:
```python
import asyncio
from pathlib import Path

import pytest
from webtest import client, make_ctx

from mtrtk.jobs import JobContext, JobRunner
from mtrtk.web.app import create_app


@pytest.fixture
async def ctx(tmp_path: Path):
    c = await make_ctx(tmp_path)
    c.jobs = JobRunner(c.db, c.bus, tmp_path / "jobs")
    try:
        yield c
    finally:
        await c.jobs.shutdown()
        await c.db.close()


async def test_jobs_endpoints(ctx) -> None:
    async def work(jctx: JobContext) -> dict:
        (jctx.dir / "result.csv").write_text("a,b\n1,2\n")
        return {"rows": 1}

    job = await ctx.jobs.submit("export", {"preset": "csrs"}, work)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if (await ctx.jobs.get(job.id)).status == "done":
            break
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/jobs")).json()
        assert body[0]["id"] == job.id and body[0]["status"] == "done" and body[0]["params"] == {"preset": "csrs"}
        assert (await c.get("/api/jobs", params={"kind": "ppk"})).json() == []
        assert (await c.get(f"/api/jobs/{job.id}")).json()["result"] == {"rows": 1}
        assert (await c.get("/api/jobs/nope")).status_code == 404
        files = (await c.get(f"/api/jobs/{job.id}/files")).json()
        assert files == [{"name": "result.csv", "bytes": 8}]
        r = await c.get(f"/api/jobs/{job.id}/files/result.csv")
        assert r.status_code == 200 and r.text == "a,b\n1,2\n"
        assert (await c.get(f"/api/jobs/{job.id}/files/..%2F..%2Fetc%2Fpasswd")).status_code in (404, 422)
        assert (await c.delete(f"/api/jobs/{job.id}")).json() == {"ok": True}
        assert (await c.get(f"/api/jobs/{job.id}")).status_code == 404


async def test_jobs_409_without_runner(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path)
    try:
        async with client(create_app(ctx)) as c:
            assert (await c.get("/api/jobs")).status_code == 409
    finally:
        await ctx.db.close()
```

In `tests/unit/test_store.py` change both `user_version == 1` assertions to `== 2`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_jobs.py tests/unit/test_web_jobs.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.jobs'`.

- [ ] **Step 3: Write the migration `src/mtrtk/store/schema/002_jobs_message.sql`**

```sql
-- v2: human-readable progress message on jobs
ALTER TABLE jobs ADD COLUMN message TEXT;
```

- [ ] **Step 4: Write `src/mtrtk/jobs.py`**

```python
"""Persistent background jobs (RINEX export, PPK) with progress, result directories and bus updates."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from mtrtk.core.bus import Bus
from mtrtk.store.db import Database

log = logging.getLogger(__name__)

Status = Literal["queued", "running", "done", "failed"]


class Job(BaseModel):
    id: str
    kind: str
    status: Status
    created_utc: datetime
    updated_utc: datetime | None = None
    progress: float = 0.0
    message: str | None = None
    params: dict[str, Any] = {}
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class JobContext:
    job: Job
    dir: Path
    _report: Callable[[float, str | None], Awaitable[None]]

    async def progress(self, fraction: float, message: str | None = None) -> None:
        await self._report(max(0.0, min(1.0, fraction)), message)


JobFn = Callable[[JobContext], Awaitable[dict[str, Any]]]


def _now() -> str:
    return datetime.now(UTC).isoformat()


class JobRunner:
    def __init__(self, db: Database, bus: Bus, root: Path, max_concurrent: int = 1) -> None:
        self.db = db
        self.bus = bus
        self.root = Path(root)
        self._sem = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------- persistence
    @staticmethod
    def _row_to_job(row: Any) -> Job:
        data = dict(row)
        data["params"] = json.loads(data.get("params") or "{}")
        data["result"] = json.loads(data["result"]) if data.get("result") else None
        return Job(**data)

    async def get(self, job_id: str) -> Job | None:
        row = await self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return self._row_to_job(row) if row else None

    async def list(self, kind: str | None = None, limit: int = 50) -> list[Job]:
        if kind:
            rows = await self.db.fetchall("SELECT * FROM jobs WHERE kind = ? ORDER BY created_utc DESC LIMIT ?", (kind, limit))
        else:
            rows = await self.db.fetchall("SELECT * FROM jobs ORDER BY created_utc DESC LIMIT ?", (limit,))
        return [self._row_to_job(r) for r in rows]

    async def _update(self, job_id: str, **fields: Any) -> Job:
        fields["updated_utc"] = _now()
        if "result" in fields and fields["result"] is not None:
            fields["result"] = json.dumps(fields["result"])
        assignments = ", ".join(f"{k} = ?" for k in fields)
        await self.db.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", [*fields.values(), job_id])
        await self.db.commit()
        job = await self.get(job_id)
        assert job is not None
        self.bus.publish("jobs.update", job)
        return job

    async def restore(self) -> None:
        await self.db.execute(
            "UPDATE jobs SET status = 'failed', error = 'interrupted by restart', updated_utc = ? WHERE status IN ('queued', 'running')",
            (_now(),),
        )
        await self.db.commit()

    # ------------------------------------------------------------- lifecycle
    async def submit(self, kind: str, params: dict[str, Any], fn: JobFn) -> Job:
        job_id = uuid.uuid4().hex[:12]
        await self.db.execute(
            "INSERT INTO jobs (id, kind, status, created_utc, progress, params) VALUES (?, ?, 'queued', ?, 0, ?)",
            (job_id, kind, _now(), json.dumps(params)),
        )
        await self.db.commit()
        job = await self.get(job_id)
        assert job is not None
        self.bus.publish("jobs.update", job)
        self._tasks[job_id] = asyncio.create_task(self._run(job, fn), name=f"job-{kind}-{job_id}")
        return job

    async def _run(self, job: Job, fn: JobFn) -> None:
        async with self._sem:
            job_dir = self.root / job.id
            job_dir.mkdir(parents=True, exist_ok=True)

            async def report(fraction: float, message: str | None) -> None:
                await self._update(job.id, progress=fraction, message=message)

            await self._update(job.id, status="running", message="started")
            try:
                result = await fn(JobContext(job, job_dir, report))
                await self._update(job.id, status="done", progress=1.0, message="finished", result=result)
            except asyncio.CancelledError:
                await self._update(job.id, status="failed", error="cancelled")
                raise
            except Exception as exc:
                log.exception("job %s (%s) failed", job.id, job.kind)
                await self._update(job.id, status="failed", error=f"{type(exc).__name__}: {exc}")
            finally:
                self._tasks.pop(job.id, None)

    async def delete(self, job_id: str) -> None:
        task = self._tasks.pop(job_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        shutil.rmtree(self.root / job_id, ignore_errors=True)
        await self.db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        await self.db.commit()

    def result_path(self, job_id: str, name: str) -> Path:
        base = (self.root / job_id).resolve()
        target = (base / name).resolve()
        if base not in target.parents and target != base:
            raise ValueError("invalid result file name")
        return target

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
```

- [ ] **Step 5: Write `src/mtrtk/web/api/jobs.py`**

```python
"""Background jobs: list, inspect, download results, delete."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from mtrtk.jobs import JobRunner

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _runner(request: Request) -> JobRunner:
    runner = request.app.state.ctx.jobs
    if runner is None:
        raise HTTPException(409, "job runner not available")
    return runner


@router.get("")
async def list_jobs(request: Request, kind: str | None = None, limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    return [j.model_dump(mode="json") for j in await _runner(request).list(kind=kind, limit=limit)]


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request) -> dict[str, Any]:
    job = await _runner(request).get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job.model_dump(mode="json")


@router.delete("/{job_id}")
async def delete_job(job_id: str, request: Request) -> dict[str, bool]:
    runner = _runner(request)
    if await runner.get(job_id) is None:
        raise HTTPException(404, "job not found")
    await runner.delete(job_id)
    return {"ok": True}


@router.get("/{job_id}/files")
async def list_files(job_id: str, request: Request) -> list[dict[str, Any]]:
    runner = _runner(request)
    if await runner.get(job_id) is None:
        raise HTTPException(404, "job not found")
    job_dir = runner.root / job_id
    if not job_dir.exists():
        return []
    return [{"name": p.name, "bytes": p.stat().st_size} for p in sorted(job_dir.iterdir()) if p.is_file()]


@router.get("/{job_id}/files/{name}")
async def download(job_id: str, name: str, request: Request) -> FileResponse:
    runner = _runner(request)
    try:
        path = runner.result_path(job_id, name)
    except ValueError as exc:
        raise HTTPException(404, "file not found") from exc
    if not path.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(path, filename=path.name)
```

- [ ] **Step 6: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_jobs.py tests/unit/test_web_jobs.py tests/unit/test_store.py -q` → all pass; `uv run ruff check . && uv run ruff format . && uv run mypy`.

```bash
git add src/mtrtk/jobs.py src/mtrtk/store/schema/002_jobs_message.sql src/mtrtk/web/api/jobs.py tests/unit/test_jobs.py tests/unit/test_web_jobs.py tests/unit/test_store.py
git commit -m "feat: persistent job runner with progress, result files and jobs API

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: In-process uvicorn, daemon wiring, healthcheck, docs, milestone

**Files:**
- Create: `src/mtrtk/web/server.py`, `tests/unit/test_web_server.py`, `docs/api.md`
- Modify: `src/mtrtk/daemon.py` (web consumer, `JobRunner`, `AppContext`, `web` attribute), `src/mtrtk/cli.py` (`healthcheck`), `docker-compose.yml` (healthcheck command), `docker/Dockerfile` (`EXPOSE 8080 2101`), `README.md`

**Interfaces:**
- Produces: `WebServer(app, host, port, log_level="warning")` with `await serve(stop: asyncio.Event)`, `.port` (bound port after startup), `.started: asyncio.Event`; uvicorn signal capture disabled (the daemon owns SIGINT/SIGTERM). `Daemon.jobs: JobRunner`, `Daemon.web: WebServer | None`, consumer `("web", self._run_web)` for every role. CLI `mtrtk healthcheck` → GET `/healthz` on the resolved bind host (`0.0.0.0` → `127.0.0.1`), exit 0/1.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_web_server.py`:
```python
import asyncio
from pathlib import Path

import httpx
import pytest
from webtest import make_ctx

from mtrtk.config import Settings
from mtrtk.daemon import Daemon
from mtrtk.web.app import create_app
from mtrtk.web.server import WebServer

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_base_30s.ubx"


async def test_webserver_serves_and_stops(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path)
    server = WebServer(create_app(ctx), "127.0.0.1", 0)
    stop = asyncio.Event()
    task = asyncio.create_task(server.serve(stop))
    await asyncio.wait_for(server.started.wait(), 5.0)
    async with httpx.AsyncClient() as c:
        r = await c.get(f"http://127.0.0.1:{server.port}/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    stop.set()
    await asyncio.wait_for(task, 5.0)
    await ctx.db.close()


async def test_daemon_runs_web_and_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    settings = Settings(_env_file=None, role="base", mtrtk_source=f"file:{FIXTURE}", replay_speed=5, data_dir=tmp_path, ntrip_bind="127.0.0.1", ntrip_port=0, web_bind="127.0.0.1", web_port=0, web_allow_insecure=True)
    daemon = Daemon(settings)
    run_task = asyncio.create_task(daemon.run())
    for _ in range(200):
        await asyncio.sleep(0.02)
        if daemon.web is not None and daemon.web.started.is_set():
            break
    assert daemon.web is not None and daemon.jobs is not None
    async with httpx.AsyncClient() as c:
        base = f"http://127.0.0.1:{daemon.web.port}"
        assert (await c.get(f"{base}/api/status")).json()["role"] == "base"
        assert (await c.get(f"{base}/api/jobs")).json() == []
        assert (await c.get(f"{base}/api/ntrip")).json()["running"] is True
    daemon.stop.set()
    await asyncio.wait_for(run_task, 30.0)


def test_healthcheck_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from mtrtk.cli import main

    class Resp:
        status_code = 200

        def json(self) -> dict:
            return {"status": "ok"}

    monkeypatch.setenv("NTRIP_PASSWORD", "")
    monkeypatch.setenv("WEB_BIND", "lan")
    monkeypatch.setenv("WEB_ALLOW_INSECURE", "1")
    calls: list[str] = []
    monkeypatch.setattr("mtrtk.cli.httpx.get", lambda url, timeout: calls.append(url) or Resp())
    r = CliRunner().invoke(main, ["healthcheck"])
    assert r.exit_code == 0 and calls == ["http://127.0.0.1:8080/healthz"]

    def boom(url: str, timeout: float) -> None:
        raise OSError("refused")

    monkeypatch.setattr("mtrtk.cli.httpx.get", boom)
    assert CliRunner().invoke(main, ["healthcheck"]).exit_code == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_web_server.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.web.server'`.

- [ ] **Step 3: Write `src/mtrtk/web/server.py`**

```python
"""Runs uvicorn inside the daemon's event loop without stealing its signal handlers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator

import uvicorn
from fastapi import FastAPI

log = logging.getLogger(__name__)


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:  # the daemon owns SIGINT/SIGTERM
        yield


class WebServer:
    def __init__(self, app: FastAPI, host: str, port: int, log_level: str = "warning") -> None:
        config = uvicorn.Config(app, host=host, port=port, log_level=log_level, access_log=False, lifespan="on", ws="websockets")
        self._server = _Server(config)
        self.started = asyncio.Event()
        self.host = host

    @property
    def port(self) -> int:
        for srv in getattr(self._server, "servers", []):
            for sock in srv.sockets:
                return int(sock.getsockname()[1])
        return int(self._server.config.port)

    async def serve(self, stop: asyncio.Event) -> None:
        serve_task = asyncio.create_task(self._server.serve(), name="uvicorn")
        try:
            while not self._server.started and not serve_task.done():
                await asyncio.sleep(0.01)
            if serve_task.done():
                serve_task.result()  # raises the bind error
            self.started.set()
            log.info("web UI/API listening on http://%s:%d", self.host, self.port)
            await stop.wait()
        finally:
            self._server.should_exit = True
            await asyncio.wait_for(serve_task, 10.0)
```

If the installed uvicorn has no `capture_signals` (older versions use `install_signal_handlers`), override that method with a no-op instead — check with `uv run python -c "import uvicorn, inspect; print([m for m in dir(uvicorn.Server) if 'signal' in m])"`.

- [ ] **Step 4: Wire the web server and job runner into `src/mtrtk/daemon.py`**

Add imports:
```python
from mtrtk.jobs import JobRunner
from mtrtk.web.app import create_app
from mtrtk.web.context import AppContext
from mtrtk.web.server import WebServer
```
In `Daemon.__init__` add `self.jobs: JobRunner | None = None` and `self.web: WebServer | None = None`.
In `_consumers()` append to the common list (before the role-specific block): `("web", self._run_web)`.
Add:
```python
    async def _run_web(self) -> None:
        s = self.settings
        host = await wait_for_bind(s.web_bind, self.stop)
        if host is None:
            return
        ctx = AppContext(settings=s, bus=self.bus, store=self.store, db=self.db, daemon=self, jobs=self.jobs)
        self.web = WebServer(create_app(ctx), host, s.web_port)
        try:
            await self.web.serve(self.stop)
        finally:
            self.web = None
```
In `run()`, right after `await self.db.open()`:
```python
        self.jobs = JobRunner(self.db, self.bus, self.settings.data_dir / "jobs")
        await self.jobs.restore()
```
and in the `finally` before `await self.db.close()`: `if self.jobs: await self.jobs.shutdown()`.

- [ ] **Step 5: Add `mtrtk healthcheck` to `src/mtrtk/cli.py`**

Add `import httpx` at the top and:
```python
@main.command()
def healthcheck() -> None:
    """Exit 0 when the local web API answers /healthz (used by Docker HEALTHCHECK)."""
    from mtrtk.core.exposure import resolve_bind

    settings = _load_settings(ntrip_password="")
    host = resolve_bind(settings.web_bind) or "127.0.0.1"
    if host == "0.0.0.0":
        host = "127.0.0.1"
    url = f"http://{host}:{settings.web_port}/healthz"
    try:
        response = httpx.get(url, timeout=3.0)
    except Exception as exc:
        click.echo(f"unhealthy: {url}: {exc}")
        raise SystemExit(1) from exc
    if response.status_code != 200:
        click.echo(f"unhealthy: {url} -> {response.status_code}")
        raise SystemExit(1)
    click.echo("ok")
```
In `docker-compose.yml` change the healthcheck test to `["CMD", "mtrtk", "healthcheck"]` and `start_period: 30s`. In `docker/Dockerfile` add `EXPOSE 8080 2101` before `ENTRYPOINT`.

- [ ] **Step 6: Run tests, lint**

Run: `uv run pytest -q` → all pass; `uv run ruff check . && uv run ruff format . && uv run mypy`.

- [ ] **Step 7: Manual milestone on replay and live**

```bash
NTRIP_PASSWORD= WEB_BIND=lan WEB_ALLOW_INSECURE=1 uv run mtrtk replay tests/fixtures/f9p_hpg113_base_30s.ubx --loop &
sleep 2
curl -s localhost:8080/healthz; echo
curl -s localhost:8080/api/status | head -c 400; echo
curl -s "localhost:8080/api/history/metrics" | head -c 200; echo
uv run python -c "
import asyncio, websockets, json
async def main():
    async with websockets.connect('ws://localhost:8080/ws?topics=pvt,rtcm') as ws:
        for _ in range(3): print(json.loads(await ws.recv())['type'])
asyncio.run(main())"
kill %1
```
Expected: `{"status":"ok",...}`, a status JSON with `"fix":{"fix_type_name":"3D"...}`, metric lists, and `snapshot`, `epoch`, `epoch`. Then with the receiver: `uv run mtrtk base` and open `http://<tailscale-ip>:8080/api/docs` from another tailnet device — the interactive OpenAPI page lists every endpoint.

- [ ] **Step 8: Write `docs/api.md`**

```markdown
# HTTP API and WebSocket

Base URL: `http://<bind-host>:8080`. Interactive docs: `/api/docs`. Health: `/healthz`.
Auth (only when `WEB_PASSWORD` is set): `POST /api/login {"password"}` sets cookie `mtrtk_session`; or send `Authorization: Bearer <token>`; WebSocket clients may pass `?token=`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | one-screen summary |
| GET | `/api/state` | full receiver state |
| GET | `/api/system` | host stats, tailscale IP, versions |
| GET/PUT | `/api/config` | settings (secrets masked); PUT writes `.env`, returns `restart_required` |
| POST | `/api/restart` | stop the daemon (supervisor restarts it) |
| GET | `/api/receiver` · POST `/api/receiver/reapply` · `/reset {"kind"}` · `/poll {"msg_class","msg_id"}` | receiver actions |
| GET/PUT | `/api/base/mode` | survey-in / fixed / off |
| GET | `/api/base/survey` · POST `/api/base/survey/freeze {"name","activate"}` | survey-in |
| GET/POST | `/api/base/sites` · DELETE `/api/base/sites/{name}` · POST `/api/base/sites/{name}/activate` | fixed sites |
| GET | `/api/ntrip` · `/api/ntrip/clients` · `/api/ntrip/history` | caster |
| GET | `/api/logs` · `/api/logs/availability?from&to` · `/api/logs/window?from&to` · `/api/logs/{name}` | raw logs (PATCH keep, DELETE) |
| GET | `/api/history?metrics=&from=&to=&res=` · `/api/history/metrics` | charts |
| GET | `/api/events` · POST `/api/events/{id}/ack` | event log |
| GET | `/api/jobs` · `/api/jobs/{id}` · `/api/jobs/{id}/files` · `/api/jobs/{id}/files/{name}` · DELETE | background jobs |

WebSocket `/ws?topics=pvt,sats,rtcm,svin,rf,span,ntrip,events,system,receiver,base,jobs,rawlog`:
first `{"type":"snapshot","state":{...}}`, then `{"type":"epoch",...}` per receiver epoch and `{"type":"update","topic":...,"data":...}` messages.
```

- [ ] **Step 9: Commit and tag**

```bash
git add src/mtrtk/web/server.py src/mtrtk/daemon.py src/mtrtk/cli.py docker-compose.yml docker/Dockerfile docs/api.md README.md tests/unit/test_web_server.py
git commit -m "feat(web): in-process uvicorn server wired into the daemon, healthcheck command, API docs

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git tag -a v0.3.0-phase3 -m "Phase 3: web API"
```

Phase 4 (frontend) is the next plan: `docs/superpowers/plans/2026-09-18-phase4-frontend.md`.
