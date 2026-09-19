import asyncio
import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from mtrtk.base import ntrip_caster
from mtrtk.base.ntrip_caster import CasterConfig, ClientInfo, NtripCaster, parse_request
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.store.db import Database
from mtrtk.store.repos import NtripLogRepo
from ubxtest import rtcm_frame

RTCM_1005 = bytes.fromhex("d300133ed7fd0382dfdc1c403db34fe8fe0cef5e6b30bd2e23")
RTCM_1077 = rtcm_frame(1077, b"\x00" * 40)
RTCM_1230 = rtcm_frame(1230, b"\x00" * 6)
AUTH = "Basic " + base64.b64encode(b"rover:secret").decode()


def config(password: str = "secret") -> CasterConfig:
    return CasterConfig(
        mountpoint="MTRK", username="rover", password=password, station_id="MTRK", country="BGD"
    )


@pytest.fixture
async def caster(tmp_path: Path):
    db = Database(tmp_path / "m.db")
    await db.open()
    bus = Bus()
    c = NtripCaster(
        bus,
        config(),
        host="127.0.0.1",
        port=0,
        ntrip_log=NtripLogRepo(db),
        position=lambda: (23.84, 90.26),
        bitrate=lambda: 1900.0,
    )
    await c.start()
    try:
        yield c, bus, NtripLogRepo(db)
    finally:
        await c.stop()
        await db.close()


def publish(bus: Bus, raw: bytes) -> None:
    for frame in Framer().feed(raw):
        bus.publish("raw.rtcm", frame)


async def request(port: int, head: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head.encode())
    await writer.drain()
    return reader, writer


async def read_headers(reader: asyncio.StreamReader) -> bytes:
    return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2.0)


def test_parse_request() -> None:
    req = parse_request(
        b"GET /MTRK HTTP/1.1\r\nHost: x\r\nNtrip-Version: Ntrip/2.0\r\n"
        b"User-Agent: NTRIP test/1.0\r\nAuthorization: Basic abc\r\n\r\n"
    )
    assert req.method == "GET" and req.path == "/MTRK" and req.v2 is True
    assert (
        req.headers["user-agent"] == "NTRIP test/1.0"
        and req.headers["authorization"] == "Basic abc"
    )
    v1 = parse_request(b"GET /MTRK HTTP/1.0\r\nUser-Agent: NTRIP str2str\r\n\r\n")
    assert v1.v2 is False and v1.headers.get("ntrip-version") is None


async def test_v1_stream_receives_cached_1005_then_live_frames(caster) -> None:
    c, bus, _ = caster
    publish(bus, RTCM_1005 + RTCM_1230)  # cached before any client connects
    await asyncio.sleep(0.02)
    reader, writer = await request(
        c.port, f"GET /MTRK HTTP/1.0\r\nUser-Agent: NTRIP str2str\r\nAuthorization: {AUTH}\r\n\r\n"
    )
    assert await read_headers(reader) == b"ICY 200 OK\r\n\r\n"
    cached = await asyncio.wait_for(reader.readexactly(len(RTCM_1005) + len(RTCM_1230)), 2.0)
    assert cached == RTCM_1005 + RTCM_1230
    publish(bus, RTCM_1077)
    assert await asyncio.wait_for(reader.readexactly(len(RTCM_1077)), 2.0) == RTCM_1077
    assert len(c.clients) == 1
    info = next(iter(c.clients.values()))
    assert info.version == 1 and info.username == "rover"
    assert info.bytes_sent == len(RTCM_1005) + len(RTCM_1230) + len(RTCM_1077)
    public = json.loads(json.dumps(info.public()))  # the web API serves this as-is
    assert public["mountpoint"] == "MTRK" and public["dropped_frames"] == 0
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)
    assert c.clients == {}


async def test_v2_stream_is_chunked_with_ntrip_headers(caster) -> None:
    c, bus, _ = caster
    reader, writer = await request(
        c.port,
        f"GET /MTRK HTTP/1.1\r\nHost: base\r\nNtrip-Version: Ntrip/2.0\r\n"
        f"User-Agent: NTRIP SWMaps\r\nAuthorization: {AUTH}\r\n\r\n",
    )
    head = await read_headers(reader)
    assert head.startswith(b"HTTP/1.1 200 OK\r\n")
    for expected in (
        b"Ntrip-Version: Ntrip/2.0",
        b"Content-Type: gnss/data",
        b"Transfer-Encoding: chunked",
        b"Cache-Control: no-store, no-cache, max-age=0",
        b"Connection: close",
    ):
        assert expected in head
    publish(bus, RTCM_1077)
    size_line = await asyncio.wait_for(reader.readline(), 2.0)
    assert int(size_line.strip(), 16) == len(RTCM_1077)
    body = await asyncio.wait_for(reader.readexactly(len(RTCM_1077) + 2), 2.0)
    assert body == RTCM_1077 + b"\r\n"
    writer.close()
    await writer.wait_closed()


