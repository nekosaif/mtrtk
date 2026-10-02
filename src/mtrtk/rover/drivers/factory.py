"""The INS rover stack for `ROVER_DRIVER=sbg_ellipse | vectornav`, built in one place.

`build_ins()` wires the vendor's framer, state adapter, driver and configuration into an
`InsController` on `INS_PORT` and returns an `InsBundle`: what the daemon runs, what the web
API and `mtrtk ins` act through (configure, reset, the identity and configuration report), and
the extra tasks the stack needs beside the controller (the SBG Port B RTCM device).

- SBG Ellipse-D: `SbgFramer`, `SbgStateAdapter` (GPS1_RAW re-framed as UBX onto `raw.ubx`, so
  `RawLogWriter` logs it; the raw logger's clock follows the unit's UTC through `raw_writer`;
  a stream that is not UBX goes to an opaque `RawCapture` "sbgraw"), `SbgDriver` (RTCM on
  `INS_RTCM_PORT` when set, else on the main port) and `sbg.config.configure`.
- VectorNav VN-200: `VnFramer`, `VnStateAdapter` (RawMeas to `RawCapture` "vnraw"),
  `VnDriver(rtcm_enabled=INS_VN_RTCM)` and `vectornav.config.configure`.

On every connect the controller runs `configure(apply=INS_APPLY_CONFIG)`: read-only by default,
it still reads the unit's identity and configuration for the UI. One `configure` runs at a
time per bundle, so the on-connect pass and an apply from the API never interleave.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.exposure import sleep_or_stop
from mtrtk.core.frames import FrameSplitter
from mtrtk.core.router import TOPIC_RAW_SBG, TOPIC_RAW_VN
from mtrtk.core.source import ByteSource, FileReplaySource, SerialSource
from mtrtk.core.statestore import StateStore
from mtrtk.rover.drivers.ins_common import InsController, RawCapture, StateAdapter
from mtrtk.rover.drivers.ins_report import jsonable, report_dict
from mtrtk.rover.drivers.sbg import config as sbg_config
from mtrtk.rover.drivers.sbg.adapter import SbgStateAdapter
from mtrtk.rover.drivers.sbg.commands import REBOOT_ONLY, SbgCommandError, SbgCommands
from mtrtk.rover.drivers.sbg.driver import SbgDriver
from mtrtk.rover.drivers.sbg.framer import SbgFramer
from mtrtk.rover.drivers.vectornav import config as vn_config
from mtrtk.rover.drivers.vectornav.adapter import VnStateAdapter
from mtrtk.rover.drivers.vectornav.driver import VnDriver
from mtrtk.rover.drivers.vectornav.framer import VnFramer
from mtrtk.rover.drivers.vectornav.registers import VnRegisters

log = logging.getLogger(__name__)

VENDOR_SBG = "sbg"
VENDOR_VN = "vectornav"
PORT_B_BACKOFF_S = (1.0, 30.0)
PORT_B_CHECK_S = 5.0  # how often a failing Port B device is looked at again

ExtraTask = Callable[[asyncio.Event], Coroutine[Any, Any, None]]


class UtcSink(Protocol):
    """The raw logger's clock (`RawLogWriter.note_utc`), or something that forwards to it."""

    def note_utc(self, dt: datetime) -> None: ...


class StoreFacade(StateStore):
    """`daemon.store` on an INS rover: the adapter's live `ReceiverState` under the name every
    consumer (web, WebSocket, sampler, NMEA, JSON, points, alerts) already reads.

    The adapter publishes its own sections; `apply` only forwards a frame to it."""

    def __init__(self, adapter: StateAdapter) -> None:
        super().__init__(None)
        self.adapter = adapter
        self.state = adapter.state

    def apply(self, frame: Any, now_mono: float | None = None) -> set[str]:
        self.adapter.handle(frame)
        return set()

    def note_rtcm_injected(self, now_mono: float | None = None) -> None:
        self.adapter.note_rtcm_injected(now_mono)


