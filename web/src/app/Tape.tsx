import { useEffect, useState } from "react";
import { useLive } from "@/lib/live";
import { cn } from "@/lib/utils";

function utcClock(): string {
  const d = new Date();
  return d.toISOString().slice(11, 19) + " UTC";
}

/**
 * Persistent status strip above every page. Task 2 replaces the placeholder
 * reading with the six live readings (fix · sats · hAcc · RTCM · link).
 */
export function Tape() {
  const status = useLive((s) => s.status);
  const [clock, setClock] = useState(utcClock());
  useEffect(() => {
    const id = setInterval(() => setClock(utcClock()), 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <div
      className="flex min-w-0 items-center gap-6 overflow-x-auto border-b border-line bg-panel px-6 py-2 text-[14px] whitespace-nowrap max-sm:gap-4 max-sm:px-4"
      role="status"
      aria-live="off"
    >
      <span className="num text-ink">{clock}</span>
      <span className="text-ink-3">no receiver data yet</span>
      <span className={cn("ml-auto flex shrink-0 items-center gap-2 text-ink-2")}>
        <span
          className={cn("inline-block size-2 rounded-full", status === "open" ? "bg-status-good" : "bg-status-warning")}
          aria-hidden
        />
        {status === "open" ? "live" : status === "reconnecting" ? "reconnecting" : "connecting"}
      </span>
    </div>
  );
}
