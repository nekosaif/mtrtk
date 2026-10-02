import asyncio
import logging
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from mtrtk.base.ntrip_caster import CasterConfig, NtripCaster
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.core.statestore import StateStore
from mtrtk.rover.drivers.base import DriverCapabilities
from mtrtk.rover.drivers.ublox import UbloxDriver
from mtrtk.rover.ntrip_client import (
    NtripClient,
    NtripClientConfig,
    NtripClientStatus,
    ProtocolError,
    decode_chunked,
)
from ubxtest import rtcm_frame

RTCM_1005 = bytes.fromhex("d300133ed7fd0382dfdc1c403db34fe8fe0cef5e6b30bd2e23")
RTCM_1077 = rtcm_frame(1077, b"\x00" * 40)
GGA = b"$GNGGA,164734.00,2350.24104,N,09015.75301,E,1,12,0.9,13.3,M,-49.6,M,0.0,0*78\r\n"


class RecordingDriver:
    name = "recording"
    capabilities = DriverCapabilities(
        accepts_rtcm=True, raw_gnss_log=True, attitude=False, imu=False, sats=True, spectrum=False
    )

    def __init__(self) -> None:
        self.injected: list[bytes] = []

    async def inject_rtcm(self, data: bytes) -> None:
        self.injected.append(data)


@pytest.fixture
async def caster(tmp_path: Path):
    bus = Bus()
    c = NtripCaster(
        bus,
        CasterConfig(
            mountpoint="MTRK", username="rover", password="pw", station_id="MTRK", country="BGD"
        ),
        host="127.0.0.1",
        port=0,
    )
    await c.start()
    try:
        yield c, bus
    finally:
        await c.stop()


def publish(bus: Bus, raw: bytes) -> None:
    for f in Framer().feed(raw):
        bus.publish("raw.rtcm", f)


async def wait_connected(client: NtripClient) -> None:
    for _ in range(100):
        await asyncio.sleep(0.02)
        if client.status.connected:
            return


def test_config_from_url() -> None:
    cfg = NtripClientConfig.from_url("ntrip://rover:s3cret@100.100.50.10:2101/MTRK")
    assert (cfg.host, cfg.port, cfg.mountpoint, cfg.username, cfg.password) == (
        "100.100.50.10",
        2101,
        "MTRK",
        "rover",
        "s3cret",
    )
    anon = NtripClientConfig.from_url("http://base.tailnet/MTRK")
    assert anon.port == 2101 and anon.username is None
    with pytest.raises(ValueError):
        NtripClientConfig.from_url("ntrip://host:2101/")


def test_config_from_url_decodes_percent_escapes_and_rejects_other_schemes() -> None:
    cfg = NtripClientConfig.from_url("ntrip://ro%40ver:p%3Aw@host:2102/MTRK")
    assert (cfg.username, cfg.password, cfg.port) == ("ro@ver", "p:w", 2102)
    with pytest.raises(ValueError):
        NtripClientConfig.from_url("ftp://host/MTRK")


