"""PPK jobs: uploads, defaults, submission. Results come through /api/jobs.

`POST /api/ppk/upload` takes one raw (UBX) or RINEX file as a multipart form (`kind`, `file`)
and keeps it under `DATA_DIR/uploads/<upload_id>/<name>`; `POST /api/ppk` names uploads by
their id. The form is parsed here as it streams in, the file part written straight to the card
the raw logs live on: a day of rover data is gigabytes, and the framework's own form parser
would first spool it into the system temporary directory - RAM on a Pi whose /tmp is a tmpfs -
and then copy it. Uploads older than `UPLOAD_TTL` are removed at the next upload.

`POST /api/ppk` queues a `JobRunner` job (kind `ppk`) running `mtrtk.ppk.pipeline.run_ppk` into
the job's own directory; its result is the run's `summary.json`, its files are served by
`/api/jobs/{id}/files/{name}`. What can be checked before queueing is checked here and refused
with 404/422 - an upload that is not there, a source missing what it needs, a navigation file
next to a raw base, coordinates that are not ECEF, a malformed rnx2rtkp override, a window
longer than `MAX_WINDOW` or one no raw log covers - so the operator
learns it at once rather than from a failed job. The remote base's password is used by the job
and never stored: not in the job's params, not in its result.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from python_multipart.multipart import MultipartParseError, MultipartParser, parse_options_header

from mtrtk.ppk.pipeline import (
    MAX_WINDOW,
    BaseSource,
    PpkContext,
    PpkRequest,
    RoverSource,
    make_ppk_job,
)
from mtrtk.ppk.rtkconf import (
    BASE_OPTIONS,
    DEMO5_OPTIONS,
    detect_build,
    render_conf,
    rnx2rtkp_available,
)
from mtrtk.rawlog.index import files_for_window
from mtrtk.rinex.convbin import convbin_available
from mtrtk.rinex.export import EXPORT_RESERVE_SHARE, header_from_settings
from mtrtk.rinex.rinexhdr import sniff_format
from mtrtk.store.repos import SitesRepo
from mtrtk.web.context import AppContext

if TYPE_CHECKING:  # a TypedDict python-multipart declares for type checkers only
    from python_multipart.multipart import MultipartCallbacks

router = APIRouter(prefix="/api/ppk", tags=["ppk"])

MAX_UPLOAD = 2 * 1024**3
PPK_UPLOAD_PATH = "/api/ppk/upload"
UPLOAD_TOO_LARGE = "upload larger than 2 GB: split the log, or run `mtrtk ppk` on the host"
UPLOADS_DIR = "uploads"  # under DATA_DIR, next to the raw logs: never the system temp directory
UPLOAD_TTL_S = 7 * 24 * 3600  # an upload this old is removed at the next upload
STALE_PART_S = 24 * 3600  # a folder holding only a partial file this old was a dropped upload
PART_PREFIX = ".part-"  # a file still arriving; hidden, never handed to a job
HEAD_BYTES = 4096
FIELD_MAX = 1024  # `kind` is a word; any other form field is not ours either
UPLOAD_ID_RE = re.compile(r"[0-9a-f]{12}")
NAME_MAX = 128
_UNSAFE_NAME = re.compile(r"[^\w.+()\- ]")
NO_RUNNER = "this daemon has no job runner"
FREE_CHECK_BYTES = 64 * 1024**2  # an upload re-checks the card's free space this often as it grows
# Bytes still to arrive per upload in flight (by folder name): what each free-space check must
# leave room for besides its own upload, so two large uploads cannot both pass against the same
# free space. Read and written under `_IN_FLIGHT_LOCK` from worker threads.
_IN_FLIGHT: dict[str, int] = {}
_IN_FLIGHT_LOCK = threading.Lock()
# An mtrtk base's web port unless its operator moved it; the form shows the guess, editable.
BASE_WEB_PORT = 8080
# A point on the equator: what overrides are checked against when no base position is given yet.
_ANY_BASE = (6378137.0, 0.0, 0.0)

UPLOAD_ERRORS: dict[int | str, dict[str, Any]] = {
    409: {"description": "not enough free space on the card for the upload"},
    413: {"description": "the file is larger than 2 GB"},
    422: {
        "description": "not a multipart form with kind and file, or a file neither UBX nor RINEX"
    },
}
SUBMIT_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "an upload id that is not there, or no raw logs cover the rover window"},
    409: {"description": "no job runner, or the runner is shutting down"},
    422: {"description": "a source missing what it needs, or a value the pipeline refuses"},
}
# The form `POST /api/ppk/upload` reads, declared by hand: the route parses its own body.
UPLOAD_FORM: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["kind", "file"],
                    "properties": {
                        "kind": {"type": "string", "enum": ["rover", "base"]},
                        "file": {"type": "string", "format": "binary"},
                    },
                }
            }
        },
    }
}


class RoverBody(BaseModel):
    kind: Literal["session", "window", "upload"]
    session_id: int | None = None
    start: datetime | None = None
    end: datetime | None = None
    upload_id: str | None = None


class BaseBody(BaseModel):
    kind: Literal["local", "remote", "upload"]
    url: str | None = None
    upload_id: str | None = None
    nav_upload_id: str | None = None
    # The remote base's WEB_PASSWORD, for this job only: excluded from every dump of the body.
    password: str | None = Field(default=None, exclude=True, repr=False)


class PpkSubmit(BaseModel):
    rover: RoverBody
    base: BaseBody
    base_site: str | None = None
    base_xyz: tuple[float, float, float] | None = None
    events: bool = True
    include_qzss: bool = False
    conf_overrides: dict[str, str] = {}


def _ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


def _uploads(ctx: AppContext) -> Path:
    return ctx.settings.data_dir / UPLOADS_DIR


def _issue(loc: list[str | int], msg: str, kind: str = "value_error") -> dict[str, Any]:
    return {"loc": loc, "msg": msg, "type": kind}


def _refuse(loc: list[str | int], msg: str) -> HTTPException:
    return HTTPException(422, [_issue(loc, msg)])


def _issues(exc: ValidationError, prefix: list[str | int]) -> list[dict[str, Any]]:
    """The app's own 422 shape, `[{loc, msg, type}]`, with nothing of the input: a model's
    `input` would carry the whole source, the remote base's password included."""
    return [
        _issue([*prefix, *err["loc"]], str(err["msg"]).removeprefix("Value error, "), err["type"])
        for err in exc.errors()
    ]


