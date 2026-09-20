/**
 * The oracle is `__fixtures__/geo.json`, written by `mtrtk.core.geo` itself through
 * `uv run python web/scripts/gen_geo_fixtures.py > web/src/lib/__fixtures__/geo.json` (R9).
 * The literal values further down are the brief's own pyproj-derived numbers, kept because they
 * pin the TypeScript port against an independent implementation as well.
 */
import fixture from "./__fixtures__/geo.json";
import { ecefToLlh, llhToEcef, llhToUtm, utmZone } from "./geo";

interface FixturePoint {
  name: string;
  note: string;
  llh: [number, number, number];
  ecef: [number, number, number];
  llh_roundtrip: [number, number, number];
  utm: { zone: number; hemisphere: string; easting: number; northing: number; label: string };
  utm_zone: number;
  dms: { lat: string; lon: string };
}
interface GeoFixture {
  constants: { a: number; f: number; b: number; e2: number; k0: number; false_easting: number; false_northing_south: number };
  utm_exceptions: { norway: boolean; svalbard: boolean };
  points: FixturePoint[];
}
const oracle = fixture as unknown as GeoFixture;

const ECEF_TOL_M = 1e-6;
const LLH_TOL_DEG = 1e-9;
const HEIGHT_TOL_M = 1e-6;
const UTM_TOL_M = 1e-4;

describe("geo fixture (Python oracle)", () => {
  it("covers the points the ruling asks for", () => {
    const names = oracle.points.map((p) => p.name);
    expect(oracle.points.length).toBeGreaterThanOrEqual(8);
    expect(names).toEqual(expect.arrayContaining(["dhaka", "sydney", "equator_prime_meridian", "norway_60n_5e", "svalbard_78n_20e", "arctic_78n_100w", "antimeridian_east", "antimeridian_west"]));
    const dhaka = oracle.points.find((p) => p.name === "dhaka")!;
    expect(dhaka.llh).toEqual([23.8373506, 90.2625502, -36.268]);
  });

  it.each(oracle.points.map((p) => [p.name, p] as const))("llhToEcef(%s) matches Python to 1e-6 m", (_name, p) => {
    const [x, y, z] = llhToEcef(...p.llh);
    expect(Math.abs(x - p.ecef[0])).toBeLessThan(ECEF_TOL_M);
    expect(Math.abs(y - p.ecef[1])).toBeLessThan(ECEF_TOL_M);
    expect(Math.abs(z - p.ecef[2])).toBeLessThan(ECEF_TOL_M);
  });

  it.each(oracle.points.map((p) => [p.name, p] as const))("ecefToLlh(%s) matches Python's round trip to 1e-9 deg", (_name, p) => {
    const [lat, lon, h] = ecefToLlh(...p.ecef);
    expect(Math.abs(lat - p.llh_roundtrip[0])).toBeLessThan(LLH_TOL_DEG);
    expect(Math.abs(lon - p.llh_roundtrip[1])).toBeLessThan(LLH_TOL_DEG);
    expect(Math.abs(h - p.llh_roundtrip[2])).toBeLessThan(HEIGHT_TOL_M);
    // and the round trip really does come back to the input
    expect(Math.abs(lat - p.llh[0])).toBeLessThan(LLH_TOL_DEG);
    expect(Math.abs(lon - p.llh[1])).toBeLessThan(LLH_TOL_DEG);
    expect(Math.abs(h - p.llh[2])).toBeLessThan(1e-4);
  });

  it.each(oracle.points.map((p) => [p.name, p] as const))("utmZone / llhToUtm(%s) match Python to 1e-4 m", (_name, p) => {
    expect(utmZone(p.llh[0], p.llh[1])).toBe(p.utm_zone);
    const u = llhToUtm(p.llh[0], p.llh[1]);
    expect(u.zone).toBe(p.utm.zone);
    expect(u.hemisphere).toBe(p.utm.hemisphere);
    expect(u.label).toBe(p.utm.label);
    expect(Math.abs(u.easting - p.utm.easting)).toBeLessThan(UTM_TOL_M);
    expect(Math.abs(u.northing - p.utm.northing)).toBeLessThan(UTM_TOL_M);
  });

  it("implements the UTM special zones exactly as the Python module does", () => {
    // The fixture records what `mtrtk.core.geo.utm_zone` does; the port must do the same thing.
    expect(oracle.utm_exceptions).toEqual({ norway: true, svalbard: true });
    expect(utmZone(60.0, 5.0)).toBe(oracle.utm_exceptions.norway ? 32 : 31);
    expect(utmZone(78.0, 20.0)).toBe(oracle.utm_exceptions.svalbard ? 33 : 34);
    // just outside the Norway box the plain formula applies again
    expect(utmZone(55.9, 5.0)).toBe(31);
    expect(utmZone(64.0, 5.0)).toBe(31);
    expect(utmZone(60.0, 12.0)).toBe(33);
    // Svalbard bands
    expect(utmZone(75, 5)).toBe(31);
    expect(utmZone(75, 15)).toBe(33);
    expect(utmZone(75, 25)).toBe(35);
    expect(utmZone(75, 40)).toBe(37);
    expect(utmZone(75, 42)).toBe(38);
  });

  it("clamps the zone to 1..60 at the antimeridian", () => {
    expect(utmZone(10, 180)).toBe(60);
    expect(utmZone(10, -180)).toBe(1);
  });
});

