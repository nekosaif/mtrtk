import { useEffect, useState } from "react";
import { AlertOctagon, X } from "lucide-react";
import { useLive } from "@/lib/live";
import { useNtripClients } from "@/lib/queries";
import { fixLevel } from "@/lib/status";
import { fmtAcc, fmtRate } from "@/lib/format";
import { StatusBadge } from "@/components/StatusBadge";
import { cn } from "@/lib/utils";

function browserClock(now: number): string {
  return new Date(now).toISOString().slice(11, 19);
}

/**
 * Persistent status strip above every page: six readings (clock · fix · sats · hAcc · RTCM ·
 * rovers) and the link state. Readings grey out when no epoch has arrived for 5 s or the socket
 * is down; the fix badge says why. A `receiver.error` shows here until dismissed.
 *
 * Numbers are `.num` (tabular figures) so they do not jitter. The strip scrolls itself on a
 * phone rather than widening the page; the readings never wrap.
 */
export function Tape() {
  const status = useLive((s) => s.status);
  const stale = useLive((s) => s.stale);
  const state = useLive((s) => s.state);
  const receiverConnected = useLive((s) => s.receiverConnected);
  const receiverError = useLive((s) => s.receiverError);
  const liveRovers = useLive((s) => s.ntripClients.length);
  // The socket lists the caster's clients on every change; until it has, the query is the only
  // source — the same fallback the Corrections page uses, so the two cannot disagree on one screen.
  const roversQuery = useNtripClients();
  const rovers = liveRovers || (Array.isArray(roversQuery.data) ? roversQuery.data.length : 0);
  const clearReceiverError = useLive((s) => s.clearReceiverError);

  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const linkDown = status !== "open";
  const dim = stale || linkDown;
  const receiverTime = state?.time.utc && !dim ? state.time.utc.slice(11, 19) : null;
  const clock = receiverTime ?? browserClock(now);
  const fix = fixLevel(state?.fix, receiverConnected, dim);
  const ink = dim ? "text-ink-3" : "text-ink";
  const unit = dim ? "text-ink-3" : "text-ink-2";

  return (
    <div
      className="flex min-w-0 items-center gap-6 overflow-x-auto border-b border-line bg-panel px-6 py-2 text-[14px] whitespace-nowrap max-sm:gap-4 max-sm:px-4"
      role="status"
      aria-live="off"
    >
      <span className={cn("num", ink)} data-testid="reading" title={receiverTime ? "Receiver time" : "Browser clock"}>
        {clock} <span className={unit}>UTC</span>
      </span>
      <StatusBadge level={fix.level} label={fix.label} />
      {state ? (
        <>
          <span className={cn("num", ink)} data-testid="reading">
            <span className={unit}>sats </span>
            {state.sat_summary.used}
            <span className={unit}>/{state.sat_summary.tracked}</span>
          </span>
          <span className={cn("num", ink)} data-testid="reading">
            <span className={unit}>hAcc </span>
            {fmtAcc(state.accuracy.h_acc_m)}
          </span>
          <span className={cn("num", ink)} data-testid="reading">
            <span className={unit}>RTCM </span>
            {fmtRate(state.rtcm_out.bytes_per_s)}
          </span>
          <span className={cn("num", ink)} data-testid="reading">
            {rovers}
            <span className={unit}> rover{rovers === 1 ? "" : "s"}</span>
          </span>
        </>
      ) : null}
      {receiverError ? (
        <span role="alert" className="flex min-w-0 items-center gap-1.5 text-ink" title={receiverError}>
          <AlertOctagon className="size-3.5 shrink-0" style={{ color: "var(--status-critical)" }} aria-hidden />
          <span className="max-w-[48ch] truncate">{receiverError}</span>
          <button
            type="button"
            onClick={clearReceiverError}
            className="inline-flex size-6 shrink-0 items-center justify-center rounded text-ink-2 hover:bg-panel-2 hover:text-ink"
            aria-label="Dismiss receiver error"
          >
            <X className="size-3.5" aria-hidden />
          </button>
        </span>
      ) : null}
      <span className="ml-auto flex shrink-0 items-center gap-2 text-ink-2">
        <span
          className={cn("inline-block size-2 rounded-full", status === "open" ? "bg-status-good" : status === "reconnecting" ? "bg-status-serious" : "bg-status-warning")}
          aria-hidden
        />
        {status === "open" ? (stale ? "live · waiting for epochs" : "live") : status === "reconnecting" ? "reconnecting" : "connecting"}
      </span>
    </div>
  );
}
