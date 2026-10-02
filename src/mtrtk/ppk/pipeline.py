"""PPK: rover raw log + base raw log/RINEX -> rnx2rtkp -> track, summary, camera events.

Inputs. The rover is a session (its time range over this host's raw logs), a UTC window over
the same logs, or an uploaded file - raw receiver data or a RINEX observation file, told apart
by content. Raw data is converted as UBX whatever produced it (a ZED-F9P, or an INS such as the
SBG Ellipse-D whose GNSS port logs UBX RXM-RAWX/SFRBX), so nothing here depends on F9P-only
messages. The base is this host's own raw logs, a remote mtrtk base (its raw hours fetched with
`GET {url}/api/logs/window` and converted here), or an uploaded raw or RINEX file.

Times. Windows and sessions are UTC; convbin compares its window with the RINEX epoch stamps,
which are GPST, so a window goes onto GPST (`GPS_UTC_OFFSET`) before it reaches convbin. The
.pos file and the event times are GPST too (labelled UTC only for arithmetic; see `ppk.pos`).

Ephemerides. convbin drops navigation messages received before its `-ts`, so the base is
converted from an hour before the window (`BASE_LEAD`): that hour supplies the broadcast
ephemerides valid at the window's start. The rover is clipped to the window exactly - its
solutions must not run outside it - and the base's navigation file covers what it loses.

Base position, first found wins: a site of this host (`base_site`), coordinates given with the
request, the remote base's active site, or the `APPROX POSITION XYZ` of an uploaded base RINEX.
A header convbin wrote here is never used: convbin fills it with its own single-point estimate,
metres off, which would shift the whole track.

Everything goes into `out`: the RINEX pair of each side, `ppk.conf`, `track.pos` (+ `.stat`),
`track.csv/.geojson/.kml`, `events.csv/.geojson` when the rover log holds camera marks,
`rnx2rtkp.log`, and `summary.json` - which is also what `run_ppk` returns. A run first clears
those names (`OUTPUTS`) from `out`, so a reused directory never passes an earlier run's files
off as this one's - but never one of this run's own inputs: a RINEX input already sitting at its
output name is used where it is, and any other input under an output name is refused. The
spliced and fetched UBX copies go into a private scratch directory inside `out`, removed on
failure too; nothing else in `out` is touched. Every failure meant for the operator is a
`PpkError`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import shutil
import signal
import sys
import tempfile
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

from mtrtk.jobs import STAGING_PREFIX, JobContext, JobFn
from mtrtk.ppk.events import (
    events_csv,
    events_geojson,
    extract_time_marks,
    gpst_datetime,
    interpolate_events,
)
from mtrtk.ppk.pos import PosRecord, iter_pos, summarize, track_csv, track_geojson, track_kml
from mtrtk.ppk.rtkconf import parse_conf, render_conf_with_notes
from mtrtk.rawlog.index import LogFile, files_for_window
from mtrtk.rinex.convbin import ConvbinError, ConvbinOptions, RinexHeader, run_convbin
from mtrtk.rinex.export import EXPORT_RESERVE_SHARE, GPS_UTC_OFFSET, OBS_PER_RAW
from mtrtk.rinex.rinexhdr import obs_span, read_header, sniff_format
from mtrtk.rinex.splice import SpliceError, SpliceResult, splice_window
from mtrtk.rover.sessions import SessionsRepo
from mtrtk.store.db import Database
from mtrtk.store.repos import SitesRepo
from mtrtk.web.auth import session_token

log = logging.getLogger(__name__)
Progress = Callable[[float, str | None], Awaitable[None]]

MAX_WINDOW = timedelta(days=7)  # what one job may splice onto the card, like an export
BASE_LEAD_HOURS = 1  # base hour before the window: ephemerides valid at its start
BASE_LEAD = timedelta(hours=BASE_LEAD_HOURS)
BASE_PAD = timedelta(seconds=60)  # base epochs past the window's end, so the last rover epoch pairs
REMOTE_CHUNK = timedelta(hours=48)  # GET /api/logs/window answers at most 48 h per request
REMOTE_TIMEOUT = httpx.Timeout(120.0, connect=15.0)
WRITE_CHUNK = 1 << 20  # fetched bytes are written to the card a MiB at a time, off the loop
RNX2RTKP_TIMEOUT_S = 3600.0  # a day of 1 Hz kinematic takes minutes on a Pi; this is a leash
REAP_TIMEOUT_S = 5.0
ERROR_TAIL_CHARS = 400
FEW_FIXED_PCT = 50.0
# Hidden, never listed among the outputs, and a job's staging (`jobs.STAGING_PREFIX`): retention
# counts it as temporary instead of pruning raw hours for it, and a crash's leftover is removed
# at the next start.
SCRATCH_PREFIX = f"{STAGING_PREFIX}ppk-"
GB = 1e9
# Free-space preflight. Per byte of raw data: the spliced copy (1) and its RINEX (OBS_PER_RAW)
# on each side; per byte of the rover's raw data, the solution (track.pos.stat is the bulk of
# it: about 0.55 on the indoor fixture, more satellites outdoors) and the track files.
SOLUTION_PER_RAW = 1.0
POSTPROCESS_TIMEOUT_S = RNX2RTKP_TIMEOUT_S
LOG_KEEP_LINES = 2000  # rnx2rtkp lines (besides `processing`) kept for its log; the rest dropped
# Every file a run writes into `out`. Cleared at the start of a run, and the only names listed.
OUTPUTS = (
    "rover.rnx",
    "rover_MN.rnx",
    "base.rnx",
    "base_MN.rnx",
    "ppk.conf",
    "track.pos",
    "track.pos.stat",
    "track.csv",
    "track.geojson",
    "track.kml",
    "events.csv",
    "events.geojson",
    "rnx2rtkp.log",
    "track_events.pos",  # demo5 writes it next to track.pos
    "summary.json",
)
# The solution layout `parse_pos` reads and the GPST the camera marks are compared in: an
# override of these would turn ECEF into "lat/lon" or shift every event by the leap seconds.
PINNED_OUTPUT_OPTIONS = (
    "out-solformat",
    "out-timesys",
    "out-timeform",
    "out-degform",
    "out-outhead",
    "out-height",
    "out-fieldsep",
)
INPUT_PATH_FIELDS = ("path", "path_ubx", "path_obs", "path_nav")
INVALID_OPTION = "invalid option"  # rnx2rtkp's only word on a value it could not parse
UNKNOWN_RECEIVERS = ("", "unknown")


class PpkError(RuntimeError):
    """A PPK run could not be made; the message is for the operator."""


def _aware(name: str, t: datetime | None) -> datetime | None:
    if t is None:
        return None
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone (use Z or +00:00)")
    return t.astimezone(UTC)


class RoverSource(BaseModel):
    kind: Literal["session", "window", "upload"]
    session_id: int | None = None
    start: datetime | None = None
    end: datetime | None = None
    path: Path | None = None  # uploaded raw data (any UBX producer) or RINEX observation file
    station: str | None = None  # raw logs of this station; default: the host's own

    @model_validator(mode="after")
    def _check(self) -> RoverSource:
        self.start, self.end = _aware("start", self.start), _aware("end", self.end)
        if self.kind == "session" and self.session_id is None:
            raise ValueError("session_id required for a session rover")
        if self.kind == "window":
            if self.start is None or self.end is None:
                raise ValueError("start and end required for a window rover")
            if self.start >= self.end:
                raise ValueError("start must be before end")
        if self.kind == "upload" and self.path is None:
            raise ValueError("path required for an uploaded rover")
        return self


class BaseSource(BaseModel):
    kind: Literal["local", "remote", "upload"]
    url: str | None = None
    path_ubx: Path | None = None
    path_obs: Path | None = None
    path_nav: Path | None = None
    # The remote base's WEB_PASSWORD. Never written out: not in model_dump, not in a repr.
    password: str | None = Field(default=None, exclude=True, repr=False)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError("url must be an http:// or https:// address of an mtrtk base")
        return value

    @model_validator(mode="after")
    def _check(self) -> BaseSource:
        if self.kind == "remote" and not self.url:
            raise ValueError("url required for a remote base")
        if self.kind == "upload" and not (self.path_ubx or self.path_obs):
            raise ValueError("path_ubx or path_obs required for an uploaded base")
        return self


class PpkRequest(BaseModel):
    rover: RoverSource
    base: BaseSource
    base_site: str | None = None
    base_xyz: tuple[float, float, float] | None = None
    events: bool = True
    include_qzss: bool = False
    conf_overrides: dict[str, str] = Field(default_factory=dict)
    max_gap_s: float = Field(default=2.0, gt=0)

    @field_validator("conf_overrides")
    @classmethod
    def _overrides(cls, value: dict[str, str]) -> dict[str, str]:
        pinned = sorted(k for k in value if k.strip() in PINNED_OUTPUT_OPTIONS)
        if pinned:
            raise ValueError(
                f"{', '.join(pinned)} cannot be overridden: the track and the camera events are "
                "read from the solution in the layout mtrtk sets (llh, degrees, GPST hh:mm:ss)"
            )
        return value

    @model_validator(mode="after")
    def _check(self) -> PpkRequest:
        if self.base_site is not None and self.base_xyz is not None:
            raise ValueError("give a base site or base coordinates, not both")
        if self.base_xyz is not None and not all(math.isfinite(v) for v in self.base_xyz):
            raise ValueError("base coordinates must be finite ECEF metres")
        return self


def _default_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=REMOTE_TIMEOUT)


@dataclass
class PpkContext:
    root: Path  # DATA_DIR: where this host's raw logs are
    station_id: str
    country: str
    header: RinexHeader  # this host's own: for whichever side comes from its raw logs
    db: Database | None  # sites and sessions; None when the host has no database yet
    http: Callable[[], httpx.AsyncClient] = field(default=_default_http)
    rnx2rtkp: str = "rnx2rtkp"
    convbin: str = "convbin"
    # MIN_FREE_GB: a job leaves `EXPORT_RESERVE_SHARE` of it free, as an export does. 0 checks
    # only that the job fits.
    min_free_gb: float = 0.0


@dataclass
class _Side:
    obs: Path
    nav: Path
    ubx: Path | None = None  # raw data the rover's camera marks are read from
    info: dict[str, Any] = field(default_factory=dict)


def _gpst(t: datetime) -> datetime:
    """A UTC instant as the naive GPST wall-clock time `ConvbinOptions` takes."""
    return (t.astimezone(UTC) + GPS_UTC_OFFSET).replace(tzinfo=None)


def _utc_from_gpst(t: datetime) -> datetime:
    return t.replace(tzinfo=UTC) - GPS_UTC_OFFSET


def fetch_chunks(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """[start, end) as back-to-back requests of at most `REMOTE_CHUNK`, every boundary but the
    last on a whole hour: the endpoint answers whole hourly files, so hour-aligned pieces never
    fetch the same hour twice."""
    t = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    out: list[tuple[datetime, datetime]] = []
    while t < end:
        nxt = min(t + REMOTE_CHUNK, end)
        out.append((t, nxt))
        t = nxt
    return out


def _generic_header(ctx: PpkContext, marker: str, receiver: str) -> RinexHeader:
    return RinexHeader(
        marker_name=marker,
        observer=ctx.header.observer,
        agency=ctx.header.agency,
        receiver_version="unknown",
        receiver_type=receiver,
        comment="mtrtk ppk",
    )


def _same_file(a: Path, b: Path) -> bool:
    with contextlib.suppress(OSError):
        return os.path.samefile(a, b)
    return False


def _copy(src: Path, dst: Path) -> None:
    if not _same_file(src, dst):
        shutil.copyfile(src, dst)


def _check_rinex_input(path: Path, who: str) -> str:
    fmt = sniff_format(path)
    if fmt == "gzip":
        raise PpkError(f"{who} file {path.name} is gzip-compressed; decompress it first")
    if fmt == "crinex":
        raise PpkError(f"{who} file {path.name} is Hatanaka-compressed; run crx2rnx on it first")
    if fmt == "rinex-nav":
        raise PpkError(f"{who} file {path.name} is a RINEX navigation file, not observations")
    return fmt


async def _db(ctx: PpkContext, what: str) -> Database:
    if ctx.db is None:
        raise PpkError(f"{what}: this host has no database yet (DATA_DIR/mtrtk.db)")
    return ctx.db


async def _resolve_window(
    req: PpkRequest, ctx: PpkContext, warnings: list[str]
) -> tuple[datetime, datetime] | None:
    r = req.rover
    if r.kind == "window":
        assert r.start is not None and r.end is not None
        return r.start, r.end
    if r.kind == "session":
        assert r.session_id is not None
        session = await SessionsRepo(await _db(ctx, "a session rover")).get(r.session_id)
        if session is None:
            raise PpkError(f"session {r.session_id} not found")
        end = session.end_utc
        if end is None:
            end = datetime.now(UTC)
            warnings.append(f"session {r.session_id} is still open; processed up to {end:%H:%M}")
        start = session.start_utc.astimezone(UTC)
        if end <= start:
            raise PpkError(f"session {r.session_id} has no duration")
        return start, end.astimezone(UTC)
    return None


def _open_hours(spliced: SpliceResult, who: str) -> list[str]:
    hours = [f"{lf.hour_utc:%Y-%m-%d %H}:00" for lf in spliced.files if not lf.complete]
    if not hours:
        return []
    return [
        f"the {who} window includes an hour still being written ({', '.join(hours)} UTC); "
        "run again after it closes to use all of its data"
    ]


async def _splice(
    ctx: PpkContext,
    who: str,
    window: tuple[datetime, datetime],
    dest: Path,
    lead_hours: int,
    station: str,
    warnings: list[str],
) -> Path:
    try:
        spliced = await asyncio.to_thread(
            splice_window, ctx.root, window[0], window[1], dest, lead_hours, station
        )
    except SpliceError as exc:
        raise PpkError(f"{who}: {exc}") from exc
    warnings.extend(_open_hours(spliced, who))
    return spliced.path


def _rinex_in_place(src: Path, obs: Path, nav: Path, nav_src: Path | None = None) -> None:
    """A RINEX observation input copied to `obs`, with its navigation file at `nav`.

    A given `nav_src` is copied to `nav` (or used there when it already is `nav`) and never
    emptied. Without one, an `obs` used in place keeps the navigation file beside it under
    `nav`; otherwise `nav` starts empty (only non-empty navigation files reach rnx2rtkp)."""
    in_place = _same_file(src, obs)
    if not in_place:
        shutil.copyfile(src, obs)
    if nav_src is not None:
        _copy(nav_src, nav)
    elif not in_place or not nav.exists():
        nav.write_text("")


async def _rover_side(
    req: PpkRequest,
    ctx: PpkContext,
    out: Path,
    scratch: Path,
    window: tuple[datetime, datetime] | None,
    warnings: list[str],
) -> _Side:
    obs, nav = out / "rover.rnx", out / "rover_MN.rnx"
    r = req.rover
    if r.kind == "upload":
        assert r.path is not None
        if _check_rinex_input(r.path, "rover") == "rinex-obs":
            await asyncio.to_thread(_rinex_in_place, r.path, obs, nav)
            return _Side(obs, nav, None, {"format": "rinex"})
        opts = ConvbinOptions(header=_generic_header(ctx, "ROVER", "u-blox"))
        res = await run_convbin(r.path, obs, nav, opts, binary=ctx.convbin)
        return _Side(res.obs_path, res.nav_path, r.path, {"format": "raw"})
    assert window is not None
    station = r.station or ctx.station_id
    ubx = await _splice(ctx, "rover", window, scratch / "rover.ubx", 0, station, warnings)
    opts = ConvbinOptions(header=ctx.header, start=_gpst(window[0]), end=_gpst(window[1]))
    res = await run_convbin(ubx, obs, nav, opts, binary=ctx.convbin)
    return _Side(res.obs_path, res.nav_path, ubx, {"format": "raw", "station": station})


def _base_opts(header: RinexHeader, window: tuple[datetime, datetime]) -> ConvbinOptions:
    return ConvbinOptions(
        header=header, start=_gpst(window[0] - BASE_LEAD), end=_gpst(window[1] + BASE_PAD)
    )


async def _base_side(
    req: PpkRequest,
    ctx: PpkContext,
    out: Path,
    scratch: Path,
    window: tuple[datetime, datetime] | None,
    warnings: list[str],
    after: int = 0,
) -> _Side:
    """The base's observations in `out`. `after` is the bytes the run still writes once the
    base is in (the solution): a remote fetch keeps room for them as it writes."""
    b = req.base
    obs, nav = out / "base.rnx", out / "base_MN.rnx"
    if b.kind == "upload":
        raw = b.path_ubx
        if raw is not None and _check_rinex_input(raw, "base") == "rinex-obs":
            raw = None  # a RINEX observation file handed over as the raw one
        obs_src = b.path_obs or b.path_ubx
        if raw is None:
            assert obs_src is not None
            if _check_rinex_input(obs_src, "base") != "rinex-obs":
                raise PpkError(f"base file {obs_src.name} is not a RINEX observation file")
            await asyncio.to_thread(_rinex_in_place, obs_src, obs, nav, b.path_nav)
            return _Side(obs, nav, None, {"kind": "upload", "format": "rinex"})
        if b.path_nav is not None:
            raise PpkError(
                f"base file {raw.name} is raw data, which carries its own ephemerides; a "
                "navigation file goes with a RINEX base observation file"
            )
        opts = ConvbinOptions(header=_generic_header(ctx, "BASE", "u-blox"))
        res = await run_convbin(raw, obs, nav, opts, binary=ctx.convbin)
        return _Side(res.obs_path, res.nav_path, None, {"kind": "upload", "format": "raw"})
    assert window is not None
    if b.kind == "local":
        ubx = await _splice(
            ctx,
            "base",
            (window[0], window[1] + BASE_PAD),
            scratch / "base.ubx",
            BASE_LEAD_HOURS,
            ctx.station_id,
            warnings,
        )
        res = await run_convbin(ubx, obs, nav, _base_opts(ctx.header, window), binary=ctx.convbin)
        logs = await asyncio.to_thread(files_for_window, ctx.root, window[0], window[1] + BASE_PAD)
        here = [lf for lf in logs if lf.station_id == ctx.station_id]
        info = {"kind": "local", "logged_site": _one_site(_logged_sites(here), req, warnings)}
        return _Side(res.obs_path, res.nav_path, None, info)
    info = {"kind": "remote"}
    fetched = scratch / "base.ubx"
    remote_site = await _fetch_remote(req, ctx, fetched, window, info, warnings, after)
    header = _generic_header(ctx, "BASE", "u-blox ZED-F9P")  # an mtrtk base is an F9P
    res = await run_convbin(fetched, obs, nav, _base_opts(header, window), binary=ctx.convbin)
    side = _Side(res.obs_path, res.nav_path, None, info)
    if remote_site is not None:
        side.info["remote_site"] = remote_site
    return side


def _logged_sites(logs: list[LogFile]) -> list[str]:
    """The base sites a window's raw hours were logged at (their sidecars' `site`)."""
    return sorted({lf.site for lf in logs if lf.site})


def _one_site(sites: list[str], req: PpkRequest, warnings: list[str]) -> str | None:
    """The one site the base's hours were logged at; None when they name none.

    Hours logged at two sites mean the base moved inside the window: no one position is right
    for all of it. Refused unless the request gives the position itself."""
    if len(sites) <= 1:
        return sites[0] if sites else None
    named = ", ".join(sites)
    if req.base_site is None and req.base_xyz is None:
        raise PpkError(
            f"the base's raw logs in this window were logged at more than one site ({named}): "
            "the base moved inside the window; process each part on its own, or give the base "
            "position (a site or ECEF XYZ)"
        )
    warnings.append(
        f"the base's raw logs in this window were logged at more than one site ({named}); "
        "the base moved inside the window, and the one position given is used for all of it"
    )
    return None


def _remote_detail(resp: httpx.Response, body: bytes) -> str:
    try:
        detail = json.loads(body).get("detail")
    except (ValueError, AttributeError):
        detail = None
    text = str(detail) if detail else body.decode("utf-8", "replace")
    return f"{resp.status_code} {text[:ERROR_TAIL_CHARS]}".strip()


async def _fetch_remote(
    req: PpkRequest,
    ctx: PpkContext,
    dest: Path,
    window: tuple[datetime, datetime],
    info: dict[str, Any],
    warnings: list[str],
    after: int = 0,
) -> dict[str, Any] | None:
    """Fetch the remote base's raw hours covering the window (plus the lead hour) into `dest`;
    returns the site its hours were logged at (else its active site) when the request gave no
    base position. Its size is not known up front, so each write first checks that the card
    has room for it, its RINEX and `after` bytes more, above the reserve."""
    b = req.base
    assert b.url is not None
    url = b.url.rstrip("/")
    headers = {"Authorization": f"Bearer {session_token(b.password)}"} if b.password else {}
    start, end = window[0] - BASE_LEAD, window[1] + BASE_PAD
    total = 0
    missing: list[tuple[datetime, datetime]] = []
    site: dict[str, Any] | None = None
    try:
        async with ctx.http() as client:
            with dest.open("wb") as fh:
                for a, z in fetch_chunks(start, end):
                    params = {"from": a.isoformat(), "to": z.isoformat()}
                    async with client.stream(
                        "GET", f"{url}/api/logs/window", params=params, headers=headers
                    ) as resp:
                        if resp.status_code == 404:
                            await resp.aread()
                            missing.append((a, z))
                            continue
                        if resp.status_code != 200:
                            _remote_refused(url, resp, await resp.aread())
                        if _mixed_stations(resp):
                            raise PpkError(
                                f"the remote base {url} holds raw logs of more than one station "
                                "in this window (a moved base or a swapped card); its hours "
                                "cannot be one base"
                            )
                        buf = bytearray()
                        async for chunk in resp.aiter_bytes():
                            buf += chunk
                            if len(buf) >= WRITE_CHUNK:
                                await asyncio.to_thread(
                                    _write_checked, fh, bytes(buf), total, after, ctx
                                )
                                total += len(buf)
                                buf.clear()
                        if buf:
                            data = bytes(buf)
                            await asyncio.to_thread(_write_checked, fh, data, total, after, ctx)
                            total += len(buf)
            sites = await _remote_logged_sites(client, url, headers, window, warnings)
            info["logged_site"] = _one_site(sites, req, warnings)
            if req.base_site is None and req.base_xyz is None:
                site = await _remote_site(client, url, headers, warnings, info["logged_site"])
    except httpx.HTTPError as exc:
        raise PpkError(f"cannot fetch raw logs from the remote base {url}: {exc}") from exc
    if total == 0:
        raise PpkError(
            f"remote base {url} has no raw logs between {window[0].isoformat()} and "
            f"{window[1].isoformat()}"
        )
    for a, z in missing:
        if a < window[1] and z > window[0]:  # the lead hour alone is no loss
            warnings.append(
                f"the remote base has no raw logs between {max(a, window[0]).isoformat()} and "
                f"{min(z, window[1]).isoformat()}; the track has no solution there"
            )
    info["remote_bytes"] = total
    return site


def _mixed_stations(resp: httpx.Response) -> bool:
    """`GET /api/logs/window` names a download of several stations' hours `MIXED_...`."""
    disposition = resp.headers.get("content-disposition", "")
    return 'filename="MIXED_' in disposition


