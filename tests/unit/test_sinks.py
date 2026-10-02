import asyncio
import contextlib
import os
import re
import socket
import tty
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from mtrtk.config import Settings
from mtrtk.rover import sinks as sinks_mod
from mtrtk.rover.sinks import SerialSink, TcpBroadcastSink, UdpSink


async def _wait_for(cond: Callable[[], bool]) -> None:
    deadline = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < deadline:
        if cond():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never became true")


async def test_tcp_broadcast_to_all_clients() -> None:
    sink = TcpBroadcastSink("127.0.0.1", 0)
    await sink.start()
    r1, w1 = await asyncio.open_connection("127.0.0.1", sink.port)
    r2, w2 = await asyncio.open_connection("127.0.0.1", sink.port)
    await _wait_for(lambda: sink.client_count == 2)
    await sink.write(b"$GNGGA,x*00\r\n")
    assert await asyncio.wait_for(r1.readline(), 1.0) == b"$GNGGA,x*00\r\n"
    assert await asyncio.wait_for(r2.readline(), 1.0) == b"$GNGGA,x*00\r\n"
    w1.close()
    await _wait_for(lambda: sink.client_count == 1)  # the closed client is forgotten
    await sink.write(b"$GNRMC,y*00\r\n")  # closed client must not raise
    assert await asyncio.wait_for(r2.readline(), 1.0) == b"$GNRMC,y*00\r\n"
    w2.close()
    await sink.close()


async def test_tcp_sink_drops_a_client_that_stops_reading() -> None:
    sink = TcpBroadcastSink("127.0.0.1", 0)
    await sink.start()
    _, w = await asyncio.open_connection("127.0.0.1", sink.port)
    await _wait_for(lambda: sink.client_count == 1)
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
    probe = TcpBroadcastSink("127.0.0.1", 0)
    await probe.start()
    port = probe.port
    await probe.close()
    sink = TcpBroadcastSink("127.0.0.1", port)  # a fixed port, like the daemon's 10110
    await sink.start()
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await _wait_for(lambda: sink.client_count == 1)
    await sink.close()  # with a client still connected
    w.close()
    await sink.start()  # a supervised restart reuses the object and rebinds the port at once
    try:
        assert sink.port == port
        r, w = await asyncio.open_connection("127.0.0.1", port)
        await _wait_for(lambda: sink.client_count == 1)
        await sink.write(b"$GNGGA,again*00\r\n")
        assert await asyncio.wait_for(r.readline(), 1.0) == b"$GNGGA,again*00\r\n"
        w.close()
    finally:
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
    await _wait_for(lambda: len(received) == 2)
    assert received == [b"hello", b"hello"]
    await sink.close()
    transport.close()


async def _udp_receiver() -> tuple[asyncio.DatagramTransport, int, list[bytes]]:
    received: list[bytes] = []

    class Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            received.append(data)

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(Proto, local_addr=("127.0.0.1", 0))
    return transport, transport.get_extra_info("sockname")[1], received


def _fake_resolver(
    monkeypatch: pytest.MonkeyPatch, answer: Callable[[str, int, int], Any]
) -> list[str]:
    """Replace the running loop's getaddrinfo: no test depends on the host's DNS."""
    loop = asyncio.get_running_loop()
    real = loop.getaddrinfo
    calls: list[str] = []
    counts: dict[str, int] = {}

    async def fake(host: str, port: int, **kw: Any) -> Any:
        calls.append(host)
        counts[host] = counts.get(host, 0) + 1
        result = answer(host, port, counts[host])
        if result is None:
            return await real(host, port, **kw)
        if isinstance(result, float):
            await asyncio.sleep(result)
            raise socket.gaierror(socket.EAI_AGAIN, "timed out")
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(loop, "getaddrinfo", fake)
    return calls


def _addr(port: int) -> Any:
    return [(socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("127.0.0.1", port))]


async def test_udp_sink_skips_unresolvable_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_resolver(monkeypatch, lambda h, p, n: socket.gaierror(socket.EAI_NONAME, "no"))
    sink = UdpSink([("no-such-host.invalid", 9)])
    await sink.start()
    await sink.write(b"hello")  # must not raise or block
    assert sink.unresolved == ["no-such-host.invalid"]
    await sink.close()


