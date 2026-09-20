/**
 * Readout formatting. Every function here is pure and locale-independent: numbers go through
 * `toFixed`/`padStart` (always ASCII digits and a "." decimal point, whatever the browser
 * locale), times go through the `Date` UTC accessors, and nothing touches `Intl` or
 * `toLocaleString`. `fmtLocal` is the one function that reads the browser's time zone; it is
 * for the local-time tooltip next to a UTC readout.
 *
 * Bytes are decimal (SI): 1 kB = 1000 B, 1 GB = 1e9 B — the same convention as the API's
 * `disk_free_gb` (`bytes / 1e9`, see `src/mtrtk/system.py`), and the one the kB/MB/GB symbols
 * stand for. `bytes`, `total_bytes`, `bytes_sent` and `bytes_per_s` arrive as raw numbers.
 */
import { llhToEcef, llhToUtm } from "./geo";
import type { Position } from "./types";

export type CoordMode = "dd" | "dms" | "utm" | "ecef";
export const COORD_MODES: readonly { value: CoordMode; label: string }[] = [
  { value: "dd", label: "Decimal degrees" },
  { value: "dms", label: "Degrees minutes seconds" },
  { value: "utm", label: "UTM" },
  { value: "ecef", label: "ECEF" },
];
export function isCoordMode(v: unknown): v is CoordMode {
  return COORD_MODES.some((m) => m.value === v);
}

/** The em dash shown for an unknown value. */
export const DASH = "—";

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** `toFixed` without a "-0.000" for values that round to zero from below. */
function fixed(v: number, digits: number): string {
  const s = v.toFixed(digits);
  return s.startsWith("-") && Number(s) === 0 ? s.slice(1) : s;
}

// ------------------------------------------------------------------ quantities

/** Accuracy in metres → "2.0 cm", "1.23 m", "12 m"; "—" when unknown. */
export function fmtAcc(m: number | null | undefined): string {
  if (!isNum(m)) return DASH;
  return m >= 10 ? `${m.toFixed(0)} m` : m >= 1 ? `${m.toFixed(2)} m` : `${(m * 100).toFixed(1)} cm`;
}

/** Bytes per second → "0 B/s", "812 B/s", "1.9 kB/s", "1.25 MB/s" (decimal). */
export function fmtRate(bytesPerS: number | null | undefined): string {
  if (!isNum(bytesPerS) || bytesPerS <= 0) return "0 B/s";
  if (bytesPerS >= 1e6) return `${(bytesPerS / 1e6).toFixed(2)} MB/s`;
  if (bytesPerS >= 1e3) return `${(bytesPerS / 1e3).toFixed(1)} kB/s`;
  return `${bytesPerS.toFixed(0)} B/s`;
}

/** Byte count → "0 B", "1.5 kB", "2.5 MB", "5.5 GB", "2.5 TB" (decimal); "—" when unknown. */
export function fmtBytes(n: number | null | undefined): string {
  if (!isNum(n) || n < 0) return DASH;
  if (n < 1e3) return `${n.toFixed(0)} B`;
  if (n < 1e6) return `${(n / 1e3).toFixed(1)} kB`;
  if (n < 1e9) return `${(n / 1e6).toFixed(1)} MB`;
  if (n < 1e12) return `${(n / 1e9).toFixed(1)} GB`;
  return `${(n / 1e12).toFixed(1)} TB`;
}

/** Metres with a fixed number of decimals (3 = millimetres). */
export function fmtMeters(m: number | null | undefined, digits = 3): string {
  return isNum(m) ? `${fixed(m, digits)} m` : DASH;
}

/** Seconds → "59s", "1m 05s", "1h 02m 03s" (hours do not roll into days). */
export function fmtDuration(s: number | null | undefined): string {
  if (!isNum(s)) return DASH;
  const total = Math.max(0, Math.round(s));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  const two = (v: number) => String(v).padStart(2, "0");
  if (h > 0) return `${h}h ${two(m)}m ${two(sec)}s`;
  if (m > 0) return `${m}m ${two(sec)}s`;
  return `${sec}s`;
}

// ----------------------------------------------------------------------- times

const HAS_ZONE = /(Z|[+-]\d{2}:?\d{2})$/i;
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

/**
 * Parse an API timestamp. The daemon's datetimes are UTC by contract, so an offset-less string
 * (which V8 would read as browser-local time) is pinned to UTC. Returns null when unparsable.
 */
