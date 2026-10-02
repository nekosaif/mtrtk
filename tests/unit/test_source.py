from pathlib import Path

import pytest
from pyubx2 import GET, UBXMessage

from mtrtk.core import source as source_mod
from mtrtk.core.source import FileReplaySource, SerialSource, find_ublox_port
from ubxtest import ubx_frame


def pvt(itow: int) -> bytes:
    return UBXMessage("NAV", "NAV-PVT", GET, iTOW=itow).serialize()


def eoe(itow: int) -> bytes:
    return UBXMessage("NAV", "NAV-EOE", GET, iTOW=itow).serialize()


RAWX = ubx_frame(0x02, 0x15, b"\x00" * 16)


async def read_all(src: FileReplaySource) -> list[bytes]:
    chunks: list[bytes] = []
    while chunk := await src.read():
        chunks.append(chunk)
    return chunks


async def test_replay_paces_on_nav_pvt(tmp_path: Path) -> None:
    path = tmp_path / "r.ubx"
    path.write_bytes(pvt(1000) + RAWX + pvt(2000) + pvt(3000))
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    src = FileReplaySource(path, speed=2.0, sleep=fake_sleep)
    await src.open()
    chunks = await read_all(src)
    assert len(chunks) == 3
    assert sleeps == [0.5, 0.5]
    assert chunks[1] == RAWX + pvt(2000)


async def test_replay_prefers_nav_eoe_marker(tmp_path: Path) -> None:
    path = tmp_path / "r.ubx"
    path.write_bytes(pvt(1000) + RAWX + eoe(1000) + pvt(2000) + eoe(2000))
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    src = FileReplaySource(path, speed=1.0, sleep=fake_sleep)
    await src.open()
    chunks = await read_all(src)
    assert len(chunks) == 2
    assert chunks[0] == pvt(1000) + RAWX + eoe(1000)
    assert sleeps == [1.0]


async def test_replay_speed_zero_never_paces_but_yields_once_per_chunk(tmp_path: Path) -> None:
    """Speed 0 must not *pace*, but it still has to give the event loop a turn per chunk."""
    path = tmp_path / "r.ubx"
    path.write_bytes(pvt(1000) + pvt(2000))
    sleeps: list[float] = []

    async def no_delay(delay: float) -> None:
        if delay > 0:
            raise AssertionError(f"must not pace at speed 0 (slept {delay})")
        sleeps.append(delay)

    src = FileReplaySource(path, speed=0, sleep=no_delay)
    await src.open()
    assert len(await read_all(src)) == 2
    assert sleeps == [0, 0]  # one cooperative yield per returned chunk, none at EOF


async def test_replay_loop_restarts(tmp_path: Path) -> None:
    path = tmp_path / "r.ubx"
    path.write_bytes(pvt(1000) + pvt(2000))

    async def fake_sleep(delay: float) -> None:
        return None

    src = FileReplaySource(path, speed=0, loop=True, sleep=fake_sleep)
    await src.open()
    chunks = [await src.read() for _ in range(5)]
    assert all(chunks)
    assert chunks[2] == pvt(1000)


async def test_replay_write_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "r.ubx"
    path.write_bytes(pvt(1))
    src = FileReplaySource(path)
    await src.open()
    await src.write(b"\xb5\x62")
    await src.close()


def test_find_ublox_port_prefers_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        source_mod.glob,
        "glob",
        lambda pattern: [
            "/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_GNSS_receiver-if00"
        ],
    )
    assert (
        find_ublox_port()
        == "/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_GNSS_receiver-if00"
    )


def test_find_ublox_port_falls_back_to_vid(monkeypatch: pytest.MonkeyPatch) -> None:
    class Port:
        def __init__(self, device: str, vid: int | None) -> None:
            self.device = device
            self.vid = vid

    monkeypatch.setattr(source_mod.glob, "glob", lambda pattern: [])
    monkeypatch.setattr(
        source_mod.list_ports,
        "comports",
        lambda: [Port("/dev/ttyUSB0", 0x0403), Port("/dev/ttyACM0", 0x1546)],
    )
    assert find_ublox_port() == "/dev/ttyACM0"


