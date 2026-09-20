from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from starlette.websockets import WebSocket
from webtest import client, make_ctx

from mtrtk.web.app import create_app
from mtrtk.web.auth import session_token, token_ok, websocket_authorized

# A latin-1 byte that Starlette decodes into a non-ASCII str: `hmac.compare_digest` rejects those.
NON_ASCII = b"\xe9"


@pytest.fixture
async def ctx(tmp_path: Path):
    c = await make_ctx(tmp_path)
    try:
        yield c
    finally:
        await c.db.close()


@pytest.fixture
async def pw_ctx(tmp_path: Path):
    """A context whose UI is password-protected, so every `/api` route is gated."""
    c = await make_ctx(tmp_path, web_password="hunter2", web_bind="lan")
    try:
        yield c
    finally:
        await c.db.close()


def fake_ws(app: FastAPI, headers: list[tuple[bytes, bytes]], query: bytes = b"") -> WebSocket:
    """A WebSocket carrying just what `websocket_authorized` reads - no handshake needed."""

    async def receive() -> dict[str, str]:
        return {"type": "websocket.connect"}

    async def send(message: Any) -> None:
        return None

    scope = {
        "type": "websocket",
        "path": "/ws",
        "headers": headers,
        "query_string": query,
        "app": app,
    }
    return WebSocket(scope, receive=receive, send=send)


async def test_healthz_is_open(ctx) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "role": "base", "connected": False, "passive": False}


async def test_unknown_api_route_is_json_404(ctx) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.get("/api/nope")
    assert r.status_code == 404 and r.json()["detail"] == "Not Found"


async def test_spa_fallback_and_missing_build(ctx, tmp_path: Path) -> None:
    async with client(create_app(ctx, static_dir=tmp_path / "nostatic")) as c:
        r = await c.get("/")
    assert r.status_code == 503 and "pnpm --dir web build" in r.json()["detail"]
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
        assert (await c.post("/api/logout")).status_code == 200
        assert (await c.get("/api/docs")).status_code == 200
        assert (await c.get("/api/openapi.json")).status_code == 200


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
            assert (await c.get("/api/status")).status_code == 401  # the cookie is gone
        async with client(app) as c2:
            good = await c2.get("/api/status", headers={"Authorization": f"Bearer {token}"})
            assert good.status_code == 200
            bad_header = await c2.get("/api/status", headers={"Authorization": "Bearer wrong"})
            assert bad_header.status_code == 401
    finally:
        await ctx.db.close()


def test_session_token_is_stable_and_password_bound() -> None:
    assert session_token("a") == session_token("a")
    assert session_token("a") != session_token("b")
    assert len(session_token("a")) == 64


async def test_logout_needs_a_session_of_its_own(pw_ctx) -> None:
    async with client(create_app(pw_ctx)) as c:
        assert (await c.post("/api/logout")).status_code == 401


async def test_docs_and_openapi_are_gated_like_any_other_api_route(pw_ctx) -> None:
    app = create_app(pw_ctx)
    async with client(app) as c:
        assert (await c.get("/api/docs")).status_code == 401
        assert (await c.get("/api/openapi.json")).status_code == 401
        assert (await c.post("/api/login", json={"password": "hunter2"})).status_code == 200
        docs = await c.get("/api/docs")
        schema = await c.get("/api/openapi.json")
    assert docs.status_code == 200 and "swagger" in docs.text.lower()
    assert schema.status_code == 200 and "/api/status" in schema.json()["paths"]