async def test_client_streams_v2_and_injects_valid_frames(caster) -> None:
    c, bus = caster
    client_bus = Bus()
    statuses = client_bus.subscribe("ntrip_client.status")
    driver = RecordingDriver()
    client = NtripClient(
        NtripClientConfig.from_url(f"ntrip://rover:pw@127.0.0.1:{c.port}/MTRK"),
        client_bus,
        driver,
        gga_provider=lambda: GGA,
        gga_interval_s=0.05,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    await wait_connected(client)
    assert client.status.connected and client.status.version == 2
    publish(bus, RTCM_1005 + RTCM_1077 + b"\xd3\x00\x05junkxx")  # last one has a bad CRC
    await asyncio.sleep(0.1)
    assert driver.injected == [RTCM_1005, RTCM_1077]
    assert client.status.frames_injected == 2
    assert client.status.bytes_received >= len(RTCM_1005) + len(RTCM_1077)
    assert any(ci.last_gga_lat is not None for ci in c.clients.values())  # GGA reached the caster
    assert statuses.queue.qsize() >= 1
    stop.set()
    await asyncio.wait_for(task, 2.0)
    assert client.status.connected is False


async def test_client_falls_back_to_v1_when_server_speaks_icy(caster) -> None:
    c, bus = caster
    driver = RecordingDriver()
    client = NtripClient(
        NtripClientConfig.from_url(f"ntrip://rover:pw@127.0.0.1:{c.port}/MTRK"),
        Bus(),
        driver,
        gga_provider=lambda: None,
    )
    client.force_v1 = True
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    await wait_connected(client)
    assert client.status.version == 1
    publish(bus, RTCM_1077)
    await asyncio.sleep(0.1)
    assert driver.injected == [RTCM_1077]
    stop.set()
    await asyncio.wait_for(task, 2.0)


async def test_wrong_password_backs_off_and_reports(
    caster, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = caster
    from mtrtk.rover import ntrip_client as mod

    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)
        if len(sleeps) >= 2:
            stop.set()

    monkeypatch.setattr(mod, "_sleep", fake_sleep)
    client = NtripClient(
        NtripClientConfig.from_url(f"ntrip://rover:wrong@127.0.0.1:{c.port}/MTRK"),
        Bus(),
        RecordingDriver(),
        gga_provider=lambda: None,
    )
    stop = asyncio.Event()
    await asyncio.wait_for(client.run(stop), 5.0)
    assert client.status.connected is False and "401" in (client.status.last_error or "")
    assert sleeps[0] == 60.0


async def test_missing_mountpoint_is_reported(caster, monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = caster
    from mtrtk.rover import ntrip_client as mod

    async def fake_sleep(d: float) -> None:
        stop.set()

    monkeypatch.setattr(mod, "_sleep", fake_sleep)
    client = NtripClient(
        NtripClientConfig.from_url(f"ntrip://rover:pw@127.0.0.1:{c.port}/NOPE"),
        Bus(),
        RecordingDriver(),
        gga_provider=lambda: None,
    )
    stop = asyncio.Event()
    await asyncio.wait_for(client.run(stop), 5.0)
    assert "mountpoint" in (client.status.last_error or "").lower()


async def test_unreachable_host_backs_off_exponentially(monkeypatch: pytest.MonkeyPatch) -> None:
    from mtrtk.rover import ntrip_client as mod

    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)
        if len(sleeps) >= 4:
            stop.set()

    monkeypatch.setattr(mod, "_sleep", fake_sleep)
    monkeypatch.setattr(mod.random, "uniform", lambda a, b: 1.0)
    with socket.socket() as s:  # a port this test owns and knows is closed, not a guess
        s.bind(("127.0.0.1", 0))
        refused = s.getsockname()[1]
    client = NtripClient(
        NtripClientConfig.from_url(f"ntrip://127.0.0.1:{refused}/MTRK"),
        Bus(),
        RecordingDriver(),
        gga_provider=lambda: None,
    )
    stop = asyncio.Event()
    await asyncio.wait_for(client.run(stop), 5.0)
    assert sleeps[:4] == [1.0, 2.0, 4.0, 8.0] and client.status.reconnects == 4


# ----------------------------------------------------------------- beyond the brief


async def _fake_caster(handler) -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_unexpected_v2_answer_retries_once_as_pure_v1() -> None:
    """Spec: anything other than 200/401/sourcetable gets one immediate retry as pure v1."""
    requests: list[bytes] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        requests.append(head)
        if b"Ntrip-Version" in head:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        writer.write(b"ICY 200 OK\r\n\r\n" + RTCM_1077)
        await writer.drain()
        await reader.read()  # hold the stream open until the client goes away
        writer.close()

    server, port = await _fake_caster(handler)
    driver = RecordingDriver()
    client = NtripClient(
        NtripClientConfig.from_url(f"ntrip://127.0.0.1:{port}/MTRK"),
        Bus(),
        driver,
        gga_provider=lambda: None,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    await wait_connected(client)
    for _ in range(50):
        if driver.injected:
            break
        await asyncio.sleep(0.02)
    assert client.status.version == 1 and driver.injected == [RTCM_1077]
    assert b"Ntrip-Version" in requests[0] and b"HTTP/1.0" in requests[1].split(b"\r\n")[0]
    stop.set()
    await asyncio.wait_for(task, 2.0)
    server.close()
    await server.wait_closed()


async def test_http_200_sourcetable_means_mount_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from mtrtk.rover import ntrip_client as mod

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        body = b"STR;OTHER;;\r\nENDSOURCETABLE\r\n"
        writer.write(
            b"HTTP/1.1 200 OK\r\nNtrip-Version: Ntrip/2.0\r\nContent-Type: gnss/sourcetable\r\n"
            b"Content-Length: %d\r\n\r\n" % len(body) + body
        )
        await writer.drain()
        writer.close()

    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)
        stop.set()

    monkeypatch.setattr(mod, "_sleep", fake_sleep)
    server, port = await _fake_caster(handler)
    client = NtripClient(
        NtripClientConfig.from_url(f"ntrip://127.0.0.1:{port}/MTRK"),
        Bus(),
        RecordingDriver(),
        gga_provider=lambda: None,
    )
    stop = asyncio.Event()
    await asyncio.wait_for(client.run(stop), 5.0)
    assert "mountpoint" in (client.status.last_error or "").lower() and sleeps == [60.0]
    server.close()
    await server.wait_closed()


async def test_stop_interrupts_a_long_backoff(caster) -> None:
    c, _ = caster
    client = NtripClient(
        NtripClientConfig.from_url(f"ntrip://rover:wrong@127.0.0.1:{c.port}/MTRK"),
        Bus(),
        RecordingDriver(),
        gga_provider=lambda: None,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if client.status.next_retry_s:
            break
    assert client.status.next_retry_s == 60.0
    stop.set()
    await asyncio.wait_for(task, 1.0)  # not 60 s


async def test_crc_drops_are_counted_and_published_status_is_a_snapshot() -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        bad = bytearray(RTCM_1077)
        bad[-1] ^= 0xFF
        writer.write(b"ICY 200 OK\r\n\r\n" + bytes(bad) + RTCM_1005)
        await writer.drain()
        await reader.read()
        writer.close()

    server, port = await _fake_caster(handler)
    bus = Bus()
    statuses = bus.subscribe("ntrip_client.status")
    driver = RecordingDriver()
    client = NtripClient(
        NtripClientConfig.from_url(f"ntrip://127.0.0.1:{port}/MTRK"),
        bus,
        driver,
        gga_provider=lambda: None,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if driver.injected:
            break
    assert driver.injected == [RTCM_1005] and client.status.crc_dropped == 1
    _, first = statuses.queue.get_nowait()
    assert first.connected is True and first is not client.status
    stop.set()
    await asyncio.wait_for(task, 2.0)
    server.close()
    await server.wait_closed()


async def test_decode_chunked_yields_chunk_payloads_then_empty() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"3\r\nabc\r\n2;ext=1\r\nde\r\n0\r\n\r\n")
    reader.feed_eof()
    assert await decode_chunked(reader) == b"abc"
    assert await decode_chunked(reader) == b"de"
    assert await decode_chunked(reader) == b""


async def test_decode_chunked_rejects_garbage_size() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"zz\r\nabc\r\n")
    reader.feed_eof()
    with pytest.raises(ProtocolError):  # a ConnectionError, so the client reconnects
        await decode_chunked(reader)


async def test_decode_chunked_refuses_a_huge_chunk_before_buffering_it() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"FFFFFFFF\r\nabc")  # no EOF: an unbounded read would just wait
    with pytest.raises(ProtocolError, match="limit"):
        await asyncio.wait_for(decode_chunked(reader), 1.0)