@dataclass
class InsBundle:
    """One INS rover stack and the operations the API and the CLI perform on it."""

    vendor: str  # VENDOR_SBG | VENDOR_VN
    settings: Settings
    controller: InsController
    adapter: SbgStateAdapter | VnStateAdapter
    driver: SbgDriver | VnDriver
    raw_topic: str  # the bus topic the vendor's frames are routed on
    raw_capture: RawCapture | None = None
    extra_tasks: list[ExtraTask] = field(default_factory=list)
    _configure: Callable[[bool], Awaitable[Any]] | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def connected(self) -> bool:
        return self.controller.connected

    async def configure(self, *, apply: bool) -> Any:
        """Read (and with *apply*, write and verify) the vendor profile; returns the report.

        `ConnectionError` when the link drops; every unit-side refusal is in the report."""
        assert self._configure is not None
        async with self._lock:
            return await self._configure(apply)

    async def reset(self) -> bool:
        """Restart the unit (no settings change): SBG `SETTINGS_ACTION REBOOT_ONLY`, VectorNav
        `$VNRST`. The link drops and the controller reconnects.

        Sent once, never resent: a resend would hit a unit that is already rebooting. Returns
        False when the command went out but its reply did not come back (the reboot can swallow
        the SBG ACK); a VectorNav that does not answer raises `TimeoutError`."""
        async with self._lock:
            if self.vendor == VENDOR_SBG:
                try:
                    await SbgCommands(self.controller).settings_action(REBOOT_ONLY)
                except SbgCommandError as exc:
                    if exc.code is None:  # sent, no ACK: the unit may already be rebooting
                        return False
                    raise
            else:
                await VnRegisters(self.controller).command("RST", retries=1)
            return True

    def info_dict(self) -> dict[str, Any] | None:
        """The unit's identity in one shape for both vendors, its own fields under `details`."""
        info = self.driver.info
        if info is None:
            return None
        if self.vendor == VENDOR_SBG:  # SbgInfo
            common = {
                "model": getattr(info, "product_code", ""),
                "serial": str(getattr(info, "serial_number", "")),
                "firmware": getattr(info, "firmware", ""),
                "hardware": getattr(info, "hardware_rev", ""),
            }
        else:
            common = {
                "model": getattr(info, "model", ""),
                "serial": getattr(info, "serial", ""),
                "firmware": getattr(info, "firmware", ""),
                "hardware": getattr(info, "hardware_revision", ""),
            }
        return {**common, "details": jsonable(info)}

    def report_dict(self) -> dict[str, Any] | None:
        return report_dict(self.driver.config_report)

    def lever_arms(self) -> list[dict[str, Any]]:
        """Each lever arm as configured (`INS_*`) next to what the unit last read back."""
        s = self.settings
        report = self.driver.config_report
        current: dict[str, Any] = getattr(report, "current", None) or {}

        def read(item: str, attr: str | None) -> Any:
            value = current.get(item)
            if value is None:
                return None
            return jsonable(getattr(value, attr) if attr else value)

        if self.vendor == VENDOR_SBG:
            rows = [
                ("gnss1", s.ins_lever_arm_gnss1, read("gnss_installation", "lever_arm_primary")),
                ("gnss2", s.ins_lever_arm_gnss2, read("gnss_installation", "lever_arm_secondary")),
                ("imu", s.ins_imu_lever_arm, read("imu_alignment", "lever_arm")),
            ]
        else:
            rows = [("gnss1", s.ins_lever_arm_gnss1, read("antenna_offset", None))]
        return [
            {"name": name, "configured": jsonable(conf), "read_back": back}
            for name, conf, back in rows
        ]

    def as_dict(self) -> dict[str, Any]:
        """`GET /api/receiver`'s `ins` block."""
        state = self.adapter.state
        return {
            "vendor": self.vendor,
            "driver": self.driver.name,
            "connected": self.connected,
            "port": self.settings.ins_port,
            "apply_config": self.settings.ins_apply_config,
            # Settings go to flash at most once per process: after that an apply lasts until
            # the unit restarts (the UI's confirm says so).
            "saved_this_run": bool(self.driver.saved_this_run),
            "info": self.info_dict(),
            "config_report": self.report_dict(),
            "lever_arms": self.lever_arms(),
            "status": state.ins.model_dump(mode="json") if state.ins is not None else None,
            "rtcm_unverified": self.driver.rtcm_unverified,
            "dropped_rtcm_bytes": self.driver.dropped_bytes,
            "raw_gnss_format": getattr(self.adapter, "raw_gnss_format", None),
            "stats": dict(self.controller.stats),
        }

    def close(self) -> None:
        if self.raw_capture is not None:
            self.raw_capture.close()


