"""The in-process uvicorn server, the daemon wiring around it and `mtrtk healthcheck`."""

import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest
import websockets
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


async def test_a_taken_port_is_an_oserror_not_a_dead_event_loop(tmp_path: Path) -> None:
    """uvicorn's own bind calls `sys.exit(1)`, which from inside a task kills the whole loop.

    The daemon supervises this consumer: a web port somebody else already owns has to be a
    failure it can back off from and retry, not the end of the receiver and the caster too.
    """
    blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    taken = int(blocker.sockets[0].getsockname()[1])
    ctx = await make_ctx(tmp_path)
    try:
        server = WebServer(create_app(ctx), "127.0.0.1", taken)
        with pytest.raises(OSError):
            await asyncio.wait_for(server.serve(asyncio.Event()), 10.0)
        assert not server.started.is_set()
    finally:
        blocker.close()
        await blocker.wait_closed()
        await ctx.db.close()


async def test_uvicorn_hangs_up_on_websockets_before_the_lifespan_shuts_down(
    tmp_path: Path,
) -> None:
    """The hub's own `_hang_up` is the safety net; this pins which side actually does it."""
    ctx = await make_ctx(tmp_path)
    app = create_app(ctx)
    server = WebServer(app, "127.0.0.1", 0)
    stop = asyncio.Event()
    task = asyncio.create_task(server.serve(stop))
    await asyncio.wait_for(server.started.wait(), 5.0)
    hub = app.state.ws_hub
    still_connected: list[int] = []
    hang_up = hub._hang_up

    async def spy() -> None:
        still_connected.append(hub.client_count)
        await hang_up()

    hub._hang_up = spy
    async with websockets.connect(f"ws://127.0.0.1:{server.port}/ws?topics=pvt") as ws:
        assert json.loads(await asyncio.wait_for(ws.recv(), 5.0))["type"] == "snapshot"
        stop.set()
        await asyncio.wait_for(task, 10.0)
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await asyncio.wait_for(ws.recv(), 5.0)
    assert still_connected == [0]  # uvicorn closed the socket before the lifespan shutdown ran
    await ctx.db.close()


async def test_daemon_runs_web_and_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    settings = Settings(
        _env_file=None,
        role="base",
        mtrtk_source=f"file:{FIXTURE}",
        replay_speed=5,
        data_dir=tmp_path,
        ntrip_bind="127.0.0.1",
        ntrip_port=0,
        web_bind="127.0.0.1",
        web_port=0,
        web_allow_insecure=True,
    )
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


async def test_the_web_layer_sees_the_daemon_and_its_job_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One `AppContext` per process, holding the live daemon - not a copy of its settings."""
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    settings = Settings(
        _env_file=None,
        role="base",
        mtrtk_source=f"file:{FIXTURE}",
        replay_speed=5,
        data_dir=tmp_path,
        ntrip_bind="127.0.0.1",
        ntrip_port=0,
        web_bind="127.0.0.1",
        web_port=0,
        web_allow_insecure=True,
    )
    daemon = Daemon(settings)
    run_task = asyncio.create_task(daemon.run())
    for _ in range(200):
        await asyncio.sleep(0.02)
        if daemon.web is not None and daemon.web.started.is_set():
            break
    ctx = daemon._app_context()
    assert ctx.daemon is daemon and ctx.jobs is daemon.jobs and ctx.db is daemon.db
    assert daemon._app_context() is ctx  # a web restart must not reset `uptime_s`
    jobs = daemon.jobs
    assert jobs is not None
    daemon.stop.set()
    await asyncio.wait_for(run_task, 30.0)
    # `shutdown()` ran before `db.close()`, so the runner refuses work rather than reaching a
    # closed database.
    with pytest.raises(RuntimeError):
        await jobs.submit("export", {}, lambda c: asyncio.sleep(0))  # type: ignore[arg-type]


def test_healthcheck_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from mtrtk.cli import main

    class Resp:
        status_code = 200

        def json(self) -> dict:  # type: ignore[type-arg]
            return {"status": "ok"}

    monkeypatch.setenv("NTRIP_PASSWORD", "")
    monkeypatch.setenv("WEB_BIND", "lan")
    monkeypatch.setenv("WEB_PORT", "8080")  # conftest pins 0; the default port is the point here
    monkeypatch.setenv("WEB_ALLOW_INSECURE", "1")
    calls: list[str] = []
    monkeypatch.setattr("mtrtk.cli.httpx.get", lambda url, timeout: calls.append(url) or Resp())
    r = CliRunner().invoke(main, ["healthcheck"])
    assert r.exit_code == 0 and calls == ["http://127.0.0.1:8080/healthz"]

    def boom(url: str, timeout: float) -> None:
        raise OSError("refused")

    monkeypatch.setattr("mtrtk.cli.httpx.get", boom)
    assert CliRunner().invoke(main, ["healthcheck"]).exit_code == 1


def test_healthcheck_reports_a_body_that_is_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from mtrtk.cli import main

    class Resp:
        status_code = 200

        def json(self) -> dict:  # type: ignore[type-arg]
            return {"status": "starting"}

    monkeypatch.setenv("NTRIP_PASSWORD", "")
    monkeypatch.setattr("mtrtk.cli.httpx.get", lambda url, timeout: Resp())
    result = CliRunner().invoke(main, ["healthcheck"])
    assert result.exit_code == 1 and "unhealthy" in result.output


def test_healthcheck_without_a_tailscale_address_is_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tailscale` never falls back to loopback: nothing is listening there to ask."""
    from click.testing import CliRunner

    from mtrtk.cli import main

    monkeypatch.setenv("NTRIP_PASSWORD", "")
    monkeypatch.setenv("WEB_BIND", "tailscale")
    monkeypatch.setattr("mtrtk.core.exposure.tailscale_ipv4", lambda: None)

    def never(url: str, timeout: float) -> None:
        raise AssertionError("no address was resolved; nothing should have been requested")

    monkeypatch.setattr("mtrtk.cli.httpx.get", never)
    result = CliRunner().invoke(main, ["healthcheck"])
    assert result.exit_code == 1 and "tailscale" in result.output