async def test_no_ungated_docs_routes_remain(pw_ctx, tmp_path: Path) -> None:
    """FastAPI's own docs routes sit on the app, where a router dependency cannot reach them."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>mtrtk</html>")
    async with client(create_app(pw_ctx, static_dir=static)) as c:
        # No longer routes at all: they fall through to the SPA, which does its own login.
        assert (await c.get("/redoc")).text == "<html>mtrtk</html>"
        assert (await c.get("/docs/oauth2-redirect")).text == "<html>mtrtk</html>"


async def test_non_ascii_credentials_are_refused_not_crashed(pw_ctx) -> None:
    app = create_app(pw_ctx)
    async with client(app) as c:
        bearer = await c.get("/api/status", headers={"Authorization": b"Bearer " + NON_ASCII})
        cookie = await c.get("/api/status", headers={"Cookie": b"mtrtk_session=" + NON_ASCII})
    assert bearer.status_code == 401
    assert cookie.status_code == 401


def test_token_ok_refuses_a_non_ascii_token_without_raising() -> None:
    assert token_ok("hunter2", NON_ASCII.decode("latin-1")) is False
    assert token_ok("hunter2", session_token("hunter2")) is True
    assert token_ok(None, NON_ASCII.decode("latin-1")) is True  # no password: nothing to check


async def test_websocket_authorization_reads_header_cookie_and_query(pw_ctx) -> None:
    app = create_app(pw_ctx)
    token = session_token("hunter2")
    assert websocket_authorized(fake_ws(app, [(b"authorization", b"Bearer " + NON_ASCII)])) is False
    assert websocket_authorized(fake_ws(app, [(b"cookie", b"mtrtk_session=" + NON_ASCII)])) is False
    assert websocket_authorized(fake_ws(app, [])) is False
    assert websocket_authorized(fake_ws(app, [], f"token={token}".encode())) is True
    assert websocket_authorized(fake_ws(app, [(b"authorization", f"Bearer {token}".encode())]))
    assert websocket_authorized(fake_ws(app, [(b"cookie", f"mtrtk_session={token}".encode())]))


async def test_a_route_404_keeps_its_own_detail(ctx) -> None:
    app = create_app(ctx)

    @app.get("/api/throwaway")
    async def throwaway() -> None:
        raise HTTPException(404, "job not found")

    async with client(app) as c:
        found = await c.get("/api/throwaway")
        unknown = await c.get("/api/nope")
    assert found.status_code == 404 and found.json()["detail"] == "job not found"
    assert unknown.status_code == 404 and unknown.json()["detail"] == "Not Found"


async def test_assets_never_fall_back_to_the_spa(ctx, tmp_path: Path) -> None:
    half_built = tmp_path / "half-built"  # index.html shipped, assets/ missing: no mount at all
    half_built.mkdir()
    (half_built / "index.html").write_text("<html>mtrtk</html>")
    async with client(create_app(ctx, static_dir=half_built)) as c:
        assert (await c.get("/")).text == "<html>mtrtk</html>"
        unmounted = await c.get("/assets/app.js")
    assert unmounted.status_code == 404 and unmounted.json()["detail"] == "Not Found"

    built = tmp_path / "built"  # mounted, but this particular asset is not there
    (built / "assets").mkdir(parents=True)
    (built / "index.html").write_text("<html>mtrtk</html>")
    async with client(create_app(ctx, static_dir=built)) as c:
        missing = await c.get("/assets/gone.js")
    assert missing.status_code == 404 and missing.json()["detail"] == "Not Found"


# ------------------------------------------------------------------ request bounds


def env_written(path: Path) -> bool:
    """Whether the settings file exists yet. A helper so the check is not pathlib on the loop."""
    return path.exists()


async def test_an_oversized_api_body_is_refused_before_anything_reads_it(ctx) -> None:
    """A 1 MB `PUT /api/config` used to be parsed, validated and written into `.env`."""
    env = Path(ctx.settings.mtrtk_env_file)
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"marker_name": "x" * (1 << 20)}})
    assert r.status_code == 413
    assert "too large" in r.json()["detail"]
    assert not env_written(env)  # nothing was written


async def test_an_oversized_streamed_api_body_is_cut_off(ctx) -> None:
    """A chunked body declares no length at all: it is counted as it arrives."""

    async def chunks():  # type: ignore[no-untyped-def]
        for _ in range(8):
            yield b"a" * (64 * 1024)

    env = Path(ctx.settings.mtrtk_env_file)
    async with client(create_app(ctx)) as c:
        r = await c.put(
            "/api/config", content=chunks(), headers={"Content-Type": "application/json"}
        )
    assert r.status_code == 413 and "too large" in r.json()["detail"]
    assert not env_written(env)


async def test_a_body_under_the_limit_is_served_normally(ctx) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"marker_name": "x" * 60}})
    assert r.status_code == 200 and r.json()["changed"] == ["marker_name"]


async def test_the_body_limit_leaves_routes_outside_api_alone(ctx, tmp_path: Path) -> None:
    """`/` and the SPA routes carry no body worth bounding, and must not be wrapped."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>mtrtk</html>")
    async with client(create_app(ctx, static_dir=static)) as c:
        r = await c.post("/anywhere", content=b"z" * (512 * 1024))
    # The SPA fallback answers with the index itself, so a body this size must reach it untouched.
    assert r.status_code == 200 and r.text == "<html>mtrtk</html>"


# ------------------------------------------------------------------ the healthcheck route


async def test_healthz_answers_head_with_no_body(ctx) -> None:
    """A monitor that only wants the status code should not have to ask for the whole body."""
    async with client(create_app(ctx)) as c:
        head = await c.head("/healthz")
        root = await c.head("/")
    assert head.status_code == 200 and head.content == b""
    assert root.status_code in (200, 503)  # answered, not 405


