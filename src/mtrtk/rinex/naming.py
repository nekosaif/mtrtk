"""RINEX file naming (RINEX 3 long names and RINEX 2 short names)."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime

_UNITS = (("D", 86400), ("H", 3600), ("M", 60), ("S", 1))
_STATION_RE = re.compile(r"^[A-Z0-9]{4}$")
_COUNTRY_RE = re.compile(r"^[A-Z]{3}$")
_SOURCES = frozenset("RSU")


def duration_code(seconds: float) -> str:
    """Two digits plus a unit. A span no unit expresses exactly is rounded up to the
    smallest unit that fits (2.5 h -> '03H', 4.5 d -> '05D'); 99D is the ceiling."""
    s = max(1, int(round(seconds)))
    for unit, size in _UNITS:
        if s % size == 0 and s // size <= 99:
            return f"{s // size:02d}{unit}"
    for unit, size in reversed(_UNITS):
        if math.ceil(s / size) <= 99:
            return f"{math.ceil(s / size):02d}{unit}"
    return "99D"


def period_code(seconds: float | None) -> str:
    """Two digits plus a unit; None or <= 0 means unspecified ('00U').

    Raises ValueError for an interval no unit represents exactly in two digits, such as
    1.5 s (which would otherwise round to '02S', the same as 2.5 s) or 150 s.
    """
    if seconds is None or seconds <= 0:
        return "00U"
    if not math.isfinite(seconds):
        raise ValueError(f"interval {seconds} is not finite")
    if seconds < 1:
        cs = seconds * 100
        if abs(cs - round(cs)) < 1e-6 and 1 <= round(cs) <= 99:
            return f"{int(round(cs)):02d}C"  # centiseconds, e.g. 5 Hz -> 20C
        raise ValueError(f"interval {seconds} s is not a whole number of centiseconds")
    if abs(seconds - round(seconds)) > 1e-9:
        raise ValueError(f"interval {seconds} s is not a whole number of seconds")
    s = int(round(seconds))
    for unit, size in _UNITS:
        if s % size == 0 and s // size <= 99:
            return f"{s // size:02d}{unit}"
    raise ValueError(f"interval {seconds} s has no two-digit RINEX period code")


def _utc(start: datetime) -> datetime:
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError(f"start must be timezone-aware, got naive {start.isoformat()}")
    return start.astimezone(UTC)


def _station(station: str) -> str:
    code = station.upper()
    if not _STATION_RE.match(code):
        raise ValueError(f"station {station!r} must be 4 letters or digits")
    return code


def rinex3_name(
    station: str,
    country: str,
    start: datetime,
    duration_s: float,
    interval_s: float | None,
    kind: str = "MO",
    source: str = "R",
) -> str:
    code = _station(station)
    ccc = country.upper()
    if not _COUNTRY_RE.match(ccc):
        raise ValueError(f"country {country!r} must be an ISO 3166 alpha-3 code such as 'BGD'")
    if source not in _SOURCES:
        raise ValueError(f"source {source!r} must be one of R, S, U")
    t = _utc(start)
    base = f"{code}00{ccc}_{source}_{t:%Y%j%H%M}_{duration_code(duration_s)}"
    if kind == "MO":
        return f"{base}_{period_code(interval_s)}_MO.rnx"
    return f"{base}_{kind}.rnx"


def rinex2_name(station: str, start: datetime, duration_s: float, kind: str = "o") -> str:
    code = _station(station).lower()
    t = _utc(start)
    session = "0" if duration_s >= 86400 else chr(ord("a") + t.hour)
    return f"{code}{t:%j}{session}.{t:%y}{kind}"
