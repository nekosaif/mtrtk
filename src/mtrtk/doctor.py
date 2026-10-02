"""Environment checks for `mtrtk doctor`.

Read-only: nothing here changes the host or a receiver's configuration. The receiver device is
only opened with `--probe`, and then only to poll MON-VER (a query, never a CFG message). Every
helper that shells out or asks psutil is a module-level function, so tests patch it.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psutil

from mtrtk.config import Role, Settings
from mtrtk.core.exposure import resolve_bind, tailscale_ipv4
from mtrtk.core.source import find_ublox_port

# Above 50 Hz, or with the unit's raw GNSS stream on top, 115200 baud cannot carry an INS's
# output: the same threshold the SBG configuration holds its fast outputs and flash save at.
INS_FAST_HZ = 50
INS_MIN_BAUD = 460800

# The same rule `install.sh` installs from udev/99-mtrtk-ublox.rules.
UDEV_RULE = (
    'ACTION=="add|change", SUBSYSTEM=="usb", ATTRS{idVendor}=="1546", ENV{ID_MM_DEVICE_IGNORE}="1"'
)
UDEV_RULES_DIR = Path("/etc/udev/rules.d")
UDEV_RULE_PATH = UDEV_RULES_DIR / "99-mtrtk-ublox.rules"
UBLOX_VID = "1546"
MIN_RECOMMENDED_FW = (1, 32)
CURRENT_FW = "1.51"
PROBE_TIMEOUT_S = 3.0
COMMAND_TIMEOUT_S = 5.0
# Tailscale hands out CGNAT IPv4 and this ULA prefix; a bind to one is as private as `tailscale`.
TAILNET_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
USB_TTY_PREFIXES = ("/dev/ttyACM", "/dev/ttyUSB")
# Checks whose `ok=None` is information rather than a warning: the table marks them INFO.
INFO_CHECKS = frozenset({"docker"})
DIALOUT_FIX = "sudo usermod -aG dialout $USER, then log out and back in"
# A listener on one of these takes the port on every address, so it clashes with any bind.
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})
NO_SYSTEMD = "no systemd here: inside a container?"


@dataclass
class Check:
    name: str
    ok: bool | None  # True = OK, False = FAIL, None = WARN / informational
    detail: str
    fix: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PortOwner:
    pid: int | None  # None: a listener whose process this user cannot see
    name: str | None
    cmdline: tuple[str, ...] = ()


class ProbeError(Exception):
    """The receiver could not be opened or read (as opposed to a receiver that stays silent)."""


# ----------------------------------------------------------------- host probes (patched in tests)


def _command_output(args: list[str]) -> str:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def _service_active(name: str) -> bool | None:
    """True / False from systemd; None when there is no systemd to ask (a container)."""
    out = _command_output(["systemctl", "is-active", name])
    if not out:
        return None
    return out == "active"


def _ntp_synchronized() -> bool | None:
    """None when neither timedatectl nor systemctl can say (a container, a non-systemd host)."""
    out = _command_output(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if out:
        return out.lower() == "yes"
    # No timedatectl: a running time daemon is the next best.
    states = [_service_active(name) for name in ("chrony", "chronyd", "ntp", "ntpd")]
    if any(state is True for state in states):
        return True
    if any(state is None for state in states):
        return None
    return False


def _udev_rule_present() -> bool:
    """Any rules file that tells ModemManager to leave u-blox devices alone."""
    try:
        files = sorted(UDEV_RULES_DIR.glob("*.rules"))
    except OSError:
        return False
    for path in files:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if any("ID_MM_DEVICE_IGNORE" in line and UBLOX_VID in line for line in text.splitlines()):
            return True
    return False


def _addresses_clash(listener: str, hosts: frozenset[str]) -> bool:
    """Whether a listener on *listener* stops a bind to one of *hosts* on the same port."""
    return listener in WILDCARD_HOSTS or bool(hosts & WILDCARD_HOSTS) or listener in hosts


def _port_owner(port: int, hosts: frozenset[str] | None = None) -> PortOwner | None:
    """The process listening on TCP *port* in the way of a bind to *hosts* (None: any address).

    None when nothing listens there (or only on other specific addresses).
    """
    try:
        conns = psutil.net_connections(kind="tcp")
    except (psutil.Error, OSError):
        return None
    for conn in conns:
        if conn.status != psutil.CONN_LISTEN or not conn.laddr or conn.laddr.port != port:
            continue
        if hosts is not None and not _addresses_clash(conn.laddr.ip, hosts):
            continue
        if not conn.pid:
            return PortOwner(pid=None, name=None)
        try:
            proc = psutil.Process(conn.pid)
            return PortOwner(pid=conn.pid, name=proc.name(), cmdline=tuple(proc.cmdline()))
        except (psutil.Error, OSError):
            return PortOwner(pid=conn.pid, name=None)
    return None


def _parse_fw(fw: str) -> tuple[int, int] | None:
    """`"HPG 1.13"` -> (1, 13)."""
    try:
        major, minor = fw.split()[-1].split(".")[:2]
        return int(major), int(minor)
    except (ValueError, IndexError):
        return None


def _read_firmware(read: Callable[[], bytes], timeout_s: float) -> str | None:
    """FWVER from the first MON-VER in a byte stream; None when none arrives in *timeout_s*."""
    from mtrtk.core.frames import Framer
    from mtrtk.core.statestore import StateStore

    framer, store = Framer(), StateStore()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        data = read()
        if not data:
            time.sleep(0.01)
            continue
        for frame in framer.feed(data):
            if frame.identity == "MON-VER":
                store.apply(frame)
                return store.state.firmware.fw_version or None
    return None


def _probe_firmware(port: str, baud: int) -> str | None:
    """Poll MON-VER (only with --probe). The poll is a query: no configuration is written.

    None when no MON-VER arrives in time; ProbeError when the port cannot be opened or read.
    """
    import serial
    from pyubx2 import POLL, UBXMessage

    poll = UBXMessage("MON", "MON-VER", POLL).serialize()
    try:
        ser = serial.Serial(port, baud, timeout=0.2)
    except (serial.SerialException, OSError) as exc:
        raise ProbeError(f"could not open {port}: {exc}") from exc
    with ser:
        try:
            ser.reset_input_buffer()
            ser.write(poll)
            return _read_firmware(lambda: bytes(ser.read(4096)), PROBE_TIMEOUT_S)
        except (serial.SerialException, OSError) as exc:
            raise ProbeError(f"reading {port} failed: {exc}") from exc


# ----------------------------------------------------------------- classification helpers


def _bind_scope(bind: str) -> str:
    """Who can reach a listener: `local`, `tailnet`, `lan` or `all`."""
    if bind == "tailscale":
        return "tailnet"
    if bind in ("lan", "all"):
        return bind
    try:
        ip = ipaddress.ip_address(bind)
    except ValueError:
        return "all"  # Settings validates binds; an unknown one is treated as the widest
    if ip.is_loopback:
        return "local"
    if any(ip in net for net in TAILNET_NETS):
        return "tailnet"
    if ip.is_unspecified or not ip.is_private:
        return "all"
    return "lan"


def _is_usb_serial(path: str) -> bool:
    if path.startswith("/dev/serial/by-id/"):
        return True
    return os.path.realpath(path).startswith(USB_TTY_PREFIXES)


def _device_kind(path: str) -> str:
    real = os.path.realpath(path)
    if real.startswith("/dev/pts/"):
        return "pty"
    if _is_usb_serial(path):
        return "USB serial"
    try:
        return "tty" if stat.S_ISCHR(os.stat(real).st_mode) else "file"
    except OSError:
        return "device"


def _is_mtrtk(owner: Any) -> bool:
    """The mtrtk program itself: `mtrtk ...`, `python .../bin/mtrtk ...` or `python -m mtrtk`.

    A path containing `mtrtk` (a dev server started from the repo, str2str writing into it) is
    not the daemon.
    """
    if getattr(owner, "name", None) == "mtrtk":
        return True
    argv = list(getattr(owner, "cmdline", ()) or ())
    if not argv:
        return False
    program = os.path.basename(argv[0])
    if program == "mtrtk":
        return True
    if not program.startswith("python"):
        return False
    if len(argv) > 1 and os.path.basename(argv[1]) == "mtrtk":
        return True
    return any(a == "-m" and b == "mtrtk" for a, b in zip(argv, argv[1:], strict=False))


def _listening_ports(settings: Settings) -> list[tuple[int, str]]:
    """The fixed TCP ports this role's daemon listens on, with their bind modes.

    Port 0 (ephemeral) is left out: there is nothing to check.
    """
    ports = [(settings.web_port, settings.web_bind)]
    if settings.role is Role.BASE:
        ports.insert(0, (settings.ntrip_port, settings.ntrip_bind))
    elif settings.nmea_tcp_port > 0:
        ports.append((settings.nmea_tcp_port, settings.nmea_tcp_bind))
    return [(p, bind) for p, bind in ports if p > 0]


def _bind_hosts(bind: str) -> frozenset[str] | None:
    """The address a bind mode listens on; None when unknown yet (any listener may clash)."""
    try:
        # This module's tailscale_ipv4 (patchable) rather than the one resolve_bind looks up.
        host = tailscale_ipv4() if bind == "tailscale" else resolve_bind(bind)
    except ValueError:
        return None
    return None if host is None else frozenset({host})


# ----------------------------------------------------------------- the checks


def run_checks(settings: Settings, *, probe_receiver: bool = False) -> list[Check]:
    checks: list[Check] = []
    v = sys.version_info
    checks.append(Check("python", v >= (3, 12), f"{v.major}.{v.minor}.{v.micro}"))

    listening = _listening_ports(settings)
    owners = {port: _port_owner(port, _bind_hosts(bind)) for port, bind in listening}

    port: str | None = None
    if settings.role is Role.ROVER and settings.rover_driver != "ublox":
        checks += _ins_checks(settings)
    elif settings.source_is_file:
        path = settings.source_path
        checks.append(Check("receiver", path.exists(), f"replay file {path}"))
    else:
        port = settings.mtrtk_source if settings.mtrtk_source != "auto" else find_ublox_port()
        daemon = _daemon_holder([p for p, _ in listening]) if probe_receiver else None
        checks += _receiver_checks(settings, port, probe_receiver, daemon)

    checks.append(_modemmanager_check(settings, port))
    checks.append(_time_sync_check())
    checks.append(_tailscale_check(settings))
    checks.append(_ports_check(owners))

    missing = [tool for tool in ("convbin", "rnx2rtkp") if shutil.which(tool) is None]
    checks.append(
        Check(
            "rtklib",
            None if missing else True,
            (
                f"missing: {', '.join(missing)} (needed for RINEX export and PPK outside Docker)"
                if missing
                else "convbin, rnx2rtkp found"
            ),
            fix="sudo apt install rtklib" if missing else None,
        )
    )

    if shutil.which("docker"):
        docker = _command_output(["docker", "--version"]) or "docker present"
    else:
        docker = "docker not installed (the native install via install.sh is fine)"
    checks.append(Check("docker", None, docker))

    checks.append(_data_dir_check(settings))
    checks.append(_exposure_check(settings))
    return checks


def _daemon_holder(ports: list[int]) -> str | None:
    """Who may be the running daemon on its ports, at any address: `mtrtk`, `unknown` or None.

    A listener whose process this user cannot see (a root container, another user's unit) may
    well be the daemon, reading the receiver: the probe must not run beside it either.
    """
    holders = [owner for owner in (_port_owner(p) for p in ports) if owner is not None]
    if any(_is_mtrtk(owner) for owner in holders):
        return "mtrtk"
    if any(owner.name is None for owner in holders):
        return "unknown"
    return None


def _receiver_checks(
    settings: Settings, port: str | None, probe: bool, daemon: str | None
) -> list[Check]:
    if port is None:
        return [
            Check(
                "receiver",
                False,
                "no u-blox receiver found on USB",
                fix="check the USB cable; ls /dev/serial/by-id/",
            )
        ]
    if not os.path.exists(port):
        if _is_usb_serial(port):
            fix = "check the USB cable and the device name: ls /dev/serial/by-id/"
        else:
            fix = "is the link that creates it (socat, ser2net) running?"
        return [Check("receiver", False, f"{port} does not exist", fix=fix)]
    usable = os.access(port, os.R_OK | os.W_OK)
    kind = _device_kind(port)
    detail = f"{port} ({kind}, {'read/write ok' if usable else 'no read/write permission'})"
    if not usable:
        return [Check("receiver", False, detail, fix=DIALOUT_FIX)]
    if not probe:
        return [Check("receiver", True, detail)]
    if daemon == "mtrtk":
        skipped = Check(
            "firmware",
            None,
            "probe skipped: the mtrtk daemon is running and owns the receiver "
            "(its Receiver page shows the firmware)",
            fix="stop the daemon, then run mtrtk doctor --probe",
        )
        return [Check("receiver", True, detail), skipped]
    if daemon == "unknown":
        skipped = Check(
            "firmware",
            None,
            "probe skipped: a process this user cannot see holds the daemon's ports and may be "
            "the daemon reading the receiver",
            fix="stop it first, or run mtrtk doctor as that user / inside the container",
        )
        return [Check("receiver", True, detail), skipped]
    try:
        fw = _probe_firmware(port, settings.baud)
    except ProbeError as exc:
        failed = Check(
            "firmware",
            None,
            f"probe failed: {exc}",
            fix=f"check that no other program holds the port (fuser -v {port}) and the permissions",
        )
        return [Check("receiver", True, detail), failed]
    if fw is None:
        silent = Check(
            "firmware",
            None,
            f"no MON-VER reply from {port} at {settings.baud} baud",
            fix="check BAUD, and that no other program is reading the port",
        )
        return [Check("receiver", True, detail), silent]
    parsed = _parse_fw(fw)
    receiver = Check("receiver", True, f"{detail} · firmware {fw}")
    if parsed is not None and parsed < MIN_RECOMMENDED_FW:
        old = Check(
            "firmware",
            None,
            f"{fw} is old; HPG 1.32+ recommended ({CURRENT_FW} current)",
            fix="upgrade with u-center on Windows; see docs/firmware.md",
        )
        return [receiver, old]
    return [receiver, Check("firmware", True, fw)]


def _modemmanager_check(settings: Settings, port: str | None) -> Check:
    if settings.source_is_file:
        return Check("modemmanager", True, "not relevant: the source is a replay file")
    if port is not None and os.path.exists(port) and not _is_usb_serial(port):
        return Check("modemmanager", True, f"not relevant: {port} is not a local USB device")
    active = _service_active("ModemManager")
    if active is None:
        return Check(
            "modemmanager",
            None,
            f"cannot tell whether ModemManager is running ({NO_SYSTEMD})",
            fix="run mtrtk doctor on the host, or check systemctl is-active ModemManager there",
        )
    if not active:
        return Check("modemmanager", True, "not running")
    if _udev_rule_present():
        return Check("modemmanager", True, "running, udev ignore rule present")
    return Check(
        "modemmanager",
        None,
        "ModemManager is running and may grab the receiver's serial port",
        fix=(
            f"echo '{UDEV_RULE}' | sudo tee {UDEV_RULE_PATH} "
            "&& sudo udevadm control --reload && sudo udevadm trigger"
        ),
    )


def _time_sync_check() -> Check:
    synced = _ntp_synchronized()
    if synced:
        return Check("time_sync", True, "host clock NTP-synchronized")
    if synced is None:
        return Check(
            "time_sync",
            None,
            f"cannot tell whether the host clock is NTP-synchronized ({NO_SYSTEMD})",
            fix="check timedatectl on the host",
        )
    return Check(
        "time_sync",
        None,
        "host clock not NTP-synchronized: raw logs rotate on receiver time, but PPP export "
        "names and event logs use host time",
        fix="sudo timedatectl set-ntp true (or install chrony)",
    )


def _tailscale_check(settings: Settings) -> Check:
    ts_ip = tailscale_ipv4()
    binds = [settings.ntrip_bind, settings.web_bind]
    if settings.role is Role.ROVER and settings.nmea_tcp_port >= 0:
        binds.append(settings.nmea_tcp_bind)
    needs_ts = "tailscale" in binds
    # Tailscale up is OK whether or not a bind uses it; missing, it fails only a bind that needs it.
    return Check(
        "tailscale",
        True if ts_ip is not None else (False if needs_ts else None),
        ts_ip or "tailscale0 has no IPv4 (is tailscaled running and logged in?)",
        fix=None if ts_ip else "sudo tailscale up",
    )


def _ports_check(owners: dict[int, Any]) -> Check:
    if not owners:
        return Check("ports", True, "no fixed ports configured")
    foreign: list[str] = []
    unknown: list[str] = []
    ours: list[str] = []
    for port, owner in owners.items():
        if owner is None:
            continue
        if owner.name is None:
            unknown.append(f"{port} held by a process this user cannot see")
        elif _is_mtrtk(owner):
            ours.append(f"{port} held by mtrtk (pid {owner.pid})")
        else:
            foreign.append(f"{port} held by {owner.name} (pid {owner.pid})")
    listed = ", ".join(str(p) for p in owners)
    if foreign:
        return Check(
            "ports",
            False,
            "; ".join(foreign + unknown),
            fix="stop the other program, or change NTRIP_PORT / WEB_PORT",
        )
    if unknown:
        return Check(
            "ports",
            None,
            "; ".join(unknown + ours),
            fix="sudo ss -ltnp to see the owner (another mtrtk under another user is fine)",
        )
    return Check("ports", True, "; ".join(ours) if ours else f"{listed} available")


def _data_dir_check(settings: Settings) -> Check:
    data_dir = settings.data_dir
    if not data_dir.exists():
        return Check("data_dir", None, f"{data_dir} does not exist yet (created on first run)")
    writable = os.access(data_dir, os.W_OK)
    try:
        free_gb = shutil.disk_usage(data_dir).free / 1e9
    except OSError as exc:
        return Check("data_dir", False, f"{data_dir}: cannot read free space ({exc})")
    ok = writable and free_gb >= settings.min_free_gb
    detail = (
        f"{data_dir}: {'writable' if writable else 'NOT writable'}, "
        f"{free_gb:.1f} GB free (min {settings.min_free_gb})"
    )
    fix = None
    if not writable:
        fix = f"sudo chown -R $USER {data_dir}"
    elif not ok:
        fix = "free disk space or lower MIN_FREE_GB"
    return Check("data_dir", ok, detail, fix=fix)


def _exposure_check(settings: Settings) -> Check:
    fails: list[str] = []
    warns: list[str] = []
    web = _bind_scope(settings.web_bind)
    is_base = settings.role is Role.BASE
    # The `public` profile's Caddy and cloudflared both forward to localhost: a listener bound to
    # the tailnet address only is out of their reach.
    if settings.public_domain and web == "tailnet":
        warns.append(f"Caddy cannot reach the web UI on WEB_BIND={settings.web_bind}")
    if settings.tunnel_token:
        if web == "tailnet":
            warns.append(
                f"Cloudflare Tunnel cannot reach the web UI on WEB_BIND={settings.web_bind}"
            )
        if is_base and _bind_scope(settings.ntrip_bind) == "tailnet":
            warns.append(
                f"Cloudflare Tunnel cannot reach the caster on NTRIP_BIND={settings.ntrip_bind}"
            )
    access = False
    if not settings.web_password and web != "tailnet":
        if settings.public_domain:
            # Caddy has nothing like Cloudflare Access in front: the UI is open to the internet.
            fails.append(
                f"PUBLIC_DOMAIN={settings.public_domain} publishes the web UI (Caddy, the public "
                "profile) without WEB_PASSWORD"
            )
        if settings.tunnel_token:
            if settings.web_allow_insecure:
                access = True
                warns.append(
                    "Cloudflare Tunnel publishes the web UI without WEB_PASSWORD: only "
                    "Cloudflare Access on the hostname protects it"
                )
            else:
                fails.append("Cloudflare Tunnel publishes the web UI without WEB_PASSWORD")
    if not settings.web_password:
        if web == "all":
            fails.append(
                f"web UI on WEB_BIND={settings.web_bind} without WEB_PASSWORD "
                "(reachable beyond the LAN)"
            )
        elif web == "lan":
            if settings.web_allow_insecure:
                warns.append(
                    f"web UI on WEB_BIND={settings.web_bind} without a password "
                    "(accepted by WEB_ALLOW_INSECURE)"
                )
            else:
                fails.append(f"web UI on WEB_BIND={settings.web_bind} without WEB_PASSWORD")
    if is_base and settings.ntrip_anonymous:
        if _bind_scope(settings.ntrip_bind) == "all":
            warns.append("NTRIP caster is anonymous on all interfaces")
        elif settings.tunnel_token and _bind_scope(settings.ntrip_bind) != "tailnet":
            warns.append("NTRIP caster is anonymous and published through the tunnel")
    if settings.tunnel_token:
        warns.append(
            "Cloudflare Tunnel: NTRIP v1 clients (str2str, u-center) cannot use the tunnel; "
            "NTRIP v2 / HTTPS clients only"
        )
    detail = "; ".join(fails + warns) or "nothing reachable beyond Tailscale without a password"
    if fails:
        return Check("exposure", False, detail, fix="set WEB_PASSWORD")
    if warns:
        fixes = []
        if any("anonymous" in w for w in warns):
            fixes.append("set NTRIP_PASSWORD")
        if any("cannot reach" in w for w in warns):
            fixes.append("bind lan (or 127.0.0.1) so Caddy / cloudflared can reach localhost")
        if access:
            fixes.append("put Cloudflare Access in front of the hostname, or set WEB_PASSWORD")
        return Check("exposure", None, detail, fix="; ".join(fixes) or None)
    return Check("exposure", True, detail)


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


def format_table(checks: list[Check]) -> str:
    marks = {True: "OK  ", False: "FAIL", None: "WARN"}
    lines = []
    for c in checks:
        mark = "INFO" if c.name in INFO_CHECKS and c.ok is None else marks[c.ok]
        lines.append(f"[{mark}] {c.name:<13} {c.detail}")
        if c.fix and c.ok is not True:
            lines.append(f"       fix: {c.fix}")
    return "\n".join(lines)
