import type { HourSlot } from "@/lib/types";
import { fmtBytes, parseUtc } from "@/lib/format";
import { cn } from "@/lib/utils";

export type SlotState = "complete" | "partial" | "missing";

export function slotState(h: HourSlot): SlotState {
  return !h.available ? "missing" : h.complete ? "complete" : "partial";
}

/** "2026-09-18 10:00" for a slot's hour. */
function hourLabel(iso: string): string {
  const d = parseUtc(iso);
  return d ? d.toISOString().slice(0, 16).replace("T", " ") : iso;
}

/**
 * One cell per hour of raw logging: brass for a complete hour, muted ink for a partial one (being
 * written, or recovered after a crash), an outlined empty cell for a missing one. The word behind
 * each colour is in the cell's name and title and in the caption under the strip, so colour is
 * never the only carrier. Clicking a cell hands the slot to `onSelect` (the Logs page fills its
 * window form from it); `selected` marks the hours inside a `[from, to)` ISO range with a thin bar
 * under the strip — never on the cells themselves, whose colour means availability.
 */
export function AvailabilityStrip({ slots, selected, onSelect, className }: { slots: HourSlot[]; selected?: [string, string] | null; onSelect?: (hour: HourSlot) => void; className?: string }) {
  if (slots.length === 0) return <p className="text-ink-2">No hours in range.</p>;
  const sel = selected ? [parseUtc(selected[0])?.getTime() ?? NaN, parseUtc(selected[1])?.getTime() ?? NaN] : null;
  const times = slots.map((h) => parseUtc(h.hour_utc)?.getTime() ?? NaN);
  const inSel = (i: number) => sel != null && times[i] >= sel[0] && times[i] < sel[1];
  const first = times.findIndex((_, i) => inSel(i));
  let last = -1;
  for (let i = times.length - 1; i >= 0; i--) if (inSel(i)) { last = i; break; }
  const columns = `repeat(${slots.length}, minmax(0, 1fr))`;
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <div role="group" aria-label="Hourly raw log availability" className="grid gap-px" style={{ gridTemplateColumns: columns }}>
        {slots.map((h, i) => {
          const state = slotState(h);
          const name = `${hourLabel(h.hour_utc).slice(11)} UTC · ${state}`;
          return (
            <button
              key={h.hour_utc}
              type="button"
              data-state={state}
              onClick={() => onSelect?.(h)}
              title={`${hourLabel(h.hour_utc)} UTC · ${state}${h.available ? ` · ${fmtBytes(h.bytes)}` : ""}`}
              aria-label={name}
              aria-pressed={inSel(i)}
              className={cn(
                "h-5 min-w-0 rounded-[2px] outline-offset-1",
                state === "complete" && "bg-brass",
                state === "partial" && "bg-ink-3",
                state === "missing" && "border border-line bg-transparent",
              )}
            />
          );
        })}
      </div>
      {/* the selected window, as a bar under the hours it covers */}
      <div aria-hidden className="grid h-0.5 gap-px" style={{ gridTemplateColumns: columns }}>
        {first !== -1 ? <div data-selection style={{ gridColumn: `${first + 1} / ${last + 2}` }} className="rounded-full bg-ink-2" /> : null}
      </div>
      <div className="num flex justify-between text-[12px] leading-4 text-ink-2">
        <span>{hourLabel(slots[0].hour_utc)} UTC</span>
        <span>{hourLabel(slots[slots.length - 1].hour_utc)} UTC</span>
      </div>
    </div>
  );
}
