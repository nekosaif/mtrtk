"""mtrtk command line interface."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import click

from mtrtk import __version__

if TYPE_CHECKING:
    from mtrtk.config import Settings


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="mtrtk")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
def main(verbose: bool) -> None:
    """mtrtk - GNSS RTK/PPK/PPP toolkit for ZED-F9P base stations and rovers."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _load_settings(**overrides: object) -> Settings:
    from pydantic import ValidationError

    from mtrtk.config import Settings

    try:
        return Settings(**{k: v for k, v in overrides.items() if v is not None})  # type: ignore[arg-type]
    except ValidationError as exc:
        raise click.ClickException(f"invalid configuration:\n{exc}") from exc


def _run_daemon(settings: Settings) -> None:
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
