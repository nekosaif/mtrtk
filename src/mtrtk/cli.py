"""mtrtk command line interface."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
import httpx

from mtrtk import __version__
from mtrtk.rinex.presets import PRESETS

HEALTHCHECK_TIMEOUT_S = 3.0  # the container healthcheck runs every 30 s; it must never hang

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from pydantic import ValidationError

    from mtrtk.config import Settings
    from mtrtk.store.db import Database
    from mtrtk.store.models import Site


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="mtrtk")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
def main(verbose: bool) -> None:
    """mtrtk - GNSS RTK/PPK/PPP toolkit for ZED-F9P base stations and rovers."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if verbose:
        _quiet_noisy_loggers()


def _quiet_noisy_loggers() -> None:
    # Debug logging is for mtrtk's own output. aiosqlite logs every statement it executes and
    # asyncio, httpx and httpcore a line per operation, which buries it several times over.
    for noisy in ("aiosqlite", "asyncio", "httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.INFO)


def _apply_log_level(settings: Settings) -> None:
    """Set the daemon's root log level from LOG_LEVEL, unless `mtrtk -v` already asked for DEBUG.

    `main` has run `basicConfig` before any settings exist, so the level is set on the root
    logger directly: a second `basicConfig` call would be a silent no-op.
    """
    ctx = click.get_current_context(silent=True)
    verbose = bool(ctx and ctx.find_root().params.get("verbose"))
    level = "DEBUG" if verbose else settings.log_level
    logging.getLogger().setLevel(level)
    if level == "DEBUG":
        _quiet_noisy_loggers()


def _load_settings(**overrides: object) -> Settings:
    from pydantic import ValidationError

    from mtrtk.config import Settings

    # The same variable names the file `PUT /api/config` writes; honouring it here keeps the read
    # and write sides of the configuration on one file (and keeps the test suite off the repo's).
    env_file = os.environ.get("MTRTK_ENV_FILE", ".env")
    kwargs = {k: v for k, v in overrides.items() if v is not None}
    try:
        return Settings(_env_file=env_file, **kwargs)  # type: ignore[arg-type, call-arg]
    except ValidationError as exc:
        raise click.ClickException(f"invalid configuration:\n{exc}") from exc


def _run_daemon(settings: Settings) -> None:
    from mtrtk.core.receiver import ProfileError
    from mtrtk.daemon import Daemon, StatusPrinter

    _apply_log_level(settings)

    async def go() -> None:
        try:
            daemon = Daemon(settings)
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        printer = StatusPrinter(daemon.bus, daemon.store, echo=click.echo)
        printer.start()
        try:
            await daemon.run()
        except ProfileError as exc:
            # RECEIVER_STRICT=1: the receiver would not take the profile, so the daemon has
            # no business running. Exit 1 with the rejected keys, not a traceback.
            raise click.ClickException(
                f"receiver configuration failed: {exc} (set RECEIVER_STRICT=0 to run anyway)"
            ) from exc
        finally:
            await printer.stop()

    asyncio.run(go())


@main.command()
def run() -> None:
    """Run the daemon in the role given by ROLE (.env)."""
    _run_daemon(_load_settings())


@main.command()
def base() -> None:
    """Run as a base station (ROLE=base)."""
    _run_daemon(_load_settings(role="base"))


@main.command()
def rover() -> None:
    """Run as a rover (ROLE=rover)."""
    _run_daemon(_load_settings(role="rover"))


@main.command()
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--speed",
    default=1.0,
    show_default=True,
    type=float,
    help="Pace multiplier; 0 = as fast as possible.",
)
@click.option("--loop", is_flag=True, help="Restart the file when it ends.")
def replay(file: Path, speed: float, loop: bool) -> None:
    """Replay a recorded .ubx stream as if it were a live receiver (no configuration is sent)."""
    settings = _load_settings(
        mtrtk_source=f"file:{file}",
        replay_speed=speed,
        replay_loop=loop,
        ntrip_password="",
    )
    _run_daemon(settings)
    click.echo("replay finished")


@main.command()
def healthcheck() -> None:
    """Exit 0 when the local web API answers /healthz (this is the container healthcheck)."""
    from mtrtk.core.exposure import BIND_ANY, resolve_bind, url_host

    # The healthcheck's whole output is read by `docker inspect`; httpx's own INFO line about
    # the request it just made is noise in front of the one word that matters.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = _load_settings(ntrip_password="")
    host = resolve_bind(settings.web_bind)
    if host is None:
        # WEB_BIND=tailscale with no tailscale0 address: the daemon is not listening anywhere
        # yet, and loopback would be the wrong place to look for it.
        click.echo(f"unhealthy: {settings.web_bind} has no address yet (is tailscaled running?)")
        raise SystemExit(1)
    if host == BIND_ANY:
        host = "127.0.0.1"  # 0.0.0.0 is what it binds, not an address to connect to
    url = f"http://{url_host(host)}:{settings.web_port}/healthz"
    try:
        response = httpx.get(url, timeout=HEALTHCHECK_TIMEOUT_S)
    except Exception as exc:
        click.echo(f"unhealthy: {url}: {exc}")
        raise SystemExit(1) from exc
    if response.status_code != 200:
        click.echo(f"unhealthy: {url} -> {response.status_code}")
        raise SystemExit(1)
    try:
        status = response.json().get("status")
    except ValueError:  # something else is answering on that port
        status = None
    if status != "ok":
        click.echo(f"unhealthy: {url} -> status {status!r}")
        raise SystemExit(1)
    click.echo("ok")


