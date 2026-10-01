"""Survey point exports: CSV, GeoJSON, KML and GPX.

Heights: CSV and GeoJSON carry the ellipsoidal height (GeoJSON's coordinate is defined against
the WGS 84 ellipsoid); KML (`altitudeMode` absolute) and GPX (`ele`) expect height above mean sea
level, so they carry `hmsl_m`, falling back to the ellipsoidal height when a point has none.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from mtrtk.core.state import CARR_SOLN_NAMES, FIX_TYPE_NAMES
from mtrtk.store.models import Point

CSV_FIELDS = [
    "id",
    "name",
    "code",
    "note",
    "time_utc",
    "lat",
    "lon",
    "height_m",
    "hmsl_m",
    "n_epochs",
    "sd_n_m",
    "sd_e_m",
    "sd_u_m",
    "fix",
    "carr_soln",
    "h_acc_m",
    "v_acc_m",
    "session_id",
]

# Characters XML 1.0 cannot carry at all, escaped or not: a stray control character in a point
# name would otherwise make the whole file unreadable.
_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _text(value: str) -> str:
    return escape(_XML_INVALID.sub("", value))


def _num(value: float, decimals: int) -> str:
    """Shortest text for `value` rounded to `decimals` places (23.8373506, not 23.837350600)."""
    return repr(round(value, decimals))


def _opt(value: float | None, fmt: str) -> str:
    return "" if value is None else format(value, fmt)


def _fix_name(p: Point) -> str:
    return FIX_TYPE_NAMES.get(p.fix_type, str(p.fix_type))


def _carr_name(p: Point) -> str:
    return CARR_SOLN_NAMES.get(p.carr_soln, str(p.carr_soln))


def _alt_msl(p: Point) -> float:
    return p.hmsl_m if p.hmsl_m is not None else p.height_m


def _csv_row(p: Point) -> dict[str, Any]:
    return {
        "id": "" if p.id is None else p.id,
        "name": p.name,
        "code": p.code or "",
        "note": p.note or "",
        "time_utc": p.ts_utc.isoformat(),
        "lat": f"{p.lat:.9f}",
        "lon": f"{p.lon:.9f}",
        "height_m": f"{p.height_m:.4f}",
        "hmsl_m": _opt(p.hmsl_m, ".4f"),
        "n_epochs": p.n_epochs,
        "sd_n_m": f"{p.sd_n:.4f}",
        "sd_e_m": f"{p.sd_e:.4f}",
        "sd_u_m": f"{p.sd_u:.4f}",
        "fix": _fix_name(p),
        "carr_soln": _carr_name(p),
        "h_acc_m": _opt(p.h_acc_m, ".4f"),
        "v_acc_m": _opt(p.v_acc_m, ".4f"),
        "session_id": "" if p.session_id is None else p.session_id,
    }


def to_csv(points: list[Point]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for p in points:
        writer.writerow(_csv_row(p))
    return buf.getvalue()


def _properties(p: Point) -> dict[str, Any]:
    props = p.model_dump(mode="json", exclude={"lat", "lon", "height_m", "sd_n", "sd_e", "sd_u"})
    props.update(
        sd_n_m=p.sd_n,
        sd_e_m=p.sd_e,
        sd_u_m=p.sd_u,
        fix_name=_fix_name(p),
        carr_soln_name=_carr_name(p),
    )
    return props


def to_geojson(points: list[Point]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [p.lon, p.lat, p.height_m]},
                "properties": _properties(p),
            }
            for p in points
        ],
    }


def _kml_placemark(p: Point) -> str:
    label = f"{p.code or ''} {p.note or ''}".strip()
    stats = (
        f"{p.n_epochs} epochs · σ N/E/U {p.sd_n:.3f}/{p.sd_e:.3f}/{p.sd_u:.3f} m · {_carr_name(p)}"
    )
    desc = f"{label} · {stats}" if label else stats
    coords = f"{_num(p.lon, 9)},{_num(p.lat, 9)},{_num(_alt_msl(p), 4)}"
    return (
        f"<Placemark><name>{_text(p.name)}</name><description>{_text(desc)}</description>"
        f"<TimeStamp><when>{p.ts_utc.isoformat()}</when></TimeStamp>"
        f"<Point><altitudeMode>absolute</altitudeMode><coordinates>{coords}</coordinates></Point>"
        "</Placemark>"
    )


def to_kml(points: list[Point]) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>mtrtk points</name>'
        + "".join(_kml_placemark(p) for p in points)
        + "</Document></kml>\n"
    )


def _gpx_wpt(p: Point) -> str:
    # wptType is a sequence: ele, time, geoidheight, name, desc, type - in that order.
    parts = [f"<ele>{_num(_alt_msl(p), 4)}</ele>", f"<time>{p.ts_utc.isoformat()}</time>"]
    if p.hmsl_m is not None:
        parts.append(f"<geoidheight>{_num(p.height_m - p.hmsl_m, 4)}</geoidheight>")
    parts.append(f"<name>{_text(p.name)}</name>")
    if p.note:
        parts.append(f"<desc>{_text(p.note)}</desc>")
    if p.code:
        parts.append(f"<type>{_text(p.code)}</type>")
    lat, lon = quoteattr(_num(p.lat, 9)), quoteattr(_num(p.lon, 9))
    return f"<wpt lat={lat} lon={lon}>{''.join(parts)}</wpt>"


def to_gpx(points: list[Point]) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="mtrtk" xmlns="http://www.topografix.com/GPX/1/1">'
        + "".join(_gpx_wpt(p) for p in points)
        + "</gpx>\n"
    )