class FakeLink:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    async def write(self, data: bytes) -> None:
        self.written.append(data)


async def test_ublox_driver_injects_when_connected_and_drops_when_not() -> None:
    link = FakeLink()
    controller = SimpleNamespace(link=link, connected=True)
    store = StateStore()
    driver = UbloxDriver(controller, store)  # type: ignore[arg-type]
    assert driver.capabilities.accepts_rtcm and driver.name == "ublox"
    await driver.inject_rtcm(RTCM_1077)
    assert link.written == [RTCM_1077] and store.state.rtk.last_rtcm_mono is not None
    controller.connected = False
    await driver.inject_rtcm(RTCM_1005)
    assert link.written == [RTCM_1077] and driver.dropped_bytes == len(RTCM_1005)
    controller.link = None
    controller.connected = True
    await driver.inject_rtcm(RTCM_1005)
    assert driver.dropped_bytes == 2 * len(RTCM_1005)


async def test_ublox_driver_drops_when_the_write_fails() -> None:
    class DeadLink:
        async def write(self, data: bytes) -> None:
            raise ConnectionError("serial port not open")

    store = StateStore()
    driver = UbloxDriver(SimpleNamespace(link=DeadLink(), connected=True), store)  # type: ignore[arg-type]
    await driver.inject_rtcm(RTCM_1077)
    assert driver.dropped_bytes == len(RTCM_1077) and store.state.rtk.last_rtcm_mono is None


