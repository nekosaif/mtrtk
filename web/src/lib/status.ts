import type { FixInfo, Level } from "./types";
import type { StatusLevel } from "./palette";

export interface StatusText {
  level: StatusLevel;
  label: string;
}

/**
 * Map receiver fix + link state to a status level and a plain-language label.
 *
 * Order matters: a receiver the daemon says is gone outranks whatever fix it last had; stale
 * data outranks a fix that may no longer be true. fixType 5 ("time only") is what a base in
 * TMODE fixed reports — its position is the configured one, so it is good news here.
 */
export function fixLevel(fix: FixInfo | null | undefined, receiverConnected: boolean | null, stale: boolean): StatusText {
  if (receiverConnected === false) return { level: "critical", label: "Receiver disconnected" };
  if (!fix || stale) return { level: "warning", label: "Waiting for data" };
  if (fix.carr_soln === 2) return { level: "good", label: "RTK fixed" };
  if (fix.carr_soln === 1) return { level: "warning", label: "RTK float" };
  if (fix.fix_type === 5) return { level: "good", label: "Fixed position" };
  if (fix.fix_type >= 3) return { level: "good", label: fix.diff_soln ? "3D DGNSS" : "3D fix" };
  if (fix.fix_type === 2) return { level: "serious", label: "2D fix" };
  return { level: "critical", label: "No fix" };
}

export function levelForEvent(level: Level): StatusLevel {
  return level === "error" ? "critical" : level === "warning" ? "warning" : "good";
}

/** The correction-age gauge's full scale, seconds. */
export const CORR_AGE_MAX_S = 30;

/** Rover correction age: good under 5 s, warning under 10 s, critical from 10 s or with none at all. */
export function corrAgeLevel(age: number | null | undefined): StatusLevel {
  if (age == null) return "critical";
  return age < 5 ? "good" : age < 10 ? "warning" : "critical";
}

/**
 * Where the base lies, seen from the rover, in degrees from north. UBX-NAV-RELPOSNED's
 * `relPosHeading` (`rtk.heading_deg`) is the heading of the relative-position vector, which runs
 * from the base to the rover; walking back to the base is the opposite direction.
 */
export function bearingToBase(relPosHeadingDeg: number): number {
  return (((relPosHeadingDeg + 180) % 360) + 360) % 360;
}
