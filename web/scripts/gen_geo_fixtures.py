"""Emit the geodesy oracle the SPA's `geo.test.ts` and `format.test.ts` check against.

Run from the repository root:

    uv run python web/scripts/gen_geo_fixtures.py > web/src/lib/__fixtures__/geo.json

The TypeScript port in `web/src/lib/geo.ts` (LLH <-> ECEF, UTM) and `fmtDms` in
`web/src/lib/format.ts` must agree with `mtrtk.core.geo` to the millimetre, so the expected
values are produced by the Python module itself rather than typed into a test. Re-run this after
any change to `src/mtrtk/core/geo.py` and commit the result. Nothing here needs a receiver, a
database or the network.

The output is deterministic: keys are sorted, floats are Python's shortest round-trip repr, and
the point list below is the only input.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from mtrtk.core import geo

# (name, lat, lon, ellipsoidal height, note). Order is the output order.
POINTS: list[tuple[str, float, float, float, str]] = [
    ("dhaka", 23.8373506, 90.2625502, -36.268, "the base station test point"),
    ("sydney", -33.8688, 151.2093, 25.0, "southern hemisphere; brief sample point"),
    ("equator_prime_meridian", 0.0, 0.0, 0.0, "origin of both angles"),
    ("london", 51.5, -0.12, 30.0, "western longitude just west of Greenwich"),
    ("bergen", 60.39, 5.32, 12.0, "brief sample point inside the Norway UTM exception"),
    ("norway_60n_5e", 60.0, 5.0, 0.0, "Norway exception corner: plain formula gives zone 31"),
    ("svalbard_brief", 78.22, 15.63, 0.0, "brief sample point inside the Svalbard exception"),
    ("svalbard_78n_20e", 78.0, 20.0, 0.0, "Svalbard exception: plain formula gives zone 34"),
    ("arctic_78n_100w", 78.0, -100.0, 50.0, "high latitude outside any special zone"),
    ("near_pole", 89.9, 10.0, 100.0, "ecef_to_llh iteration near the polar axis"),
    ("antimeridian_east", 10.0, 179.9, 0.0, "last zone, east of the antimeridian"),
    ("antimeridian_west", 10.0, -179.9, 0.0, "first zone, west of the antimeridian"),
]

# Extra format_dms cases beyond each point's lat/lon: (value, is_lat, decimals, note).
DMS_CASES: list[tuple[float, bool, int, str]] = [
    (23.8373506, True, 4, "brief: Dhaka latitude"),
    (-90.2625502, False, 4, "brief: negative longitude -> W"),
    (45.99999999, True, 2, "brief: seconds round to 60 and carry into the degrees"),
    (-0.5, True, 1, "tests/unit/test_geo.py: zero degrees, southern"),
    (10.999999999, True, 4, "carry at four decimals"),
    (89.99999999, True, 4, "carry into 90 degrees"),
    (0.0, False, 4, "zero longitude is east"),
    (12.5, False, 0, "no decimals: seconds width is two digits"),
    (179.9, False, 4, "near the antimeridian, east"),
    (-179.9, False, 4, "near the antimeridian, west"),
    (-33.8688, True, 4, "southern latitude"),
]


def _point(name: str, lat: float, lon: float, h: float, note: str) -> dict[str, Any]:
    x, y, z = geo.llh_to_ecef(lat, lon, h)
    lat2, lon2, h2 = geo.ecef_to_llh(x, y, z)
    utm = geo.llh_to_utm(lat, lon)
    return {
        "name": name,
        "note": note,
        "llh": [lat, lon, h],
        "ecef": [x, y, z],
        "llh_roundtrip": [lat2, lon2, h2],
        "utm": {
            "zone": utm.zone,
            "hemisphere": utm.hemisphere,
            "easting": utm.easting,
            "northing": utm.northing,
            "label": utm.label,
        },
        "utm_zone": geo.utm_zone(lat, lon),
        "dms": {"lat": geo.format_dms(lat, True), "lon": geo.format_dms(lon, False)},
    }


def build() -> dict[str, Any]:
    return {
        "generator": "web/scripts/gen_geo_fixtures.py",
        "source": "mtrtk.core.geo",
        "constants": {
            "a": geo.WGS84_A,
            "f": geo.WGS84_F,
            "b": geo.WGS84_B,
            "e2": geo.WGS84_E2,
            "k0": geo.UTM_K0,
            "false_easting": geo.UTM_FALSE_EASTING,
            "false_northing_south": geo.UTM_FALSE_NORTHING_SOUTH,
        },
        # What the Python `utm_zone` actually does with the two UTM special zones. The test
        # asserts whichever behaviour is recorded here, so the fixture stays the oracle.
        "utm_exceptions": {
            "norway": geo.utm_zone(60.0, 5.0) == 32,
            "svalbard": geo.utm_zone(78.0, 20.0) == 33,
        },
        "points": [_point(*p) for p in POINTS],
        "dms_cases": [
            {
                "value": value,
                "is_lat": is_lat,
                "decimals": decimals,
                "note": note,
                "text": geo.format_dms(value, is_lat, decimals),
            }
            for value, is_lat, decimals, note in DMS_CASES
        ],
    }


def main() -> None:
    sys.stdout.write(json.dumps(build(), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
