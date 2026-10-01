"""Vendor-neutral INS plumbing shared by the SBG Ellipse and VectorNav drivers.

- `InsController`: serial lifecycle (open, read, route, silence watchdog, reconnect with
  backoff), one serialised writer, and request/response correlation on routed frames.
- `StateAdapter`: base for the vendor adapters that turn frames into `ReceiverState`, with the
  same publishing contract as the u-blox `StateStore` (sections, decimated `state.epoch`).
- `RawCapture`: hourly opaque capture of a vendor raw stream with a JSON sidecar.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame, FrameSplitter
from mtrtk.core.router import Router
from mtrtk.core.source import ByteSource
from mtrtk.core.state import MAX_TIME_MARKS, ReceiverState, TimeMark

log = logging.getLogger(__name__)

Configure = Callable[["InsController"], Awaitable[None]]
Matcher = Callable[[Frame], bool]

BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 30.0
WRITE_TIMEOUT_S = 5.0  # a write that has not drained by then is a wedged port
SOURCE_ENDED = "source ended"  # the disconnect reason alerts treat as the end of a replay
PENDING_CAP_BYTES = 8 * 1024 * 1024  # buffered before the unit reports a time
BACKSTEP_HOLD_S = 5.0  # a unit clock earlier than the open hour must stay there this long
SIDECAR_EVERY_S = 10.0  # refresh an open hour's sidecar this often (crash safety)
MIN_VALID_YEAR = 2020  # an earlier unit clock is an unsynced epoch (1980, 2000), not UTC


def _mono() -> float:  # indirection points for the tests
    return time.monotonic()


def _host_now() -> datetime:
    return datetime.now(UTC)


def _floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


class InsController:
    """Owns one INS serial connection for as long as `run(stop)` runs.

    Per connection: open the source, publish `receiver.connected`, start `configure` (if any)
    alongside the reader, route bytes through a fresh vendor framer until EOF, a link error or
    `rx_timeout_s` of silence, then publish `receiver.disconnected` and reconnect with a
    jittered 1 -> 30 s backoff. A configure failure is reported as `receiver.error` and the
    connection is kept, so a unit that refuses a setting still streams read-only.

    As `ReceiverController`: a source with `ends_at_eof` (a recording) that runs out ends the
    run with reason "source ended", and any unexpected exception in the session (a framer or
    namer bug) is reported as `receiver.error` and reconnected, never raised out of `run`.
    """

    def __init__(
        self,
        bus: Bus,
        source_factory: Callable[[], ByteSource],
        framer_factory: Callable[[], FrameSplitter],
        configure: Configure | None,
        rx_timeout_s: float = 5.0,
    ) -> None:
        self.bus = bus
        self.source_factory = source_factory
        self.framer_factory = framer_factory
        self.configure = configure
        self.rx_timeout_s = rx_timeout_s
        self.write_timeout_s = WRITE_TIMEOUT_S
        self.backoff_s: tuple[float, float] = (BACKOFF_MIN_S, BACKOFF_MAX_S)
        self.connected = False
        self.router: Router | None = None
        self.source: ByteSource | None = None
        self.stats: dict[str, int] = {"bytes_in": 0, "frames": 0, "reconnects": 0, "writes": 0}
        self._write_lock = asyncio.Lock()
        self._waiters: list[tuple[Matcher, asyncio.Future[Frame]]] = []
        self._last_rx = 0.0

    # ------------------------------------------------------------- lifecycle
    async def run(self, stop: asyncio.Event) -> None:
        delay = self.backoff_s[0]
        while not stop.is_set():
            source = self.source_factory()
            try:
                await source.open()
            except Exception as exc:  # serial errors derive from OSError / ValueError
                log.warning("cannot open %s: %s (retry in %.0fs)", source.name, exc, delay)
                self.bus.publish("receiver.error", f"open failed: {exc}")
                await self._sleep(delay, stop)
                delay = min(delay * 2, self.backoff_s[1])
                continue
            delay = self.backoff_s[0]  # a session was established: a fresh backoff ladder
            ended = await self._session(source, stop)
            if ended or stop.is_set():
                return
            self.stats["reconnects"] += 1
            await self._sleep(delay, stop)
            delay = min(delay * 2, self.backoff_s[1])

    async def _session(self, source: ByteSource, stop: asyncio.Event) -> bool:
        """One connection. True when the source ended for good (a finished recording)."""
        self.source, self.router = source, Router(self.bus, self.framer_factory())
        self.connected = True
        self.bus.publish("receiver.connected", source.name)
        cfg_task = (
            asyncio.create_task(self._configure_safely(), name="ins-configure")
            if self.configure is not None
            else None
        )
        reason = "stopped"
        try:
            reason = await self._watch(source, stop)
        except Exception as exc:  # one bad frame must never end the supervisor
            reason = f"unexpected failure: {exc!r}"
            log.exception("unexpected INS failure")
            self.bus.publish("receiver.error", reason)
        finally:
            if cfg_task is not None:
                cfg_task.cancel()
                await asyncio.gather(cfg_task, return_exceptions=True)
            self.connected = False  # new writers bail out from here on
            self._fail_waiters(ConnectionError(reason))
            # Never close the port under a write in progress (an RTCM chunk from the NTRIP
            # client, say): wait for it. `write_timeout_s` bounds that wait.
            async with self._write_lock:
                self.source = None
                try:
                    await source.close()
                except Exception:  # an unplugged device can fail to close; reconnect anyway
                    log.debug("closing %s failed", source.name, exc_info=True)
            self.bus.publish("receiver.disconnected", reason)
        return reason == SOURCE_ENDED

    async def _configure_safely(self) -> None:
        assert self.configure is not None
        try:
            await self.configure(self)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("INS configuration failed")
            self.bus.publish("receiver.error", f"configuration failed: {exc}")

    async def _watch(self, source: ByteSource, stop: asyncio.Event) -> str:
        """Run one long-lived reader and watch it for silence; return the disconnect reason.

        One reader task per session, not one per chunk: at 921600 baud the unit sends
        hundreds of chunks a second. The watchdog sleeps until the silence deadline, which
        moves on every chunk (`_last_rx`). An exception the reader does not handle itself
        (a framer or namer bug) propagates to `_session`.
        """
        self._last_rx = time.monotonic()
        reader = asyncio.create_task(self._reader(source), name="ins-read")
        stopping = asyncio.ensure_future(stop.wait())
        try:
            while True:
                left = self._last_rx + self.rx_timeout_s - time.monotonic()
                if left <= 0:
                    return f"no data for {self.rx_timeout_s:g}s"
                done, _ = await asyncio.wait(
                    {reader, stopping}, timeout=left, return_when=asyncio.FIRST_COMPLETED
                )
                if reader in done:
                    return reader.result()
                if stopping in done:
                    return "stopped"
        finally:
            reader.cancel()
            stopping.cancel()
            await asyncio.gather(reader, stopping, return_exceptions=True)

    async def _reader(self, source: ByteSource) -> str:
        router = self.router
        assert router is not None
        while True:
            try:
                data = await source.read()
            except Exception as exc:  # unplugged, permission revoked, ...
                log.warning("INS link failure: %s", exc)
                self.bus.publish("receiver.error", f"link failure: {exc}")
                return f"link failure: {exc}"
            if not data:
                # A recording that ran out is the expected end of a run: alerts stay quiet
                # for "source ended", as for `ReceiverController`. A live port reconnects.
                return SOURCE_ENDED if getattr(source, "ends_at_eof", False) else "eof"
            self._last_rx = time.monotonic()
            self.stats["bytes_in"] += len(data)
            for frame in router.feed(data):
                self.stats["frames"] += 1
                self._resolve_waiters(frame)

    # ------------------------------------------------------------- writing
    async def write(self, data: bytes) -> None:
        """Write to the unit. One writer at a time: RTCM injection and configuration commands
        must never interleave inside a frame. A write that does not drain within
        `write_timeout_s` (a wedged port) raises `ConnectionError` and frees the lock."""
        if not self.connected or self.source is None:
            raise ConnectionError("INS not connected")
        async with self._write_lock:
            source = self.source
            if source is None or not self.connected:
                raise ConnectionError("INS not connected")
            try:
                await asyncio.wait_for(source.write(data), self.write_timeout_s)
            except TimeoutError as exc:
                raise ConnectionError(f"INS write stalled for {self.write_timeout_s:g}s") from exc
            self.stats["writes"] += 1

    async def request(self, match: Matcher, send: bytes, timeout_s: float = 2.0) -> Frame:
        """Write *send* and return the first routed frame that *match* accepts.

        The waiter is registered before the write, so a reply that races the write is not
        missed. `TimeoutError` when nothing matches in time; `ConnectionError` when the link
        drops first.
        """
        fut: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
        entry = (match, fut)
        self._waiters.append(entry)
        try:
            await self.write(send)
            return await asyncio.wait_for(fut, timeout_s)
        finally:
            with contextlib.suppress(ValueError):
                self._waiters.remove(entry)
            if fut.done() and not fut.cancelled():
                # The link dropped while the write was still running: `_fail_waiters` set
                # the future's exception and `write()` raised its own. Mark it retrieved, or
                # asyncio logs "Future exception was never retrieved" on every such unplug.
                fut.exception()

    def _resolve_waiters(self, frame: Frame) -> None:
        for match, fut in list(self._waiters):
            if fut.done():
                continue
            try:
                hit = match(frame)
            except Exception:  # a matcher that chokes on an unrelated frame is not a match
                hit = False
            if hit:
                fut.set_result(frame)

    def _fail_waiters(self, exc: Exception) -> None:
        for _, fut in self._waiters:
            if not fut.done():
                fut.set_exception(exc)
        self._waiters.clear()

    async def _sleep(self, delay: float, stop: asyncio.Event) -> None:
        """Wait out *delay* (jittered +-20 %), returning early when `stop` fires."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), delay * random.uniform(0.8, 1.2))


