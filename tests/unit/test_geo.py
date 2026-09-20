import math

import pyproj
import pytest

from mtrtk.core.geo import ecef_to_enu, ecef_to_llh, format_dms, llh_to_ecef, llh_to_utm, utm_zone

POINTS = [
    (23.8373506, 90.2625502, -36.268),  # Dhaka (the base station)
    (0.0, 0.0, 0.0),
    (89.9, 10.0, 100.0),
    (-33.8688, 151.2093, 25.0),  # Sydney
    (60.39, 5.32, 12.0),  # Bergen (UTM zone 32 exception)
]


@pytest.mark.parametrize("lat,lon,h", POINTS)
def test_llh_to_ecef_matches_pyproj(lat: float, lon: float, h: float) -> None:
    t = pyproj.Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
    ex, ey, ez = t.transform(lon, lat, h)
    x, y, z = llh_to_ecef(lat, lon, h)
    assert (x, y, z) == pytest.approx((ex, ey, ez), abs=1e-4)


@pytest.mark.parametrize("lat,lon,h", POINTS)
def test_ecef_roundtrip(lat: float, lon: float, h: float) -> None:
    lat2, lon2, h2 = ecef_to_llh(*llh_to_ecef(lat, lon, h))
    assert lat2 == pytest.approx(lat, abs=1e-9)
    assert lon2 == pytest.approx(lon, abs=1e-9)
    assert h2 == pytest.approx(h, abs=1e-4)


def test_ecef_equator_and_pole() -> None:
    assert llh_to_ecef(0, 0, 0) == pytest.approx((6378137.0, 0.0, 0.0))
    assert llh_to_ecef(90, 0, 0) == pytest.approx((0.0, 0.0, 6356752.314245), abs=1e-6)
    assert ecef_to_llh(0.0, 0.0, 6356752.314245)[0] == pytest.approx(90.0)


def test_utm_zones() -> None:
    assert utm_zone(23.8373506, 90.2625502) == 46
    assert utm_zone(-33.8688, 151.2093) == 56
    assert utm_zone(60.39, 5.32) == 32  # Norway exception
    assert utm_zone(78.22, 15.63) == 33  # Svalbard exception
    assert utm_zone(51.5, -0.12) == 30


@pytest.mark.parametrize("lat,lon,h", POINTS[:4])
def test_utm_matches_pyproj(lat: float, lon: float, h: float) -> None:
    utm = llh_to_utm(lat, lon)
    crs = pyproj.CRS.from_dict(
        {"proj": "utm", "zone": utm.zone, "south": lat < 0, "ellps": "WGS84"}
    )
    e, n = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(lon, lat)
    assert utm.easting == pytest.approx(e, abs=0.01)
    assert utm.northing == pytest.approx(n, abs=0.01)
    assert utm.hemisphere == ("S" if lat < 0 else "N")
    assert utm.label == f"{utm.zone}{utm.hemisphere}"


def test_enu_offsets() -> None:
    lat, lon, h = 23.8373506, 90.2625502, -36.268
    x, y, z = llh_to_ecef(lat, lon, h + 1.0)
    e, n, u = ecef_to_enu(lat, lon, h, x, y, z)
    assert (e, n, u) == pytest.approx((0.0, 0.0, 1.0), abs=1e-6)
    x, y, z = llh_to_ecef(lat + 1e-5, lon, h)
    e, n, u = ecef_to_enu(lat, lon, h, x, y, z)
    assert n == pytest.approx(1.1057, abs=0.002) and abs(e) < 1e-3 and abs(u) < 1e-3


def test_format_dms() -> None:
    assert format_dms(23.8373506, is_lat=True) == "23°50'14.4622\"N"
    assert format_dms(-90.2625502, is_lat=False) == "90°15'45.1807\"W"
    assert format_dms(-0.5, is_lat=True, decimals=1) == "0°30'00.0\"S"
    assert format_dms(45.99999999, is_lat=True, decimals=2) == "46°00'00.00\"N"  # carry
    assert math.isfinite(float(format_dms(1.5, is_lat=True).split("°")[0]))
