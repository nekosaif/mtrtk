"""RINEX export: presets, background export jobs, and a bounded synchronous zip download.

`POST /api/export` queues a `JobRunner` job (kind `export`, one at a time) whose result files and
`manifest.json` land in the job's own directory, where `/api/jobs/{id}/files` serves them.
`GET /api/export/rinex` does the same work inside the request for a window of at most 6 h and
answers with one zip.

One export at a time: the synchronous route refuses (409) while another synchronous export or an
export job is queued or running, and `POST /api/export` refuses while a synchronous one runs.
Each export stages its spliced UBX and its RINEX on the card the raw logs live on, and retention
deletes the oldest raw hours when free space drops below its floor - so exports stacking up must
not be what pushes it there, and two convbins at once are more than a Pi should be asked for.

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
from collections.abc import Callable
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
SYNC_BUSY = (
    "another export is running: a synchronous download is being made; wait for it to finish, "
    "then try again"
)
JOB_BUSY = (
    "another export is running: an export job is queued or running; wait for it (GET /api/jobs) "
    "or queue this one too with POST /api/export"
)
CROSS_SITE = (
    "refused: this download was started from another site; open it from the mtrtk UI, "
    "or call it without a browser"
)
_SYNC_FLAG = "sync_export_running"  # on `app.state`: one synchronous export per app
# ExportRequest field -> the query parameter `GET /api/export/rinex` takes it from.
QUERY_NAMES = {"start": "from", "end": "to", "interval_s": "interval"}
WORK_DIR = "tmp"  # under DATA_DIR: a 6 h export is too big for a RAM-backed /tmp on a Pi
WORK_PREFIX = "export-"
STALE_WORK_S = 24 * 3600  # a working directory this old was left by a daemon that died mid-export

SUBMIT_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "no raw logs of this station cover the window"},
    409: {
        "description": (
            "no job runner, the runner is shutting down, or a synchronous export is running"
        )
    },
}
RINEX_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "no raw logs of this station cover the window"},
    409: {
        "description": (
            "another export is running, or the export could not be made (settings, convbin, "
            "disk); detail says why"
        )
    },
    403: {"description": "a browser request started from another site"},
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
        min_free_gb=ctx.settings.min_free_gb,
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
    job_fn = make_export_job(req, await _export_context(ctx))
    # Checked last, with no await before `submit` registers the job: a synchronous export that
    # started while the window was being checked is seen here, and one starting after sees the job.
    if getattr(request.app.state, _SYNC_FLAG, False):
        raise HTTPException(409, SYNC_BUSY)
    try:
        job = await ctx.jobs.submit("export", req.model_dump(mode="json"), job_fn)
    except RuntimeError as exc:  # the runner is shutting down
        raise HTTPException(409, str(exc)) from exc
    return job.model_dump(mode="json")


def _issues(exc: ValidationError) -> list[dict[str, Any]]:
    """The `[{loc, msg, type}]` shape of the app's own 422 handler, with nothing of the input.

    `loc` names the query parameter the client sent (`interval`), not the model field.
    """
    return [
        {
            "loc": ["query", *(QUERY_NAMES.get(str(p), p) for p in err["loc"])],
            "msg": _query_message(str(err["msg"])),
            "type": err["type"],
        }
        for err in exc.errors()
    ]


def _query_message(msg: str) -> str:
    """A validator's message as this route's caller reads it: without pydantic's framing, and
    naming the query parameters (`'interval'`) rather than the model's fields."""
    msg = msg.removeprefix("Value error, ")
    for field, param in QUERY_NAMES.items():
        msg = msg.replace(f"'{field}'", f"'{param}'")
    return msg


def _same_origin(request: Request) -> bool:
    """False for a request a browser says came from another site.

    The session cookie is SameSite=Lax, which still rides along on a top-level navigation from
    any page - and this GET does work: it splices, converts and takes the one export slot.
    A browser sends `Sec-Fetch-Site`; a script or curl sends none, and is let through.
    """
    site = request.headers.get("sec-fetch-site")
    return site is None or site in ("same-origin", "none")


def _stage_message(exc: OSError) -> str:
    return f"cannot stage the download under DATA_DIR/{WORK_DIR}: {exc.strerror or exc}"


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
    a `background` task would be skipped when the client hangs up mid-download. *on_close* runs
    after that, whatever happened: it frees the one synchronous export slot."""

    def __init__(
        self, archive: Path, work: Path, on_close: Callable[[], None] | None = None
    ) -> None:
        super().__init__(archive, media_type="application/zip", filename=archive.name)
        self.work = work
        self.on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await asyncio.shield(
                    asyncio.to_thread(shutil.rmtree, self.work, ignore_errors=True)
                )
            finally:
                if self.on_close is not None:
                    self.on_close()


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
    if not _same_origin(request):
        raise HTTPException(403, CROSS_SITE)
    try:
        req = ExportRequest(
            start=from_, end=to, preset=preset, interval_s=interval, hatanaka=hatanaka, gzip=gzip
        )
    except ValidationError as exc:
        raise HTTPException(422, _issues(exc)) from exc
    if req.end - req.start > SYNC_MAX:
        raise HTTPException(422, SYNC_TOO_LONG)
    ctx = _ctx(request)
    state = request.app.state
    # Checked and set with no await in between, so two requests cannot both get through.
    if getattr(state, _SYNC_FLAG, False):
        raise HTTPException(409, SYNC_BUSY)
    if ctx.jobs is not None and ctx.jobs.active("export"):
        raise HTTPException(409, JOB_BUSY)
    setattr(state, _SYNC_FLAG, True)

    def release() -> None:
        setattr(state, _SYNC_FLAG, False)

    try:
        await _require_data(ctx, req)
        export_ctx = await _export_context(ctx)
        try:
            work = await asyncio.to_thread(_work_dir, ctx.settings.data_dir)
        except OSError as exc:  # a full card or an unwritable DATA_DIR: say so, not a bare 500
            raise HTTPException(409, _stage_message(exc)) from exc
    except BaseException:
        release()
        raise
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
        try:
            await asyncio.to_thread(_zip, out, result, archive)
        except OSError as exc:
            raise HTTPException(409, _stage_message(exc)) from exc
    except BaseException:
        try:
            await asyncio.shield(asyncio.to_thread(shutil.rmtree, work, ignore_errors=True))
        finally:
            release()
        raise
    return _ZipResponse(archive, work, on_close=release)