class StateAdapter:
    """Vendor adapters subclass this and implement `apply(frame) -> changed sections`.

    Publishing matches `StateStore`: `state.<section>` per changed section, `state.epoch` as a
    deep copy (consumers queue it and read it later), `state.time_mark` per new rising edge.
    INS units emit navigation far faster than the UI, sampler and NMEA outputs want it, so
    `state.epoch` is decimated to `nav_hz_cap`; `epoch_count` still counts every epoch.
    """

    def __init__(
        self, bus: Bus, state: ReceiverState | None = None, nav_hz_cap: float = 5.0
    ) -> None:
        if not nav_hz_cap > 0:  # also rejects NaN
            raise ValueError(f"nav_hz_cap must be > 0 Hz, got {nav_hz_cap}")
        self.bus = bus
        self.state = state if state is not None else ReceiverState()
        self.nav_hz_cap = nav_hz_cap
        self._last_epoch_pub: float | None = None

    def apply(self, frame: Frame) -> set[str]:
        raise NotImplementedError

    def handle(self, frame: Frame) -> None:
        """Apply and publish; a malformed frame is logged, never raised into the read loop."""
        try:
            changed = self.apply(frame)
        except Exception:
            log.exception("failed to apply %s", frame.identity)
            return
        self.publish_sections(changed)

    def publish_sections(self, changed: set[str]) -> None:
        for section in sorted(changed):
            self.bus.publish(f"state.{section}", getattr(self.state, section))

    def end_epoch(self, now_mono: float | None = None) -> None:
        now = time.monotonic() if now_mono is None else now_mono
        self.state.epoch_count += 1
        self.state.last_epoch_mono = now
        rtk = self.state.rtk
        if rtk.last_rtcm_mono is not None:
            rtk.corr_age_s = max(0.0, now - rtk.last_rtcm_mono)
        if self._last_epoch_pub is not None and now - self._last_epoch_pub < (
            1.0 / self.nav_hz_cap - 1e-6
        ):
            return
        self._last_epoch_pub = now
        if rtk.last_rtcm_mono is not None:
            self.bus.publish("state.rtk", rtk)
        self.bus.publish("state.epoch", self.state.model_copy(deep=True))

    def note_rtcm_injected(self, now_mono: float | None = None) -> None:
        """Record that RTCM corrections were just written to the unit (the NTRIP client)."""
        self.state.rtk.last_rtcm_mono = time.monotonic() if now_mono is None else now_mono

    def push_time_mark(self, mark: TimeMark) -> None:
        marks = self.state.time_marks
        marks.append(mark)
        if len(marks) > MAX_TIME_MARKS:
            del marks[: len(marks) - MAX_TIME_MARKS]
        if mark.new_rising:
            self.bus.publish("state.time_mark", mark)
        self.publish_sections({"time_marks"})