async def test_healthz_says_whether_the_receiver_is_passive(ctx, tmp_path: Path) -> None:
    """A replay daemon answers every status route; `connected` alone cannot tell it from a base."""
    async with client(create_app(ctx)) as c:
        live = (await c.get("/healthz")).json()
    assert live == {"status": "ok", "role": "base", "connected": False, "passive": False}
    replay = await make_ctx(tmp_path / "replay", mtrtk_source="file:/tmp/none.ubx")
    try:
        async with client(create_app(replay)) as c:
            assert (await c.get("/healthz")).json()["passive"] is True
    finally:
        await replay.db.close()


# ------------------------------------------------------- paths, and the index-file stat


async def test_a_doubled_slash_is_still_an_api_path(ctx, tmp_path: Path) -> None:
    """`//api/status` reaches the router as a miss; answering it with the SPA would hide it.

    Proxies and hand-written clients produce these, and a JSON API that answers `text/html` to
    one of them is a debugging session nobody needs.
    """
    import httpx

    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>mtrtk</html>")
    async with client(create_app(ctx, static_dir=static)) as c:
        doubled = await c.send(httpx.Request("GET", "http://test//api/status"))
        spa = await c.send(httpx.Request("GET", "http://test//satellites"))
    assert doubled.status_code == 404 and doubled.json()["detail"] == "Not Found"
    assert spa.text == "<html>mtrtk</html>"  # a doubled slash outside /api is still the SPA


def test_is_spa_path_collapses_repeated_slashes() -> None:
    from mtrtk.web.app import is_api_path, is_spa_path

    for path in ("/api/status", "//api/status", "///api//status", "/api", "//api"):
        assert is_spa_path(path) is False, path
        assert is_api_path(path) is True, path
    for path in ("//ws", "//healthz/x", "//assets/app.js"):
        assert is_spa_path(path) is False, path
    for path in ("/satellites", "//satellites", "/apidocs", "//apidocs"):
        assert is_spa_path(path) is True, path
        assert is_api_path(path) is False, path


async def test_the_index_file_is_not_stated_on_every_404(ctx, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A 404 storm must not become one stat per request - but a bundle mounted late must show up.

    The choice: cached at `create_app`, and re-stat'ed at most once every `INDEX_RECHECK_S`.
    """
    from mtrtk.web import app as app_module

    static = tmp_path / "static"
    static.mkdir()
    app = create_app(ctx, static_dir=static)
    async with client(app) as c:
        assert (await c.get("/")).status_code == 503  # no bundle yet, and that answer is cached
        (static / "index.html").write_text("<html>mtrtk</html>")
        assert (await c.get("/")).status_code == 503  # still the cached reading
        monkeypatch.setattr(app_module, "INDEX_RECHECK_S", 0.0)
        assert (await c.get("/")).text == "<html>mtrtk</html>"  # re-stat'ed once the TTL is up


# ------------------------------------------------------------------ the route inventory

# Every path the API serves, as the schema Phase 4 generates its client from sees them. A router
# that moves, or one that stops being imported, is a whole panel of the UI 404ing silently - so
# the inventory is pinned here rather than left to whichever test happens to call which route.
API_PATHS = [
    "/api/base/mode",
    "/api/base/sites",
    "/api/base/sites/{name}",
    "/api/base/sites/{name}/activate",
    "/api/base/survey",
    "/api/base/survey/freeze",
    "/api/base/survey/restart",
    "/api/config",
    "/api/events",
    "/api/events/{event_id}/ack",
    "/api/history",
    "/api/history/metrics",
    "/api/jobs",
    "/api/jobs/{job_id}",
    "/api/jobs/{job_id}/files",
    "/api/jobs/{job_id}/files/{name}",
    "/api/login",
    "/api/logout",
    "/api/logs",
    "/api/logs/availability",
    "/api/logs/window",
    "/api/logs/{name}",
    "/api/ntrip",
    "/api/ntrip/clients",
    "/api/ntrip/history",
    "/api/receiver",
    "/api/receiver/poll",
    "/api/receiver/reapply",
    "/api/receiver/reset",
    "/api/restart",
    "/api/state",
    "/api/status",
    "/api/system",
    "/healthz",
]


async def test_the_whole_route_inventory_is_mounted(ctx) -> None:
    from mtrtk.web.app import API_MODULES

    async with client(create_app(ctx)) as c:
        schema = (await c.get("/api/openapi.json")).json()
    assert sorted(schema["paths"]) == API_PATHS
    assert len(API_PATHS) == 34
    assert len(API_MODULES) == 10


def test_a_router_that_will_not_import_is_not_silently_dropped(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Every module in `API_MODULES` exists now, so an import error is a bug, not a phase gap."""
    from mtrtk.web import app as app_module

    monkeypatch.setattr(app_module, "API_MODULES", ("status", "does_not_exist"))
    with pytest.raises(ModuleNotFoundError):
        app_module._include_api_routers(FastAPI())
