"""Hourly raw UBX logger. Rotation keyed on receiver UTC (NAV-PVT); JSON sidecar per file."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO

from mtrtk.core.bus import Bus, Policy
from mtrtk.core.frames import Frame, Proto

log = logging.getLogger(__name__)

PENDING_CAP_BYTES = 8 * 1024 * 1024  # buffered before receiver time is known
SIDECAR_UPDATE_S = 60.0
FLUSH_INTERVAL_S = 1.0
BACKPRESSURE_REPEAT_S = 60.0  # one `rawlog.backpressure` per minute while the queue stays high


def _fsync_fd(fd: int) -> None:
    """Only the fsync, never a buffer flush: this runs in a worker thread (see `_maybe_fsync`)
    while the event loop still owns the file object."""
    os.fsync(fd)


def _fsync_and_close(fd: int) -> None:
    """Both steps in the worker thread. Closing the descriptor from the loop - which is what a
    cancelled ticker would do - can land while this thread is still inside `os.fsync` on it,
    and by the time the call returns the number may already name another file."""
    try:
        _fsync_fd(fd)
    finally:
        os.close(fd)


def _fsync_sidecar(fh: IO[str]) -> None:
    """The sidecar is a few kB and is written at most once a minute, so this one stays on the
    calling thread; it is separate only so a test can see it."""
    fh.flush()
    os.fsync(fh.fileno())


def log_path(root: Path, station_id: str, hour_utc: datetime) -> Path:
    name = f"{station_id}_{hour_utc:%Y%m%d}_{hour_utc:%H}.ubx"
    return root / "ubx" / f"{hour_utc:%Y}" / f"{hour_utc:%j}" / name


def sidecar_path(path: Path) -> Path:
    return path.with_suffix(".json")


@dataclass
class Sidecar:
    station_id: str
    role: str
    start_utc: str | None
    end_utc: str | None = None
    hour_utc: str | None = None
    bytes: int = 0
    msg_counts: dict[str, int] = field(default_factory=dict)
    sha256: str | None = None
    firmware: str = ""
    site: str | None = None
    keep: bool = False
    time_source: str = "receiver"  # "receiver" | "host"
    complete: bool = False
    recovered: bool = False
    end_utc_source: str | None = None  # "unknown" where `end_utc` could not be recovered

    def dump(self, path: Path) -> None:
        """Atomic and durable: the temporary file is fsynced before it takes the sidecar's
        name, so a power cut leaves either the old sidecar or the whole new one."""
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w") as fh:
            fh.write(json.dumps(asdict(self), indent=2))
            _fsync_sidecar(fh)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> Sidecar:
        return cls(**json.loads(path.read_text()))


class _OpenLog:
    def __init__(self, path: Path, hour: datetime, sidecar: Sidecar) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.hour = hour
        self.sidecar = sidecar
        self.hasher = hashlib.sha256()
        if path.exists() and path.stat().st_size:  # resumed after a restart inside the same hour
            self.hasher.update(path.read_bytes())
            self.sidecar.bytes = path.stat().st_size
            self._carry_over(sidecar_path(path))
        self.fh = path.open("ab")

    def _carry_over(self, sc_path: Path) -> None:
        """Adopt the whole sidecar of the file being appended to, so the finished sidecar
        describes the whole file and not just the part written since the restart.

        Everything the restarted writer cannot know again is taken from the previous sidecar:
        the counts, the start time, the `keep` mark an operator may have set, the firmware and
        site last seen, and the clock the file was named by (the name does not change, so
        neither may `time_source`)."""
        self.sidecar.recovered = True
        try:
            previous = Sidecar.load(sc_path)
        except (OSError, TypeError, ValueError):
            log.warning("no usable sidecar at %s; counts restart from zero", sc_path)
            return
        self.sidecar.msg_counts = dict(previous.msg_counts)
        self.sidecar.start_utc = previous.start_utc or self.sidecar.start_utc
        self.sidecar.keep = previous.keep or self.sidecar.keep
        # Live metadata wins: the daemon has just told this writer what the firmware and the
        # site are *now*. The previous sidecar only fills in what the new writer cannot know.
        self.sidecar.firmware = self.sidecar.firmware or previous.firmware
        self.sidecar.site = self.sidecar.site or previous.site
        self.sidecar.time_source = previous.time_source or self.sidecar.time_source
        # `recovered` needs no carry-over: resuming a file is itself a recovery (set above).

    def write(self, raw: bytes, identity: str) -> None:
        self.fh.write(raw)
        self.hasher.update(raw)
        self.sidecar.bytes += len(raw)
        self.sidecar.msg_counts[identity] = self.sidecar.msg_counts.get(identity, 0) + 1

    def write_bulk(self, raw: bytes, counts: dict[str, int]) -> None:
        """Write the buffer held while the receiver clock was still unknown."""
        self.fh.write(raw)
        self.hasher.update(raw)
        self.sidecar.bytes += len(raw)
        for identity, count in counts.items():
            self.sidecar.msg_counts[identity] = self.sidecar.msg_counts.get(identity, 0) + count

    def flush(self) -> None:
        self.fh.flush()

    def fsync(self) -> None:
        self.fh.flush()
        _fsync_fd(self.fh.fileno())

    def dup_fd(self) -> int:
        """A duplicate descriptor for the worker thread: it keeps the open file description
        alive, so an fsync still in flight when the hour rotates cannot land on a reused fd."""
        return os.dup(self.fh.fileno())

    def dump_sidecar(self) -> None:
        self.sidecar.dump(sidecar_path(self.path))

    def close(self, end_utc: datetime | None) -> None:
        """Best-effort: every step is guarded, so a dying disk still finalizes what it can
        and never masks the failure that is ending the log."""
        for what, step in (("fsync", self.fsync), ("close", self.fh.close)):
            try:
                step()
            except OSError:
                log.warning("ignoring %s failure on %s", what, self.path, exc_info=True)
        self.sidecar.end_utc = end_utc.isoformat() if end_utc else None
        self.sidecar.sha256 = self.hasher.hexdigest()
        self.sidecar.complete = True
        try:
            self.dump_sidecar()
        except OSError:
            log.warning("could not write the sidecar for %s", self.path, exc_info=True)


class RawLogWriter:
    def __init__(
        self,
        bus: Bus,
        root: Path,
        station_id: str,
        messages: list[str],
        role: str = "base",
        fsync_interval_s: int = 10,
        firmware: str = "",
        site: str | None = None,
    ) -> None:
        self.bus = bus
        self.root = Path(root)
        self.station_id = station_id
        self.filter = frozenset(messages)
        self.role = role
        self.fsync_interval_s = fsync_interval_s
        self._current: _OpenLog | None = None  # before the metadata setters, which write to it
        self._firmware = firmware
        self._site = site
        self.sub = bus.subscribe("raw.ubx", policy=Policy.UNBOUNDED, high_water=2000)
        self._pending = bytearray()
        self._pending_counts: dict[str, int] = {}
        self._utc: datetime | None = None  # receiver clock: the only thing rotation keys on
        self._utc_mono: float | None = None  # monotonic stamp of that reading
        self._closed_hour: datetime | None = None  # the last hour the ticker finalised
        self._time_source = "receiver"
        self._last_fsync = time.monotonic()
        self._last_sidecar = time.monotonic()
        self._fsync_pending = False
        self._backpressure_active = False
        self._backpressure_last = 0.0
        self._clamped_second = False

    # ------------------------------------------------------------ properties
    @property
    def current_path(self) -> Path | None:
        return self._current.path if self._current else None

    @property
    def receiver_utc(self) -> datetime | None:
        return self._utc

    @property
    def firmware(self) -> str:
        return self._firmware

    @firmware.setter
    def firmware(self, value: str) -> None:
        """Late metadata still belongs to the hour it arrives in: the open sidecar is updated
        too, so the next periodic dump persists it instead of waiting for the next file."""
        self._firmware = value
        if self._current is not None:
            self._current.sidecar.firmware = value

    @property
    def site(self) -> str | None:
        return self._site

    @site.setter
    def site(self, value: str | None) -> None:
        self._site = value
        if self._current is not None:
            self._current.sidecar.site = value

    # ------------------------------------------------------------- sync core
    def handle(self, frame: Frame) -> None:
        if frame.proto is not Proto.UBX:
            return
        identity = frame.identity
        if identity == "NAV-PVT":
            self._update_time(frame)
        if identity not in self.filter:
            return
        if self._utc is None:
            self._buffer_pending(frame.raw, identity)
            return
        hour = self._utc.replace(minute=0, second=0, microsecond=0)
        if self._closed_hour is not None and hour <= self._closed_hour:
            # The ticker has already finalised that hour. A receiver that keeps repeating a
            # stale timestamp instead of dropping validity must not reopen it: the file would
            # be read back, re-hashed and closed again on every tick from here on.
            hour = self._closed_hour + timedelta(hours=1)
        if self._current is None or self._current.hour != hour:
            self._rotate(hour)
        assert self._current is not None
        self._current.write(frame.raw, identity)

    def _update_time(self, frame: Frame) -> None:
        m = frame.parsed()
        if m.validDate and m.validTime:
            second = self._clamp_second(m.second)
            self._utc = datetime(m.year, m.month, m.day, m.hour, m.min, second, tzinfo=UTC)
            self._utc_mono = time.monotonic()
            # A receiver that finally got time takes the naming back from the host clock; the
            # file already named by the host keeps its own sidecar's time_source="host".
            self._time_source = "receiver"

    def _clamp_second(self, second: int) -> int:
        """u-blox documents NAV-PVT `sec` as 0..60: a leap second must not kill the logger."""
        if 0 <= second <= 59:
            return second
        if not self._clamped_second:
            self._clamped_second = True
            log.info("NAV-PVT second=%d outside 0..59 (leap second?); clamping", second)
        return min(max(second, 0), 59)

    def _buffer_pending(self, raw: bytes, identity: str) -> None:
        self._pending += raw
        self._pending_counts[identity] = self._pending_counts.get(identity, 0) + 1
        if len(self._pending) > PENDING_CAP_BYTES:
            log.warning(
                "no receiver time after %d buffered bytes; naming file by host clock",
                len(self._pending),
            )
            self._utc = datetime.now(UTC)
            self._utc_mono = time.monotonic()
            self._time_source = "host"
            self._rotate(self._utc.replace(minute=0, second=0, microsecond=0))

    def _rotate(self, hour: datetime) -> None:
        if self._current is not None:
            self._close_current()
        path = log_path(self.root, self.station_id, hour)
        sidecar = Sidecar(
            station_id=self.station_id,
            role=self.role,
            start_utc=self._utc.isoformat() if self._utc else None,
            hour_utc=hour.isoformat(),
            firmware=self.firmware,
            site=self.site,
            time_source=self._time_source,
        )
        self._current = _OpenLog(path, hour, sidecar)
        if self._pending:
            self._current.write_bulk(bytes(self._pending), self._pending_counts)
            self._pending.clear()
            self._pending_counts.clear()
        self._current.dump_sidecar()
        log.info("logging to %s", path)
        self.bus.publish("rawlog.rotated", path)

    def _close_current(self) -> None:
        assert self._current is not None
        current, self._current = self._current, None  # dropped first: never written to again
        current.close(self._utc)
        self.bus.publish("rawlog.closed", current.path)

    def tick(self, now_mono: float) -> None:
        """Called about once per second: close an elapsed hour, flush, refresh the sidecar.

        The fsync is deliberately not here - it runs off the loop, from `_maybe_fsync`.
        """
        if self._current is None:
            return
        self._close_elapsed_hour(now_mono)
        if self._current is None:
            return
        self._current.flush()
        if now_mono - self._last_sidecar >= SIDECAR_UPDATE_S:
            self._current.dump_sidecar()
            self._last_sidecar = now_mono

    def _close_elapsed_hour(self, now_mono: float) -> None:
        """Rotation is otherwise write-driven, so a receiver that goes quiet at 16:59 would
        leave that hour open and unfinalised until it speaks again. Project the last receiver
        UTC forward by the monotonic time since it was read and close the hour when it ends."""
        assert self._current is not None
        if self._utc is None or self._utc_mono is None:
            return
        elapsed = now_mono - self._utc_mono
        if elapsed <= 0:
            return
        if self._utc + timedelta(seconds=elapsed) < self._current.hour + timedelta(hours=1):
            return
        log.info("no frames since %s; closing the hour %s", self._utc, self._current.hour)
        closed = self._current.hour
        self._close_current()  # `end_utc` is the last real reading, so advance only after it
        # The projected clock is what the next frame names a file by. Left at the stale
        # reading it names the hour just finalised, and `handle()` reopens it - so a receiver
        # that stops stamping time while data still flows churns the file once per tick.
        self._utc += timedelta(seconds=elapsed)
        self._utc_mono = now_mono
        self._closed_hour = closed

    def close(self) -> None:
        if self._current is not None:
            self._close_current()

    # ------------------------------------------------------------ async run
    def stop(self) -> None:
        """End `run()`: the queued frames still drain before the loop exits."""
        self.bus.unsubscribe(self.sub)

    def _report(self, what: str, exc: BaseException) -> None:
        """One bad frame or one bad write must never end the raw log."""
        log.exception("raw log %s failed", what)
        self.bus.publish("rawlog.error", f"{what}: {exc!r}")

    async def run(self, stop: asyncio.Event) -> None:
        ticker = asyncio.create_task(self._ticker(stop), name="rawlog-ticker")
        try:
            async for _, frame in self.sub:
                try:
                    self.handle(frame)
                except Exception as exc:  # a write error must not silence the logger
                    self._report("write", exc)
                self._check_pressure()
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
            if self._backpressure_active:
                # The supervisor may be about to start a replacement writer, and that one will
                # never publish `rawlog.drained` for a queue it never had: without this the
                # `logger_backpressure` alert stays raised for the life of the process.
                self._backpressure_active = False
                self.bus.publish("rawlog.drained", {"queued": self.sub.queue.qsize()})
            try:
                self.close()
            finally:
                self.stop()  # never leave an UNBOUNDED queue behind, whatever ended the loop

    def _check_pressure(self) -> None:
        """One event when the queue first goes over the mark, one a minute while it stays
        there, and one when it has genuinely drained - not one per frame."""
        queued = self.sub.queue.qsize()
        if queued >= self.sub.high_water:
            now = time.monotonic()
            if self._backpressure_active and now - self._backpressure_last < BACKPRESSURE_REPEAT_S:
                return
            self._backpressure_active = True
            self._backpressure_last = now
            log.warning("raw log %d frames behind", queued)
            self.bus.publish("rawlog.backpressure", {"queued": queued})
        elif self._backpressure_active and queued < self.sub.high_water // 2:
            self._backpressure_active = False
            self.bus.publish("rawlog.drained", {"queued": queued})

    async def _maybe_fsync(self, now_mono: float) -> None:
        """An fsync on a stalling SD card blocks for seconds; on the loop that would freeze the
        caster and the state loop with it, so it runs in a worker thread - one at a time."""
        current = self._current
        if current is None or self._fsync_pending:
            return
        if now_mono - self._last_fsync < self.fsync_interval_s:
            return
        self._last_fsync = now_mono
        self._fsync_pending = True
        fd = current.dup_fd()
        try:
            await asyncio.to_thread(_fsync_and_close, fd)
        finally:
            self._fsync_pending = False

    async def _ticker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), FLUSH_INTERVAL_S)
            try:
                self.tick(time.monotonic())
                await self._maybe_fsync(time.monotonic())
            except Exception as exc:  # same for a failing flush / fsync / sidecar dump
                self._report("flush", exc)
        self.stop()  # stop requested: close the subscription so run() drains and returns


def recover_incomplete(root: Path) -> list[Path]:
    """Finalize sidecars left incomplete by a crash; drop sidecars whose data file is gone."""
    recovered: list[Path] = []
    for tmp in sorted(Path(root).glob("ubx/*/*/*.json.tmp")):
        # A crash between the write and the `os.replace` in `Sidecar.dump` leaves this behind;
        # nothing reads it, so it would sit in the tree for ever.
        log.info("removing stray sidecar temporary %s", tmp)
        tmp.unlink(missing_ok=True)
    for sc_path in sorted(Path(root).glob("ubx/*/*/*.json")):
        try:
            sc = Sidecar.load(sc_path)
        except (OSError, TypeError, ValueError):  # json.JSONDecodeError is a ValueError
            log.warning("unreadable sidecar %s", sc_path)
            continue
        if sc.complete:
            continue
        data = sc_path.with_suffix(".ubx")
        if not data.exists():
            sc_path.unlink()
            continue
        sc.bytes = data.stat().st_size
        sc.sha256 = hashlib.sha256(data.read_bytes()).hexdigest()
        # `end_utc` is receiver UTC everywhere else; the file's mtime is the host clock, which
        # on a base without RTC may be years off. An unknown end is better than a wrong one.
        sc.end_utc = None
        sc.end_utc_source = "unknown"
        sc.complete = True
        sc.recovered = True
        sc.dump(sc_path)
        recovered.append(data)
    return recovered
