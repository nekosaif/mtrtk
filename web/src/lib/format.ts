// Number formatting for readouts. Task 3 completes this file; Task 2 needs only these two.

/** Accuracy in metres → "2.0 cm", "1.23 m", "12 m"; "—" when unknown. */
export function fmtAcc(m: number | null | undefined): string {
  if (m == null) return "—";
  return m >= 10 ? `${m.toFixed(0)} m` : m >= 1 ? `${m.toFixed(2)} m` : `${(m * 100).toFixed(1)} cm`;
}

/** Bytes per second → "0 B/s", "812 B/s", "1.2 kB/s". */
export function fmtRate(bytesPerS: number | null | undefined): string {
  if (bytesPerS == null || bytesPerS <= 0) return "0 B/s";
  return bytesPerS >= 1024 ? `${(bytesPerS / 1024).toFixed(1)} kB/s` : `${bytesPerS.toFixed(0)} B/s`;
}