def _only_ntrip_password_unset(exc: click.ClickException) -> bool:
    """Whether loading failed only on the base's NTRIP_PASSWORD cross-check."""
    from pydantic import ValidationError

    cause = exc.__cause__
    if not isinstance(cause, ValidationError):
        return False
    errors = cause.errors()
    return len(errors) == 1 and "NTRIP_PASSWORD must be set" in str(errors[0].get("msg", ""))


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option(
    "--probe", is_flag=True, help="Also poll the receiver's firmware (stop the daemon first)."
)
def doctor(as_json: bool, probe: bool) -> None:
    """Check receiver access, host services, Tailscale, ports, RTKLIB, disk and exposure.

    Exits 1 when any check FAILs; warnings alone exit 0.
    """
    import json

    from mtrtk.doctor import Check, format_table, run_checks

    checks: list[Check] = []
    try:
        settings = _load_settings()
    except click.ClickException as exc:
        # The base refuses to start without NTRIP_PASSWORD; with a stand-in for it, the rest of
        # the host can still be checked. Any other invalid value is the whole answer.
        settings = None
        if _only_ntrip_password_unset(exc):
            # Not "": an unset password is not an anonymous caster, so no anonymous warning.
            try:
                settings = _load_settings(ntrip_password="unset-for-doctor")
            except click.ClickException as again:
                exc = again
        if settings is None:
            checks.append(Check("config", False, exc.message, fix="fix the values in .env"))
        else:
            checks.append(
                Check(
                    "config",
                    False,
                    "NTRIP_PASSWORD is not set: the base will not start",
                    fix="set NTRIP_PASSWORD (an empty NTRIP_PASSWORD= allows anonymous rovers)",
                )
            )
    if settings is not None:
        checks += run_checks(settings, probe_receiver=probe)
    if as_json:
        click.echo(json.dumps([c.to_json() for c in checks], indent=2))
    else:
        click.echo(format_table(checks))
    if any(c.ok is False for c in checks):
        raise SystemExit(1)


@main.command()
@click.option(
    "--port",
    default="auto",
    show_default=True,
    help="Serial device, or 'auto' to find a u-blox receiver.",
)
@click.option("--baud", default=115200, show_default=True, type=int)
@click.option("--seconds", default=60.0, show_default=True, type=float)
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False, path_type=Path))
def record(port: str, baud: int, seconds: float, out_path: Path) -> None:
    """Record the raw receiver byte stream to a file (for fixtures and replay)."""
    from mtrtk.core.recorder import record_stream

    try:
        stats = record_stream(port, baud, seconds, out_path)
    except (RuntimeError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"wrote {stats.bytes} bytes to {out_path}: frames={stats.frames} "
        f"garbage={stats.garbage_bytes} checksum_errors={stats.checksum_errors}"
    )


@main.group()
def sites() -> None:
    """Manage fixed base-station sites (ECEF positions)."""


def _with_db(fn: Callable[[Database], Awaitable[None]]) -> None:
    """Run an async function with an open Database at DATA_DIR/mtrtk.db."""
    from mtrtk.store.db import Database

    async def runner() -> None:
        settings = _load_settings(ntrip_password="")
        db = Database(settings.data_dir / "mtrtk.db")
        await db.open()
        try:
            await fn(db)
        finally:
            await db.close()

    asyncio.run(runner())


@sites.command("list")
def sites_list() -> None:
    """List saved sites; the active one is marked with *."""
    from mtrtk.store.repos import SitesRepo

    async def go(db: Database) -> None:
        rows = await SitesRepo(db).list()
        if not rows:
            click.echo("no sites saved")
            return
        for s in rows:
            mark = "*" if s.active else " "
            sigma = f"{s.sigma_3d:.4f}" if s.sigma_3d is not None else "-"
            click.echo(
                f"{mark} {s.name:<16} {s.x:14.4f} {s.y:14.4f} {s.z:14.4f}  "
                f"σ3D {sigma:>8} m  {s.frame:<10} {s.source}"
            )

    _with_db(go)


