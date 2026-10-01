import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { EmptyState } from "@/components/EmptyState";
import { Panel } from "@/components/Panel";
import { StatusBadge } from "@/components/StatusBadge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { deleteJob, describeError, jobFileUrl, probeDownload } from "@/lib/api";
import { fmtBytes, fmtUtcDate, parseUtc, relTime } from "@/lib/format";
import { useLive } from "@/lib/live";
import type { StatusLevel } from "@/lib/palette";
import { useJobFiles, useJobs } from "@/lib/queries";
import type { ExportFile, Job, JobKind, JobStatus } from "@/lib/types";
import { cn } from "@/lib/utils";

const JOB_STATUS: Record<JobStatus, { level: StatusLevel; label: string }> = {
  queued: { level: "warning", label: "Queued" },
  running: { level: "warning", label: "Running" },
  done: { level: "good", label: "Done" },
  failed: { level: "critical", label: "Failed" },
};

/** What an export's result file is, by the `role` its manifest gives it. */
const FILE_ROLE: Record<string, string> = { obs: "observations", nav: "navigation", manifest: "manifest" };

function useNow(ms = 10_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

/** The result's `files` and `warnings` (an export's manifest), read defensively: `result` is untyped. */
function resultOf(job: Job): { roles: Map<string, string>; warnings: string[] } {
  const files = Array.isArray(job.result?.files) ? (job.result.files as Partial<ExportFile>[]) : [];
  const roles = new Map<string, string>();
  for (const f of files) if (typeof f?.name === "string" && typeof f.role === "string") roles.set(f.name, FILE_ROLE[f.role] ?? f.role);
  const warnings = Array.isArray(job.result?.warnings) ? (job.result.warnings as unknown[]).filter((w): w is string => typeof w === "string") : [];
  return { roles, warnings };
}

/** A job's window, `params.start`/`params.end` (a PPK job's `params.rover`), as a short UTC range; null when it has none. */
function windowOf(job: Job): string | null {
  const rover = job.params?.rover;
  const { start, end } = (rover && typeof rover === "object" ? rover : job.params ?? {}) as Record<string, unknown>;
  const a = typeof start === "string" ? parseUtc(start) : null;
  const b = typeof end === "string" ? parseUtc(end) : null;
  if (!a || !b) return null;
  const [sa, sb] = [a.toISOString(), b.toISOString()];
  // The end's date is left out when it is the start's: "2026-09-18 00:00 → 06:00 UTC".
  const endDay = sa.slice(0, 10) === sb.slice(0, 10) ? "" : `${sb.slice(0, 10)} `;
  return `${sa.slice(0, 10)} ${sa.slice(11, 16)} → ${endDay}${sb.slice(11, 16)} UTC`;
}

/**
 * One result file, downloaded only once the daemon has said it is there. A bare `<a download>`
 * would save the daemon's 404 or 401 body under the RINEX name - a file someone could then
 * upload to a PPP service.
 */
export function JobFileLink({ url, name, label }: { url: string; name: string; /** The link's accessible name; the file name by default. */ label?: string }) {
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const probed = useRef(false);
  return (
    <span className="flex min-w-0 flex-col">
      <a
        href={url}
        download
        aria-busy={checking || undefined}
        aria-label={label}
        className="num min-w-0 break-all hover:underline"
        onClick={(e) => {
          if (probed.current) {
            probed.current = false; // the probe passed: this is the real click, let it through
            return;
          }
          e.preventDefault();
          const link = e.currentTarget;
          setError(null);
          setChecking(true);
          probeDownload(url)
            .then(() => {
              probed.current = true;
              link.click();
            })
            .catch((err: unknown) => setError(describeError(err)))
            .finally(() => setChecking(false));
        }}
      >
        {name}
      </a>
      {error ? (
        <span role="alert" className="text-[12px] leading-4 text-status-critical-text">
          {error}
        </span>
      ) : null}
    </span>
  );
}

function JobRow({
  job,
  now,
  onDelete,
  onSelect,
  selected = false,
}: {
  job: Job;
  now: number;
  onDelete: (id: string) => Promise<unknown>;
  onSelect?: (job: Job) => void;
  selected?: boolean;
}) {
  const status = JOB_STATUS[job.status] ?? { level: "warning" as StatusLevel, label: job.status };
  const files = useJobFiles(job.status === "done" ? job.id : null);
  const running = job.status === "running";
  const pct = Math.round(Math.max(0, Math.min(1, job.progress)) * 100);
  const kind = job.kind.replace(/_/g, " ");
  const preset = typeof job.params?.preset === "string" ? job.params.preset : null;
  const span = windowOf(job);
  const failed = job.status === "failed";
  const { roles, warnings } = resultOf(job);
  return (
    <li data-job={job.id} data-selected={selected || undefined} className={cn("flex flex-col gap-2 border-b border-line py-3 last:border-0", selected && "-mx-2 rounded-md bg-panel-2 px-2")}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge level={status.level} label={status.label} />
          <span className="font-medium">
            {kind}
            {preset ? ` · ${preset}` : ""}
          </span>
          {span ? <span className="num text-[12px] leading-4 text-ink-2">{span}</span> : null}
          <span className="num text-[12px] leading-4 text-ink-2" title={fmtUtcDate(job.created_utc)}>
            {relTime(job.created_utc, now)}
          </span>
        </div>
        <span className="inline-flex items-center gap-1">
          {onSelect && job.status === "done" ? (
            <Button type="button" size="sm" variant={selected ? "default" : "outline"} aria-pressed={selected} aria-label={`View job ${job.id}`} onClick={() => onSelect(job)}>
              View
            </Button>
          ) : null}
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
        </span>
      </div>
      {running || job.status === "queued" ? <Progress value={pct} aria-label={`${kind} progress`} className="h-1.5" /> : null}
      {/* A failed job keeps its last progress message: worded as where it stopped, not as work going on. */}
      {job.message && !(failed && job.error) ? <p className="text-ink-2">{failed ? `Stopped while: ${job.message}` : job.message}</p> : null}
      {job.error ? (
        <p className="text-ink">
          <span className="font-medium">Error{failed && job.message ? ` while ${job.message}` : ""}:</span> {job.error}
        </p>
      ) : null}
      {files.data && files.data.length > 0 ? (
        <ul className="flex flex-wrap gap-x-4 gap-y-1 text-[14px]">
          {files.data.map((fl) => (
            <li key={fl.name} className="flex min-w-0 items-baseline gap-1.5">
              <JobFileLink url={jobFileUrl(job.id, fl.name)} name={fl.name} />
              <span className="num shrink-0 text-[12px] leading-4 text-ink-2">
                {roles.has(fl.name) ? `${roles.get(fl.name)} · ` : ""}
                {fmtBytes(fl.bytes)}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      {warnings.length > 0 ? (
        <ul className="flex list-disc flex-col gap-1 pl-4">
          {warnings.map((w) => (
            <li key={w} className="text-[12px] leading-4 text-status-warning-text">
              {w}
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

/** When a copy of a job was last written by the daemon (ms), for picking the fresher of two. */
const stamp = (j: Job): number => Date.parse(j.updated_utc ?? j.created_utc) || 0;

/**
 * The listing and the live slice merged by id, the fresher copy of each job winning.
 *
 * The listing (`GET /api/jobs`, polled) is the truth for what exists: the daemon publishes
 * nothing when a job is deleted, and a bus that falls behind drops updates without closing the
 * socket, so a live copy can be stale for as long as the tab stays connected. A job only the
 * live slice has is shown while it is queued or running, or when it is newer than the listing
 * (`listedAt`, ms): one that finished before the listing was read and is not in it was deleted
 * elsewhere.
 */
export function mergeJobs(listed: Job[] | undefined, live: Job[], listedAt: number): Job[] {
  const byId = new Map<string, Job>();
  for (const j of listed ?? []) byId.set(j.id, j);
  for (const j of live) {
    const known = byId.get(j.id);
    if (known) {
      if (stamp(j) >= stamp(known)) byId.set(j.id, j); // a tie is the same row: either will do
    } else if (!listed || j.status === "queued" || j.status === "running" || stamp(j) > listedAt) {
      byId.set(j.id, j);
    }
  }
  return [...byId.values()].sort((a, b) => (a.created_utc < b.created_utc ? 1 : a.created_utc > b.created_utc ? -1 : 0));
}

/**
 * The daemon's jobs, newest first: the listing seeded from `GET /api/jobs`, each `jobs.update`
 * from the socket laid over it by id when it is the fresher copy (`mergeJobs`). The live slice
 * empties on every snapshot (each reconnect) and refills as updates arrive; the listing keeps
 * every job on screen meanwhile. `kind` narrows both to one kind of job (`export`); without it
 * every job shows.
 */
export function JobsPanel({
  kind,
  title = "Jobs",
  className = "col-span-12",
  id,
  onSelect,
  selectedId = null,
}: {
  kind?: JobKind;
  title?: string;
  className?: string;
  id?: string;
  /** Offer a "View" button on each done job (the PPK page's result view). */
  onSelect?: (job: Job) => void;
  selectedId?: string | null;
}) {
  const qc = useQueryClient();
  const listed = useJobs(kind);
  const live = useLive((s) => s.jobs);
  const now = useNow();
  const jobs = useMemo(
    () =>
      mergeJobs(
        Array.isArray(listed.data) ? listed.data : undefined,
        Object.values(live).filter((j) => !kind || j.kind === kind),
        listed.dataUpdatedAt,
      ),
    [listed.data, listed.dataUpdatedAt, live, kind],
  );
  const forgetJob = useLive((s) => s.forgetJob);
  const remove = useMutation({
    mutationFn: (jobId: string) => deleteJob(jobId),
    // The daemon publishes nothing on a delete, so the live slice would keep laying the job over
    // the refetched listing until the next reconnect: drop it here.
    onSuccess: (_r, jobId) => {
      forgetJob(jobId);
      return qc.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
  return (
    <Panel className={className} title={title} id={id}>
      {listed.isPending && jobs.length === 0 ? (
        <p className="text-ink-2">Loading jobs…</p>
      ) : listed.isError && jobs.length === 0 ? (
        <Alert variant="destructive">
          <AlertDescription className="text-[14px] leading-5">The jobs could not be read: {describeError(listed.error)}</AlertDescription>
        </Alert>
      ) : jobs.length === 0 ? (
        kind === "export" ? (
          <EmptyState title="No export jobs yet" body="Start one from Export RINEX; its progress and result files appear here." />
        ) : kind === "ppk" ? (
          <EmptyState title="No PPK jobs yet" body="Run one from the form; its progress and result files appear here." />
        ) : (
          <EmptyState title="No jobs yet" body="Exports and other background work appear here." />
        )
      ) : (
        <ul className="-my-3">
          {jobs.map((j) => (
            <JobRow key={j.id} job={j} now={now} onDelete={(jobId) => remove.mutateAsync(jobId)} onSelect={onSelect} selected={j.id === selectedId} />
          ))}
        </ul>
      )}
    </Panel>
  );
}