def _remote_refused(url: str, resp: httpx.Response, body: bytes) -> None:
    if resp.status_code == 401:
        raise PpkError(
            f"the remote base {url} asks for its web password (WEB_PASSWORD); give it with the "
            "request (CLI: --base-password or MTRTK_BASE_PASSWORD)"
        )
    raise PpkError(
        f"the remote base {url} refused the raw log download: {_remote_detail(resp, body)}"
    )


def _write_checked(fh: Any, data: bytes, written: int, after: int, ctx: PpkContext) -> None:
    """Write a piece of the remote base's raw data, if the card has room for it, for the RINEX
    of everything fetched so far, and for `after` bytes more."""
    need = len(data) + int((written + len(data)) * OBS_PER_RAW) + after
    _check_space(Path(fh.name).parent, need, ctx.min_free_gb, "the remote base's raw logs")
    fh.write(data)


async def _remote_logged_sites(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    window: tuple[datetime, datetime],
    warnings: list[str],
) -> list[str]:
    """The sites the remote base's hours in the window were logged at (`GET /api/logs`)."""
    resp = await client.get(f"{url}/api/logs", headers=headers)
    if resp.status_code != 200:
        warnings.append(
            f"could not read the remote base's log listing ({resp.status_code}), so not which "
            "site its hours were logged at"
        )
        return []
    sites: set[str] = set()
    try:
        for f in resp.json()["files"]:
            hour = datetime.fromisoformat(f["hour_utc"])
            inside = hour < window[1] + BASE_PAD and hour + timedelta(hours=1) > window[0]
            if inside and f.get("site"):  # `site` is absent from an older mtrtk's listing
                sites.add(str(f["site"]))
    except (ValueError, KeyError, TypeError):
        warnings.append("the remote base's log listing could not be read")
        return []
    return sorted(sites)


