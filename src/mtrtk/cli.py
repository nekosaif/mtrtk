"""mtrtk command line interface."""

from __future__ import annotations

from pathlib import Path

import click

from mtrtk import __version__


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="mtrtk")
def main() -> None:
    """mtrtk - GNSS RTK/PPK/PPP toolkit for ZED-F9P base stations and rovers."""


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