def test_find_ublox_port_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source_mod.glob, "glob", lambda pattern: [])
    monkeypatch.setattr(source_mod.list_ports, "comports", lambda: [])
    assert find_ublox_port() is None


async def test_replay_without_marker_yields_whole_file_once(tmp_path: Path) -> None:
    path = tmp_path / "r.ubx"
    path.write_bytes(RAWX + RAWX)
    src = FileReplaySource(path, speed=0)
    await src.open()
    assert await src.read() == RAWX + RAWX
    assert await src.read() == b""
    assert await src.read() == b""
    await src.close()
    await src.close()


async def test_replay_skips_pacing_on_rollback_and_long_gaps(tmp_path: Path) -> None:
    path = tmp_path / "r.ubx"
    path.write_bytes(pvt(3000) + pvt(1000) + pvt(2000) + pvt(90_000) + pvt(91_000))
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    src = FileReplaySource(path, speed=1.0, sleep=fake_sleep)
    await src.open()
    assert len(await read_all(src)) == 5
    assert sleeps == [1.0, 1.0]


async def test_replay_drops_trailing_partial_frame(tmp_path: Path) -> None:
    path = tmp_path / "r.ubx"
    path.write_bytes(pvt(1000) + pvt(2000)[:7])
    src = FileReplaySource(path, speed=0)
    await src.open()
    assert await read_all(src) == [pvt(1000)]


async def test_serial_source_before_open_reads_empty_and_refuses_write() -> None:
    src = SerialSource("/dev/ttyNOPE", baud=460800)
    assert src.name == "serial:/dev/ttyNOPE"
    assert await src.read() == b""
    with pytest.raises(ConnectionError):
        await src.write(b"\xb5\x62")
    await src.close()
    await src.close()


async def test_host_paced_replay_sends_the_raw_bytes_at_the_line_rate(tmp_path: Path) -> None:
    """An INS vendor stream has no UBX iTOW: `pace="host"` sends the file's bytes as they are
    (nothing framed away) in chunks, each after the time it takes on an 8N1 line at *baud*."""
    path = tmp_path / "r.sbg"
    data = bytes(range(256)) * 10  # 2,560 bytes, not one UBX frame
    path.write_bytes(data)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    src = FileReplaySource(path, speed=2.0, sleep=fake_sleep, pace="host", baud=115200)
    await src.open()
    chunks = await read_all(src)
    assert b"".join(chunks) == data
    assert [len(c) for c in chunks] == [1024, 1024, 512]
    line_s = 10 / 115200 / 2  # one byte on the wire, at speed 2
    assert sleeps == pytest.approx([1024 * line_s, 1024 * line_s, 512 * line_s])
    assert await src.read() == b""  # and stays ended


async def test_host_paced_replay_at_speed_zero_only_yields_and_loops(tmp_path: Path) -> None:
    path = tmp_path / "r.vn"
    path.write_bytes(b"\xfa" * 1500)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    src = FileReplaySource(path, speed=0, loop=True, sleep=fake_sleep, pace="host")
    await src.open()
    sizes = [len(await src.read()) for _ in range(4)]
    assert sizes == [1024, 476, 1024, 476]  # restarted at EOF
    assert sleeps == [0, 0, 0, 0]
    await src.write(b"$VNWRG")  # a replay takes no writes
    await src.close()


