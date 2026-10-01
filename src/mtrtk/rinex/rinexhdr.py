"""Read the few RINEX observation header fields PPK needs, and tell an input's format apart.

Only the header is read (up to `END OF HEADER`), except by `obs_span` on a file whose header
does not carry `TIME OF LAST OBS`, which then walks the RINEX 3 epoch records.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

HEADER_MAX_LINES = 2000  # a RINEX header is tens of lines; past this it is not one

Format = Literal["raw", "rinex-obs", "rinex-nav", "crinex", "gzip"]


@dataclass(frozen=True)
class RinexHeaderInfo:
    version: str
    marker: str
    receiver_type: str
    antenna_type: str
    approx_xyz: tuple[float, float, float] | None
    interval: float | None
    first_obs: datetime | None = None  # naive, on the header's time system (GPS for convbin)
    last_obs: datetime | None = None


def _header_time(body: str) -> datetime | None:
    """`  2026    09    18    20    23   27.9980000     GPS` -> a naive datetime."""
    parts = body.split()
    if len(parts) < 6:
        return None
    try:
        y, mo, d, h, mi = (int(p) for p in parts[:5])
        sec = float(parts[5])
        whole = int(sec)
        return datetime(y, mo, d, h, mi, whole, round((sec - whole) * 1e6) % 1_000_000)
    except ValueError:
        return None


def _epoch_time(line: str) -> datetime | None:
    """A RINEX 3 epoch record, `> 2026 09 18 20 23 27.9980000  0 35`."""
    return _header_time(line[1:]) if line.startswith(">") else None


def read_header(path: Path) -> RinexHeaderInfo:
    version = marker = receiver = antenna = ""
    xyz: tuple[float, float, float] | None = None
    interval: float | None = None
    first: datetime | None = None
    last: datetime | None = None
    with Path(path).open(encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh):
            if n >= HEADER_MAX_LINES:
                break
            label = line[60:].strip()
            body = line[:60]
            if label == "RINEX VERSION / TYPE":
                version = body[:9].strip()
            elif label == "MARKER NAME":
                marker = body.strip()
            elif label == "REC # / TYPE / VERS":
                receiver = body[20:40].strip()
            elif label == "ANT # / TYPE":
                antenna = body[20:40].strip()
            elif label == "APPROX POSITION XYZ":
                try:
                    x, y, z = (float(body[i : i + 14]) for i in (0, 14, 28))
                    xyz = (x, y, z) if any((x, y, z)) else None
                except ValueError:
                    xyz = None
            elif label == "INTERVAL":
                try:
                    interval = float(body.strip())
                except ValueError:
                    interval = None
            elif label == "TIME OF FIRST OBS":
                first = _header_time(body)
            elif label == "TIME OF LAST OBS":
                last = _header_time(body)
            elif label == "END OF HEADER":
                break
    return RinexHeaderInfo(version, marker, receiver, antenna, xyz, interval, first, last)


def obs_span(path: Path) -> tuple[datetime, datetime] | None:
    """First and last epoch of an observation file (naive, GPST for anything convbin wrote), or
    None when neither the header nor the epoch records say."""
    info = read_header(path)
    if info.first_obs and info.last_obs:
        return info.first_obs, info.last_obs
    first = last = None
    with Path(path).open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith(">") and (t := _epoch_time(line)) is not None:
                first = first or t
                last = t
    first = info.first_obs or first
    if first is None or last is None:
        return None
    return first, last


def sniff_format(path: Path) -> Format:
    """What a file is, from its content (never its name): a RINEX observation or navigation
    file, a Hatanaka-compressed or gzipped one, or else raw receiver data for convbin.

    Raw data is taken as UBX whatever produced it - an F9P, or an INS whose GNSS port emits
    UBX RXM-RAWX/SFRBX - so a file named `.bin` or `.log` converts like a `.ubx`.
    """
    with Path(path).open("rb") as fh:
        head = fh.read(256)
    if head[:2] == b"\x1f\x8b":
        return "gzip"
    first = head.split(b"\n", 1)[0].decode("ascii", "replace")
    label = first[60:].strip()
    if label == "CRINEX VERS   / TYPE":
        return "crinex"
    if label == "RINEX VERSION / TYPE":
        return "rinex-obs" if first[20:21].upper() == "O" else "rinex-nav"
    return "raw"