# ------------------------------------------------------------------------------- defaults


def _base_url_guess(ntrip_url: str | None, port: int = BASE_WEB_PORT) -> str | None:
    """The base's web UI from the rover's caster URL: same host, an mtrtk base's web port.
    This host's own `WEB_PORT` says nothing about the base's."""
    if not ntrip_url:
        return None
    url = ntrip_url if "://" in ntrip_url else f"ntrip://{ntrip_url}"
    try:
        host = urlparse(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def _defaults(binary: str = "rnx2rtkp") -> dict[str, Any]:
    demo5 = detect_build(binary) == "demo5"  # reads the executable: off the event loop
    conf = dict(BASE_OPTIONS)
    if demo5:
        conf.update(DEMO5_OPTIONS)
    return {
        "rnx2rtkp": rnx2rtkp_available(binary),
        "convbin": convbin_available(),
        "demo5": demo5,
        "conf": conf,
    }


@router.get("/defaults")
async def defaults(request: Request) -> dict[str, Any]:
    """What this host can run, the option file a job starts from, and the base's web address
    guessed from `NTRIP_URL` (`http://<caster host>:8080`), for the form to prefill."""
    s = _ctx(request).settings
    out = await asyncio.to_thread(_defaults)
    out["ntrip_base_url"] = _base_url_guess(s.ntrip_url)
    out["max_upload_bytes"] = MAX_UPLOAD
    return out


# -------------------------------------------------------------------------------- uploads


class _FormRefused(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _safe_name(filename: str, kind: str) -> str:
    """The uploaded file's own name, one path component of plain characters."""
    name = re.split(r"[/\\]", filename)[-1].strip()
    name = _UNSAFE_NAME.sub("_", name).lstrip(".")
    if not name:
        return f"{kind}.ubx"
    if len(name) > NAME_MAX:
        stem, dot, ext = name.rpartition(".")
        ext = ext[:16] if dot else ""
        name = (stem if dot else name)[: NAME_MAX - len(ext) - 1] + (f".{ext}" if ext else "")
    return name


class _UploadForm:
    """One multipart body, parsed as it arrives: small fields in memory, the `file` part
    written into `folder` (as a hidden partial file) a network chunk at a time."""

    def __init__(
        self, folder: Path, limit: int, min_free_gb: float = 0.0, declared: int = 0
    ) -> None:
        self.folder = folder
        self.limit = limit
        self.min_free_gb = min_free_gb
        self.declared = declared
        self.ended = False  # the parser met the closing boundary
        self._checked = 0  # bytes written when free space was last checked
        self.fields: dict[str, bytearray] = {}
        self.filename: str | None = None
        self.size = 0
        self.head = bytearray()
        self.part: Path | None = None
        self._fh: IO[bytes] | None = None
        self._pending: list[bytes] = []
        self._header_name = b""
        self._header_value = b""
        self._disposition = b""
        self._field: str | None = None
        self._in_file = False

    # python-multipart callbacks: synchronous, so file data is only queued here
    def on_part_begin(self) -> None:
        self._disposition = b""
        self._field = None
        self._in_file = False

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def on_header_end(self) -> None:
        if self._header_name.lower() == b"content-disposition":
            self._disposition = self._header_value
        self._header_name = b""
        self._header_value = b""

    def on_headers_finished(self) -> None:
        _, options = parse_options_header(self._disposition)
        name = options.get(b"name", b"").decode("utf-8", "replace")
        if b"filename" in options:
            if name != "file" or self.filename is not None:
                raise _FormRefused(422, "send exactly one file, as the form field 'file'")
            self.filename = options[b"filename"].decode("utf-8", "replace")
            self._in_file = True
        else:
            self._field = name
            self.fields.setdefault(name, bytearray())

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if self._in_file:
            self.size += len(chunk)
            if self.size > self.limit:
                raise _FormRefused(413, UPLOAD_TOO_LARGE)
            if len(self.head) < HEAD_BYTES:
                self.head += chunk[: HEAD_BYTES - len(self.head)]
            self._pending.append(bytes(chunk))
        elif self._field is not None:
            value = self.fields[self._field]
            if len(value) + len(chunk) > FIELD_MAX:
                raise _FormRefused(422, f"form field {self._field!r} is too long")
            value += chunk

    def on_part_end(self) -> None:
        self._in_file = False

    def on_end(self) -> None:
        self.ended = True

    def callbacks(self) -> MultipartCallbacks:
        return {
            "on_part_begin": self.on_part_begin,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_end": self.on_end,
        }

    async def flush(self) -> None:
        """Write what the parser queued, off the event loop."""
        if not self._pending:
            return
        data = b"".join(self._pending)
        self._pending.clear()
        if self._fh is None:
            self.part = self.folder / f"{PART_PREFIX}{uuid.uuid4().hex[:8]}"
            self._fh = await asyncio.to_thread(self.part.open, "wb")
        await asyncio.to_thread(self._fh.write, data)
        if self.size - self._checked >= FREE_CHECK_BYTES:
            self._checked = self.size
            await asyncio.to_thread(self._check_free)

    def _check_free(self) -> None:
        """Still room for the rest of this upload and of the others, above `MIN_FREE_GB`?
        An upload sent without a Content-Length is only checked here, as it grows."""
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT[self.folder.name] = max(0, self.declared - self.size)
            free = shutil.disk_usage(self.folder).free
            if free - sum(_IN_FLIGHT.values()) < _reserve(self.min_free_gb):
                raise _FormRefused(409, _no_room(self.size, free, self.min_free_gb))

    async def close(self) -> None:
        if self._fh is not None:
            fh, self._fh = self._fh, None
            await asyncio.to_thread(fh.close)

    def field(self, name: str) -> str | None:
        value = self.fields.get(name)
        return None if value is None else value.decode("utf-8", "replace").strip()


def _prune(uploads: Path, now: float) -> None:
    """Remove uploads past `UPLOAD_TTL_S`, and folders a dropped upload left half written."""
    if not uploads.is_dir():
        return
    for folder in uploads.iterdir():
        try:
            age = now - folder.stat().st_mtime
            if not folder.is_dir():
                continue
            done = any(not p.name.startswith(".") for p in folder.iterdir())
        except OSError:
            continue
        if age > UPLOAD_TTL_S or (not done and age > STALE_PART_S):
            shutil.rmtree(folder, ignore_errors=True)


def _reserve(min_free_gb: float) -> float:
    """Bytes an upload must leave free: the share of `MIN_FREE_GB` an export leaves too.

    `MIN_FREE_GB` itself is not the limit: retention keeps a full card right at it, so that
    rule would refuse every upload once the card has filled. An upload may use the margin
    (retention then prunes the oldest unkept raw hours back to the floor), never the last
    `EXPORT_RESERVE_SHARE` of it, which live raw logging needs while retention catches up.
    """
    return max(0.0, min_free_gb) * EXPORT_RESERVE_SHARE * 1e9


def _no_room(need: int, free: int, min_free_gb: float) -> str:
    return (
        f"not enough free space for a {need / 1e9:.2f} GB upload: {free / 1e9:.2f} GB free, "
        f"uploads in progress included, and {_reserve(min_free_gb) / 1e9:.2f} GB "
        f"({EXPORT_RESERVE_SHARE:.0%} of MIN_FREE_GB) must stay free for the raw logs"
    )


def _make_folder(uploads: Path, need: int, min_free_gb: float) -> Path:
    """A fresh upload folder, after pruning and checking the card can take `need` bytes - and
    what the uploads already in flight still have to write - and keep `_reserve` free. The
    folder's `need` stays reserved until `_release`."""
    uploads.mkdir(parents=True, exist_ok=True)
    _prune(uploads, time.time())
    with _IN_FLIGHT_LOCK:
        free = shutil.disk_usage(uploads).free
        if free - sum(_IN_FLIGHT.values()) - need < _reserve(min_free_gb):
            raise _FormRefused(409, _no_room(need, free, min_free_gb))
        folder = uploads / uuid.uuid4().hex[:12]
        folder.mkdir()
        _IN_FLIGHT[folder.name] = need
    return folder


def _release(folder: Path | None) -> None:
    if folder is not None:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.pop(folder.name, None)


def _detect(path: Path, head: bytes) -> tuple[str, str | None]:
    """`("ubx", None)` or `("rinex", "obs" | "nav")`; `_FormRefused` for anything else."""
    fmt = sniff_format(path)
    if fmt == "gzip":
        raise _FormRefused(422, "the file is gzip-compressed; decompress it and upload it again")
    if fmt == "crinex":
        raise _FormRefused(422, "the file is Hatanaka-compressed; run crx2rnx on it first")
    if fmt in ("rinex-obs", "rinex-nav"):
        return "rinex", "obs" if fmt == "rinex-obs" else "nav"
    if b"\xb5\x62" in head:
        return "ubx", None
    raise _FormRefused(422, "file is neither UBX nor RINEX")


def _declared_length(request: Request) -> int:
    try:
        return max(0, int(request.headers.get("content-length", "0")))
    except ValueError:
        return 0


@router.post("/upload", responses=UPLOAD_ERRORS, openapi_extra=UPLOAD_FORM)
async def upload(request: Request) -> dict[str, Any]:
    """Keep one rover or base file for a PPK job: raw UBX (any producer) or RINEX."""
    ctx = _ctx(request)
    _, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if (
        not request.headers.get("content-type", "").startswith("multipart/form-data")
        or not boundary
    ):
        raise HTTPException(422, "send a multipart/form-data body with the fields kind and file")
    folder: Path | None = None
    form: _UploadForm | None = None
    declared = _declared_length(request)
    min_free = ctx.settings.min_free_gb
    try:
        folder = await asyncio.to_thread(_make_folder, _uploads(ctx), declared, min_free)
        form = _UploadForm(folder, MAX_UPLOAD, min_free, declared)
        parser = MultipartParser(boundary, form.callbacks())
        async for chunk in request.stream():
            parser.write(chunk)
            await form.flush()
        parser.finalize()  # python-multipart checks nothing here: `form.ended` does
        await form.flush()
        await form.close()
        if not form.ended:
            raise _FormRefused(
                422,
                "malformed multipart body: it ends before its closing boundary (was the upload "
                "cut short?)",
            )
        kind = form.field("kind")
        if kind not in ("rover", "base"):
            raise _FormRefused(422, "form field 'kind' must be rover or base")
        if form.filename is None or form.part is None:
            raise _FormRefused(422, "form field 'file' is missing or empty")
        name = _safe_name(form.filename, kind)
        target = folder / name
        await asyncio.to_thread(os.replace, form.part, target)
        detected, rinex = await asyncio.to_thread(_detect, target, bytes(form.head))
    except _FormRefused as exc:
        await _discard(form, folder)
        raise HTTPException(exc.status, exc.detail) from None
    except MultipartParseError as exc:
        await _discard(form, folder)
        raise HTTPException(422, f"malformed multipart body: {exc}") from None
    except OSError as exc:
        await _discard(form, folder)
        raise HTTPException(409, f"cannot store the upload: {exc.strerror or exc}") from None
    except BaseException:  # a client that hung up, a cancelled request: nothing is kept
        await asyncio.shield(_discard(form, folder))
        raise
    finally:
        _release(folder)
    return {
        "upload_id": folder.name,
        "name": name,
        "bytes": form.size,
        "detected": detected,
        "rinex": rinex,
        "kind": kind,
    }


async def _discard(form: _UploadForm | None, folder: Path | None) -> None:
    if form is not None:
        with contextlib.suppress(OSError):
            await form.close()
    if folder is not None:
        await asyncio.to_thread(shutil.rmtree, folder, True)


def _upload_file(uploads: Path, upload_id: str) -> Path | None:
    folder = uploads / upload_id
    if not UPLOAD_ID_RE.fullmatch(upload_id) or not folder.is_dir():
        return None
    files = sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith("."))
    return files[0] if files else None


async def _resolve(ctx: AppContext, upload_id: str) -> tuple[Path, str]:
    """The uploaded file and what it is (`sniff_format`), or 404."""
    path = await asyncio.to_thread(_upload_file, _uploads(ctx), upload_id)
    if path is None:
        raise HTTPException(404, f"upload {upload_id} not found (uploads are kept 7 days)")
    return path, await asyncio.to_thread(sniff_format, path)


# --------------------------------------------------------------------------------- submit


def _covered(root: Path, station: str, start: datetime, end: datetime) -> bool:
    return any(lf.station_id == station for lf in files_for_window(root, start, end))


async def _rover(ctx: AppContext, body: RoverBody) -> RoverSource:
    path: Path | None = None
    if body.kind == "upload":
        if not body.upload_id:
            raise _refuse(
                ["body", "rover", "upload_id"], "upload_id required for an uploaded rover"
            )
        path, fmt = await _resolve(ctx, body.upload_id)
        if fmt == "rinex-nav":
            raise _refuse(
                ["body", "rover", "upload_id"],
                f"{path.name} is a RINEX navigation file; the rover needs observations",
            )
    try:
        return RoverSource(
            kind=body.kind,
            session_id=body.session_id,
            start=body.start,
            end=body.end,
            path=path,
        )
    except ValidationError as exc:
        raise HTTPException(422, _issues(exc, ["body", "rover"])) from None


async def _base(ctx: AppContext, body: BaseBody) -> BaseSource:
    kw: dict[str, Any] = {"kind": body.kind, "url": body.url, "password": body.password}
    if body.kind != "upload" and body.nav_upload_id:
        raise _refuse(
            ["body", "base", "nav_upload_id"], "a navigation file goes with an uploaded base"
        )
    if body.kind == "upload":
        if not body.upload_id:
            raise _refuse(["body", "base", "upload_id"], "upload_id required for an uploaded base")
        path, fmt = await _resolve(ctx, body.upload_id)
        if fmt == "rinex-nav":
            raise _refuse(
                ["body", "base", "upload_id"],
                f"{path.name} is a RINEX navigation file: give it as nav_upload_id, next to the "
                "base's observation file",
            )
        kw["path_obs" if fmt == "rinex-obs" else "path_ubx"] = path
        if body.nav_upload_id and fmt != "rinex-obs":
            raise _refuse(
                ["body", "base", "nav_upload_id"],
                f"{path.name} is raw data, which carries its own ephemerides; a navigation file "
                "goes with a RINEX base observation file",
            )
        if body.nav_upload_id:
            nav, nav_fmt = await _resolve(ctx, body.nav_upload_id)
            if nav_fmt != "rinex-nav":
                raise _refuse(
                    ["body", "base", "nav_upload_id"],
                    f"{nav.name} is not a RINEX navigation file",
                )
            kw["path_nav"] = nav
    try:
        return BaseSource(**kw)
    except ValidationError as exc:
        raise HTTPException(422, _issues(exc, ["body", "base"])) from None


def _check_conf(body: PpkSubmit) -> None:
    """The rnx2rtkp option file this request would make, refused now rather than in the job:
    overrides that are not option lines, coordinates that are not ECEF metres."""
    try:
        render_conf(body.base_xyz or _ANY_BASE, overrides=body.conf_overrides)
    except ValueError as exc:
        loc: list[str | int] = [
            "body",
            "base_xyz" if "base position" in str(exc) else "conf_overrides",
        ]
        raise _refuse(loc, str(exc)) from None


@router.post("", responses=SUBMIT_ERRORS)
async def submit(body: PpkSubmit, request: Request) -> dict[str, Any]:
    """Queue a PPK job and answer with its row; progress arrives on the `jobs` topic."""
    ctx = _ctx(request)
    if ctx.jobs is None:
        raise HTTPException(409, NO_RUNNER)
    rover = await _rover(ctx, body.rover)
    if rover.kind == "window":
        assert rover.start is not None and rover.end is not None
        if rover.end - rover.start > MAX_WINDOW:
            raise _refuse(
                ["body", "rover", "end"],
                f"the window is {rover.end - rover.start}; process at most "
                f"{MAX_WINDOW.days} days at a time",
            )
    base = await _base(ctx, body.base)
    base_site = body.base_site
    if base.kind == "local" and base_site is None and body.base_xyz is None:
        # This host's own logs as the base: its active site is where they were logged.
        active = await SitesRepo(ctx.db).active()
        base_site = active.name if active is not None else None
    try:
        req = PpkRequest(
            rover=rover,
            base=base,
            base_site=base_site,
            base_xyz=body.base_xyz,
            events=body.events,
            include_qzss=body.include_qzss,
            conf_overrides=body.conf_overrides,
        )
    except ValidationError as exc:
        raise HTTPException(422, _issues(exc, ["body"])) from None
    await asyncio.to_thread(_check_conf, body)
    station = ctx.settings.station_id
    if rover.kind == "window":
        assert rover.start is not None and rover.end is not None
        root = ctx.settings.data_dir
        if not await asyncio.to_thread(_covered, root, station, rover.start, rover.end):
            raise HTTPException(
                404,
                f"no raw logs for station {station} between {rover.start.isoformat()} and "
                f"{rover.end.isoformat()}; check GET /api/logs/availability for the hours on disk",
            )
    site = await SitesRepo(ctx.db).active()
    pctx = PpkContext(
        root=ctx.settings.data_dir,
        station_id=station,
        country=ctx.settings.country,
        header=header_from_settings(ctx.settings, ctx.store.state, site),
        db=ctx.db,
    )
    params = body.model_dump(mode="json")  # `password` is excluded from the dump
    try:
        job = await ctx.jobs.submit("ppk", params, make_ppk_job(req, pctx))
    except RuntimeError as exc:  # the runner is shutting down
        raise HTTPException(409, str(exc)) from exc
    return job.model_dump(mode="json")