def build_ins(
    settings: Settings,
    bus: Bus,
    *,
    source_factory: Callable[[], ByteSource] | None = None,
    raw_writer: UtcSink | None = None,
    configure_on_connect: bool = True,
    capture: bool = True,
) -> InsBundle:
    """The INS stack for `settings.rover_driver` (`sbg_ellipse` or `vectornav`).

    *source_factory* replaces the serial port on `INS_PORT` (tests, a read-only wrapper);
    *raw_writer* is the raw logger's clock (SBG only); *configure_on_connect* False leaves the
    unit alone on connect (`mtrtk ins` runs configure itself); *capture* False writes no raw
    capture files.

    With `MTRTK_SOURCE=file:<capture>` (and no *source_factory*) the capture is replayed instead
    of `INS_PORT`, paced on host time at `INS_BAUD` (`FileReplaySource(pace="host")`). A replay
    is passive, as for a u-blox one: nothing is configured on connect, `INS_RTCM_PORT` is not
    opened, and raw captures are written only with `REPLAY_LOG=1`.
    """
    port = settings.ins_port
    replay = source_factory is None and settings.source_is_file
    if replay:
        path, baud = settings.source_path, settings.ins_baud
        speed, loop = settings.replay_speed, settings.replay_loop
        source_factory = lambda: FileReplaySource(  # noqa: E731
            path, speed=speed, loop=loop, pace="host", baud=baud
        )
        configure_on_connect = False
        capture = capture and settings.replay_log
    elif source_factory is None:
        if port is None:
            raise ValueError(f"INS_PORT is required for ROVER_DRIVER={settings.rover_driver}")
        baud = settings.ins_baud
        # Exclusive: `mtrtk ins` and the daemon must never share the unit's port.
        source_factory = lambda: SerialSource(port, baud, exclusive=True)  # noqa: E731
    holder: list[InsBundle] = []

    async def on_connect(_controller: InsController) -> None:
        await holder[0].configure(apply=settings.ins_apply_config)

    hook = on_connect if configure_on_connect else None
    nav_hz_cap = float(settings.rover_nav_hz)
    framer_factory: Callable[[], FrameSplitter]
    if settings.rover_driver == "sbg_ellipse":
        framer_factory = SbgFramer
        controller = InsController(bus, source_factory, framer_factory, hook)
        sbg_capture = (
            RawCapture(settings.data_dir, settings.station_id, "sbgraw", bus, vendor=VENDOR_SBG)
            if capture and settings.ins_raw_gnss
            else None
        )
        sbg_adapter = SbgStateAdapter(
            bus,
            nav_hz_cap=nav_hz_cap,
            raw_writer=raw_writer,
            raw_capture=sbg_capture,
            raw_gnss=settings.ins_raw_gnss,
        )
        rtcm_source = (
            SerialSource(settings.ins_rtcm_port, settings.ins_rtcm_baud_or_main, exclusive=True)
            if settings.ins_rtcm_port and not replay
            else None
        )
        sbg_driver = SbgDriver(controller, sbg_adapter, rtcm_source=rtcm_source)

        async def sbg_configure(apply: bool) -> Any:
            return await sbg_config.configure(controller, sbg_driver, settings, apply=apply)

        extra: list[ExtraTask] = []
        if rtcm_source is not None:
            port_b = rtcm_source

            async def hold(stop: asyncio.Event) -> None:
                await hold_port_b(port_b, sbg_driver, bus, stop)

            extra.append(hold)
        bundle = InsBundle(
            vendor=VENDOR_SBG,
            settings=settings,
            controller=controller,
            adapter=sbg_adapter,
            driver=sbg_driver,
            raw_topic=TOPIC_RAW_SBG,
            raw_capture=sbg_capture,
            extra_tasks=extra,
            _configure=sbg_configure,
        )
    elif settings.rover_driver == "vectornav":
        framer_factory = VnFramer
        controller = InsController(bus, source_factory, framer_factory, hook)
        vn_capture = (
            RawCapture(settings.data_dir, settings.station_id, "vnraw", bus, vendor=VENDOR_VN)
            if capture and settings.ins_raw_gnss
            else None
        )
        vn_adapter = VnStateAdapter(bus, nav_hz_cap=nav_hz_cap, raw_capture=vn_capture)
        vn_driver = VnDriver(controller, vn_adapter, rtcm_enabled=settings.ins_vn_rtcm)

        async def vn_configure(apply: bool) -> Any:
            return await vn_config.configure(controller, vn_driver, settings, apply=apply)

        bundle = InsBundle(
            vendor=VENDOR_VN,
            settings=settings,
            controller=controller,
            adapter=vn_adapter,
            driver=vn_driver,
            raw_topic=TOPIC_RAW_VN,
            raw_capture=vn_capture,
            _configure=vn_configure,
        )
    else:
        raise ValueError(f"ROVER_DRIVER={settings.rover_driver} is not an INS driver")
    holder.append(bundle)
    return bundle


