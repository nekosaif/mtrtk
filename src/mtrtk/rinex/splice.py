"""Concatenate the hourly raw files that cover a time window (plus a lead hour for ephemerides)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from mtrtk.rawlog.index import LogFile, files_for_window

CHUNK = 1 << 20


class NoDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpliceResult:
    path: Path
    files: list[LogFile]
    bytes: int
    lead_hours: int = 1


def splice_window(
    root: Path, start: datetime, end: datetime, dest: Path, lead_hours: int = 1
) -> SpliceResult:
    files = files_for_window(root, start - timedelta(hours=lead_hours), end)
    if not files:
        raise NoDataError(f"no raw logs between {start.isoformat()} and {end.isoformat()}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with dest.open("wb") as out:
        for lf in files:
            with lf.path.open("rb") as fh:
                while chunk := fh.read(CHUNK):
                    out.write(chunk)
                    total += len(chunk)
    return SpliceResult(dest, files, total, lead_hours)