def _site_xyz(s: dict[str, Any]) -> dict[str, Any]:
    return {"name": str(s["name"]), "x": float(s["x"]), "y": float(s["y"]), "z": float(s["z"])}


async def _remote_site(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    warnings: list[str],
    logged: str | None = None,
) -> dict[str, Any] | None:
    """The site the remote's hours were logged at when it still has it, else its active one."""
    resp = await client.get(f"{url}/api/base/sites", headers=headers)
    if resp.status_code != 200:
        warnings.append(f"could not read the remote base's sites ({resp.status_code})")
        return None
    try:
        sites = [s for s in resp.json() if isinstance(s, dict)]
        active = next((s for s in sites if s.get("active")), None)
        active_name = str(active["name"]) if active is not None else None
        if logged is not None:
            match = next((s for s in sites if str(s.get("name")) == logged), None)
            if match is not None:
                if active_name != logged:
                    warnings.append(
                        f"the remote base's raw logs in this window were logged at site "
                        f"{logged}; the track is placed there, not on its active site "
                        f"{active_name or '(none)'}"
                    )
                return _site_xyz(match)
            warnings.append(
                f"the remote base's raw logs in this window were logged at site {logged}, "
                f"which it no longer has; its active site {active_name or '(none)'} is used"
            )
        return _site_xyz(active) if active is not None else None
    except (ValueError, KeyError, TypeError):
        warnings.append("the remote base's site list could not be read")
        return None