export function parseUtc(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  let s = iso.trim();
  if (!s) return null;
  if (s.includes(" ") && !s.includes("T")) s = s.replace(" ", "T");
  if (!HAS_ZONE.test(s) && !DATE_ONLY.test(s)) s += "Z"; // a date alone is already UTC in JS
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "16:47:34" in UTC, whatever the browser's zone; "—" when missing or unparsable. */
export function fmtUtc(iso: string | null | undefined): string {
  const d = parseUtc(iso);
  return d ? d.toISOString().slice(11, 19) : DASH;
}

/** "2026-09-18 16:47:34 UTC". */
export function fmtUtcDate(iso: string | null | undefined): string {
  const d = parseUtc(iso);
  if (!d) return DASH;
  const s = d.toISOString();
  return `${s.slice(0, 10)} ${s.slice(11, 19)} UTC`;
}

/**
 * The same instant in the browser's zone, with its offset spelled out:
 * "2026-09-18 22:47:34 UTC+06:00". For the tooltip on a UTC readout — never for the readout.
 */
export function fmtLocal(iso: string | null | undefined): string {
  const d = parseUtc(iso);
  if (!d) return DASH;
  const two = (v: number) => String(v).padStart(2, "0");
  const offMin = -d.getTimezoneOffset();
  const sign = offMin < 0 ? "-" : "+";
  const abs = Math.abs(offMin);
  return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())} UTC${sign}${two(Math.floor(abs / 60))}:${two(abs % 60)}`;
}

/** "12 s ago", "47 min ago", "3 h ago", "2 d ago" relative to `now` (epoch ms; pass a ticking clock). */
export function relTime(iso: string | null | undefined, now: number): string {
  const d = parseUtc(iso);
  if (!d || !isNum(now)) return DASH;
  const dt = Math.max(0, Math.round((now - d.getTime()) / 1000));
  if (dt < 60) return `${dt} s ago`;
  if (dt < 3600) return `${Math.floor(dt / 60)} min ago`;
  if (dt < 86400) return `${Math.floor(dt / 3600)} h ago`;
  return `${Math.floor(dt / 86400)} d ago`;
}

// ----------------------------------------------------------------- coordinates

/**
 * Degrees → `23°50'14.4622"N`. Mirrors `mtrtk.core.geo.format_dms` step for step: the total
 * seconds are rounded first, so 45.99999999° carries to `46°00'00.00"N` instead of `45°60'…`.
 */
export function fmtDms(value: number, isLat: boolean, decimals = 4): string {
  const hemi = isLat ? (value >= 0 ? "N" : "S") : value >= 0 ? "E" : "W";
  let totalSeconds = Number((Math.abs(value) * 3600).toFixed(decimals));
  const d = Math.floor(totalSeconds / 3600);
  totalSeconds -= d * 3600;
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds - m * 60;
  const width = decimals ? 3 + decimals : 2;
  return `${d}°${String(m).padStart(2, "0")}'${s.toFixed(decimals).padStart(width, "0")}"${hemi}`;
}

/** ECEF metres → `["X -26748.1720  Y 5837156.6184", "Z 2561801.2607"]`. */
export function fmtEcef(x: number | null | undefined, y: number | null | undefined, z: number | null | undefined): [string, string] {
  if (!isNum(x) || !isNum(y) || !isNum(z)) return [DASH, DASH];
  return [`X ${fixed(x, 4)}  Y ${fixed(y, 4)}`, `Z ${fixed(z, 4)}`];
}

/**
 * A latitude/longitude pair in the operator's coordinate mode, as two strings for two lines.
 * `h` is the ellipsoidal height the ECEF mode needs (default 0 — prefer `fmtPosition`, which
 * uses the receiver's own ECEF and never invents a height).
 */
export function fmtCoord(lat: number | null | undefined, lon: number | null | undefined, mode: CoordMode, h = 0): [string, string] {
  if (!isNum(lat) || !isNum(lon)) return [DASH, DASH];
  switch (mode) {
    case "dd":
      return [`${fixed(lat, 7)}°`, `${fixed(lon, 7)}°`];
    case "dms":
      return [fmtDms(lat, true), fmtDms(lon, false)];
    case "utm": {
      const u = llhToUtm(lat, lon);
      return [u.label, `E ${u.easting.toFixed(3)}  N ${u.northing.toFixed(3)}`];
    }
    case "ecef": {
      const [x, y, z] = llhToEcef(lat, lon, h);
      return fmtEcef(x, y, z);
    }
  }
}

/** Ellipsoidal and mean-sea-level heights as `["-36.268 m", "13.200 m"]`; label them on the page. */
export function fmtHeights(pos: Pick<Position, "height_m" | "hmsl_m"> | null | undefined, digits = 3): [string, string] {
  return [fmtMeters(pos?.height_m, digits), fmtMeters(pos?.hmsl_m, digits)];
}

type PositionLike = Partial<Pick<Position, "lat" | "lon" | "height_m" | "ecef_x_m" | "ecef_y_m" | "ecef_z_m" | "invalid_llh">>;

/**
 * The receiver's position in the operator's coordinate mode. ECEF mode shows the receiver's
 * own `ecef_*_m` (NAV-HPPOSECEF) when present and derives it from LLH + `height_m` otherwise;
 * with neither, or with `invalid_llh`, it shows dashes rather than a made-up height.
 */
export function fmtPosition(pos: PositionLike | null | undefined, mode: CoordMode): [string, string] {
  if (!pos || pos.invalid_llh) return [DASH, DASH];
  if (mode === "ecef") {
    if (isNum(pos.ecef_x_m) && isNum(pos.ecef_y_m) && isNum(pos.ecef_z_m)) return fmtEcef(pos.ecef_x_m, pos.ecef_y_m, pos.ecef_z_m);
    if (!isNum(pos.height_m)) return [DASH, DASH];
    return fmtCoord(pos.lat, pos.lon, "ecef", pos.height_m);
  }
  return fmtCoord(pos.lat, pos.lon, mode);
}
