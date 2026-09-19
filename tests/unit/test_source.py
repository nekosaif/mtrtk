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
