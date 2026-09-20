/**
 * The event log: what the daemon decided the operator should know, newest first.
 *
 * Two sources, one list. `GET /api/events` is the page (newest 200, optionally one level), and
 * the live store holds whatever has arrived on `events.new` since the tab opened — the same
 * rows, but seconds earlier. They are merged by id so a live entry that the next refetch also
 * returns is shown once; an entry the daemon published before its row id was known is still
 * shown, and is the only kind that cannot be acknowledged.
 */
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { PageHeader } from "@/app/PageHeader";
import { EmptyState } from "@/components/EmptyState";
import { Panel } from "@/components/Panel";
import { StatusBadge } from "@/components/StatusBadge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ackEvent, describeError } from "@/lib/api";
import { fmtLocal, fmtUtcDate, relTime } from "@/lib/format";
import { useLive } from "@/lib/live";
import { useEvents } from "@/lib/queries";
import { levelForEvent } from "@/lib/status";
import type { EventItem, Level } from "@/lib/types";
import { cn } from "@/lib/utils";

const FILTERS: { value: Level | undefined; label: string }[] = [
  { value: undefined, label: "All" },
  { value: "info", label: "Info" },
  { value: "warning", label: "Warning" },
  { value: "error", label: "Error" },
];

/** "3 min ago" only has to be right to the minute; a slower tick is a quieter page. */
const TICK_MS = 10_000;

function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(id);
  }, []);
  return now;
}

/** Live entries the fetched page does not already carry, then the page itself. */
export function mergeEvents(live: EventItem[], fetched: EventItem[], level: Level | undefined): EventItem[] {
  const seen = new Set(fetched.map((e) => e.id).filter((id): id is number => id != null));
  const extra = live.filter((e) => (!level || e.level === level) && (e.id == null || !seen.has(e.id)));
  return [...extra, ...fetched];
}

export default function Events() {
  const [level, setLevel] = useState<Level | undefined>(undefined);
  const query = useEvents(level);
  const live = useLive((s) => s.events);
  const qc = useQueryClient();
  const now = useNow();

  const ack = useMutation({
    mutationFn: (id: number) => ackEvent(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["events"] }),
  });

  const fetched = Array.isArray(query.data) ? query.data : [];
  const rows = useMemo(() => mergeEvents(live, fetched, level), [live, fetched, level]);
  const unacked = rows.filter((e) => !e.acked).length;

  return (
    <>
      <PageHeader title="Events">
        <div className="flex flex-wrap gap-1" role="group" aria-label="Filter by level">
          {FILTERS.map((f) => {
            const active = level === f.value;
            return (
              <button
                key={f.label}
                type="button"
                aria-pressed={active}
                onClick={() => setLevel(f.value)}
                className={cn(
                  "rounded-full border px-3 py-1 text-[14px]",
                  active ? "border-brass bg-panel-2 text-ink" : "border-line text-ink-2 hover:bg-panel-2 hover:text-ink",
                )}
              >
                {f.label}
              </button>
            );
          })}
        </div>
      </PageHeader>

      {ack.isError ? (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription className="text-[14px] leading-5">{describeError(ack.error)}</AlertDescription>
        </Alert>
      ) : null}

      <Panel
        bodyClassName="p-0"
        title={level ? `${FILTERS.find((f) => f.value === level)!.label} events` : "All events"}
        actions={<span className="num text-ink-2">{unacked} unacknowledged</span>}
      >
        {rows.length === 0 ? (
          <div className="p-4">
            {query.isLoading ? (
              <p className="text-ink-2">Reading the log…</p>
            ) : (
              <EmptyState
                title={level ? `No ${level} events` : "Nothing has been logged yet"}
                body={
                  level
                    ? "The daemon has not raised anything at this level. Try another level, or All."
                    : "The daemon writes an event when something changes that you would want to know about: a lost fix, RF interference, a survey-in finishing, a disk filling up."
                }
              />
            )}
          </div>
        ) : (
          <ul aria-label="Events">
            {rows.map((e, i) => (
              <EventRow key={e.id ?? `live-${i}-${e.ts_utc}-${e.kind}`} event={e} now={now} onAck={(id) => ack.mutate(id)} busy={ack.isPending} />
            ))}
          </ul>
        )}
      </Panel>
    </>
  );
}

function EventRow({ event, now, onAck, busy }: { event: EventItem; now: number; onAck: (id: number) => void; busy: boolean }) {
  const meta = Object.keys(event.meta ?? {});
  return (
    // On a phone the badge and the acknowledge button share the first line and the message takes
    // the second; `grow basis-[240px]` is what makes the row wrap rather than squeeze the message
    // into the strip left between them.
    <li className={cn("flex flex-wrap items-start gap-x-3 gap-y-2 border-b border-line px-4 py-3 last:border-0", event.acked && "opacity-60")}>
      <StatusBadge level={levelForEvent(event.level)} label={event.level} className="min-w-[104px]" />
      <div className="min-w-0 grow basis-[240px] max-sm:order-last max-sm:basis-full">
        <p data-testid="event-message">{event.message}</p>
        <p className="text-[12px] leading-4 text-ink-2">
          <span className="num">{event.kind}</span>
          {" · "}
          <span className="num" title={`Local time ${fmtLocal(event.ts_utc)}`}>
            {fmtUtcDate(event.ts_utc)}
          </span>
          {" · "}
          <span className="num">{relTime(event.ts_utc, now)}</span>
        </p>
        {meta.length ? (
          <details className="mt-1 text-[12px] leading-4 text-ink-2">
            <summary className="cursor-pointer">details</summary>
            <pre className="num mt-1 overflow-x-auto whitespace-pre-wrap">{JSON.stringify(event.meta, null, 2)}</pre>
          </details>
        ) : null}
      </div>
      <div className="max-sm:ml-auto">
        {event.acked ? (
          <span className="text-[12px] leading-4 text-ink-3">acknowledged</span>
        ) : (
          <Button
            size="sm"
            variant="outline"
            disabled={event.id == null || busy}
            title={event.id == null ? "This one arrived on the socket before the daemon had written it down" : undefined}
            onClick={() => event.id != null && onAck(event.id)}
          >
            Acknowledge
          </Button>
        )}
      </div>
    </li>
  );
}
