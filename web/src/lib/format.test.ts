import fixture from "./__fixtures__/geo.json";
import {
  COORD_MODES,
  DASH,
  fmtAcc,
  fmtBytes,
  fmtCoord,
  fmtDms,
  fmtDuration,
  fmtEcef,
  fmtHeights,
  fmtLocal,
  fmtMeters,
  fmtPosition,
  fmtRate,
  fmtUtc,
  fmtUtcDate,
  isCoordMode,
  relTime,
} from "./format";

interface DmsCase {
  value: number;
  is_lat: boolean;
  decimals: number;
  note: string;
  text: string;
}
const dmsCases = (fixture as unknown as { dms_cases: DmsCase[]; points: { name: string; llh: [number, number, number]; dms: { lat: string; lon: string } }[] }).dms_cases;
const points = (fixture as unknown as { points: { name: string; llh: [number, number, number]; dms: { lat: string; lon: string } }[] }).points;

const NOW = Date.parse("2026-09-18T16:47:46Z");

describe("format", () => {
  it("accuracy switches units", () => {
    expect(fmtAcc(0.012)).toBe("1.2 cm");
    expect(fmtAcc(1.234)).toBe("1.23 m");
    expect(fmtAcc(12.3)).toBe("12 m");
    expect(fmtAcc(null)).toBe("—");
    expect(fmtAcc(undefined)).toBe(DASH);
    expect(fmtAcc(Number.NaN)).toBe(DASH);
  });

  it("rates and bytes", () => {
    expect(fmtRate(0)).toBe("0 B/s");
    expect(fmtRate(1900)).toBe("1.9 kB/s");
    expect(fmtBytes(0)).toBe("0 B");
    expect(fmtBytes(1536)).toBe("1.5 kB");
    expect(fmtBytes(5.5e9)).toBe("5.5 GB");
  });

  it("bytes are decimal (SI), like the API's disk_free_gb = bytes / 1e9", () => {
    expect(fmtBytes(999)).toBe("999 B");
    expect(fmtBytes(1000)).toBe("1.0 kB");
    expect(fmtBytes(1_000_000)).toBe("1.0 MB");
    expect(fmtBytes(2_500_000)).toBe("2.5 MB");
    expect(fmtBytes(1e9)).toBe("1.0 GB");
    expect(fmtBytes(2.5e12)).toBe("2.5 TB");
    expect(fmtBytes(null)).toBe(DASH);
    expect(fmtBytes(-1)).toBe(DASH);
    expect(fmtRate(812)).toBe("812 B/s");
    expect(fmtRate(1000)).toBe("1.0 kB/s");
    expect(fmtRate(1_250_000)).toBe("1.25 MB/s");
    expect(fmtRate(null)).toBe("0 B/s");
    expect(fmtRate(-5)).toBe("0 B/s");
  });

  it("metres", () => {
    expect(fmtMeters(-36.268)).toBe("-36.268 m");
    expect(fmtMeters(13, 1)).toBe("13.0 m");
    expect(fmtMeters(null)).toBe(DASH);
  });

  it("durations and times", () => {
    expect(fmtDuration(3723)).toBe("1h 02m 03s");
    expect(fmtDuration(59)).toBe("59s");
    expect(fmtUtc("2026-09-18T16:47:34+00:00")).toBe("16:47:34");
    expect(relTime("2026-09-18T16:47:34+00:00", Date.parse("2026-09-18T16:47:46Z"))).toBe("12 s ago");
    expect(relTime("2026-09-18T16:00:00+00:00", Date.parse("2026-09-18T16:47:46Z"))).toBe("47 min ago");
  });

  it("durations: minutes, rounding, days, unknown", () => {
    expect(fmtDuration(0)).toBe("0s");
    expect(fmtDuration(65)).toBe("1m 05s");
    expect(fmtDuration(3600)).toBe("1h 00m 00s");
    expect(fmtDuration(90061.4)).toBe("25h 01m 01s");
    expect(fmtDuration(-3)).toBe("0s");
    expect(fmtDuration(null)).toBe(DASH);
    expect(fmtDuration(Number.NaN)).toBe(DASH);
  });

  it("UTC times never depend on the browser zone or locale", () => {
    expect(fmtUtcDate("2026-09-18T16:47:34+00:00")).toBe("2026-09-18 16:47:34 UTC");
    expect(fmtUtc("2026-09-18T16:47:34Z")).toBe("16:47:34");
    expect(fmtUtc("2026-09-18T16:47:34.123456+00:00")).toBe("16:47:34");
    expect(fmtUtc("2026-09-18T22:47:34+06:00")).toBe("16:47:34");
    // An offset-less stamp from the API is UTC by contract (V8 would read it as local time).
    expect(fmtUtc("2026-09-18T16:47:34")).toBe("16:47:34");
    expect(fmtUtcDate("2026-09-18 16:47:34")).toBe("2026-09-18 16:47:34 UTC");
    expect(fmtUtcDate("2026-09-18")).toBe("2026-09-18 00:00:00 UTC");
    expect(fmtUtc("2026-09-18T16:47:34-0230")).toBe("19:17:34");
    expect(fmtUtc(null)).toBe(DASH);
    expect(fmtUtc("")).toBe(DASH);
    expect(fmtUtc("not a date")).toBe(DASH);
    expect(fmtUtcDate("not a date")).toBe(DASH);
  });

  it("fmtLocal renders the browser's zone with an explicit UTC offset (for tooltips)", () => {
    const saved = process.env.TZ;
    try {
      process.env.TZ = "Asia/Dhaka";
      expect(fmtLocal("2026-09-18T16:47:34+00:00")).toBe("2026-09-18 22:47:34 UTC+06:00");
      process.env.TZ = "America/St_Johns";
      expect(fmtLocal("2026-09-18T16:47:34+00:00")).toBe("2026-09-18 14:17:34 UTC-02:30");
      process.env.TZ = "UTC";
      expect(fmtLocal("2026-09-18T16:47:34+00:00")).toBe("2026-09-18 16:47:34 UTC+00:00");
      expect(fmtLocal(null)).toBe(DASH);
      expect(fmtLocal("nope")).toBe(DASH);
    } finally {
      if (saved === undefined) delete process.env.TZ;
      else process.env.TZ = saved;
    }
  });

  it("relTime is pure in `now` and steps through its units", () => {
    expect(relTime("2026-09-18T16:47:46Z", NOW)).toBe("0 s ago");
    expect(relTime("2026-09-18T16:48:00Z", NOW)).toBe("0 s ago"); // future clamps
    expect(relTime("2026-09-18T16:46:46Z", NOW)).toBe("1 min ago");
    expect(relTime("2026-09-18T13:47:46Z", NOW)).toBe("3 h ago");
    expect(relTime("2026-09-16T16:47:46Z", NOW)).toBe("2 d ago");
    expect(relTime("2026-09-18T16:47:34", NOW)).toBe("12 s ago"); // offset-less = UTC
    expect(relTime(null, NOW)).toBe(DASH);
    expect(relTime("garbage", NOW)).toBe(DASH);
  });

  it("DMS matches the backend formatter", () => {
    expect(fmtDms(23.8373506, true)).toBe("23°50'14.4622\"N");
    expect(fmtDms(-90.2625502, false)).toBe("90°15'45.1807\"W");
    expect(fmtDms(45.99999999, true, 2)).toBe("46°00'00.00\"N");
  });

  it.each(dmsCases.map((c) => [`${c.value} ${c.is_lat ? "lat" : "lon"} @${c.decimals}`, c] as const))("fmtDms(%s) equals Python format_dms", (_label, c) => {
    expect(fmtDms(c.value, c.is_lat, c.decimals)).toBe(c.text);
  });

  it.each(points.map((p) => [p.name, p] as const))("fmtDms for the %s fixture point equals Python", (_name, p) => {
    expect(fmtDms(p.llh[0], true)).toBe(p.dms.lat);
    expect(fmtDms(p.llh[1], false)).toBe(p.dms.lon);
  });

  it("fmtCoord modes", () => {
    expect(fmtCoord(23.8373506, 90.2625502, "dd")).toEqual(["23.8373506°", "90.2625502°"]);
    expect(fmtCoord(23.8373506, 90.2625502, "dms")[0]).toBe("23°50'14.4622\"N");
    // Brief said "E 221150.294": that is pyproj's exact TM (221150.293509). The Snyder series in
    // mtrtk.core.geo gives 221150.293499, 10 µm away across the rounding boundary — oracle wins.
    expect(fmtCoord(23.8373506, 90.2625502, "utm")).toEqual(["46N", "E 221150.293  N 2638912.702"]);
    expect(fmtCoord(null, null, "dd")).toEqual(["—", "—"]);
  });

  it("fmtCoord: dms, ecef, negative zero, and half a coordinate", () => {
    expect(fmtCoord(23.8373506, 90.2625502, "dms")).toEqual(["23°50'14.4622\"N", "90°15'45.1807\"E"]);
    expect(fmtCoord(-33.8688, 151.2093, "utm")).toEqual(["56S", "E 334368.634  N 6250948.345"]);
    expect(fmtCoord(23.8373506, 90.2625502, "ecef", -36.268)).toEqual(["X -26748.1720  Y 5837156.6184", "Z 2561801.2607"]);
    expect(fmtCoord(0, 0, "ecef")).toEqual(["X 6378137.0000  Y 0.0000", "Z 0.0000"]);
    expect(fmtCoord(-0.00000001, 0, "dd")).toEqual(["0.0000000°", "0.0000000°"]);
    expect(fmtCoord(23.8, null, "dd")).toEqual([DASH, DASH]);
    expect(fmtCoord(undefined, 90.2, "utm")).toEqual([DASH, DASH]);
  });

  it("fmtEcef and fmtHeights", () => {
    expect(fmtEcef(-26748.17198672455, 5837156.618418689, 2561801.2607014133)).toEqual(["X -26748.1720  Y 5837156.6184", "Z 2561801.2607"]);
    expect(fmtEcef(null, 1, 2)).toEqual([DASH, DASH]);
    expect(fmtHeights({ height_m: -36.268, hmsl_m: 13.2 })).toEqual(["-36.268 m", "13.200 m"]);
    expect(fmtHeights({ height_m: null, hmsl_m: null })).toEqual([DASH, DASH]);
    expect(fmtHeights(null)).toEqual([DASH, DASH]);
  });

  it("fmtPosition prefers the receiver's own ECEF and never invents a height", () => {
    const pos = { lat: 23.8373506, lon: 90.2625502, height_m: -36.268, hmsl_m: 13.2, ecef_x_m: -26748.1, ecef_y_m: 5837156.6, ecef_z_m: 2561801.2, invalid_llh: false };
    expect(fmtPosition(pos, "dd")).toEqual(["23.8373506°", "90.2625502°"]);
    expect(fmtPosition(pos, "utm")).toEqual(["46N", "E 221150.293  N 2638912.702"]);
    expect(fmtPosition(pos, "ecef")).toEqual(["X -26748.1000  Y 5837156.6000", "Z 2561801.2000"]);
    // no receiver ECEF: derive it from LLH + ellipsoidal height
    expect(fmtPosition({ ...pos, ecef_x_m: null, ecef_y_m: null, ecef_z_m: null }, "ecef")).toEqual(["X -26748.1720  Y 5837156.6184", "Z 2561801.2607"]);
    // no ECEF and no height: nothing to derive from
    expect(fmtPosition({ ...pos, ecef_x_m: null, ecef_y_m: null, ecef_z_m: null, height_m: null }, "ecef")).toEqual([DASH, DASH]);
    expect(fmtPosition({ ...pos, invalid_llh: true }, "dd")).toEqual([DASH, DASH]);
    expect(fmtPosition(null, "dd")).toEqual([DASH, DASH]);
    expect(fmtPosition(undefined, "dms")).toEqual([DASH, DASH]);
  });

  it("coordinate modes", () => {
    expect(COORD_MODES.map((m) => m.value)).toEqual(["dd", "dms", "utm", "ecef"]);
    expect(isCoordMode("utm")).toBe(true);
    expect(isCoordMode("UTM")).toBe(false);
    expect(isCoordMode(null)).toBe(false);
  });
});
