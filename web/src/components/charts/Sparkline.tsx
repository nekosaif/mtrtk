import { useId, useState } from "react";
import { cn } from "@/lib/utils";

/**
 * An inline trend readout: one polyline over the last N values, no axes, the latest (or hovered)
 * value beside the label, and a `<title>` that says min, max and last for anyone who cannot see
 * the line. Gaps (`null`) are skipped. Hovering shows a crosshair and that point's value.
 */
export function Sparkline({
  label,
  values,
  format,
  height = 40,
  width = 240,
  color = "var(--ink-2)",
  className,
}: {
  label: string;
  values: readonly (number | null | undefined)[];
  format: (v: number) => string;
  height?: number;
  width?: number;
  color?: string;
  className?: string;
}) {
  const id = useId();
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
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
  const x = (i: number) => (n > 1 ? (i / (n - 1)) * width : 0);
  const y = (v: number) => (max === min ? height / 2 : height - 3 - ((v - min) / (max - min)) * (height - 6));
  const points = pts.map((p) => `${x(p.i).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const hovered = hoverIdx != null ? pts[hoverIdx] : null;
  const title = `${label}: min ${format(min)}, max ${format(max)}, last ${format(last)}`;

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
        className="block"
        onMouseMove={(e) => {
          const rect = e.currentTarget.getBoundingClientRect();
          if (!rect.width) return;
          const fx = ((e.clientX - rect.left) / rect.width) * width;
          let best = 0;
          for (let k = 1; k < pts.length; k++) if (Math.abs(x(pts[k].i) - fx) < Math.abs(x(pts[best].i) - fx)) best = k;
          setHoverIdx(best);
        }}
        onMouseLeave={() => setHoverIdx(null)}
      >
        <title id={id}>{title}</title>
        <polyline points={points} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
        {hovered ? <line data-crosshair x1={x(hovered.i)} x2={x(hovered.i)} y1={0} y2={height} stroke="var(--ink-3)" strokeDasharray="2 2" vectorEffect="non-scaling-stroke" /> : null}
      </svg>
    </div>
  );
}
