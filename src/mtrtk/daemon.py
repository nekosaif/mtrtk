"""Process supervisor: wires source -> router -> bus -> state per role, runs the role's
consumers under restart supervision, and prints status."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mtrtk.alerts import AlertEngine
from mtrtk.base.basemode import BaseModeManager
from mtrtk.base.ntrip_caster import CasterConfig, NtripCaster
from mtrtk.config import Role, Settings
from mtrtk.core.bus import Bus, Policy
from mtrtk.core.exposure import (
    REBIND_CHECK_S,
    sleep_or_stop,
    url_host,
    wait_for_bind,
    wait_for_rebind,
)
from mtrtk.core.receiver import ReceiverController
from mtrtk.core.router import TOPIC_RAW_RTCM, TOPIC_RAW_UBX
from mtrtk.core.source import (
    NO_UBLOX_RECEIVER,
    SOURCE_ENDED,
    ByteSource,
    FileReplaySource,
    NoReceiverSource,
    SerialSource,
    find_ublox_port,
)
from mtrtk.core.statestore import StateStore
from mtrtk.core.ubx_config import base_profile, rover_profile
from mtrtk.jobs import JobRunner
from mtrtk.rawlog.retention import RetentionPolicy
from mtrtk.rawlog.writer import RawLogWriter, recover_incomplete
from mtrtk.rover.drivers.base import RoverDriver
from mtrtk.rover.drivers.factory import InsBundle, StoreFacade, build_ins
from mtrtk.rover.drivers.ublox import UbloxDriver
from mtrtk.rover.json_out import JsonUdpPublisher
from mtrtk.rover.nmea_out import NmeaPublisher, build_gga, has_valid_fix
from mtrtk.rover.ntrip_client import NtripClient, NtripClientConfig
from mtrtk.rover.points import PointCollector, PointsRepo
from mtrtk.rover.sessions import SessionsRepo
from mtrtk.rover.sinks import NmeaSink, SerialSink, TcpBroadcastSink, UdpSink
from mtrtk.store.db import Database
from mtrtk.store.models import Level
from mtrtk.store.repos import EventsRepo, NtripLogRepo, SitesRepo
from mtrtk.store.sampler import Sampler
from mtrtk.system import SystemMonitor
from mtrtk.web.app import create_app
from mtrtk.web.context import AppContext
from mtrtk.web.server import WebServer

log = logging.getLogger(__name__)

ConsumerFactory = Callable[[], Awaitable[None]]
SUPERVISE_BACKOFF_START_S = 1.0
SUPERVISE_BACKOFF_MAX_S = 60.0
# How long a consumer may take to wind down on `stop` before it is cancelled instead. Every
# consumer ends on `stop` by itself; the cancel is only there so one that wedges cannot hold
# the process open for ever.
CONSUMER_SHUTDOWN_GRACE_S = 15.0

RTCM_MSM7_FORMATS = "1005(1),1077(1),1087(1),1097(1),1127(1),1230(%d)"
RTCM_MSM4_FORMATS = "1005(1),1074(1),1084(1),1094(1),1124(1),1230(%d)"

PTY_LINK_NAME = "ttyMTRTK"  # DATA_DIR/ttyMTRTK -> the pty slave when NMEA_SERIAL=pty
PTY_LINK_POLL_S = 0.1
LINK_EVENTS = ("receiver.connected", "receiver.disconnected")


class StatusPrinter:
    """At most one status line per interval, driven by `state.epoch`.

    Both topics are throttled: an epoch arrives once per *navigation* epoch, so a 5 Hz rover
    would otherwise print five lines a second. `state.epoch` is closed by NAV-EOE or, on a
    stream without it, inferred by the store. `state.position` is only a fallback until the
    first epoch is published - from then on it stops printing, so a drifting PVT-to-epoch gap
    can never emit two lines for the same epoch.
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
                continue  # the epoch drives the line; position is the fallback, not a second
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
        line = (
            f"{utc} {s.fix.fix_type_name:<9} {s.fix.carr_soln_name:<9} "
            f"sats {s.sat_summary.used}/{s.sat_summary.tracked} "
            f"lat {lat} lon {lon} h {height} hAcc {hacc} rtcm {s.rtcm_out.bytes_per_s:.0f} B/s"
        )
        svin = s.survey_in
        if svin.active or svin.valid:
            # A survey-in is the one thing a base operator watches for minutes on end, so it
            # rides on the same line rather than in a second stream of output.
            acc = f"{svin.mean_acc_m:.2f}" if svin.mean_acc_m is not None else "-"
            line += f" svin {svin.dur_s}s σ{acc}m {'✓' if svin.valid else '…'}"
        rtk = s.rtk
        if rtk.corr_age_s is not None:
            # Only once corrections have flowed: a base, or a rover without NTRIP, never has any.
            base = f"{rtk.baseline_m:.1f}m" if rtk.baseline_m is not None else "-"
            line += f" rtk {rtk.carr_soln_name} age {rtk.corr_age_s:.1f}s base {base}"
        if s.attitude is not None:
            # An INS rover: the filter mode and the heading are what an operator watches.
            mode = (s.ins.mode_name or "-") if s.ins is not None else "-"
            hdg = s.attitude.heading_deg
            line += f" ins {mode} att {f'{hdg:.1f}°' if hdg is not None else '-'}"
        return line