async def test_a_caster_that_accepts_then_hangs_up_still_backs_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 followed by an instant close must not turn into a 1 s reconnect loop."""
    from mtrtk.rover import ntrip_client as mod

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"ICY 200 OK\r\n\r\n")
        await writer.drain()
        writer.close()

    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)
        if len(sleeps) >= 3:
            stop.set()

    server, port = await _fake_caster(handler)
    monkeypatch.setattr(mod, "_sleep", fake_sleep)
    monkeypatch.setattr(mod.random, "uniform", lambda a, b: 1.0)
    client = NtripClient(
        NtripClientConfig.from_url(f"ntrip://127.0.0.1:{port}/MTRK"),
        Bus(),
        RecordingDriver(),
        gga_provider=lambda: None,
    )
    stop = asyncio.Event()
    await asyncio.wait_for(client.run(stop), 5.0)
    assert sleeps == [1.0, 2.0, 4.0] and "closed" in (client.status.last_error or "")
    server.close()
    await server.wait_closed()


# ----------------------------------------------------------------- fix round 1


def drain(sub) -> list[NtripClientStatus]:
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait()[1])
    return out


def recording_sleep(stop_after: int) -> tuple[list[float], asyncio.Event, object]:
    """A `_sleep` replacement that records each backoff and sets stop after *stop_after*."""
    sleeps: list[float] = []
    stop = asyncio.Event()

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)
        if len(sleeps) >= stop_after:
            stop.set()

    return sleeps, stop, fake_sleep


def client_for(port: int, bus: Bus | None = None, driver=None, **kw) -> NtripClient:
    kw.setdefault("gga_provider", lambda: None)
    return NtripClient(
        NtripClientConfig.from_url(f"ntrip://127.0.0.1:{port}/MTRK"),
        bus or Bus(),
        driver or RecordingDriver(),
        **kw,
    )


async def test_a_caster_refusing_both_versions_is_retried_once_then_backs_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One v1 retry, not a hot loop: a 400 to v2 and v1 alike must reach the jittered backoff."""
    from mtrtk.rover import ntrip_client as mod

    requests: list[bytes] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        await writer.drain()
        writer.close()

    sleeps, stop, fake_sleep = recording_sleep(1)
    monkeypatch.setattr(mod, "_sleep", fake_sleep)
    monkeypatch.setattr(mod.random, "uniform", lambda a, b: 1.0)
    server, port = await _fake_caster(handler)
    bus = Bus()
    statuses = bus.subscribe("ntrip_client.status")
    await asyncio.wait_for(client_for(port, bus).run(stop), 5.0)
    server.close()
    await server.wait_closed()
    assert len(requests) == 2 and sleeps == [1.0]
    assert b"Ntrip-Version" in requests[0]
    assert requests[1].startswith(b"GET /MTRK HTTP/1.0") and b"Ntrip-Version" not in requests[1]
    # The v2 refusal itself is published before the v1 attempt, not only the v1 outcome.
    first = drain(statuses)[0]
    assert "400" in (first.last_error or "") and first.next_retry_s is None
    assert first.reconnects == 0


