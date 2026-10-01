import asyncio
import os
from pathlib import Path

import pytest

from mtrtk.config import Settings
from mtrtk.rover.sinks import SerialSink, TcpBroadcastSink, UdpSink


async def test_tcp_broadcast_to_all_clients() -> None:
    sink = TcpBroadcastSink("127.0.0.1", 0)
    await sink.start()
    r1, w1 = await asyncio.open_connection("127.0.0.1", sink.port)
    r2, w2 = await asyncio.open_connection("127.0.0.1", sink.port)
    await asyncio.sleep(0.02)
    assert sink.client_count == 2
    await sink.write(b"$GNGGA,x*00\r\n")
    assert await asyncio.wait_for(r1.readline(), 1.0) == b"$GNGGA,x*00\r\n"
    assert await asyncio.wait_for(r2.readline(), 1.0) == b"$GNGGA,x*00\r\n"
    w1.close()
    await asyncio.sleep(0.02)
    await sink.write(b"$GNRMC,y*00\r\n")  # closed client must not raise
    assert await asyncio.wait_for(r2.readline(), 1.0) == b"$GNRMC,y*00\r\n"
    w2.close()
    await sink.close()


async def test_tcp_sink_drops_a_client_that_stops_reading() -> None:
    sink = TcpBroadcastSink("127.0.0.1", 0)
    await sink.start()
    _, w = await asyncio.open_connection("127.0.0.1", sink.port)
    await asyncio.sleep(0.02)
    try:
        chunk = b"x" * 65536
        for _ in range(200):  # far more than the kernel buffers plus the backlog limit
            await sink.write(chunk)
            if sink.client_count == 0:
                break
        assert sink.client_count == 0
        assert sink.dropped_clients == 1
    finally:
        w.close()
        await sink.close()


async def test_tcp_sink_restarts_on_the_same_port() -> None:
    sink = TcpBroadcastSink("127.0.0.1", 0)
    await sink.start()
    await sink.close()
    await sink.start()  # a supervised restart reuses the object
    _, w = await asyncio.open_connection("127.0.0.1", sink.port)
    w.close()
    await sink.close()


async def test_udp_sink_sends_to_each_target() -> None:
    loop = asyncio.get_running_loop()
    received: list[bytes] = []

    class Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            received.append(data)

    transport, _ = await loop.create_datagram_endpoint(Proto, local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]
    sink = UdpSink([("127.0.0.1", port), ("localhost", port)])
    await sink.start()
    await sink.write(b"hello")
    await asyncio.sleep(0.05)
    assert received == [b"hello", b"hello"]
    await sink.close()
    transport.close()


async def test_udp_sink_skips_unresolvable_targets() -> None:
    sink = UdpSink([("no-such-host.invalid", 9)])
    await sink.start()
    await sink.write(b"hello")  # must not raise or block
    assert sink.unresolved == ["no-such-host.invalid"]
    await sink.close()


async def test_pty_sink(tmp_path: Path) -> None:
    sink = SerialSink("pty")
    await sink.start()
    assert sink.slave_path and os.path.exists(sink.slave_path)  # noqa: ASYNC240
    fd = os.open(sink.slave_path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        await sink.write(b"$GNGGA,test*00\r\n")
        await asyncio.sleep(0.02)
        assert os.read(fd, 100) == b"$GNGGA,test*00\r\n"
    finally:
        os.close(fd)
        await sink.close()


async def test_pty_sink_without_reader_never_blocks_or_raises() -> None:
    sink = SerialSink("pty")
    await sink.start()
    try:
        for _ in range(2000):
            await sink.write(b"$GNGGA,nobody-reads-this*00\r\n")
        assert sink.dropped_bytes > 0
    finally:
        await sink.close()
    assert sink.slave_path is None


async def test_missing_serial_device_raises_on_start(tmp_path: Path) -> None:
    sink = SerialSink(str(tmp_path / "ttyNOPE"), 115200)
    with pytest.raises(OSError):
        await sink.start()


def test_nmea_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    s = Settings(_env_file=None, ntrip_password="pw")
    assert s.nmea_sentences == ["GGA", "RMC", "GST", "GSA", "GSV", "VTG", "ZDA"]
    assert s.nmea_slow_interval_s == 1.0
    monkeypatch.setenv("NMEA_SENTENCES", " gga, hdt ,PASHR,")
    monkeypatch.setenv("NMEA_SLOW_INTERVAL_S", "2.5")
    s = Settings(_env_file=None, ntrip_password="pw")
    assert s.nmea_sentences == ["GGA", "HDT", "PASHR"] and s.nmea_slow_interval_s == 2.5
    monkeypatch.setenv("NMEA_SENTENCES", "GGA,GLL")
    with pytest.raises(ValueError, match="GLL"):
        Settings(_env_file=None, ntrip_password="pw")
    monkeypatch.setenv("NMEA_SENTENCES", "GGA")
    monkeypatch.setenv("NMEA_SLOW_INTERVAL_S", "0")
    with pytest.raises(ValueError):
        Settings(_env_file=None, ntrip_password="pw")
