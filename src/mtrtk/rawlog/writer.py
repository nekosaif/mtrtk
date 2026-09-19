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
from datetime import UTC, datetime
from pathlib import Path

from mtrtk.core.bus import Bus, Policy
from mtrtk.core.frames import Frame, Proto

log = logging.getLogger(__name__)

PENDING_CAP_BYTES = 8 * 1024 * 1024  # buffered before receiver time is known
SIDECAR_UPDATE_S = 60.0
FLUSH_INTERVAL_S = 1.0


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

    def dump(self, path: Path) -> None:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
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
        self.fh = path.open("ab")

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
        os.fsync(self.fh.fileno())

    def dump_sidecar(self) -> None:
        self.sidecar.dump(sidecar_path(self.path))

    def close(self, end_utc: datetime | None) -> None:
        self.fsync()
        self.fh.close()
        self.sidecar.end_utc = end_utc.isoformat() if end_utc else None
        self.sidecar.sha256 = self.hasher.hexdigest()
        self.sidecar.complete = True
        self.dump_sidecar()


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
        self.firmware = firmware
        self.site = site
        self.sub = bus.subscribe("raw.ubx", policy=Policy.UNBOUNDED, high_water=20_000)
        self._current: _OpenLog | None = None
        self._pending = bytearray()
        self._pending_counts: dict[str, int] = {}
        self._utc: datetime | None = None  # receiver clock: the only thing rotation keys on
        self._time_source = "receiver"
        self._last_fsync = time.monotonic()
        self._last_sidecar = time.monotonic()
        self._backpressure_seen = 0

    # ------------------------------------------------------------ properties
    @property
    def current_path(self) -> Path | None:
        return self._current.path if self._current else None

    @property
    def receiver_utc(self) -> datetime | None:
        return self._utc

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
        if self._current is None or self._current.hour != hour:
            self._rotate(hour)
        assert self._current is not None
        self._current.write(frame.raw, identity)

    def _update_time(self, frame: Frame) -> None:
        m = frame.parsed()
        if m.validDate and m.validTime:
            self._utc = datetime(m.year, m.month, m.day, m.hour, m.min, m.second, tzinfo=UTC)
            # A receiver that finally got time takes the naming back from the host clock; the
            # file already named by the host keeps its own sidecar's time_source="host".
            self._time_source = "receiver"

    def _buffer_pending(self, raw: bytes, identity: str) -> None:
        self._pending += raw
        self._pending_counts[identity] = self._pending_counts.get(identity, 0) + 1
        if len(self._pending) > PENDING_CAP_BYTES:
            log.warning(
                "no receiver time after %d buffered bytes; naming file by host clock",
                len(self._pending),
            )
            self._utc = datetime.now(UTC)
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
        self._current.close(self._utc)
        self.bus.publish("rawlog.closed", self._current.path)
        self._current = None

    def tick(self, now_mono: float) -> None:
        """Called about once per second: flush, periodic fsync and sidecar refresh."""
        if self._current is None:
            return
        self._current.flush()
        if now_mono - self._last_fsync >= self.fsync_interval_s:
            self._current.fsync()
            self._last_fsync = now_mono
        if now_mono - self._last_sidecar >= SIDECAR_UPDATE_S:
            self._current.dump_sidecar()
            self._last_sidecar = now_mono

    def close(self) -> None:
        if self._current is not None:
            self._close_current()

    # ------------------------------------------------------------ async run
    def stop(self) -> None:
        """End `run()`: the queued frames still drain before the loop exits."""
        self.bus.unsubscribe(self.sub)

    async def run(self, stop: asyncio.Event) -> None:
        ticker = asyncio.create_task(self._ticker(stop), name="rawlog-ticker")
        try:
            async for _, frame in self.sub:
                self.handle(frame)
                if self.sub.high_water_hits > self._backpressure_seen:
                    self._backpressure_seen = self.sub.high_water_hits
                    self.bus.publish("rawlog.backpressure", self.sub.queue.qsize())
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
            self.close()

    async def _ticker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), FLUSH_INTERVAL_S)
            self.tick(time.monotonic())
        self.stop()  # stop requested: close the subscription so run() drains and returns


def recover_incomplete(root: Path) -> list[Path]:
    """Finalize sidecars left incomplete by a crash; drop sidecars whose data file is gone."""
    recovered: list[Path] = []
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
        sc.end_utc = datetime.fromtimestamp(data.stat().st_mtime, UTC).isoformat()
        sc.complete = True
        sc.recovered = True
        sc.dump(sc_path)
        recovered.append(data)
    return recovered
