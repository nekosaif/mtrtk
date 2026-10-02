import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest

from mtrtk.core.bus import Bus
from mtrtk.rover.drivers.base import DriverCapabilities, RoverDriver
from mtrtk.rover.drivers.sbg.adapter import SbgStateAdapter
from mtrtk.rover.drivers.sbg.driver import SbgDriver

from .helpers import frame, gps_pos, gps_raw, items, rtcm3, sat, sat_list

RTCM = b"\xd3\x00\x13" + bytes(19) + b"\x00\x00\x00"


class FakeController:
    def __init__(self, *, connected: bool = True, fail: BaseException | None = None) -> None:
        self.connected = connected
        self.fail = fail
        self.written: list[bytes] = []

    async def write(self, data: bytes) -> None:
        if not self.connected:
            raise ConnectionError("INS not connected")
        if self.fail is not None:
            raise self.fail
        self.written.append(data)


class FakeRtcmPort:
    name = "serial:/dev/fake-port-b"
    ends_at_eof = False

    def __init__(self, *, fail: BaseException | None = None, stall: bool = False) -> None:
        self.fail = fail
        self.stall = stall
        self.written: list[bytes] = []

    async def open(self) -> None:
        pass

    async def read(self) -> bytes:
        return b""

    async def write(self, data: bytes) -> None:
        if self.stall:
            await asyncio.sleep(3600)
        if self.fail is not None:
            raise self.fail
        self.written.append(data)

    async def close(self) -> None:
        pass


def make(
    controller: FakeController | None = None, rtcm: FakeRtcmPort | None = None
) -> tuple[SbgDriver, SbgStateAdapter, FakeController]:
    ctl = controller if controller is not None else FakeController()
    adapter = SbgStateAdapter(Bus())
    return SbgDriver(ctl, adapter, rtcm_source=rtcm), adapter, ctl


async def test_inject_rtcm_prefers_separate_port() -> None:
    port = FakeRtcmPort()
    driver, adapter, ctl = make(rtcm=port)
    await driver.inject_rtcm(RTCM)
    assert port.written == [RTCM] and ctl.written == []
    assert adapter.state.rtk.last_rtcm_mono is not None and driver.dropped_bytes == 0


async def test_inject_rtcm_same_port_when_no_rtcm_source() -> None:
    driver, adapter, ctl = make()
    await driver.inject_rtcm(RTCM)
    assert ctl.written == [RTCM] and adapter.state.rtk.last_rtcm_mono is not None
    ctl.connected = False
    await driver.inject_rtcm(RTCM)
    await driver.inject_rtcm(RTCM)
    assert ctl.written == [RTCM] and driver.dropped_bytes == 2 * len(RTCM)


@pytest.mark.parametrize("exc", [ConnectionError("gone"), OSError(5, "I/O error")])
async def test_inject_rtcm_link_failures_are_dropped_not_raised(exc: OSError) -> None:
    driver, adapter, _ = make(FakeController(fail=exc))
    await driver.inject_rtcm(RTCM)
    assert driver.dropped_bytes == len(RTCM) and adapter.state.rtk.last_rtcm_mono is None
    port_driver, port_adapter, _ = make(rtcm=FakeRtcmPort(fail=exc))
    await port_driver.inject_rtcm(RTCM)
    assert port_driver.dropped_bytes == len(RTCM) and port_adapter.state.rtk.last_rtcm_mono is None


async def test_inject_rtcm_stalled_port_b_is_bounded() -> None:
    driver, adapter, _ = make(rtcm=FakeRtcmPort(stall=True))
    driver.write_timeout_s = 0.05
    await asyncio.wait_for(driver.inject_rtcm(RTCM), 2.0)
    assert driver.dropped_bytes == len(RTCM) and adapter.state.rtk.last_rtcm_mono is None