// Reference values computed with pyproj (WGS84) during planning.
describe("geo (brief literals)", () => {
  it("llh -> ecef matches pyproj", () => {
    const [x, y, z] = llhToEcef(23.8373506, 90.2625502, -36.268);
    expect(x).toBeCloseTo(-26748.172, 3);
    expect(y).toBeCloseTo(5837156.6184, 3);
    expect(z).toBeCloseTo(2561801.2607, 3);
    const [sx, sy, sz] = llhToEcef(-33.8688, 151.2093, 25.0);
    // Brief wrote Y as 2553216.34 (two decimals) into a three-decimal comparison; pyproj and the
    // Python module both give 2553216.33946.
    expect([sx, sy, sz].map((v) => Math.round(v * 1000) / 1000)).toEqual([-4646069.464, 2553216.339, -3534386.32].map((v) => Math.round(v * 1000) / 1000));
  });
  it("round trips", () => {
    const [lat, lon, h] = ecefToLlh(...llhToEcef(23.8373506, 90.2625502, -36.268));
    expect(lat).toBeCloseTo(23.8373506, 8);
    expect(lon).toBeCloseTo(90.2625502, 8);
    expect(h).toBeCloseTo(-36.268, 3);
    expect(llhToEcef(0, 0, 0)[0]).toBeCloseTo(6378137, 6);
  });
  it("handles the polar axis without dividing by zero", () => {
    const b = 6356752.314245179;
    expect(ecefToLlh(0, 0, b)).toEqual([90, 0, expect.closeTo(0, 6)]);
    expect(ecefToLlh(0, 0, -(b + 10))).toEqual([-90, 0, expect.closeTo(10, 6)]);
  });
  it("utm matches pyproj", () => {
    expect(utmZone(23.8373506, 90.2625502)).toBe(46);
    expect(utmZone(60.39, 5.32)).toBe(32);
    expect(utmZone(78.22, 15.63)).toBe(33);
    const d = llhToUtm(23.8373506, 90.2625502);
    expect(d.zone).toBe(46);
    expect(d.hemisphere).toBe("N");
    expect(d.easting).toBeCloseTo(221150.294, 2);
    expect(d.northing).toBeCloseTo(2638912.702, 2);
    const s = llhToUtm(-33.8688, 151.2093);
    expect(s.label).toBe("56S");
    expect(s.easting).toBeCloseTo(334368.634, 2);
    expect(s.northing).toBeCloseTo(6250948.345, 2);
  });
});