def test_replay_pace_must_be_itow_or_host(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pace"):
        FileReplaySource(tmp_path / "r", pace="fast")


async def test_an_exclusive_serial_source_refuses_a_port_another_holds() -> None:
    """INS ports open exclusively: a second process (`mtrtk ins` beside the daemon) is told
    the port is busy instead of splitting the stream with the first."""
    import os

    master, slave = os.openpty()
    try:
        name = os.ttyname(slave)
        first = SerialSource(name, exclusive=True)
        await first.open()
        try:
            with pytest.raises(OSError, match="in use by another process"):
                await SerialSource(name, exclusive=True).open()
        finally:
            await first.close()
        again = SerialSource(name, exclusive=True)  # released on close
        await again.open()
        await again.close()
    finally:
        os.close(master)
        os.close(slave)


# A port nobody drains (a stalled tunnel, a USB-serial adapter whose far end stopped reading):
# the write must wait for the port, not spin. Run in a child process, because the defect froze
# the whole event loop (pyserial's non-blocking write loops on EAGAIN), timeouts included.
_STALLED_PORT = """
import asyncio, os
from mtrtk.core.source import SerialSource

async def main():
    master, slave = os.openpty()
    source = SerialSource(os.ttyname(slave))
    await source.open()
    beats = 0

    async def heartbeat():
        nonlocal beats
        while True:
            await asyncio.sleep(0.01)
            beats += 1

    async def writes():  # RTCM-sized writes: one lands exactly when the kernel queue is full
        for _ in range(100_000):
            await source.write(bytes(256))

    task = asyncio.create_task(heartbeat())
    try:
        await asyncio.wait_for(writes(), 0.5)
    except TimeoutError:
        pass  # the far end never reads: the writes wait, as they should
    task.cancel()
    print("beats", beats, flush=True)
    os._exit(0)  # the unread megabyte would keep a clean close waiting

asyncio.run(main())
"""


def test_a_write_to_a_port_nobody_reads_never_freezes_the_loop() -> None:
    import subprocess
    import sys

    try:
        done = subprocess.run(
            [sys.executable, "-c", _STALLED_PORT], capture_output=True, text=True, timeout=20
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the write spun inside pyserial and the event loop never ran again")
    assert done.returncode == 0, done.stderr
    beats = int(done.stdout.split()[-1])
    assert beats >= 10, done.stdout  # the loop kept running while the write waited


def _assert_writes_without_spinning(source: SerialSource) -> None:
    """Fail at once, rather than hang the suite, when the fd-level write is not installed:
    pyserial's own would freeze the loop, and no asyncio timeout could fire."""
    port = source._writer.transport.serial  # type: ignore[union-attr]
    assert "write" in vars(port), "SerialSource.open() did not replace pyserial's write"


async def test_a_serial_write_arrives_whole_and_in_order() -> None:
    """Through the fd-level write: a write larger than the kernel queue is buffered and goes
    out as the far end reads, every byte in order."""
    import asyncio
    import os

    master, slave = os.openpty()
    os.set_blocking(master, False)
    source = SerialSource(os.ttyname(slave))
    await source.open()
    try:
        _assert_writes_without_spinning(source)
        payload = bytes(range(256)) * 1024  # 256 KiB: far more than a pty holds
        writer = asyncio.create_task(source.write(payload))
        got = bytearray()
        async with asyncio.timeout(10):
            while len(got) < len(payload):
                try:
                    got += os.read(master, 65536)
                except BlockingIOError:
                    await asyncio.sleep(0.001)
            await writer
        assert bytes(got) == payload
    finally:
        await source.close()
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("how", ["eio", "unplugged"])
async def test_a_serial_write_that_fails_ends_the_link_cleanly(
    how: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fd-level write raises a raw OSError where pyserial raised SerialException (an
    adapter pulled out: EIO). The transport must close and the caller see an error promptly:
    write raises and read fails or ends, never a hang. `eio` fails only the write; `unplugged`
    closes the pty's far end, as pulling a USB adapter does."""
    import asyncio
    import contextlib
    import errno
    import os

    master, slave = os.openpty()
    source = SerialSource(os.ttyname(slave))
    await source.open()
    try:
        _assert_writes_without_spinning(source)
        transport = source._writer.transport  # type: ignore[union-attr]
        if how == "eio":
            fd, real = transport.serial.fd, os.write

            def failing(f: int, data: bytes) -> int:
                if f == fd:
                    raise OSError(errno.EIO, "Input/output error")
                return real(f, data)

            monkeypatch.setattr(source_mod.os, "write", failing)
        else:
            os.close(master)
            master = -1
        async with asyncio.timeout(5):
            with pytest.raises(OSError):  # ConnectionResetError, or the EIO itself
                for _ in range(100):
                    await source.write(b"\xb5b" * 64)
                    await asyncio.sleep(0.01)
            assert transport.is_closing()
            with contextlib.suppress(OSError):  # the reader may report the same lost link
                assert await source.read() == b""
    finally:
        monkeypatch.undo()
        await source.close()
        if master >= 0:
            os.close(master)
        os.close(slave)
