"""Ellipse-D rover driver: capabilities and RTCM injection.

RTCM goes to a separate serial device wired to the unit's Port B (`INS_RTCM_PORT`) when one is
configured, else onto the main port next to the sbgECom traffic, through the controller's one
serialised writer so it never interleaves with a configuration command. Whether the unit takes
RTCM on the main port is a `VERIFY(sbg-rtcm-port-a)` (`rtcm_unverified` stays True until the
unit echoes an RTCM3 frame in RTCM_RAW or GPS1_POS reports an RTK solution). Capabilities
follow what the unit actually streams: `sats` once a GPS1_SAT arrived, `raw_gnss_log` until
GPS1_RAW turned out not to be UBX.

A failing Port B device (unplugged, never opened) is reported once per outage as a WARNING and
a `receiver.error`, and its bytes count into `dropped_bytes`; the driver does not reopen it.
When a holder task owns the device (`factory.hold_port_b`), it sets `port_b_ready`: while the
device is closed RTCM is dropped quietly (the holder reports the open failure and notes it,
`note_port_b_outage`), and a reopen clears `port_b_failing` (`note_port_b_reopened`). The end
of a reported outage is published once as `receiver.recovered`
(`{"source": <device>, "message": ...}`), on the first write that goes through: the main port
stays connected meanwhile, so no `receiver.connected` would clear the `receiver_error` alert the
outage raised. An open that works is no proof that RTCM flows (a wedged adapter opens, then
times out every write), so a reopen alone ends nothing, and a write failing again while the
outage is open reports nothing new. A main-port reconnect or configure clears `receiver_error`
while Port B may still be down: the holder then has the outage reported again
(`reassert_port_b_outage`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Protocol

from mtrtk.core.source import ByteSource
from mtrtk.rover.drivers.base import DriverCapabilities, RoverDriver
from mtrtk.rover.drivers.ins_common import WRITE_TIMEOUT_S
from mtrtk.rover.drivers.sbg.adapter import SbgStateAdapter
from mtrtk.rover.drivers.sbg.logs import SbgInfo

if TYPE_CHECKING:
    from mtrtk.rover.drivers.sbg.config import SbgConfigReport

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
        self.config_report: SbgConfigReport | None = None  # the last configure's report
        self.saved_this_run = False  # SAVE_SETTINGS is sent at most once per process
        self._rtcm_lock = asyncio.Lock()
        self._port_b_failing = False  # the last write failed; reset by a reopen or a good write
        # The reported Port B outage (its last `receiver.error` message) until a write goes
        # through and `receiver.recovered` ends it; None while there is none.
        self._port_b_outage: str | None = None
        # Set by the task that holds Port B open: False while it is closed, True once open.
        # None (no holder) writes and reports as above.
        self.port_b_ready: bool | None = None

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
    def port_b_failing(self) -> bool:
        """The last RTCM write to the Port B device failed (reset by the next that succeeds)."""
        return self._port_b_failing

    def note_port_b_outage(self, message: str) -> None:
        """The holder reported an outage itself (`receiver.error` *message*: failed opens); the
        first write that goes through after it ends it."""
        self._port_b_outage = message

    def note_port_b_reopened(self) -> None:
        """The holder opened the Port B device again. The device is written to again, but the
        outage stays open until a write goes through: an open is no proof that RTCM flows."""
        self.port_b_ready = True
        self._port_b_failing = False

    def reassert_port_b_outage(self) -> None:
        """Publish the open outage's `receiver.error` again: a main-port reconnect or configure
        just cleared `receiver_error` while Port B is still down."""
        if self._port_b_outage is not None:
            self.adapter.bus.publish("receiver.error", self._port_b_outage)

    def _publish_port_b_recovered(self) -> None:
        name = self.rtcm_source.name if self.rtcm_source is not None else "Port B"
        msg = f"RTCM to {name} restored (Port B)"
        log.info(msg)
        self.adapter.bus.publish("receiver.recovered", {"source": name, "message": msg})

    @property
    def rtcm_unverified(self) -> bool:
        """Corrections are forwarded but the unit has shown no sign of taking them: no RTCM_RAW
        echo and no RTK solution yet."""
        return not (self.adapter.rtcm_echo_seen or self.adapter.rtk_seen)

    async def inject_rtcm(self, data: bytes) -> None:
        try:
            if self.rtcm_source is not None:
                if self.port_b_ready is False:  # not open (yet): the holder reports and retries
                    self.dropped_bytes += len(data)
                    return
                async with self._rtcm_lock:
                    await asyncio.wait_for(self.rtcm_source.write(data), self.write_timeout_s)
            elif not self.controller.connected:
                self.dropped_bytes += len(data)
                return
            else:
                await self.controller.write(data)  # VERIFY(sbg-rtcm-port-a)
        except (OSError, TimeoutError) as exc:  # ConnectionError included; a wedged port
            # A timed-out write may still drain from the transport buffer: counted as dropped
            # all the same, since nothing says it reached the unit.
            self.dropped_bytes += len(data)
            if self.rtcm_source is not None:
                self._port_b_failing = True  # the holder closes and reopens the device
            if self.rtcm_source is not None and self._port_b_outage is None:
                reason = str(exc) or type(exc).__name__
                msg = f"RTCM to {self.rtcm_source.name} failed ({reason}): corrections dropped"
                log.warning(msg)
                self._port_b_outage = msg
                self.adapter.bus.publish("receiver.error", msg)
            else:  # an open outage, or the main port's (whose controller reports it)
                log.debug("RTCM inject dropped %d bytes: %s", len(data), exc)
            return
        self._port_b_failing = False
        if self._port_b_outage is not None:  # only Port B sets it
            self._port_b_outage = None
            self._publish_port_b_recovered()
        self.adapter.note_rtcm_injected()


def _conforms(driver: SbgDriver) -> RoverDriver:
    """Never called: lets the mypy gate check that `SbgDriver` satisfies `RoverDriver`."""
    return driver
