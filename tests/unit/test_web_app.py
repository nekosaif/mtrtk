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
