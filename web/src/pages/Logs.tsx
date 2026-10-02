import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router";
import { CircleDashed, Lock } from "lucide-react";
import { PageHeader } from "@/app/PageHeader";
import { Panel } from "@/components/Panel";
import { EmptyState } from "@/components/EmptyState";
import { StatusBadge } from "@/components/StatusBadge";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { EXPORT_JOBS_ANCHOR, EXPORT_MAX_DAYS, EXPORT_PANEL_ANCHOR, ExportPanel } from "@/components/ExportPanel";
import { JobsPanel } from "@/components/JobsPanel";
import { DataTable, type Column } from "@/components/DataTable";
import { AvailabilityStrip } from "@/components/charts/AvailabilityStrip";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { deleteLog, describeError, logFileUrl, logWindowUrl, probeDownload, setKeep } from "@/lib/api";
import { fmtBytes, fmtUtcDate, fromInput, parseUtc, toInput } from "@/lib/format";
import { useLive } from "@/lib/live";
import type { StatusLevel } from "@/lib/palette";
import { useAvailability, useLogs } from "@/lib/queries";
import type { LogFile, LogsResponse } from "@/lib/types";

const HOUR_MS = 3600_000;
/** The window export's cap, mirrored client-side so the button says so before the daemon has to. */
export const WINDOW_MAX_H = 48;
/** How many whole hours the strip shows, the current one included. */
const STRIP_HOURS = 48;
/** `?hours=` the export panel accepts: up to the export's own 7-day cap. */
const EXPORT_HOURS_MAX = EXPORT_MAX_DAYS * 24;

// ------------------------------------------------------------------------------------- helpers

/** The export panel's start from `?export=<preset>&hours=<n>`: a bad or missing `hours` is 24. */
export function exportParams(params: URLSearchParams): { preset?: string; hours: number } {
  const raw = Number(params.get("hours"));
  const hours = Number.isFinite(raw) && raw > 0 && raw <= EXPORT_HOURS_MAX ? raw : 24;
  return { preset: params.get("export") ?? undefined, hours };
}

/** Why a window cannot be asked for yet, or null when it can. */
export function windowProblem(from: string, to: string): string | null {
  const a = fromInput(from);
  const b = fromInput(to);
  if (a == null || b == null) return "Enter both times.";
  if (a >= b) return "From must be before to.";
  const hours = (b - a) / HOUR_MS;
  if (hours > WINDOW_MAX_H) return `At most ${WINDOW_MAX_H} hours per request; this window is ${hours % 1 === 0 ? hours : hours.toFixed(1)} hours.`;
  return null;
}

/** Disk state against retention's floor: nothing while above it, serious under it, critical under half of it. */
export function diskLevel(free: number | undefined, min: number | undefined): { level: StatusLevel; label: string } | null {
  if (typeof free !== "number" || typeof min !== "number" || min <= 0 || free >= min) return null;
  return free < min / 2 ? { level: "critical", label: "Disk low" } : { level: "serious", label: "Disk warning" };
}

// ------------------------------------------------------------------------------------ files

function StateCell({ f }: { f: LogFile }) {
  if (f.open) {
    return (
      <span className="inline-flex items-center gap-1">
        <CircleDashed className="size-3.5 text-status-warning-mark" aria-hidden />
        writing
      </span>
    );
  }
  if (f.complete) return <span>complete</span>;
  return <span className="text-ink-2">partial</span>;
}

/**
 * The delete step for one hour. Held (with the reason as a tooltip) for the hour being written
 * and for a keep-protected one — the daemon would answer 409 anyway. When the daemon reports no
 * open hour at all (a rover, a replay without `REPLAY_LOG=1`) it guesses that the newest file is
 * the one being written; the checkbox is that guess's `?force=1` override.
 */