@sites.command("add")
@click.argument("name")
@click.option(
    "--ecef",
    nargs=3,
    type=float,
    metavar="X Y Z",
    help="ECEF metres (preferred: paste from a PPP report).",
)
@click.option(
    "--llh", nargs=3, type=float, metavar="LAT LON H", help="Geodetic degrees + height metres."
)
@click.option("--sigma", type=float, default=None, help="1-sigma per axis, metres.")
@click.option("--source", default="manual", show_default=True)
@click.option("--frame", default="ITRF2020", show_default=True)
@click.option("--epoch", default=None)
@click.option("--notes", default=None)
def sites_add(
    name: str,
    ecef: tuple[float, float, float] | None,
    llh: tuple[float, float, float] | None,
    sigma: float | None,
    source: str,
    frame: str,
    epoch: str | None,
    notes: str | None,
) -> None:
    """Save a site from ECEF or LLH coordinates."""
    from mtrtk.core.geo import llh_to_ecef
    from mtrtk.store.models import Site
    from mtrtk.store.repos import SitesRepo

    if ecef:
        x, y, z = ecef
    elif llh:
        x, y, z = llh_to_ecef(*llh)
    else:
        raise click.UsageError("give --ecef X Y Z or --llh LAT LON H")

    async def go(db: Database) -> None:
        try:
            site = await SitesRepo(db).add(
                Site.from_ecef(
                    name,
                    x,
                    y,
                    z,
                    sigma_m=sigma,
                    source=source,
                    frame=frame,
                    epoch=epoch,
                    notes=notes,
                )
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(
            f"saved site {site.name}: lat {site.lat:.8f} lon {site.lon:.8f} h {site.height_m:.3f}"
        )

    _with_db(go)


@sites.command("activate")
@click.argument("name")
def sites_activate(name: str) -> None:
    """Make NAME the active fixed site (a running base daemon applies it within 10 s)."""
    from mtrtk.store.repos import SitesRepo

    async def go(db: Database) -> None:
        try:
            site = await SitesRepo(db).activate(name)
        except KeyError as exc:
            raise click.ClickException(f"no site named {name!r}") from exc
        click.echo(f"{site.name} is now the active site; set BASE_MODE=fixed to use it at startup")

    _with_db(go)


@sites.command("delete")
@click.argument("name")
def sites_delete(name: str) -> None:
    """Delete a saved site."""
    from mtrtk.store.repos import SitesRepo

    async def go(db: Database) -> None:
        repo = SitesRepo(db)
        # `SitesRepo.delete` is a no-op for an unknown name; reporting success for a typo would
        # leave the operator believing a site is gone when it is still there under its real name.
        if await repo.get(name) is None:
            raise click.ClickException(f"no site named {name!r}")
        try:
            await repo.delete(name)
        except ValueError as exc:  # the active site: the base is broadcasting that position
            raise click.ClickException(str(exc)) from exc
        click.echo(f"deleted {name}")

    _with_db(go)


def _validation_message(exc: ValidationError, options: dict[str, str] | None = None) -> str:
    """The validators' own messages, without pydantic's framing or the input they refused.

    *options* maps model fields to the command's options: a field's message then starts with
    the option the value came from, and a model-level message names options, not fields.
    """
    parts = []
    for e in exc.errors():
        msg = str(e["msg"]).removeprefix("Value error, ")
        if options is not None:
            for name, option in options.items():
                msg = msg.replace(repr(name), option)
                if "_" in name:  # an identifier no sentence uses: `interval_s` is `--interval`
                    msg = re.sub(rf"\b{re.escape(name)}\b", option, msg)
            if e["loc"]:
                msg = f"{options.get(str(e['loc'][0]), e['loc'][0])}: {msg}"
        parts.append(msg)
    return "; ".join(parts)


# `mtrtk export`'s options for the ExportRequest fields they fill.
EXPORT_OPTIONS = {
    "start": "--from",
    "end": "--to",
    "preset": "--preset",
    "interval_s": "--interval",
    "hatanaka": "--hatanaka",
    "gzip": "--gzip",
}


def _parse_time(value: str, option: str) -> datetime:
    from datetime import datetime

    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise click.ClickException(
            f"{option} {value!r} is not an ISO-8601 time; give one with a timezone, "
            "e.g. 2026-09-18T00:00:00Z"
        ) from exc


@main.command()
@click.option(
    "--from",
    "start",
    required=True,
    help="Window start, ISO-8601 with timezone (e.g. 2026-09-18T00:00:00Z).",
)
@click.option("--to", "end", required=True, help="Window end, ISO-8601 with timezone.")
@click.option(
    "--preset",
    default="csrs-ppp",
    show_default=True,
    type=click.Choice(list(PRESETS)),  # the stable ids the API and UI use too
)
@click.option(
    "--interval", type=float, default=None, help="Observation interval, seconds (generic only)."
)
@click.option(
    "--hatanaka/--no-hatanaka",
    default=None,
    help="Hatanaka-compress the observation file (generic only).",
)
@click.option("--gzip/--no-gzip", default=None, help="gzip the output files (generic only).")
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False, path_type=Path))
@click.option("--overwrite", is_flag=True, help="Replace the files of an earlier export in --out.")
def export(
    start: str,
    end: str,
    preset: str,
    interval: float | None,
    hatanaka: bool | None,
    gzip: bool | None,
    out_dir: Path,
    overwrite: bool,
) -> None:
    """Export a raw-log window as RINEX for a PPP service or other post-processing."""
    from pydantic import ValidationError

    from mtrtk.rinex.export import (
        EXPORT_ERRORS,
        ExportContext,
        ExportRequest,
        export_to_dir,
        frequencies_from_firmware,
        header_from_settings,
    )

    try:
        request = ExportRequest(
            start=_parse_time(start, "--from"),
            end=_parse_time(end, "--to"),
            preset=preset,
            interval_s=interval,
            hatanaka=hatanaka,
            gzip=gzip,
        )
    except ValidationError as exc:
        raise click.ClickException(_validation_message(exc, EXPORT_OPTIONS)) from exc
    settings = _load_settings(ntrip_password="")
    if logging.getLogger().getEffectiveLevel() > logging.DEBUG:
        # The command prints what matters (progress, files, warnings) itself; the convbin argv
        # and the store's chatter are for `-v`.
        for chatty in ("mtrtk.rinex", "mtrtk.store"):
            logging.getLogger(chatty).setLevel(logging.WARNING)

    async def go() -> None:
        site = await _active_site_readonly(settings)
        # No live receiver state here (the daemon owns the receiver): the header's position
        # comes from the active site, the receiver version and convbin's frequency count from
        # the firmware the raw logs of the window recorded.
        firmware = await asyncio.to_thread(_window_firmware, settings, request)
        ctx = ExportContext(
            root=settings.data_dir,
            station_id=settings.station_id,
            country=settings.country,
            header=header_from_settings(settings, None, site, firmware=firmware),
            frequencies=frequencies_from_firmware(firmware),
            min_free_gb=settings.min_free_gb,
        )

        async def progress(p: float, msg: str | None) -> None:
            click.echo(f"[{p * 100:3.0f}%] {msg or ''}", err=True)

        try:
            result = await export_to_dir(
                request, ctx, out_dir, progress=progress, overwrite=overwrite
            )
        except EXPORT_ERRORS as exc:
            hint = "" if overwrite or "already holds" not in str(exc) else " (--overwrite)"
            raise click.ClickException(f"{exc}{hint}") from exc
        click.echo(f"wrote {out_dir}:")
        for f in result.files:
            click.echo(f"  {f['name']:<48} {f['bytes']:>12} bytes  {f['role']}")
        click.echo(
            f"{result.obs_epochs} observation epochs, {result.nav_messages} navigation messages, "
            f"RINEX {result.version}"
        )
        for w in result.warnings:
            click.echo(f"warning: {w}")

    asyncio.run(go())


