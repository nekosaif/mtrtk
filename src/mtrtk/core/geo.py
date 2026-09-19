"""WGS84 geodesy: LLH <-> ECEF, UTM (forward), ENU offsets and DMS formatting. Pure functions."""

from __future__ import annotations

import math
from dataclasses import dataclass

WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
WGS84_B = WGS84_A * (1 - WGS84_F)
WGS84_E2 = WGS84_F * (2 - WGS84_F)  # first eccentricity squared
_EP2 = WGS84_E2 / (1 - WGS84_E2)  # second eccentricity squared
UTM_K0 = 0.9996
UTM_FALSE_EASTING = 500_000.0
UTM_FALSE_NORTHING_SOUTH = 10_000_000.0


def llh_to_ecef(lat_deg: float, lon_deg: float, h_m: float) -> tuple[float, float, float]:
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * sin_lat * sin_lat)
    x = (n + h_m) * cos_lat * math.cos(lon)
    y = (n + h_m) * cos_lat * math.sin(lon)
    z = (n * (1 - WGS84_E2) + h_m) * sin_lat
    return x, y, z


def ecef_to_llh(x: float, y: float, z: float) -> tuple[float, float, float]:
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    if p < 1e-9:  # on the polar axis
        lat = math.copysign(math.pi / 2, z)
        return math.degrees(lat), math.degrees(lon), abs(z) - WGS84_B
    lat = math.atan2(z, p * (1 - WGS84_E2))
    for _ in range(20):
        sin_lat = math.sin(lat)
        n = WGS84_A / math.sqrt(1 - WGS84_E2 * sin_lat * sin_lat)
        h = p / math.cos(lat) - n
        new_lat = math.atan2(z, p * (1 - WGS84_E2 * n / (n + h)))
        if abs(new_lat - lat) < 1e-14:
            lat = new_lat
            break
        lat = new_lat
    sin_lat = math.sin(lat)
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * sin_lat * sin_lat)
    h = p / math.cos(lat) - n
    return math.degrees(lat), math.degrees(lon), h


@dataclass(frozen=True)
class Utm:
    zone: int
    hemisphere: str  # "N" | "S"
    easting: float
    northing: float

    @property
    def label(self) -> str:
        return f"{self.zone}{self.hemisphere}"


def utm_zone(lat_deg: float, lon_deg: float) -> int:
    zone = int((lon_deg + 180) // 6) + 1
    if 56 <= lat_deg < 64 and 3 <= lon_deg < 12:
        zone = 32  # south-west Norway
    if lat_deg >= 72 and 0 <= lon_deg < 42:  # Svalbard
        if lon_deg < 9:
            zone = 31
        elif lon_deg < 21:
            zone = 33
        elif lon_deg < 33:
            zone = 35
        else:
            zone = 37
    return min(max(zone, 1), 60)


def llh_to_utm(lat_deg: float, lon_deg: float) -> Utm:
    """Transverse Mercator series (Snyder 1987), accurate to ~1 mm inside the zone."""
    zone = utm_zone(lat_deg, lon_deg)
    hemisphere = "N" if lat_deg >= 0 else "S"
    lat = math.radians(lat_deg)
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    d_lon = math.radians(lon_deg) - lon0
    e2, ep2 = WGS84_E2, _EP2
    sin_lat, cos_lat, tan_lat = math.sin(lat), math.cos(lat), math.tan(lat)
    n = WGS84_A / math.sqrt(1 - e2 * sin_lat * sin_lat)
    t = tan_lat * tan_lat
    c = ep2 * cos_lat * cos_lat
    a = d_lon * cos_lat
    m = WGS84_A * (
        (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256) * lat
        - (3 * e2 / 8 + 3 * e2**2 / 32 + 45 * e2**3 / 1024) * math.sin(2 * lat)
        + (15 * e2**2 / 256 + 45 * e2**3 / 1024) * math.sin(4 * lat)
        - (35 * e2**3 / 3072) * math.sin(6 * lat)
    )
    easting = (
        UTM_K0
        * n
        * (a + (1 - t + c) * a**3 / 6 + (5 - 18 * t + t * t + 72 * c - 58 * ep2) * a**5 / 120)
    )
    northing = UTM_K0 * (
        m
        + n
        * tan_lat
        * (
            a * a / 2
            + (5 - t + 9 * c + 4 * c * c) * a**4 / 24
            + (61 - 58 * t + t * t + 600 * c - 330 * ep2) * a**6 / 720
        )
    )
    easting += UTM_FALSE_EASTING
    if hemisphere == "S":
        northing += UTM_FALSE_NORTHING_SOUTH
    return Utm(zone, hemisphere, easting, northing)


def ecef_to_enu(
    ref_lat_deg: float, ref_lon_deg: float, ref_h_m: float, x: float, y: float, z: float
) -> tuple[float, float, float]:
    x0, y0, z0 = llh_to_ecef(ref_lat_deg, ref_lon_deg, ref_h_m)
    dx, dy, dz = x - x0, y - y0, z - z0
    lat, lon = math.radians(ref_lat_deg), math.radians(ref_lon_deg)
    sin_lat, cos_lat, sin_lon, cos_lon = math.sin(lat), math.cos(lat), math.sin(lon), math.cos(lon)
    e = -sin_lon * dx + cos_lon * dy
    n = -sin_lat * cos_lon * dx - sin_lat * sin_lon * dy + cos_lat * dz
    u = cos_lat * cos_lon * dx + cos_lat * sin_lon * dy + sin_lat * dz
    return e, n, u


def format_dms(value_deg: float, is_lat: bool, decimals: int = 4) -> str:
    hemi = ("N" if value_deg >= 0 else "S") if is_lat else ("E" if value_deg >= 0 else "W")
    total_seconds = round(abs(value_deg) * 3600, decimals)
    degrees = int(total_seconds // 3600)
    minutes = int((total_seconds - degrees * 3600) // 60)
    seconds = total_seconds - degrees * 3600 - minutes * 60
    width = 3 + decimals if decimals else 2
    return f"{degrees}°{minutes:02d}'{seconds:0{width}.{decimals}f}\"{hemi}"
