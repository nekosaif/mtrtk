"""DNS rebinding and cross-site WebSockets against a UI without a password (security-2)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from webtest import client, make_ctx

from mtrtk.config import Settings
from mtrtk.web import hostguard
from mtrtk.web.app import create_app
from mtrtk.web.auth import session_token


@pytest.fixture(autouse=True)
def no_tailscale(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here asks the real tailscaled; this host's own name is a fixed one."""
    monkeypatch.setattr(
        hostguard, "tailscale_dns_names", lambda: frozenset({"station.tail1.ts.net", "station"})
    )
    monkeypatch.setattr(hostguard.socket, "gethostname", lambda: "jetson")


@pytest.mark.parametrize(
    ("host", "name"),
    [
        ("localhost:8080", "localhost"),
        ("[::1]:8080", "::1"),
        ("::1", "::1"),
        ("Example.COM.:443", "example.com"),
        ("100.64.0.5", "100.64.0.5"),
        ("", ""),
    ],
)
def test_host_name(host: str, name: str) -> None:
    assert hostguard.host_name(host) == name


async def test_a_rebound_name_is_refused_without_a_password(tmp_path: Path) -> None:
    """Found by review: `Host: attacker.example` got the full config from a loopback daemon."""
    app = create_app(await make_ctx(tmp_path, web_bind="127.0.0.1", web_allow_insecure=True))
    async with client(app) as c:
        refused = await c.get("/api/config", headers={"host": "attacker.example"})
        assert refused.status_code == 400
        assert "WEB_ALLOWED_HOSTS" in refused.json()["detail"]
        assert "values" not in refused.text
        assert (await c.get("/healthz", headers={"host": "attacker.example"})).status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "localhost:8080",
        "127.0.0.1:8080",
        "[::1]:8080",
        "100.101.1.2:8080",  # any IP literal: rebinding needs a name
        "192.168.1.20",
        "jetson:8080",  # this host's own name, short and .local
        "jetson.local",
        "station.tail1.ts.net",  # MagicDNS
        "station:8080",
        "rtk.example.com",  # PUBLIC_DOMAIN
        "ui.lab.test",  # WEB_ALLOWED_HOSTS
        "a.corp.test",  # WEB_ALLOWED_HOSTS' .corp.test
        "app.localhost",
    ],
)
async def test_names_the_daemon_was_given_are_served(tmp_path: Path, host: str) -> None:
    ctx = await make_ctx(
        tmp_path,
        web_bind="lan",
        web_allow_insecure=True,
        public_domain="rtk.example.com",
        web_allowed_hosts="ui.lab.test,.corp.test",
    )
    async with client(create_app(ctx)) as c:
        assert (await c.get("/api/config", headers={"host": host})).status_code == 200


async def test_a_wildcard_turns_the_guard_off(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path, web_bind="lan", web_allow_insecure=True, web_allowed_hosts="*")
    async with client(create_app(ctx)) as c:
        assert (await c.get("/api/config", headers={"host": "anything.test"})).status_code == 200


async def test_with_a_password_the_cookie_does_the_job(tmp_path: Path) -> None:
    """A rebound name carries no session cookie: the password stands, the guard steps aside."""
    ctx = await make_ctx(tmp_path, web_bind="lan", web_password="pw")
    async with client(create_app(ctx)) as c:
        anon = await c.get("/api/config", headers={"host": "attacker.example"})
        assert anon.status_code == 401
        token = {"authorization": f"Bearer {session_token('pw')}", "host": "rtk.other.test"}
        assert (await c.get("/api/config", headers=token)).status_code == 200


def test_allowed_hosts_setting_is_normalised() -> None:
    settings = Settings(
        _env_file=None, ntrip_password="x", web_allowed_hosts=" RTK.Example.com. , ,lab"
    )
    assert settings.web_allowed_hosts == ["rtk.example.com", "lab"]


# TestClient addresses a WebSocket to `testserver` unless the URL names the host.
WS = "ws://localhost:8080/ws"


def settle(ws, app) -> None:  # type: ignore[no-untyped-def]
    """As in test_web_ws: close, and wait for the hub to let go before the session exits."""
    ws.close()
    deadline = time.monotonic() + 2.0
    while app.state.ws_hub.client_count and time.monotonic() < deadline:
        time.sleep(0.005)
    for _ in range(3):
        ws.portal.call(asyncio.sleep, 0)


def _insecure_app(tmp_path: Path):  # type: ignore[no-untyped-def]
    ctx = asyncio.run(
        make_ctx(
            tmp_path, web_bind="127.0.0.1", web_allow_insecure=True, public_domain="rtk.example.com"
        )
    )
    return create_app(ctx)


def test_a_cross_site_websocket_is_refused_without_a_password(tmp_path: Path) -> None:
    """Found by review: a WebSocket from `Origin: http://evil.example` got the live snapshot."""
    with TestClient(_insecure_app(tmp_path), base_url="http://localhost:8080") as c:
        for origin in ("http://evil.example", "null", "http://127.0.0.2:8080"):
            with (
                pytest.raises(WebSocketDisconnect) as refused,
                c.websocket_connect(WS, headers={"origin": origin}) as ws,
            ):
                ws.receive_json()
            assert refused.value.code == hostguard.POLICY_VIOLATION
        with (
            pytest.raises(WebSocketDisconnect),
            c.websocket_connect("ws://attacker.example/ws") as ws,
        ):
            ws.receive_json()


@pytest.mark.parametrize(
    "headers",
    [
        {},  # a script, the ROS bridge: no Origin
        {"origin": "http://localhost:8080"},  # the UI itself
        {"origin": "http://localhost:5173", "host": "localhost:5173"},  # `pnpm dev`'s proxy
        {"origin": "https://rtk.example.com"},  # a proxy that rewrote Host to localhost
    ],
)
def test_same_origin_websockets_are_served(tmp_path: Path, headers: dict[str, str]) -> None:
    app = _insecure_app(tmp_path)
    with (
        TestClient(app, base_url="http://localhost:8080") as c,
        c.websocket_connect(WS, headers=headers) as ws,
    ):
        assert ws.receive_json()["type"] == "snapshot"
        settle(ws, app)