async def _active_site_readonly(settings: Settings) -> Site | None:
    """The active site, read without writing to the database: `mtrtk export` runs next to a
    live daemon, and a DATA_DIR typo must not create an empty database. No database file is
    no active site."""
    from mtrtk.store.db import Database
    from mtrtk.store.repos import SitesRepo

    path = settings.data_dir / "mtrtk.db"
    if not await asyncio.to_thread(path.is_file):
        return None
    db = Database(path)
    try:
        await db.open(readonly=True)
    except (RuntimeError, OSError, sqlite3.Error) as exc:
        raise click.ClickException(f"cannot read the sites from {path}: {exc}") from exc
    try:
        return await SitesRepo(db).active()
    finally:
        await db.close()


def _window_firmware(settings: Settings, request: Any) -> str:
    """The firmware the newest raw log of the window recorded in its sidecar, or ""."""
    from mtrtk.rawlog.index import files_for_window
    from mtrtk.rawlog.writer import Sidecar

    hours = [
        lf
        for lf in files_for_window(settings.data_dir, request.start, request.end)
        if lf.station_id == settings.station_id
    ]
    for lf in reversed(hours):
        try:
            firmware = Sidecar.load(lf.sidecar_path).firmware
        except (OSError, TypeError, ValueError):
            continue
        if firmware:
            return firmware
    return ""


