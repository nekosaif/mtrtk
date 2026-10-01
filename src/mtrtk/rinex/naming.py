"""RINEX file naming (RINEX 3 long names and RINEX 2 short names)."""

from __future__ import annotations

from datetime import datetime


def duration_code(seconds: float) -> str:
    s = int(round(seconds))
    if s % 86400 == 0 and s >= 86400:
        return f"{s // 86400:02d}D"
    if s % 3600 == 0 and s >= 3600:
        return f"{s // 3600:02d}H"
    if s % 60 == 0 and s >= 60:
        return f"{s // 60:02d}M"
    return f"{s:02d}S"


def period_code(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return "00U"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60):02d}M"
    if seconds >= 1:
        return f"{int(round(seconds)):02d}S"
    return f"{int(round(seconds * 100)):02d}C"  # centiseconds, e.g. 5 Hz -> 20C


def rinex3_name(
    station: str,
    country: str,
    start: datetime,
    duration_s: float,
    interval_s: float | None,
    kind: str = "MO",
    source: str = "R",
) -> str:
    base = (
        f"{station.upper():<4}00{country.upper():<3}_{source}_{start:%Y%j%H%M}"
        f"_{duration_code(duration_s)}"
    )
    if kind == "MO":
        return f"{base}_{period_code(interval_s)}_MO.rnx"
    return f"{base}_{kind}.rnx"


def rinex2_name(station: str, start: datetime, duration_s: float, kind: str = "o") -> str:
    session = "0" if duration_s >= 86400 else chr(ord("a") + start.hour)
    return f"{station.lower()[:4]}{start:%j}{session}.{start:%y}{kind}"
