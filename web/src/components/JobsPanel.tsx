import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { EmptyState } from "@/components/EmptyState";
import { Panel } from "@/components/Panel";
import { StatusBadge } from "@/components/StatusBadge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { deleteJob, describeError, jobFileUrl } from "@/lib/api";
import { fmtBytes, fmtUtcDate, relTime } from "@/lib/format";
import { useLive } from "@/lib/live";
import type { StatusLevel } from "@/lib/palette";
import { useJobFiles, useJobs } from "@/lib/queries";
import type { ExportFile, Job, JobKind, JobStatus } from "@/lib/types";

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

function JobRow({ job, now, onDelete }: { job: Job; now: number; onDelete: (id: string) => Promise<unknown> }) {
  const status = JOB_STATUS[job.status] ?? { level: "warning" as StatusLevel, label: job.status };
  const files = useJobFiles(job.status === "done" ? job.id : null);
  const running = job.status === "running";
  const pct = Math.round(Math.max(0, Math.min(1, job.progress)) * 100);
  const kind = job.kind.replace(/_/g, " ");
  const preset = typeof job.params?.preset === "string" ? job.params.preset : null;
  const { roles, warnings } = resultOf(job);
  return (
    <li data-job={job.id} className="flex flex-col gap-2 border-b border-line py-3 last:border-0">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge level={status.level} label={status.label} />
          <span className="font-medium">
            {kind}
            {preset ? ` · ${preset}` : ""}
          </span>
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
      {running || job.status === "queued" ? <Progress value={pct} aria-label={`${kind} progress`} className="h-1.5" /> : null}
      {job.message ? <p className="text-ink-2">{job.message}</p> : null}
      {job.error ? <p className="text-ink-2">{job.error}</p> : null}
      {files.data && files.data.length > 0 ? (
        <ul className="flex flex-wrap gap-x-4 gap-y-1 text-[14px]">
          {files.data.map((fl) => (
            <li key={fl.name} className="flex min-w-0 items-baseline gap-1.5">
              <a href={jobFileUrl(job.id, fl.name)} download className="num min-w-0 break-all hover:underline">
                {fl.name}
              </a>
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

/**
 * The daemon's jobs, newest first: the listing seeded from `GET /api/jobs`, each `jobs.update`
 * from the socket laid over it by id. The listing is the floor: the live slice empties on every
 * snapshot (each reconnect) and refills as updates arrive, and a job never drops out meanwhile.
 * `kind` narrows both to one kind of job (`export`); without it every job shows.
 */
export function JobsPanel({ kind, title = "Jobs", className = "col-span-12", id }: { kind?: JobKind; title?: string; className?: string; id?: string }) {
  const qc = useQueryClient();
  const listed = useJobs(kind);
  const live = useLive((s) => s.jobs);
  const now = useNow();
  const jobs = useMemo(() => {
    const byId = new Map<string, Job>();
    for (const j of Array.isArray(listed.data) ? listed.data : []) byId.set(j.id, j);
    for (const j of Object.values(live)) if (!kind || j.kind === kind) byId.set(j.id, j);
    return [...byId.values()].sort((a, b) => (a.created_utc < b.created_utc ? 1 : a.created_utc > b.created_utc ? -1 : 0));
  }, [listed.data, live, kind]);
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
        ) : (
          <EmptyState title="No jobs yet" body="Exports and other background work appear here." />
        )
      ) : (
        <ul className="-my-3">
          {jobs.map((j) => (
            <JobRow key={j.id} job={j} now={now} onDelete={(jobId) => remove.mutateAsync(jobId)} />
          ))}
        </ul>
      )}
    </Panel>
  );
}