def _sigma(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


@main.command("ppp-import")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--prefer-frame",
    type=click.Choice(["itrf", "nad83"]),
    default="itrf",
    show_default=True,
    help="Which frame to take from an OPUS report (it gives both).",
)
@click.option("--save-site", "site_name", default=None, help="Save the result as a site, NAME.")
@click.option(
    "--activate", is_flag=True, help="Also make it the active site (used when BASE_MODE=fixed)."
)
def ppp_import(file: Path, prefer_frame: str, site_name: str | None, activate: bool) -> None:
    """Read a PPP result (CSRS-PPP .sum/.pos/.zip, AUSPOS SINEX, OPUS) and optionally save it."""
    from pydantic import ValidationError

    from mtrtk.rinex.ppp_result import PppParseError, parse_ppp_result
    from mtrtk.store.repos import SitesRepo
    from mtrtk.web.api.base import PPP_UPLOAD_LIMIT, SiteBody

    if activate and site_name is None:
        raise click.UsageError("--activate needs --save-site NAME")
    settings = _load_settings(ntrip_password="")
    try:
        with file.open("rb") as fh:
            content = fh.read(PPP_UPLOAD_LIMIT + 1)
    except OSError as exc:
        raise click.ClickException(f"cannot read {file}: {exc.strerror or exc}") from exc
    if len(content) > PPP_UPLOAD_LIMIT:
        raise click.ClickException(
            f"{file} is larger than {PPP_UPLOAD_LIMIT // (1024 * 1024)} MB: give the PPP "
            "result file itself, not the RINEX"
        )
    try:
        # Only matched as text, never executed. No event loop is running yet, so the CPU-bound
        # parse holds nothing up.
        result = parse_ppp_result(
            file.name, content, prefer_frame=prefer_frame, station_id=settings.station_id
        )
    except PppParseError as exc:
        raise click.ClickException(f"{exc.message}. {exc.hint}") from exc

    click.echo(f"source   {result.source} ({result.format})")
    click.echo(f"frame    {result.frame}{f' @ {result.epoch}' if result.epoch else ''}")
    for axis, value, sigma in (
        ("X", result.x, result.sigma_x),
        ("Y", result.y, result.sigma_y),
        ("Z", result.z, result.sigma_z),
    ):
        click.echo(f"{axis}        {value:15.4f} m   1σ {_sigma(sigma)} m")
    click.echo(f"lat/lon  {result.lat:.9f} {result.lon:.9f}   h {result.height_m:.4f} m")
    for note in result.notes:
        click.echo(f"note: {note}")
    if site_name is None:
        click.echo(f"save it with --save-site {result.suggested_site_name(settings.station_id)}")
        return

    # The same body `POST /api/base/sites` takes when the web UI saves an imported result.
    try:
        body = SiteBody(
            name=site_name,
            x=result.x,
            y=result.y,
            z=result.z,
            sigma_x=result.sigma_x,
            sigma_y=result.sigma_y,
            sigma_z=result.sigma_z,
            source=result.source,
            frame=result.frame,
            epoch=result.epoch,
            notes=f"imported from {file.name}",
        )
    except ValidationError as exc:
        raise click.ClickException(_validation_message(exc, {"name": "--save-site"})) from exc

    async def go(db: Database) -> None:
        repo = SitesRepo(db)
        try:
            site = await repo.add(body.to_site())
        except ValueError as exc:  # the name is taken
            raise click.ClickException(str(exc)) from exc
        click.echo(f"saved site {site.name}")
        if activate:
            # What `mtrtk sites activate` and the API do with no base manager in this process:
            # the row is the durable part, and a running base picks it up within 10 s.
            site = await repo.activate(site.name)
            click.echo(
                f"{site.name} is now the active site; set BASE_MODE=fixed to use it at startup"
            )

    _with_db(go)