async def _base_xyz(
    req: PpkRequest, ctx: PpkContext, base: _Side, warnings: list[str]
) -> tuple[tuple[float, float, float], str]:
    logged = base.info.get("logged_site")
    if req.base_site:
        site = await SitesRepo(await _db(ctx, f"site {req.base_site!r}")).get(req.base_site)
        if site is None:
            raise PpkError(f"site {req.base_site!r} not found")
        if logged and logged != req.base_site:
            warnings.append(
                f"the base's raw logs in this window were logged at site {logged}; the track "
                f"is placed on {req.base_site}, as asked"
            )
        return (site.x, site.y, site.z), f"site:{req.base_site}"
    if req.base_xyz:
        if logged:
            warnings.append(
                f"the base's raw logs in this window were logged at site {logged}; the track "
                "is placed on the coordinates given, as asked"
            )
        return req.base_xyz, "request"
    remote = base.info.get("remote_site")
    if remote:
        return (remote["x"], remote["y"], remote["z"]), f"remote-site:{remote['name']}"
    if base.info.get("kind") == "local" and ctx.db is not None:
        # This host's own logs: the site they were logged at, else the one active now.
        repo = SitesRepo(ctx.db)
        active = await repo.active()
        if logged:
            at = await repo.get(logged)
            if at is None:
                raise PpkError(
                    f"the base's raw logs in this window were logged at site {logged!r}, which "
                    "this host no longer has; give the base position (a site or ECEF XYZ)"
                )
            if active is None or active.name != logged:
                warnings.append(
                    f"the base's raw logs in this window were logged at site {logged}; the "
                    f"track is placed there, not on the active site "
                    f"{active.name if active else '(none)'}"
                )
            return (at.x, at.y, at.z), f"site:{logged}"
        if active is not None:
            return (active.x, active.y, active.z), f"site:{active.name}"
    if base.info.get("format") == "rinex":
        hdr = await asyncio.to_thread(read_header, base.obs)
        if hdr.approx_xyz:
            return hdr.approx_xyz, "rinex-header"
    raise PpkError(
        "base position unknown: give a site name, ECEF coordinates, or a base RINEX with "
        "APPROX POSITION XYZ (or activate a site on the remote base)"
    )


