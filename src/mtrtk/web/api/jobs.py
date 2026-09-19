"""Background jobs: list, inspect, download results, delete.

Read-only plus a delete. Nothing here submits work - Phase 5 registers the export and PPK job
functions and calls `JobRunner.submit` - so the whole router degrades to one status code on a
daemon that runs no job runner at all.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from mtrtk.jobs import Job, JobBusy, JobRunner

NO_RUNNER = "this daemon has no job runner"
NO_JOB = "no job with that id"
NO_FILE = "no such result file"

# Declared so the schema Phase 4 generates its client from carries them; FastAPI only infers
# the 2xx and the validation 422 on its own.
UNAVAILABLE: dict[int | str, dict[str, Any]] = {409: {"description": NO_RUNNER}}
JOB_ERRORS: dict[int | str, dict[str, Any]] = {**UNAVAILABLE, 404: {"description": NO_JOB}}
FILE_ERRORS: dict[int | str, dict[str, Any]] = {
    **UNAVAILABLE,
    404: {"description": "no such job, or no result file by that name"},
}
DELETE_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": NO_JOB},
    409: {"description": f"{NO_RUNNER}, or the job is still running"},
}

router = APIRouter(prefix="/api/jobs", tags=["jobs"], responses=UNAVAILABLE)


def _runner(request: Request) -> JobRunner:
    runner: JobRunner | None = request.app.state.ctx.jobs
    if runner is None:
        # 409, not 501: the same URL works on a daemon that runs jobs, and the UI hides the
        # panel rather than treating it as a bug in the request.
        raise HTTPException(409, NO_RUNNER)
    return runner


async def _job(runner: JobRunner, job_id: str) -> Job:
    job = await runner.get(job_id)
    if job is None:
        raise HTTPException(404, NO_JOB)
    return job


def _job_dir(runner: JobRunner, job_id: str) -> Path:
    try:
        return runner.job_dir(job_id)
    except ValueError as exc:  # an id no job could have; never a path to walk
        raise HTTPException(404, NO_JOB) from exc


def _scan(job_dir: Path) -> list[dict[str, Any]]:
    """The result files, by name. Off the event loop: it stats a directory on the SD card."""
    if not job_dir.is_dir():
        return []  # queued, or a job that wrote nothing
    return [
        {"name": p.name, "bytes": p.stat().st_size}
        for p in sorted(job_dir.iterdir())
        if p.is_file()
    ]


@router.get("")
async def list_jobs(
    request: Request, kind: str | None = None, limit: int = Query(50, ge=1, le=500)
) -> list[dict[str, Any]]:
    """Every job this daemon remembers, newest first."""
    jobs = await _runner(request).list(kind=kind, limit=limit)
    return [j.model_dump(mode="json") for j in jobs]


@router.get("/{job_id}", responses=JOB_ERRORS)
async def get_job(job_id: str, request: Request) -> dict[str, Any]:
    job = await _job(_runner(request), job_id)
    return job.model_dump(mode="json")


@router.delete("/{job_id}", responses=DELETE_ERRORS)
async def delete_job(job_id: str, request: Request) -> dict[str, bool]:
    """Forget a job: its row and its result directory. A running job is refused with 409."""
    runner = _runner(request)
    await _job(runner, job_id)
    try:
        await runner.delete(job_id)
    except JobBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@router.get("/{job_id}/files", responses=JOB_ERRORS)
async def list_result_files(job_id: str, request: Request) -> list[dict[str, Any]]:
    runner = _runner(request)
    await _job(runner, job_id)
    return await asyncio.to_thread(_scan, _job_dir(runner, job_id))


@router.get("/{job_id}/files/{name}", responses=FILE_ERRORS)
async def download(job_id: str, name: str, request: Request) -> FileResponse:
    """One result file, as an attachment.

    `result_path` is the guard: it accepts one path component and resolves it strictly inside
    the job's own directory, so neither a `..` nor a symlink the job wrote gets out of it.
    """
    runner = _runner(request)
    try:
        path = runner.result_path(job_id, name)
    except ValueError as exc:
        raise HTTPException(404, NO_FILE) from exc
    if not await asyncio.to_thread(path.is_file):
        raise HTTPException(404, NO_FILE)
    # octet-stream with a filename: a result is downloaded, never rendered in our own origin.
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)
