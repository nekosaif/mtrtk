"""Camera/event time marks (UBX TIM-TM2) interpolated onto a PPK track."""

from __future__ import annotations

import bisect
import csv
import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from mtrtk.core.frames import Framer, Proto

log = logging.getLogger(__name__)

GPS_EPOCH = datetime(1980, 1, 6, tzinfo=UTC)
WEEK_S = 604800
# GPS - UTC. Constant since 2017-01-01; only used for TIM-TM2 marks reported on a UTC time
# base and for the informational `time_utc` column (the .pos epochs themselves are GPST).
GPS_UTC_LEAP_S = 18
TIME_BASE_UTC = 2  # TIM-TM2 flags.timeBase: 0 receiver, 1 GNSS, 2 UTC
_READ_CHUNK = 256 * 1024  # well under the framer's 1 MiB buffer


class TrackEpoch(Protocol):
    """What interpolation needs from a track epoch; `mtrtk.ppk.pos.PosRecord` satisfies it."""

    @property
    def time(self) -> datetime: ...
    @property
    def lat(self) -> float: ...
    @property
    def lon(self) -> float: ...
    @property
    def height(self) -> float: ...
    @property
    def q(self) -> int: ...
    @property
    def sdn(self) -> float: ...
    @property
    def sde(self) -> float: ...
    @property
    def sdu(self) -> float: ...


@dataclass(frozen=True)
class RawTimeMark:
    count: int
    week: int
    tow_s: float  # GPST
    acc_est_ns: int


@dataclass(frozen=True)
class EventFix:
    n: int
    count: int
    week: int
    tow_s: float
    time: datetime  # GPST, labelled with UTC tzinfo like the .pos records
    lat: float | None
    lon: float | None
    height: float | None
    q: int | None
    sdn: float | None
    sde: float | None
    sdu: float | None
    gap_s: float | None
    status: str  # "ok" | "no_neighbours" | "gap_too_large"


def gpst_datetime(week: int, tow_s: float) -> datetime:
    """GPS week + time of week -> datetime on the GPST scale (no leap-second correction)."""
    return GPS_EPOCH + timedelta(weeks=week, seconds=tow_s)


def extract_time_marks(ubx_path: Path) -> list[RawTimeMark]:
    """Rising edges with a valid time from every TIM-TM2 in a UBX log, oldest first.

    The receiver repeats TIM-TM2 until the next edge, so the same pulse shows up many
    times; a pulse is identified by its count *and* its time, which keeps two pulses
    apart after the 16-bit counter wraps.
    """
    marks: dict[tuple[int, int, int, int], RawTimeMark] = {}
    framer = Framer()
    with Path(ubx_path).open("rb") as fh:
        while chunk := fh.read(_READ_CHUNK):
            for frame in framer.feed(chunk):
                if frame.proto is not Proto.UBX or frame.identity != "TIM-TM2":
                    continue
                try:
                    m = frame.parsed()
                except Exception as exc:  # checksum-valid but unparseable: skip the frame
                    log.debug("unparseable TIM-TM2 in %s: %s", ubx_path, exc)
                    continue
                if not (m.newRisingEdge and m.time):
                    continue
                key = (int(m.count), int(m.wnR), int(m.towMsR), int(m.towSubMsR))
                if key in marks:
                    continue
                week = int(m.wnR)
                tow_s = int(m.towMsR) / 1000 + int(m.towSubMsR) / 1e9
                if int(m.timeBase) == TIME_BASE_UTC:
                    tow_s += GPS_UTC_LEAP_S
                    if tow_s >= WEEK_S:
                        week, tow_s = week + 1, tow_s - WEEK_S
                marks[key] = RawTimeMark(int(m.count), week, tow_s, int(m.accEst))
    return sorted(marks.values(), key=lambda mk: (mk.week, mk.tow_s))


def _lerp(a: float, b: float, f: float) -> float:
    return a + (b - a) * f


