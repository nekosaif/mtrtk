import type { BaseInfo } from "./live";
import type { BaseModeView, FixInfo, Level } from "./types";
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

export interface SiteCheckView {
  level: StatusLevel;
  label: string;
  /** ΔX, ΔY, ΔZ against the site for a mismatch (or the receiver's reason), else null. */
  offset: string | null;
}

/**
 * What the RTCM 1005 check says about the active site, in the words every page uses. The socket's
 * `base` slice is authoritative once it has spoken (`site_verified` / `site_mismatch`); the query
 * covers the first view, where the slice is still empty. Nothing is said unless the receiver is
 * on a fixed site: a survey-in base has no site to check against.
 */
export function siteCheck(live: BaseInfo, view: BaseModeView | undefined): SiteCheckView | null {
  if (view && !view.available) return null;
  const mode = live.mode ?? view?.mode ?? null;
  if (mode !== "fixed" && live.verified == null) return null;
  const site = live.site ?? view?.site ?? null;
  const suffix = site ? ` · ${site}` : "";
  if (live.verified === true || (live.verified == null && view?.verified)) return { level: "good", label: `Site verified${suffix}`, offset: null };
  if (live.verified === false) {
    const m = live.mismatch;
    const offset = m?.dx != null && m.dy != null && m.dz != null ? `${m.dx.toFixed(3)}, ${m.dy.toFixed(3)}, ${m.dz.toFixed(3)} m` : m?.reason ?? null;
    return { level: "critical", label: `Site mismatch${suffix}`, offset };
  }
  return { level: "warning", label: `Not yet verified${suffix}`, offset: null };
}
