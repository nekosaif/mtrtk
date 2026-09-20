import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { SYSTEM_ORDER, systemColor } from "@/lib/palette";
import type { Satellite } from "@/lib/types";
import { Legend } from "./Legend";
import { satId } from "./SkyPlot";

/** The scale is fixed so a bar means the same thing from one epoch to the next. */
export const CNO_MAX = 55;
/** Reference lines: below 20 dB-Hz a signal is hard to use; above 40 it is strong. */
export const CNO_REFS = [20, 40] as const;
/** A tracked signal with no C/N0 is drawn this tall (px), over the 1 px baseline, so it stays visible. */
export const HAIRLINE = 2;

const LEFT = 30;
const RIGHT = 6;
const TOP = 6;
const BOTTOM = 20;
const GAP = 2;
const MIN_BAR = 8;
const MAX_BAR = 26;

interface Bar {
  key: string;
  system: string;
  sv: number;
  id: string;
  signal: string;
  cno: number;
  used: boolean;
  /** Position within its satellite's signals: 0 is the primary signal. */
  idx: number;
}

function systemRank(name: string): number {
  const i = (SYSTEM_ORDER as readonly string[]).indexOf(name);
  return i === -1 ? SYSTEM_ORDER.length : i;
}

/** One entry per signal, grouped per satellite, satellites in system order then by SV id. */
export function barsOf(sats: Satellite[]): Bar[] {
  const ordered = [...sats].sort((a, b) => systemRank(a.gnss) - systemRank(b.gnss) || a.sv_id - b.sv_id);
  const bars: Bar[] = [];
  for (const s of ordered) {
    const id = satId(s);
    if (s.signals.length === 0) {
      // NAV-SIG not reported: the satellite's own C/N0 stands for its one bar
      bars.push({ key: `${s.gnss}:${s.sv_id}:`, system: s.gnss, sv: s.sv_id, id, signal: "", cno: s.cno, used: s.used, idx: 0 });
      continue;
    }
    s.signals.forEach((sig, idx) => bars.push({ key: `${s.gnss}:${s.sv_id}:${sig.name}`, system: s.gnss, sv: s.sv_id, id, signal: sig.name, cno: sig.cno, used: sig.pr_used, idx }));
  }
  return bars;
}

/** `G5 L1C/A · 45 dB-Hz · used` — the bar's tooltip and the hover readout. */
export function barTitle(b: Bar): string {
  const name = b.signal ? `${b.id} ${b.signal}` : b.id;
  const cno = b.cno > 0 ? `${Math.round(b.cno)} dB-Hz` : "no signal";
  return `${name} · ${cno} · ${b.used ? "used" : "tracked"}`;
}

/** The width of the element, followed through resizes (0 before layout, as in jsdom). */
function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(el.clientWidth);
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width] as const;
}

/**
 * C/N0 per signal as a bar chart: one bar per signal, grouped per satellite, satellites in the
 * fixed system order then by SV id. Bars take the system colour; a satellite's second and later
 * signals are drawn at 55 % so the primary reads first. The y scale is fixed at 0–55 dB-Hz with
 * ink reference lines at 20 and 40. A tracked signal with no C/N0 is a 2 px hairline over the
 * baseline, so "tracked but silent" stays visible. Bars widen to fill the container and scroll
 * sideways when more are tracked than fit. Every bar has a `<title>`; a hover readout, a legend
 * and a table follow the plot.
 */