function DeleteLog({ f, newestWithoutWriter, onDelete }: { f: LogFile; newestWithoutWriter: boolean; onDelete: (name: string, force: boolean) => Promise<unknown> }) {
  const [force, setForce] = useState(false);
  const reason = f.open ? "This hour is being written; it can be deleted once the writer has rotated out of it." : f.keep ? "Marked keep; clear the mark before deleting." : null;
  return (
    <span title={reason ?? undefined} className="inline-flex">
      <ConfirmDialog
        trigger={
          <Button type="button" size="sm" variant="ghost" disabled={reason != null}>
            Delete
          </Button>
        }
        title={`Delete ${f.name}?`}
        body="Raw data for this hour cannot be recovered."
        confirmLabel="Delete"
        destructive
        onConfirm={() => onDelete(f.name, force)}
      >
        {newestWithoutWriter ? (
          <label className="flex items-start gap-2 text-[14px] leading-5 text-ink-2">
            <input type="checkbox" className="mt-1 accent-brass" checked={force} onChange={(e) => setForce(e.target.checked)} />
            <span>This daemon reports no open hour, so it treats the newest hour as possibly still being written. Delete it anyway.</span>
          </label>
        ) : null}
      </ConfirmDialog>
    </span>
  );
}

function fileColumns(opts: { onKeep: (f: LogFile, keep: boolean) => void; keepBusy: string | null; newestWithoutWriter: string | null; onDelete: (name: string, force: boolean) => Promise<unknown> }): Column<LogFile>[] {
  return [
    { key: "name", header: "File", cell: (f) => <span className="num">{f.name}</span>, sortValue: (f) => f.name },
    { key: "hour", header: "Hour (UTC)", cell: (f) => <span className="num">{fmtUtcDate(f.hour_utc).slice(0, 16)}</span>, sortValue: (f) => f.hour_utc, firstDir: "desc" },
    { key: "bytes", header: "Size", align: "right", cell: (f) => fmtBytes(f.bytes), sortValue: (f) => f.bytes },
    { key: "rawx", header: "RAWX epochs", align: "right", cell: (f) => f.msg_counts["RXM-RAWX"] ?? 0, sortValue: (f) => f.msg_counts["RXM-RAWX"] ?? 0 },
    { key: "state", header: "State", cell: (f) => <StateCell f={f} />, sortValue: (f) => (f.open ? "writing" : f.complete ? "complete" : "partial") },
    {
      key: "keep",
      header: "Keep",
      cell: (f) => <Switch aria-label={`Keep ${f.name}`} checked={f.keep} disabled={opts.keepBusy === f.name} onCheckedChange={(v) => opts.onKeep(f, v)} />,
      sortValue: (f) => f.keep,
    },
    {
      key: "actions",
      header: "",
      cell: (f) => (
        <div className="flex justify-end gap-1">
          <Button size="sm" variant="outline" asChild>
            <a href={logFileUrl(f.name)} download>
              Download
            </a>
          </Button>
          <DeleteLog f={f} newestWithoutWriter={opts.newestWithoutWriter === f.name} onDelete={opts.onDelete} />
        </div>
      ),
    },
  ];
}

// -------------------------------------------------------------------------------------- page

function Summary({ data }: { data: LogsResponse }) {
  const disk = diskLevel(data.disk_free_gb, data.min_free_gb);
  return (
    <>
      <span className="num text-ink-2">
        {fmtBytes(data.total_bytes)} in {data.hours} {data.hours === 1 ? "hour" : "hours"} · {data.disk_free_gb.toFixed(1)} GB free
      </span>
      {disk ? (
        <>
          <StatusBadge level={disk.level} label={disk.label} />
          <span className="num text-ink-2">Retention prunes below {data.min_free_gb.toFixed(1)} GB.</span>
        </>
      ) : null}
    </>
  );
}

/**
 * The raw UBX hours on the card: the last two days as a strip, a bounded window export, the
 * RINEX export (opened on `?export=<preset>&hours=<n>`, the Site page's deep link) with its
 * jobs, and the file table with keep/download/delete. The hour being written is the one the
 * daemon flags `open` — it downloads (what exists so far) and never deletes. An hour clicked on
 * the strip loads into both the raw window and the export.
 */