@main.command()
@click.option(
    "--rover",
    "rover_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Rover raw log (UBX from any receiver) or RINEX observation file.",
)
@click.option(
    "--session", "session_id", type=int, help="Rover session id: its time range over the raw logs."
)
@click.option("--from", "start", help="Rover window start, ISO-8601 with timezone.")
@click.option("--to", "end", help="Rover window end, ISO-8601 with timezone.")
@click.option(
    "--base",
    "base_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Base raw log (UBX) or RINEX observation file.",
)
@click.option(
    "--base-nav",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Base RINEX navigation file (with a RINEX --base).",
)
@click.option(
    "--base-url",
    help="Remote mtrtk base, e.g. http://100.100.50.10:8080 (its raw logs are fetched).",
)
@click.option(
    "--base-password",
    envvar="MTRTK_BASE_PASSWORD",
    help="The remote base's WEB_PASSWORD (or MTRTK_BASE_PASSWORD).",
)
@click.option("--base-logs", is_flag=True, help="Use this host's own raw logs as the base.")
@click.option("--site", help="Base position from this host's site NAME.")
@click.option("--base-xyz", nargs=3, type=float, default=None, help="Base ECEF X Y Z, metres.")
@click.option("--no-events", is_flag=True, help="Skip TIM-TM2 camera event interpolation.")
@click.option("--qzss", is_flag=True, help="Include QZSS.")
@click.option(
    "--set", "overrides", multiple=True, help="rnx2rtkp option override key=value (repeatable)."
)
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False, path_type=Path))
def ppk(
    rover_file: Path | None,
    session_id: int | None,
    start: str | None,
    end: str | None,
    base_file: Path | None,
    base_nav: Path | None,
    base_url: str | None,
    base_password: str | None,
    base_logs: bool,
    site: str | None,
    base_xyz: tuple[float, float, float] | None,
    no_events: bool,
    qzss: bool,
    overrides: tuple[str, ...],
    out_dir: Path,
) -> None:
    """Post-process rover raw data against a base (PPK) with RTKLIB."""
    from pydantic import ValidationError

    from mtrtk.ppk.pipeline import (
        BaseSource,
        PpkContext,
        PpkError,
        PpkRequest,
        RoverSource,
        run_ppk,
    )
    from mtrtk.rinex.export import header_from_settings
    from mtrtk.rinex.rinexhdr import sniff_format

    rovers = [rover_file is not None, session_id is not None, bool(start or end)]
    if sum(rovers) == 0:
        raise click.UsageError("give a rover source: --rover FILE, --session ID, or --from/--to")
    if sum(rovers) > 1:
        raise click.UsageError("give one rover source: --rover, --session or --from/--to")
    if bool(start) != bool(end):
        raise click.UsageError("a rover window needs both --from and --to")
    bases = [base_file is not None, base_url is not None, base_logs]
    if sum(bases) == 0:
        raise click.UsageError("give a base source: --base FILE, --base-url URL, or --base-logs")
    if sum(bases) > 1:
        raise click.UsageError("give one base source: --base, --base-url or --base-logs")
    conf: dict[str, str] = {}
    for item in overrides:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise click.UsageError(f"--set takes key=value, got {item!r}")
        conf[key.strip()] = value.strip()
    try:
        if rover_file is not None:
            rover = RoverSource(kind="upload", path=rover_file)
        elif session_id is not None:
            rover = RoverSource(kind="session", session_id=session_id)
        else:
            assert start is not None and end is not None
            rover = RoverSource(
                kind="window", start=_parse_time(start, "--from"), end=_parse_time(end, "--to")
            )
        if base_file is not None:
            is_obs = sniff_format(base_file) == "rinex-obs"
            base = BaseSource(
                kind="upload",
                path_ubx=None if is_obs else base_file,
                path_obs=base_file if is_obs else None,
                path_nav=base_nav,
            )
        elif base_url is not None:
            base = BaseSource(kind="remote", url=base_url, password=base_password or None)
        else:
            base = BaseSource(kind="local")
        request = PpkRequest(
            rover=rover,
            base=base,
            base_site=site,
            base_xyz=(base_xyz[0], base_xyz[1], base_xyz[2]) if base_xyz else None,
            events=not no_events,
            include_qzss=qzss,
            conf_overrides=conf,
        )
    except ValidationError as exc:
        raise click.ClickException(_validation_message(exc)) from exc
    except OSError as exc:
        raise click.ClickException(f"cannot read {base_file}: {exc.strerror or exc}") from exc
    settings = _load_settings(ntrip_password="")
    if logging.getLogger().getEffectiveLevel() > logging.DEBUG:
        for chatty in ("mtrtk.rinex", "mtrtk.store", "mtrtk.ppk", "httpx"):
            logging.getLogger(chatty).setLevel(logging.WARNING)

    async def go() -> None:
        from mtrtk.store.db import Database

        # Read-only, next to a live daemon; no database file is no sites and no sessions.
        path = settings.data_dir / "mtrtk.db"
        db: Database | None = None
        if await asyncio.to_thread(path.is_file):
            db = Database(path)
            try:
                await db.open(readonly=True)
            except (RuntimeError, OSError, sqlite3.Error) as exc:
                raise click.ClickException(f"cannot read {path}: {exc}") from exc
        try:
            ctx = PpkContext(
                root=settings.data_dir,
                station_id=settings.station_id,
                country=settings.country,
                header=header_from_settings(settings, None, None),
                db=db,
                min_free_gb=settings.min_free_gb,
            )

            async def progress(p: float, msg: str | None) -> None:
                click.echo(f"[{p * 100:3.0f}%] {msg or ''}", err=True)

            try:
                result = await run_ppk(request, ctx, out_dir, progress=progress)
            except PpkError as exc:
                raise click.ClickException(str(exc)) from exc
        finally:
            if db is not None:
                await db.close()
        s = result["summary"]
        click.echo(
            f"{s['epochs']} epochs · fixed {s['fixed_pct']:.1f}% · float {s['float_pct']:.1f}% "
            f"· single {s['single_pct']:.1f}% · base {result['inputs']['base_xyz_source']}"
        )
        if result["events"]["total"]:
            click.echo(
                f"events: {result['events']['ok']} of {result['events']['total']} positioned"
            )
        for w in result["warnings"]:
            click.echo(f"warning: {w}")
        click.echo(f"outputs in {out_dir}")

    asyncio.run(go())


# ----------------------------------------------------------------------------------- ins
INS_CONNECT_TIMEOUT_S = 10.0


@main.group()
def ins() -> None:
    """INS rover tools (ROVER_DRIVER=sbg_ellipse | vectornav on INS_PORT).

    Run them with the daemon stopped: they open INS_PORT themselves, exclusively, so beside a
    running daemon they fail with "in use by another process".
    """


def _ins_settings() -> Settings:
    settings = _load_settings(ntrip_password="")
    if settings.rover_driver == "ublox":
        raise click.ClickException(
            "ROVER_DRIVER=ublox: set ROVER_DRIVER=sbg_ellipse or vectornav, and INS_PORT"
        )
    if settings.ins_port is None:
        raise click.ClickException(
            "INS_PORT is not set: the INS tools talk to the unit on INS_PORT "
            "(a MTRTK_SOURCE=file: capture is replayed by `mtrtk run` only)"
        )
    if settings.source_is_file:
        # build_ins would replay the capture: these tools query and configure the unit itself.
        settings = settings.model_copy(update={"mtrtk_source": "auto"})
    return settings


