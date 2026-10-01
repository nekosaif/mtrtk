"""RTKLIB .pos solution files: parse, summarize, and write track CSV/GeoJSON/KML."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from xml.sax.saxutils import escape

Q_NAMES = {1: "fixed", 2: "float", 3: "sbas", 4: "dgps", 5: "single", 6: "ppp"}
# KML colours are aabbggrr
Q_COLORS_KML = {
    1: "ff00c30c",
    2: "ff19b2fa",
    3: "ff5a83ec",
    4: "ff5a83ec",
    5: "ff3b3bd0",
    6: "ffe9b085",
}

_TIME_FORMATS = ("%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S")
_MIN_COLUMNS = 15  # date, time, lat, lon, h, Q, ns, sdn, sde, sdu, sdne, sdeu, sdun, age, ratio


@dataclass(frozen=True, slots=True)  # slots: a day of 5 Hz epochs is 432k of these
class PosRecord:
    time: datetime  # GPST labelled with UTC tzinfo for arithmetic; the CSV header says GPST
    lat: float
    lon: float
    height: float
    q: int
    ns: int
    sdn: float
    sde: float
    sdu: float
    sdne: float
    sdeu: float
    sdun: float
    age: float
    ratio: float

    @property
    def quality(self) -> str:
        return Q_NAMES.get(self.q, str(self.q))


def _parse_time(date: str, clock: str) -> datetime | None:
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(f"{date} {clock}", fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def parse_pos(text: str) -> list[PosRecord]:
    """Parse an rnx2rtkp llh solution; `%` headers, blank and malformed lines are skipped."""
    return list(iter_pos(text.splitlines()))


def iter_pos(lines: Iterable[str]) -> Iterator[PosRecord]:
    """`parse_pos` one line at a time (an open file works): nothing but the record is held."""
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("%"):
            continue
        parts = line.split()
        if len(parts) < _MIN_COLUMNS:
            continue
        t = _parse_time(parts[0], parts[1])
        if t is None:
            continue
        try:
            n = [float(v) for v in parts[2:_MIN_COLUMNS]]
        except ValueError:
            continue
        yield PosRecord(
            time=t,
            lat=n[0],
            lon=n[1],
            height=n[2],
            q=int(n[3]),
            ns=int(n[4]),
            sdn=n[5],
            sde=n[6],
            sdu=n[7],
            sdne=n[8],
            sdeu=n[9],
            sdun=n[10],
            age=n[11],
            ratio=n[12],
        )


@dataclass
class PpkSummary:
    epochs: int = 0
    duration_s: float = 0.0
    interval_s: float | None = None
    fixed_pct: float = 0.0
    float_pct: float = 0.0
    single_pct: float = 0.0
    mean_sd_fixed: dict[str, float] | None = None
    gaps: list[tuple[datetime, datetime, float]] = field(default_factory=list)
    first_time: datetime | None = None
    last_time: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "epochs": self.epochs,
            "duration_s": self.duration_s,
            "interval_s": self.interval_s,
            "fixed_pct": self.fixed_pct,
            "float_pct": self.float_pct,
            "single_pct": self.single_pct,
            "mean_sd_fixed": self.mean_sd_fixed,
            "gaps": [[a.isoformat(), b.isoformat(), s] for a, b, s in self.gaps],
            "first_time": self.first_time.isoformat() if self.first_time else None,
            "last_time": self.last_time.isoformat() if self.last_time else None,
        }


def _pct(count: int, total: int) -> float:
    return round(100 * count / total, 2)


def summarize(records: list[PosRecord], gap_s: float = 2.0) -> PpkSummary:
    """Fix/float/single shares, mean fixed sigmas, median interval and gaps longer than gap_s."""
    s = PpkSummary(epochs=len(records))
    if not records:
        return s
    s.first_time, s.last_time = records[0].time, records[-1].time
    s.duration_s = (s.last_time - s.first_time).total_seconds()
    pairs = list(zip(records, records[1:], strict=False))
    deltas = sorted((b.time - a.time).total_seconds() for a, b in pairs)
    if deltas:
        s.interval_s = deltas[len(deltas) // 2]
    n = len(records)
    s.fixed_pct = _pct(sum(r.q == 1 for r in records), n)
    s.float_pct = _pct(sum(r.q == 2 for r in records), n)
    s.single_pct = _pct(sum(r.q == 5 for r in records), n)
    fixed = [r for r in records if r.q == 1]
    if fixed:
        k = len(fixed)
        s.mean_sd_fixed = {
            "n": round(sum(r.sdn for r in fixed) / k, 5),
            "e": round(sum(r.sde for r in fixed) / k, 5),
            "u": round(sum(r.sdu for r in fixed) / k, 5),
        }
    for a, b in pairs:
        dt = (b.time - a.time).total_seconds()
        if dt > gap_s:
            s.gaps.append((a.time, b.time, dt))
    return s


def _gpst_label(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}"


def track_csv(records: list[PosRecord]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(
        [
            "time_gpst",
            "lat",
            "lon",
            "height_m",
            "q",
            "quality",
            "ns",
            "sdn_m",
            "sde_m",
            "sdu_m",
            "age_s",
            "ratio",
        ]
    )
    for r in records:
        w.writerow(
            [
                _gpst_label(r.time),
                f"{r.lat:.9f}",
                f"{r.lon:.9f}",
                f"{r.height:.4f}",
                r.q,
                r.quality,
                r.ns,
                f"{r.sdn:.4f}",
                f"{r.sde:.4f}",
                f"{r.sdu:.4f}",
                f"{r.age:.2f}",
                f"{r.ratio:.1f}",
            ]
        )
    return buf.getvalue()


def _runs(records: list[PosRecord]) -> list[list[PosRecord]]:
    """Consecutive records grouped by solution quality."""
    runs: list[list[PosRecord]] = []
    for r in records:
        if runs and runs[-1][-1].q == r.q:
            runs[-1].append(r)
        else:
            runs.append([r])
    return runs


def track_geojson(records: list[PosRecord], point_every: int = 10) -> dict[str, Any]:
    """One LineString per same-quality run plus a Point every `point_every` epochs."""
    features: list[dict[str, Any]] = []
    for run in _runs(records):
        coords = [[r.lon, r.lat, r.height] for r in run]
        if len(coords) == 1:
            coords = coords * 2  # a LineString needs at least two positions
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {
                    "q": run[0].q,
                    "quality": run[0].quality,
                    "start": run[0].time.isoformat(),
                    "end": run[-1].time.isoformat(),
                    "epochs": len(run),
                },
            }
        )
    step = max(1, point_every)
    for i, r in enumerate(records):
        if i % step == 0:
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [r.lon, r.lat, r.height]},
                    "properties": {
                        "time": r.time.isoformat(),
                        "q": r.q,
                        "quality": r.quality,
                        "ns": r.ns,
                        "sdn": r.sdn,
                        "sde": r.sde,
                        "sdu": r.sdu,
                    },
                }
            )
    return {"type": "FeatureCollection", "features": features}


def track_kml(records: list[PosRecord]) -> str:
    """KML document with one coloured LineString per same-quality run.

    Clamped to the ground: the heights are ellipsoidal, and Google Earth would read an
    `absolute` altitude as height above mean sea level (tens of metres off where the geoid
    is far from the ellipsoid, about -50 m in Bangladesh)."""
    styles = "".join(
        f'<Style id="q{q}"><LineStyle><color>{color}</color><width>3</width></LineStyle></Style>'
        for q, color in Q_COLORS_KML.items()
    )
    marks: list[str] = []
    for run in _runs(records):
        coords = " ".join(f"{r.lon},{r.lat},{r.height}" for r in run)
        if len(run) == 1:
            coords = f"{coords} {coords}"
        name = escape(f"{run[0].quality} {run[0].time:%H:%M:%S}–{run[-1].time:%H:%M:%S}")
        marks.append(
            f"<Placemark><name>{name}</name><styleUrl>#q{run[0].q}</styleUrl>"
            "<LineString><altitudeMode>clampToGround</altitudeMode>"
            f"<coordinates>{coords}</coordinates></LineString></Placemark>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>mtrtk PPK track</name>'
        + styles
        + "".join(marks)
        + "</Document></kml>\n"
    )
