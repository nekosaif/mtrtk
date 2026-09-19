"""Process supervisor: wires source -> router -> bus -> state per role, and prints status."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import Callable

from mtrtk.config import Role, Settings
from mtrtk.core.bus import Bus, Policy
from mtrtk.core.receiver import ReceiverController
from mtrtk.core.router import TOPIC_RAW_RTCM, TOPIC_RAW_UBX
from mtrtk.core.source import ByteSource, FileReplaySource, SerialSource, find_ublox_port
from mtrtk.core.statestore import StateStore
from mtrtk.core.ubx_config import base_profile, rover_profile

log = logging.getLogger(__name__)


class StatusPrinter:
    """At most one status line per interval, driven by NAV-EOE where the receiver sends it.

    Both topics are throttled: NAV_EOE arrives once per *navigation* epoch, so a 5 Hz rover
    would otherwise print five lines a second. `state.position` is only a fallback for streams
    that carry no NAV-EOE at all - once an epoch has been seen it stops printing, so a drifting
    PVT-to-EOE gap can never emit two lines for the same epoch.
    """

    def __init__(
        self,
        bus: Bus,
        store: StateStore,
        echo: Callable[[str], object] = print,
        interval_s: float = 1.0,
    ) -> None:
        self.store = store
        self.echo = echo
        self.interval_s = interval_s
        self._last = 0.0
        self._saw_epoch = False
        self._sub = bus.subscribe("state.epoch", "state.position", maxsize=50)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="status-printer")

    async def stop(self) -> None:
        self._sub.close()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        async for topic, _ in self._sub:
            if topic == "state.epoch":
                self._saw_epoch = True
            elif self._saw_epoch:
                continue  # NAV-EOE drives the line; position is the fallback, not a second line
            now = time.monotonic()
            if now - self._last < self.interval_s:
                continue
            self._last = now
            self.echo(self.format_line())

    def format_line(self) -> str:
        s = self.store.state
        utc = s.time.utc.strftime("%H:%M:%S") if s.time.utc else "--:--:--"
        lat = f"{s.position.lat:.7f}" if s.position.lat is not None else "-"
        lon = f"{s.position.lon:.7f}" if s.position.lon is not None else "-"
        height = f"{s.position.height_m:.2f}" if s.position.height_m is not None else "-"
        hacc = f"{s.accuracy.h_acc_m:.2f}" if s.accuracy.h_acc_m is not None else "-"
        return (
            f"{utc} {s.fix.fix_type_name:<9} {s.fix.carr_soln_name:<9} "
            f"sats {s.sat_summary.used}/{s.sat_summary.tracked} "
            f"lat {lat} lon {lon} h {height} hAcc {hacc} rtcm {s.rtcm_out.bytes_per_s:.0f} B/s"
        )


class Daemon:
    def __init__(
        self,
        settings: Settings,
        source_factory: Callable[[], ByteSource] | None = None,
        passive: bool | None = None,
    ) -> None:
        self.settings = settings
        self.bus = Bus()
        self.store = StateStore(self.bus)
        self.stop = asyncio.Event()
        self.error: BaseException | None = None  # what ended the run, if it was a failure
        # Replay must be lossless: an unpaced file outruns the state loop, and dropping its
        # tail would silently rewrite history. A live receiver paces itself, so there a bounded
        # queue that sheds the oldest frames is the right back-pressure.
        self._raw_sub = (
            self.bus.subscribe(TOPIC_RAW_UBX, TOPIC_RAW_RTCM, policy=Policy.UNBOUNDED)
            if settings.source_is_file
            else self.bus.subscribe(TOPIC_RAW_UBX, TOPIC_RAW_RTCM, maxsize=5000)
        )
        # The receiver's own events, not the wire, decide `ReceiverState.connected`/`.source`.
        self._events_sub = self.bus.subscribe("receiver.connected", "receiver.disconnected")
        passive = settings.source_is_file if passive is None else passive
        profile = base_profile(settings) if settings.role is Role.BASE else rover_profile(settings)
        self.controller = ReceiverController(
            self.bus,
            source_factory or self._default_source_factory(),
            profile=None if passive else profile,
            strict=settings.receiver_strict,
            passive=passive,
        )

    def _default_source_factory(self) -> Callable[[], ByteSource]:
        s = self.settings
        if s.source_is_file:
            path = s.source_path
            return lambda: FileReplaySource(path, speed=s.replay_speed, loop=s.replay_loop)
        port = s.mtrtk_source
        if port == "auto":
            found = find_ublox_port()
            if found is None:
                raise RuntimeError(
                    "no u-blox receiver found; set MTRTK_SOURCE to the serial device"
                )
            port = found
        return lambda: SerialSource(port, s.baud)

    async def _state_loop(self) -> None:
        async for _, frame in self._raw_sub:
            self.store.apply(frame)

    async def _events_loop(self) -> None:
        """Mirror the receiver's connection events into the published state."""
        async for topic, item in self._events_sub:
            if topic == "receiver.connected":
                self.store.state.source = str(item)
                self.store.state.connected = True
            else:
                # `source` keeps naming the link we lost: a UI showing "disconnected" still
                # has to say from what.
                self.store.state.connected = False

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # not the main thread / not supported (Windows)
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, self.stop.set)
        state_task = asyncio.create_task(self._state_loop(), name="state-loop")
        events_task = asyncio.create_task(self._events_loop(), name="receiver-events")
        controller_task = asyncio.create_task(self.controller.run(self.stop), name="receiver")
        try:
            await controller_task  # returns on EOF (replay) or when stop is set
        except BaseException as exc:  # a strict profile failure ends the process
            self.error = exc
            raise
        finally:
            self.stop.set()
            self._raw_sub.close()  # state loop drains what is queued, then exits
            self._events_sub.close()
            await asyncio.gather(state_task, events_task, return_exceptions=True)
