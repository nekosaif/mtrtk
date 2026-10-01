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
`rnx2rtkp.log`, and `summary.json` - which is also what `run_ppk` returns. The spliced and
fetched UBX copies are scratch and removed, on failure too. Every failure meant for the operator
is a `PpkError`.
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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

from mtrtk.jobs import JobContext, JobFn
from mtrtk.ppk.events import (
    events_csv,
    events_geojson,
    extract_time_marks,
    gpst_datetime,
    interpolate_events,
)
from mtrtk.ppk.pos import PosRecord, parse_pos, summarize, track_csv, track_geojson, track_kml
from mtrtk.ppk.rtkconf import parse_conf, render_conf_with_notes
from mtrtk.rinex.convbin import ConvbinError, ConvbinOptions, RinexHeader, run_convbin
from mtrtk.rinex.export import GPS_UTC_OFFSET
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
SCRATCH = ("rover.ubx", "base.ubx")
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


async def _rover_side(
    req: PpkRequest,
    ctx: PpkContext,
    out: Path,
    window: tuple[datetime, datetime] | None,
    warnings: list[str],
) -> _Side:
    obs, nav = out / "rover.rnx", out / "rover_MN.rnx"
    r = req.rover
    if r.kind == "upload":
        assert r.path is not None
        if _check_rinex_input(r.path, "rover") == "rinex-obs":
            await asyncio.to_thread(_copy, r.path, obs)
            await asyncio.to_thread(nav.write_text, "")
            return _Side(obs, nav, None, {"format": "rinex"})
        opts = ConvbinOptions(header=_generic_header(ctx, "ROVER", "u-blox"))
        res = await run_convbin(r.path, obs, nav, opts, binary=ctx.convbin)
        return _Side(res.obs_path, res.nav_path, r.path, {"format": "raw"})
    assert window is not None
    station = r.station or ctx.station_id
    ubx = await _splice(ctx, "rover", window, out / "rover.ubx", 0, station, warnings)
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
    window: tuple[datetime, datetime] | None,
    warnings: list[str],
) -> _Side:
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
            await asyncio.to_thread(_copy, obs_src, obs)
            if b.path_nav is not None:
                await asyncio.to_thread(_copy, b.path_nav, nav)
            else:
                await asyncio.to_thread(nav.write_text, "")
            return _Side(obs, nav, None, {"kind": "upload", "format": "rinex"})
        opts = ConvbinOptions(header=_generic_header(ctx, "BASE", "u-blox"))
        res = await run_convbin(raw, obs, nav, opts, binary=ctx.convbin)
        return _Side(res.obs_path, res.nav_path, None, {"kind": "upload", "format": "raw"})
    assert window is not None
    if b.kind == "local":
        ubx = await _splice(
            ctx,
            "base",
            (window[0], window[1] + BASE_PAD),
            out / "base.ubx",
            BASE_LEAD_HOURS,
            ctx.station_id,
            warnings,
        )
        res = await run_convbin(ubx, obs, nav, _base_opts(ctx.header, window), binary=ctx.convbin)
        return _Side(res.obs_path, res.nav_path, None, {"kind": "local"})
    info: dict[str, Any] = {"kind": "remote"}
    remote_site = await _fetch_remote(req, ctx, out / "base.ubx", window, info, warnings)
    header = _generic_header(ctx, "BASE", "u-blox ZED-F9P")  # an mtrtk base is an F9P
    res = await run_convbin(
        out / "base.ubx", obs, nav, _base_opts(header, window), binary=ctx.convbin
    )
    side = _Side(res.obs_path, res.nav_path, None, info)
    if remote_site is not None:
        side.info["remote_site"] = remote_site
    return side


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
) -> dict[str, Any] | None:
    """Fetch the remote base's raw hours covering the window (plus the lead hour) into `dest`;
    returns its active site when the request gave no base position."""
    b = req.base
    assert b.url is not None
    url = b.url.rstrip("/")
    headers = {"Authorization": f"Bearer {session_token(b.password)}"} if b.password else {}
    start, end = window[0] - BASE_LEAD, window[1] + BASE_PAD
    total = 0
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
                            continue
                        if resp.status_code != 200:
                            _remote_refused(url, resp, await resp.aread())
                        buf = bytearray()
                        async for chunk in resp.aiter_bytes():
                            buf += chunk
                            if len(buf) >= WRITE_CHUNK:
                                await asyncio.to_thread(fh.write, bytes(buf))
                                total += len(buf)
                                buf.clear()
                        if buf:
                            await asyncio.to_thread(fh.write, bytes(buf))
                            total += len(buf)
            if req.base_site is None and req.base_xyz is None:
                site = await _remote_site(client, url, headers, warnings)
    except httpx.HTTPError as exc:
        raise PpkError(f"cannot fetch raw logs from the remote base {url}: {exc}") from exc
    if total == 0:
        raise PpkError(
            f"remote base {url} has no raw logs between {window[0].isoformat()} and "
            f"{window[1].isoformat()}"
        )
    info["remote_bytes"] = total
    return site