def test_capabilities() -> None:
    driver, adapter, _ = make()
    caps = driver.capabilities
    assert isinstance(caps, DriverCapabilities) and driver.name == "sbg_ellipse"
    assert caps.accepts_rtcm and caps.attitude and caps.imu and not caps.spectrum
    assert caps.raw_gnss_log and not caps.sats and driver.info is None
    adapter.handle(sat_list(sat(5, 1, [(14, 5, 40)], used=True)))
    assert driver.capabilities.sats
    nmea = b"$GNGGA,,,,,,0,00,,,M,,M,,*78\r\n" * 700
    for i in range(0, len(nmea), 2000):
        adapter.handle(gps_raw(nmea[i : i + 2000]))
    assert adapter.raw_gnss_format == "unknown" and not driver.capabilities.raw_gnss_log
    off, _, _ = make()
    off.adapter.raw_gnss = False
    assert not off.capabilities.raw_gnss_log


def test_rtcm_unverified_until_the_unit_reports_rtk() -> None:
    driver, adapter, _ = make()
    assert driver.rtcm_unverified
    adapter.handle(gps_pos(3))  # PSRDIFF/SBAS: says nothing about our RTCM
    assert driver.rtcm_unverified
    adapter.handle(gps_pos(6))
    assert not driver.rtcm_unverified


def test_rtcm_unverified_cleared_by_the_units_rtcm_echo() -> None:
    driver, adapter, _ = make()
    adapter.handle(frame("RTCM_RAW", rtcm3(1005)))  # the unit received the corrections
    assert not driver.rtcm_unverified and not adapter.rtk_seen


def test_driver_satisfies_rover_driver_protocol() -> None:
    driver, _, _ = make()
    as_protocol: RoverDriver = driver
    assert as_protocol.capabilities.accepts_rtcm


class GatedRtcmPort(FakeRtcmPort):
    """A Port B whose write yields to the loop between its start and its end."""

    def __init__(self) -> None:
        super().__init__()
        self.log: list[tuple[str, bytes]] = []
        self.gate = asyncio.Event()

    async def write(self, data: bytes) -> None:
        self.log.append(("start", data))
        await self.gate.wait()
        self.log.append(("end", data))


async def test_port_b_writes_are_whole_and_in_order() -> None:
    port = GatedRtcmPort()
    driver, _, _ = make(rtcm=port)
    a, b = rtcm3(1005), rtcm3(1077, 60)
    both = asyncio.gather(driver.inject_rtcm(a), driver.inject_rtcm(b))
    for _ in range(5):
        await asyncio.sleep(0)
    assert port.log == [("start", a)]  # the second write waits for the first to finish
    port.gate.set()
    await both
    assert port.log == [("start", a), ("end", a), ("start", b), ("end", b)]


async def test_port_b_outage_is_reported_once(caplog: pytest.LogCaptureFixture) -> None:
    port = FakeRtcmPort(fail=OSError(5, "I/O error"))
    driver, adapter, _ = make(rtcm=port)
    sub = adapter.bus.subscribe("receiver.error")
    with caplog.at_level(logging.WARNING, logger="mtrtk.rover.drivers.sbg.driver"):
        for _ in range(3):
            await driver.inject_rtcm(RTCM)
        errors = items(sub)
        assert len(errors) == 1 and "fake-port-b" in errors[0][1]
        assert len(caplog.records) == 1 and driver.dropped_bytes == 3 * len(RTCM)
        port.fail = None
        await driver.inject_rtcm(RTCM)  # back: the next failure is a new outage
        port.fail = OSError(5, "I/O error")
        await driver.inject_rtcm(RTCM)
        assert len(items(sub)) == 1 and len(caplog.records) == 2


class FlakyPort(FakeRtcmPort):
    """A Port B device that fails its first *fail_opens* opens."""

    def __init__(self, fail_opens: int = 0) -> None:
        super().__init__()
        self.fail_opens = fail_opens
        self.opens = 0

    async def open(self) -> None:
        if self.fail_opens:
            self.fail_opens -= 1
            raise OSError("no such device")
        self.opens += 1


def _by_topic(sub: Any) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for topic, item in items(sub):
        out.setdefault(topic, []).append(item)
    return out


