"""Raw UBX window -> RINEX files for a PPP service or generic post-processing.

One export is: splice the hourly logs that cover the window (plus a lead hour, so the
navigation file has ephemerides), run `convbin` clipped to the window, compress as the preset
says, and write `manifest.json` next to the result files. `export_to_dir` does that into any
directory - a job's result directory (`make_export_job`) or a temporary one for the synchronous
download and the CLI.

Requests are in UTC; `convbin` compares its window with the RINEX epoch stamps, which are GPST,
so the window is moved onto GPST (`GPS_UTC_OFFSET`) before it reaches the wrapper.

Errors: `SpliceError` (no logs, several stations) and `ConvbinError` (the conversion failed) go
out as they are; anything else that stops an export - a setting that cannot name the files, an
option convbin cannot take, a compression failure - is an `ExportError` with a message meant for
the operator.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import shutil
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import hatanaka
from pydantic import BaseModel, field_validator, model_validator

from mtrtk.jobs import JobContext, JobFn
from mtrtk.rinex.convbin import ConvbinOptions, RinexHeader, run_convbin
from mtrtk.rinex.naming import rinex2_name, rinex3_name
from mtrtk.rinex.presets import PRESETS, ResolvedOptions, resolve_options
from mtrtk.rinex.splice import SpliceResult, splice_window

log = logging.getLogger(__name__)

Progress = Callable[[float, str | None], Awaitable[None]]
MAX_WINDOW = timedelta(days=7)
LEAD_HOURS = 1  # the hour before the window: its ephemerides are still valid at the start
GPS_UTC_OFFSET = timedelta(seconds=18)  # GPST - UTC since 2017-01-01; no leap second announced
PPP_MIN_S = 3600  # below this CSRS-PPP and AUSPOS give a poor solution or refuse it
FEW_NAV_MESSAGES = 20
SPLICED_NAME = "spliced.ubx"
MANIFEST_NAME = "manifest.json"


class ExportError(RuntimeError):
    """An export that cannot go ahead or finish, for a reason the operator can act on."""


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


def _hatanaka_file(path: Path) -> tuple[Path, list[str]]:
    """`.rnx` -> `.crx.gz`, and what rnx2crx complained about without failing.

    rnx2crx reports its non-fatal complaints as Python warnings, and keeps the source file when
    it has any; both are collected here so they reach the result rather than the log alone.
    `catch_warnings` is process-wide in 3.12, which is acceptable for the second or so this
    takes: nothing else in the daemon depends on warnings being shown.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = Path(hatanaka.compress_on_disk(path, compression="gz", delete=True))
    path.unlink(missing_ok=True)
    return out, [f"Hatanaka compression: {w.message}" for w in caught]


def _has_content(path: Path) -> bool:
    return path.stat().st_size > 0


def _package(
    obs_path: Path, nav_path: Path, opts: ResolvedOptions, keep_nav: bool
) -> tuple[list[dict[str, Any]], list[str]]:
    """Compress as the preset says and list the result files, observations first.

    Hatanaka implies gzip (`.crx.gz`, the form the PPP services take); the navigation file is
    never Hatanaka-compressed, only gzipped alongside. A navigation file not kept is removed.
    """
    found: list[str] = []
    try:
        if opts.hatanaka:
            obs_path, found = _hatanaka_file(obs_path)
        elif opts.gzip:
            obs_path = _gzip_file(obs_path)
    except (hatanaka.HatanakaException, ValueError, OSError) as exc:
        raise ExportError(f"compressing {obs_path.name} failed: {exc}") from exc
    files: list[dict[str, Any]] = [
        {"name": obs_path.name, "bytes": obs_path.stat().st_size, "role": "obs"}
    ]
    if keep_nav:
        if opts.gzip or opts.hatanaka:
            nav_path = _gzip_file(nav_path)
        files.append({"name": nav_path.name, "bytes": nav_path.stat().st_size, "role": "nav"})
    else:
        nav_path.unlink(missing_ok=True)
    return files, found


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
) -> ExportResult:
    """Export `request` into `out_dir` and write its manifest there.

    Raises `NoDataError`/`MixedStationsError` (from splicing), `ConvbinError`, or
    `ExportError`. On any failure, cancellation included, the files this call wrote are removed.
    """

    async def report(p: float, msg: str) -> None:
        if progress is not None:
            await progress(p, msg)

    opts = resolve_options(request.preset, request.interval_s, request.hatanaka, request.gzip)
    obs_name, nav_name = _names(request, ctx, opts)  # before any I/O: a bad setting costs nothing
    await asyncio.to_thread(out_dir.mkdir, parents=True, exist_ok=True)
    duration_s = (request.end - request.start).total_seconds()
    warns: list[str] = []
    if request.preset in ("csrs-ppp", "auspos") and duration_s < PPP_MIN_S:
        warns.append("window shorter than 1 h: PPP services want several hours (24 h recommended)")
    if request.preset == "opus":
        warns.append(
            "OPUS wants GPS L2 data; the F9P provides L2C (not L2P). "
            "Check acceptance before relying on it."
        )

    spliced_path = out_dir / SPLICED_NAME
    outputs = [
        out_dir / name
        for base in (obs_name, nav_name)
        for name in (
            base,
            f"{base}.gz",
            base.replace(".rnx", ".crx"),
            base.replace(".rnx", ".crx.gz"),
        )
    ] + [out_dir / MANIFEST_NAME]
    try:
        await report(0.05, "splicing raw logs")
        spliced = await asyncio.to_thread(
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
            start=_gpst(request.start),
            end=_gpst(request.end),
        )
        try:
            result = await run_convbin(
                spliced.path, out_dir / obs_name, out_dir / nav_name, convbin_opts
            )
        except ValueError as exc:
            raise ExportError(f"invalid conversion options: {exc}") from exc
        finally:
            await asyncio.to_thread(spliced_path.unlink, missing_ok=True)

        # convbin writes no navigation file for a window without ephemerides, and the empty
        # one the wrapper leaves in its place is not valid RINEX: it is never shipped.
        has_nav = result.nav_messages > 0 and await asyncio.to_thread(_has_content, result.nav_path)
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
        files, crx_warnings = await asyncio.to_thread(
            _package, result.obs_path, result.nav_path, opts, request.include_nav and has_nav
        )
        warns += crx_warnings

        res = ExportResult(
            files,
            result.obs_epochs,
            result.nav_messages,
            opts.interval_s,
            opts.version,
            request.preset,
            request.start.isoformat(),
            request.end.isoformat(),
            warns,
        )
        await asyncio.to_thread(_write_manifest, out_dir, res)
    except BaseException:
        for path in (spliced_path, *outputs):
            path.unlink(missing_ok=True)
        raise
    for w in warns:
        log.info("export %s %s..%s: %s", request.preset, request.start, request.end, w)
    await report(1.0, "done")
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
