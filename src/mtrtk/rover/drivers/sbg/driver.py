"""Ellipse-D rover driver: capabilities and RTCM injection.

RTCM goes to a separate serial device wired to the unit's Port B (`INS_RTCM_PORT`) when one is
configured, else onto the main port next to the sbgECom traffic, through the controller's one
serialised writer so it never interleaves with a configuration command. Whether the unit takes
RTCM on the main port is a `# VERIFY` (`rtcm_unverified` stays True until the unit echoes an
RTCM3 frame in RTCM_RAW or GPS1_POS reports an RTK solution). Capabilities follow what the unit
actually streams: `sats` once a GPS1_SAT arrived, `raw_gnss_log` until GPS1_RAW turned out not
to be UBX.

A failing Port B device (unplugged, never opened) is reported once per outage as a WARNING and
a `receiver.error`, and its bytes count into `dropped_bytes`; the driver does not reopen it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from mtrtk.core.source import ByteSource
from mtrtk.rover.drivers.base import DriverCapabilities, RoverDriver
from mtrtk.rover.drivers.ins_common import WRITE_TIMEOUT_S
from mtrtk.rover.drivers.sbg.adapter import SbgStateAdapter
from mtrtk.rover.drivers.sbg.logs import SbgInfo

log = logging.getLogger(__name__)


class Writer(Protocol):
    """What the driver needs of `InsController`."""

    connected: bool

    async def write(self, data: bytes) -> None: ...


class SbgDriver:
    name = "sbg_ellipse"

    def __init__(
        self,
        controller: Writer,
        adapter: SbgStateAdapter,
        *,
        rtcm_source: ByteSource | None = None,
    ) -> None:
        self.controller = controller
        self.adapter = adapter
        self.rtcm_source = rtcm_source
        self.dropped_bytes = 0
        self.write_timeout_s = WRITE_TIMEOUT_S  # bounds a wedged Port B device
        self.info: SbgInfo | None = None  # CMD INFO reply, set by the configure step
        self._rtcm_lock = asyncio.Lock()
        self._port_b_failing = False  # reported; reset by the next write that goes through

    @property
    def capabilities(self) -> DriverCapabilities:
        a = self.adapter
        return DriverCapabilities(
            accepts_rtcm=True,
            raw_gnss_log=a.raw_gnss and a.raw_gnss_format != "unknown",
            attitude=True,
            imu=True,
            sats=a.sats_seen,
            spectrum=False,
        )

    @property
    def rtcm_unverified(self) -> bool:
        """Corrections are forwarded but the unit has shown no sign of taking them: no RTCM_RAW
        echo and no RTK solution yet."""
        return not (self.adapter.rtcm_echo_seen or self.adapter.rtk_seen)

    async def inject_rtcm(self, data: bytes) -> None:
        try:
            if self.rtcm_source is not None:
                async with self._rtcm_lock:
                    await asyncio.wait_for(self.rtcm_source.write(data), self.write_timeout_s)
            elif not self.controller.connected:
                self.dropped_bytes += len(data)
                return
            else:
                await self.controller.write(data)  # VERIFY: RTCM multiplexed on Port A
        except (OSError, TimeoutError) as exc:  # ConnectionError included; a wedged port
            # A timed-out write may still drain from the transport buffer: counted as dropped
            # all the same, since nothing says it reached the unit.
            self.dropped_bytes += len(data)
            if self.rtcm_source is not None and not self._port_b_failing:
                self._port_b_failing = True
                reason = str(exc) or type(exc).__name__
                msg = f"RTCM to {self.rtcm_source.name} failed ({reason}): corrections dropped"
                log.warning(msg)
                self.adapter.bus.publish("receiver.error", msg)
            else:  # the main port's own disconnect is reported by the controller
                log.debug("RTCM inject dropped %d bytes: %s", len(data), exc)
            return
        self._port_b_failing = False
        self.adapter.note_rtcm_injected()


def _conforms(driver: SbgDriver) -> RoverDriver:
    """Never called: lets the mypy gate check that `SbgDriver` satisfies `RoverDriver`."""
    return driver
