"""WebSocket hub: snapshot, epoch bundles, topic filtering, throttling and lifecycle."""

import asyncio
import logging
import time
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from webtest import load_fixture_into, make_ctx

from mtrtk.base.ntrip_caster import ClientInfo
from mtrtk.config import BaseMode
from mtrtk.core.receiver import Capabilities
from mtrtk.core.state import RfBlock, Spectrum
from mtrtk.store.models import Event, SystemStats
from mtrtk.web.app import create_app
from mtrtk.web.ws import TOPICS, WsHub, epoch_message, snapshot_message


class FakeSocket:
    def __init__(self, hold_open: bool = True) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self.close_code: int | None = None
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
        self.close_code = code
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


def settle(ws, hub: WsHub, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    """Close from the client side and wait for the server to finish with the socket.

    `WebSocketTestSession.__exit__` sends the disconnect and then cancels the task running the
    app, re-raising that cancellation at the end of the `with` block. A streaming endpoint is
    still tearing down at that moment, so leaving it to the exit stack is a race the test loses
    roughly one run in ten. Waiting for the hub to let go of the socket makes it deterministic.
    """
    ws.close()
    deadline = time.monotonic() + timeout
    while hub.client_count and time.monotonic() < deadline:
        time.sleep(0.005)
    assert hub.client_count == 0
    for _ in range(3):  # drain the portal loop so the endpoint coroutine has returned too
        ws.portal.call(asyncio.sleep, 0)


def spectrum(block_id: int = 0) -> Spectrum:
    return Spectrum(block_id=block_id, span_hz=1, res_hz=1, center_hz=1, pga_db=0, bins=[0] * 256)


def capabilities() -> Capabilities:
    return Capabilities(
        protver="27.12",
        fw_version="HPG 1.13",
        module="ZED-F9P",
        supported={"NAV-PVT", "MON-SPAN"},
        unsupported={"RXM-RAWX"},
    )


def event() -> Event:
    return Event(
        ts_utc=datetime(2026, 9, 18, 21, 55, tzinfo=UTC),
        level="warning",
        kind="rtcm_stalled",
        message="no RTCM for 10 s",
    )


def client_info(**overrides) -> ClientInfo:  # type: ignore[no-untyped-def]
    fields = {
        "id": 1,
        "ip": "100.64.0.9",
        "port": 51000,
        "mountpoint": "MTRTK",
        "user_agent": "NTRIP test/1",
        "username": "rover",
        "version": 2,
        "connected_utc": datetime(2026, 9, 18, 21, 55, tzinfo=UTC),
        "bytes_sent": 10,
    }
    return ClientInfo(**{**fields, **overrides})


def test_snapshot_and_epoch_shapes(ctx) -> None:
    snap = snapshot_message(ctx, set(TOPICS))
    assert snap["type"] == "snapshot" and snap["role"] == "base"
    assert snap["state"]["fix"]["fix_type"] == 3
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
    ctx.bus.publish("state.spectrum", [spectrum()])  # not subscribed
    ctx.bus.publish(
        "system.stats",
        SystemStats(cpu_pct=1, mem_pct=2, disk_free_gb=3, disk_used_pct=4, uptime_s=5),
    )
    await asyncio.sleep(0.02)
    types = [(m["type"], m.get("topic")) for m in sock.sent[1:]]
    assert types == [("epoch", None), ("update", "rf"), ("update", "system")]
    assert "sats" not in sock.sent[1]
    assert sock.sent[2]["data"][0]["jam_ind"] == 5
    sock.disconnect()
    await asyncio.wait_for(task, 1.0)
    await hub.aclose()


async def test_span_is_throttled_to_one_per_second(ctx) -> None:
    hub = WsHub(ctx)
    sock = FakeSocket()
    task = asyncio.create_task(hub.serve(sock, {"span"}))
    await asyncio.sleep(0.01)
    bands = [spectrum()]
    for _ in range(5):
        ctx.bus.publish("state.spectrum", bands)
    await asyncio.sleep(0.02)
    assert sum(1 for m in sock.sent if m.get("topic") == "span") == 1
    sock.disconnect()
    await asyncio.wait_for(task, 1.0)
    await hub.aclose()


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
    await hub.aclose()


def test_websocket_route_snapshot_via_testclient(ctx) -> None:
    app = create_app(ctx)
    with TestClient(app) as client, client.websocket_connect("/ws?topics=pvt,rtcm") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot" and sorted(first["topics"]) == ["pvt", "rtcm"]
        settle(ws, app.state.ws_hub)


def test_websocket_requires_token_when_password_set(tmp_path: Path) -> None:
    import asyncio as aio

    ctx = aio.run(make_ctx(tmp_path, web_password="pw", web_bind="lan"))
    app = create_app(ctx)
    from mtrtk.web.auth import session_token

    with TestClient(app) as client:
        # A policy-violation close before accept: the handshake fails, so entering the session
        # raises rather than yielding a socket that then goes quiet.
        with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect("/ws") as ws:
            ws.receive_json()
        assert refused.value.code == 1008
        with client.websocket_connect(f"/ws?token={session_token('pw')}") as ws:
            assert ws.receive_json()["type"] == "snapshot"
            settle(ws, app.state.ws_hub)
    aio.run(ctx.db.close())


# --------------------------------------------------------------- ruling-driven tests


async def test_one_subscription_per_process_not_per_socket(ctx) -> None:
    """Ruling 1: the hub owns a single bus subscription; sockets add none and leak none."""
    before = ctx.bus.subscriber_count
    hub = WsHub(ctx)
    assert ctx.bus.subscriber_count == before + 1
    socks = [FakeSocket(), FakeSocket()]
    tasks = [asyncio.create_task(hub.serve(s, {"pvt"})) for s in socks]
    await asyncio.sleep(0.01)
    assert ctx.bus.subscriber_count == before + 1  # two clients, still one subscription
    assert hub.client_count == 2
    socks[0].disconnect()
    await asyncio.wait_for(tasks[0], 1.0)
    assert hub.client_count == 1
    assert ctx.bus.subscriber_count == before + 1  # a disconnect releases nothing shared
    socks[1].disconnect()
    await asyncio.wait_for(tasks[1], 1.0)
    await hub.aclose()
    assert ctx.bus.subscriber_count == before
    assert hub.client_count == 0
    ctx.bus.publish("state.epoch", ctx.store.state)  # nothing left to receive it


def test_the_app_lifespan_owns_exactly_one_hub(ctx) -> None:
    """Ruling 1: built once at startup, released at shutdown, shared by every socket."""
    app = create_app(ctx)
    before = ctx.bus.subscriber_count
    assert getattr(app.state, "ws_hub", None) is None
    with TestClient(app) as client:
        hub = app.state.ws_hub
        assert isinstance(hub, WsHub)
        during = ctx.bus.subscriber_count
        assert during == before + 3  # the system cache, the hub and the log index mirror
        for _ in range(3):
            with client.websocket_connect("/ws") as ws:
                assert ws.receive_json()["type"] == "snapshot"
                assert ctx.bus.subscriber_count == during
                assert hub.client_count == 1
                settle(ws, hub)
        assert ctx.bus.subscriber_count == during
    assert getattr(app.state, "ws_hub", None) is None
    assert ctx.bus.subscriber_count == before


async def test_live_payloads_are_serialised_before_any_await(ctx) -> None:
    """Ruling 2: the caster's live ClientInfo list is JSON by the time the handler yields."""
    hub = WsHub(ctx)
    sock = FakeSocket()
    gate = asyncio.Event()
    record = sock.send_json

    async def gated(obj: dict) -> None:
        await gate.wait()
        await record(obj)

    sock.send_json = gated  # type: ignore[method-assign]
    task = asyncio.create_task(hub.serve(sock, {"ntrip"}))
    await asyncio.sleep(0.01)
    info = client_info()
    live = [info]
    ctx.bus.publish("ntrip.clients", live)
    await asyncio.sleep(0.01)  # the hub handler has run; the sender is still blocked
    live.clear()  # the caster drops the client...
    info.bytes_sent = 999  # ...and keeps mutating the object it handed us
    gate.set()
    await asyncio.sleep(0.01)
    payload = [m for m in sock.sent if m.get("topic") == "ntrip"][0]["data"]
    assert payload == [client_info().public()]
    assert payload[0]["bytes_sent"] == 10 and payload[0]["connected_utc"].endswith("+00:00")
    sock.disconnect()
    await asyncio.wait_for(task, 1.0)
    await hub.aclose()


async def test_every_documented_topic_reaches_its_subscriber(ctx) -> None:
    hub = WsHub(ctx)
    sock = FakeSocket()
    task = asyncio.create_task(hub.serve(sock, set(TOPICS)))
    await asyncio.sleep(0.01)
    published = [
        ("state.hardware", ctx.store.state.hardware, "rf"),
        ("state.rf", [RfBlock(block_id=0)], "rf"),
        ("ntrip.clients", [client_info()], "ntrip"),
        ("events.new", event(), "events"),
        ("receiver.connected", "/dev/ttyACM0", "receiver"),
        ("receiver.disconnected", "eof", "receiver"),
        ("receiver.capabilities", capabilities(), "receiver"),
        ("base.mode", {"mode": BaseMode.SURVEY_IN, "site": "roof", "reason": "startup"}, "base"),
        ("base.site_mismatch", {"site": "roof", "reason": "moved"}, "base"),
        ("rawlog.rotated", Path("/tmp/a.ubx"), "rawlog"),
        ("rawlog.backpressure", {"queued": 3}, "rawlog"),
        ("jobs.update", {"id": 1, "state": "running"}, "jobs"),
    ]
    for bus_topic, item, _ in published:
        ctx.bus.publish(bus_topic, item)
    ctx.bus.publish("sampler.error", "not a websocket topic")
    await asyncio.sleep(0.03)
    got = [(m["source"], m["topic"]) for m in sock.sent if m["type"] == "update"]
    assert got == [(bus_topic, topic) for bus_topic, _, topic in published]
    assert all(isinstance(m["data"], (dict, list, str)) for m in sock.sent if m["type"] == "update")
    by_source = {m["source"]: m["data"] for m in sock.sent if m["type"] == "update"}
    # A dataclass carrying `set[str]`: sorted lists, never a set's repr.
    assert by_source["receiver.capabilities"]["supported"] == ["MON-SPAN", "NAV-PVT"]
    assert by_source["receiver.capabilities"]["unsupported"] == ["RXM-RAWX"]
    # A pydantic model with a datetime: ISO-8601, UTC.
    assert by_source["events.new"]["ts_utc"] == "2026-09-18T21:55:00Z"
    assert by_source["events.new"]["level"] == "warning" and by_source["events.new"]["meta"] == {}
    # A StrEnum is a `str`, so only the branch order keeps a live member off the wire.
    mode = by_source["base.mode"]["mode"]
    assert mode == "survey-in" and type(mode) is str and not isinstance(mode, Enum)
    sock.disconnect()
    await asyncio.wait_for(task, 1.0)
    await hub.aclose()


async def test_one_slow_client_never_stalls_the_others(ctx) -> None:
    """The fan-out is synchronous: a blocked socket cannot hold up the bus or its neighbours."""
    hub = WsHub(ctx)
    quick, stuck = FakeSocket(), FakeSocket()

    async def never(obj: dict) -> None:
        await asyncio.Event().wait()

    stuck.send_json = never  # type: ignore[method-assign]
    tasks = [
        asyncio.create_task(hub.serve(quick, {"pvt"})),
        asyncio.create_task(hub.serve(stuck, {"pvt"})),
    ]
    await asyncio.sleep(0.01)
    for _ in range(60):
        ctx.bus.publish("state.epoch", ctx.store.state)
    await asyncio.sleep(0.05)
    assert stuck.closed is True and stuck.close_code == 1008
    assert len(quick.sent) == 61  # snapshot plus every epoch
    assert hub.client_count == 1
    quick.disconnect()
    await asyncio.gather(*tasks, return_exceptions=True)
    await hub.aclose()


async def test_unknown_topics_are_dropped_and_an_empty_request_means_everything(ctx) -> None:
    hub = WsHub(ctx)
    picky, silent = FakeSocket(), FakeSocket()
    tasks = [
        asyncio.create_task(hub.serve(picky, {"pvt", "bogus"})),
        asyncio.create_task(hub.serve(silent, set())),
    ]
    await asyncio.sleep(0.01)
    assert picky.sent[0]["topics"] == ["pvt"]
    assert silent.sent[0]["topics"] == sorted(TOPICS)
    picky.disconnect()
    silent.disconnect()
    await asyncio.gather(*tasks)
    await hub.aclose()


async def test_aclose_hangs_up_the_sockets_it_is_still_serving(ctx) -> None:
    """Shutdown is not allowed to leave a socket open with nobody feeding it."""
    before = ctx.bus.subscriber_count
    hub = WsHub(ctx)
    socks = [FakeSocket(), FakeSocket()]
    tasks = [asyncio.create_task(hub.serve(s, {"pvt"})) for s in socks]
    await asyncio.sleep(0.01)
    assert hub.client_count == 2
    await hub.aclose()
    assert [(s.closed, s.close_code) for s in socks] == [(True, 1001), (True, 1001)]
    assert hub.client_count == 0
    assert ctx.bus.subscriber_count == before
    await asyncio.wait_for(asyncio.gather(*tasks), 1.0)


async def test_a_binary_frame_does_not_kill_the_connection(ctx) -> None:
    """Starlette's `receive_text` raises KeyError on a binary frame; ignore it and read on."""
    hub = WsHub(ctx)
    sock = FakeSocket()
    hold = sock.receive_text
    frames = {"n": 0}

    async def receive_text() -> str:
        frames["n"] += 1
        if frames["n"] == 1:
            raise KeyError("text")  # what Starlette does with `websocket.receive` {"bytes": ...}
        return await hold()

    sock.receive_text = receive_text  # type: ignore[method-assign]
    task = asyncio.create_task(hub.serve(sock, {"pvt"}))
    await asyncio.sleep(0.01)
    ctx.bus.publish("state.epoch", ctx.store.state)
    await asyncio.sleep(0.02)
    assert frames["n"] >= 2  # it went back for the next frame instead of tearing down
    assert hub.client_count == 1
    assert [m["type"] for m in sock.sent] == ["snapshot", "epoch"]
    sock.disconnect()
    await asyncio.wait_for(task, 1.0)
    await hub.aclose()


async def test_span_flows_again_once_the_interval_has_elapsed(ctx) -> None:
    now = [1_000.0]
    hub = WsHub(ctx, clock=lambda: now[0])
    sock = FakeSocket()
    task = asyncio.create_task(hub.serve(sock, {"span"}))
    await asyncio.sleep(0.01)
    for _ in range(3):
        ctx.bus.publish("state.spectrum", [spectrum()])
    await asyncio.sleep(0.02)
    assert sum(1 for m in sock.sent if m.get("topic") == "span") == 1
    now[0] += 0.9  # still inside the window
    ctx.bus.publish("state.spectrum", [spectrum()])
    await asyncio.sleep(0.02)
    assert sum(1 for m in sock.sent if m.get("topic") == "span") == 1
    now[0] += 0.2  # 1.1 s since the last one
    ctx.bus.publish("state.spectrum", [spectrum()])
    await asyncio.sleep(0.02)
    assert sum(1 for m in sock.sent if m.get("topic") == "span") == 2
    sock.disconnect()
    await asyncio.wait_for(task, 1.0)
    await hub.aclose()


async def test_bus_drops_are_logged_once_and_then_at_most_once_a_minute(ctx, caplog) -> None:
    now = [1_000.0]
    hub = WsHub(ctx, clock=lambda: now[0])
    hub.start()
    stats = SystemStats(cpu_pct=1, mem_pct=2, disk_free_gb=3, disk_used_pct=4, uptime_s=5)
    with caplog.at_level(logging.WARNING, logger="mtrtk.web.ws"):
        for _ in range(400):  # more than the hub's own bus queue holds, in one synchronous burst
            ctx.bus.publish("system.stats", stats)
        await asyncio.sleep(0.05)
        assert sum("dropped" in r.message for r in caplog.records) == 1
        for _ in range(400):
            ctx.bus.publish("system.stats", stats)
        await asyncio.sleep(0.05)
        assert sum("dropped" in r.message for r in caplog.records) == 1  # same minute, one line
        now[0] += 61
        for _ in range(400):
            ctx.bus.publish("system.stats", stats)
        await asyncio.sleep(0.05)
        assert sum("dropped" in r.message for r in caplog.records) == 2
    await hub.aclose()


async def test_lifespan_releases_the_cache_even_if_the_hub_will_not_close(ctx) -> None:
    """Ruling 5: nested teardown - one failing close must not strand the other subscriber."""
    app = create_app(ctx)
    before = ctx.bus.subscriber_count

    async def boom() -> None:
        raise RuntimeError("hub is wedged")

    hub = None
    with pytest.raises(RuntimeError, match="wedged"):
        async with app.router.lifespan_context(app):
            assert ctx.bus.subscriber_count == before + 3
            hub = app.state.ws_hub
            hub.aclose = boom
    # Only the wedged hub's own subscription is left; the cache and the log index mirror on
    # either side of it were released regardless.
    assert ctx.bus.subscriber_count == before + 1
    assert getattr(app.state, "system_cache", None) is None
    assert getattr(app.state, "log_index", None) is None
    del hub.aclose  # drop the stub and release the hub for real
    await hub.aclose()
    assert ctx.bus.subscriber_count == before
