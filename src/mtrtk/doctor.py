"""Environment checks for `mtrtk doctor`."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass

from mtrtk.config import Role, Settings
from mtrtk.core.exposure import tailscale_ipv4
from mtrtk.core.source import find_ublox_port

# Above 50 Hz, or with the unit's raw GNSS stream on top, 115200 baud cannot carry an INS's
# output: the same threshold the SBG configuration holds its fast outputs and flash save at.
INS_FAST_HZ = 50
INS_MIN_BAUD = 460800


@dataclass
class Check:
    name: str
    ok: bool | None  # None = warning
    detail: str


def run_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    v = sys.version_info
    checks.append(Check("python", v >= (3, 12), f"{v.major}.{v.minor}.{v.micro}"))

    if settings.role is Role.ROVER and settings.rover_driver != "ublox":
        checks += _ins_checks(settings)
    elif settings.source_is_file:
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


def _ins_checks(settings: Settings) -> list[Check]:
    """An INS rover reads INS_PORT, not a u-blox receiver; a replay reads only its file."""
    checks: list[Check] = []
    if settings.source_is_file:
        path = settings.source_path
        checks.append(Check("receiver", path.exists(), f"replay file {path}"))
        return checks
    port = settings.ins_port or ""
    if not os.path.exists(port):
        checks.append(Check("ins_port", False, f"{port or 'INS_PORT'} does not exist"))
    else:
        usable = os.access(port, os.R_OK | os.W_OK)
        state = "read/write ok" if usable else "no permission: add user to dialout"
        checks.append(Check("ins_port", usable, f"{port} ({state}, {settings.rover_driver})"))
    heavy = settings.ins_output_hz > INS_FAST_HZ or settings.ins_raw_gnss
    if heavy and settings.ins_baud < INS_MIN_BAUD:
        why = (
            f"INS_OUTPUT_HZ={settings.ins_output_hz}"
            if settings.ins_output_hz > INS_FAST_HZ
            else "INS_RAW_GNSS=1"
        )
        checks.append(
            Check(
                "ins_baud",
                None,
                f"INS_BAUD={settings.ins_baud} is low for {why}: set the unit's port to "
                f"{INS_MIN_BAUD} or more (sbgCenter / VectorNav Control Center), then INS_BAUD",
            )
        )
    if settings.rover_driver == "sbg_ellipse" and settings.ntrip_url and not settings.ins_rtcm_port:
        checks.append(
            Check(
                "ins_rtcm",
                None,
                "RTCM on same port unverified: corrections go to the Ellipse's main port; wire "
                "Port B to a second serial device and set INS_RTCM_PORT for the documented input",
            )
        )
    return checks
