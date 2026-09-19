"""Read-only view of the raw log directory: what hours exist, which files cover a window."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mtrtk.rawlog.writer import Sidecar, sidecar_path

_NAME_RE = re.compile(r"^(?P<station>[A-Z0-9]{4})_(?P<date>\d{8})_(?P<hour>\d{2})\.ubx$")


@dataclass(frozen=True)
class LogFile:
    path: Path
    sidecar_path: Path
    station_id: str
    hour_utc: datetime
    bytes: int
    complete: bool
    keep: bool
    msg_counts: dict[str, int] = field(default_factory=dict)
    start_utc: str | None = None
    end_utc: str | None = None

    @property
    def hour_end(self) -> datetime:
        return self.hour_utc + timedelta(hours=1)


@dataclass(frozen=True)
class HourSlot:
    hour_utc: datetime
    file: LogFile | None


def parse_log_name(path: Path) -> tuple[str, datetime] | None:
    m = _NAME_RE.match(path.name)
    if not m:
        return None
    try:
        hour = datetime.strptime(m["date"] + m["hour"], "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError:
        return None
    return m["station"], hour


def _read_sidecar(sc_path: Path) -> Sidecar | None:
    """A missing, truncated or hand-edited sidecar leaves the file itself usable."""
    try:
        return Sidecar.load(sc_path)
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError):  # json.JSONDecodeError is a ValueError
        return None


def _load_logfile(path: Path) -> LogFile | None:
    parsed = parse_log_name(path)
    if parsed is None:
        return None
    station, hour = parsed
    sc_path = sidecar_path(path)
    try:
        size = path.stat().st_size
    except OSError:  # pruned between the glob and the stat
        return None
    sc = _read_sidecar(sc_path)
    if sc is None:
        return LogFile(path, sc_path, station, hour, size, complete=False, keep=False)
    return LogFile(
        path,
        sc_path,
        station,
        hour,
        size,
        sc.complete,
        sc.keep,
        dict(sc.msg_counts),
        sc.start_utc,
        sc.end_utc,
    )


def list_logs(root: Path) -> list[LogFile]:
    logs = [lf for p in Path(root).glob("ubx/*/*/*.ubx") if (lf := _load_logfile(p)) is not None]
    return sorted(logs, key=lambda lf: lf.hour_utc)


def files_for_window(root: Path, start: datetime, end: datetime) -> list[LogFile]:
    """Hourly files overlapping [start, end)."""
    return [lf for lf in list_logs(root) if lf.hour_utc < end and lf.hour_end > start]


def hour_availability(root: Path, start: datetime, end: datetime) -> list[HourSlot]:
    """One slot per hour in [start, end), so the UI can draw gaps as well as coverage."""
    by_hour = {lf.hour_utc: lf for lf in list_logs(root)}
    slots: list[HourSlot] = []
    hour = start.replace(minute=0, second=0, microsecond=0)
    while hour < end:
        slots.append(HourSlot(hour, by_hour.get(hour)))
        hour += timedelta(hours=1)
    return slots
