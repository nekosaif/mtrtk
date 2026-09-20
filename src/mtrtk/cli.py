"""mtrtk command line interface."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import click
import httpx

from mtrtk import __version__

HEALTHCHECK_TIMEOUT_S = 3.0  # the container healthcheck runs every 30 s; it must never hang

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

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

    try:
        return Settings(**{k: v for k, v in overrides.items() if v is not None})  # type: ignore[arg-type]
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
