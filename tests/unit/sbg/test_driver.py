import asyncio

import pytest

from mtrtk.core.bus import Bus
from mtrtk.rover.drivers.base import DriverCapabilities, RoverDriver
from mtrtk.rover.drivers.sbg.adapter import SbgStateAdapter
from mtrtk.rover.drivers.sbg.driver import SbgDriver

from .helpers import frame, gps_pos, gps_raw, rtcm3, sat, sat_list

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
    nmea = b"$GNGGA,,,,,,0,00,,,M,,M,,*66\r\n" * 700
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