export function CnoBars({ sats, height = 180, className }: { sats: Satellite[]; height?: number; className?: string }) {
  const [hover, setHover] = useState<Bar | null>(null);
  const [frameRef, frameW] = useWidth<HTMLDivElement>();
  const bars = useMemo(() => barsOf(sats), [sats]);
  if (bars.length === 0) return <EmptyState title="No signals tracked yet" body="Bars appear here as the receiver acquires signals." />;

  const avail = Math.max(0, frameW - LEFT - RIGHT);
  const step = Math.max(MIN_BAR + GAP, Math.min(MAX_BAR + GAP, Math.floor(avail / bars.length) || 0));
  const barW = step - GAP;
  const width = LEFT + bars.length * step - GAP + RIGHT;
  const plotH = height - TOP - BOTTOM;
  const baseline = TOP + plotH;
  const y = (cno: number) => baseline - (Math.min(CNO_MAX, Math.max(0, cno)) / CNO_MAX) * plotH;
  const used = sats.filter((s) => s.used).length;

  // one SV label per group; when groups are narrower than a label, label every k-th satellite
  const groups: { id: string; from: number; count: number }[] = [];
  bars.forEach((b, i) => {
    const last = groups[groups.length - 1];
    if (last && last.id === b.id) last.count += 1;
    else groups.push({ id: b.id, from: i, count: 1 });
  });
  const minGroupW = Math.min(...groups.map((g) => g.count * step));
  const every = Math.max(1, Math.ceil(26 / minGroupW));

  const legend = SYSTEM_ORDER.filter((n) => sats.some((s) => s.gnss === n)).map((n) => ({
    label: n,
    color: systemColor(n),
    value: `${sats.filter((s) => s.gnss === n && s.used).length}/${sats.filter((s) => s.gnss === n).length}`,
  }));

  return (
    <div className={className}>
      <div ref={frameRef} className="overflow-x-auto">
        <svg
          role="img"
          aria-label={`Signal strength: ${bars.length} signals from ${sats.length} satellites, ${used} used`}
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          className="block"
          onMouseLeave={() => setHover(null)}
        >
          {CNO_REFS.map((ref) => (
            <g key={ref}>
              <line data-ref={ref} x1={LEFT} x2={width - RIGHT} y1={y(ref)} y2={y(ref)} stroke="var(--line)" strokeDasharray="3 3" />
              <text x={LEFT - 6} y={y(ref) + 4} textAnchor="end" fontSize={12} fill="var(--ink-3)" className="num">
                {ref}
              </text>
            </g>
          ))}
          <line x1={LEFT} x2={width - RIGHT} y1={baseline} y2={baseline} stroke="var(--line)" />
          {bars.map((b, i) => {
            const silent = b.cno <= 0;
            const h = silent ? HAIRLINE : Math.max(HAIRLINE, baseline - y(b.cno));
            return (
              <rect
                key={b.key}
                data-signal={b.key}
                data-no-signal={silent ? "true" : undefined}
                x={LEFT + i * step}
                y={baseline - h}
                width={barW}
                height={h}
                rx={1.5}
                fill={systemColor(b.system)}
                fillOpacity={b.idx === 0 ? 1 : 0.55}
                stroke={hover?.key === b.key ? "var(--ink)" : "none"}
                className="motion-safe:[transition:y_0.4s_ease,height_0.4s_ease]"
                onMouseEnter={() => setHover(b)}
              >
                <title>{barTitle(b)}</title>
              </rect>
            );
          })}
          {groups.map((g, gi) =>
            gi % every === 0 ? (
              <text key={g.id} x={LEFT + g.from * step + (g.count * step - GAP) / 2} y={height - 5} textAnchor="middle" fontSize={12} fill="var(--ink-2)" className="num">
                {g.id}
              </text>
            ) : null,
          )}
        </svg>
      </div>
      <div className="mt-2 flex flex-wrap items-start justify-between gap-x-4 gap-y-1">
        <Legend items={legend} label="Systems" />
        <span className="num min-h-4 text-[12px] leading-4 text-ink-2">{hover ? barTitle(hover) : "dB-Hz · dashed lines at 20 and 40 · lighter bars are a satellite's further signals"}</span>
      </div>
      <details className="mt-2 text-[12px] leading-4">
        <summary className="cursor-pointer text-ink-2 hover:text-ink">Table</summary>
        <table className="mt-2 w-full">
          <thead>
            <tr className="text-ink-2">
              <th scope="col" className="py-1 text-left font-medium">Satellite</th>
              <th scope="col" className="py-1 text-left font-medium">System</th>
              <th scope="col" className="py-1 text-left font-medium">Signal</th>
              <th scope="col" className="py-1 text-right font-medium">C/N0</th>
              <th scope="col" className="py-1 text-left font-medium">Used</th>
            </tr>
          </thead>
          <tbody>
            {bars.map((b) => (
              <tr key={b.key} className="border-t border-line">
                <td className="num py-1">{b.id}</td>
                <td className="py-1">{b.system}</td>
                <td className="py-1">{b.signal || "—"}</td>
                <td className="num py-1 text-right">{b.cno > 0 ? `${Math.round(b.cno)} dB-Hz` : "—"}</td>
                <td className="py-1">{b.used ? "yes" : "no"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}
