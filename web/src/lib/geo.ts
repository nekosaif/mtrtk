/**
 * WGS84 geodesy for the browser: LLH <-> ECEF and forward UTM (Snyder 1987 transverse Mercator
 * series). A line-for-line port of `src/mtrtk/core/geo.py`; `geo.test.ts` checks it against
 * `__fixtures__/geo.json`, which that Python module writes (`web/scripts/gen_geo_fixtures.py`).
 * Keep the two in step: a change here without a regenerated fixture fails the tests, and a
 * change to the Python side needs the fixture regenerated and this port re-checked.
 */

export const WGS84_A = 6378137.0;
export const WGS84_F = 1 / 298.257223563;
export const WGS84_B = WGS84_A * (1 - WGS84_F);
/** First eccentricity squared. */
export const WGS84_E2 = WGS84_F * (2 - WGS84_F);
/** Second eccentricity squared. */
const EP2 = WGS84_E2 / (1 - WGS84_E2);
export const UTM_K0 = 0.9996;
export const UTM_FALSE_EASTING = 500_000;
export const UTM_FALSE_NORTHING_SOUTH = 10_000_000;

const rad = (d: number): number => (d * Math.PI) / 180;
const deg = (r: number): number => (r * 180) / Math.PI;

/** Geodetic latitude/longitude (degrees) and ellipsoidal height (m) → ECEF metres. */
export function llhToEcef(latDeg: number, lonDeg: number, hM: number): [number, number, number] {
  const lat = rad(latDeg);
  const lon = rad(lonDeg);
  const sinLat = Math.sin(lat);
  const cosLat = Math.cos(lat);
  const n = WGS84_A / Math.sqrt(1 - WGS84_E2 * sinLat * sinLat);
  return [(n + hM) * cosLat * Math.cos(lon), (n + hM) * cosLat * Math.sin(lon), (n * (1 - WGS84_E2) + hM) * sinLat];
}

/** ECEF metres → [lat°, lon°, ellipsoidal height m]; iterative, converges to 1e-14 rad. */
export function ecefToLlh(x: number, y: number, z: number): [number, number, number] {
  const lon = Math.atan2(y, x);
  const p = Math.hypot(x, y);
  if (p < 1e-9) {
    // on the polar axis
    return [z >= 0 ? 90 : -90, deg(lon), Math.abs(z) - WGS84_B];
  }
  let lat = Math.atan2(z, p * (1 - WGS84_E2));
  for (let i = 0; i < 20; i++) {
    const sinLat = Math.sin(lat);
    const n = WGS84_A / Math.sqrt(1 - WGS84_E2 * sinLat * sinLat);
    const h = p / Math.cos(lat) - n;
    const next = Math.atan2(z, p * (1 - (WGS84_E2 * n) / (n + h)));
    if (Math.abs(next - lat) < 1e-14) {
      lat = next;
      break;
    }
    lat = next;
  }
  const sinLat = Math.sin(lat);
  const n = WGS84_A / Math.sqrt(1 - WGS84_E2 * sinLat * sinLat);
  return [deg(lat), deg(lon), p / Math.cos(lat) - n];
}

/** UTM zone 1..60, with the south-west Norway (32) and Svalbard (31/33/35/37) exceptions. */
export function utmZone(latDeg: number, lonDeg: number): number {
  let zone = Math.floor((lonDeg + 180) / 6) + 1;
  if (latDeg >= 56 && latDeg < 64 && lonDeg >= 3 && lonDeg < 12) zone = 32; // south-west Norway
  if (latDeg >= 72 && lonDeg >= 0 && lonDeg < 42) {
    // Svalbard
    if (lonDeg < 9) zone = 31;
    else if (lonDeg < 21) zone = 33;
    else if (lonDeg < 33) zone = 35;
    else zone = 37;
  }
  return Math.min(Math.max(zone, 1), 60);
}

export interface Utm {
  zone: number;
  hemisphere: "N" | "S";
  /** metres, false easting included */
  easting: number;
  /** metres, false northing included in the south */
  northing: number;
  /** `"46N"` */
  label: string;
}

/** Forward UTM (WGS84), accurate to ~1 mm inside the zone. Raw doubles, like the Python side. */
export function llhToUtm(latDeg: number, lonDeg: number): Utm {
  const zone = utmZone(latDeg, lonDeg);
  const hemisphere: "N" | "S" = latDeg >= 0 ? "N" : "S";
  const lat = rad(latDeg);
  const lon0 = rad((zone - 1) * 6 - 180 + 3);
  const dLon = rad(lonDeg) - lon0;
  const e2 = WGS84_E2;
  const ep2 = EP2;
  const sinLat = Math.sin(lat);
  const cosLat = Math.cos(lat);
  const tanLat = Math.tan(lat);
  const n = WGS84_A / Math.sqrt(1 - e2 * sinLat * sinLat);
  const t = tanLat * tanLat;
  const c = ep2 * cosLat * cosLat;
  const a = dLon * cosLat;
  const m =
    WGS84_A *
    ((1 - e2 / 4 - (3 * e2 ** 2) / 64 - (5 * e2 ** 3) / 256) * lat -
      ((3 * e2) / 8 + (3 * e2 ** 2) / 32 + (45 * e2 ** 3) / 1024) * Math.sin(2 * lat) +
      ((15 * e2 ** 2) / 256 + (45 * e2 ** 3) / 1024) * Math.sin(4 * lat) -
      ((35 * e2 ** 3) / 3072) * Math.sin(6 * lat));
  let easting = UTM_K0 * n * (a + ((1 - t + c) * a ** 3) / 6 + ((5 - 18 * t + t * t + 72 * c - 58 * ep2) * a ** 5) / 120);
  let northing =
    UTM_K0 *
    (m + n * tanLat * ((a * a) / 2 + ((5 - t + 9 * c + 4 * c * c) * a ** 4) / 24 + ((61 - 58 * t + t * t + 600 * c - 330 * ep2) * a ** 6) / 720));
  easting += UTM_FALSE_EASTING;
  if (hemisphere === "S") northing += UTM_FALSE_NORTHING_SOUTH;
  return { zone, hemisphere, easting, northing, label: `${zone}${hemisphere}` };
}
