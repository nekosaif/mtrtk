"""mtrtk command line interface."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

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
        # `-v` is for mtrtk's own debug output. aiosqlite logs every statement it executes and
        # asyncio, httpx and httpcore a line per operation, which buries it several times over.
        for noisy in ("aiosqlite", "asyncio", "httpx", "httpcore", "websockets"):
            logging.getLogger(noisy).setLevel(logging.INFO)


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


@main.command()
def doctor() -> None:
    """Check receiver access, Tailscale, RTKLIB and disk."""
    from mtrtk.doctor import run_checks

    failed = False
    for check in run_checks(_load_settings(ntrip_password="")):
        mark = {True: "OK  ", False: "FAIL", None: "WARN"}[check.ok]
        failed |= check.ok is False
        click.echo(f"[{mark}] {check.name:<10} {check.detail}")
    if failed:
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


def _validation_message(exc: ValidationError) -> str:
    """The validators' own messages, without pydantic's framing or the input they refused."""
    return "; ".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())


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
        frequencies_from_state,
        header_from_settings,
    )
    from mtrtk.store.repos import SitesRepo

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
        raise click.ClickException(_validation_message(exc)) from exc
    settings = _load_settings(ntrip_password="")

    async def go(db: Database) -> None:
        site = await SitesRepo(db).active()
        # No live receiver state here (the daemon owns the receiver): the header's position
        # comes from the active site, and convbin takes the two frequencies HPG 1.13 has.
        ctx = ExportContext(
            root=settings.data_dir,
            station_id=settings.station_id,
            country=settings.country,
            header=header_from_settings(settings, None, site),
            frequencies=frequencies_from_state(None),
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

    _with_db(go)


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
            f"{file} is larger than 20 MB: give the PPP result file itself, not the RINEX"
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
        raise click.ClickException(_validation_message(exc)) from exc

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
