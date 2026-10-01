"""ZED-F9P rover driver: corrections go straight into the receiver's USB port via its UbxLink."""

from __future__ import annotations

import logging

from mtrtk.core.receiver import ReceiverController
from mtrtk.core.statestore import StateStore
from mtrtk.rover.drivers.base import DriverCapabilities

log = logging.getLogger(__name__)


class UbloxDriver:
    name = "ublox"
    capabilities = DriverCapabilities(
        accepts_rtcm=True, raw_gnss_log=True, attitude=False, imu=False, sats=True, spectrum=True
    )

    def __init__(self, controller: ReceiverController, store: StateStore) -> None:
        self.controller = controller
        self.store = store
        self.dropped_bytes = 0

    async def inject_rtcm(self, data: bytes) -> None:
        link = self.controller.link
        if link is None or not self.controller.connected:
            self.dropped_bytes += len(data)
            return
        try:
            await link.write(data)
        except OSError as exc:  # unplugged mid-write: the controller reconnects, we just drop
            self.dropped_bytes += len(data)
            log.debug("RTCM inject dropped %d bytes: %s", len(data), exc)
            return
        self.store.note_rtcm_injected()