export default function Logs() {
  const qc = useQueryClient();
  const [params] = useSearchParams();
  const [exportStart] = useState(() => exportParams(params));
  const [deepLinked] = useState(() => params.has("export"));
  useEffect(() => {
    // Opened from a deep link (the Site page's "Export the last 24 h"): at phone width the export
    // panel is the third one down, so bring it into view (the panel focuses its target itself).
    if (deepLinked) document.getElementById(EXPORT_PANEL_ANCHOR)?.scrollIntoView?.({ block: "start" });
  }, [deepLinked]);
  const [pickedHour, setPickedHour] = useState<[string, string] | undefined>(undefined);
  const logs = useLogs();
  const rawlog = useLive((s) => s.rawlog);
  const [now] = useState(() => Date.now());
  const hourFloor = Math.floor(now / HOUR_MS) * HOUR_MS;
  const availability = useAvailability(new Date(hourFloor - (STRIP_HOURS - 1) * HOUR_MS).toISOString(), new Date(hourFloor + HOUR_MS).toISOString());

  const [winFrom, setWinFrom] = useState(() => toInput(now - 24 * HOUR_MS));
  const [winTo, setWinTo] = useState(() => toInput(now));
  const [winError, setWinError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const anchorRef = useRef<HTMLAnchorElement>(null);
  const problem = windowProblem(winFrom, winTo);
  const windowUrl = problem ? null : logWindowUrl(`${winFrom}:00Z`, `${winTo}:00Z`);
  const selected = useMemo<[string, string] | null>(() => {
    const a = fromInput(winFrom);
    const b = fromInput(winTo);
    return a != null && b != null ? [new Date(a).toISOString(), new Date(b).toISOString()] : null;
  }, [winFrom, winTo]);

  const download = async () => {
    if (!windowUrl) return;
    setWinError(null);
    setChecking(true);
    try {
      await probeDownload(windowUrl);
      const a = anchorRef.current;
      if (a) {
        a.href = windowUrl;
        a.click();
      }
    } catch (err) {
      setWinError(describeError(err));
    } finally {
      setChecking(false);
    }
  };

  const invalidate = () => qc.invalidateQueries({ queryKey: ["logs"] });
  const keep = useMutation({ mutationFn: ({ name, value }: { name: string; value: boolean }) => setKeep(name, value), onSuccess: invalidate });
  const remove = useMutation({ mutationFn: ({ name, force }: { name: string; force: boolean }) => deleteLog(name, force), onSuccess: invalidate });

  const files = Array.isArray(logs.data?.files) ? logs.data.files : [];
  const anyOpen = files.some((f) => f.open);
  const newest = files.reduce<LogFile | null>((m, f) => (!m || f.hour_utc > m.hour_utc ? f : m), null);
  const columns = fileColumns({
    onKeep: (f, value) => keep.mutate({ name: f.name, value }),
    keepBusy: keep.isPending ? keep.variables.name : null,
    newestWithoutWriter: !anyOpen && newest ? newest.name : null,
    onDelete: (name, force) => remove.mutateAsync({ name, force }),
  });

  return (
    <>
      <PageHeader title="Logs">
        <div className="flex flex-wrap items-center gap-3">
          {logs.data ? <Summary data={logs.data} /> : null}
          {rawlog.error ? <StatusBadge level="critical" label="Log writer error" /> : rawlog.backpressure ? <StatusBadge level="serious" label="Log queue backing up" /> : null}
        </div>
      </PageHeader>
      {rawlog.error ? <p className="mb-4 text-ink-2">{rawlog.error}</p> : null}
      <div className="grid grid-cols-12 gap-4">
        <Panel className="col-span-12 lg:col-span-8" title="Last 48 hours">
          {availability.data ? (
            <AvailabilityStrip
              slots={availability.data}
              selected={selected}
              onSelect={(h) => {
                const t = parseUtc(h.hour_utc);
                if (!t) return;
                const hour: [string, string] = [toInput(t.getTime()), toInput(t.getTime() + HOUR_MS)];
                setWinFrom(hour[0]);
                setWinTo(hour[1]);
                setPickedHour(hour);
                setWinError(null);
              }}
            />
          ) : availability.isError ? (
            <Alert variant="destructive">
              <AlertDescription className="text-[14px] leading-5">Availability could not be read: {describeError(availability.error)}</AlertDescription>
            </Alert>
          ) : (
            <p className="text-ink-2">Loading…</p>
          )}
          <p className="mt-2 text-[12px] leading-4 text-ink-2">Strongest cells = complete hours, muted grey = partial (being written or recovered), empty outline = missing; each cell's name says which. Click an hour to load it into the raw window and the RINEX export; the bar underneath marks the raw window.</p>
        </Panel>

        <Panel className="col-span-12 lg:col-span-4" title="Download a raw window">
          <div className="flex flex-col gap-3">
            <div className="flex flex-col gap-1">
              <Label htmlFor="win-from">From (UTC)</Label>
              <Input id="win-from" type="datetime-local" className="num" value={winFrom} onChange={(e) => setWinFrom(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="win-to">To (UTC)</Label>
              <Input id="win-to" type="datetime-local" className="num" value={winTo} onChange={(e) => setWinTo(e.target.value)} />
            </div>
            {problem ? <p className="text-[12px] leading-4 text-ink-2">{problem}</p> : null}
            <Button type="button" disabled={problem != null || checking} onClick={download}>
              {checking ? "Checking…" : "Download .ubx"}
            </Button>
            <a ref={anchorRef} href={windowUrl ?? "#"} download hidden aria-hidden tabIndex={-1}>
              window
            </a>
            {winError ? (
              <Alert variant="destructive">
                <AlertDescription className="text-[14px] leading-5">{winError}</AlertDescription>
              </Alert>
            ) : null}
            <p className="text-[12px] leading-4 text-ink-2">Every hourly file overlapping the window, concatenated ({WINDOW_MAX_H}-hour cap). For a PPP service or PPK, Export RINEX below converts the window for you.</p>
          </div>
        </Panel>

        <Panel className="col-span-12 lg:col-span-6" title="Export RINEX" id={EXPORT_PANEL_ANCHOR}>
          <ExportPanel focusTarget={deepLinked} initialPreset={exportStart.preset} initialHours={exportStart.hours} window={pickedHour} onSubmitted={() => void qc.invalidateQueries({ queryKey: ["jobs"] })} />
        </Panel>
        <JobsPanel kind="export" title="Export jobs" className="col-span-12 lg:col-span-6" id={EXPORT_JOBS_ANCHOR} />

        <Panel className="col-span-12" title="Files" bodyClassName="p-2">
          {logs.isPending ? (
            <p className="p-2 text-ink-3">Loading the raw logs…</p>
          ) : logs.isError ? (
            <Alert variant="destructive" className="m-2">
              <AlertDescription className="text-[14px] leading-5">The raw logs could not be read: {describeError(logs.error)}</AlertDescription>
            </Alert>
          ) : (
            <DataTable
              aria-label="Raw log files"
              columns={columns}
              rows={files}
              rowKey={(f) => f.name}
              initialSort={{ key: "hour", dir: "desc" }}
              dense
              empty={<EmptyState title="No raw logs yet" body="Hourly UBX files appear here as soon as the raw logger writes its first hour." />}
            />
          )}
          {keep.isError ? (
            <div className="px-2 pt-3">
              <Alert variant="destructive">
                <AlertDescription className="text-[14px] leading-5">
                  <Lock className="mr-1 inline size-3.5" aria-hidden />
                  Keep for {keep.variables?.name} could not be changed: {describeError(keep.error)}
                </AlertDescription>
              </Alert>
            </div>
          ) : null}
        </Panel>
      </div>
    </>
  );
}
