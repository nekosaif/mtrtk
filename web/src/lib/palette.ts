/** Fixed categorical order for GNSS systems (dataviz rule: never cycled). */
export const SYSTEM_ORDER = ["GPS", "GLONASS", "Galileo", "BeiDou", "QZSS", "SBAS"] as const;
export type SystemName = (typeof SYSTEM_ORDER)[number];

/** UBX gnssId to system name (u-blox interface description, NAV-SAT / NAV-SIG). */
export const GNSS_ID_TO_NAME: Record<number, string> = {
  0: "GPS",
  1: "SBAS",
  2: "Galileo",
  3: "BeiDou",
  4: "IMES",
  5: "QZSS",
  6: "GLONASS",
  7: "NavIC",
};

const SYSTEM_VAR: Record<SystemName, string> = {
  GPS: "var(--sys-gps)",
  GLONASS: "var(--sys-glonass)",
  Galileo: "var(--sys-galileo)",
  BeiDou: "var(--sys-beidou)",
  QZSS: "var(--sys-qzss)",
  SBAS: "var(--sys-sbas)",
};

/**
 * CSS colour for a system, by name or UBX gnssId. The value is a `var(--sys-*)`
 * reference, so it follows the active theme by itself; IMES, NavIC and unknown
 * systems fall back to muted ink.
 */
export function systemColor(system: string | number): string {
  const name = typeof system === "number" ? GNSS_ID_TO_NAME[system] : system;
  return (name && (SYSTEM_VAR as Record<string, string>)[name]) || "var(--ink-3)";
}

/** Status colours are fixed (never themed) and always paired with an icon or a word. */
export const STATUS = {
  good: "var(--status-good)",
  warning: "var(--status-warning)",
  serious: "var(--status-serious)",
  critical: "var(--status-critical)",
} as const;
export type StatusLevel = keyof typeof STATUS;

/**
 * The same four levels for *text*. `STATUS` is the mark colour — a gauge fill, a badge border,
 * an icon — and it never changes with the theme. A status word set in it does have to stay
 * readable, and on the light surface the fixed amber is 1.8:1 against white, so the text form
 * is a darkened member of the same hue there. In dark it resolves to the fixed value itself.
 */
export const STATUS_TEXT = {
  good: "var(--status-good-text)",
  warning: "var(--status-warning-text)",
  serious: "var(--status-serious-text)",
  critical: "var(--status-critical-text)",
} as const satisfies Record<StatusLevel, string>;
