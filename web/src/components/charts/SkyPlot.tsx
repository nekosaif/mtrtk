import { useMemo, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { SYSTEM_ORDER, systemColor } from "@/lib/palette";
import type { Satellite } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Legend } from "./Legend";

const RING_ELEVATIONS = [0, 30, 60] as const;
const CARDINALS: [string, number][] = [["N", 0], ["E", 90], ["S", 180], ["W", 270]];
/** RINEX 3 system letters; a system without one keeps its full name. */
const SYSTEM_LETTER: Record<string, string> = { GPS: "G", GLONASS: "R", Galileo: "E", BeiDou: "C", QZSS: "J", SBAS: "S", NavIC: "I" };

/** `G12`, `R3`, `E4`, `C21`, `J194` (RINEX); `IMES 7` for systems without a letter. */
export function satId(s: Pick<Satellite, "gnss" | "sv_id">): string {
  const letter = SYSTEM_LETTER[s.gnss];
  return letter ? `${letter}${s.sv_id}` : `${s.gnss} ${s.sv_id}`;
}

/** Disc radius in px: 3.5 at 0 dB-Hz up to 8 at 55 dB-Hz, clamped. */
export function radiusForCno(cno: number): number {
  return 3.5 + 4.5 * Math.min(1, Math.max(0, cno) / 55);
}

/** `G12 · 43° el · 118° az · 41 dB-Hz · used` */
export function discTitle(s: Satellite): string {
  return `${satId(s)} · ${Math.round(s.elev ?? 0)}° el · ${Math.round(s.azim ?? 0)}° az · ${Math.round(s.cno)} dB-Hz · ${s.used ? "used" : "tracked"}`;
}

const canPlace = (s: Satellite): s is Satellite & { elev: number; azim: number } => s.elev != null && s.azim != null && s.elev >= 0;

/**
 * Polar sky chart: zenith at the centre, horizon on the outer ring, north up, east right.
 * Elevation rings at 0/30/60 in brass (the one place brass is spent on a chart), a disc per
 * satellite coloured by system, sized by C/N0, filled when used in the fix and hollow when only
 * tracked. Discs glide to a new az/el unless the operator prefers reduced motion. A legend with
 * per-system counts and a table follow the plot.
 */
export function SkyPlot({ sats, size = 320, className }: { sats: Satellite[]; size?: number; className?: string }) {
  const [hover, setHover] = useState<Satellite | null>(null);
  const c = size / 2;
  const R = c - 18;
  const placed = useMemo(
    () =>
      sats.filter(canPlace).map((s) => {
        const r = ((90 - s.elev) / 90) * R;
        const a = (s.azim * Math.PI) / 180;
        return { s, cx: c + r * Math.sin(a), cy: c - r * Math.cos(a) };
      }),
    [sats, R, c],
  );
  const used = sats.filter((s) => s.used).length;
  if (sats.length === 0) return <EmptyState title="No satellites tracked yet" body="Discs appear here as the receiver acquires signals." />;

  const legendItems = SYSTEM_ORDER.filter((name) => sats.some((s) => s.gnss === name)).map((name) => ({
    label: name,
    color: systemColor(name),
    value: `${sats.filter((s) => s.gnss === name && s.used).length}/${sats.filter((s) => s.gnss === name).length}`,
  }));
  const rows = [...sats].sort((a, b) => SYSTEM_ORDER.indexOf(a.gnss as never) - SYSTEM_ORDER.indexOf(b.gnss as never) || a.sv_id - b.sv_id);

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <svg
        role="img"
        aria-label={`Sky plot: ${sats.length} satellites, ${used} used`}
        viewBox={`0 0 ${size} ${size}`}
        width="100%"
        style={{ maxWidth: size }}
        className="mx-auto block"
      >
        {RING_ELEVATIONS.map((elev) => (
          <circle
            key={elev}
            data-ring={elev}
            cx={c}
            cy={c}
            r={((90 - elev) / 90) * R}
            fill={elev === 0 ? "var(--panel-2)" : "none"}
            stroke="var(--brass)"
            strokeOpacity={elev === 0 ? 0.9 : 0.45}
            strokeWidth={elev === 0 ? 1.5 : 1}
          />
        ))}
        <line x1={c} y1={c - R} x2={c} y2={c + R} stroke="var(--line)" />
        <line x1={c - R} y1={c} x2={c + R} y2={c} stroke="var(--line)" />
        {RING_ELEVATIONS.slice(1).map((elev) => (
          <text key={elev} x={c + 4} y={c - ((90 - elev) / 90) * R - 4} className="num" fontSize={12} fill="var(--ink-3)">
            {elev}°
          </text>
        ))}
        {CARDINALS.map(([label, az]) => {
          const a = (az * Math.PI) / 180;
          return (
            <text key={label} x={c + (R + 11) * Math.sin(a)} y={c - (R + 11) * Math.cos(a) + 4} textAnchor="middle" fontSize={12} fill="var(--ink-2)">
              {label}
            </text>
          );
        })}
        {placed.map(({ s, cx, cy }) => {
          const color = systemColor(s.gnss);
          const key = `${s.gnss}-${s.sv_id}`;
          return (
            <circle
              key={key}
              data-sat={key}
              cx={cx}
              cy={cy}
              r={radiusForCno(s.cno)}
              fill={s.used ? color : "none"}
              stroke={color}
              strokeWidth={1.5}
              className="motion-safe:[transition:cx_0.8s_ease,cy_0.8s_ease,r_0.4s_ease]"
              onMouseEnter={() => setHover(s)}
              onMouseLeave={() => setHover(null)}
            >
              <title>{discTitle(s)}</title>
            </circle>
          );
        })}
      </svg>
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1">
        <Legend items={legendItems} label="Systems" />
        <span className="num min-h-4 text-[12px] leading-4 text-ink-2">{hover ? discTitle(hover) : "Filled discs are used in the fix; hollow ones are tracked only."}</span>
      </div>
      <details className="text-[12px] leading-4">
        <summary className="cursor-pointer text-ink-2 hover:text-ink">Table</summary>
        <table className="mt-2 w-full">
          <thead>
            <tr className="text-ink-2">
              <th scope="col" className="py-1 text-left font-medium">Satellite</th>
              <th scope="col" className="py-1 text-left font-medium">System</th>
              <th scope="col" className="py-1 text-right font-medium">Elevation</th>
              <th scope="col" className="py-1 text-right font-medium">Azimuth</th>
              <th scope="col" className="py-1 text-right font-medium">C/N0</th>
              <th scope="col" className="py-1 text-left font-medium">Used</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((s) => (
              <tr key={`${s.gnss}-${s.sv_id}`} className="border-t border-line">
                <td className="num py-1">{satId(s)}</td>
                <td className="py-1">{s.gnss}</td>
                <td className="num py-1 text-right">{s.elev != null ? `${Math.round(s.elev)}°` : "—"}</td>
                <td className="num py-1 text-right">{s.azim != null ? `${Math.round(s.azim)}°` : "—"}</td>
                <td className="num py-1 text-right">{Math.round(s.cno)} dB-Hz</td>
                <td className="py-1">{s.used ? "yes" : "no"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}