async def test_sourcetable_v1_and_v2(caster) -> None:
    c, _, _ = caster
    reader, writer = await request(c.port, "GET / HTTP/1.0\r\nUser-Agent: NTRIP x\r\n\r\n")
    head = await read_headers(reader)
    assert head.startswith(b"SOURCETABLE 200 OK\r\n") and b"Content-Type: text/plain" in head
    body = await asyncio.wait_for(reader.read(-1), 2.0)
    assert body.startswith(
        b"STR;MTRK;mtrtk;RTCM 3.3;1005(1),1077(1),1087(1),1097(1),1127(1),1230(5);2;"
        b"GPS+GLO+GAL+BDS;mtrtk;BGD;23.84;90.26;0;0;u-blox ZED-F9P;none;B;N;1900;\r\n"
    )
    assert body.endswith(b"ENDSOURCETABLE\r\n")
    writer.close()
    reader, writer = await request(
        c.port, "GET / HTTP/1.1\r\nHost: x\r\nNtrip-Version: Ntrip/2.0\r\n\r\n"
    )
    head = await read_headers(reader)
    assert head.startswith(b"HTTP/1.1 200 OK\r\n") and b"Content-Type: gnss/sourcetable" in head
    writer.close()


async def test_unknown_mount_v1_sourcetable_v2_404(caster) -> None:
    c, _, _ = caster
    reader, writer = await request(c.port, f"GET /NOPE HTTP/1.0\r\nAuthorization: {AUTH}\r\n\r\n")
    assert (await read_headers(reader)).startswith(b"SOURCETABLE 200 OK\r\n")
    writer.close()
    reader, writer = await request(
        c.port, f"GET /NOPE HTTP/1.1\r\nNtrip-Version: Ntrip/2.0\r\nAuthorization: {AUTH}\r\n\r\n"
    )
    assert (await read_headers(reader)).startswith(b"HTTP/1.1 404 Not Found\r\n")
    writer.close()


async def test_bad_or_missing_auth_is_401(caster) -> None:
    c, _, _ = caster
    reader, writer = await request(c.port, "GET /MTRK HTTP/1.0\r\n\r\n")
    head = await read_headers(reader)
    assert head.startswith(b"HTTP/1.0 401 Unauthorized\r\n")
    assert b'WWW-Authenticate: Basic realm="mtrtk"' in head
    writer.close()
    bad = "Basic " + base64.b64encode(b"rover:wrong").decode()
    reader, writer = await request(
        c.port, f"GET /MTRK HTTP/1.1\r\nNtrip-Version: Ntrip/2.0\r\nAuthorization: {bad}\r\n\r\n"
    )
    assert (await read_headers(reader)).startswith(b"HTTP/1.1 401 Unauthorized\r\n")
    writer.close()


async def test_anonymous_when_password_empty(tmp_path: Path) -> None:
    bus = Bus()
    c = NtripCaster(bus, config(password=""), host="127.0.0.1", port=0)
    await c.start()
    try:
        reader, writer = await request(c.port, "GET /MTRK HTTP/1.0\r\n\r\n")
        assert await read_headers(reader) == b"ICY 200 OK\r\n\r\n"
        assert c.sourcetable_body().split(b";")[15] == b"N"  # authentication field
        writer.close()
    finally:
        await c.stop()


async def test_gga_intake_updates_client_and_publishes(caster) -> None:
    c, bus, log_repo = caster
    clients_sub = bus.subscribe("ntrip.clients")
    reader, writer = await request(
        c.port, f"GET /MTRK HTTP/1.0\r\nUser-Agent: NTRIP rover\r\nAuthorization: {AUTH}\r\n\r\n"
    )
    await read_headers(reader)
    writer.write(
        b"$GNGGA,164734.00,2350.24104,N,09015.75301,E,1,12,0.9,13.3,M,-49.6,M,0.0,0*78\r\n"
    )
    await writer.drain()
    await asyncio.sleep(0.05)
    info = next(iter(c.clients.values()))
    assert info.last_gga_lat == pytest.approx(23.8373507, abs=1e-6)
    assert info.last_gga_lon == pytest.approx(90.2625502, abs=1e-6)
    assert clients_sub.queue.qsize() >= 2  # connect + gga
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)
    rows = await log_repo.recent()
    assert len(rows) == 1
    assert rows[0].last_lat == pytest.approx(23.8373507, abs=1e-6)
    assert rows[0].disconnected_utc is not None


