import { useLayoutEffect, useRef, useState } from "react";

export interface TsPoint {
  /** Seconds since the epoch (the API's `ts`). */
  t: number;
  v: number | null;
}

const LEFT = 56;
const RIGHT = 12;
const TOP = 10;
const BOTTOM = 22;
const MIN_WIDTH = 240;
const DEFAULT_WIDTH = 720;
/** Rows in the table alternative; a day of 1 s samples is sampled down to this. */
const TABLE_ROWS = 60;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** Candidate x steps in seconds: minutes, hours, days, weeks, a month. */
const TIME_STEPS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800, 1209600, 2592000];

/**
 * Round y ticks covering `[min, max]`: the 1-2-5 step whose interval count is closest to four
 * (never more than six), the first tick at or under `min`, the last at or over `max`.
 */
export function niceTicks(min: number, max: number, target = 4, most = 6): number[] {
  if (!(Number.isFinite(min) && Number.isFinite(max))) return [];
  if (max - min <= 0) {
    const step = min === 0 ? 1 : 10 ** Math.floor(Math.log10(Math.abs(min))) || 1;
    return [round(min), round(min + step)];
  }
  const range = max - min;
  const base = 10 ** Math.floor(Math.log10(range));
  const candidates: number[] = [];
  for (const mag of [base / 10, base, base * 10]) for (const m of [1, 2, 5]) candidates.push(m * mag);
  let best: { step: number; n: number } | null = null;
  for (const step of candidates) {
    const n = Math.ceil(max / step - 1e-9) - Math.floor(min / step + 1e-9);
    if (n < 1 || n > most) continue;
    if (!best || Math.abs(n - target) < Math.abs(best.n - target)) best = { step, n };
  }
  const step = best?.step ?? candidates[candidates.length - 1];
  const lo = Math.floor(min / step + 1e-9) * step;
  const hi = Math.ceil(max / step - 1e-9) * step;
  const out: number[] = [];
  for (let v = lo; v <= hi + step / 2; v += step) out.push(round(v));
  return out;
}

function round(v: number): number {
  return Number(v.toFixed(10));
}

/** UTC-aligned x ticks between `t0` and `t1` (seconds): the finest step that keeps at most `most` ticks. */
export function timeTicks(t0: number, t1: number, most = 6): number[] {
  const span = Math.max(1, t1 - t0);
  const step = TIME_STEPS.find((s) => span / s <= most) ?? TIME_STEPS[TIME_STEPS.length - 1];
  const out: number[] = [];
  for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) out.push(t);
  return out;
}

/** "22:14" for most ticks; the date ("15 Nov") on a tick that falls on midnight UTC. */
export function fmtTick(t: number): string {
  const d = new Date(t * 1000);
  if (t % 86400 === 0) return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
  return d.toISOString().slice(11, 16);
}

const fmtTime = (t: number) => new Date(t * 1000).toISOString().slice(11, 19);
const fmtDateTime = (t: number) => new Date(t * 1000).toISOString().slice(0, 19).replace("T", " ");

/** The typical spacing between consecutive points, for telling a gap from a slope. */
function medianStep(points: TsPoint[]): number {
  const d: number[] = [];
  for (let i = 1; i < points.length; i++) d.push(points[i].t - points[i - 1].t);
  if (d.length === 0) return 1;
  d.sort((a, b) => a - b);
  return d[Math.floor(d.length / 2)] || 1;
}

/**
 * One metric over time: a thin line in one colour, a left y axis of round ticks, a bottom time
 * axis in UTC placed on real timestamps (HH:MM, the date at midnight), a crosshair with the
 * time and value under the pointer, and a `<details>` table of the samples. The line breaks
 * where `v` is null and where two samples are further apart than `gapS` (default: three times
 * the typical spacing), so downtime reads as a gap rather than a slope. `domain` pins the x axis
 * to the window that was asked for, so missing edges show as empty space. Drawn at the
 * container's pixel width (a phone fits), nothing animates, text is never in the series colour.
 */
