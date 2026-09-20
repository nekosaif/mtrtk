import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleDashed, Lock } from "lucide-react";
import { PageHeader } from "@/app/PageHeader";
import { Panel } from "@/components/Panel";
import { EmptyState } from "@/components/EmptyState";
import { StatusBadge } from "@/components/StatusBadge";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { AvailabilityStrip } from "@/components/charts/AvailabilityStrip";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Switch } from "@/components/ui/switch";
import { deleteJob, deleteLog, describeError, jobFileUrl, logFileUrl, logWindowUrl, probeDownload, setKeep } from "@/lib/api";
import { fmtBytes, fmtUtcDate, parseUtc, relTime } from "@/lib/format";
import { useLive } from "@/lib/live";
import type { StatusLevel } from "@/lib/palette";
import { useAvailability, useJobFiles, useJobs, useLogs } from "@/lib/queries";
import type { Job, JobStatus, LogFile, LogsResponse } from "@/lib/types";

const HOUR_MS = 3600_000;
/** The window export's cap, mirrored client-side so the button says so before the daemon has to. */
export const WINDOW_MAX_H = 48;
/** How many whole hours the strip shows, the current one included. */
const STRIP_HOURS = 48;

// ------------------------------------------------------------------------------------- helpers

/** A `datetime-local` value ("2026-09-18T12:20") for an instant, read as UTC. */
export function toInput(ms: number): string {
  return new Date(ms).toISOString().slice(0, 16);
}

/** The instant a `datetime-local` value names, taken as UTC; null when the field is empty or odd. */
export function fromInput(value: string): number | null {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) return null;
  const ms = Date.parse(`${value}:00Z`);
  return Number.isNaN(ms) ? null : ms;
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

const JOB_STATUS: Record<JobStatus, { level: StatusLevel; label: string }> = {
  queued: { level: "warning", label: "Queued" },
  running: { level: "warning", label: "Running" },
  done: { level: "good", label: "Done" },
  failed: { level: "critical", label: "Failed" },
};

function useNow(ms = 10_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

// ------------------------------------------------------------------------------------ files

function StateCell({ f }: { f: LogFile }) {
  if (f.open) {
    return (
      <span className="inline-flex items-center gap-1">
        <CircleDashed className="size-3.5 text-status-warning" aria-hidden />
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

// ------------------------------------------------------------------------------------- jobs

function JobRow({ job, now, onDelete }: { job: Job; now: number; onDelete: (id: string) => Promise<unknown> }) {
  const status = JOB_STATUS[job.status] ?? { level: "warning" as StatusLevel, label: job.status };
  const files = useJobFiles(job.status === "done" ? job.id : null);
  const running = job.status === "running";
  const pct = Math.round(Math.max(0, Math.min(1, job.progress)) * 100);
  return (
    <li className="flex flex-col gap-2 border-b border-line py-3 last:border-0">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge level={status.level} label={status.label} />
          <span className="font-medium">{job.kind.replace(/_/g, " ")}</span>
          <span className="num text-[12px] leading-4 text-ink-2" title={fmtUtcDate(job.created_utc)}>
            {relTime(job.created_utc, now)}
          </span>
        </div>
        <span title={running ? "A running job cannot be deleted." : undefined} className="inline-flex">
          <ConfirmDialog
            trigger={
              <Button type="button" size="sm" variant="ghost" disabled={running}>
                Delete
              </Button>
            }
            title={`Delete job ${job.id}?`}
            body="The job's record and its result files are removed."
            confirmLabel="Delete"
            destructive
            onConfirm={() => onDelete(job.id)}
          />
        </span>
      </div>
      {running || job.status === "queued" ? <Progress value={pct} aria-label={`${job.kind.replace(/_/g, " ")} progress`} className="h-1.5" /> : null}
      {job.message ? <p className="text-ink-2">{job.message}</p> : null}
      {job.error ? <p className="text-ink-2">{job.error}</p> : null}
      {files.data && files.data.length > 0 ? (
        <ul className="flex flex-wrap gap-x-4 gap-y-1 text-[14px]">
          {files.data.map((fl) => (
            <li key={fl.name} className="flex items-baseline gap-1.5">
              <a href={jobFileUrl(job.id, fl.name)} download className="num hover:underline">
                {fl.name}
              </a>
              <span className="num text-[12px] leading-4 text-ink-2">{fmtBytes(fl.bytes)}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

/**
 * Every job the daemon remembers, newest first: the listing seeded from `GET /api/jobs`, each
 * `jobs.update` from the socket laid over it. No job is started from here yet — the exports that
 * make them arrive in Phase 5.
 */
function JobsPanel() {
  const qc = useQueryClient();
  const listed = useJobs();
  const live = useLive((s) => s.jobs);
  const now = useNow();
  const jobs = useMemo(() => {
    const byId = new Map<string, Job>();
    for (const j of Array.isArray(listed.data) ? listed.data : []) byId.set(j.id, j);
    for (const j of Object.values(live)) byId.set(j.id, j);
    return [...byId.values()].sort((a, b) => (a.created_utc < b.created_utc ? 1 : a.created_utc > b.created_utc ? -1 : 0));
  }, [listed.data, live]);
  const remove = useMutation({ mutationFn: (id: string) => deleteJob(id), onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }) });
  return (
    <Panel className="col-span-12" title="Jobs">
      {listed.isPending && jobs.length === 0 ? (
        <p className="text-ink-2">Loading jobs…</p>
      ) : listed.isError && jobs.length === 0 ? (
        <Alert variant="destructive">
          <AlertDescription className="text-[14px] leading-5">The jobs could not be read: {describeError(listed.error)}</AlertDescription>
        </Alert>
      ) : jobs.length === 0 ? (
        <EmptyState title="No jobs yet" body="Exports appear here — Phase 5." />
      ) : (
        <ul className="-my-3">
          {jobs.map((j) => (
            <JobRow key={j.id} job={j} now={now} onDelete={(id) => remove.mutateAsync(id)} />
          ))}
        </ul>
      )}
    </Panel>
  );
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
 * file table with keep/download/delete, and the daemon's jobs. The hour being written is the
 * one the daemon flags `open` — it downloads (what exists so far) and never deletes.
 */
export default function Logs() {
  const qc = useQueryClient();
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
                setWinFrom(toInput(t.getTime()));
                setWinTo(toInput(t.getTime() + HOUR_MS));
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
          <p className="mt-2 text-[12px] leading-4 text-ink-2">Brass = complete hour, grey = partial (being written or recovered), empty = missing. Click an hour to load it into the window beside; the bar underneath marks that window.</p>
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
            <p className="text-[12px] leading-4 text-ink-2">Every hourly file overlapping the window, concatenated ({WINDOW_MAX_H}-hour cap). Convert with RTKLIB convbin for PPP or PPK.</p>
          </div>
        </Panel>

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

        <JobsPanel />
      </div>
    </>
  );
}