async def test_udp_sink_retries_unresolved_targets_in_the_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, port, received = await _udp_receiver()
    monkeypatch.setattr(sinks_mod, "UDP_RESOLVE_RETRY_S", 0.01)
    # A container that is not up yet: the first lookup fails, a later one resolves.
    _fake_resolver(
        monkeypatch,
        lambda h, p, n: socket.gaierror(socket.EAI_NONAME, "no") if n == 1 else _addr(port),
    )
    sink = UdpSink([("late.example", port)])
    await sink.start()
    try:
        assert sink.unresolved == ["late.example"]
        await _wait_for(lambda: sink.unresolved == [])
        await sink.write(b"hello")
        await _wait_for(lambda: received == [b"hello"])
    finally:
        await sink.close()
        transport.close()


async def test_udp_sink_write_never_waits_on_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, port, received = await _udp_receiver()
    monkeypatch.setattr(sinks_mod, "UDP_RESOLVE_RETRY_S", 0.0)  # a retry is always due
    monkeypatch.setattr(sinks_mod, "UDP_RESOLVE_TIMEOUT_S", 0.05)
    # A resolver that cannot be reached: every lookup of the name hangs for 2 s, then fails.
    _fake_resolver(monkeypatch, lambda h, p, n: 2.0 if h == "pending.example" else None)
    sink = UdpSink([("pending.example", 9), ("127.0.0.1", port)])
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await sink.start()
    try:
        assert loop.time() - t0 < 0.5  # the lookup at start is bounded
        assert sink.unresolved == ["pending.example"]
        for _ in range(3):
            t0 = loop.time()
            await sink.write(b"hello")
            assert loop.time() - t0 < 0.05  # write() only sends; the retry runs elsewhere
        await _wait_for(lambda: len(received) == 3)
    finally:
        await sink.close()
        transport.close()


async def test_udp_sink_splits_a_large_epoch_on_sentence_boundaries() -> None:
    transport, port, received = await _udp_receiver()
    sink = UdpSink([("127.0.0.1", port)])
    await sink.start()
    try:
        sentences = [f"$GPGSV,{i:03d},{'x' * 70}*00\r\n".encode() for i in range(40)]
        payload = b"".join(sentences)  # about 3.2 KB: over one 1500-byte MTU
        await sink.write(payload)
        await _wait_for(lambda: b"".join(received) == payload)
        assert len(received) > 1
        assert all(len(d) <= sinks_mod.UDP_MAX_PAYLOAD and d.endswith(b"\r\n") for d in received)
        received.clear()
        doc = b'{"lat":' + b"1" * 2000 + b"}"  # a JSON document has no CRLF: sent whole
        await sink.write(doc)
        await _wait_for(lambda: received == [doc])
    finally:
        await sink.close()
        transport.close()


