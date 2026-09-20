/**
 * Swatch + label (+ optional tabular value) per series. Hidden for a single series: one colour
 * needs no key. Text is never set in the series colour.
 */
export function Legend({ items, label = "Series" }: { items: { label: string; color: string; value?: string }[]; label?: string }) {
  if (items.length < 2) return null;
  return (
    <ul aria-label={label} className="flex flex-wrap gap-x-4 gap-y-1 text-[12px] leading-4 text-ink-2">
      {items.map((it) => (
        <li key={it.label} className="flex items-center gap-1.5">
          <span className="inline-block size-2.5 rounded-sm" style={{ background: it.color }} aria-hidden />
          <span className="text-ink">{it.label}</span>
          {it.value ? <span className="num">{it.value}</span> : null}
        </li>
      ))}
    </ul>
  );
}
