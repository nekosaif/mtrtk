import { Legend } from "@/components/charts/Legend";
import { STATUS_MARK } from "@/lib/palette";

/** RTKLIB solution quality Q → mark colour: 1 fixed, 2 float, 3 SBAS, 4 DGPS, 5 single, 6 PPP. */
export const Q_COLOR: Record<number, string> = { 1: STATUS_MARK.good, 2: STATUS_MARK.warning, 3: STATUS_MARK.serious, 4: STATUS_MARK.serious, 5: STATUS_MARK.critical, 6: "var(--sys-galileo)" };
const LEGEND = [
  { label: "fixed", color: STATUS_MARK.good },
  { label: "float", color: STATUS_MARK.warning },
  { label: "dgps/sbas", color: STATUS_MARK.serious },
  { label: "single", color: STATUS_MARK.critical },
];
/** A day at 1 Hz is 86 400 epochs: drawn as at most this many cells, each its epochs' commonest Q. */
export const MAX_CELLS = 600;

/** `qs` binned into at most `max` cells, each the most frequent Q of its epochs (ties: the better Q). */
export function binQualities(qs: number[], max = MAX_CELLS): number[] {
  if (qs.length <= max) return qs;
  const out: number[] = [];
  for (let c = 0; c < max; c++) {
    const lo = Math.floor((c * qs.length) / max);
    const hi = Math.floor(((c + 1) * qs.length) / max);
    const counts = new Map<number, number>();
    for (let i = lo; i < hi; i++) counts.set(qs[i], (counts.get(qs[i]) ?? 0) + 1);
    let best = qs[lo];
    let n = 0;
    for (const [q, k] of counts) if (k > n || (k === n && q < best)) [best, n] = [q, k];
    out.push(best);
  }
  return out;
}

/** One cell per epoch (binned past `MAX_CELLS`) coloured by RTKLIB solution quality Q. */
export function QualityStrip({ qs }: { qs: number[] }) {
  if (qs.length === 0) return <p className="text-ink-2">No epochs.</p>;
  const fixed = qs.filter((q) => q === 1).length;
  const pct = Math.round((fixed / qs.length) * 100);
  const cells = binQualities(qs);
  return (
    <div className="flex flex-col gap-2">
      <svg role="img" aria-label={`Solution quality: ${pct}% fixed`} viewBox={`0 0 ${cells.length} 10`} preserveAspectRatio="none" className="h-6 w-full rounded-sm">
        {cells.map((q, i) => (
          <rect key={i} x={i} y={0} width={1.02} height={10} fill={Q_COLOR[q] ?? "var(--ink-3)"} />
        ))}
      </svg>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Legend items={LEGEND} label="Solution quality" />
        <span className="num text-[12px] leading-4 text-ink-2">
          {pct}% fixed · {qs.length} epochs
        </span>
      </div>
    </div>
  );
}
