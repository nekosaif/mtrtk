import { useId } from "react";
import { CopyButton } from "@/components/CopyButton";
import { Readout } from "@/components/Readout";
import { COORD_MODES, type CoordMode, fmtAcc, fmtHeights, fmtPosition } from "@/lib/format";
import { useCoordMode } from "@/lib/prefs";
import type { Accuracy, Position } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * The hero: the base position in the operator's coordinate mode (persisted per browser as
 * `mtrtk:coordMode`, shared by every page), set in the display serif, with both heights and the
 * horizontal/vertical accuracies under it. The copy button puts the two lines on the clipboard.
 */
export function CoordinateReadout({ position, accuracy, className }: { position: Position | null; accuracy: Accuracy | null; className?: string }) {
  const [mode, setMode] = useCoordMode();
  const selectId = useId();
  const [line1, line2] = fmtPosition(position, mode);
  const [ellipsoidal, msl] = fmtHeights(position);
  // UTM and ECEF lines are long; step the display size down so they stay on one line each.
  const long = Math.max(line1.length, line2.length) > 18;
  return (
    <div data-testid="coordinate-readout" className={cn("flex h-full flex-col gap-4", className)}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label htmlFor={selectId} className="text-[12px] leading-4 text-ink-2">
          Coordinate format
        </label>
        <div className="flex items-center gap-2">
          <select
            id={selectId}
            value={mode}
            onChange={(e) => setMode(e.target.value as CoordMode)}
            className="h-8 rounded-md border border-line bg-panel-2 px-2 text-[12px] text-ink"
          >
            {COORD_MODES.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>
          <CopyButton text={`${line1}\n${line2}`} />
        </div>
      </div>
      <div className={cn("display num min-w-0 whitespace-pre-wrap break-words", long ? "text-[28px] leading-[34px]" : "text-[40px] leading-[44px] max-sm:text-[28px] max-sm:leading-[34px]")}>
        <div>{line1}</div>
        <div>{line2}</div>
      </div>
      <div className="grid grid-cols-2 gap-x-6 gap-y-3">
        <Readout label="Height (ellipsoidal)" value={ellipsoidal} size="sm" />
        <Readout label="Height (MSL)" value={msl} size="sm" />
        <Readout label="Horizontal accuracy" value={fmtAcc(accuracy?.h_acc_m)} size="sm" />
        <Readout label="Vertical accuracy" value={fmtAcc(accuracy?.v_acc_m)} size="sm" />
      </div>
    </div>
  );
}
