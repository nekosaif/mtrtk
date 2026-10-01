import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from mtrtk.base.ntrip_caster import CasterConfig, NtripCaster
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.core.statestore import StateStore
from mtrtk.rover.drivers.base import DriverCapabilities
from mtrtk.rover.drivers.ublox import UbloxDriver
from mtrtk.rover.ntrip_client import NtripClient, NtripClientConfig, decode_chunked
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
    client = NtripClient(
        NtripClientConfig.from_url("ntrip://127.0.0.1:1/MTRK"),
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
    with pytest.raises(ValueError):
        await decode_chunked(reader)


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
