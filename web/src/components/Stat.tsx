import { STATUS, type StatusLevel } from "@/lib/palette";

/**
 * One row of a stat list: label left, tabular value right, a hairline under each row. `level`
 * colours the value (status colours only; the label is the word that goes with the colour);
 * `hint` is a muted second line under the value.
 */
export function Stat({ label, value, hint, level }: { label: string; value: string; hint?: string; level?: StatusLevel }) {
  return (
    <div className="border-b border-line py-1.5 last:border-0">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-ink-2">{label}</span>
        <span className="num text-right" data-stat-value style={level ? { color: STATUS[level] } : undefined}>
          {value}
        </span>
      </div>
      {hint ? <div className="num text-right text-[12px] leading-4 text-ink-3">{hint}</div> : null}
    </div>
  );
}