async def _kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.communicate(), REAP_TIMEOUT_S)


def _rnx2rtkp_argv(binary: str, out: Path, navs: list[Path]) -> list[str]:
    """The argv, run in `out`; navigation files only when they hold anything. Clears an
    earlier attempt's `track.pos`, which would otherwise pass for this run's."""
    exe = os.path.abspath(binary) if os.sep in binary else binary  # found from `out` too
    names = [n.name for n in navs if n.exists() and n.stat().st_size > 0]
    (out / "track.pos").unlink(missing_ok=True)
    return [exe, "-k", "ppk.conf", "-o", "track.pos", "rover.rnx", "base.rnx", *names]


async def _run_rnx2rtkp(
    ctx: PpkContext, out: Path, navs: list[Path], *, append: bool = False
) -> Path:
    """rnx2rtkp in `out` with bare file names: it copies its input paths into the .pos header,
    and a host's directory layout has no business in a file handed to a client."""
    binary = ctx.rnx2rtkp
    cmd = await asyncio.to_thread(_rnx2rtkp_argv, binary, out, navs)
    pos = out / "track.pos"
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=out,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise PpkError(f"cannot run rnx2rtkp ({binary}): {exc}") from exc
    kept: deque[str] = deque(maxlen=LOG_KEEP_LINES)
    bad: list[str] = []
    assert proc.stdout is not None
    try:
        await asyncio.wait_for(
            asyncio.gather(_read_output(proc.stdout, kept, bad), proc.wait()), RNX2RTKP_TIMEOUT_S
        )
    except TimeoutError as exc:
        await _kill_and_reap(proc)
        raise PpkError(f"rnx2rtkp timed out after {RNX2RTKP_TIMEOUT_S:.0f}s") from exc
    except BaseException:
        await _kill_and_reap(proc)
        raise
    lines = list(kept)
    shown = " ".join([binary, *cmd[1:]])
    with (out / "rnx2rtkp.log").open("a" if append else "w") as fh:
        fh.write(shown + "\n" + "".join(f"{ln}\n" for ln in lines))
    tail = " ".join(lines)[-ERROR_TAIL_CHARS:]
    if proc.returncode != 0:
        raise PpkError(f"rnx2rtkp failed (exit {proc.returncode}): {tail}")
    if bad:
        raise PpkError(f"rnx2rtkp did not accept the options: {'; '.join(bad)[:ERROR_TAIL_CHARS]}")
    if not pos.exists():
        raise PpkError(f"rnx2rtkp wrote no track.pos: {tail}")
    return pos


async def _read_output(stream: asyncio.StreamReader, kept: deque[str], bad: list[str]) -> None:
    """rnx2rtkp's output as it comes: its `processing` progress lines (one per epoch, megabytes
    on a long run) are dropped, the last `LOG_KEEP_LINES` others kept, `invalid option` noted."""
    pending = b""
    while chunk := await stream.read(1 << 16):
        pending += chunk
        *done, pending = pending.replace(b"\r", b"\n").split(b"\n")
        for raw in done:
            _keep_line(raw, kept, bad)
        if len(pending) > 1 << 16:  # a runaway line: keep its end
            pending = pending[-(1 << 16) :]
    _keep_line(pending, kept, bad)


