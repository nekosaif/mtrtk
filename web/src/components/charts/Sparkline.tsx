import { type PointerEvent, useId, useState } from "react";
import { cn } from "@/lib/utils";
import { SCRUB, clearsOnLeave } from "./pointer";

/** The typical spacing of `ts`, for telling a silence from a slow change. */
function medianStep(ts: number[]): number {
  const d: number[] = [];
  for (let i = 1; i < ts.length; i++) d.push(ts[i] - ts[i - 1]);
  if (d.length === 0) return 0;
  d.sort((a, b) => a - b);
  return d[Math.floor(d.length / 2)];
}

/**
 * An inline trend readout: a line over the last N values, no axes, the latest (or pointed-at)
 * value beside the label, and a `<title>` that says min, max and last for anyone who cannot see
 * the line. Gaps (`null`) are skipped. With `times` (ms, one per value) the points sit at their
 * own moment and the line breaks where two samples are more than three typical steps apart, so
 * a reconnect reads as a gap rather than a smooth slope; without, they are evenly spaced. The
 * pointer (mouse, pen or a finger dragging sideways) shows a crosshair and that point's value.
 */
export function Sparkline({
  label,
  values,
  times,
  format,
  height = 40,
  width = 240,
  color = "var(--ink-2)",
  className,
}: {
  label: string;
  values: readonly (number | null | undefined)[];
  /**
   * Sample times in ms, index-aligned with `values`. Only for a ring pushed by the data itself
   * (epochs, MON-RF): a ring sampled on a timer has gaps where the browser throttled the timer.
   */
  times?: readonly number[];
  format: (v: number) => string;
  height?: number;
  width?: number;
  color?: string;
  className?: string;
}) {
  const id = useId();
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  // Timestamps that do not span any time (one burst, a test's frozen clock) say nothing about spacing.
  const timed = times != null && times.length === values.length && times[times.length - 1] > times[0];
  const pts: { i: number; v: number }[] = [];
  values.forEach((v, i) => {
    if (typeof v === "number" && Number.isFinite(v)) pts.push({ i, v });
  });
  if (pts.length < 2) {
    return (
      <div className={cn("flex flex-col gap-1 text-[12px] leading-4", className)}>
        <span className="text-ink-2">{label}</span>
        <span className="text-ink-3">Collecting…</span>
      </div>
    );
  }
  const n = values.length;
  let min = Infinity;
  let max = -Infinity;
  for (const p of pts) {
    if (p.v < min) min = p.v;
    if (p.v > max) max = p.v;
  }
  const last = pts[pts.length - 1].v;
  const t0 = timed ? times[0] : 0;
  const tSpan = timed ? times[n - 1] - t0 : 0;
  const x = (i: number) => (timed ? ((times[i] - t0) / tSpan) * width : n > 1 ? (i / (n - 1)) * width : 0);
  const y = (v: number) => (max === min ? height / 2 : height - 3 - ((v - min) / (max - min)) * (height - 6));
  // runs of points that are not separated by a silence
  const step = timed ? medianStep(pts.map((p) => times[p.i])) : 0;
  const gap = step > 0 ? 3 * step : Infinity;
  const runs: { i: number; v: number }[][] = [];
  for (const p of pts) {
    const run = runs[runs.length - 1];
    if (run && !(timed && times[p.i] - times[run[run.length - 1].i] > gap)) run.push(p);
    else runs.push([p]);
  }
  const hovered = hoverIdx != null ? pts[hoverIdx] : null;
  const title = `${label}: min ${format(min)}, max ${format(max)}, last ${format(last)}`;
  const read = (e: PointerEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    if (!rect.width) return;
    const fx = ((e.clientX - rect.left) / rect.width) * width;
    let best = 0;
    for (let k = 1; k < pts.length; k++) if (Math.abs(x(pts[k].i) - fx) < Math.abs(x(pts[best].i) - fx)) best = k;
    setHoverIdx(best);
  };

  return (
    <div className={cn("flex min-w-0 flex-col gap-1", className)}>
      <div className="flex items-baseline justify-between gap-3 text-[12px] leading-4">
        <span className="text-ink-2">{label}</span>
        <span className="num text-[14px] leading-5 text-ink">{format(hovered ? hovered.v : last)}</span>
      </div>
      <svg
        role="img"
        aria-labelledby={id}
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        height={height}
        preserveAspectRatio="none"
        className={cn("block", SCRUB)}
        onPointerDown={read}
        onPointerMove={read}
        onPointerLeave={(e) => clearsOnLeave(e) && setHoverIdx(null)}
      >
        <title id={id}>{title}</title>
        {runs.map((run) =>
          run.length > 1 ? (
            <polyline
              key={run[0].i}
              points={run.map((p) => `${x(p.i).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ")}
              fill="none"
              stroke={color}
              strokeWidth={1.5}
              strokeLinejoin="round"
              strokeLinecap="round"
              vectorEffect="non-scaling-stroke"
            />
          ) : (
            <circle key={run[0].i} cx={x(run[0].i)} cy={y(run[0].v)} r={1.5} fill={color} />
          ),
        )}
        {hovered ? <line data-crosshair x1={x(hovered.i)} x2={x(hovered.i)} y1={0} y2={height} stroke="var(--ink-3)" strokeDasharray="2 2" vectorEffect="non-scaling-stroke" /> : null}
      </svg>
    </div>
  );
}
