import { STATUS, type StatusLevel } from "@/lib/palette";
import { cn } from "@/lib/utils";

/**
 * A horizontal bar for a bounded reading: the jamming indicator (0–255), the AGC count
 * (0–8191). The value is printed beside the label in tabular numerals; the fill is neutral
 * ink unless a `level` is given, and a level should only be given where the word that goes
 * with the colour is visible next to it (a badge in the same panel). The width eases only
 * under `motion-safe`. `aria-valuenow` is kept inside 0–max; `valueText` (aria-valuetext) is
 * what a screen reader says instead of the bare number, when that number alone would mislead.
 */
export function Gauge({
  label,
  value,
  max,
  level,
  format = (v: number) => String(v),
  valueText,
  className,
}: {
  label: string;
  value: number;
  max: number;
  level?: StatusLevel;
  format?: (v: number) => string;
  valueText?: string;
  className?: string;
}) {
  const pct = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
  const now = Math.max(0, Math.min(max, value));
  return (
    <div className={cn("flex flex-col gap-1", className)} role="meter" aria-valuemin={0} aria-valuemax={max} aria-valuenow={now} aria-valuetext={valueText} aria-label={label}>
      <div className="flex items-baseline justify-between gap-3 text-[12px] leading-4 text-ink-2">
        <span>{label}</span>
        <span className="num text-[14px] leading-5 text-ink">{format(value)}</span>
      </div>
      <div className="h-1.5 w-full rounded-full bg-panel-2">
        <div
          data-gauge-fill
          className="h-full rounded-full motion-safe:transition-[width] motion-safe:duration-400 motion-safe:ease-out"
          style={{ width: `${(pct * 100).toFixed(1)}%`, background: level ? STATUS[level] : "var(--ink-2)" }}
        />
      </div>
    </div>
  );
}