def _keep_line(raw: bytes, kept: deque[str], bad: list[str]) -> None:
    line = raw.decode("utf-8", "replace")
    if not line.strip() or line.startswith("processing"):
        return
    kept.append(line)
    if INVALID_OPTION in line and len(bad) < 50:
        bad.append(line.strip())


def _postprocess(
    out: Path,
    records: list[PosRecord],
    req: PpkRequest,
    rover_ubx: Path | None,
    window: tuple[datetime, datetime] | None,
    warnings: list[str],
    zero_baseline: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse the solution and write the track and event files (blocking: `mtrtk.ppk.post` runs
    it in a child process, see `_postprocess_in_child`)."""
    records = sorted(records, key=lambda r: r.time)  # a backward-only solution is newest first
    summary = summarize(records, gap_s=req.max_gap_s)
    (out / "track.csv").write_text(track_csv(records))
    (out / "track.geojson").write_text(json.dumps(track_geojson(records, gap_s=req.max_gap_s)))
    (out / "track.kml").write_text(track_kml(records, gap_s=req.max_gap_s))
    events: dict[str, Any] = {"total": 0, "ok": 0, "gap_too_large": 0, "no_neighbours": 0}
    if req.events and rover_ubx is not None:
        marks = extract_time_marks(rover_ubx)
        if window is not None:
            # A spliced rover log is whole hours: keep the marks of the processed window.
            lo, hi = _gpst(window[0]), _gpst(window[1])
            inside = [
                m for m in marks if lo <= gpst_datetime(m.week, m.tow_s).replace(tzinfo=None) < hi
            ]
            if dropped := len(marks) - len(inside):
                warnings.append(f"{dropped} camera mark(s) outside the window were left out")
            marks = inside
        if marks:
            # The raw max_gap_s, not the rate-adapted gap of the track: a pulse placed across
            # a longer step is a guess at the path (docs/ppk.md, Geotagging photos).
            fixes = interpolate_events(marks, records, req.max_gap_s)
            (out / "events.csv").write_text(events_csv(fixes))
            (out / "events.geojson").write_text(json.dumps(events_geojson(fixes)))
            events = {"total": len(fixes)}
            for k in ("ok", "gap_too_large", "no_neighbours"):
                events[k] = sum(e.status == k for e in fixes)
            if missing := len(fixes) - events["ok"]:
                warnings.append(f"{missing} of {len(fixes)} camera mark(s) could not be placed")
    if summary.fixed_pct < FEW_FIXED_PCT and not zero_baseline:  # that one is explained already
        warnings.append(
            f"only {summary.fixed_pct:.0f}% of epochs are RTK fixed; check baseline length, "
            "sky view and that the base and rover windows overlap"
        )
    return summary.to_json(), events


def _read_pos(path: Path) -> list[PosRecord]:
    with path.open(errors="replace") as fh:
        return list(iter_pos(fh))


def _count_epochs(path: Path) -> int:
    """Solution epochs in a .pos file, read line by line (nothing held)."""
    with path.open(errors="replace") as fh:
        return sum(1 for _ in iter_pos(fh))


async def _postprocess_in_child(
    scratch: Path,
    out: Path,
    req: PpkRequest,
    rover_ubx: Path | None,
    window: tuple[datetime, datetime] | None,
    zero_baseline: bool,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """`_postprocess` in a child process (`python -m mtrtk.ppk.post`).

    A long track's records and output strings take hundreds of MB, and the parsing holds the
    GIL: in the daemon's own process that could get the caster or NTRIP client OOM-killed, or
    starve the event loop that forwards RTCM. In a child, the worst case fails this job.
    """
    args = {
        "out": str(out),
        "request": req.model_dump(mode="json"),
        "rover_ubx": str(rover_ubx) if rover_ubx is not None else None,
        "window": [t.isoformat() for t in window] if window else None,
        "zero_baseline": zero_baseline,
    }
    args_path = await asyncio.to_thread(_write_args, scratch, args)
    result_path = args_path.with_suffix(".out")
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "mtrtk.ppk.post",
            str(args_path),
            str(result_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), POSTPROCESS_TIMEOUT_S)
        except TimeoutError as exc:
            await _kill_and_reap(proc)
            raise PpkError(
                f"writing the track timed out after {POSTPROCESS_TIMEOUT_S:.0f}s"
            ) from exc
        except BaseException:
            await _kill_and_reap(proc)
            raise
        if proc.returncode != 0:
            tail = err.decode("utf-8", "replace").strip()[-ERROR_TAIL_CHARS:]
            if proc.returncode is not None and proc.returncode < 0:
                why = f"was killed (signal {-proc.returncode}; out of memory?)"
            else:
                why = f"failed (exit {proc.returncode})"
            raise PpkError(f"writing the track {why}: {tail}")
        result = json.loads(await asyncio.to_thread(result_path.read_text))
        return result["summary"], result["events"], list(result["warnings"])
    finally:
        await asyncio.to_thread(args_path.unlink, missing_ok=True)
        await asyncio.to_thread(result_path.unlink, missing_ok=True)


def _write_args(scratch: Path, args: dict[str, Any]) -> Path:
    fd, name = tempfile.mkstemp(prefix="post-", suffix=".json", dir=scratch)
    with os.fdopen(fd, "w") as fh:
        json.dump(args, fh)
    return Path(name)


def _with_forward(conf: str) -> str:
    """`conf` with its solution type set to forward-only, saying why."""
    lines = [
        f"{'pos1-soltype':<20}=forward" if ln.split("=", 1)[0].strip() == "pos1-soltype" else ln
        for ln in conf.splitlines()
    ]
    note = "# combined solution was empty: rerun forward-only"
    return "\n".join([lines[0], note, *lines[1:]]) + "\n"


def _glonass_ar(receivers: dict[str, str], warnings: list[str]) -> str:
    """`on` only between u-blox receivers: GLONASS integer ambiguities need matching
    inter-channel biases, which a receiver of another make on either side does not have."""
    others = {
        side: rt.strip()
        for side, rt in receivers.items()
        if rt.strip().lower() not in UNKNOWN_RECEIVERS and "u-blox" not in rt.lower()
    }
    if not others:
        return "on"
    named = ", ".join(f"{side} receiver is {rt!r}" for side, rt in others.items())
    warnings.append(f"{named}; GLONASS ambiguity resolution set to autocal")
    return "autocal"


def _inputs(req: PpkRequest) -> list[Path]:
    r, b = req.rover, req.base
    return [p for p in (r.path, b.path_ubx, b.path_obs, b.path_nav) if p is not None]


def _in_place(req: PpkRequest, out: Path) -> set[str]:
    """Output names in `out` that are this run's own RINEX inputs, used where they are."""
    r, b = req.rover, req.base
    keep: set[str] = set()

    def rinex_at(name: str, src: Path | None) -> bool:
        return src is not None and _same_file(out / name, src) and sniff_format(src) == "rinex-obs"

    if rinex_at("rover.rnx", r.path):
        keep |= {"rover.rnx", "rover_MN.rnx"}  # with the navigation file beside it, if any
    if rinex_at("base.rnx", b.path_obs or b.path_ubx):
        keep.add("base.rnx")
        if b.path_nav is None:
            keep.add("base_MN.rnx")
    if b.path_nav is not None and _same_file(out / "base_MN.rnx", b.path_nav):
        keep.add("base_MN.rnx")
    return keep


def _clear_outputs(req: PpkRequest, out: Path) -> None:
    """Remove an earlier run's outputs from `out`, refusing to overwrite one of the inputs."""
    keep = _in_place(req, out)
    inputs = _inputs(req)
    for name in OUTPUTS:
        path = out / name
        if name in keep or not (path.exists() or path.is_symlink()):
            continue
        if any(_same_file(path, src) for src in inputs):
            raise PpkError(
                f"{name} in {out} is an input of this run and would be overwritten by an "
                "output of that name; move it, or use another output directory"
            )
        path.unlink()


def _check_window(window: tuple[datetime, datetime]) -> None:
    if window[1] - window[0] > MAX_WINDOW:
        raise PpkError(f"the window is {window[1] - window[0]}; process at most 7 days at a time")


def _disk_usage(path: Path) -> Any:
    return shutil.disk_usage(path)


def _check_space(where: Path, need: int, min_free_gb: float, what: str) -> None:
    """Refuse a step that would eat into the space live logging needs.

    As for an export (`rinex.export._check_space`): `MIN_FREE_GB` itself is not the limit,
    since retention keeps a full card right at it; a job may use the margin, never the last
    `EXPORT_RESERVE_SHARE` of it. Retention does not count the job's scratch (it is staging),
    so without this a full card would fill to the last byte under live raw logging.
    """
    target = where
    while not target.exists() and target != target.parent:
        target = target.parent
    free = int(_disk_usage(target).free)
    reserve = int(max(0.0, min_free_gb) * EXPORT_RESERVE_SHARE * GB)
    if free - need < reserve:
        raise PpkError(
            f"not enough free space for {what}: it needs about {need / GB:.2f} GB while it "
            f"runs and {free / GB:.2f} GB is free where it is written, of which "
            f"{reserve / GB:.2f} GB stays free for live raw logging; process a shorter window "
            "or free some space (delete old jobs)"
        )


def _raw_bytes(ctx: PpkContext, start: datetime, end: datetime, station: str) -> int:
    return sum(
        lf.bytes for lf in files_for_window(ctx.root, start, end) if lf.station_id == station
    )


def _file_bytes(*paths: Path | None) -> int:
    return sum(p.stat().st_size for p in paths if p is not None and p.is_file())


def _rover_need(
    req: PpkRequest, ctx: PpkContext, window: tuple[datetime, datetime] | None
) -> tuple[int, int]:
    """(bytes the rover side writes, the rover's raw bytes)."""
    r = req.rover
    if r.kind == "upload":
        size = _file_bytes(r.path)
        return int(size * OBS_PER_RAW), size
    assert window is not None
    raw = _raw_bytes(ctx, window[0], window[1], r.station or ctx.station_id)
    return int(raw * (1 + OBS_PER_RAW)), raw


def _base_need(req: PpkRequest, ctx: PpkContext, window: tuple[datetime, datetime] | None) -> int:
    """Bytes the base side writes; a remote base is checked as it arrives (`_write_checked`)."""
    b = req.base
    if b.kind == "upload":
        return int(_file_bytes(b.path_ubx, b.path_obs, b.path_nav) * OBS_PER_RAW)
    if b.kind == "local" and window is not None:
        raw = _raw_bytes(ctx, window[0] - BASE_LEAD, window[1] + BASE_PAD, ctx.station_id)
        return int(raw * (1 + OBS_PER_RAW))
    return 0


def _check_tools(ctx: PpkContext) -> None:
    for name, binary in (("convbin", ctx.convbin), ("rnx2rtkp", ctx.rnx2rtkp)):
        if not shutil.which(binary):  # an executable on PATH, or at a path
            raise PpkError(f"{name} not found ({binary}); install RTKLIB or use the Docker image")


async def run_ppk(
    req: PpkRequest, ctx: PpkContext, out: Path, progress: Progress | None = None
) -> dict[str, Any]:
    """Post-process `req` into `out`; returns the `summary.json` content."""

    async def report(p: float, msg: str) -> None:
        if progress:
            await progress(p, msg)

    _check_tools(ctx)
    try:
        await asyncio.to_thread(out.mkdir, parents=True, exist_ok=True)
    except OSError as exc:
        raise PpkError(f"cannot create {out}: {exc.strerror or exc}") from exc
    warnings: list[str] = []
    try:
        scratch = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix=SCRATCH_PREFIX, dir=out))
    except OSError as exc:
        raise PpkError(f"cannot write into {out}: {exc.strerror or exc}") from exc
    try:
        return await _run(req, ctx, out, scratch, report, warnings)
    except ConvbinError as exc:
        raise PpkError(str(exc)) from exc
    except (
        OSError
    ) as exc:  # an input that cannot be read as much as an output that cannot be written
        where = f" ({exc.filename})" if exc.filename else ""
        raise PpkError(f"PPK file error: {exc.strerror or exc}{where}") from exc
    finally:
        await asyncio.to_thread(shutil.rmtree, scratch, True)