async def test_port_b_recovery_after_a_write_failure_is_published_once() -> None:
    """The edge `AlertEngine` clears the outage's `receiver_error` on: the main port stays up,
    so no `receiver.connected` comes to clear it."""
    port = FakeRtcmPort()
    driver, adapter, _ = make(rtcm=port)
    sub = adapter.bus.subscribe("receiver.error", "receiver.recovered")
    await driver.inject_rtcm(RTCM)  # no outage before it: a working write recovers nothing
    assert items(sub) == []
    port.fail = OSError(5, "I/O error")
    await driver.inject_rtcm(RTCM)
    await driver.inject_rtcm(RTCM)
    port.fail = None
    await driver.inject_rtcm(RTCM)
    await driver.inject_rtcm(RTCM)
    got = _by_topic(sub)
    (error,) = got["receiver.error"]
    (back,) = got["receiver.recovered"]
    assert back == {"source": port.name, "message": f"RTCM to {port.name} restored (Port B)"}
    assert back["source"] in error and not driver.port_b_failing
    port.fail = OSError(5, "I/O error")
    await driver.inject_rtcm(RTCM)
    port.fail = None
    await driver.inject_rtcm(RTCM)  # a second outage, a second recovery
    assert [len(v) for v in _by_topic(sub).values()] == [1, 1]


async def test_main_port_rtcm_never_publishes_a_port_b_recovery() -> None:
    driver, adapter, ctl = make(FakeController(fail=ConnectionError("gone")))
    sub = adapter.bus.subscribe("receiver.recovered")
    await driver.inject_rtcm(RTCM)
    ctl.fail = None
    await driver.inject_rtcm(RTCM)
    assert items(sub) == []  # the controller's own connect / disconnect cover the main port


async def test_port_b_holder_publishes_the_recovery_of_a_reported_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mtrtk.rover.drivers import factory

    monkeypatch.setattr(factory, "PORT_B_BACKOFF_S", (0.01, 0.02))
    monkeypatch.setattr(factory, "PORT_B_CHECK_S", 0.01)
    port = FlakyPort(fail_opens=2)
    driver, adapter, _ = make(rtcm=port)
    bus = adapter.bus
    sub = bus.subscribe("receiver.error", "receiver.recovered")
    stop = asyncio.Event()
    task = asyncio.create_task(factory.hold_port_b(port, driver, bus, stop))
    try:
        await until(lambda: port.opens == 1)
        got = _by_topic(sub)
        assert len(got["receiver.error"]) == 1  # two failed opens, one report
        assert got["receiver.recovered"] == [
            {"source": port.name, "message": f"RTCM to {port.name} restored (Port B)"}
        ]
        port.fail = OSError(5, "I/O error")  # unplugged: reported, closed, reopened
        await driver.inject_rtcm(RTCM)
        port.fail = None
        await until(lambda: port.opens == 2)
        await driver.inject_rtcm(RTCM)  # the reopen ended the outage: no second recovery
        got = _by_topic(sub)
        assert len(got["receiver.error"]) == 1 and len(got["receiver.recovered"]) == 1
    finally:
        stop.set()
        await task


async def test_port_b_holder_first_open_is_no_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    from mtrtk.rover.drivers import factory

    monkeypatch.setattr(factory, "PORT_B_CHECK_S", 0.01)
    port = FlakyPort()
    driver, adapter, _ = make(rtcm=port)
    sub = adapter.bus.subscribe("receiver.error", "receiver.recovered")
    stop = asyncio.Event()
    task = asyncio.create_task(factory.hold_port_b(port, driver, adapter.bus, stop))
    try:
        await until(lambda: driver.port_b_ready is True)
        await driver.inject_rtcm(RTCM)
        assert items(sub) == []
    finally:
        stop.set()
        await task


async def until(pred: Callable[[], bool], timeout_s: float = 5.0) -> None:
    for _ in range(int(timeout_s / 0.005)):
        if pred():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"condition not met in {timeout_s}s")