async def _ins_session(
    settings: Settings, body: Callable[[Any, Any], Awaitable[None]], *, epochs: bool = False
) -> None:
    """Open INS_PORT with the vendor stack (no configure on connect, no raw capture), feed the
    state adapter, run *body(bundle, epochs)*, close. *epochs* subscribes `state.epoch` before
    the port opens, so no epoch is missed (else None is passed)."""
    from mtrtk.core.bus import Bus
    from mtrtk.rover.drivers.factory import build_ins

    bus = Bus()
    bundle = build_ins(settings, bus, configure_on_connect=False, capture=False)
    errors = bus.subscribe("receiver.error", maxsize=20)
    frames = bus.subscribe(bundle.raw_topic, maxsize=5000)
    epoch_sub = bus.subscribe("state.epoch", maxsize=50) if epochs else None
    stop = asyncio.Event()

    async def pump() -> None:
        async for _, frame in frames:
            bundle.adapter.handle(frame)

    pump_task = asyncio.create_task(pump(), name="ins-state")
    run_task = asyncio.create_task(bundle.controller.run(stop), name="ins-controller")
    loop = asyncio.get_running_loop()
    try:
        deadline = loop.time() + INS_CONNECT_TIMEOUT_S
        while not bundle.connected:
            if run_task.done() or loop.time() > deadline:
                last = ""
                while not errors.queue.empty():
                    last = str(errors.queue.get_nowait()[1])
                raise click.ClickException(
                    f"cannot open {settings.ins_port}: {last or 'no connection'}"
                )
            await asyncio.sleep(0.05)
        try:
            await body(bundle, epoch_sub)
        except (ConnectionError, TimeoutError) as exc:  # the link dropped, or the unit went quiet
            reason = str(exc) or type(exc).__name__
            raise click.ClickException(f"lost {settings.ins_port}: {reason}") from exc
    finally:
        stop.set()
        frames.close()
        if epoch_sub is not None:
            bus.unsubscribe(epoch_sub)
        await asyncio.gather(run_task, pump_task, return_exceptions=True)
        bus.unsubscribe(errors)
        bundle.close()


def _ins_value(value: object) -> str:
    import json

    if value is None:
        return "-"
    if isinstance(value, dict | list):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _echo_ins_info(bundle: Any) -> None:
    s = bundle.settings
    click.echo(f"vendor    {bundle.vendor} ({s.rover_driver}) on {s.ins_port} @ {s.ins_baud}")
    info = bundle.info_dict()
    if info is None:
        click.echo("identity  the unit did not answer")
        return
    for key in ("model", "serial", "firmware", "hardware"):
        click.echo(f"{key:<9} {info.get(key) or '-'}")


def _fail_unless_identified(bundle: Any) -> None:
    """Exit 1 when the unit never told who it is: the port opened, but nothing on it answered
    (a wrong INS_BAUD, the port in another protocol), and configure touched nothing. A
    VectorNav whose model register timed out keeps an empty identity, so no model and no
    serial counts as silence too."""
    info = bundle.info_dict()
    if info is None or not (info.get("model") or info.get("serial")):
        s = bundle.settings
        raise click.ClickException(
            f"the unit on {s.ins_port} did not answer: check INS_BAUD ({s.ins_baud}) and that "
            "the port speaks sbgECom (Ellipse Port A) or VectorNav binary"
        )


def _echo_ins_report(bundle: Any, *, dry_run: bool = False) -> None:
    report = bundle.report_dict()
    if report is None:
        click.echo("no configuration report")
        return
    if dry_run:
        pending = [i for i in report["items"] if i["state"] in ("pending", "mismatched")]
        if not pending:
            click.echo("nothing to write: the unit matches the profile")
        for item in pending:
            wanted = _ins_value(item["wanted"]) if item["wanted"] is not None else "(profile)"
            click.echo(f"would write {item['name']}: {_ins_value(item['current'])} -> {wanted}")
    else:
        click.echo(f"{'STATE':<12} {'ITEM':<28} CURRENT")
        for item in report["items"]:
            click.echo(f"{item['state']:<12} {item['name']:<28} {_ins_value(item['current'])}")
    for message in report["errors"]:
        click.echo(f"error: {message}")
    for note in report["notes"]:
        click.echo(f"note: {note}")
    for arm in bundle.lever_arms():
        conf, back = _ins_value(arm["configured"]), _ins_value(arm["read_back"])
        click.echo(f"lever arm {arm['name']}: configured {conf}, unit {back}")


@ins.command("info")
def ins_info() -> None:
    """Read the unit's identity and current configuration (writes nothing but queries)."""
    settings = _ins_settings()

    async def body(bundle: Any, _epochs: Any) -> None:
        await bundle.configure(apply=False)
        _echo_ins_info(bundle)
        _echo_ins_report(bundle)
        _fail_unless_identified(bundle)

    asyncio.run(_ins_session(settings, body))


