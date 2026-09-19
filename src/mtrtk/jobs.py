"""Persistent background jobs (RINEX export, PPK) with progress, result directories and bus updates.

A job is three things kept in step: a row in `jobs`, a directory of result files under
`<data_dir>/jobs/<id>/`, and a coroutine that fills both. The row is the truth - it outlives the
process, so the UI can still show what last night's export produced - and every change to it is
published on `jobs.update`, which the WebSocket hub already forwards to the `jobs` topic. Nothing
here touches the hub, or a socket, directly.

One job runs at a time by default. RTKLIB on a Pi is CPU-bound, and a second `convbin` next to
the first would starve the receiver reader and the caster, which is the one thing this daemon
must never stop doing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from mtrtk.core.bus import Bus
from mtrtk.store.db import Database

log = logging.getLogger(__name__)

TOPIC = "jobs.update"
LIVE = "status IN ('queued', 'running')"  # what a crash, or a shutdown, can leave behind
INTERRUPTED = "interrupted by restart"
SHUTDOWN_REASON = "shutdown"
CANCELLED_REASON = "cancelled"
SHUTDOWN_GRACE_S = 5.0  # how long shutdown waits for a cancelled job to let go

Status = Literal["queued", "running", "done", "failed"]


class JobBusy(RuntimeError):
    """The job is running, and what was asked cannot be done to a job that is running."""


class Job(BaseModel):
    id: str
    kind: str
    status: Status
    created_utc: datetime
    updated_utc: datetime | None = None
    progress: float = 0.0
    message: str | None = None
    params: dict[str, Any] = {}
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class JobContext:
    """What a job function is handed: its own row, its own directory, and a way to report."""

    job: Job
    dir: Path
    _report: Callable[[float, str | None], Awaitable[None]]

    async def progress(self, fraction: float, message: str | None = None) -> None:
        await self._report(max(0.0, min(1.0, fraction)), message)


JobFn = Callable[[JobContext], Awaitable[dict[str, Any]]]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _component(value: str, label: str) -> str:
    """One path component, or `ValueError`.

    Job ids and result file names arrive from the URL and end up in `rmtree` and in an open()
    - so a separator, a `..`, an absolute path or a NUL is refused here rather than resolved
    into something outside the job directory.
    """
    if not value or value in (".", "..") or "\x00" in value:
        raise ValueError(f"invalid {label}: {value!r}")
    if "/" in value or "\\" in value or Path(value).name != value:
        raise ValueError(f"invalid {label}: {value!r}")
    return value


class JobRunner:
    """Owns the job rows, their directories and the tasks running them.

    Built once per process - by the daemon, which calls `restore()` after the database is open
    and `shutdown()` before it closes - and handed to the web layer as `AppContext.jobs`.
    """

    def __init__(self, db: Database, bus: Bus, root: Path, max_concurrent: int = 1) -> None:
        self.db = db
        self.bus = bus
        self.root = Path(root)
        self.max_concurrent = max_concurrent
        self._sem = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # Why a task was cancelled, so `_run` can say so in the row. `None` means "the row is
        # being deleted": do not write to it on the way out.
        self._cancel_reason: dict[str, str | None] = {}
        self._closing = False

    # ------------------------------------------------------------------ paths
    def job_dir(self, job_id: str) -> Path:
        """Where a job's results live. The id is validated: this path reaches `rmtree`."""
        return self.root / _component(job_id, "job id")

    def result_path(self, job_id: str, name: str) -> Path:
        """One result file of one job, or `ValueError`.

        Both halves are single path components, and the resolved path is checked against the
        resolved job directory as well - so a symlink a job wrote into its own directory cannot
        be used to read the rest of the card either.
        """
        base = self.job_dir(job_id).resolve()
        target = (base / _component(name, "result file name")).resolve()
        if target == base or not target.is_relative_to(base):
            raise ValueError(f"invalid result file name: {name!r}")
        return target

    # ------------------------------------------------------------ persistence
    @staticmethod
    def _row_to_job(row: Any) -> Job:
        data = dict(row)
        data["params"] = json.loads(data.get("params") or "{}")
        data["result"] = json.loads(data["result"]) if data.get("result") else None
        return Job(**data)

    async def get(self, job_id: str) -> Job | None:
        row = await self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return self._row_to_job(row) if row else None

    async def list(self, kind: str | None = None, limit: int = 50) -> list[Job]:
        """Newest first. `id` breaks the tie between two jobs submitted in the same microsecond."""
        order = "ORDER BY created_utc DESC, id DESC LIMIT ?"
        if kind:
            sql = f"SELECT * FROM jobs WHERE kind = ? {order}"
            rows = await self.db.fetchall(sql, (kind, limit))
        else:
            rows = await self.db.fetchall(f"SELECT * FROM jobs {order}", (limit,))
        return [self._row_to_job(r) for r in rows]

    async def _update(self, job_id: str, **fields: Any) -> Job | None:
        """Write one change and publish the row that came of it.

        The column names are this module's own keyword arguments, never anything from a request,
        so building the assignment list is not string-interpolating user input into SQL.
        """
        fields["updated_utc"] = _now()
        if fields.get("result") is not None:
            fields["result"] = json.dumps(fields["result"])
        assignments = ", ".join(f"{k} = ?" for k in fields)
        await self.db.execute(
            f"UPDATE jobs SET {assignments} WHERE id = ?", [*fields.values(), job_id]
        )
        await self.db.commit()
        job = await self.get(job_id)
        if job is None:  # deleted while it was in flight: there is nothing left to announce
            log.debug("job %s vanished before its update could be published", job_id)
            return None
        self.bus.publish(TOPIC, job)
        return job

    async def restore(self) -> None:
        """Fail whatever the last run left in flight. Called once, at startup.

        Nothing is requeued. A `running` row means the process died part-way through a job whose
        result directory is half written, and re-running it unasked would repeat whatever it had
        already done to the card. The operator sees why it stopped and submits it again.
        """
        async with self.db.transaction():
            rows = await self.db.fetchall(f"SELECT id FROM jobs WHERE {LIVE}")
            if rows:
                await self.db.execute(
                    f"UPDATE jobs SET status = 'failed', error = ?, updated_utc = ? WHERE {LIVE}",
                    (INTERRUPTED, _now()),
                )
        if not rows:
            return
        log.info("marked %d job(s) interrupted by the last restart as failed", len(rows))
        for row in rows:
            job = await self.get(str(row["id"]))
            if job is not None:
                self.bus.publish(TOPIC, job)

    # -------------------------------------------------------------- lifecycle
    async def submit(self, kind: str, params: dict[str, Any], fn: JobFn) -> Job:
        """Queue a job and return its row. The work starts as soon as a slot frees up."""
        if self._closing:
            raise RuntimeError("the job runner has shut down; no new jobs are accepted")
        job_id = uuid.uuid4().hex[:12]
        await self.db.execute(
            "INSERT INTO jobs (id, kind, status, created_utc, progress, params) "
            "VALUES (?, ?, 'queued', ?, 0, ?)",
            (job_id, kind, _now(), json.dumps(params)),
        )
        await self.db.commit()
        job = await self.get(job_id)
        assert job is not None  # we just inserted it, inside our own serialised connection
        self.bus.publish(TOPIC, job)
        self._tasks[job_id] = asyncio.create_task(self._run(job, fn), name=f"job-{kind}-{job_id}")
        return job

    async def _run(self, job: Job, fn: JobFn) -> None:
        try:
            async with self._sem:
                await self._execute(job, fn)
        except asyncio.CancelledError:
            await self._mark_cancelled(job)
            raise
        except Exception:
            # Nothing awaits these tasks while they run, so an exception that escaped here would
            # surface only as "Task exception was never retrieved", long after the fact.
            log.exception("job %s (%s) ended abnormally", job.id, job.kind)
        finally:
            self._tasks.pop(job.id, None)
            self._cancel_reason.pop(job.id, None)

    async def _execute(self, job: Job, fn: JobFn) -> None:
        job_dir = self.job_dir(job.id)
        await asyncio.to_thread(job_dir.mkdir, parents=True, exist_ok=True)

        async def report(fraction: float, message: str | None) -> None:
            await self._update(job.id, progress=fraction, message=message)

        await self._update(job.id, status="running", message="started")
        try:
            result = await fn(JobContext(job, job_dir, report))
            if not isinstance(result, dict):
                raise TypeError(f"a job must return a dict, not {type(result).__name__}")
            # Encoded here rather than inside the UPDATE: a result that will not serialise is the
            # job's failure, not a write that dies half-way and leaves the row saying "running".
            json.dumps(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("job %s (%s) failed", job.id, job.kind)
            await self._update(job.id, status="failed", error=f"{type(exc).__name__}: {exc}")
            return
        await self._update(job.id, status="done", progress=1.0, message="finished", result=result)

    async def _mark_cancelled(self, job: Job) -> None:
        """Record why a cancelled job stopped, unless its row is on its way out anyway."""
        reason = self._cancel_reason.get(job.id, CANCELLED_REASON)
        if reason is None:  # `delete()` is about to remove the row
            return
        try:
            await self._update(job.id, status="failed", error=reason)
        except asyncio.CancelledError:  # cancelled a second time: the row keeps its last state
            raise
        except Exception:  # e.g. the database went away first
            log.warning("could not record the end of job %s", job.id, exc_info=True)

    async def delete(self, job_id: str) -> None:
        """Remove a job's row and its result directory. A running job is refused.

        Killing work half-way leaves a directory nobody can interpret and, for an export, a
        partial file an operator may already be downloading. Stopping it is a separate decision
        from forgetting it, so this says no and the caller decides.
        """
        job = await self.get(job_id)
        if job is not None and job.status == "running":
            raise JobBusy(f"job {job_id} is still running")
        task = self._tasks.pop(job_id, None)
        if task is not None:
            # Queued and never started. Its row is going, so `_run` must not write to it.
            self._cancel_reason[job_id] = None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(shutil.rmtree, self.job_dir(job_id), ignore_errors=True)
        await self.db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        await self.db.commit()

    async def shutdown(self, grace_s: float = SHUTDOWN_GRACE_S) -> None:
        """Cancel what is running, wait briefly for the rows to say so, and accept no more.

        Bounded on purpose: a job wedged in a blocking call cannot be cancelled at all, and the
        daemon has a receiver and a caster to close behind this. Anything still pending keeps
        its entry in `_tasks`, so nothing collects a task that is still touching the database.
        """
        self._closing = True
        tasks = list(self._tasks.values())
        for job_id in self._tasks:
            self._cancel_reason.setdefault(job_id, SHUTDOWN_REASON)
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        # `asyncio.wait`, not `asyncio.timeout`: a timeout that expired around a `gather`
        # would cancel these tasks a second time, part-way through the very UPDATE that records
        # why they stopped. This waits, and then simply stops waiting.
        done, pending = await asyncio.wait(tasks, timeout=grace_s)
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                log.error("job task ended with %r", task.exception())
        if pending:
            log.warning("%d job task(s) did not stop within %.1fs", len(pending), grace_s)
