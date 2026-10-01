"""Raw UBX window -> RINEX files for a PPP service or generic post-processing.

One export is: splice the hourly logs that cover the window (plus a lead hour, so the
navigation file has ephemerides), run `convbin` clipped to the window, compress as the preset
says, and write `manifest.json` next to the result files. `export_to_dir` does that into any
directory - a job's result directory (`make_export_job`) or a temporary one for the synchronous
download and the CLI.

Everything is built in a private staging directory inside `out_dir` and moved into place only
once it is complete, the manifest last. A failed or cancelled export therefore never touches
what `out_dir` already holds, and an export whose file names (or `manifest.json`) are already
there is refused unless the caller asks to overwrite.

Requests are in UTC; `convbin` compares its window with the RINEX epoch stamps, which are GPST,
so the window is moved onto GPST (`GPS_UTC_OFFSET`) before it reaches the wrapper.

Errors: `SpliceError` (no logs, several stations) and `ConvbinError` (the conversion failed) go
out as they are; anything else that stops an export - a setting that cannot name the files, an
option convbin cannot take, a compression failure, a directory that cannot be written - is an
`ExportError` with a message meant for the operator. `EXPORT_ERRORS` is the three of them, for a
caller that maps them all to one answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import importlib.resources
import json
import logging
import os
import shutil
import signal
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

from pydantic import BaseModel, field_validator, model_validator

from mtrtk.jobs import JobContext, JobFn
from mtrtk.rinex.convbin import ConvbinError, ConvbinOptions, RinexHeader, run_convbin
from mtrtk.rinex.naming import rinex2_name, rinex3_name
from mtrtk.rinex.presets import PRESETS, ResolvedOptions, resolve_options
from mtrtk.rinex.splice import SpliceError, SpliceResult, splice_window

log = logging.getLogger(__name__)

Progress = Callable[[float, str | None], Awaitable[None]]
MAX_WINDOW = timedelta(days=7)
LEAD_HOURS = 1  # the hour before the window: its ephemerides are still valid at the start
GPS_UTC_OFFSET = timedelta(seconds=18)  # GPST - UTC since 2017-01-01; no leap second announced
PPP_MIN_S = 3600  # below this CSRS-PPP and AUSPOS give a poor solution or refuse it
FEW_NAV_MESSAGES = 20
SPLICED_NAME = "spliced.ubx"
MANIFEST_NAME = "manifest.json"
STAGE_PREFIX = ".export-"  # the staging directory inside out_dir; hidden, removed afterwards
HATANAKA_TIMEOUT_S = 900.0  # rnx2crx does a day of 1 Hz in well under a minute; this is a leash
REAP_TIMEOUT_S = 5.0
THREAD_DRAIN_S = 5.0  # how long a cancelled export waits for its worker thread to let go
ERROR_TAIL_CHARS = 500
# Signals in the L5/E5a/B2a band: a receiver tracking one has three frequencies to convert.
L5_SIGNALS = frozenset({"L5I", "L5Q", "E5aI", "E5aQ", "B2a", "L5A"})


class ExportError(RuntimeError):
    """An export that cannot go ahead or finish, for a reason the operator can act on."""


# Everything `export_to_dir` raises on purpose: map these to a 4xx / exit 1, anything else is a bug.
EXPORT_ERRORS: tuple[type[Exception], ...] = (SpliceError, ConvbinError, ExportError)


class ExportRequest(BaseModel):
    start: datetime
    end: datetime
    preset: str = "csrs-ppp"
    interval_s: float | None = None
    hatanaka: bool | None = None
    gzip: bool | None = None
    include_nav: bool = True

    @field_validator("preset")
    @classmethod
    def _preset(cls, v: str) -> str:
        if v not in PRESETS:
            raise ValueError(f"unknown preset {v!r}; choose one of {sorted(PRESETS)}")
        return v

    @model_validator(mode="after")
    def _window(self) -> ExportRequest:
        if self.start.utcoffset() is None or self.end.utcoffset() is None:
            raise ValueError("start and end must be timezone-aware (UTC)")
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.end - self.start > MAX_WINDOW:
            raise ValueError("window longer than 7 days")
        resolve_options(self.preset, self.interval_s, self.hatanaka, self.gzip)  # fixed presets
        return self


@dataclass(frozen=True)
class ExportContext:
    root: Path
    station_id: str
    country: str
    header: RinexHeader
    frequencies: int = 2  # convbin -f; 3 once the receiver tracks L5 (`frequencies_from_state`)


@dataclass
class ExportResult:
    files: list[dict[str, Any]]
    obs_epochs: int
    nav_messages: int
    interval_s: float | None
    version: str
    preset: str
    start: str
    end: str
    warnings: list[str]
    created_utc: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_manifest(self) -> dict[str, Any]:
        return {
            "preset": self.preset,
            "version": self.version,
            "interval_s": self.interval_s,
            "start": self.start,
            "end": self.end,
            "obs_epochs": self.obs_epochs,
            "nav_messages": self.nav_messages,
            "files": self.files,
            "warnings": self.warnings,
            "created_utc": self.created_utc,
        }


def frequencies_from_state(state: Any) -> int:
    """convbin's `-f` for what the receiver tracks: 3 when any satellite reports an L5-band
    signal (HPG 1.51+ firmware), else 2 (L1/L2, all HPG 1.13 has)."""
    if state is None:
        return 2
    tracked = {sig.name for sat in state.sats for sig in sat.signals}
    return 3 if tracked & L5_SIGNALS else 2


def _gpst(t: datetime) -> datetime:
    """A UTC instant as the naive GPST wall-clock time `ConvbinOptions` takes."""
    return (t.astimezone(UTC) + GPS_UTC_OFFSET).replace(tzinfo=None)


def _names(request: ExportRequest, ctx: ExportContext, opts: ResolvedOptions) -> tuple[str, str]:
    duration_s = (request.end - request.start).total_seconds()
    try:
        if opts.version.startswith("2"):
            return (
                rinex2_name(ctx.station_id, request.start, duration_s, "o"),
                rinex2_name(ctx.station_id, request.start, duration_s, "n"),
            )
        return (
            rinex3_name(
                ctx.station_id, ctx.country, request.start, duration_s, opts.interval_s, "MO"
            ),
            rinex3_name(ctx.station_id, ctx.country, request.start, duration_s, None, "MN"),
        )
    except ValueError as exc:
        raise ExportError(
            f"cannot name the RINEX files: {exc}. Check STATION_ID and COUNTRY in the settings."
        ) from exc


def _gzip_file(path: Path) -> Path:
    out = path.with_name(path.name + ".gz")
    with path.open("rb") as src, gzip.open(out, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()
    return out


def _crx_name(name: str) -> str:
    """The Compact RINEX name of an observation file: `.rnx` -> `.crx`, RINEX 2 `.yyo` -> `.yyd`."""
    if name.endswith(".rnx"):
        return name[: -len(".rnx")] + ".crx"
    return name[:-1] + "d"


def _final_names(obs_name: str, nav_name: str, opts: ResolvedOptions) -> tuple[str, str]:
    """The names the observation and navigation files end up with once compressed."""
    if opts.hatanaka:
        obs_name = _crx_name(obs_name) + ".gz"
    elif opts.gzip:
        obs_name += ".gz"
    if opts.hatanaka or opts.gzip:
        nav_name += ".gz"
    return obs_name, nav_name


def rnx2crx_binary() -> Path:
    """The rnx2crx executable the `hatanaka` wheel ships."""
    return Path(str(importlib.resources.files("hatanaka.bin").joinpath("rnx2crx")))


def _open_pair(src: Path, dst: Path) -> tuple[IO[bytes], IO[bytes]]:
    fin = src.open("rb")
    try:
        return fin, dst.open("wb")
    except BaseException:
        fin.close()
        raise


async def _kill(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    try:
        await asyncio.wait_for(proc.communicate(), REAP_TIMEOUT_S)
    except TimeoutError:
        log.warning("rnx2crx (pid %d) still holds its pipes after SIGKILL", proc.pid)


async def _hatanaka(path: Path, crx: Path) -> list[str]:
    """`.rnx` -> `.crx` with the bundled rnx2crx, file to file, and what it warned about.

    Not `hatanaka.compress_on_disk`: that reads the whole file into memory, holds rnx2crx's
    output there too and gzips it in one piece - gigabytes for a week at 1 Hz, inside the
    daemon. rnx2crx itself streams, so it is handed the two files directly; the gzip step that
    follows streams as well. The exit codes are rnx2crx's own: 0 clean, 2 done with warnings,
    anything else failed. On cancellation or timeout the child is killed and reaped.
    """
    binary = rnx2crx_binary()
    fin, fout = await asyncio.to_thread(_open_pair, path, crx)
    try:
        proc = await asyncio.create_subprocess_exec(
            str(binary),
            "-",
            stdin=fin,
            stdout=fout,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise ExportError(f"cannot run rnx2crx ({binary}) for Hatanaka compression: {exc}") from exc
    finally:
        fin.close()
        fout.close()
    try:
        _, err = await asyncio.wait_for(proc.communicate(), HATANAKA_TIMEOUT_S)
    except BaseException as exc:
        await _kill(proc)
        if isinstance(exc, TimeoutError):
            raise ExportError(
                f"Hatanaka compression of {path.name} timed out after {HATANAKA_TIMEOUT_S:g}s"
            ) from exc
        raise
    said = " ".join(err.decode("ascii", "backslashreplace").split())
    if proc.returncode not in (0, 2):
        raise ExportError(
            f"Hatanaka compression of {path.name} failed (rnx2crx exited {proc.returncode}): "
            f"{said[-ERROR_TAIL_CHARS:]}"
        )
    if said:
        return [f"Hatanaka compression: {said[-ERROR_TAIL_CHARS:]}"]
    if proc.returncode == 2:
        return ["Hatanaka compression: rnx2crx exited with an unspecified warning"]
    return []


def _has_content(path: Path) -> bool:
    return path.stat().st_size > 0


def _sizes(paths: list[tuple[Path, str]]) -> list[dict[str, Any]]:
    return [{"name": p.name, "bytes": p.stat().st_size, "role": role} for p, role in paths]


async def _package(
    obs_path: Path, nav_path: Path, opts: ResolvedOptions, keep_nav: bool
) -> tuple[list[dict[str, Any]], list[str]]:
    """Compress as the preset says and list the result files, observations first.

    Hatanaka implies gzip (`.crx.gz`, the form the PPP services take); the navigation file is
    never Hatanaka-compressed, only gzipped alongside. A navigation file not kept is removed.
    """
    found: list[str] = []
    if opts.hatanaka:
        crx = obs_path.with_name(_crx_name(obs_path.name))
        found = await _hatanaka(obs_path, crx)
        await _in_thread(obs_path.unlink)
        obs_path = await _in_thread(_gzip_file, crx)
    elif opts.gzip:
        obs_path = await _in_thread(_gzip_file, obs_path)
    listed = [(obs_path, "obs")]
    if keep_nav:
        if opts.gzip or opts.hatanaka:
            nav_path = await _in_thread(_gzip_file, nav_path)
        listed.append((nav_path, "nav"))
    else:
        await _in_thread(nav_path.unlink, missing_ok=True)
    return await _in_thread(_sizes, listed), found


def _write_manifest(out_dir: Path, result: ExportResult) -> None:
    """Write `manifest.json`, listing itself with its own final size.

    The size is part of the text it measures, so the entry is refined until the two agree;
    the digit count settles within a couple of rounds.
    """
    entry: dict[str, Any] = {"name": MANIFEST_NAME, "bytes": 0, "role": "manifest"}
    result.files.append(entry)
    for _ in range(10):
        text = json.dumps(result.to_manifest(), indent=2)
        size = len(text.encode())
        if size == entry["bytes"]:
            break
        entry["bytes"] = size
    else:  # pragma: no cover - a size that keeps changing its own digit count
        raise ExportError("manifest size did not settle")
    (out_dir / MANIFEST_NAME).write_text(text)


def _refuse_existing(out_dir: Path, names: list[str]) -> None:
    taken = [name for name in names if (out_dir / name).exists()]
    if taken:
        raise ExportError(
            f"{out_dir} already holds {', '.join(taken)} from an earlier export; "
            "choose another directory, or overwrite it"
        )


def _make_stage(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=STAGE_PREFIX, dir=out_dir))


def _publish(stage: Path, out_dir: Path, names: list[str], overwrite: bool) -> None:
    """Move the finished files out of the staging directory, in order - the manifest is last,
    so a manifest in `out_dir` always describes files that are there. A move that fails takes
    back the ones already made, so `out_dir` gets the whole export or none of it."""
    if not overwrite:
        _refuse_existing(out_dir, names)  # again: something may have appeared meanwhile
    moved: list[Path] = []
    try:
        for name in names:
            os.replace(stage / name, out_dir / name)
            moved.append(out_dir / name)
    except BaseException:
        for path in moved:
            path.unlink(missing_ok=True)
        raise


def _os_message(out_dir: Path, exc: OSError) -> str:
    where = f" ({exc.filename})" if exc.filename else ""
    return f"cannot write the export into {out_dir}: {exc.strerror or exc}{where}"


def _retrieve(task: asyncio.Future[Any]) -> None:
    if not task.cancelled():
        task.exception()  # an abandoned thread's error is not "never retrieved"


async def _in_thread[**P, T](fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """`asyncio.to_thread`, except that a cancellation waits (up to `THREAD_DRAIN_S`) for the
    thread to finish before it goes on. A thread cannot be stopped; one still running after the
    cleanup could write into a job directory already marked cancelled."""
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    task.add_done_callback(_retrieve)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait({task}, timeout=THREAD_DRAIN_S)
        raise


def _warn_open_hours(spliced: SpliceResult) -> list[str]:
    open_hours = [f"{lf.hour_utc:%Y-%m-%d %H}:00" for lf in spliced.files if not lf.complete]
    if not open_hours:
        return []
    return [
        f"the window includes an hour that is still being written ({', '.join(open_hours)} "
        "UTC); export again after it closes to get all of its data"
    ]


async def export_to_dir(
    request: ExportRequest,
    ctx: ExportContext,
    out_dir: Path,
    progress: Progress | None = None,
    *,
    overwrite: bool = False,
) -> ExportResult:
    """Export `request` into `out_dir` and write its manifest there.

    Raises `NoDataError`/`MixedStationsError` (from splicing), `ConvbinError`, or
    `ExportError` - the last also when `out_dir` already holds one of this export's file names
    or a `manifest.json` and `overwrite` is false. The work happens in a staging directory
    inside `out_dir`, removed on the way out, so on any failure, cancellation included,
    `out_dir` is left as it was.
    """

    async def report(p: float, msg: str | None) -> None:
        if progress is not None:
            await progress(p, msg)

    opts = resolve_options(request.preset, request.interval_s, request.hatanaka, request.gzip)
    obs_name, nav_name = _names(request, ctx, opts)  # before any I/O: a bad setting costs nothing
    duration_s = (request.end - request.start).total_seconds()
    warns: list[str] = []
    if request.preset in ("csrs-ppp", "auspos") and duration_s < PPP_MIN_S:
        warns.append("window shorter than 1 h: PPP services want several hours (24 h recommended)")
    if request.preset == "opus":
        warns.append(
            "OPUS wants GPS L2 data; the F9P provides L2C (not L2P). "
            "Check acceptance before relying on it."
        )

    stage: Path | None = None
    try:
        try:
            if not overwrite:
                final = [*_final_names(obs_name, nav_name, opts), MANIFEST_NAME]
                await _in_thread(_refuse_existing, out_dir, final)
            stage = await _in_thread(_make_stage, out_dir)
            res = await _export(request, ctx, opts, stage, obs_name, nav_name, warns, report)
            await _in_thread(_publish, stage, out_dir, [f["name"] for f in res.files], overwrite)
        except OSError as exc:
            raise ExportError(_os_message(out_dir, exc)) from exc
    finally:
        if stage is not None:
            # Shielded: a second cancellation must not leave the staging directory behind.
            await asyncio.shield(asyncio.to_thread(shutil.rmtree, stage, ignore_errors=True))
    for w in warns:
        log.info("export %s %s..%s: %s", request.preset, request.start, request.end, w)
    await report(1.0, "done")
    return res


async def _export(
    request: ExportRequest,
    ctx: ExportContext,
    opts: ResolvedOptions,
    stage: Path,
    obs_name: str,
    nav_name: str,
    warns: list[str],
    report: Progress,
) -> ExportResult:
    """Splice, convert, compress and write the manifest, all inside `stage`."""
    await report(0.05, "splicing raw logs")
    spliced_path = stage / SPLICED_NAME
    spliced = await _in_thread(
        splice_window,
        ctx.root,
        request.start,
        request.end,
        spliced_path,
        LEAD_HOURS,
        station=ctx.station_id,
    )
    warns += _warn_open_hours(spliced)

    await report(0.2, "converting with convbin")
    convbin_opts = ConvbinOptions(
        header=ctx.header,
        version=opts.version,
        interval_s=opts.interval_s,
        exclude_systems=opts.exclude_systems,
        frequencies=ctx.frequencies,
        start=_gpst(request.start),
        end=_gpst(request.end),
    )
    try:
        result = await run_convbin(spliced.path, stage / obs_name, stage / nav_name, convbin_opts)
    except ValueError as exc:
        raise ExportError(f"invalid conversion options: {exc}") from exc
    finally:
        await _in_thread(spliced_path.unlink, missing_ok=True)  # the biggest file: free it now

    # convbin writes no navigation file for a window without ephemerides, and the empty
    # one the wrapper leaves in its place is not valid RINEX: it is never shipped.
    has_nav = result.nav_messages > 0 and await _in_thread(_has_content, result.nav_path)
    if not has_nav:
        warns.append(
            "no navigation messages (ephemerides) in the window, so no navigation file "
            "was written; PPP services fetch their own, other tools may need one"
        )
    elif result.nav_messages < FEW_NAV_MESSAGES:
        warns.append(
            f"only {result.nav_messages} navigation messages in the window; the navigation "
            "file may not cover every satellite (PPP services fetch their own ephemerides)"
        )

    await report(0.8, "compressing")
    files, crx_warnings = await _package(
        result.obs_path, result.nav_path, opts, request.include_nav and has_nav
    )
    warns += crx_warnings

    res = ExportResult(
        files,
        result.obs_epochs,
        result.nav_messages,
        opts.interval_s,
        opts.version,
        request.preset,
        request.start.astimezone(UTC).isoformat(),
        request.end.astimezone(UTC).isoformat(),
        warns,
    )
    await _in_thread(_write_manifest, stage, res)
    return res


def make_export_job(request: ExportRequest, ctx: ExportContext) -> JobFn:
    """A `JobRunner` job (kind `export`) that writes into its own result directory and
    returns the manifest as the job's result."""

    async def job(jctx: JobContext) -> dict[str, Any]:
        result = await export_to_dir(request, ctx, jctx.dir, progress=jctx.progress)
        return result.to_manifest()

    return job


def header_from_settings(settings: Any, state: Any, site: Any | None) -> RinexHeader:
    """The RINEX header for this station: identity from the settings, the approximate position
    from the active site, else the receiver's current ECEF fix, else none."""
    approx: tuple[float, float, float] | None = None
    if site is not None:
        approx = (site.x, site.y, site.z)
    elif state is not None:
        p = state.position
        if p.ecef_x_m is not None and p.ecef_y_m is not None and p.ecef_z_m is not None:
            approx = (p.ecef_x_m, p.ecef_y_m, p.ecef_z_m)
    firmware = state.firmware.fw_version if state is not None else ""
    return RinexHeader(
        marker_name=settings.marker_name or settings.station_id,
        marker_number=settings.station_id,
        observer=settings.observer,
        agency=settings.agency,
        receiver_version=firmware or "unknown",
        antenna_type=settings.antenna_type,
        approx_xyz=approx,
        delta_hen=(settings.antenna_height_m, 0.0, 0.0),
        comment="mtrtk export",
    )