async def test_status_is_published_while_streaming_on_disconnect_and_on_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mtrtk.rover import ntrip_client as mod

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"ICY 200 OK\r\n\r\n")
        for _ in range(5):
            writer.write(RTCM_1077)
            await writer.drain()
            await asyncio.sleep(0.01)
        writer.close()

    _, stop, fake_sleep = recording_sleep(1)
    monkeypatch.setattr(mod, "_sleep", fake_sleep)
    monkeypatch.setattr(mod, "STATUS_INTERVAL_S", 0.0)
    server, port = await _fake_caster(handler)
    bus = Bus()
    statuses = bus.subscribe("ntrip_client.status")
    driver = RecordingDriver()
    await asyncio.wait_for(client_for(port, bus, driver).run(stop), 5.0)
    server.close()
    await server.wait_closed()
    seen = drain(statuses)
    assert len(driver.injected) == 5
    assert sum(1 for x in seen if x.connected) > 1  # the connect publish plus periodic ones
    dropped = [x for x in seen if not x.connected and x.next_retry_s]
    assert dropped and "closed" in (dropped[0].last_error or "")
    assert seen[-1].connected is False and seen[-1].next_retry_s is None


async def test_a_silent_stream_is_dropped_by_the_no_data_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mtrtk.rover import ntrip_client as mod

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"ICY 200 OK\r\n\r\n")
        await writer.drain()
        await reader.read()  # stay connected, say nothing
        writer.close()

    sleeps, stop, fake_sleep = recording_sleep(1)
    monkeypatch.setattr(mod, "_sleep", fake_sleep)
    monkeypatch.setattr(mod, "NO_DATA_TIMEOUT_S", 0.05)
    monkeypatch.setattr(mod.random, "uniform", lambda a, b: 1.0)
    server, port = await _fake_caster(handler)
    client = client_for(port)
    await asyncio.wait_for(client.run(stop), 2.0)
    server.close()
    await server.wait_closed()
    assert "no data" in (client.status.last_error or "") and sleeps == [1.0]


async def test_icy_followed_straight_by_data_with_no_blank_line() -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"ICY 200 OK\r\n" + RTCM_1077)
        await writer.drain()
        await reader.read()
        writer.close()

    server, port = await _fake_caster(handler)
    driver = RecordingDriver()
    client = client_for(port, driver=driver)
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if driver.injected:
            break
    assert client.status.version == 1 and driver.injected == [RTCM_1077]
    stop.set()
    await asyncio.wait_for(task, 2.0)
    server.close()
    await server.wait_closed()


async def test_v1_sticks_after_it_cured_a_v2_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    from mtrtk.rover import ntrip_client as mod

    requests: list[bytes] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        requests.append(head)
        if b"Ntrip-Version" in head:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        else:
            writer.write(b"ICY 200 OK\r\n\r\n" + RTCM_1077)
        await writer.drain()
        writer.close()  # each v1 stream then drops

    _, stop, fake_sleep = recording_sleep(2)
    monkeypatch.setattr(mod, "_sleep", fake_sleep)
    server, port = await _fake_caster(handler)
    await asyncio.wait_for(client_for(port).run(stop), 5.0)
    server.close()
    await server.wait_closed()
    assert len(requests) == 3 and b"Ntrip-Version" in requests[0]
    for later in requests[1:]:
        assert later.startswith(b"GET /MTRK HTTP/1.0") and b"Ntrip-Version" not in later