async def test_pty_sink(tmp_path: Path) -> None:
    sink = SerialSink("pty")
    await sink.start()
    assert sink.slave_path and os.path.exists(sink.slave_path)  # noqa: ASYNC240
    fd = os.open(sink.slave_path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        await sink.write(b"$GNGGA,test*00\r\n")
        got = bytearray()

        def readable() -> bool:
            with contextlib.suppress(BlockingIOError):
                got.extend(os.read(fd, 100))
            return len(got) >= 16

        await _wait_for(readable)
        assert bytes(got) == b"$GNGGA,test*00\r\n"
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


async def test_pty_reader_that_attaches_late_gets_recent_sentences_first() -> None:
    sink = SerialSink("pty")
    await sink.start()
    assert sink.slave_path is not None
    n = 6000
    try:
        for i in range(n):  # about 240 KB with nobody reading: far more than any buffer
            await sink.write(f"$GNGGA,{i:06d},nobody-reads-yet*00\r\n".encode())
        assert sink.dropped_bytes > 0
        fd = os.open(sink.slave_path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            data = bytearray()
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                data.extend(chunk)
        finally:
            os.close(fd)
    finally:
        await sink.close()
    lines = bytes(data).split(b"\r\n")[:-1]  # the last piece may still be partly queued
    assert lines, "the late reader got nothing"
    pattern = re.compile(rb"\$GNGGA,(\d{6}),nobody-reads-yet\*00")
    seqs = []
    for line in lines:
        m = pattern.fullmatch(line)
        assert m, f"spliced or partial sentence: {line!r}"
        seqs.append(int(m.group(1)))
    assert seqs == sorted(seqs)
    # Drop-oldest: what is still queued is the newest data, not sentence 0 from the start.
    assert seqs[0] > n // 2


def _raw_pty() -> tuple[int, int, str]:
    """A pty whose slave stands in for a USB-serial adapter (no hardware is touched)."""
    master, slave = os.openpty()
    tty.setraw(slave)
    os.set_blocking(master, False)
    return master, slave, os.ttyname(slave)


async def test_real_serial_port_writes_drops_backlog_and_closes() -> None:
    master, slave, path = _raw_pty()
    sink = SerialSink(path, 115200)
    try:
        await sink.start()
        await sink.write(b"$GNGGA,serial*00\r\n")
        got = bytearray()

        def readable() -> bool:
            with contextlib.suppress(BlockingIOError):
                got.extend(os.read(master, 100))
            return len(got) >= 18

        await _wait_for(readable)
        assert bytes(got) == b"$GNGGA,serial*00\r\n"
        for _ in range(400):  # nobody reads the other end any more: the backlog is capped
            await sink.write(b"x" * 1024)
        assert sink.dropped_bytes > 0
        await sink.close()
        await sink.write(b"$GNGGA,closed*00\r\n")  # a closed sink is a no-op
    finally:
        await sink.close()
        os.close(master)
        os.close(slave)


async def test_real_serial_port_reports_a_vanished_device() -> None:
    master, slave, path = _raw_pty()
    sink = SerialSink(path, 115200)
    await sink.start()
    try:
        await sink.write(b"$GNGGA,before*00\r\n")
        os.close(master)  # the adapter is unplugged
        os.close(slave)
        with pytest.raises(ConnectionError):
            await sink.write(b"$GNGGA,after*00\r\n")  # the publisher must hear about it
    finally:
        await sink.close()


async def test_missing_serial_device_raises_on_start(tmp_path: Path) -> None:
    sink = SerialSink(str(tmp_path / "ttyNOPE"), 115200)
    with pytest.raises(OSError):
        await sink.start()


def test_nmea_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NMEA_SENTENCES", raising=False)
    monkeypatch.delenv("NMEA_SLOW_INTERVAL_S", raising=False)
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


async def test_tcp_sink_turns_away_clients_over_its_cap() -> None:
    """Each client costs a task, an fd and a buffer: past the cap a connection is closed at once."""
    sink = TcpBroadcastSink("127.0.0.1", 0, max_clients=1)
    await sink.start()
    try:
        r1, w1 = await asyncio.open_connection("127.0.0.1", sink.port)
        await _wait_for(lambda: sink.client_count == 1)
        r2, w2 = await asyncio.open_connection("127.0.0.1", sink.port)
        assert await asyncio.wait_for(r2.read(), 1.0) == b""  # closed by the server
        assert sink.client_count == 1 and sink.refused_clients == 1
        await sink.write(b"$GNGGA,x*00\r\n")
        assert await asyncio.wait_for(r1.readline(), 1.0) == b"$GNGGA,x*00\r\n"
        w1.close()
        w2.close()
    finally:
        await sink.close()


async def test_tcp_sink_on_tailscale_never_falls_back_to_every_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NMEA_TCP_BIND=tailscale with tailscale0 down: start fails (and is retried), no 0.0.0.0."""
    from mtrtk.core import exposure

    monkeypatch.setattr(exposure, "tailscale_ipv4", lambda: None)
    sink = TcpBroadcastSink("tailscale", 0)
    with pytest.raises(OSError, match="tailscale"):
        await sink.start()
    assert sink.client_count == 0
    monkeypatch.setattr(exposure, "tailscale_ipv4", lambda: "127.0.0.1")
    await sink.start()
    try:
        assert sink.host == "127.0.0.1" and sink.port > 0
    finally:
        await sink.close()