def test_auto_source_resolves_the_port_on_every_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hardware reset drops the USB device and brings it back, sometimes as another node."""
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    found = iter(["/dev/ttyACM0", "/dev/ttyACM0", "/dev/ttyACM1", None])
    monkeypatch.setattr("mtrtk.daemon.find_ublox_port", lambda: next(found))
    daemon = Daemon(Settings(_env_file=None, role="base", mtrtk_source="auto"))
    factory = daemon.controller._source_factory
    assert factory().port == "/dev/ttyACM0"
    assert factory().port == "/dev/ttyACM1"  # re-enumerated: the new node, not the stale one
    assert factory().port == "/dev/ttyACM1"  # gone this instant: retry the last one we saw


# ----------------------------------------------------------- the final fix wave (groups B, C)


async def test_an_unauthenticated_websocket_is_an_http_403_on_the_wire(tmp_path: Path) -> None:
    """`ws.close()` before `accept()` never reaches the wire: uvicorn fails the handshake.

    This is the whole reason docs/api.md lists a 403 rather than close code 1008 for auth - a
    client keys "log in again" on the handshake status, not on a code it can never see.
    """
    from mtrtk.web.auth import session_token

    ctx = await make_ctx(tmp_path, web_password="pw", web_bind="lan")
    server = WebServer(create_app(ctx), "127.0.0.1", 0)
    stop = asyncio.Event()
    task = asyncio.create_task(server.serve(stop))
    try:
        await asyncio.wait_for(server.started.wait(), 5.0)
        url = f"ws://127.0.0.1:{server.port}/ws"
        with pytest.raises(websockets.exceptions.InvalidStatus) as refused:
            await asyncio.wait_for(websockets.connect(url), 5.0)
        assert refused.value.response.status_code == 403
        async with websockets.connect(f"{url}?token={session_token('pw')}") as ws:
            assert json.loads(await asyncio.wait_for(ws.recv(), 5.0))["type"] == "snapshot"
    finally:
        stop.set()
        await asyncio.wait_for(task, 10.0)
        await ctx.db.close()


async def test_a_uvicorn_that_will_not_stop_still_releases_the_bus_subscribers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The lifespan owns three bus subscriptions; a uvicorn we gave up waiting for never runs it."""
    ctx = await make_ctx(tmp_path)
    app = create_app(ctx)
    before = ctx.bus.subscriber_count
    server = WebServer(app, "127.0.0.1", 0)
    monkeypatch.setattr("mtrtk.web.server.SHUTDOWN_TIMEOUT_S", 0.05)
    lifespan = app.router.lifespan_context(app)

    async def wedged(sockets: object = None) -> None:
        """Comes up, subscribes, and then stops answering `should_exit` - no shutdown, ever."""
        await lifespan.__aenter__()
        server._server.started = True
        await asyncio.Event().wait()

    monkeypatch.setattr(server._server, "serve", wedged)
    stop = asyncio.Event()
    task = asyncio.create_task(server.serve(stop))
    try:
        await asyncio.wait_for(server.started.wait(), 5.0)
        assert ctx.bus.subscriber_count == before + 3
        stop.set()
        with caplog.at_level(logging.WARNING, logger="mtrtk.web.server"):
            await asyncio.wait_for(task, 5.0)
        assert ctx.bus.subscriber_count == before  # released, not leaked
        assert getattr(app.state, "ws_hub", None) is None
        assert getattr(app.state, "system_cache", None) is None
        assert getattr(app.state, "log_index", None) is None
        assert "websocket client" in caplog.text
    finally:
        # Unwind the lifespan the stub abandoned, so no suspended generator survives the test.
        # Everything it holds is closed already, and both `aclose`s are idempotent.
        await lifespan.__aexit__(None, None, None)
        await ctx.db.close()