async def test_a_stream_that_was_stable_resets_the_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mtrtk.rover import ntrip_client as mod

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"ICY 200 OK\r\n\r\n" + RTCM_1077)
        await writer.drain()
        writer.close()

    sleeps, stop, fake_sleep = recording_sleep(3)
    monkeypatch.setattr(mod, "_sleep", fake_sleep)
    monkeypatch.setattr(mod, "STABLE_S", 0.0)  # every stream counts as having been stable
    monkeypatch.setattr(mod.random, "uniform", lambda a, b: 1.0)
    server, port = await _fake_caster(handler)
    await asyncio.wait_for(client_for(port).run(stop), 5.0)
    server.close()
    await server.wait_closed()
    assert sleeps == [1.0, 1.0, 1.0]  # not 1, 2, 4 as for a caster that hangs up at once


async def test_crc_dropped_accumulates_across_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    from mtrtk.rover import ntrip_client as mod

    bad = bytearray(RTCM_1077)
    bad[-1] ^= 0xFF

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"ICY 200 OK\r\n\r\n" + bytes(bad) + RTCM_1005)
        await writer.drain()
        writer.close()

    _, stop, fake_sleep = recording_sleep(2)
    monkeypatch.setattr(mod, "_sleep", fake_sleep)
    server, port = await _fake_caster(handler)
    driver = RecordingDriver()
    client = client_for(port, driver=driver)
    await asyncio.wait_for(client.run(stop), 5.0)
    server.close()
    await server.wait_closed()
    assert driver.injected == [RTCM_1005, RTCM_1005] and client.status.crc_dropped == 2


async def test_unknown_mountpoint_as_http_404_waits_60s(monkeypatch: pytest.MonkeyPatch) -> None:
    from mtrtk.rover import ntrip_client as mod

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 404 Not Found\r\n\r\n")
        await writer.drain()
        writer.close()

    sleeps, stop, fake_sleep = recording_sleep(1)
    monkeypatch.setattr(mod, "_sleep", fake_sleep)
    server, port = await _fake_caster(handler)
    client = client_for(port)
    await asyncio.wait_for(client.run(stop), 5.0)
    server.close()
    await server.wait_closed()
    assert "mountpoint" in (client.status.last_error or "") and sleeps == [60.0]


@pytest.mark.parametrize("first", ["raise", "str", "oserror"])
async def test_a_bad_gga_provider_answer_does_not_end_gga_upload(first: str) -> None:
    """A provider that raises (an OSError included: it may read a file or a port) or returns a
    str once is logged; later GGA still goes out, with CRLF appended when the provider left it
    off. Only the caster's socket failing ends the GGA upload."""
    lines: list[bytes] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"ICY 200 OK\r\n\r\n")
        await writer.drain()
        lines.append(await reader.readuntil(b"\r\n"))
        await reader.read()
        writer.close()

    calls = 0

    def provider():
        nonlocal calls
        calls += 1
        if calls == 1:
            if first == "raise":
                raise RuntimeError("no fix yet")
            if first == "oserror":
                raise OSError(5, "Input/output error")
            return GGA.decode()
        return GGA.rstrip()  # no CRLF

    server, port = await _fake_caster(handler)
    client = client_for(port, gga_provider=provider, gga_interval_s=0.02)
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if lines:
            break
    assert lines == [GGA]
    stop.set()
    await asyncio.wait_for(task, 2.0)
    server.close()
    await server.wait_closed()