async def _run(
    req: PpkRequest,
    ctx: PpkContext,
    out: Path,
    scratch: Path,
    report: Callable[[float, str], Awaitable[None]],
    warnings: list[str],
) -> dict[str, Any]:
    window = await _resolve_window(req, ctx, warnings)
    if window is not None:
        _check_window(window)
    await asyncio.to_thread(_clear_outputs, req, out)
    r = req.rover
    zero_baseline = (
        r.kind != "upload" and req.base.kind == "local" and r.station in (None, ctx.station_id)
    )
    if zero_baseline:
        warnings.append(
            "the rover and the base are both this host's raw logs of the same station "
            f"({ctx.station_id}): a zero baseline against itself, not a survey"
        )
    rover_need, rover_raw = await asyncio.to_thread(_rover_need, req, ctx, window)
    solution = int(rover_raw * SOLUTION_PER_RAW)
    base_need = await asyncio.to_thread(_base_need, req, ctx, window)
    await asyncio.to_thread(
        _check_space, out, rover_need + base_need + solution, ctx.min_free_gb, "this PPK job"
    )
    await report(0.05, "converting rover observations")
    rover = await _rover_side(req, ctx, out, scratch, window, warnings)
    if req.events and rover.ubx is None:
        warnings.append(
            "camera events need the rover's raw data (TIM-TM2); a RINEX rover has none, so "
            "no events were placed"
        )
    if window is None and req.base.kind != "upload":
        span = await asyncio.to_thread(obs_span, rover.obs)
        if span is None:
            raise PpkError("the rover file has no epochs to take the base window from")
        window = (_utc_from_gpst(span[0]), _utc_from_gpst(span[1]) + timedelta(seconds=1))
        _check_window(window)
    await report(0.3, "preparing base observations")
    # The rover side is on disk now (and a rover upload's window is known): check again.
    base_need = await asyncio.to_thread(_base_need, req, ctx, window)
    await asyncio.to_thread(
        _check_space, out, base_need + solution, ctx.min_free_gb, "this PPK job"
    )
    base = await _base_side(req, ctx, out, scratch, window, warnings, after=solution)
    xyz, xyz_source = await _base_xyz(req, ctx, base, warnings)
    if xyz_source == "rinex-header":
        warnings.append(
            "the base position is the base RINEX's APPROX POSITION XYZ; often only an "
            "approximation, and the whole track moves with it - give a surveyed position if "
            "there is one"
        )
    base_hdr = await asyncio.to_thread(read_header, base.obs)
    rover_hdr = await asyncio.to_thread(read_header, rover.obs)
    receivers = {"rover": rover_hdr.receiver_type, "base": base_hdr.receiver_type}
    glonass_ar = _glonass_ar(receivers, warnings)
    try:
        conf, notes = await asyncio.to_thread(
            render_conf_with_notes,
            xyz,
            overrides=req.conf_overrides,
            glonass_ar=glonass_ar,
            include_qzss=req.include_qzss,
            binary=ctx.rnx2rtkp,
        )
    except ValueError as exc:
        raise PpkError(str(exc)) from exc
    warnings.extend(notes)
    await asyncio.to_thread((out / "ppk.conf").write_text, conf)
    await report(0.45, "running rnx2rtkp")
    navs = [rover.nav, base.nav]
    if not await asyncio.to_thread(lambda: any(_has_data(n) for n in navs)):
        raise PpkError(
            "no navigation data: neither the rover nor the base brought ephemerides; give the "
            "base RINEX navigation file with its observations (CLI: --base-nav)"
        )
    epochs = await asyncio.to_thread(_count_epochs, await _run_rnx2rtkp(ctx, out, navs))
    soltype = parse_conf(conf).get("pos1-soltype")
    if not epochs and soltype == "combined":
        # The backward pass can fail outright on a short or degenerate span (stock 2.4.3 on a
        # minute of data does), and a combined solution needs both passes.
        conf = _with_forward(conf)
        await asyncio.to_thread((out / "ppk.conf").write_text, conf)
        epochs = await asyncio.to_thread(
            _count_epochs, await _run_rnx2rtkp(ctx, out, navs, append=True)
        )
        if epochs:
            soltype = "forward"
            warnings.append(
                "the combined (forward + backward) solution was empty; this track is the "
                "forward-only solution"
            )
    if not epochs:
        raise PpkError(
            "rnx2rtkp produced no solution epochs; check rnx2rtkp.log (do the rover and base "
            "windows overlap? do they share signals?)"
        )
    await report(0.8, "writing track")
    summary, events, notes = await _postprocess_in_child(
        scratch, out, req, rover.ubx, window, zero_baseline
    )
    warnings.extend(notes)
    await asyncio.to_thread(shutil.rmtree, scratch, True)
    files = await asyncio.to_thread(_list_files, out)
    base_info = {k: v for k, v in base.info.items() if k != "remote_site"}
    result: dict[str, Any] = {
        "summary": summary,
        "events": events,
        "warnings": warnings,
        "inputs": {
            "rover": {**_described(req.rover), **rover.info},
            "base": {**_described(req.base), **base_info},
            "base_xyz": list(xyz),
            "base_xyz_source": xyz_source,
            "base_receiver": base_hdr.receiver_type or None,
            "glonass_ar": glonass_ar,
            "soltype": soltype,
            "window": [window[0].isoformat(), window[1].isoformat()] if window else None,
            "conf_overrides": req.conf_overrides,
        },
        "files": files,
    }
    text = json.dumps(result, indent=2, default=str)
    await asyncio.to_thread((out / "summary.json").write_text, text)
    await report(1.0, "done")
    return result


def _has_data(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def _described(source: BaseModel) -> dict[str, Any]:
    """A source for summary.json: an uploaded file by its name, never the host path it had."""
    out: dict[str, Any] = source.model_dump(mode="json")
    for key in INPUT_PATH_FIELDS:
        if out.get(key):
            out[key] = Path(out[key]).name
    return out


def _list_files(out: Path) -> list[dict[str, Any]]:
    """This run's outputs (`OUTPUTS`, less summary.json itself), not whatever else `out` holds."""
    return [
        {"name": name, "bytes": (out / name).stat().st_size}
        for name in sorted(OUTPUTS)
        if name != "summary.json" and (out / name).is_file()
    ]


def make_ppk_job(req: PpkRequest, ctx: PpkContext) -> JobFn:
    """A `JobRunner` job (kind `ppk`) that writes into its own result directory and returns
    the summary as the job's result."""

    async def job(jctx: JobContext) -> dict[str, Any]:
        return await run_ppk(req, ctx, jctx.dir, progress=jctx.progress)

    return job