async def test_source_upload_not_supported(caster) -> None:
    c, _, _ = caster
    reader, writer = await request(c.port, "SOURCE pw /MTRK\r\nSource-Agent: NTRIP x\r\n\r\n")
    assert (await asyncio.wait_for(reader.read(-1), 2.0)).startswith(b"ERROR - Not Supported")
    writer.close()


async def test_oversized_header_is_rejected(caster) -> None:
    c, _, _ = caster
    reader, writer = await asyncio.open_connection("127.0.0.1", c.port)
    writer.write(b"GET /MTRK HTTP/1.0\r\nX: " + b"a" * 9000 + b"\r\n\r\n")
    await writer.drain()
    data = await asyncio.wait_for(reader.read(-1), 2.0)
    assert data == b"" or data.startswith(b"HTTP/1.0 400")
    writer.close()


async def test_unterminated_header_times_out(caster, monkeypatch: pytest.MonkeyPatch) -> None:
    c, _, _ = caster
    monkeypatch.setattr(ntrip_caster, "HEADER_TIMEOUT_S", 0.05)
    reader, writer = await asyncio.open_connection("127.0.0.1", c.port)
    writer.write(b"GET /MTRK HTTP/1.0\r\n")  # a head that never ends
    await writer.drain()
    assert (await asyncio.wait_for(reader.read(-1), 2.0)).startswith(b"HTTP/1.0 400")
    assert c.clients == {}
    writer.close()
    await writer.wait_closed()


def test_offer_drops_oldest_when_client_queue_full() -> None:
    from mtrtk.base.ntrip_caster import CLIENT_QUEUE_FRAMES, _Client

    client = _Client.__new__(_Client)
    client.queue = asyncio.Queue(maxsize=CLIENT_QUEUE_FRAMES)
    client.dropped = 0
    for i in range(CLIENT_QUEUE_FRAMES + 3):
        NtripCaster._offer(client, bytes([i]))
    assert client.dropped == 3 and client.queue.qsize() == CLIENT_QUEUE_FRAMES
    assert client.queue.get_nowait() == bytes([3])


class _StalledWriter:
    """A writer whose send buffer never drains, as a rover that stops reading looks to us."""

    def __init__(self, buffered: int) -> None:
        self.transport = SimpleNamespace(
            get_write_buffer_size=lambda: buffered,
            set_write_buffer_limits=lambda **kwargs: None,
        )
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written += data

    def is_closing(self) -> bool:
        return False

    async def drain(self) -> None:
        return None


async def test_slow_client_is_disconnected_after_the_grace_period(
    caster, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _, _ = caster
    monkeypatch.setattr(ntrip_caster, "SLOW_CLIENT_GRACE_S", 0.0)
    writer = _StalledWriter(ntrip_caster.SLOW_CLIENT_BYTES + 1)
    info = ClientInfo(
        id=99,
        ip="127.0.0.1",
        port=1,
        mountpoint="MTRK",
        user_agent="stalled",
        username="rover",
        version=2,
        connected_utc=datetime.now(UTC),
    )
    conn = ntrip_caster._Client(info, writer, v2=True)
    for _ in range(3):
        conn.queue.put_nowait(RTCM_1077)
    reason = await asyncio.wait_for(c._write_loop(conn), 2.0)
    assert reason == "slow client"
    assert bytes(writer.written).startswith(f"{len(RTCM_1077):X}\r\n".encode() + RTCM_1077)


async def test_stop_ends_the_v2_stream_with_the_terminating_chunk() -> None:
    bus = Bus()
    c = NtripCaster(bus, config(password=""), host="127.0.0.1", port=0)
    await c.start()
    reader, writer = await request(c.port, "GET /MTRK HTTP/1.1\r\nNtrip-Version: Ntrip/2.0\r\n\r\n")
    await read_headers(reader)
    publish(bus, RTCM_1077)
    await asyncio.sleep(0.05)
    await c.stop()
    rest = await asyncio.wait_for(reader.read(-1), 2.0)
    assert rest.endswith(b"0\r\n\r\n")
    assert c.clients == {}
    assert bus.subscriber_count == 0
    writer.close()
    await writer.wait_closed()
