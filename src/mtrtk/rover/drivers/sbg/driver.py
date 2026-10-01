"""Ellipse-D rover driver: capabilities and RTCM injection.

RTCM goes to a separate serial device wired to the unit's Port B (`INS_RTCM_PORT`) when one is
configured, else onto the main port next to the sbgECom traffic, through the controller's one
serialised writer so it never interleaves with a configuration command. Whether the unit takes
RTCM on the main port is a `# VERIFY` (`rtcm_unverified` stays True until the unit echoes an
RTCM3 frame in RTCM_RAW or GPS1_POS reports an RTK solution). Capabilities follow what the unit
actually streams: `sats` once a GPS1_SAT arrived, `raw_gnss_log` until GPS1_RAW turned out not
to be UBX.
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
            self.dropped_bytes += len(data)
            log.debug("RTCM inject dropped %d bytes: %s", len(data), exc)
            return
        self.adapter.note_rtcm_injected()


def _conforms(driver: SbgDriver) -> RoverDriver:
    """Never called: lets the mypy gate check that `SbgDriver` satisfies `RoverDriver`."""
    return driver