class RawCapture:
    """Hourly opaque capture of a vendor raw stream, e.g. VectorNav raw measurements.

    Files are `<root>/ins/YYYY/DDD/{STATION}_{YYYYMMDD}_{HH}.{suffix}` with a `.json` sidecar.
    Rotation keys on the unit's own UTC (`note_utc`), as `RawLogWriter` does on NAV-PVT; bytes
    written before the first reading are buffered, and past `PENDING_CAP_BYTES` the file is
    named by the host clock instead (`time_source: "host"`). Reopening an hour after a restart
    appends and carries the sidecar's counters forward.

    Between readings the last one is projected forward on the monotonic clock (the host clock
    in host mode), so a unit that goes quiet still has its hours closed on time. A reading
    that steps back across the hour boundary is only followed once it has held for
    `BACKSTEP_HOLD_S`, so a clock correction does not flip between two files. The sidecar of
    the open hour is rewritten every `SIDECAR_EVERY_S`.
    """

    def __init__(self, root: Path, station_id: str, suffix: str, bus: Bus, *, vendor: str) -> None:
        self.root = Path(root)
        self.station_id = station_id
        self.suffix = suffix
        self.bus = bus
        self.vendor = vendor
        self._utc: datetime | None = None
        self._utc_mono: float | None = None  # monotonic time of `_utc`, for projecting it
        self._back_since: datetime | None = None  # first reading of a step back, if held
        self._warned_implausible = False
        self._side_dumped = 0.0
        self._hour: datetime | None = None
        self._time_source = "receiver"
        self._pending = bytearray()
        self._pending_frames = 0
        self._fh: IO[bytes] | None = None
        self._path: Path | None = None
        self._side: dict[str, Any] = {}

    @property
    def current_path(self) -> Path | None:
        return self._path if self._fh is not None else None

    def path_for(self, hour: datetime) -> Path:
        name = f"{self.station_id}_{hour:%Y%m%d}_{hour:%H}.{self.suffix}"
        return self.root / "ins" / f"{hour:%Y}" / f"{hour:%j}" / name

    def note_utc(self, dt: datetime) -> None:
        """Set the unit clock that rotation keys on.

        Pass only UTC the unit itself vouches for (the vendor's UTC-valid / time-status flag
        set): before GNSS sync an INS reports a free-running clock, and naming files by it
        would resume or append into hours of 1980 or 2000. As a backstop a reading before
        `MIN_VALID_YEAR` is dropped (warned once). A naive datetime is taken as UTC.
        """
        utc = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
        if utc.year < MIN_VALID_YEAR:
            if not self._warned_implausible:
                self._warned_implausible = True
                log.warning("%s: ignoring implausible unit time %s", self.vendor, utc.isoformat())
            return
        hour = _floor_hour(utc)
        if self._fh is not None and self._hour is not None and hour < self._hour:
            if self._back_since is None:
                self._back_since = utc
            if utc - self._back_since < timedelta(seconds=BACKSTEP_HOLD_S):
                return  # a correction across the boundary: keep filing into the open hour
        self._back_since = None
        self._utc, self._utc_mono = utc, _mono()
        self._time_source = "receiver"
        if self._fh is None or self._hour != hour:
            self._rotate(hour)

    def write(self, data: bytes) -> None:
        if self._fh is None:
            self._pending += data
            self._pending_frames += 1
            if len(self._pending) > PENDING_CAP_BYTES:
                log.warning(
                    "%s: no unit time after %d buffered bytes; naming by host clock",
                    self.vendor,
                    len(self._pending),
                )
                self._utc, self._utc_mono = _host_now(), _mono()
                self._time_source = "host"
                self._rotate(_floor_hour(self._utc))
            return
        now = _mono()
        self._advance_hour(now)
        assert self._fh is not None
        self._fh.write(data)
        self._side["bytes"] += len(data)
        self._side["frames"] += 1
        if now - self._side_dumped >= SIDECAR_EVERY_S:
            self.flush()

    def _advance_hour(self, now: float) -> None:
        """Close the open hour once the clock has moved past it with no reading saying so."""
        if self._time_source == "host":
            utc = _host_now()
        elif self._utc is not None and self._utc_mono is not None:
            utc = self._utc + timedelta(seconds=now - self._utc_mono)
        else:
            return
        hour = _floor_hour(utc)
        if self._hour is not None and hour > self._hour:
            self._utc, self._utc_mono = utc, now
            self._rotate(hour)

    def _rotate(self, hour: datetime) -> None:
        self.close()
        path = self.path_for(hour)
        path.parent.mkdir(parents=True, exist_ok=True)
        side: dict[str, Any] = {
            "vendor": self.vendor,
            "station_id": self.station_id,
            "hour_utc": hour.isoformat(),
            "start_utc": self._utc.isoformat() if self._utc else None,
            "end_utc": None,
            "time_source": self._time_source,
            "bytes": 0,
            "frames": 0,
        }
        side_path = path.with_suffix(".json")
        if path.exists() and side_path.exists():  # a restart inside the hour: resume it
            try:
                previous = json.loads(side_path.read_text())
                side["start_utc"] = previous.get("start_utc") or side["start_utc"]
                side["frames"] = int(previous.get("frames", 0))
            except (OSError, ValueError):
                log.warning("unreadable sidecar %s; starting it afresh", side_path)
        side["bytes"] = path.stat().st_size if path.exists() else 0
        self._fh = path.open("ab")
        self._path, self._hour, self._side = path, hour, side
        self._dump_sidecar()
        if self._pending:
            pending, frames = bytes(self._pending), self._pending_frames
            self._pending.clear()
            self._pending_frames = 0
            self._fh.write(pending)
            self._side["bytes"] += len(pending)
            self._side["frames"] += frames
        self.bus.publish("rawcapture.rotated", path)

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            self._dump_sidecar()

    def _dump_sidecar(self) -> None:
        assert self._path is not None
        self._path.with_suffix(".json").write_text(json.dumps(self._side, indent=2))
        self._side_dumped = _mono()

    def close(self) -> None:
        if self._fh is None:
            return
        self._fh.close()
        self._fh = None
        self._side["end_utc"] = self._utc.isoformat() if self._utc else None
        self._dump_sidecar()
