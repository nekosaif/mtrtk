"""Concatenate the hourly raw files that cover a time window (plus a lead hour for ephemerides)."""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mtrtk.rawlog.index import LogFile, files_for_window

CHUNK = 1 << 20


class SpliceError(RuntimeError):
    pass


class NoDataError(SpliceError):
    pass


class MixedStationsError(SpliceError):
    """The window holds hours from more than one station; the caller must pick one."""


@dataclass(frozen=True)
class SpliceResult:
    path: Path
    files: list[LogFile]
    bytes: int
    lead_hours: int = 1


def _utc(name: str, t: datetime) -> datetime:
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware, got naive {t.isoformat()}")
    return t.astimezone(UTC)


def splice_window(
    root: Path,
    start: datetime,
    end: datetime,
    dest: Path,
    lead_hours: int = 1,
    station: str | None = None,
) -> SpliceResult:
    """Concatenate the hourly logs overlapping [start - lead_hours, end) into ``dest``.

    Raises NoDataError unless at least one file overlaps [start, end) itself (a lead hour
    alone is not data), and MixedStationsError when ``station`` is None and the files come
    from more than one station. ``dest`` is written via a ``.part`` file and is never left
    behind half-written.
    """
    start, end = _utc("start", start), _utc("end", end)
    if start >= end:
        raise ValueError(f"empty window: start {start.isoformat()} >= end {end.isoformat()}")
    if lead_hours < 0:
        raise ValueError(f"lead_hours must be >= 0, got {lead_hours}")
    files = files_for_window(root, start - timedelta(hours=lead_hours), end)
    if station is not None:
        files = [lf for lf in files if lf.station_id == station]
    if not any(lf.hour_utc < end and lf.hour_end > start for lf in files):
        who = f" for station {station}" if station is not None else ""
        raise NoDataError(f"no raw logs{who} between {start.isoformat()} and {end.isoformat()}")
    stations = sorted({lf.station_id for lf in files})
    if len(stations) > 1:
        raise MixedStationsError(
            f"the window holds logs from several stations ({', '.join(stations)}); pick one"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    total = 0
    try:
        with part.open("wb") as out:
            for lf in files:
                try:
                    fh = lf.path.open("rb")
                except OSError as exc:
                    raise NoDataError(
                        f"raw log for {lf.hour_utc:%Y-%m-%d %H}:00 UTC disappeared during "
                        f"the export ({exc.strerror or exc})"
                    ) from exc
                with fh:
                    while chunk := fh.read(CHUNK):
                        out.write(chunk)
                        total += len(chunk)
        os.replace(part, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink()
        raise
    return SpliceResult(dest, files, total, lead_hours)
