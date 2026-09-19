"""Raw UBX logs: list, availability, download, the keep flag, delete and a window export.

The filesystem is the truth here - `list_logs` walks the tree every request, so a card moved
between machines, a file recovered by `recover_incomplete` or one deleted by hand is visible at
once. The `log_files` table is a mirror of it, kept in step by `LogIndexMirror` below.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from mtrtk.rawlog.index import (
    LogFile,
    files_for_window,
    hour_availability,
    list_logs,
    parse_log_name,
)
from mtrtk.rawlog.retention import GB
from mtrtk.rawlog.writer import Sidecar, log_path, sidecar_path
from mtrtk.store.repos import LogFilesRepo
from mtrtk.web.context import AppContext

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/logs", tags=["logs"])

CHUNK = 1 << 20  # what one read of a window export pulls off the card
WARN_INTERVAL_S = 60.0  # the mirror's complaints: the first, then at most one a minute

NOT_A_LOG = "not a raw log file name (expected SSSS_YYYYMMDD_HH.ubx)"
OPEN_FILE_DETAIL = (
    "that hour is being written right now; it is deleted once the writer has rotated out of it"
)
NEWEST_DETAIL = (
    "this is the newest hour and is most likely the file being written; "
    "pass ?force=1 to delete it anyway"
)
KEEP_DETAIL = (
    'this file is marked keep; clear the mark with PATCH {"keep": false} before deleting it'
)

# Declared so the schema Phase 4 generates its client from carries them; FastAPI only infers
# the 2xx and the validation 422 on its own.
NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"description": "not a raw log file name, or no such hour on this daemon"},
}
KEEP_ERRORS: dict[int | str, dict[str, Any]] = {**NOT_FOUND}
DELETE_ERRORS: dict[int | str, dict[str, Any]] = {
    **NOT_FOUND,
    409: {"description": "the file is open, is the newest hour (?force=1), or is marked keep"},
}
WINDOW_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "no raw log overlaps that window"},
    422: {"description": "from/to are not ISO-8601 instants with a timezone, or are out of order"},
}
RANGE_ERRORS: dict[int | str, dict[str, Any]] = {422: WINDOW_ERRORS[422]}


class KeepBody(BaseModel):
    keep: bool


class LogIndexMirror:
    """Keeps the `log_files` table in step with the raw log tree.

    Owned by the app's lifespan (`app.state.log_index`): one subscription for the life of the
    process, never one per request. Nothing else writes the table - the writer publishes
    `rawlog.rotated` / `rawlog.closed` and retention publishes `rawlog.pruned`, and this turns
    each of those into a row. `GET /api/logs` does not read the table (the filesystem is the
    truth); the history and job endpoints join against it.
    """

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._bus = ctx.bus
        self._sub = ctx.bus.subscribe(
            "rawlog.rotated", "rawlog.closed", "rawlog.pruned", maxsize=64
        )
        self._task: asyncio.Task[None] | None = None
        self._last_warn = 0.0
        self.applied = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="web-log-index")

    async def _run(self) -> None:
        async for topic, item in self._sub:
            if not isinstance(item, (str, Path)):  # the three topics all carry a Path
                continue
            try:
                await self._apply(topic, Path(item))
            except Exception:  # one bad row must never end the mirror
                self._warn("could not mirror %s for %s", topic, item, exc_info=True)
            else:
                self.applied += 1

    async def _apply(self, topic: str, path: Path) -> None:
        repo = LogFilesRepo(self._ctx.db)
        if topic == "rawlog.pruned":
            await repo.delete(path)
            return
        try:
            sidecar = Sidecar.load(sidecar_path(path))
        except (OSError, TypeError, ValueError):  # json.JSONDecodeError is a ValueError
            # The file itself is still usable and still listed; only its row is skipped.
            self._warn("no usable sidecar for %s; not mirrored into log_files", path)
            return
        await repo.upsert(path, sidecar)

    def _warn(self, message: str, *args: Any, exc_info: bool = False) -> None:
        """A tree of unreadable sidecars must not fill the journal: one line a minute."""
        now = time.monotonic()
        if self._last_warn and now - self._last_warn < WARN_INTERVAL_S:
            return
        self._last_warn = now
        log.warning(message, *args, exc_info=exc_info)

    def close(self) -> None:
        # `bus.unsubscribe`, never `self._sub.close()` alone: closing only pushes the sentinel
        # that stops the reader and leaves the subscription registered on the bus.
        self._bus.unsubscribe(self._sub)

    async def aclose(self) -> None:
        self.close()
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


def _open_path(ctx: AppContext) -> Path | None:
    """The hour the raw logger has open, when this daemon runs one (Task 10 sets `rawlog`)."""
    writer = getattr(ctx.daemon, "rawlog", None)
    path: Path | None = getattr(writer, "current_path", None)
    return path


def _free_gb(root: Path) -> float:
    """Free space where the logs live - the same reading retention prunes against."""
    return shutil.disk_usage(root if root.exists() else root.parent).free / GB


async def _scan(root: Path) -> list[LogFile]:
    """The whole tree, oldest first. A full card is thousands of stats, so it goes to a thread:
    on the event loop it would stall the caster and the state loop for the whole walk."""
    return await asyncio.to_thread(list_logs, root)


def _logfile_json(lf: LogFile, open_path: Path | None) -> dict[str, Any]:
    return {
        "name": lf.path.name,
        "hour_utc": lf.hour_utc.isoformat(),
        "bytes": lf.bytes,
        "complete": lf.complete,
        "keep": lf.keep,
        "open": lf.path == open_path,
        "msg_counts": lf.msg_counts,
        "start_utc": lf.start_utc,
        "end_utc": lf.end_utc,
    }


def _parse_dt(value: str, label: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, f"{label} must be ISO-8601") from exc
    if dt.tzinfo is None:
        # Raw logs are named in receiver UTC; a naive instant would silently mean local time.
        raise HTTPException(422, f"{label} must include a timezone (use Z or +00:00)")
    return dt


def _window(from_: str, to: str) -> tuple[datetime, datetime]:
    start, end = _parse_dt(from_, "from"), _parse_dt(to, "to")
    if start >= end:
        raise HTTPException(422, "from must be before to")
    return start, end


async def _resolve(ctx: AppContext, name: str) -> tuple[LogFile, list[LogFile]]:
    """Turn a name from the URL into a log this daemon owns, or raise 404.

    The name never becomes part of a path: `parse_log_name` accepts only `SSSS_YYYYMMDD_HH.ubx`,
    and the path is *rebuilt* from the station and the hour it yields, under the data directory.
    Anything with a separator or a `..` in it fails that grammar, and the rebuilt path is checked
    against `<data_dir>/ubx` as well - belt and braces, so a later change to the grammar cannot
    turn into a traversal. The listing is returned with it: the delete guard needs it, and one
    walk per request is enough.
    """
    if "/" in name or "\\" in name or name != Path(name).name:
        raise HTTPException(404, NOT_A_LOG)
    parsed = parse_log_name(Path(name))
    if parsed is None:
        raise HTTPException(404, NOT_A_LOG)
    station, hour = parsed
    root = Path(ctx.settings.data_dir)
    path = log_path(root, station, hour)
    if not path.resolve().is_relative_to((root / "ubx").resolve()):
        raise HTTPException(404, NOT_A_LOG)
    files = await _scan(root)
    lf = next((x for x in files if x.path == path), None)
    if lf is None:
        raise HTTPException(404, f"no raw log named {name!r} on this daemon")
    return lf, files


@router.get("")
async def list_files(request: Request) -> dict[str, Any]:
    """Every hour on the card, oldest first, with what retention has to work with."""
    ctx = _ctx(request)
    root = Path(ctx.settings.data_dir)
    files = await _scan(root)
    open_path = _open_path(ctx)
    return {
        "files": [_logfile_json(lf, open_path) for lf in files],
        "total_bytes": sum(lf.bytes for lf in files),
        "hours": len(files),
        "disk_free_gb": await asyncio.to_thread(_free_gb, root),
        "min_free_gb": ctx.settings.min_free_gb,
    }


@router.get("/availability", responses=RANGE_ERRORS)
async def availability(
    request: Request, from_: str = Query(alias="from"), to: str = Query()
) -> list[dict[str, Any]]:
    """One slot per hour in [from, to), so the UI can draw the gaps as well as the coverage."""
    root = Path(_ctx(request).settings.data_dir)
    start, end = _window(from_, to)
    slots = await asyncio.to_thread(hour_availability, root, start, end)
    return [
        {
            "hour_utc": s.hour_utc.isoformat(),
            "available": s.file is not None,
            "bytes": s.file.bytes if s.file else 0,
            "complete": s.file.complete if s.file else False,
        }
        for s in slots
    ]


@router.get("/window", responses=WINDOW_ERRORS)
async def window(
    request: Request, from_: str = Query(alias="from"), to: str = Query()
) -> StreamingResponse:
    """The hourly files overlapping [from, to), concatenated into one download.

    Whole hours, never a slice: a UBX stream cut mid-message is not a UBX stream, and every
    post-processor takes more data over a broken file.
    """
    root = Path(_ctx(request).settings.data_dir)
    start, end = _window(from_, to)
    files = await asyncio.to_thread(files_for_window, root, start, end)
    if not files:
        raise HTTPException(404, "no raw logs in that window")

    async def body() -> AsyncIterator[bytes]:
        for lf in files:
            try:
                fh = lf.path.open("rb")
            except OSError:  # pruned or moved between the listing and the read
                log.warning("skipping %s in a window export: %s", lf.path, "unreadable")
                continue
            try:
                # Off the loop: a stalling SD card would otherwise freeze the caster with it.
                while chunk := await asyncio.to_thread(fh.read, CHUNK):
                    yield chunk
            finally:
                fh.close()

    name = f"{files[0].station_id}_{files[0].hour_utc:%Y%m%d%H}_{files[-1].hour_utc:%Y%m%d%H}.ubx"
    return StreamingResponse(
        body(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/{name}", responses=NOT_FOUND)
async def download(name: str, request: Request) -> FileResponse:
    """One hour, as it is on the card."""
    lf, _ = await _resolve(_ctx(request), name)
    return FileResponse(lf.path, media_type="application/octet-stream", filename=name)


@router.patch("/{name}", responses=KEEP_ERRORS)
async def set_keep(name: str, body: KeepBody, request: Request) -> dict[str, Any]:
    """Mark an hour so retention will not prune it (or clear the mark).

    Retention reads `keep` off the sidecar, so that file is what has to change; the row is a
    mirror. When the hour named is the one being written, the writer sets it: it holds that
    sidecar in memory and its next dump would otherwise overwrite anything written behind it.
    """
    ctx = _ctx(request)
    lf, _ = await _resolve(ctx, name)
    writer = getattr(ctx.daemon, "rawlog", None)
    if writer is not None and getattr(writer, "current_path", None) == lf.path:
        writer.set_keep(body.keep)
    repo = LogFilesRepo(ctx.db)
    await _ensure_row(repo, lf.path)
    await repo.set_keep(lf.path, body.keep)
    return _logfile_json(replace(lf, keep=body.keep), _open_path(ctx))


async def _ensure_row(repo: LogFilesRepo, path: Path) -> None:
    """`LogFilesRepo.set_keep` is an UPDATE: an hour the mirror never saw - one written before
    this daemon started, or recovered into the tree - has no row for it to land on, and the flag
    would live on the sidecar alone. The upsert leaves `keep` to `set_keep` on an existing row."""
    try:
        sidecar = Sidecar.load(sidecar_path(path))
    except (OSError, TypeError, ValueError):  # json.JSONDecodeError is a ValueError
        return  # `set_keep` logs the unusable sidecar; no row can be built without one
    await repo.upsert(path, sidecar)


@router.delete("/{name}", responses=DELETE_ERRORS)
async def delete_file(name: str, request: Request, force: int = Query(0)) -> dict[str, bool]:
    """Delete one hour: the file, its sidecar and its row.

    Three refusals. The hour the writer has open goes nowhere, `force` or not - unlinking it
    leaves the writer filling an inode nothing can reach, and the operator would watch the free
    space keep falling. Without a running writer to ask, the newest hour is assumed to be that
    file, and `?force=1` says otherwise. A `keep` mark is a deliberate decision and is cleared
    deliberately, with `PATCH {"keep": false}`, rather than overridden by a query flag.
    """
    ctx = _ctx(request)
    lf, files = await _resolve(ctx, name)
    open_path = _open_path(ctx)
    if open_path is not None and lf.path == open_path:
        raise HTTPException(409, OPEN_FILE_DETAIL)
    if open_path is None and lf.path == files[-1].path and not force:
        raise HTTPException(409, NEWEST_DETAIL)
    if lf.keep:
        raise HTTPException(409, KEEP_DETAIL)
    await asyncio.to_thread(_unlink, lf)
    await LogFilesRepo(ctx.db).delete(lf.path)
    return {"ok": True}


def _unlink(lf: LogFile) -> None:
    lf.path.unlink(missing_ok=True)
    lf.sidecar_path.unlink(missing_ok=True)
