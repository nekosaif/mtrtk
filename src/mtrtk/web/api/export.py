"""RINEX export: presets, background export jobs, and a bounded synchronous zip download.

`POST /api/export` queues a `JobRunner` job (kind `export`, one at a time) whose result files and
`manifest.json` land in the job's own directory, where `/api/jobs/{id}/files` serves them.
`GET /api/export/rinex` does the same work inside the request for a window of at most 6 h and
answers with one zip.

Errors: a window no raw log covers is a 404 - refused before a job is queued, so the operator
learns it at once rather than from a failed job. Everything else `export_to_dir` raises on
purpose (`EXPORT_ERRORS`: a setting that cannot name the files, convbin missing or failing, a
directory that cannot be written) is a 409 carrying the exporter's own message, which is written
for the operator.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
import zipfile
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import ValidationError
from starlette.types import Receive, Scope, Send

from mtrtk.rawlog.index import files_for_window
from mtrtk.rinex.export import (
    EXPORT_ERRORS,
    ExportContext,
    ExportRequest,
    ExportResult,
    export_to_dir,
    frequencies_from_state,
    header_from_settings,
    make_export_job,
)
from mtrtk.rinex.presets import PRESETS
from mtrtk.rinex.splice import NoDataError
from mtrtk.store.repos import SitesRepo
from mtrtk.web.context import AppContext

SYNC_MAX = timedelta(hours=6)
SYNC_TOO_LONG = (
    "the synchronous download is limited to 6 h; use POST /api/export (a background job) "
    "for a longer window"
)
NO_RUNNER = "this daemon has no job runner"
WORK_DIR = "tmp"  # under DATA_DIR: a 6 h export is too big for a RAM-backed /tmp on a Pi
WORK_PREFIX = "export-"
STALE_WORK_S = 24 * 3600  # a working directory this old was left by a daemon that died mid-export

SUBMIT_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "no raw logs of this station cover the window"},
    409: {"description": NO_RUNNER},
}
RINEX_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "no raw logs of this station cover the window"},
    409: {"description": "the export could not be made (settings, convbin, disk); detail says why"},
    422: {"description": "a bad window or option, or a window longer than 6 h"},
}

router = APIRouter(prefix="/api/export", tags=["export"])


def _ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


async def _export_context(ctx: AppContext) -> ExportContext:
    state = ctx.store.state
    site = await SitesRepo(ctx.db).active()
    return ExportContext(
        root=ctx.settings.data_dir,
        station_id=ctx.settings.station_id,
        country=ctx.settings.country,
        header=header_from_settings(ctx.settings, state, site),
        frequencies=frequencies_from_state(state),
    )


def _covered(root: Path, station: str, start: datetime, end: datetime) -> bool:
    """The rule `splice_window` applies: one of this station's hours overlaps the window itself.

    The lead hour before the window is not data - it only feeds the navigation file.
    """
    return any(lf.station_id == station for lf in files_for_window(root, start, end))


async def _require_data(ctx: AppContext, req: ExportRequest) -> None:
    station = ctx.settings.station_id
    if not await asyncio.to_thread(_covered, ctx.settings.data_dir, station, req.start, req.end):
        raise HTTPException(
            404,
            f"no raw logs for station {station} between {req.start.isoformat()} and "
            f"{req.end.isoformat()}; check GET /api/logs/availability for the hours on disk",
        )


@router.get("/presets")
async def presets() -> list[dict[str, Any]]:
    """The fixed presets, in the order the UI offers them. Tuples come out as JSON lists."""
    return [asdict(p) for p in PRESETS.values()]


@router.post("", responses=SUBMIT_ERRORS)
async def submit(req: ExportRequest, request: Request) -> dict[str, Any]:
    """Queue an export job and answer with its row; progress arrives on the `jobs` topic."""
    ctx = _ctx(request)
    if ctx.jobs is None:
        raise HTTPException(409, NO_RUNNER)
    await _require_data(ctx, req)
    job = await ctx.jobs.submit(
        "export", req.model_dump(mode="json"), make_export_job(req, await _export_context(ctx))
    )
    return job.model_dump(mode="json")


def _issues(exc: ValidationError) -> list[dict[str, Any]]:
    """The `[{loc, msg, type}]` shape of the app's own 422 handler, with nothing of the input."""
    return [
        {"loc": ["query", *err["loc"]], "msg": err["msg"], "type": err["type"]}
        for err in exc.errors()
    ]


def _zip(work: Path, result: ExportResult, dest: Path) -> None:
    """Pack the export into `dest`; files that are already gzipped are stored, not deflated."""
    with zipfile.ZipFile(dest, "w") as zf:
        for f in result.files:
            method = zipfile.ZIP_STORED if f["name"].endswith(".gz") else zipfile.ZIP_DEFLATED
            zf.write(work / f["name"], f["name"], compress_type=method)


def _work_dir(data_dir: Path) -> Path:
    """A fresh working directory, after clearing any a crashed daemon left behind."""
    base = data_dir / WORK_DIR
    base.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - STALE_WORK_S
    for old in base.glob(f"{WORK_PREFIX}*"):
        try:
            stale = old.is_dir() and old.stat().st_mtime < cutoff
        except OSError:
            continue
        if stale:
            shutil.rmtree(old, ignore_errors=True)
    return Path(tempfile.mkdtemp(prefix=WORK_PREFIX, dir=base))


class _ZipResponse(FileResponse):
    """The zip, sent from the working directory that is removed once it has gone - or failed to:
    a `background` task would be skipped when the client hangs up mid-download."""

    def __init__(self, archive: Path, work: Path) -> None:
        super().__init__(archive, media_type="application/zip", filename=archive.name)
        self.work = work

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await asyncio.shield(asyncio.to_thread(shutil.rmtree, self.work, ignore_errors=True))


@router.get("/rinex", responses=RINEX_ERRORS)
async def rinex_zip(
    request: Request,
    from_: Annotated[datetime, Query(alias="from")],
    to: Annotated[datetime, Query()],
    preset: str = "generic",
    interval: float | None = None,
    hatanaka: bool | None = None,
    gzip: bool | None = None,
) -> FileResponse:
    """Export a window of at most 6 h and answer with a zip of the RINEX files and manifest.

    The zip is named after the observation file. Longer windows go through `POST /api/export`.
    """
    try:
        req = ExportRequest(
            start=from_, end=to, preset=preset, interval_s=interval, hatanaka=hatanaka, gzip=gzip
        )
    except ValidationError as exc:
        raise HTTPException(422, _issues(exc)) from exc
    if req.end - req.start > SYNC_MAX:
        raise HTTPException(422, SYNC_TOO_LONG)
    ctx = _ctx(request)
    await _require_data(ctx, req)
    export_ctx = await _export_context(ctx)
    work = await asyncio.to_thread(_work_dir, ctx.settings.data_dir)
    try:
        out = work / "out"
        try:
            result = await export_to_dir(req, export_ctx, out)
        except NoDataError as exc:  # the hour went away between the check and the splice
            raise HTTPException(404, str(exc)) from exc
        except EXPORT_ERRORS as exc:
            raise HTTPException(409, str(exc)) from exc
        obs = next(f["name"] for f in result.files if f["role"] == "obs")
        stem = obs.split(".")[0]
        archive = work / f"{stem}.zip"
        await asyncio.to_thread(_zip, out, result, archive)
    except BaseException:
        await asyncio.shield(asyncio.to_thread(shutil.rmtree, work, ignore_errors=True))
        raise
    return _ZipResponse(archive, work)