@dataclass
class RoverServices:
    """What a running rover role exposes to the web layer (`AppContext.rover`)."""

    driver: RoverDriver
    collector: PointCollector
    sessions_repo: SessionsRepo
    points_repo: PointsRepo
    ntrip_client: NtripClient | None = None
    nmea: NmeaPublisher | None = None
    json_udp: JsonUdpPublisher | None = None
    _set_url: Callable[[str], Awaitable[None]] | None = None

    async def set_ntrip_url(self, url: str) -> None:
        """Stop the running NTRIP client (if any) and start one on *url*."""
        if self._set_url is not None:
            await self._set_url(url)


class _RawlogClock:
    """The raw logger's clock as an INS adapter sees it.

    `RawLogWriter` is built by the `rawlog` consumer (and rebuilt when the supervisor restarts
    it), after the adapter: this forwards the unit's UTC to whichever writer is running now.
    """

    def __init__(self, daemon: Daemon) -> None:
        self._daemon = daemon

    def note_utc(self, dt: datetime) -> None:
        writer = self._daemon.rawlog
        if writer is not None:
            writer.note_utc(dt)


class Daemon:
    def __init__(
        self,
        settings: Settings,
        source_factory: Callable[[], ByteSource] | None = None,
        passive: bool | None = None,
    ) -> None:
        self.settings = settings
        self.bus = Bus()
        self.store: StateStore = StateStore(self.bus)
        self.stop = asyncio.Event()
        self._stop_trigger: str | None = None  # what asked for the stop, when we know
        self.error: BaseException | None = None  # what ended the run, if it was a failure
        self.db = Database(settings.data_dir / "mtrtk.db")
        self.caster: NtripCaster | None = None
        self.basemode: BaseModeManager | None = None
        # The hour the raw logger has open, so the API refuses to delete it rather than guessing.
        self.rawlog: RawLogWriter | None = None
        self.jobs: JobRunner | None = None  # built in `run()`: it reads and writes the database
        self.web: WebServer | None = None
        # How often a `tailscale` web/caster bind checks that tailscale0 still has its address.
        self.rebind_check_s = REBIND_CHECK_S
        self._ctx: AppContext | None = None
        self.rover: RoverServices | None = None  # set while the rover role is running
        self._ntrip_task: asyncio.Task[None] | None = None
        # One URL change at a time: two overlapping ones would each stop the old client and
        # the slower one would drop the faster one's new client without stopping it.
        self._ntrip_lock = asyncio.Lock()
        # An INS rover (ROVER_DRIVER=sbg_ellipse|vectornav) runs the vendor's stack on
        # INS_PORT instead of the u-blox one: no ReceiverController, no UBX profile.
        self.ins: InsBundle | None = None
        self.controller: ReceiverController | None = None
        # The receiver's own events, not the wire, decide `ReceiverState.connected`/`.source`.
        self._events_sub = self.bus.subscribe("receiver.connected", "receiver.disconnected")
        if settings.role is Role.ROVER and settings.rover_driver != "ublox":
            # A replay configures nothing; nor does a caller that asks for a passive session.
            self.passive = settings.source_is_file if passive is None else passive
            self.ins = build_ins(
                settings,
                self.bus,
                source_factory=source_factory,
                raw_writer=_RawlogClock(self),
                configure_on_connect=not self.passive,
            )
            # Every consumer keeps reading `daemon.store.state`: it is the adapter's state.
            self.store = StoreFacade(self.ins.adapter)
            # A live unit paces itself: a bounded queue that sheds the oldest frames. A replay
            # (MTRTK_SOURCE=file:) must be lossless, as for the u-blox path below.
            self._raw_sub = (
                self.bus.subscribe(self.ins.raw_topic, policy=Policy.UNBOUNDED)
                if settings.source_is_file
                else self.bus.subscribe(self.ins.raw_topic, maxsize=5000)
            )
            return
        # Replay must be lossless: an unpaced file outruns the state loop, and dropping its
        # tail would silently rewrite history. A live receiver paces itself, so there a bounded
        # queue that sheds the oldest frames is the right back-pressure.
        # The link events ride the same queue, so the store sees them between the right frames.
        raw_topics = (TOPIC_RAW_UBX, TOPIC_RAW_RTCM, *LINK_EVENTS)
        self._raw_sub = (
            self.bus.subscribe(*raw_topics, policy=Policy.UNBOUNDED)
            if settings.source_is_file
            else self.bus.subscribe(*raw_topics, maxsize=5000)
        )
        self.passive = settings.source_is_file if passive is None else passive
        profile = base_profile(settings) if settings.role is Role.BASE else rover_profile(settings)
        self.controller = ReceiverController(
            self.bus,
            source_factory or self._default_source_factory(),
            profile=None if self.passive else profile,
            strict=settings.receiver_strict,
            passive=self.passive,
            ack_timeout_s=settings.receiver_ack_timeout_s,
        )

    # ----------------------------------------------------------------- sources
    def _default_source_factory(self) -> Callable[[], ByteSource]:
        s = self.settings
        if s.source_is_file:
            path = s.source_path
            return lambda: FileReplaySource(path, speed=s.replay_speed, loop=s.replay_loop)
        if s.mtrtk_source != "auto":
            port = s.mtrtk_source
            return lambda: SerialSource(port, s.baud)
        # `auto` with nothing plugged in is a receiver that is not there *yet*, exactly like a
        # configured device path that does not exist: the daemon serves the UI, the API and the
        # caster, the controller reports the receiver down and keeps scanning with backoff, and
        # the receiver starts the moment one enumerates. Exiting here instead had Docker
        # restart-loop the container with no UI reachable to say why.
        last: str | None = find_ublox_port()
        if last is None:
            log.warning("%s; serving the UI and scanning USB until one appears", NO_UBLOX_RECEIVER)

        def auto_source() -> ByteSource:
            """Re-resolve the device on every (re)connect.

            A hardware or factory reset takes the USB device off the bus and brings it back,
            and udev may hand it a different `ttyACM*` on the way in. Resolving once at startup
            would leave the reconnect loop retrying a node that no longer exists, for ever.
            """
            nonlocal last
            port = find_ublox_port()
            if port is None:
                if last is None:
                    # Never seen one: a source whose open fails like a missing device's.
                    return NoReceiverSource()
                # Nothing is enumerated this instant - mid-reset, most likely. Retry the last
                # path we saw: opening it fails with an `OSError` the controller already backs
                # off from, and the next attempt resolves again. Raising here would take the
                # whole daemon down instead.
                port = last
            elif last is None:
                log.info("u-blox receiver found at %s", port)
                last = port
            elif port != last:
                log.info("u-blox receiver is now at %s (was %s)", port, last)
                last = port
            return SerialSource(port, s.baud)

        return auto_source

    # --------------------------------------------------------------- consumers
    def _consumers(self) -> list[tuple[str, ConsumerFactory]]:
        """The long-running jobs this role owns, each supervised and restarted on failure."""
        s = self.settings
        stop = self.stop
        host = socket.gethostname()
        consumers: list[tuple[str, ConsumerFactory]] = [
            ("system", lambda: SystemMonitor(self.bus, s.data_dir).run(stop)),
            ("sampler", lambda: Sampler(self.bus, self.db).run(stop)),
            (
                "alerts",
                lambda: AlertEngine(
                    self.bus,
                    EventsRepo(self.db),
                    role=s.role.value,
                    host=host,
                    min_free_gb=s.min_free_gb,
                    webhook_url=s.alert_webhook_url,
                ).run(stop),
            ),
            # Every role serves the API: a rover's UI is the same one screen as a base's.
            ("web", self._run_web),
        ]
        # Both roles log raw: a base for PPP, a rover for PPK. Replaying a file must not spend
        # the disk it is being read from, so raw logging is opt-in there (REPLAY_LOG=1) and
        # always on for a live receiver.
        if not s.source_is_file or s.replay_log:
            consumers.append(("rawlog", self._run_rawlog))
            consumers.append(
                (
                    "retention",
                    lambda: RetentionPolicy(
                        s.data_dir, s.min_free_gb, self.bus, reclaim=self._reclaim_exports
                    ).run(stop),
                )
            )
        if s.role is Role.BASE:
            consumers.append(("ntrip", self._run_caster))
            if not self.passive:
                # The mode manager writes TMODE to the receiver; a replay has none to write to.
                consumers.append(("basemode", self._run_basemode))
        elif s.role is Role.ROVER:
            consumers.append(("rover", self._run_rover))
        return consumers

    async def _reclaim_exports(self, ended_by: datetime) -> None:
        """Retention's `reclaim`: the export jobs made of raw hours it is about to delete."""
        if self.jobs is not None:
            await self.jobs.prune_exports(ended_by)

    async def _run_rawlog(self) -> None:
        s = self.settings
        writer = RawLogWriter(
            self.bus,
            s.data_dir,
            s.station_id,
            s.log_messages,
            role=s.role.value,
            fsync_interval_s=s.fsync_interval_s,
        )
        # Published as soon as it exists: `DELETE /api/logs/{name}` asks the writer which hour
        # is open rather than assuming it is the newest one.
        self.rawlog = writer
        meta_sub = self.bus.subscribe("receiver.capabilities", "base.mode", maxsize=10)

        async def track_metadata() -> None:
            """Name the firmware and the site in every sidecar written from here on."""
            async for topic, item in meta_sub:
                if topic == "receiver.capabilities":
                    writer.firmware = getattr(item, "fw_version", "") or writer.firmware
                elif topic == "base.mode":
                    writer.site = item.get("site")

        meta_task = asyncio.create_task(track_metadata(), name="rawlog-meta")
        run_task = asyncio.create_task(writer.run(self.stop), name="rawlog-run")
        stop_task = asyncio.create_task(self.stop.wait(), name="rawlog-stop")
        outcomes: tuple[Any, ...] = ()
        try:
            await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.rawlog = None  # before the supervisor can build a replacement
            writer.stop()  # closes the subscription -> run() drains it, then closes the file
            # Unsubscribe, not close: the supervisor restarts this consumer, and a closed-but-
            # still-registered subscription stays in `Bus._subs` and is offered every message
            # for the rest of the process.
            self.bus.unsubscribe(meta_sub)
            stop_task.cancel()
            outcomes = await asyncio.gather(run_task, meta_task, stop_task, return_exceptions=True)
        # `asyncio.wait` never raises what its awaitables raised, and gathering with
        # `return_exceptions` retrieves them. Without this the supervisor would read a writer
        # that died on a full disk as a clean return and never start another one.
        for outcome in outcomes:
            if isinstance(outcome, BaseException) and not isinstance(
                outcome, asyncio.CancelledError
            ):
                raise outcome

    async def _run_caster(self) -> None:
        await self._serve_on_bind("NTRIP caster", self.settings.ntrip_bind, self._serve_caster)

    async def _serve_caster(self, host: str, until: asyncio.Event) -> None:
        """Run the caster on *host* until *until* is set (shutdown, or the address moved)."""
        s = self.settings
        template = RTCM_MSM7_FORMATS if s.rtcm_msm == 7 else RTCM_MSM4_FORMATS
        config = CasterConfig(
            mountpoint=s.mountpoint,
            username=s.ntrip_user,
            password=s.ntrip_password or "",
            station_id=s.station_id,
            country=s.country,
            format_details=template % s.rtcm_1230_rate,
        )

        def position() -> tuple[float, float] | None:
            p = self.store.state.position
            return (p.lat, p.lon) if p.lat is not None and p.lon is not None else None

        # Construction and `start()` are inside the try: `NtripCaster` subscribes in its
        # constructor, so a bind that fails - the port is taken, the interface went away - must
        # still reach `stop()`, or every restart the supervisor makes leaves a subscription.
        caster: NtripCaster | None = None
        try:
            caster = NtripCaster(
                self.bus,
                config,
                host,
                s.ntrip_port,
                ntrip_log=NtripLogRepo(self.db),
                position=position,
                bitrate=lambda: self.store.state.rtcm_out.bytes_per_s * 8,
                max_clients=s.ntrip_max_clients,
            )
            await caster.start()  # published only once it is actually listening
            self.caster = caster
            # On a rebind `stop()` below hangs up the rovers still on the old address: nothing
            # routes to it any more, and each reconnects to the caster on the new one.
            await until.wait()
        finally:
            self.caster = None
            if caster is not None:
                await caster.stop()

    def _app_context(self) -> AppContext:
        """What the web layer reads this daemon through.

        Built once per process, not once per web (re)start: `AppContext.started_mono` is what
        `/api/status` reports as `uptime_s`, and a supervised restart of the server must not
        make the daemon look as though it had just come up.
        """
        if self._ctx is None:
            self._ctx = AppContext(
                settings=self.settings,
                bus=self.bus,
                store=self.store,
                db=self.db,
                daemon=self,
                jobs=self.jobs,
            )
        return self._ctx

    async def _run_web(self) -> None:
        await self._serve_on_bind("web UI", self.settings.web_bind, self._serve_web)

    async def _serve_web(self, host: str, until: asyncio.Event) -> None:
        """Serve the UI/API on *host* until *until* is set (shutdown, or the address moved)."""
        # A fresh app per attempt (and per rebind): its lifespan owns the WebSocket hub and the
        # other bus subscribers, and re-entering the lifespan of one that has already shut down
        # would leave the restarted server serving closed subscriptions.
        server = WebServer(create_app(self._app_context()), host, self.settings.web_port)
        self.web = server
        try:
            await server.serve(until)
        finally:
            self.web = None

    async def _serve_on_bind(
        self, what: str, mode: str, serve: Callable[[str, asyncio.Event], Awaitable[None]]
    ) -> None:
        """Run *serve* on the address *mode* resolves to, and again on a new one if it moves.

        tailscaled restores its cached state at boot before the new netmap arrives, so a node
        whose tailnet address was changed comes up on the old one for a few seconds. A daemon
        that bound then would listen on a dead address for good (Docker does not restart an
        unhealthy container). Every `rebind_check_s` a `tailscale` bind checks tailscale0, and
        when its address has changed, *serve* is wound down and started again on the new one,
        in-process, with an info event naming both. Never 0.0.0.0. A fixed bind never moves.
        """
        host = await wait_for_bind(mode, self.stop)
        while host is not None:  # None: stop was set while waiting for the interface
            until = asyncio.Event()
            watch = asyncio.create_task(self._watch_bind(mode, host, until), name=f"rebind-{what}")
            try:
                await serve(host, until)
            finally:
                until.set()
                watch.cancel()  # a no-op once it has returned the new address
                outcome = (await asyncio.gather(watch, return_exceptions=True))[0]
            if isinstance(outcome, BaseException) and not isinstance(
                outcome, asyncio.CancelledError
            ):
                # The watcher failed, and its `finally` wound the server down. Returning would
                # read to the supervisor as "done": nothing listening, nothing logged, nothing
                # restarted. Raised, it is reported and the server is started again.
                raise outcome
            new = outcome if isinstance(outcome, str) else None
            if new is None or self.stop.is_set():
                return
            log.info("%s: tailnet address changed from %s to %s; re-binding there", what, host, new)
            # Recorded before the new bind is tried: a bind that fails there is the supervisor's
            # to report and retry, so this must not claim a listener that may not come up.
            await self._note_event(
                "info",
                "bind_changed",
                f"{what} moving from {url_host(host)} to {url_host(new)}: tailscale0's address "
                "changed, so it is re-binding on the new one",
            )
            host = new

    async def _watch_bind(self, mode: str, host: str, until: asyncio.Event) -> str | None:
        """Set *until* once *mode* resolves somewhere other than *host*; return the new address."""
        try:
            return await wait_for_rebind(mode, host, self.stop, self.rebind_check_s)
        finally:
            until.set()

    async def _run_basemode(self) -> None:
        s = self.settings
        assert self.controller is not None  # a base always runs the u-blox receiver
        manager = BaseModeManager(
            self.bus,
            self.controller,
            SitesRepo(self.db),
            self.store,
            base_mode=s.base_mode,
            svin_min_duration_s=s.svin_min_duration_s,
            svin_acc_limit_m=s.svin_acc_limit_m,
            active_site_name=s.active_site,
        )
        self.basemode = manager
        try:
            await manager.run(self.stop)
        finally:
            self.basemode = None

    # ------------------------------------------------------------------- rover
    def _nmea_sinks(self) -> list[NmeaSink]:
        s = self.settings
        sinks: list[NmeaSink] = []
        if s.nmea_tcp_port >= 0:  # 0 = an ephemeral port (tests); negative turns the server off
            sinks.append(
                TcpBroadcastSink(
                    s.nmea_tcp_bind, s.nmea_tcp_port, max_clients=s.nmea_tcp_max_clients
                )
            )
        targets = s.udp_targets()
        if len(targets) != len(s.nmea_udp_targets):
            log.warning(
                "NMEA_UDP_TARGETS: ignoring %d entr(ies) that are not host:port",
                len(s.nmea_udp_targets) - len(targets),
            )
        if targets:
            sinks.append(UdpSink(targets))
        if s.nmea_serial:
            sinks.append(SerialSink(s.nmea_serial, s.nmea_serial_baud))
        return sinks

    async def _run_rover(self) -> None:
        """The rover role: point collector, NMEA/JSON outputs and the NTRIP client.

        Everything that subscribes to the bus is built inside the `try`, so a failure part-way
        (and the supervisor's restart after it) never leaves a subscription behind.
        """
        s = self.settings
        stop = self.stop
        sessions, points = SessionsRepo(self.db), PointsRepo(self.db)
        driver: RoverDriver
        if self.ins is not None:
            driver = self.ins.driver
        else:
            assert self.controller is not None
            driver = UbloxDriver(self.controller, self.store)
        rover: RoverServices | None = None
        tasks: list[asyncio.Task[None]] = []
        try:
            collector = PointCollector(
                self.bus,
                self.store,
                points,
                sessions,
                default_epochs=s.point_epochs,
                default_fixed_only=s.point_fixed_only,
            )
            rover = RoverServices(driver, collector, sessions, points, _set_url=self._restart_ntrip)
            sinks = self._nmea_sinks()
            if sinks:
                rover.nmea = NmeaPublisher(
                    self.bus, self.store, sinks, s.nmea_sentences, s.nmea_slow_interval_s
                )
            if s.json_udp_port:
                rover.json_udp = JsonUdpPublisher(self.bus, [("127.0.0.1", s.json_udp_port)])
            self.rover = rover
            tasks.append(asyncio.create_task(collector.run(stop), name="points"))
            if rover.nmea is not None:
                nmea = rover.nmea
                supervised = self._supervise("nmea", lambda: nmea.run(stop))
                tasks.append(asyncio.create_task(supervised, name="nmea"))
                if s.nmea_serial == "pty":
                    tasks.append(asyncio.create_task(self._link_pty(nmea), name="pty-link"))
            if rover.json_udp is not None:
                json_udp = rover.json_udp
                tasks.append(
                    asyncio.create_task(
                        self._supervise("json-udp", lambda: json_udp.run(stop)), name="json-udp"
                    )
                )
            if s.ntrip_url:
                try:
                    await self._restart_ntrip(s.ntrip_url)
                except ValueError as exc:
                    # Not the message: it can quote the URL, and the URL carries the password.
                    # The rover keeps running; `PUT /api/rover/ntrip` can set a working one.
                    log.error(
                        "NTRIP_URL is not a usable caster URL (%s); running without "
                        "corrections until one is set",
                        type(exc).__name__,
                    )
                    await self._note_event(
                        "warning",
                        "ntrip_url_invalid",
                        "NTRIP_URL is set but is not a usable caster URL "
                        f"({type(exc).__name__}); running without corrections until one is "
                        "set (Change caster on the RTK page)",
                    )
            await stop.wait()
        finally:
            # Unpublished first: a `PUT /api/rover/ntrip` arriving now gets the 409 for a rover
            # that is not running instead of starting a client this teardown would miss.
            self.rover = None
            # Taken, not just read: a URL change already waiting on this same task sees the
            # rover gone when it resumes and starts nothing, so there is no newer task to lose.
            ntrip_task, self._ntrip_task = self._ntrip_task, None
            if ntrip_task is not None:
                ntrip_task.cancel()
                await asyncio.gather(ntrip_task, return_exceptions=True)
            if rover is not None:
                rover.collector.stop()
                for publisher in (rover.nmea, rover.json_udp):
                    if publisher is not None:
                        publisher.stop()
            for task in tasks:
                if task.get_name() == "pty-link":
                    task.cancel()  # it polls until stop; the others end on their own
            await self._stop_consumers(tasks)

    async def _link_pty(self, nmea: NmeaPublisher) -> None:
        """Keep `DATA_DIR/ttyMTRTK` pointing at the NMEA pseudo-terminal's slave.

        The slave exists only once the sink has started, and a sink that fails is restarted on
        a new pty, so the link follows it rather than being made once. Docker users mount the
        data directory and point their NMEA consumer at `data/ttyMTRTK`. The link is removed
        on the way out: one left behind would name a pty that is gone (or someone else's).
        """
        link = self.settings.data_dir / PTY_LINK_NAME
        linked: str | None = None
        try:
            while not self.stop.is_set():
                slave = next(
                    (
                        path
                        for sink in nmea.sinks
                        if (path := getattr(sink, "slave_path", None)) is not None
                    ),
                    None,
                )
                if slave is not None and slave != linked:
                    try:
                        if link.is_symlink() or link.exists():
                            link.unlink()
                        os.symlink(slave, link)
                        linked = slave
                        log.info("NMEA pseudo-terminal linked at %s -> %s", link, slave)
                    except OSError as exc:
                        log.warning("could not link %s to %s: %s", link, slave, exc)
                        linked = slave  # do not retry (and warn) every poll
                await sleep_or_stop(self.stop, PTY_LINK_POLL_S)
        finally:
            if linked is not None:
                with contextlib.suppress(OSError):
                    if os.readlink(link) == linked:
                        link.unlink()

    async def _restart_ntrip(self, url: str) -> None:
        """Start an NTRIP client on *url*, stopping the one running now.

        The URL is parsed before anything is stopped, so a bad one raises `ValueError` and
        leaves the running client alone.
        """
        async with self._ntrip_lock:
            rover = self.rover
            if rover is None:
                raise RuntimeError("the rover role is not running")
            config = NtripClientConfig.from_url(url)
            if not rover.driver.capabilities.accepts_rtcm:
                # A VN-200 without INS_VN_RTCM=1: pulling corrections would only drop them.
                await self._note_no_corrections(rover.driver.name)
                return
            # Left in place while it hangs up, so a teardown starting meanwhile still finds,
            # cancels and awaits it.
            old = self._ntrip_task
            if old is not None:
                old.cancel()
                await asyncio.gather(old, return_exceptions=True)
                if self._ntrip_task is old:
                    self._ntrip_task = None
            if self.rover is not rover:
                # The rover tore down (or was restarted) while the old client hung up: a
                # client started now would belong to nobody and never be stopped.
                raise RuntimeError("the rover role stopped while the NTRIP URL was changing")
            self._start_ntrip(rover, config)

    async def _note_no_corrections(self, driver: str) -> None:
        """Say once, in the event log, why a configured NTRIP_URL is not being used."""
        message = (
            f"NTRIP client not started: corrections not supported by driver {driver} "
            "(set INS_VN_RTCM=1 to forward RTCM to a VectorNav unit)"
        )
        log.info(message)
        await self._note_event("info", "ntrip_unsupported", message)

    async def _note_event(self, level: Level, kind: str, message: str) -> None:
        """One event-log entry; the log is a courtesy here, the rover runs on regardless."""
        try:
            event = await EventsRepo(self.db).add(level, kind, message)
        except Exception:
            log.exception("could not record the %s event", kind)
            return
        self.bus.publish("events.new", event)

    def _start_ntrip(self, rover: RoverServices, config: NtripClientConfig) -> None:
        client = NtripClient(
            config,
            self.bus,
            rover.driver,
            gga_provider=self._gga_for_caster,
            gga_interval_s=self.settings.ntrip_gga_interval_s,
        )
        rover.ntrip_client = client
        self._ntrip_task = asyncio.create_task(
            self._supervise("ntrip-client", lambda: client.run(self.stop)), name="ntrip-client"
        )

    async def set_ntrip_url(self, url: str) -> None:
        """Point the rover's NTRIP client at another caster (the web API's `PUT /ntrip`)."""
        await self._restart_ntrip(url)

    def _gga_for_caster(self) -> bytes | None:
        """The rover's position for VRS casters; None until there is a fix to report (an empty
        quality-0 GGA can get an odd answer from a nearest-base or VRS caster)."""
        state = self.store.state
        return build_gga(state) if has_valid_fix(state) else None

    async def _supervise(self, name: str, factory: ConsumerFactory) -> None:
        """Run one consumer until it returns, restarting it with backoff if it raises.

        A consumer that returns is done (the caster after `stop`, the replay's raw logger after
        EOF); only a failure is retried, and the backoff never outlives shutdown.
        """
        backoff = SUPERVISE_BACKOFF_START_S
        while not self.stop.is_set():
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("consumer %s failed", name)
                self.bus.publish(
                    "daemon.consumer_failed",
                    {"name": name, "error": f"{type(exc).__name__}: {exc}"},
                )
                await sleep_or_stop(self.stop, backoff)
                backoff = min(backoff * 2, SUPERVISE_BACKOFF_MAX_S)

    async def _stop_consumers(self, tasks: list[asyncio.Task[None]]) -> None:
        """Wind the consumers down: `stop` is already set, so give them a chance to finish
        their own cleanup (close the raw log, hang up the rovers) before cancelling."""
        if not tasks:
            return
        try:
            _, pending = await asyncio.wait(set(tasks), timeout=CONSUMER_SHUTDOWN_GRACE_S)
            for task in pending:
                log.warning("%s did not stop in time; cancelling it", task.get_name())
        finally:
            # Also the path where the caller itself is cancelled: nothing may be left running.
            for task in tasks:
                task.cancel()  # a no-op on the ones that already finished
            await asyncio.gather(*tasks, return_exceptions=True)

    # --------------------------------------------------------------------- run
    def _request_stop(self, trigger: str) -> None:
        """Set `stop`, remembering what asked for it so the shutdown can say so in the log."""
        self._stop_trigger = self._stop_trigger or trigger
        self.stop.set()

    async def _state_loop(self) -> None:
        async for topic, frame in self._raw_sub:
            if topic in LINK_EVENTS:
                # An epoch inferred for a stream without NAV-EOE must not straddle a link change.
                # A file replay that reached its end is whole up to its last frame, and that
                # epoch has no NAV-EOE to close it. Any other disconnect cut the open epoch: a
                # live link, and also a replay stopped (or timed out) mid-file, which a paced
                # read leaves with only the epoch's NAV-PVT delivered.
                ended = topic == "receiver.disconnected" and frame == SOURCE_ENDED
                if ended and self.settings.source_is_file:
                    self.store.end_of_stream()
                else:
                    self.store.reset_epoch_inference()
                continue
            self.store.apply(frame)
        # Nothing to close here: a shutdown cuts the epoch it lands in, so that one is not
        # published, and a replay's last epoch was closed by its SOURCE_ENDED above.

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
                loop.add_signal_handler(sig, self._request_stop, signal.Signals(sig).name)
        # Startup is inside the `try` as well: a database or a recovery pass that fails must
        # still close what it opened and cancel whatever was already started.
        loops: list[asyncio.Task[None]] = []
        consumer_tasks: list[asyncio.Task[None]] = []
        ended = False  # the receiver run returned by itself: a replay reached its end
        try:
            self.settings.data_dir.mkdir(parents=True, exist_ok=True)
            await self.db.open()
            # After `db.open()`: `restore()` reads and rewrites the rows the last run left in
            # flight, so it needs the database - and the web layer needs the runner.
            self.jobs = JobRunner(self.db, self.bus, self.settings.data_dir / "jobs")
            await self.jobs.restore()
            await asyncio.to_thread(_clear_export_work_dirs, self.settings.data_dir)
            # Built here rather than on the web consumer's first attempt: the context carries the
            # runner, so which of the two comes first stops being something to get right. It is
            # also what `uptime_s` is measured from, and a supervised web restart must not move it.
            self._app_context()
            recovered = recover_incomplete(self.settings.data_dir)
            if recovered:
                log.info("recovered %d incomplete raw log(s) from a previous run", len(recovered))
            loops = [
                asyncio.create_task(self._state_loop(), name="state-loop"),
                asyncio.create_task(self._events_loop(), name="receiver-events"),
            ]
            # Started before the controller so no consumer misses the first frames off the wire.
            consumer_tasks = [
                asyncio.create_task(self._supervise(name, factory), name=f"consumer-{name}")
                for name, factory in self._consumers()
            ]
            if self.ins is not None:
                ins_stop = self.stop
                loops += [
                    asyncio.create_task(task(ins_stop), name=f"ins-extra-{i}")
                    for i, task in enumerate(self.ins.extra_tasks)
                ]
                receiver_run = self.ins.controller.run(self.stop)
            else:
                assert self.controller is not None
                receiver_run = self.controller.run(self.stop)
            controller_task = asyncio.create_task(receiver_run, name="receiver")
            await controller_task  # returns on EOF (replay) or when stop is set
            ended = not self.stop.is_set()
        except BaseException as exc:  # a strict profile failure ends the process
            # A cancellation is somebody shutting this daemon down, not the daemon failing:
            # `error` is what the caller reports as the reason the process is going away.
            if not isinstance(exc, asyncio.CancelledError):
                self.error = exc
            self._stop_trigger = self._stop_trigger or type(exc).__name__
            raise
        finally:
            if ended and loops:
                # A replay's last epoch is published as the state loop drains, when the file
                # never carried NAV-EOE to close it. Consumers are told to stop only once it is
                # out, or the sampler (History) and the WebSocket never see it.
                self._raw_sub.close()
                await asyncio.gather(loops[0], return_exceptions=True)
            self.stop.set()
            log.info("shutting down (%s)", self._stop_trigger or "the receiver run ended")
            self._raw_sub.close()  # state loop drains what is queued, then exits
            self._events_sub.close()
            await asyncio.gather(*loops, return_exceptions=True)
            await self._stop_consumers(consumer_tasks)
            if self.ins is not None:
                self.ins.close()  # the opaque raw capture's open hour and its sidecar
            if self.jobs is not None:
                # Before `db.close()`: a cancelled job writes its own `failed` row on the way out.
                await self.jobs.shutdown()
            await self.db.close()  # last: every consumer that writes to it has stopped
            log.info("mtrtk stopped")


def _clear_export_work_dirs(data_dir: Path) -> None:
    """What a download that died with the last run left in `DATA_DIR/tmp` (see `restore`)."""
    from mtrtk.web.api.export import clear_work_dirs

    if removed := clear_work_dirs(data_dir):
        log.info("removed %d export working dir(s) left by the last run", removed)
