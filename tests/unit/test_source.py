from collections.abc import Callable
from pathlib import Path

import pytest
from pyubx2 import GET, UBXMessage

from mtrtk.core import source as source_mod
from mtrtk.core.frames import Framer, Proto
from mtrtk.core.source import REPLAY_PACES, FileReplaySource, SerialSource, find_ublox_port
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


# --- replay of a file larger than the framer's 1 MiB buffer ---------------------------------
# A capture of any size replays every frame, in order, with bounded memory: the file is
# streamed through the framer, never handed to it whole (the framer keeps only its last MiB).

SEQ = (0x02, 0x13)  # an RXM-SFRBX-sized frame whose payload starts with its sequence number


def seq_frame(n: int, size: int = 900) -> bytes:
    return ubx_frame(*SEQ, n.to_bytes(4, "little") + bytes(size - 4))


def big_capture(epochs: int, marker: Callable[[int], bytes] = pvt) -> bytes:
    """`epochs` 1 Hz epochs of a sequence frame then the epoch marker: ~1 KB an epoch."""
    return b"".join(seq_frame(n) + marker(1000 * (n + 1)) for n in range(epochs))


def seqs_in(data: bytes) -> list[int]:
    frames = Framer(max_buffer=len(data) + 1).feed(data)
    return [
        int.from_bytes(f.raw[6:10], "little")
        for f in frames
        if f.proto is Proto.UBX and f.ubx_class_id == SEQ
    ]


async def test_replay_of_a_file_over_1_mib_delivers_every_frame_in_order(tmp_path: Path) -> None:
    path = tmp_path / "big.ubx"
    data = big_capture(3200)  # ~3.2 MiB
    assert len(data) > 3 << 20
    path.write_bytes(data)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    src = FileReplaySource(path, speed=4.0, sleep=fake_sleep)
    await src.open()
    chunks = await read_all(src)
    assert b"".join(chunks) == data  # every byte: nothing framed away, nothing reordered
    assert seqs_in(b"".join(chunks)) == list(range(3200))
    assert len(chunks) == 3200  # still one epoch a read, paced on receiver time as before
    assert chunks[0] == seq_frame(0) + pvt(1000)
    assert sleeps == [0.25] * 3199
    await src.close()


async def test_replay_of_a_big_nav_eoe_capture_still_paces_on_eoe(tmp_path: Path) -> None:
    path = tmp_path / "big.ubx"
    data = b"".join(pvt(1000 * (n + 1)) + seq_frame(n) + eoe(1000 * (n + 1)) for n in range(1500))
    path.write_bytes(data)
    src = FileReplaySource(path, speed=0)
    await src.open()
    chunks = await read_all(src)
    assert len(chunks) == 1500
    assert chunks[-1] == pvt(1_500_000) + seq_frame(1499) + eoe(1_500_000)
    assert b"".join(chunks) == data


async def test_replay_streams_the_file_in_bounded_memory(tmp_path: Path) -> None:
    import gc
    import tracemalloc

    path = tmp_path / "big.ubx"
    path.write_bytes(big_capture(4200))  # ~4.2 MiB
    gc.collect()
    src = FileReplaySource(path, speed=0)
    total = 0
    tracemalloc.start()
    try:
        await src.open()
        while chunk := await src.read():
            total += len(chunk)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    await src.close()
    assert total == path.stat().st_size
    assert peak < 1 << 20, f"replay held {peak} bytes for a {total}-byte file"


async def test_replay_of_a_big_file_loops_from_its_first_frame(tmp_path: Path) -> None:
    path = tmp_path / "big.ubx"
    data = big_capture(1200)
    path.write_bytes(data)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    src = FileReplaySource(path, speed=1.0, loop=True, sleep=fake_sleep)
    await src.open()
    chunks = [await src.read() for _ in range(2 * 1200 + 3)]
    assert b"".join(chunks[:1200]) == data
    assert b"".join(chunks[1200:2400]) == data  # the second pass is the whole file again
    assert chunks[2400] == chunks[0] == seq_frame(0) + pvt(1000)
    # one second an epoch; none across the restart (receiver time went back)
    assert sleeps == [1.0] * 1199 + [1.0] * 1199 + [1.0, 1.0]