async def test_a_gga_write_that_raises_something_else_does_not_end_gga_upload(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Only an OSError (the socket is gone) ends the GGA upload; any other write failure is
    logged and the next GGA still goes out."""
    from mtrtk.rover import ntrip_client as mod

    lines: list[bytes] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"ICY 200 OK\r\n\r\n")
        await writer.drain()
        lines.append(await reader.readuntil(b"\r\n"))
        await reader.read()
        writer.close()

    real_write = asyncio.StreamWriter.write
    failed = []

    def write_once_broken(self: asyncio.StreamWriter, data: bytes) -> None:
        if data.startswith(b"$") and not failed:
            failed.append(data)
            raise RuntimeError("transport refused the write")
        real_write(self, data)

    monkeypatch.setattr(asyncio.StreamWriter, "write", write_once_broken)
    server, port = await _fake_caster(handler)
    client = client_for(port, gga_provider=lambda: GGA, gga_interval_s=0.02)
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    with caplog.at_level(logging.ERROR, logger=mod.__name__):
        for _ in range(100):
            await asyncio.sleep(0.02)
            if lines:
                break
    assert failed and lines == [GGA]
    assert any("GGA write failed" in r.getMessage() for r in caplog.records)
    stop.set()
    await asyncio.wait_for(task, 2.0)
    server.close()
    await server.wait_closed()


@pytest.mark.parametrize("error", [ValueError, RuntimeError])
async def test_a_driver_bug_is_logged_as_a_crash_and_retried_with_backoff(
    error: type[Exception], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from mtrtk.rover import ntrip_client as mod

    class BrokenDriver(RecordingDriver):
        async def inject_rtcm(self, data: bytes) -> None:
            raise error("driver bug")

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"ICY 200 OK\r\n\r\n" + RTCM_1077)
        await writer.drain()
        await reader.read()
        writer.close()

    sleeps, stop, fake_sleep = recording_sleep(1)
    monkeypatch.setattr(mod, "_sleep", fake_sleep)
    monkeypatch.setattr(mod.random, "uniform", lambda a, b: 1.0)
    server, port = await _fake_caster(handler)
    client = client_for(port, driver=BrokenDriver())
    with caplog.at_level(logging.ERROR, logger=mod.__name__):
        await asyncio.wait_for(client.run(stop), 5.0)
    server.close()
    await server.wait_closed()
    assert sleeps == [1.0] and error.__name__ in (client.status.last_error or "")
    crashes = [r for r in caplog.records if r.levelno >= logging.ERROR and r.exc_info]
    assert crashes and "driver bug" in str(crashes[0].exc_info[1])  # type: ignore[index]


async def test_cancelling_run_leaves_a_disconnected_status() -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"ICY 200 OK\r\n\r\n")
        await writer.drain()
        await reader.read()
        writer.close()

    server, port = await _fake_caster(handler)
    bus = Bus()
    statuses = bus.subscribe("ntrip_client.status")
    client = client_for(port, bus)
    task = asyncio.create_task(client.run(asyncio.Event()))
    await wait_connected(client)
    assert client.status.connected
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2.0)
    server.close()
    await server.wait_closed()
    assert client.status.connected is False
    assert drain(statuses)[-1].connected is False


@pytest.mark.parametrize("interval", [0, -1])
async def test_a_zero_gga_interval_sends_no_gga(interval: float) -> None:
    """0 means "do not send GGA" (as in str2str), never a write loop with no pause."""
    received = bytearray()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"ICY 200 OK\r\n\r\n")
        await writer.drain()
        while chunk := await reader.read(65536):
            received.extend(chunk)
        writer.close()

    calls = 0

    def provider() -> bytes:
        nonlocal calls
        calls += 1
        return GGA

    server, port = await _fake_caster(handler)
    client = client_for(port, gga_provider=provider, gga_interval_s=interval)
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    await wait_connected(client)
    await asyncio.sleep(0.2)
    assert client.status.connected
    assert calls == 0 and bytes(received) == b""
    stop.set()
    await asyncio.wait_for(task, 2.0)
    server.close()
    await server.wait_closed()
