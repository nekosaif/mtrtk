"""VN-200 rover driver: capabilities and RTCM forwarding.

Whether a VN-200 uses RTCM corrections is undocumented (vnproglib's RTCM example forwards
them unacknowledged to RTK-capable VectorNav units), so forwarding is opt-in
(`INS_VN_RTCM=1`) and reported as unverified until the unit reports an RTK fix (GPS Fix 7/8).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from mtrtk.core.bus import Bus
from mtrtk.rover.drivers.base import DriverCapabilities
from mtrtk.rover.drivers.vectornav.adapter import VnStateAdapter

if TYPE_CHECKING:
    from mtrtk.rover.drivers.vectornav.config import VnConfigReport

log = logging.getLogger(__name__)


class Writer(Protocol):
    """What the driver needs of `InsController`."""

    bus: Bus

    async def write(self, data: bytes) -> None: ...


@dataclass
class VnInfo:
    model: str = ""
    hardware_revision: str = ""
    serial: str = ""
    firmware: str = ""


class VnDriver:
    name = "vectornav"

    def __init__(self, controller: Writer, adapter: VnStateAdapter, *, rtcm_enabled: bool) -> None:
        self.controller = controller
        self.adapter = adapter
        self.rtcm_enabled = rtcm_enabled
        self.dropped_bytes = 0
        self.info: VnInfo | None = None
        self.config_report: VnConfigReport | None = None
        self.saved_this_run = False  # `$VNWNV` is sent at most once per process
        # What binary output 1 is set to carry, from configure's read-back (None = not read).
        self.raw_meas_output: bool | None = None
        self.sat_info_output: bool | None = None

    @property
    def capabilities(self) -> DriverCapabilities:
        return DriverCapabilities(
            accepts_rtcm=self.rtcm_enabled,
            raw_gnss_log=self.adapter.raw_meas_seen or self.raw_meas_output is True,
            attitude=True,
            imu=True,
            sats=self.adapter.sats_seen or self.sat_info_output is True,
            spectrum=False,
        )

    @property
    def rtcm_unverified(self) -> bool:
        """RTCM is being forwarded but the unit has not yet shown it uses it."""
        return self.rtcm_enabled and not self.adapter.rtk_fix_seen

    async def inject_rtcm(self, data: bytes) -> None:
        if not self.rtcm_enabled:
            self.dropped_bytes += len(data)
            return
        try:
            await self.controller.write(data)  # VERIFY: the VN-200 consumes RTCM on this port
        except OSError as exc:  # ConnectionError included: not connected, or a wedged port
            self.dropped_bytes += len(data)
            log.debug("RTCM forward dropped %d bytes: %s", len(data), exc)
            return
        self.adapter.note_rtcm_injected()