async def test_a_big_replay_drops_a_truncated_last_frame_and_loops_cleanly(
    tmp_path: Path,
) -> None:
    path = tmp_path / "big.ubx"
    data = big_capture(1500)
    path.write_bytes(data + seq_frame(1500)[:500])  # the capture was cut mid-frame
    src = FileReplaySource(path, speed=0)
    await src.open()
    chunks = await read_all(src)
    assert b"".join(chunks) == data
    assert await src.read() == b""

    looping = FileReplaySource(path, speed=0, loop=True)
    await looping.open()
    chunks = [await looping.read() for _ in range(1500 + 2)]
    assert b"".join(chunks[:1500]) == data
    # the cut frame's bytes are not glued onto the start of the next pass
    assert chunks[1500] == seq_frame(0) + pvt(1000)
    assert chunks[1501] == seq_frame(1) + pvt(2000)


async def test_a_replay_that_never_sees_a_marker_still_comes_in_bounded_chunks(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nomarker.ubx"
    data = b"".join(seq_frame(n) for n in range(3000))  # ~2.7 MiB and no NAV-PVT at all
    path.write_bytes(data)
    src = FileReplaySource(path, speed=0)
    await src.open()
    chunks = await read_all(src)
    assert b"".join(chunks) == data
    assert max(len(c) for c in chunks) <= source_mod.MAX_REPLAY_CHUNK


async def test_a_host_paced_replay_of_a_big_file_sends_every_byte(tmp_path: Path) -> None:
    path = tmp_path / "big.sbg"
    data = bytes(range(256)) * (12 << 10)  # 3 MiB
    path.write_bytes(data)
    src = FileReplaySource(path, speed=0, loop=True, pace="host")
    await src.open()
    n = len(data) // source_mod.HOST_CHUNK
    chunks = [await src.read() for _ in range(n + 1)]
    assert b"".join(chunks[:n]) == data
    assert chunks[n] == data[: source_mod.HOST_CHUNK]  # looped
    await src.close()


async def test_an_empty_replay_file_ends_instead_of_spinning(tmp_path: Path) -> None:
    path = tmp_path / "empty.ubx"
    path.write_bytes(b"")
    for pace in REPLAY_PACES:
        src = FileReplaySource(path, speed=0, loop=True, pace=pace)
        await src.open()
        assert await src.read() == b""
        await src.close()


@pytest.mark.parametrize("read_size", [1, 5, 7, 11, 12, 13, 64])
async def test_the_marker_scan_finds_a_nav_eoe_cut_by_a_file_read(
    tmp_path: Path, read_size: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The NAV-EOE scan reads the file in pieces: a frame split between two reads still
    counts, and one with a bad checksum does not."""
    monkeypatch.setattr(source_mod, "REPLAY_READ", read_size)
    bad = bytearray(eoe(1000))
    bad[-1] ^= 0xFF
    path = tmp_path / "r.ubx"
    path.write_bytes(RAWX + pvt(1000) + bytes(bad) + pvt(2000))
    src = FileReplaySource(path, speed=0)
    await src.open()
    assert src._marker == source_mod.NAV_PVT
    assert await read_all(src) == [RAWX + pvt(1000), pvt(2000)]

    path.write_bytes(RAWX + pvt(1000) + eoe(1000) + pvt(2000) + eoe(2000))
    await src.open()  # reopening starts over, and finds the marker anew
    assert src._marker == source_mod.NAV_EOE
    assert await read_all(src) == [RAWX + pvt(1000) + eoe(1000), pvt(2000) + eoe(2000)]
    await src.close()


def big_log(epochs: int) -> bytes:
    """A u-blox log well over the framer's 1 MiB buffer, as an hourly log is (about 6.5 MB)."""
    filler = ubx_frame(0x02, 0x15, b"\x00" * 1000)  # an RXM-RAWX-sized frame per epoch
    return b"".join(filler + pvt(1000 * i) for i in range(1, epochs + 1))


async def test_replay_frames_a_log_bigger_than_the_framer_buffer(tmp_path: Path) -> None:
    """Framing the whole file in one feed() kept only its last 1 MiB: ~85% of an hour lost."""
    path = tmp_path / "hour.ubx"
    data = big_log(1500)
    assert len(data) > 1 << 20
    path.write_bytes(data)

    async def no_delay(delay: float) -> None:
        return None

    src = FileReplaySource(path, speed=0, sleep=no_delay)
    await src.open()
    chunks = await read_all(src)
    assert len(chunks) == 1500  # one per NAV-PVT, from the first epoch of the file
    assert chunks[0] == ubx_frame(0x02, 0x15, b"\x00" * 1000) + pvt(1000)
    assert b"".join(chunks) == data


def epoch_without_eoe(itow: int) -> bytes:
    """A pre-NAV-EOE hourly log's epoch: RAWX-sized filler, then its NAV-PVT."""
    return ubx_frame(0x02, 0x15, b"\x00" * 1000) + pvt(itow)


def epoch_with_eoe(itow: int) -> bytes:
    return pvt(itow) + ubx_frame(0x02, 0x15, b"\x00" * 1000) + eoe(itow)


async def test_a_log_joined_across_the_nav_eoe_change_is_paced_in_both_parts(
    tmp_path: Path,
) -> None:
    """Hours logged before NAV-EOE was in LOG_MESSAGES, then hours logged after it, joined
    with cat: the file holds a NAV-EOE, but its first stretch has none. That stretch is
    paced on its NAV-PVTs, and the later one on its NAV-EOEs."""
    old = b"".join(epoch_without_eoe(1000 * i) for i in range(1, 601))  # 10 min, no EOE
    new = b"".join(epoch_with_eoe(1000 * i) for i in range(601, 611))  # then 10 s with EOE
    path = tmp_path / "joined.ubx"
    path.write_bytes(old + new)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    src = FileReplaySource(path, speed=1.0, sleep=fake_sleep)
    await src.open()
    chunks = await read_all(src)
    assert b"".join(chunks) == old + new
    assert sum(sleeps) == pytest.approx(609.0)  # one second per epoch after the first
    assert max(sleeps) == pytest.approx(1.0)
    # the EOE stretch still comes one whole epoch per read, ending on its NAV-EOE
    assert chunks[-1] == pvt(610_000) + ubx_frame(0x02, 0x15, b"\x00" * 1000) + eoe(610_000)


async def test_a_log_whose_nav_eoe_stops_part_way_is_paced_after_it_too(tmp_path: Path) -> None:
    new = b"".join(epoch_with_eoe(1000 * i) for i in range(1, 11))
    old = b"".join(epoch_without_eoe(1000 * i) for i in range(11, 31))
    path = tmp_path / "joined.ubx"
    path.write_bytes(new + old)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    src = FileReplaySource(path, speed=1.0, sleep=fake_sleep)
    await src.open()
    assert b"".join(await read_all(src)) == new + old
    assert sum(sleeps) == pytest.approx(29.0)


async def test_a_paced_replay_yields_between_the_capped_chunks_of_a_markerless_run(
    tmp_path: Path,
) -> None:
    """A run with no marker comes in MAX_REPLAY_CHUNK pieces. At speed 1 too, each piece gives
    the event loop a turn: without one the receiver drained the whole run in a single step."""
    data = b"".join(seq_frame(n) for n in range(1000))  # ~0.9 MiB and no NAV-PVT at all
    path = tmp_path / "nomarker.ubx"
    path.write_bytes(data)
    calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        calls.append(delay)

    src = FileReplaySource(path, speed=1.0, sleep=fake_sleep)
    await src.open()
    chunks = await read_all(src)
    assert b"".join(chunks) == data
    assert len(chunks) > 1
    assert calls == [0] * len(chunks)  # one yield per piece, the EOF tail's included


async def test_open_reads_only_the_head_of_the_file_for_its_first_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """open() runs on the event loop, at every (re)connect: it used to read the whole file for
    a NAV-EOE, which on a Pi's SD card is seconds of a frozen web server and bus for a 1 GB
    concatenation. Only the first epoch's marker comes from it, so the head is enough."""
    read_bytes: list[int] = []
    real_open = Path.open

    def counting_open(self: Path, *args: object, **kwargs: object) -> object:
        f = real_open(self, *args, **kwargs)  # type: ignore[call-overload]
        real_read = f.read

        def read(n: int = -1) -> bytes:
            data: bytes = real_read(n)
            read_bytes.append(len(data))
            return data

        f.read = read
        return f

    path = tmp_path / "big.ubx"
    path.write_bytes(big_capture(1500))  # over 1 MiB, no NAV-EOE: the old scan read it all
    monkeypatch.setattr(Path, "open", counting_open)
    src = FileReplaySource(path, speed=0)
    await src.open()
    assert sum(read_bytes) <= source_mod.REPLAY_HEAD_SCAN < path.stat().st_size
    await src.close()


async def test_the_first_marker_comes_from_the_head_of_a_joined_log(tmp_path: Path) -> None:
    """An old log (no NAV-EOE) joined before a new one: its first epoch ends at its NAV-PVT, as
    every later one in that part does, even though the file holds NAV-EOE further on."""
    head = b"".join(pvt(1000 * (n + 1)) + seq_frame(n) for n in range(100))
    path = tmp_path / "joined.ubx"
    path.write_bytes(head + pvt(200_000) + eoe(200_000))
    assert len(head) > source_mod.REPLAY_HEAD_SCAN
    src = FileReplaySource(path, speed=0)
    await src.open()
    assert src._marker == source_mod.NAV_PVT
    assert (await src.read()) == pvt(1000)
    await src.close()


def test_the_source_ended_reason_is_spelled_once() -> None:
    """The daemon publishes a replay's last inferred epoch only when the disconnect reason is
    SOURCE_ENDED, and alerts stay quiet on it: a second spelling that drifted would drop that
    epoch from History, points and the socket with no error."""
    assert source_mod.SOURCE_ENDED == "source ended"  # the wire value the web and the tests read
    src = Path(source_mod.__file__).resolve().parents[1]
    literal = '"source ended"'
    spelled = sorted(
        p.relative_to(src).as_posix() for p in src.rglob("*.py") if literal in p.read_text()
    )
    assert spelled == ["core/source.py"]


async def test_a_long_frameless_stretch_is_scanned_in_yielding_steps(tmp_path: Path) -> None:
    """A wrong or corrupt `file:` (random bytes, a gzip of a log) has no frame for megabytes;
    framing it in one event-loop step froze the UI and API for seconds. The scan yields every
    FRAMELESS_READS_PER_YIELD reads, and still never returns b"" (SourceEnded) before EOF."""
    reads = source_mod.FRAMELESS_READS_PER_YIELD
    path = tmp_path / "r.ubx"
    path.write_bytes(b"\x00" * (3 * reads * source_mod.REPLAY_READ) + pvt(1000) + pvt(2000))
    yields = 0

    async def fake_sleep(delay: float) -> None:
        nonlocal yields
        yields += delay == 0

    src = FileReplaySource(path, sleep=fake_sleep)
    await src.open()
    try:
        first = await src.read()
        assert first == pvt(1000)
        assert yields >= 3
        assert await src.read() == pvt(2000)
        assert await src.read() == b""
    finally:
        await src.close()
