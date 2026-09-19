"""Process supervisor: wires source -> router -> bus -> state per role, runs the role's
consumers under restart supervision, and prints status."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any

from mtrtk.alerts import AlertEngine
from mtrtk.base.basemode import BaseModeManager
from mtrtk.base.ntrip_caster import CasterConfig, NtripCaster
from mtrtk.config import Role, Settings
from mtrtk.core.bus import Bus, Policy
from mtrtk.core.exposure import sleep_or_stop, wait_for_bind
from mtrtk.core.receiver import ReceiverController
from mtrtk.core.router import TOPIC_RAW_RTCM, TOPIC_RAW_UBX
from mtrtk.core.source import ByteSource, FileReplaySource, SerialSource, find_ublox_port
from mtrtk.core.statestore import StateStore
from mtrtk.core.ubx_config import base_profile, rover_profile
from mtrtk.rawlog.retention import RetentionPolicy
from mtrtk.rawlog.writer import RawLogWriter, recover_incomplete
from mtrtk.store.db import Database
from mtrtk.store.repos import EventsRepo, NtripLogRepo, SitesRepo
from mtrtk.store.sampler import Sampler
from mtrtk.system import SystemMonitor

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
        return line


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
        self.db = Database(settings.data_dir / "mtrtk.db")
        self.caster: NtripCaster | None = None
        self.basemode: BaseModeManager | None = None
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
        self.passive = settings.source_is_file if passive is None else passive
        profile = base_profile(settings) if settings.role is Role.BASE else rover_profile(settings)
        self.controller = ReceiverController(
            self.bus,
            source_factory or self._default_source_factory(),
            profile=None if self.passive else profile,
            strict=settings.receiver_strict,
            passive=self.passive,
        )

    # ----------------------------------------------------------------- sources
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
        ]
        if s.role is Role.BASE:
            # Replaying a file must not spend the disk it is being read from, so raw logging is
            # opt-in there (REPLAY_LOG=1) and always on for a live receiver.
            if not s.source_is_file or s.replay_log:
                consumers.append(("rawlog", self._run_rawlog))
                consumers.append(
                    (
                        "retention",
                        lambda: RetentionPolicy(s.data_dir, s.min_free_gb, self.bus).run(stop),
                    )
                )
            consumers.append(("ntrip", self._run_caster))
            if not self.passive:
                # The mode manager writes TMODE to the receiver; a replay has none to write to.
                consumers.append(("basemode", self._run_basemode))
        return consumers

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
            writer.stop()  # closes the subscription -> run() drains it, then closes the file
            meta_sub.close()
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
        s = self.settings
        host = await wait_for_bind(s.ntrip_bind, self.stop)
        if host is None:  # stop was set while waiting for the interface to come up
            return
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

        caster = NtripCaster(
            self.bus,
            config,
            host,
            s.ntrip_port,
            ntrip_log=NtripLogRepo(self.db),
            position=position,
            bitrate=lambda: self.store.state.rtcm_out.bytes_per_s * 8,
        )
        await caster.start()  # published only once it is actually listening
        self.caster = caster
        try:
            await self.stop.wait()
        finally:
            self.caster = None
            await caster.stop()

    async def _run_basemode(self) -> None:
        s = self.settings
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
        # Startup is inside the `try` as well: a database or a recovery pass that fails must
        # still close what it opened and cancel whatever was already started.
        loops: list[asyncio.Task[None]] = []
        consumer_tasks: list[asyncio.Task[None]] = []
        try:
            self.settings.data_dir.mkdir(parents=True, exist_ok=True)
            await self.db.open()
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
            controller_task = asyncio.create_task(self.controller.run(self.stop), name="receiver")
            await controller_task  # returns on EOF (replay) or when stop is set
        except BaseException as exc:  # a strict profile failure ends the process
            self.error = exc
            raise
        finally:
            self.stop.set()
            self._raw_sub.close()  # state loop drains what is queued, then exits
            self._events_sub.close()
            await asyncio.gather(*loops, return_exceptions=True)
            await self._stop_consumers(consumer_tasks)
            await self.db.close()  # last: every consumer that writes to it has stopped