export function TimeSeries({
  points,
  label,
  unit,
  format,
  height = 160,
  color = "var(--brass)",
  domain,
  gapS,
  className,
}: {
  points: TsPoint[];
  label: string;
  unit: string;
  format: (v: number) => string;
  height?: number;
  color?: string;
  /** `[from, to]` in seconds; defaults to the data extent. */
  domain?: [number, number];
  /** Two samples further apart than this many seconds are not joined. */
  gapS?: number;
  className?: string;
}) {
  const frameRef = useRef<HTMLDivElement>(null);
  const [frameWidth, setFrameWidth] = useState(0);
  const [hover, setHover] = useState<{ t: number; v: number } | null>(null);

  useLayoutEffect(() => {
    const el = frameRef.current;
    if (!el) return;
    const measure = () => setFrameWidth(el.clientWidth);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const valid = points.filter((p): p is { t: number; v: number } => p.v != null && Number.isFinite(p.v));
  if (valid.length < 2) {
    return (
      <p className={className ? `text-ink-2 ${className}` : "text-ink-2"}>
        {label}: no data in this range.
      </p>
    );
  }

  const width = Math.max(MIN_WIDTH, frameWidth || DEFAULT_WIDTH);
  const plotW = width - LEFT - RIGHT;
  const plotH = height - TOP - BOTTOM;
  const t0 = domain ? domain[0] : points[0].t;
  const t1 = domain ? domain[1] : points[points.length - 1].t;
  const span = Math.max(1e-9, t1 - t0);
  let min = Infinity;
  let max = -Infinity;
  for (const p of valid) {
    if (p.v < min) min = p.v;
    if (p.v > max) max = p.v;
  }
  const yTicks = niceTicks(min, max);
  const yLo = yTicks[0];
  const yHi = yTicks[yTicks.length - 1];
  // An integer format over a fractional step would print "25, 25, 26, 26": keep one tick per label.
  const yLabelled = yTicks.filter((v, i) => i === 0 || format(v) !== format(yTicks[i - 1]));
  const x = (t: number) => LEFT + ((t - t0) / span) * plotW;
  const y = (v: number) => TOP + plotH - ((v - yLo) / Math.max(1e-12, yHi - yLo)) * plotH;

  const gap = gapS ?? 3 * medianStep(points);
  let d = "";
  let pen = false;
  let prevT = NaN;
  for (const p of points) {
    if (p.v == null || !Number.isFinite(p.v)) {
      pen = false;
      continue;
    }
    if (pen && p.t - prevT > gap) pen = false;
    d += `${pen ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)} `;
    pen = true;
    prevT = p.t;
  }

  const xTicks = timeTicks(t0, t1, Math.max(2, Math.floor(plotW / 80)));
  const tableStep = Math.max(1, Math.ceil(valid.length / TABLE_ROWS));
  const readout = hover ? `${fmtTime(hover.t)} UTC · ${format(hover.v)}` : `${format(min)} – ${format(max)}`;
  const labelOnLeft = hover ? x(hover.t) > LEFT + plotW * 0.6 : false;

  return (
    <div className={className}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 text-[14px] leading-5">
        <span>
          {label}
          {unit ? <span className="ml-1 text-[12px] text-ink-2">{unit}</span> : null}
        </span>
        <span data-testid="timeseries-readout" className="num text-ink-2">
          {readout}
        </span>
      </div>
      <div ref={frameRef} className="mt-1 overflow-x-auto">
        <svg
          role="img"
          aria-label={`${label} over time`}
          data-domain={`${t0},${t1}`}
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          className="block"
          onMouseMove={(e) => {
            const rect = e.currentTarget.getBoundingClientRect();
            // jsdom (no layout) gives a zero rect: treat clientX as drawn pixels so the hover math still holds
            const scale = rect.width > 0 ? width / rect.width : 1;
            const px = (e.clientX - rect.left) * scale;
            let best = valid[0];
            for (const p of valid) if (Math.abs(x(p.t) - px) < Math.abs(x(best.t) - px)) best = p;
            setHover(best);
          }}
          onMouseLeave={() => setHover(null)}
        >
          <title>{`${label} over time`}</title>
          {yLabelled.map((v) => (
            <g key={v}>
              <line x1={LEFT} x2={width - RIGHT} y1={y(v)} y2={y(v)} stroke="var(--line)" />
              <text data-y-tick={v} x={LEFT - 8} y={y(v) + 4} textAnchor="end" fontSize={11} fill="var(--ink-3)" className="num">
                {format(v)}
              </text>
            </g>
          ))}
          {xTicks.map((t) => (
            <g key={t}>
              <line x1={x(t)} x2={x(t)} y1={TOP + plotH} y2={TOP + plotH + 4} stroke="var(--line)" />
              <text data-x-tick={t} x={x(t)} y={height - 6} textAnchor="middle" fontSize={11} fill="var(--ink-3)" className="num">
                {fmtTick(t)}
              </text>
            </g>
          ))}
          <path data-series d={d.trim()} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" />
          {hover ? (
            <g data-crosshair>
              <line x1={x(hover.t)} x2={x(hover.t)} y1={TOP} y2={TOP + plotH} stroke="var(--ink-3)" strokeDasharray="2 2" />
              <circle cx={x(hover.t)} cy={y(hover.v)} r={3.5} fill={color} stroke="var(--panel)" strokeWidth={2} />
              <text x={x(hover.t) + (labelOnLeft ? -8 : 8)} y={TOP + 10} textAnchor={labelOnLeft ? "end" : "start"} fontSize={11} fill="var(--ink-2)" className="num">
                {`${fmtTime(hover.t)} · ${format(hover.v)}`}
              </text>
            </g>
          ) : null}
        </svg>
      </div>
      <details className="mt-1 text-[12px] leading-4">
        <summary className="cursor-pointer text-ink-2 hover:text-ink">Show as table</summary>
        {tableStep > 1 ? <p className="mt-1 text-ink-2">Every {tableStep}th sample of {valid.length}.</p> : null}
        <table className="mt-1 w-full" aria-label={`${label} as a table`}>
          <thead>
            <tr className="text-ink-2">
              <th scope="col" className="py-1 text-left font-medium">Time (UTC)</th>
              <th scope="col" className="py-1 text-right font-medium">{unit ? `${label} (${unit})` : label}</th>
            </tr>
          </thead>
          <tbody>
            {valid
              .filter((_, i) => i % tableStep === 0)
              .map((p) => (
                <tr key={p.t} className="border-t border-line">
                  <td className="num py-1">{fmtDateTime(p.t)}</td>
                  <td className="num py-1 text-right">{format(p.v)}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}