def _lerp_lon(a: float, b: float, f: float) -> float:
    if b - a > 180.0:
        b -= 360.0
    elif a - b > 180.0:
        b += 360.0
    lon = _lerp(a, b, f)
    return lon - 360.0 if lon > 180.0 else lon + 360.0 if lon < -180.0 else lon


def interpolate_events(
    marks: Sequence[RawTimeMark], records: Sequence[TrackEpoch], max_gap_s: float = 2.0
) -> list[EventFix]:
    """Place each mark on the track by linear interpolation between its two neighbouring
    epochs; the neighbours must be at most `max_gap_s` apart. Quality and sigmas are the
    worse of the two neighbours. A mark exactly on an epoch takes that epoch."""
    recs = sorted(records, key=lambda r: r.time)
    times = [r.time for r in recs]
    out: list[EventFix] = []
    for n, mk in enumerate(marks, start=1):
        t = gpst_datetime(mk.week, mk.tow_s)
        head = (n, mk.count, mk.week, mk.tow_s, t)
        idx = bisect.bisect_left(times, t)
        if idx < len(recs) and times[idx] == t:
            r = recs[idx]
            out.append(EventFix(*head, r.lat, r.lon, r.height, r.q, r.sdn, r.sde, r.sdu, 0.0, "ok"))
            continue
        if idx == 0 or idx >= len(recs):
            out.append(EventFix(*head, *(None,) * 8, "no_neighbours"))
            continue
        a, b = recs[idx - 1], recs[idx]
        gap = (b.time - a.time).total_seconds()
        if gap > max_gap_s:
            out.append(EventFix(*head, *(None,) * 7, gap, "gap_too_large"))
            continue
        f = (t - a.time).total_seconds() / gap
        out.append(
            EventFix(
                *head,
                _lerp(a.lat, b.lat, f),
                _lerp_lon(a.lon, b.lon, f),
                _lerp(a.height, b.height, f),
                max(a.q, b.q),
                max(a.sdn, b.sdn),
                max(a.sde, b.sde),
                max(a.sdu, b.sdu),
                gap,
                "ok",
            )
        )
    return out


def _fmt(value: float | None, spec: str) -> str:
    return "" if value is None else format(value, spec)


CSV_COLUMNS = [
    "n",
    "count",
    "gps_week",
    "gps_tow_s",
    "time_gpst",
    "lat",
    "lon",
    "height_m",
    "q",
    "sdn_m",
    "sde_m",
    "sdu_m",
    "interp_gap_s",
    "status",
    "time_utc",
]


def events_csv(events: Sequence[EventFix]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for e in events:
        w.writerow(
            [
                e.n,
                e.count,
                e.week,
                f"{e.tow_s:.6f}",
                e.time.isoformat(),
                _fmt(e.lat, ".9f"),
                _fmt(e.lon, ".9f"),
                _fmt(e.height, ".4f"),
                "" if e.q is None else e.q,
                _fmt(e.sdn, ".4f"),
                _fmt(e.sde, ".4f"),
                _fmt(e.sdu, ".4f"),
                _fmt(e.gap_s, ".3f"),
                e.status,
                (e.time - timedelta(seconds=GPS_UTC_LEAP_S)).isoformat(),
            ]
        )
    return buf.getvalue()


def events_geojson(events: Sequence[EventFix]) -> dict[str, Any]:
    """Placed events only (an event without a position has no geometry)."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [e.lon, e.lat, e.height]},
                "properties": {
                    "n": e.n,
                    "count": e.count,
                    "gps_week": e.week,
                    "gps_tow_s": e.tow_s,
                    "time": e.time.isoformat(),
                    "q": e.q,
                    "sdn": e.sdn,
                    "sde": e.sde,
                    "sdu": e.sdu,
                    "interp_gap_s": e.gap_s,
                    "status": e.status,
                },
            }
            for e in events
            if e.lat is not None
        ],
    }