def _remote_refused(url: str, resp: httpx.Response, body: bytes) -> None:
    if resp.status_code == 401:
        raise PpkError(
            f"the remote base {url} asks for its web password (WEB_PASSWORD); give it with the "
            "request (CLI: --base-password or MTRTK_BASE_PASSWORD)"
        )
    raise PpkError(
        f"the remote base {url} refused the raw log download: {_remote_detail(resp, body)}"
    )


async def _remote_site(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], warnings: list[str]
) -> dict[str, Any] | None:
    resp = await client.get(f"{url}/api/base/sites", headers=headers)
    if resp.status_code != 200:
        warnings.append(f"could not read the remote base's sites ({resp.status_code})")
        return None
    try:
        sites = resp.json()
        active = next((s for s in sites if isinstance(s, dict) and s.get("active")), None)
        if active is None:
            return None
        return {
            "name": str(active["name"]),
            "x": float(active["x"]),
            "y": float(active["y"]),
            "z": float(active["z"]),
        }
    except (ValueError, KeyError, TypeError):
        warnings.append("the remote base's site list could not be read")
        return None


async def _base_xyz(
    req: PpkRequest, ctx: PpkContext, base: _Side
) -> tuple[tuple[float, float, float], str]:
    if req.base_site:
        site = await SitesRepo(await _db(ctx, f"site {req.base_site!r}")).get(req.base_site)
        if site is None:
            raise PpkError(f"site {req.base_site!r} not found")
        return (site.x, site.y, site.z), f"site:{req.base_site}"
    if req.base_xyz:
        return req.base_xyz, "request"
    remote = base.info.get("remote_site")
    if remote:
        return (remote["x"], remote["y"], remote["z"]), f"remote-site:{remote['name']}"
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
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), RNX2RTKP_TIMEOUT_S)
    except TimeoutError as exc:
        await _kill_and_reap(proc)
        raise PpkError(f"rnx2rtkp timed out after {RNX2RTKP_TIMEOUT_S:.0f}s") from exc
    except BaseException:
        await _kill_and_reap(proc)
        raise
    text = output.decode("utf-8", "replace").replace("\r", "\n")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("processing")]
    shown = " ".join([binary, *cmd[1:]])
    with (out / "rnx2rtkp.log").open("a" if append else "w") as fh:
        fh.write(shown + "\n" + "".join(f"{ln}\n" for ln in lines))
    tail = " ".join(lines)[-ERROR_TAIL_CHARS:]
    if proc.returncode != 0:
        raise PpkError(f"rnx2rtkp failed (exit {proc.returncode}): {tail}")
    if bad := [ln.strip() for ln in lines if INVALID_OPTION in ln]:
        raise PpkError(f"rnx2rtkp did not accept the options: {'; '.join(bad)[:ERROR_TAIL_CHARS]}")
    if not pos.exists():
        raise PpkError(f"rnx2rtkp wrote no track.pos: {tail}")
    return pos