@ins.command("config")
@click.option("--apply", "do_apply", is_flag=True, help="Write the profile, then read it back.")
@click.option("--dry-run", is_flag=True, help="List what --apply would write; write nothing.")
def ins_config(do_apply: bool, dry_run: bool) -> None:
    """Show the unit's configuration against the mtrtk profile, or apply it.

    --apply writes the profile and verifies it by reading back. It is saved to flash only with
    INS_APPLY_CONFIG=1, on either vendor (otherwise it lasts until the unit restarts).
    """
    if do_apply and dry_run:
        raise click.UsageError("use either --apply or --dry-run")
    settings = _ins_settings()

    async def body(bundle: Any, _epochs: Any) -> None:
        await bundle.configure(apply=do_apply)
        _echo_ins_info(bundle)
        _echo_ins_report(bundle, dry_run=dry_run)
        _fail_unless_identified(bundle)
        if do_apply:
            saved = (bundle.report_dict() or {}).get("saved")
            if saved:
                click.echo("saved to flash")
            elif not settings.ins_apply_config:
                click.echo(
                    "not saved to flash (INS_APPLY_CONFIG=0): written to the unit, in effect at "
                    "the latest after a save and reboot, and lost when the unit restarts. Set "
                    "INS_APPLY_CONFIG=1 and the next connect saves them to flash, although they "
                    "will read back as unchanged"
                )
            else:
                click.echo("not saved to flash (see the notes and errors above)")

    asyncio.run(_ins_session(settings, body))


@ins.command("monitor")
@click.option("--seconds", default=0.0, type=float, help="Stop after this long (0 = Ctrl-C).")
def ins_monitor(seconds: float) -> None:
    """One line per navigation epoch: INS mode, position, heading, fix. Writes nothing."""
    settings = _ins_settings()

    async def body(bundle: Any, sub: Any) -> None:
        async def lines() -> None:
            async for _, s in sub:
                ins_state, att = s.ins, s.attitude
                utc = s.time.utc.strftime("%H:%M:%S.%f")[:-4] if s.time.utc else "--:--:--"
                mode = (ins_state.mode_name or "-") if ins_state else "-"
                lat = f"{s.position.lat:.7f}" if s.position.lat is not None else "-"
                lon = f"{s.position.lon:.7f}" if s.position.lon is not None else "-"
                hdg = att.heading_deg if att is not None else None
                gnss = (ins_state.gnss_fix_name or "-") if ins_state else "-"
                click.echo(
                    f"{utc} {mode:<14} lat {lat} lon {lon} "
                    f"hdg {f'{hdg:.1f}' if hdg is not None else '-'} "
                    f"fix {s.fix.fix_type_name} gnss {gnss}"
                )

        task = asyncio.create_task(lines())
        try:
            if seconds > 0:
                await asyncio.sleep(seconds)
            else:
                await asyncio.Event().wait()  # until Ctrl-C
        finally:
            sub.close()
            await asyncio.gather(task, return_exceptions=True)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_ins_session(settings, body, epochs=True))


@main.command()
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--with-secrets", is_flag=True, help="Keep passwords/tokens in the archived .env.")
def backup(out_path: Path, with_secrets: bool) -> None:
    """Archive the database, sites and (masked) .env; safe while the daemon runs."""
    from mtrtk.backup import BackupError, create_backup

    settings = _load_settings(ntrip_password="")
    try:
        path = create_backup(settings, out_path, with_secrets=with_secrets)
    except (BackupError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"wrote {path} ({path.stat().st_size} bytes)")
    db_file, env_file = settings.data_dir / "mtrtk.db", settings.mtrtk_env_file.absolute()
    if not db_file.exists():
        click.echo(f"note: no database at {db_file}; only .env archived ({env_file})")
    elif not env_file.is_file():
        click.echo(f"note: no .env at {env_file}; only the database archived")
    else:
        click.echo(f"archived {db_file} and {env_file}")
    if with_secrets:
        click.echo("this archive holds the station's passwords: keep it private")


@main.command()
@click.argument("archive", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--force", is_flag=True, help="Overwrite an existing database (a copy is kept).")
def restore(archive: Path, force: bool) -> None:
    """Restore a backup into DATA_DIR (stop the daemon first)."""
    from mtrtk.backup import BackupError, env_differences, restore_backup

    settings = _load_settings(ntrip_password="")
    try:
        info = restore_backup(archive, settings, force=force)
    except (BackupError, FileExistsError) as exc:
        raise click.ClickException(str(exc)) from exc
    manifest = info["manifest"]
    created = manifest.get("created_utc", "an unknown time")
    if info["db"]:
        n = info["sites"]
        click.echo(f"restored database from {created} ({n} site{'' if n == 1 else 's'})")
    else:
        click.echo(f"the backup from {created} holds no database; nothing was replaced")
    if info["previous_db"]:
        click.echo(f"the database it replaced is kept as {info['previous_db']}")
    if info["env_path"] is None:
        click.echo("the backup holds no .env")
        return
    secrets = "with secrets" if manifest.get("with_secrets") else "secrets masked"
    click.echo(f"the archived .env ({secrets}) is in {info['env_path']}; it was not applied")
    env_file = settings.mtrtk_env_file
    # Differences only, and never a secret's value: scrollback is no place for passwords.
    lines = env_differences(info["env"], env_file)
    if not lines:
        click.echo(f"it matches {env_file}")
        return
    click.echo(f"merge by hand what differs from {env_file}:")
    for line in lines:
        click.echo(f"  {line}")
