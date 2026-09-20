import { SYSTEM_ORDER, systemColor } from "@/lib/palette";
import type { SatSummary } from "@/lib/types";

/** One chip per constellation in the fixed order (others after): swatch, name, used/tracked. */
export function SystemChips({ summary }: { summary: SatSummary }) {
  const known = SYSTEM_ORDER.filter((n) => summary.per_gnss[n]);
  const others = Object.keys(summary.per_gnss).filter((n) => !(SYSTEM_ORDER as readonly string[]).includes(n));
  const names = [...known, ...others];
  if (names.length === 0) return <p className="text-ink-2">No satellites yet.</p>;
  return (
    <ul aria-label="Satellites by system" className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-1">
      {names.map((name) => {
        const s = summary.per_gnss[name];
        return (
          <li key={name} className="flex items-center gap-2 rounded-md border border-line px-3 py-2">
            <span className="inline-block size-2.5 shrink-0 rounded-sm" style={{ background: systemColor(name) }} aria-hidden />
            <span className="min-w-0 flex-1">{name}</span>
            <span className="num">
              {s.used}
              <span className="text-ink-2">/{s.tracked}</span>
            </span>
          </li>
        );
      })}
    </ul>
  );
}