def _postprocess(
    out: Path,
    records: list[PosRecord],
    req: PpkRequest,
    rover_ubx: Path | None,
    window: tuple[datetime, datetime] | None,
    warnings: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse the solution and write the track and event files (blocking: run in a thread)."""
    summary = summarize(records, gap_s=req.max_gap_s)
    (out / "track.csv").write_text(track_csv(records))
    (out / "track.geojson").write_text(json.dumps(track_geojson(records)))
    (out / "track.kml").write_text(track_kml(records))
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
            fixes = interpolate_events(marks, records, req.max_gap_s)
            (out / "events.csv").write_text(events_csv(fixes))
            (out / "events.geojson").write_text(json.dumps(events_geojson(fixes)))
            events = {"total": len(fixes)}
            for k in ("ok", "gap_too_large", "no_neighbours"):
                events[k] = sum(e.status == k for e in fixes)
            if missing := len(fixes) - events["ok"]:
                warnings.append(f"{missing} of {len(fixes)} camera mark(s) could not be placed")
    if summary.fixed_pct < FEW_FIXED_PCT:
        warnings.append(
            f"only {summary.fixed_pct:.0f}% of epochs are RTK fixed; check baseline length, "
            "sky view and that the base and rover windows overlap"
        )
    return summary.to_json(), events


def _read_pos(path: Path) -> list[PosRecord]:
    return parse_pos(path.read_text(errors="replace"))


def _with_forward(conf: str) -> str:
    """`conf` with its solution type set to forward-only, saying why."""
    lines = [
        f"{'pos1-soltype':<20}=forward" if ln.split("=", 1)[0].strip() == "pos1-soltype" else ln
        for ln in conf.splitlines()
    ]
    note = "# combined solution was empty: rerun forward-only"
    return "\n".join([lines[0], note, *lines[1:]]) + "\n"


def _glonass_ar(receiver_type: str, warnings: list[str]) -> str:
    """`on` only between u-blox receivers: GLONASS integer ambiguities need matching
    inter-channel biases, which another make's base does not have."""
    rt = receiver_type.strip()
    if rt.lower() in UNKNOWN_RECEIVERS or "u-blox" in rt.lower():
        return "on"
    warnings.append(f"base receiver is {rt!r}; GLONASS ambiguity resolution set to autocal")
    return "autocal"


def _cleanup(out: Path) -> None:
    for scratch in SCRATCH:
        with contextlib.suppress(OSError):
            (out / scratch).unlink(missing_ok=True)
            (out / f"{scratch}.part").unlink(missing_ok=True)


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
        return await _run(req, ctx, out, report, warnings)
    except ConvbinError as exc:
        raise PpkError(str(exc)) from exc
    except OSError as exc:
        where = f" ({exc.filename})" if exc.filename else ""
        raise PpkError(
            f"cannot write the PPK outputs into {out}: {exc.strerror or exc}{where}"
        ) from exc
    finally:
        await asyncio.to_thread(_cleanup, out)


async def _run(
    req: PpkRequest,
    ctx: PpkContext,
    out: Path,
    report: Callable[[float, str], Awaitable[None]],
    warnings: list[str],
) -> dict[str, Any]:
    window = await _resolve_window(req, ctx, warnings)
    if window is not None and window[1] - window[0] > MAX_WINDOW:
        raise PpkError(f"the window is {window[1] - window[0]}; process at most 7 days at a time")
    await report(0.05, "converting rover observations")
    rover = await _rover_side(req, ctx, out, window, warnings)
    if window is None and req.base.kind != "upload":
        span = await asyncio.to_thread(obs_span, rover.obs)
        if span is None:
            raise PpkError("the rover file has no epochs to take the base window from")
        window = (_utc_from_gpst(span[0]), _utc_from_gpst(span[1]) + timedelta(seconds=1))
    await report(0.3, "preparing base observations")
    base = await _base_side(req, ctx, out, window, warnings)
    xyz, xyz_source = await _base_xyz(req, ctx, base)
    base_hdr = await asyncio.to_thread(read_header, base.obs)
    glonass_ar = _glonass_ar(base_hdr.receiver_type, warnings)
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
    records = await asyncio.to_thread(_read_pos, await _run_rnx2rtkp(ctx, out, navs))
    soltype = parse_conf(conf).get("pos1-soltype")
    if not records and soltype == "combined":
        # The backward pass can fail outright on a short or degenerate span (stock 2.4.3 on a
        # minute of data does), and a combined solution needs both passes.
        conf = _with_forward(conf)
        await asyncio.to_thread((out / "ppk.conf").write_text, conf)
        records = await asyncio.to_thread(
            _read_pos, await _run_rnx2rtkp(ctx, out, navs, append=True)
        )
        if records:
            soltype = "forward"
            warnings.append(
                "the combined (forward + backward) solution was empty; this track is the "
                "forward-only solution"
            )
    if not records:
        raise PpkError(
            "rnx2rtkp produced no solution epochs; check rnx2rtkp.log (do the rover and base "
            "windows overlap? do they share signals?)"
        )
    await report(0.8, "writing track")
    summary, events = await asyncio.to_thread(
        _postprocess, out, records, req, rover.ubx, window, warnings
    )
    await asyncio.to_thread(_cleanup, out)
    files = await asyncio.to_thread(_list_files, out)
    base_info = {k: v for k, v in base.info.items() if k != "remote_site"}
    result: dict[str, Any] = {
        "summary": summary,
        "events": events,
        "warnings": warnings,
        "inputs": {
            "rover": {**req.rover.model_dump(mode="json"), **rover.info},
            "base": {**req.base.model_dump(mode="json"), **base_info},
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


def _list_files(out: Path) -> list[dict[str, Any]]:
    return [
        {"name": p.name, "bytes": p.stat().st_size}
        for p in sorted(out.iterdir())
        if p.is_file() and not p.name.startswith(".") and p.name != "summary.json"
    ]


def make_ppk_job(req: PpkRequest, ctx: PpkContext) -> JobFn:
    """A `JobRunner` job (kind `ppk`) that writes into its own result directory and returns
    the summary as the job's result."""

    async def job(jctx: JobContext) -> dict[str, Any]:
        return await run_ppk(req, ctx, jctx.dir, progress=jctx.progress)

    return job
