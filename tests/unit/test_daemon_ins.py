"""The daemon with an INS rover driver: the SBG / VectorNav stack instead of the u-blox one."""

import asyncio
import functools
import json
import struct
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Proto
from mtrtk.core.state import Attitude, InsStatus
from mtrtk.daemon import Daemon, StatusPrinter
from mtrtk.rawlog.index import list_logs
from mtrtk.rover.drivers.factory import StoreFacade, build_ins
from mtrtk.rover.drivers.sbg.framer import SbgFramer, encode
from mtrtk.rover.drivers.sbg.ids import CMD, LOG
from sbgdevice import GET_SELECTOR_LEN
from ubxtest import ubx_frame

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ins"
TEST_TIMEOUT_S = 60.0


def bounded(test: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    @functools.wraps(test)
    async def run(*args: Any, **kwargs: Any) -> None:
        async with asyncio.timeout(TEST_TIMEOUT_S):
            await test(*args, **kwargs)

    return run


async def _wait_for(predicate: Callable[[], object], timeout_s: float = 10.0) -> None:
    for _ in range(int(timeout_s / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition not met in {timeout_s}s")


def sbg_fixture() -> bytes:
    lines = (FIXTURES / "sbg_frames.hex").read_text().splitlines()
    return bytes.fromhex("".join(ln.strip() for ln in lines if ln.strip()))


def vn_fixture() -> bytes:
    lines = (FIXTURES / "vn_frames.hex").read_text().splitlines()
    return bytes.fromhex("".join(ln.strip() for ln in lines if ln and not ln.startswith("#")))


class ScriptedSource:
    """Serves *chunks*, then holds the link open (a live unit) until `close()`.

    Writes (the on-connect configure's commands) are recorded and never answered.
    """

    name = "scripted"
    ends_at_eof = False

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self.chunks = list(chunks)
        self.written: list[bytes] = []
        self._closed = asyncio.Event()

    async def open(self) -> None:
        return None

    async def read(self) -> bytes:
        await asyncio.sleep(0.01)
        if self.chunks:
            return self.chunks.pop(0)
        await self._closed.wait()
        return b""

    async def write(self, data: bytes) -> None:
        self.written.append(data)

    async def close(self) -> None:
        self._closed.set()


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "role": "rover",
        "rover_driver": "sbg_ellipse",
        "ins_port": "/dev/null",
        "data_dir": tmp_path,
        "nmea_tcp_port": -1,
        "web_bind": "127.0.0.1",
        "web_port": 0,
        "web_allow_insecure": True,
        "ntrip_password": "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


async def _run(daemon: Daemon, until: Callable[[], object]) -> None:
    task = asyncio.create_task(daemon.run())
    try:
        await _wait_for(until)
    finally:
        daemon._request_stop("test")
        await task


@bounded
async def test_daemon_builds_ins_bundle_for_sbg(tmp_path: Path) -> None:
    source = ScriptedSource([sbg_fixture()])
    daemon = Daemon(_settings(tmp_path), source_factory=lambda: source)
    assert daemon.controller is None
    assert daemon.ins is not None and daemon.ins.vendor == "sbg"
    assert isinstance(daemon.store, StoreFacade)
    seen: dict[str, Any] = {}

    def ready() -> bool:
        if daemon.rover is not None:
            seen["driver"] = daemon.rover.driver.name
        return daemon.store.state.position.lat is not None and "driver" in seen

    await _run(daemon, ready)
    s = daemon.store.state
    assert s.position.lat == pytest.approx(23.7275)
    assert s.ins is not None and s.ins.vendor == "sbg"
    assert seen["driver"] == "sbg_ellipse"
    # The on-connect configure ran read-only (INS_APPLY_CONFIG=0): every frame it sent is a GET
    # (a selector, no settings payload). The source never answers, so it stops at INFO.
    sent = list(SbgFramer().feed(b"".join(source.written)))
    assert [f.raw[2] for f in sent][:1] == [CMD["INFO"]]
    assert all(len(f.payload) == GET_SELECTOR_LEN.get(f.raw[2], 0) for f in sent)


@bounded
async def test_ntrip_client_skipped_when_driver_rejects_rtcm(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        rover_driver="vectornav",
        ntrip_url="ntrip://u:p@127.0.0.1:9/MTRK",
        ins_vn_rtcm=False,
    )
    daemon = Daemon(settings, source_factory=lambda: ScriptedSource([vn_fixture()]))
    events = daemon.bus.subscribe("events.new")
    seen: dict[str, Any] = {}

    def ready() -> bool:
        if daemon.rover is not None:
            seen["client"] = daemon.rover.ntrip_client
            seen["accepts"] = daemon.rover.driver.capabilities.accepts_rtcm
        return "client" in seen and not events.queue.empty()

    await _run(daemon, ready)
    assert seen == {"client": None, "accepts": False}
    _, event = events.queue.get_nowait()
    assert event.level == "info" and "corrections not supported by driver" in event.message


@bounded
async def test_ubx_reframed_frames_reach_raw_logger(tmp_path: Path) -> None:
    """GPS1_RAW carries the internal receiver's UBX: re-framed, it lands in the raw log, named
    by the unit's UTC (UTC_TIME), since the stream has no NAV-PVT of its own."""
    rawx = ubx_frame(0x02, 0x15, struct.pack("<dHbBBB2x", 37815.25, 2437, 18, 0, 1, 1))
    chunks = [rawx[:10], rawx[10:]] * 3  # the unit cuts the stream without regard to frames
    stream = sbg_fixture() + b"".join(encode(0, LOG["GPS1_RAW"], c) for c in chunks)
    daemon = Daemon(_settings(tmp_path), source_factory=lambda: ScriptedSource([stream]))
    raw = daemon.bus.subscribe("raw.ubx")
    await _run(daemon, lambda: raw.queue.qsize() >= 3 and daemon.rawlog is not None)
    _, frame = raw.queue.get_nowait()
    assert frame.proto is Proto.UBX and frame.identity == "RXM-RAWX"
    logs = list_logs(tmp_path)
    assert [lf.path.name for lf in logs] == ["MTRK_20260919_10.ubx"]
    assert logs[0].path.read_bytes() == rawx * 3


async def test_bundle_feeds_the_raw_writer_clock_from_utc_time(tmp_path: Path) -> None:
    bus = Bus()
    notes: list[datetime] = []

    class Clock:
        def note_utc(self, dt: datetime) -> None:
            notes.append(dt)

    bundle = build_ins(
        _settings(tmp_path), bus, source_factory=lambda: ScriptedSource([]), raw_writer=Clock()
    )
    facade = StoreFacade(bundle.adapter)
    for frame in bundle.controller.framer_factory().feed(sbg_fixture()):
        facade.apply(frame)
    assert notes == [datetime(2026, 9, 19, 10, 30, 15, 250000, tzinfo=UTC)]
    assert facade.state is bundle.adapter.state and facade.state.attitude is not None
    facade.note_rtcm_injected(12.0)
    assert bundle.adapter.state.rtk.last_rtcm_mono == 12.0


def test_status_line_carries_ins_mode_and_heading(tmp_path: Path) -> None:
    bundle = build_ins(_settings(tmp_path), Bus(), source_factory=lambda: ScriptedSource([]))
    store = StoreFacade(bundle.adapter)
    printer = StatusPrinter(Bus(), store, echo=lambda _: None)
    assert " ins " not in printer.format_line()
    store.state.ins = InsStatus(vendor="sbg", mode=4, mode_name="Nav position")
    store.state.attitude = Attitude(heading_deg=91.25, source="sbg-ekf")
    assert printer.format_line().endswith("ins Nav position att 91.2°")


def test_vectornav_bundle_and_settings_validation(tmp_path: Path) -> None:
    bundle = build_ins(
        _settings(tmp_path, rover_driver="vectornav", ins_vn_rtcm=True),
        Bus(),
        source_factory=lambda: ScriptedSource([]),
    )
    assert bundle.vendor == "vectornav" and bundle.raw_topic == "raw.vn"
    assert bundle.driver.capabilities.accepts_rtcm is True
    assert bundle.raw_capture is not None and bundle.raw_capture.suffix == "vnraw"
    with pytest.raises(ValueError, match="not an INS driver"):
        build_ins(
            _settings(tmp_path, rover_driver="ublox"),
            Bus(),
            source_factory=lambda: ScriptedSource([]),
        )


class PortB:
    """The SBG Port B RTCM device: fails its first *fail_opens* opens, then each write while
    `fail_writes` is set."""

    name = "serial:/dev/portb"
    ends_at_eof = False

    def __init__(self, fail_opens: int = 0) -> None:
        self.fail_opens = fail_opens
        self.fail_writes = False
        self.opens = 0
        self.closes = 0
        self.written: list[bytes] = []

    async def open(self) -> None:
        if self.fail_opens:
            self.fail_opens -= 1
            raise OSError("no such device")
        self.opens += 1

    async def close(self) -> None:
        self.closes += 1

    async def read(self) -> bytes:
        return b""

    async def write(self, data: bytes) -> None:
        if self.fail_writes:
            raise OSError("write failed: device gone")
        self.written.append(data)


async def test_port_b_is_held_open_reported_once_and_reopened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mtrtk.rover.drivers import factory

    monkeypatch.setattr(factory, "PORT_B_BACKOFF_S", (0.01, 0.02))
    monkeypatch.setattr(factory, "PORT_B_CHECK_S", 0.01)
    port = PortB(fail_opens=2)
    monkeypatch.setattr(factory, "SerialSource", lambda name, baud: port)
    bus = Bus()
    errors = bus.subscribe("receiver.error")
    bundle = build_ins(
        _settings(tmp_path, ins_rtcm_port="/dev/portb"),
        bus,
        source_factory=lambda: ScriptedSource([]),
        capture=False,
    )
    driver = bundle.driver
    assert isinstance(driver, factory.SbgDriver)
    stop = asyncio.Event()
    task = asyncio.create_task(factory.hold_port_b(port, driver, bus, stop))
    await asyncio.sleep(0)
    # RTCM before the first open lands is dropped quietly: the holder reports the open failure.
    await driver.inject_rtcm(b"\xd3\x00\x01")
    assert driver.dropped_bytes == 3 and not driver.port_b_failing
    await _wait_for(lambda: port.opens == 1)
    assert errors.queue.qsize() == 1  # two failed opens, one report
    errors.queue.get_nowait()
    await driver.inject_rtcm(b"\xd3\x00")
    assert port.written == [b"\xd3\x00"]
    port.fail_writes = True
    await driver.inject_rtcm(b"\xd3\x00")  # the device went away: reported, then reopened
    assert driver.port_b_failing and errors.queue.qsize() == 1
    port.fail_writes = False
    await _wait_for(lambda: port.opens == 2)
    # The reopen ends the outage: no RTCM flowing (NTRIP idle) must not reopen it every check.
    assert not driver.port_b_failing and driver.port_b_ready
    await asyncio.sleep(0.1)
    assert port.opens == 2
    stop.set()
    await task
    assert port.closes == port.opens  # every open is closed, the last one on stop
    assert driver.port_b_ready is False


def test_sbg_bundle_with_port_b_has_the_holder_task(tmp_path: Path) -> None:
    bundle = build_ins(
        _settings(tmp_path, ins_rtcm_port="/dev/ttyUSB9"),
        Bus(),
        source_factory=lambda: ScriptedSource([]),
    )
    assert len(bundle.extra_tasks) == 1
    assert bundle.driver.rtcm_source is not None  # type: ignore[union-attr]


@bounded
async def test_daemon_runs_the_port_b_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With INS_RTCM_PORT set the daemon starts the bundle's extra task: Port B is opened while
    it runs and closed when it stops."""
    from mtrtk.rover.drivers import factory

    port = PortB()
    monkeypatch.setattr(factory, "SerialSource", lambda name, baud: port)
    daemon = Daemon(
        _settings(tmp_path, ins_rtcm_port="/dev/portb"),
        source_factory=lambda: ScriptedSource([sbg_fixture()]),
    )
    await _run(daemon, lambda: port.opens == 1)
    assert port.closes == 1


@bounded
async def test_daemon_closes_the_opaque_raw_capture_on_exit(tmp_path: Path) -> None:
    """INS_RAW_GNSS on a GPS1_RAW stream that is not UBX goes to the opaque capture; the daemon
    closes it on the way out, so the open hour's sidecar gets its end time."""
    junk = bytes(range(16)) * 64  # 1 kB with no UBX sync
    stream = sbg_fixture() + b"".join(encode(0, LOG["GPS1_RAW"], junk) for _ in range(9))
    stream += sbg_fixture()  # a UTC_TIME after the switch names the hour
    daemon = Daemon(
        _settings(tmp_path, ins_raw_gnss=True), source_factory=lambda: ScriptedSource([stream])
    )
    assert daemon.ins is not None and daemon.ins.raw_capture is not None
    capture = daemon.ins.raw_capture
    await _run(daemon, lambda: capture.current_path is not None)
    sidecars = list((tmp_path / "ins").rglob("*.json"))
    assert len(sidecars) == 1
    side = json.loads(sidecars[0].read_text())
    assert side["end_utc"] is not None and side["bytes"] >= 9 * len(junk)