async def hold_port_b(source: ByteSource, driver: SbgDriver, bus: Bus, stop: asyncio.Event) -> None:
    """Keep the SBG Port B RTCM device (`INS_RTCM_PORT`) open until *stop*.

    An open that fails is reported once (`receiver.error`) and retried with backoff; once open,
    a device the driver finds failing (unplugged, or wedged: writes time out) is closed and
    reopened. The driver ends a reported outage (`receiver.recovered`) on the first write that
    goes through, not on the open. A main-port `receiver.connected` or `receiver.capabilities`
    clears `receiver_error` while Port B may still be down: the open outage is reported again
    after each (`SbgDriver.reassert_port_b_outage`). Nothing is read from the device: Port B is
    an input to the unit.
    """
    delay = PORT_B_BACKOFF_S[0]
    reported = False
    opened = False
    driver.port_b_ready = False
    edges = bus.subscribe("receiver.connected", "receiver.capabilities", maxsize=8)

    async def reassert() -> None:
        async for _ in edges:
            driver.reassert_port_b_outage()

    watcher = asyncio.create_task(reassert())
    try:
        while not stop.is_set():
            if not opened:
                try:
                    await source.open()
                except Exception as exc:
                    if not reported:
                        reported = True
                        msg = f"INS_RTCM_PORT {source.name}: open failed ({exc}); retrying"
                        log.warning(msg)
                        driver.note_port_b_outage(msg)
                        bus.publish("receiver.error", msg)
                    await sleep_or_stop(stop, delay)
                    delay = min(delay * 2, PORT_B_BACKOFF_S[1])
                    continue
                driver.note_port_b_reopened()  # an outage before it ends on a good write
                opened, reported, delay = True, False, PORT_B_BACKOFF_S[0]
                log.info("RTCM to the INS goes to %s (Port B)", source.name)
            await sleep_or_stop(stop, PORT_B_CHECK_S)
            if driver.port_b_failing and not stop.is_set():
                log.info("reopening %s after a failed RTCM write", source.name)
                driver.port_b_ready = False
                with contextlib.suppress(Exception):
                    await source.close()
                opened = False
    finally:
        bus.unsubscribe(edges)
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        driver.port_b_ready = False
        if opened:
            with contextlib.suppress(Exception):
                await source.close()
