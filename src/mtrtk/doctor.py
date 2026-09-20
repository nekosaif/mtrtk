"""Environment checks for `mtrtk doctor`."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass

from mtrtk.config import Settings
from mtrtk.core.exposure import tailscale_ipv4
from mtrtk.core.source import find_ublox_port


@dataclass
class Check:
    name: str
    ok: bool | None  # None = warning
    detail: str


def run_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    v = sys.version_info
    checks.append(Check("python", v >= (3, 12), f"{v.major}.{v.minor}.{v.micro}"))

    if settings.source_is_file:
        path = settings.source_path
        checks.append(Check("receiver", path.exists(), f"replay file {path}"))
    else:
        port = settings.mtrtk_source if settings.mtrtk_source != "auto" else find_ublox_port()
        if port is None:
            checks.append(
                Check(
                    "receiver",
                    False,
                    "no u-blox receiver found (check USB cable, /dev/serial/by-id)",
                )
            )
        else:
            readable = os.access(port, os.R_OK | os.W_OK)
            state = "read/write ok" if readable else "no permission: add user to dialout"
            checks.append(Check("receiver", readable, f"{port} ({state})"))

    ts_ip = tailscale_ipv4()
    needs_ts = "tailscale" in (settings.ntrip_bind, settings.web_bind)
    checks.append(
        Check(
            "tailscale",
            (ts_ip is not None) if needs_ts else None,
            ts_ip or "tailscale0 has no IPv4 (is tailscaled running?)",
        )
    )

    missing = [tool for tool in ("convbin", "rnx2rtkp") if shutil.which(tool) is None]
    checks.append(
        Check(
            "rtklib",
            None if missing else True,
            "missing: " + ", ".join(missing) if missing else "convbin, rnx2rtkp found",
        )
    )

    data_dir = settings.data_dir
    if data_dir.exists():
        usage = shutil.disk_usage(data_dir)
        free_gb = usage.free / 1e9
        checks.append(
            Check(
                "data_dir",
                free_gb >= settings.min_free_gb,
                f"{data_dir}: {free_gb:.1f} GB free (min {settings.min_free_gb})",
            )
        )
    else:
        checks.append(
            Check("data_dir", None, f"{data_dir} does not exist yet (created on first run)")
        )
    return checks
