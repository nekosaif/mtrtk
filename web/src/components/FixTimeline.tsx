import { Legend } from "@/components/charts/Legend";
import { describeError } from "@/lib/api";
import { STATUS } from "@/lib/palette";
import { useHistory } from "@/lib/queries";

type FixState = "fixed" | "float" | "3d" | "2d" | "none";

const STATES: Record<FixState, { label: string; color: string }> = {
  fixed: { label: "Fixed", color: STATUS.good },
  float: { label: "Float", color: STATUS.warning },
  "3d": { label: "3D", color: "var(--ink-3)" },
  "2d": { label: "2D", color: STATUS.serious },
  none: { label: "No fix", color: STATUS.critical },
};
const LEGEND = (Object.keys(STATES) as FixState[]).map((k) => STATES[k]);

/** The solution one second was in: the carrier solution first, then the fix type behind it. */
export function fixState(carr: number | null, fix: number | null): FixState {
  if (carr === 2) return "fixed";
  if (carr === 1) return "float";
  if (fix != null && fix >= 3) return "3d";
  if (fix === 2) return "2d";
  return "none";
}

/**
 * One cell per second of `/api/history?res=1s` between `from` and `to`, coloured by fix state.
 * Cells sit at their own second within the window, so a gap in the samples (the daemon was down,
 * the receiver silent) is an empty stretch rather than a strip squeezed shut. Each cell's
 * tooltip gives its UTC second and state; the label carries the share of fixed seconds.
 */
export function FixTimeline({ from, to }: { from: string; to: string }) {
  const q = useHistory(["carr_soln", "fix_type"], from, to, "1s");
  if (q.isError) return <p className="text-status-critical-text">Fix history unavailable: {describeError(q.error)}</p>;
  if (!q.data) return <p className="text-ink-2">Loading…</p>;
  const { columns, rows } = q.data;
  if (rows.length === 0) return <p className="text-ink-2">No samples in this range.</p>;
  const ic = columns.indexOf("carr_soln");
  const iff = columns.indexOf("fix_type");
  const t0 = Math.floor(Date.parse(from) / 1000);
  const span = Math.max(1, Math.ceil((Date.parse(to) - Date.parse(from)) / 1000));
  const cells = rows.map((r) => ({ ts: r[0] as number, state: fixState(r[ic] ?? null, r[iff] ?? null) }));
  const fixed = cells.filter((c) => c.state === "fixed").length;
  const pct = Math.round((fixed / cells.length) * 100);
  return (
    <div className="flex flex-col gap-2">
      <svg role="img" aria-label={`Fix state over time: ${pct}% RTK fixed`} viewBox={`0 0 ${span} 10`} preserveAspectRatio="none" className="h-6 w-full rounded-sm bg-panel-2">
        {cells.map((c) => (
          <rect key={c.ts} x={c.ts - t0} y={0} width={1} height={10} fill={STATES[c.state].color}>
            <title>{`${new Date(c.ts * 1000).toISOString().slice(11, 19)} UTC · ${STATES[c.state].label}`}</title>
          </rect>
        ))}
      </svg>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Legend items={LEGEND} label="Fix states" />
        <span className="num text-[12px] text-ink-2">
          {pct}% fixed over {cells.length} s
        </span>
      </div>
    </div>
  );
}
