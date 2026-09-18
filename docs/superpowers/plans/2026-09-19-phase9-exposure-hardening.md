# mtrtk Phase 9: Exposure, Hardening, Docs and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make mtrtk deployable by anyone with `git clone`, `.env`, `docker compose up -d` on a Raspberry Pi or x86 box: reachable over Tailscale (default), a public IP (Caddy TLS) or a Cloudflare Tunnel with a custom domain; hardened container and host setup; a native systemd install path; a `doctor` that catches the common field problems; a complete docs set; and a tagged, multi-arch release pipeline.

**Architecture:** Compose profiles add sidecars without touching the daemon: `public` runs Caddy (automatic HTTPS for `PUBLIC_DOMAIN` → `127.0.0.1:8080`); `cloudflare` runs `cloudflared` with a remotely-managed tunnel token whose ingress (configured in the Cloudflare dashboard) maps `rtk.<domain>` → `http://localhost:8080` and `ntrip.<domain>` → `http://localhost:2101`. The daemon container drops to a non-root user with the `dialout` group, keeps host networking (Tailscale bind), and gets `LOG_LEVEL`. `install.sh` provides the Docker-free path (uv venv + systemd + udev). `mtrtk doctor` grows host checks (ModemManager, time sync, ports, firmware age, exposure sanity) and `--json`. `mtrtk backup/restore` moves a station between hosts. Release: `release.yml` builds and pushes multi-arch images on `v*` tags and drafts GitHub Releases from `CHANGELOG.md`.

**Tech Stack:** Docker Compose profiles, Caddy 2, cloudflared, systemd, udev, GitHub Actions (buildx, ghcr.io), Python click/psutil.

**Spec:** `docs/superpowers/specs/2026-09-18-mtrtk-design.md` — *Exposure*, *docker / deploy*, *Phase 9*, *Cloudflare Tunnel* risk. Prerequisites: Phases 1–8. **Implementer: load the `cloudflare` skill before Task 3** to check current cloudflared/tunnel syntax against Cloudflare docs.

## Global Constraints

- Default exposure stays Tailscale-only (`NTRIP_BIND=tailscale`, `WEB_BIND=tailscale`); nothing in this phase changes defaults.
- Public web access always goes through TLS (Caddy or Cloudflare) and requires `WEB_PASSWORD` (Phase 1 `Settings` refuses non-tailscale binds without it unless `WEB_ALLOW_INSECURE=1`).
- Cloudflare Tunnel carries HTTP only: the web UI and **NTRIP v2 over HTTPS**. NTRIP v1 (`ICY`) clients (RTKLIB `str2str`, u-center) do not work through the tunnel — documented, and `doctor` says so when `TUNNEL_TOKEN` is set. Raw TCP for v1 clients needs the public-IP path (router forward of 2101).
- Container runs as uid/gid `1000:1000` with supplementary group `20` (`dialout`); `/data` is chowned at startup by a tiny entrypoint when it is root-owned (first run on a bind mount).
- `install.sh` is idempotent, targets Debian/Ubuntu/Raspberry Pi OS (64-bit), never overwrites an existing `.env`, installs `uv` per-user, a systemd unit `mtrtk.service` (restart always, `After=network-online.target tailscaled.service`), and a udev rule marking u-blox devices `ID_MM_DEVICE_IGNORE=1`.
- `mtrtk doctor` exit code: 1 when any check FAILs; `--json` prints machine-readable results; checks never modify the system.
- Backups contain `mtrtk.db`, `.env` **without** secret values (masked) unless `--with-secrets`, and `sites.json`; restore refuses to overwrite an existing database without `--force`.
- Release tags follow `vMAJOR.MINOR.PATCH`; images are tagged with the version and `latest`; the ROS image with `<version>-humble` / `<version>-jazzy`.
- Commit per task, Conventional Commits, trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File structure (this plan)

| Path | Responsibility |
|---|---|
| `src/mtrtk/doctor.py` (rewrite), `src/mtrtk/cli.py` (modify) | host checks, `--json`, `backup`, `restore` |
| `src/mtrtk/backup.py` | archive create/restore |
| `install.sh`, `uninstall.sh`, `systemd/mtrtk.service`, `udev/99-mtrtk-ublox.rules` | native install |
| `docker/Dockerfile` (modify), `docker/entrypoint.sh`, `docker-compose.yml` (modify), `docker/Caddyfile`, `scripts/check-exposure.sh` | container hardening, profiles |
| `docs/setup.md`, `docs/hardware.md`, `docs/exposure.md`, `docs/firmware.md`, `docs/troubleshooting.md`, `docs/acceptance.md`, `README.md` (rewrite) | documentation |
| `.github/workflows/release.yml`, `CHANGELOG.md`, `scripts/bump-version.py` | release pipeline |
| `tests/unit/test_doctor.py` (extend), `tests/unit/test_backup.py`, `tests/unit/test_version_sync.py` | tests |

---

### Task 1: `doctor` host checks and `--json`

**Files:**
- Modify: `src/mtrtk/doctor.py`, `src/mtrtk/cli.py`, `tests/unit/test_doctor.py`

**Interfaces:**
- Produces: `Check(name, ok: bool | None, detail: str, fix: str | None = None)`; `run_checks(settings, *, probe_receiver=False) -> list[Check]` with checks `python`, `receiver` (device + permissions; with `probe_receiver` also MON-VER firmware and a WARN when `HPG < 1.32`), `modemmanager` (WARN when `ModemManager` is active and no udev ignore rule exists; `fix` = the rule), `time_sync` (`timedatectl show` `NTPSynchronized=yes` or chrony/ntp service active; WARN otherwise: hour rotation uses receiver time but PPP export names and logs use host time for events), `tailscale` (needed binds → IPv4 present; FAIL when needed and absent), `ports` (2101 / `WEB_PORT` free or held by a process named `mtrtk`; FAIL when another process holds them), `rtklib` (`convbin` + `rnx2rtkp`, WARN), `docker` (INFO: present/version or absent), `data_dir` (exists/writable/free space), `exposure` (FAIL: `WEB_BIND=all|lan` without password and without `WEB_ALLOW_INSECURE`; WARN: `NTRIP_BIND=all` anonymous; WARN: `TUNNEL_TOKEN` set → "NTRIP v1 clients cannot use the tunnel"); `format_table(checks) -> str`; CLI `mtrtk doctor [--json] [--probe]`.

- [ ] **Step 1: Extend the tests**

Replace `tests/unit/test_doctor.py` with:
```python
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from mtrtk import doctor
from mtrtk.cli import main
from mtrtk.config import Settings


@pytest.fixture
def quiet_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    monkeypatch.setattr(doctor, "find_ublox_port", lambda: None)
    monkeypatch.setattr(doctor, "tailscale_ipv4", lambda: "100.100.50.10")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(doctor, "_service_active", lambda name: False)
    monkeypatch.setattr(doctor, "_ntp_synchronized", lambda: True)
    monkeypatch.setattr(doctor, "_port_owner", lambda port: None)
    monkeypatch.setattr(doctor, "_udev_rule_present", lambda: False)
    monkeypatch.setattr(doctor, "_command_output", lambda args: "Docker version 29.6.2")
    return Settings(_env_file=None, data_dir=tmp_path)


def by_name(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {c.name: c for c in checks}


def test_baseline_checks(quiet_host: Settings) -> None:
    c = by_name(doctor.run_checks(quiet_host))
    assert c["python"].ok is True
    assert c["receiver"].ok is False and "USB" in c["receiver"].detail
    assert c["tailscale"].ok is True
    assert c["ports"].ok is True
    assert c["rtklib"].ok is None
    assert c["docker"].ok is None and "29.6.2" in c["docker"].detail
    assert c["time_sync"].ok is True
    assert c["modemmanager"].ok is True
    assert c["exposure"].ok is True and c["data_dir"].ok is True


def test_modemmanager_warns_with_fix(quiet_host: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_service_active", lambda name: name == "ModemManager")
    c = by_name(doctor.run_checks(quiet_host))
    assert c["modemmanager"].ok is None and "ID_MM_DEVICE_IGNORE" in (c["modemmanager"].fix or "")


def test_ports_fail_when_foreign_process_holds_them(quiet_host: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_port_owner", lambda port: SimpleNamespace(pid=4242, name="str2str") if port == 2101 else None)
    c = by_name(doctor.run_checks(quiet_host))
    assert c["ports"].ok is False and "str2str" in c["ports"].detail
    monkeypatch.setattr(doctor, "_port_owner", lambda port: SimpleNamespace(pid=1, name="mtrtk"))
    assert by_name(doctor.run_checks(quiet_host))["ports"].ok is True


def test_exposure_rules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, quiet_host: Settings) -> None:
    insecure = Settings(_env_file=None, data_dir=tmp_path, ntrip_password="", ntrip_bind="all", web_bind="all", web_allow_insecure=True)
    c = by_name(doctor.run_checks(insecure))
    assert c["exposure"].ok is False and "WEB_PASSWORD" in c["exposure"].detail
    tunnel = Settings(_env_file=None, data_dir=tmp_path, ntrip_password="pw", tunnel_token="abc")
    assert "NTRIP v1" in by_name(doctor.run_checks(tunnel))["exposure"].detail


def test_time_sync_warns(quiet_host: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_ntp_synchronized", lambda: False)
    assert by_name(doctor.run_checks(quiet_host))["time_sync"].ok is None


def test_cli_json_and_exit_code(quiet_host: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(quiet_host.data_dir))
    r = CliRunner().invoke(main, ["doctor", "--json"])
    assert r.exit_code == 1  # receiver missing
    data = json.loads(r.output)
    assert {c["name"] for c in data} >= {"python", "receiver", "tailscale", "exposure"}
    assert any(c["ok"] is False for c in data)
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/unit/test_doctor.py -q` → failures.

- [ ] **Step 3: Rewrite `src/mtrtk/doctor.py`**

```python
"""Environment checks for `mtrtk doctor`. Read-only; every helper is patchable in tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psutil

from mtrtk.config import Settings
from mtrtk.core.exposure import tailscale_ipv4
from mtrtk.core.source import find_ublox_port

UDEV_RULE = 'ACTION=="add|change", SUBSYSTEM=="usb", ATTRS{idVendor}=="1546", ENV{ID_MM_DEVICE_IGNORE}="1"'
UDEV_RULE_PATH = Path("/etc/udev/rules.d/99-mtrtk-ublox.rules")
MIN_RECOMMENDED_FW = (1, 32)


@dataclass
class Check:
    name: str
    ok: bool | None  # None = warning / informational
    detail: str
    fix: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _command_output(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _service_active(name: str) -> bool:
    return _command_output(["systemctl", "is-active", name]) == "active"


def _ntp_synchronized() -> bool:
    out = _command_output(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if out:
        return out.strip().lower() == "yes"
    return _service_active("chrony") or _service_active("chronyd") or _service_active("ntpd")


def _udev_rule_present() -> bool:
    return UDEV_RULE_PATH.exists() and "ID_MM_DEVICE_IGNORE" in UDEV_RULE_PATH.read_text(errors="replace")


def _port_owner(port: int) -> Any | None:
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port and conn.pid:
                proc = psutil.Process(conn.pid)
                return type("Owner", (), {"pid": conn.pid, "name": proc.name()})()
    except (psutil.Error, PermissionError):
        return None
    return None


def _parse_fw(fw: str) -> tuple[int, int] | None:
    try:
        major, minor = fw.split()[-1].split(".")[:2]
        return int(major), int(minor)
    except (ValueError, IndexError):
        return None


def _probe_firmware(port: str, baud: int) -> str | None:
    """Poll MON-VER synchronously (only with --probe; the daemon must not be running)."""
    try:
        import serial
        from pyubx2 import POLL, UBXMessage, UBXReader

        with serial.Serial(port, baud, timeout=2) as ser:
            ser.write(UBXMessage("MON", "MON-VER", POLL).serialize())
            reader = UBXReader(ser, protfilter=2)
            for _ in range(200):
                _, msg = reader.read()
                if msg is not None and msg.identity == "MON-VER":
                    for k, v in msg.__dict__.items():
                        if k.startswith("extension") and b"FWVER=" in v:
                            return v.split(b"=", 1)[1].split(b"\x00", 1)[0].decode()
    except Exception:
        return None
    return None


def run_checks(settings: Settings, *, probe_receiver: bool = False) -> list[Check]:
    checks: list[Check] = []
    v = sys.version_info
    checks.append(Check("python", v >= (3, 12), f"{v.major}.{v.minor}.{v.micro}"))

    if settings.source_is_file:
        checks.append(Check("receiver", settings.source_path.exists(), f"replay file {settings.source_path}"))
    else:
        port = settings.mtrtk_source if settings.mtrtk_source != "auto" else find_ublox_port()
        if port is None:
            checks.append(Check("receiver", False, "no u-blox receiver found on USB", fix="check the USB cable; `ls /dev/serial/by-id/`"))
        else:
            readable = os.access(port, os.R_OK | os.W_OK)
            detail = f"{port} ({'read/write ok' if readable else 'no permission'})"
            fix = None if readable else "add your user to dialout: sudo usermod -aG dialout $USER && re-login"
            if readable and probe_receiver:
                fw = _probe_firmware(port, settings.baud)
                if fw:
                    detail += f" · firmware {fw}"
                    parsed = _parse_fw(fw)
                    if parsed and parsed < MIN_RECOMMENDED_FW:
                        checks.append(Check("firmware", None, f"{fw} is old; HPG 1.32+ recommended (1.51 current)", fix="upgrade with u-center on Windows; see docs/firmware.md"))
            checks.append(Check("receiver", readable, detail, fix))

    mm_active = _service_active("ModemManager")
    if mm_active and not _udev_rule_present():
        checks.append(Check("modemmanager", None, "ModemManager is running and may grab the receiver's serial port", fix=f"sudo tee {UDEV_RULE_PATH} <<< '{UDEV_RULE}' && sudo udevadm control --reload"))
    else:
        checks.append(Check("modemmanager", True, "not running" if not mm_active else "running, udev ignore rule present"))

    checks.append(Check("time_sync", True if _ntp_synchronized() else None, "host clock synchronized" if _ntp_synchronized() else "host clock not NTP-synchronized (receiver time still drives log rotation)", fix=None if _ntp_synchronized() else "sudo timedatectl set-ntp true  (or install chrony)"))

    ts_ip = tailscale_ipv4()
    needs_ts = "tailscale" in (settings.ntrip_bind, settings.web_bind)
    checks.append(Check("tailscale", (ts_ip is not None) if needs_ts else None, ts_ip or "tailscale0 has no IPv4 (is tailscaled running and logged in?)", fix=None if ts_ip else "sudo tailscale up"))

    port_problems = []
    for port in (settings.ntrip_port, settings.web_port):
        owner = _port_owner(port)
        if owner is not None and "mtrtk" not in str(owner.name).lower():
            port_problems.append(f"{port} held by {owner.name} (pid {owner.pid})")
    checks.append(Check("ports", not port_problems, "; ".join(port_problems) or f"{settings.ntrip_port} and {settings.web_port} available", fix="stop the other program or change NTRIP_PORT / WEB_PORT" if port_problems else None))

    missing = [tool for tool in ("convbin", "rnx2rtkp") if shutil.which(tool) is None]
    checks.append(Check("rtklib", None if missing else True, ("missing: " + ", ".join(missing) + " (only needed for RINEX export / PPK outside Docker)") if missing else "convbin, rnx2rtkp found", fix="sudo apt install rtklib" if missing else None))

    docker = shutil.which("docker")
    checks.append(Check("docker", None, _command_output(["docker", "--version"]) or "docker present" if docker else "docker not installed (native install via install.sh is fine)"))

    data_dir = settings.data_dir
    if data_dir.exists():
        writable = os.access(data_dir, os.W_OK)
        free_gb = shutil.disk_usage(data_dir).free / 1e9
        checks.append(Check("data_dir", writable and free_gb >= settings.min_free_gb, f"{data_dir}: {'writable' if writable else 'NOT writable'}, {free_gb:.1f} GB free (min {settings.min_free_gb})"))
    else:
        checks.append(Check("data_dir", None, f"{data_dir} does not exist yet (created on first run)"))

    exposure_issues: list[str] = []
    exposure_warn: list[str] = []
    if settings.web_bind != "tailscale" and not settings.web_password:
        exposure_issues.append("web UI reachable beyond Tailscale without WEB_PASSWORD")
    if settings.ntrip_bind == "all" and settings.ntrip_anonymous:
        exposure_warn.append("NTRIP caster is anonymous on all interfaces")
    if getattr(settings, "tunnel_token", None):
        exposure_warn.append("Cloudflare Tunnel: NTRIP v1 clients (str2str, u-center) cannot connect through the tunnel; v2/HTTPS clients only")
    detail = "; ".join(exposure_issues + exposure_warn) or "Tailscale-only defaults"
    checks.append(Check("exposure", False if exposure_issues else (None if exposure_warn else True), detail, fix="set WEB_PASSWORD" if exposure_issues else None))
    return checks


def format_table(checks: list[Check]) -> str:
    marks = {True: "OK  ", False: "FAIL", None: "WARN"}
    lines = [f"[{marks[c.ok]}] {c.name:<13} {c.detail}" + (f"\n       fix: {c.fix}" if c.fix and c.ok is not True else "") for c in checks]
    return "\n".join(lines)
```
`Settings` needs `tunnel_token: str | None = None` (add in `config.py`; it is already in `.env.example`).

- [ ] **Step 4: Update the CLI command**

```python
@main.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--probe", is_flag=True, help="Also poll the receiver's firmware (stop the daemon first).")
def doctor(as_json: bool, probe: bool) -> None:
    """Check receiver access, host services, Tailscale, ports, RTKLIB, disk and exposure."""
    import json as _json

    from mtrtk.doctor import format_table, run_checks

    checks = run_checks(_load_settings(ntrip_password=""), probe_receiver=probe)
    click.echo(_json.dumps([c.to_json() for c in checks], indent=2) if as_json else format_table(checks))
    if any(c.ok is False for c in checks):
        raise SystemExit(1)
```

- [ ] **Step 5: Run tests, lint, commit**

`uv run pytest tests/unit/test_doctor.py -q` → `6 passed`.
```bash
git add src/mtrtk/doctor.py src/mtrtk/cli.py src/mtrtk/config.py tests/unit/test_doctor.py
git commit -m "feat(doctor): host checks (ModemManager, time sync, ports, exposure, firmware age) and --json

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Backup/restore and native install

**Files:**
- Create: `src/mtrtk/backup.py`, `tests/unit/test_backup.py`, `install.sh`, `uninstall.sh`, `systemd/mtrtk.service`, `udev/99-mtrtk-ublox.rules`
- Modify: `src/mtrtk/cli.py`

**Interfaces:**
- Produces: `create_backup(settings, out: Path, *, with_secrets=False) -> Path` (tar.gz with `mtrtk.db`, `env` (masked unless with_secrets), `sites.json`, `manifest.json` {version, created_utc, station_id, with_secrets}); `restore_backup(archive: Path, settings, *, force=False) -> dict` (copies `mtrtk.db` into `DATA_DIR`, writes `sites.json` for reference, prints the env for manual merge; refuses when a DB exists unless `force`); CLI `mtrtk backup --out FILE [--with-secrets]`, `mtrtk restore FILE [--force]`.
- `install.sh`: installs `uv` if missing, creates `.venv` via `uv sync --frozen --no-dev`, copies `.env.example` → `.env` when absent, installs `udev/99-mtrtk-ublox.rules`, adds the invoking user to `dialout`, installs `systemd/mtrtk.service` (templated with the repo path and user), enables + starts it, prints `mtrtk doctor` output. `uninstall.sh` stops/disables the unit and removes the udev rule (keeps data and `.env`).

- [ ] **Step 1: Write the failing backup tests**

`tests/unit/test_backup.py`:
```python
import json
import tarfile
from pathlib import Path

import pytest

from mtrtk.backup import create_backup, restore_backup
from mtrtk.config import Settings
from mtrtk.store.db import Database
from mtrtk.store.models import Site
from mtrtk.store.repos import SitesRepo


async def seed(data_dir: Path) -> None:
    db = Database(data_dir / "mtrtk.db")
    await db.open()
    await SitesRepo(db).add(Site.from_ecef("roof", 1.0, 2.0, 3.0, source="manual"))
    await db.close()


async def test_backup_masks_secrets_and_includes_sites(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    await seed(data)
    env = tmp_path / ".env"
    env.write_text("ROLE=base\nNTRIP_PASSWORD=supersecret\nWEB_PASSWORD=alsosecret\nSTATION_ID=MTRK\n")
    settings = Settings(_env_file=None, data_dir=data, ntrip_password="supersecret", mtrtk_env_file=env)
    archive = create_backup(settings, tmp_path / "b.tar.gz")
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        assert {"manifest.json", "mtrtk.db", "env", "sites.json"} <= set(names)
        env_text = tar.extractfile("env").read().decode()
        assert "supersecret" not in env_text and "NTRIP_PASSWORD=***" in env_text and "STATION_ID=MTRK" in env_text
        sites = json.loads(tar.extractfile("sites.json").read())
        assert sites[0]["name"] == "roof"
        manifest = json.loads(tar.extractfile("manifest.json").read())
        assert manifest["with_secrets"] is False and manifest["station_id"] == "MTRK"


async def test_backup_with_secrets(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    await seed(data)
    env = tmp_path / ".env"
    env.write_text("NTRIP_PASSWORD=supersecret\n")
    settings = Settings(_env_file=None, data_dir=data, ntrip_password="supersecret", mtrtk_env_file=env)
    archive = create_backup(settings, tmp_path / "b.tar.gz", with_secrets=True)
    with tarfile.open(archive) as tar:
        assert "supersecret" in tar.extractfile("env").read().decode()


async def test_restore_refuses_then_forces(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    await seed(src)
    settings_src = Settings(_env_file=None, data_dir=src, ntrip_password="x", mtrtk_env_file=tmp_path / "none.env")
    archive = create_backup(settings_src, tmp_path / "b.tar.gz")
    dst = tmp_path / "dst"
    dst.mkdir()
    settings_dst = Settings(_env_file=None, data_dir=dst, ntrip_password="x")
    info = restore_backup(archive, settings_dst)
    assert (dst / "mtrtk.db").exists() and info["sites"] == 1
    with pytest.raises(FileExistsError):
        restore_backup(archive, settings_dst)
    restore_backup(archive, settings_dst, force=True)
    db = Database(dst / "mtrtk.db")
    await db.open()
    assert [s.name for s in await SitesRepo(db).list()] == ["roof"]
    await db.close()
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Write `src/mtrtk/backup.py`**

```python
"""Move a station between hosts: database + sites + (masked) environment."""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mtrtk import __version__
from mtrtk.config import Settings
from mtrtk.web.api.config import SECRET_KEYS

SECRET_ENV_KEYS = {k.upper() for k in SECRET_KEYS}


def _masked_env(path: Path, with_secrets: bool) -> str:
    if not path.exists():
        return ""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0].strip()
        if not with_secrets and "=" in line and key in SECRET_ENV_KEYS and line.split("=", 1)[1].strip():
            out.append(f"{key}=***")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def _sites_json(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM sites ORDER BY name")]


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(datetime.now(UTC).timestamp())
    tar.addfile(info, io.BytesIO(data))


def create_backup(settings: Settings, out: Path, *, with_secrets: bool = False) -> Path:
    db_path = settings.data_dir / "mtrtk.db"
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"version": __version__, "created_utc": datetime.now(UTC).isoformat(), "station_id": settings.station_id, "role": settings.role.value, "with_secrets": with_secrets}
    with tarfile.open(out, "w:gz") as tar:
        _add_bytes(tar, "manifest.json", json.dumps(manifest, indent=2).encode())
        if db_path.exists():
            with tempfile.TemporaryDirectory() as tmp:  # consistent copy even while the daemon writes (WAL)
                snapshot = Path(tmp) / "mtrtk.db"
                with sqlite3.connect(db_path) as src, sqlite3.connect(snapshot) as dst:
                    src.backup(dst)
                tar.add(snapshot, arcname="mtrtk.db")
        _add_bytes(tar, "env", _masked_env(settings.mtrtk_env_file, with_secrets).encode())
        _add_bytes(tar, "sites.json", json.dumps(_sites_json(db_path), indent=2, default=str).encode())
    return out


def restore_backup(archive: Path, settings: Settings, *, force: bool = False) -> dict[str, Any]:
    db_path = settings.data_dir / "mtrtk.db"
    if db_path.exists() and not force:
        raise FileExistsError(f"{db_path} exists; pass --force to overwrite")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())
        with tempfile.TemporaryDirectory() as tmp:
            tar.extractall(tmp, filter="data")
            extracted = Path(tmp) / "mtrtk.db"
            if extracted.exists():
                for suffix in ("", "-wal", "-shm"):
                    (settings.data_dir / f"mtrtk.db{suffix}").unlink(missing_ok=True)
                shutil.copy(extracted, db_path)
            sites = json.loads((Path(tmp) / "sites.json").read_text()) if (Path(tmp) / "sites.json").exists() else []
            (settings.data_dir / "restored-sites.json").write_text(json.dumps(sites, indent=2))
            env_text = (Path(tmp) / "env").read_text() if (Path(tmp) / "env").exists() else ""
    return {"manifest": manifest, "sites": len(sites), "env": env_text}
```

- [ ] **Step 4: CLI commands**

```python
@main.command()
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--with-secrets", is_flag=True, help="Keep passwords/tokens in the archived .env.")
def backup(out_path: Path, with_secrets: bool) -> None:
    """Archive the database, sites and (masked) .env."""
    from mtrtk.backup import create_backup

    path = create_backup(_load_settings(ntrip_password=""), out_path, with_secrets=with_secrets)
    click.echo(f"wrote {path} ({path.stat().st_size} bytes)")


@main.command()
@click.argument("archive", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--force", is_flag=True, help="Overwrite an existing database.")
def restore(archive: Path, force: bool) -> None:
    """Restore a backup into DATA_DIR (stop the daemon first)."""
    from mtrtk.backup import restore_backup

    try:
        info = restore_backup(archive, _load_settings(ntrip_password=""), force=force)
    except FileExistsError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"restored database from {info['manifest']['created_utc']} ({info['sites']} sites); the archived .env is:")
    click.echo(info["env"])
```

- [ ] **Step 5: Native install files**

`udev/99-mtrtk-ublox.rules`:
```
# u-blox GNSS receivers: keep ModemManager away and make the port group-accessible
ACTION=="add|change", SUBSYSTEM=="usb", ATTRS{idVendor}=="1546", ENV{ID_MM_DEVICE_IGNORE}="1"
KERNEL=="ttyACM[0-9]*", ATTRS{idVendor}=="1546", MODE="0660", GROUP="dialout"
```
`systemd/mtrtk.service` (placeholders replaced by `install.sh`):
```ini
[Unit]
Description=mtrtk GNSS RTK daemon
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=__USER__
Group=dialout
WorkingDirectory=__REPO__
EnvironmentFile=__REPO__/.env
Environment=DATA_DIR=__REPO__/data
ExecStart=__REPO__/.venv/bin/mtrtk run
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=25
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```
`install.sh`:
```bash
#!/usr/bin/env bash
# Native install for Debian/Ubuntu/Raspberry Pi OS (64-bit). Idempotent. Run from the repo root as the user who will operate mtrtk.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="${SUDO_USER:-$USER}"
echo "==> mtrtk native install for $USER_NAME in $REPO"
if ! command -v uv >/dev/null 2>&1; then
  echo "==> installing uv"; curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"
fi
command -v convbin >/dev/null 2>&1 || { echo "==> installing RTKLIB (apt)"; sudo apt-get update -qq && sudo apt-get install -y -qq rtklib; }
echo "==> python environment"; uv sync --frozen --no-dev
[ -f "$REPO/.env" ] || { cp "$REPO/.env.example" "$REPO/.env"; echo "==> created .env from .env.example — edit NTRIP_PASSWORD at least"; }
mkdir -p "$REPO/data"
echo "==> udev rule + dialout group"
sudo install -m 0644 "$REPO/udev/99-mtrtk-ublox.rules" /etc/udev/rules.d/99-mtrtk-ublox.rules
sudo udevadm control --reload && sudo udevadm trigger
sudo usermod -aG dialout "$USER_NAME"
echo "==> systemd unit"
sed -e "s#__REPO__#$REPO#g" -e "s#__USER__#$USER_NAME#g" "$REPO/systemd/mtrtk.service" | sudo tee /etc/systemd/system/mtrtk.service >/dev/null
sudo systemctl daemon-reload && sudo systemctl enable --now mtrtk.service
echo "==> doctor"; "$REPO/.venv/bin/mtrtk" doctor || true
echo "Done. Logs: journalctl -u mtrtk -f   ·   UI: http://<tailscale-ip>:8080"
```
`uninstall.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
sudo systemctl disable --now mtrtk.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/mtrtk.service /etc/udev/rules.d/99-mtrtk-ublox.rules
sudo systemctl daemon-reload; sudo udevadm control --reload
echo "mtrtk service and udev rule removed; data/ and .env kept."
```
`chmod +x install.sh uninstall.sh`.

- [ ] **Step 6: Run tests, lint, commit**

`uv run pytest tests/unit/test_backup.py -q` → `3 passed`; `bash -n install.sh uninstall.sh` (syntax check).
```bash
git add src/mtrtk/backup.py src/mtrtk/cli.py tests/unit/test_backup.py install.sh uninstall.sh systemd udev
git commit -m "feat: backup/restore commands and native systemd install with udev rule

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Compose profiles — public IP (Caddy) and Cloudflare Tunnel

**Files:**
- Create: `docker/Caddyfile`, `scripts/check-exposure.sh`
- Modify: `docker-compose.yml`, `.env.example`

**Interfaces:**
- Profile `public`: service `caddy` (`caddy:2-alpine`, `network_mode: host`, volumes `./docker/Caddyfile:/etc/caddy/Caddyfile:ro`, `caddy_data`, `caddy_config`), Caddyfile serving `{$PUBLIC_DOMAIN}` → `reverse_proxy 127.0.0.1:8080` with automatic HTTPS; NTRIP stays on TCP 2101 (router forward). Requires `WEB_BIND=lan` (or `127.0.0.1`) and `WEB_PASSWORD`.
- Profile `cloudflare`: service `cloudflared` (`cloudflare/cloudflared:latest`, `network_mode: host`, `command: tunnel --no-autoupdate run --token ${TUNNEL_TOKEN}`); ingress is configured in Cloudflare Zero Trust → Networks → Tunnels: public hostname `rtk.<domain>` → `http://localhost:8080`; `ntrip.<domain>` → `http://localhost:2101`. Optional Cloudflare Access policy on `rtk.<domain>`.
- `scripts/check-exposure.sh <web-url> <ntrip-url> <user> <pass>`: checks `GET /healthz` over HTTPS, then an NTRIP v2 request over HTTPS and confirms RTCM (`d3 00`) bytes arrive within 15 s.

- [ ] **Step 1: Load the `cloudflare` skill** (Skill tool: `cloudflare`) and confirm: the remotely-managed tunnel run command (`cloudflared tunnel --no-autoupdate run --token <TOKEN>`), that HTTP ingress supports chunked/streaming responses, and the WebSocket support needed for `/ws`. Note any deviation in the report and adjust the compose command below.

- [ ] **Step 2: Compose and Caddyfile**

Append to `docker-compose.yml`:
```yaml
  caddy:
    profiles: ["public"]
    image: caddy:2-alpine
    container_name: mtrtk-caddy
    network_mode: host
    restart: unless-stopped
    environment:
      PUBLIC_DOMAIN: ${PUBLIC_DOMAIN:?set PUBLIC_DOMAIN in .env}
      ACME_EMAIL: ${ACME_EMAIL:-}
    volumes:
      - ./docker/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on: [mtrtk]

  cloudflared:
    profiles: ["cloudflare"]
    image: cloudflare/cloudflared:latest
    container_name: mtrtk-cloudflared
    network_mode: host
    restart: unless-stopped
    command: tunnel --no-autoupdate run --token ${TUNNEL_TOKEN:?set TUNNEL_TOKEN in .env}
    depends_on: [mtrtk]

volumes:
  caddy_data:
  caddy_config:
```
`docker/Caddyfile`:
```
{
	email {$ACME_EMAIL}
}

{$PUBLIC_DOMAIN} {
	encode zstd gzip
	reverse_proxy 127.0.0.1:8080
}
```
`.env.example` exposure block: add `ACME_EMAIL=` and comments:
```
# ---------------------------------------------------------------- exposure beyond Tailscale (optional)
# Public IP:   docker compose --profile public up -d   (forward TCP 443 -> this host; forward 2101 for NTRIP v1 clients)
#              needs WEB_BIND=lan (Caddy proxies to 127.0.0.1:8080) and WEB_PASSWORD
# Cloudflare:  docker compose --profile cloudflare up -d   (tunnel ingress: rtk.<domain> -> http://localhost:8080, ntrip.<domain> -> http://localhost:2101)
#              NTRIP v2-over-HTTPS clients only through the tunnel; WEB_BIND=lan and WEB_PASSWORD (or Cloudflare Access)
PUBLIC_DOMAIN=
ACME_EMAIL=
TUNNEL_TOKEN=
```

- [ ] **Step 3: Write `scripts/check-exposure.sh`**

```bash
#!/usr/bin/env bash
# Usage: scripts/check-exposure.sh https://rtk.example.com https://ntrip.example.com/MTRK rover password
set -euo pipefail
WEB="$1"; NTRIP="$2"; USER="${3:-}"; PASS="${4:-}"
echo "== web healthz =="; curl -fsS --max-time 10 "$WEB/healthz"; echo
echo "== NTRIP v2 over HTTPS (15 s) =="
AUTH=(); [ -n "$USER" ] && AUTH=(-u "$USER:$PASS")
BYTES=$(curl -sS --max-time 15 -H "Ntrip-Version: Ntrip/2.0" -H "User-Agent: NTRIP mtrtk-check" "${AUTH[@]}" "$NTRIP" | head -c 2000 | xxd -p | tr -d '\n' | grep -o 'd300' | wc -l || true)
if [ "$BYTES" -gt 0 ]; then echo "OK: RTCM frames arrived through the tunnel ($BYTES preambles seen)"; else echo "FAIL: no RTCM bytes; check tunnel ingress for the NTRIP hostname and the caster's bind"; exit 1; fi
```
`chmod +x scripts/check-exposure.sh`.

- [ ] **Step 4: Verify (user has a Cloudflare domain; needs the tunnel token)**

1. Cloudflare Zero Trust → Networks → Tunnels → create tunnel → copy token into `.env` `TUNNEL_TOKEN`; add public hostnames `rtk.<domain>` → `HTTP localhost:8080` and `ntrip.<domain>` → `HTTP localhost:2101`.
2. `.env`: `WEB_BIND=lan`, `WEB_PASSWORD=<strong>`, `NTRIP_BIND=lan`.
3. `docker compose --profile cloudflare up -d` then `scripts/check-exposure.sh https://rtk.<domain> https://ntrip.<domain>/MTRK rover <pw>`.
4. From a phone on mobile data: SW Maps → NTRIP client → `https://ntrip.<domain>` port 443, mountpoint MTRK, user/pass → RTK float/fixed. Browser → `https://rtk.<domain>` → login → live UI (WebSocket works through the tunnel).
Record in the report: time-to-first-RTCM through the tunnel and whether the stream stalls (Cloudflare buffering) — if it stalls, note it in `docs/exposure.md` and recommend the public-IP path for NTRIP.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml docker/Caddyfile scripts/check-exposure.sh .env.example
git commit -m "build: public (Caddy TLS) and cloudflare (tunnel) compose profiles with an exposure check script

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Container hardening — non-root, entrypoint, healthcheck, log level

**Files:**
- Create: `docker/entrypoint.sh`
- Modify: `docker/Dockerfile`, `docker-compose.yml`, `.env.example`, `src/mtrtk/config.py` (`log_level`), `src/mtrtk/daemon.py` (apply `LOG_LEVEL`), `tests/unit/test_config.py`

**Interfaces:**
- `Settings.log_level: Literal["DEBUG","INFO","WARNING","ERROR"] = "INFO"`; daemon configures `logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")` once.
- Image: runtime stage adds `RUN groupadd -g 1000 mtrtk && useradd -u 1000 -g 1000 -G 20 -m mtrtk` (gid 20 = `dialout` on Debian; the compose `group_add` also adds the host's dialout gid via `DIALOUT_GID`), `ENTRYPOINT ["/entrypoint.sh"]`, `CMD ["mtrtk","run"]`, `HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 CMD ["mtrtk","healthcheck"]`, `STOPSIGNAL SIGTERM`.
- `entrypoint.sh`: runs as root only to `chown -R mtrtk:mtrtk /data` when `/data` is not owned by uid 1000 (first run on a fresh bind mount), then `exec gosu mtrtk "$@"` (install `gosu` in the runtime stage). With `MTRTK_RUN_AS_ROOT=1` skips the drop (escape hatch for odd device permissions).
- Compose: `group_add: ["${DIALOUT_GID:-20}"]`, `stop_grace_period: 25s`, `read_only: false` (SQLite + logs live in `/data`), `tmpfs: [/tmp]`, `security_opt: [no-new-privileges:true]`, `cap_drop: [ALL]` (host networking needs no caps; serial access is via cgroup rules + group), `logging: {driver: json-file, options: {max-size: "10m", max-file: "5"}}`.

- [ ] **Step 1: Test for `log_level`**

Append to `tests/unit/test_config.py`:
```python
def test_log_level_default_and_validation(tmp_path: Path) -> None:
    assert Settings(_env_file=None, data_dir=tmp_path, ntrip_password="x").log_level == "INFO"
    assert Settings(_env_file=None, data_dir=tmp_path, ntrip_password="x", log_level="DEBUG").log_level == "DEBUG"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, data_dir=tmp_path, ntrip_password="x", log_level="LOUD")
```
Run → FAIL. Add the field to `Settings`; in `Daemon.__init__` (or `cli.run` before constructing the daemon) call `logging.basicConfig(level=settings.log_level, ...)`. Run → PASS.

- [ ] **Step 2: Entrypoint**

`docker/entrypoint.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" = "0" ] && [ "${MTRTK_RUN_AS_ROOT:-0}" != "1" ]; then
  if [ -d /data ] && [ "$(stat -c %u /data)" != "1000" ]; then
    echo "entrypoint: taking ownership of /data for uid 1000" >&2
    chown -R 1000:1000 /data
  fi
  exec gosu 1000:1000 "$@"
fi
exec "$@"
```
`chmod +x docker/entrypoint.sh`.

- [ ] **Step 3: Dockerfile runtime stage changes**

Add to the `apt-get install` line: `gosu`. After copying the app:
```dockerfile
RUN groupadd -g 1000 mtrtk && useradd -u 1000 -g 1000 -G dialout -m -s /usr/sbin/nologin mtrtk \
 && mkdir -p /data && chown mtrtk:mtrtk /data
COPY docker/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["mtrtk", "run"]
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 CMD ["mtrtk", "healthcheck"]
```
(`USER` stays root so the entrypoint can chown; `gosu` drops privileges.) Keep the Phase 1 stage names (`web`, `rtklib`, `runtime`).

- [ ] **Step 4: Compose hardening on the `mtrtk` service**

```yaml
    group_add: ["${DIALOUT_GID:-20}"]
    stop_grace_period: 25s
    tmpfs: [/tmp]
    security_opt: [no-new-privileges:true]
    cap_drop: [ALL]
    cap_add: [CHOWN, SETUID, SETGID]   # entrypoint chown + gosu drop only
    logging:
      driver: json-file
      options: {max-size: "10m", max-file: "5"}
```
`.env.example`: add `LOG_LEVEL=INFO` and `DIALOUT_GID=20  # $(getent group dialout | cut -d: -f3) if your host differs`.

- [ ] **Step 5: Verify**

```bash
docker compose build && docker compose up -d && sleep 45 && docker inspect --format '{{.State.Health.Status}}' mtrtk
docker exec mtrtk id                          # uid=1000(mtrtk) gid=1000(mtrtk) groups=1000,20(dialout)
docker exec mtrtk ls -l /dev/ttyACM0          # crw-rw---- root dialout
docker logs mtrtk --tail 20                   # profile applied, no permission errors
ls -ln data | head -3                          # owned by 1000
```
Expected health `healthy`; the daemon reads the receiver as non-root. If the receiver open fails with `EACCES`, record the host's dialout gid and confirm `DIALOUT_GID` fixes it.

- [ ] **Step 6: Commit**

```bash
git add docker/Dockerfile docker/entrypoint.sh docker-compose.yml .env.example src/mtrtk/config.py src/mtrtk/daemon.py src/mtrtk/cli.py tests/unit/test_config.py
git commit -m "build: run container as non-root with gosu entrypoint, cap_drop, healthcheck and LOG_LEVEL

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Documentation set

**Files:**
- Create: `docs/setup.md`, `docs/hardware.md`, `docs/exposure.md`, `docs/firmware.md`, `docs/troubleshooting.md`
- Modify: `README.md` (rewrite as landing page), `docs/base.md`, `docs/rover.md` (link updates only)

Each doc is written for a user who has the hardware and a Linux box but no mtrtk knowledge. Real commands, real paths, no placeholders other than `<...>` for user values. Screenshots are not required; ASCII layouts are fine.

- [ ] **Step 1: `README.md`** (≤ 150 lines)

Sections: one-paragraph what it is; feature list (base / rover / PPK / PPP / ROS 2 / INS drivers status: planned); **Quick start** (Docker):
```bash
git clone https://github.com/<you>/mtrtk && cd mtrtk
cp .env.example .env && $EDITOR .env      # ROLE, NTRIP_PASSWORD, STATION_ID at least
docker compose up -d
docker compose exec mtrtk mtrtk doctor
# UI: http://<tailscale-ip>:8080   NTRIP: ntrip://<user>:<pass>@<tailscale-ip>:2101/MTRK
```
Native alternative: `./install.sh`. Docs index table (every `docs/*.md` with one line). Supported hardware table (ZED-F9P verified; SBG Ellipse-D / VectorNav VN-200 spec-based). Architecture diagram (from spec, condensed). License.

- [ ] **Step 2: `docs/setup.md`**

Host prep (64-bit OS, Docker install one-liner, Tailscale install + `tailscale up`, `usermod -aG docker`), clone, `.env` walkthrough grouped by section with the 8 values that matter first, `docker compose up -d`, verifying with `doctor` and the UI, updating (`git pull && docker compose pull && docker compose up -d` or `docker compose build` for source builds), logs (`docker compose logs -f`), backups (`mtrtk backup`), moving to a new host (`restore`), Raspberry Pi notes (use an SSD or high-endurance card; `MIN_FREE_GB`; power supply; disable Wi-Fi power save), Jetson notes (arm64 image, `/dev/ttyACM` naming), native install (`install.sh`, `journalctl -u mtrtk -f`, `uninstall.sh`).

- [ ] **Step 3: `docs/hardware.md`**

SparkFun GPS-RTK-SMA board facts (USB-C CDC, VID 1546, u.FL/SMA, no antenna detect wiring → `CFG-HW-ANT` off), antenna: multi-band magnetic mount needs a ≥10 cm metal ground plane (or the roof itself), clear sky ≥ 10° elevation, away from metal edges and Wi-Fi/LTE antennas ≥ 1 m, cable loss (5 m RG174 ≈ 3–4 dB at L1/L2, fine), fixed mounting for a base (any movement invalidates the PPP position), **ARP vs antenna phase center** (this antenna has no ANTEX calibration; all mtrtk positions refer to the ARP; expect a cm-level vertical offset vs. calibrated antennas; set `ANTENNA_HEIGHT_M` to the ARP height above the marker), two-receiver hosts (`CFG_USB_SERIAL_NO_STR`), power (Pi USB port is fine; 5 V/100 mA), ModemManager, RF interference indicators in the Receiver page and what to do (jamming → move antenna; AGC pinned → check cable; noise high → USB 3 hubs).

- [ ] **Step 4: `docs/exposure.md`**

Three paths with a comparison table (transport, TLS, auth, NTRIP v1 support, needs router config, cost):

| | Tailscale (default) | Public IP + Caddy | Cloudflare Tunnel |
|---|---|---|---|
| Web UI | http over WireGuard | https (Let's Encrypt) | https (Cloudflare) |
| NTRIP | v1+v2 plain TCP 2101 | v1+v2 plain TCP 2101 (router forward) | v2 over HTTPS only |
| Auth | tailnet identity (+ optional password) | WEB_PASSWORD required | WEB_PASSWORD or Cloudflare Access |
| Router changes | none | forward 443 + 2101 | none |

Per path: `.env` values, commands, verification (`scripts/check-exposure.sh`), client setup examples (SW Maps, Emlid Flow, u-center, `str2str`) and which work on which path, threat notes (never `WEB_ALLOW_INSECURE=1` on a public bind; rotate `NTRIP_PASSWORD` when sharing with third parties; Cloudflare sees plaintext at their edge).

- [ ] **Step 5: `docs/firmware.md`**

Why upgrade (1.13 → 1.51: L5-capable RINEX with `-f 3`, MON-SPAN, improved RTK, `lastCorrectionAge`), how to check (`mtrtk doctor --probe` or the Receiver page), the upgrade procedure (Windows + u-center 1 (not u-center 2): Tools → Firmware Update → `UBX_F9_100_HPG151.xxx.bin` from u-blox, "Use this baudrate for update" 9600, enter safeboot, ~2 min), **after upgrade**: mtrtk re-applies its profile automatically on next connect (flash layer is wiped by the update), re-run survey-in or re-activate the site, the capability list in the Receiver page changes. What mtrtk does on each version (table from spec: gated keys/messages).

- [ ] **Step 6: `docs/troubleshooting.md`**

Symptom → cause → fix table, at least: no receiver found; permission denied; ModemManager; container healthy but UI empty (WS blocked by proxy/adblock); Tailscale IP missing on boot (`After=tailscaled`, retry loop explained); "no RTCM 1005" (survey-in not done / TMODE off); rover stays FLOAT (baseline > 20 km, base position poor, MSM mismatch, antenna); hour files missing (disk full / retention); RINEX export rejected by CSRS (duration, interval, marker name); PPK 0 % fixed (nav file, time overlap, wrong base coordinates); ROS 2 node no topics (`ws_url`); clock skew alert; high CPU on Pi 3 (reduce `ROVER_NAV_HZ`).

- [ ] **Step 7: Verify links and commit**

```bash
uv run python - <<'EOF'
import re, pathlib
bad = []
for md in list(pathlib.Path("docs").glob("*.md")) + [pathlib.Path("README.md")]:
    for m in re.finditer(r"\]\(((?!https?://)[^)#]+)", md.read_text()):
        target = (md.parent / m.group(1)).resolve()
        if not target.exists(): bad.append((md, m.group(1)))
print(bad or "all relative links resolve")
EOF
git add README.md docs
git commit -m "docs: setup, hardware, exposure, firmware, troubleshooting guides and README landing page

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Release pipeline

**Files:**
- Create: `.github/workflows/release.yml`, `CHANGELOG.md`, `scripts/bump-version.py`, `tests/unit/test_version_sync.py`
- Modify: `.github/workflows/ci.yml` (image build on `main` pushes tagged `edge`), `web/package.json` (version field kept in sync)

**Interfaces:**
- `scripts/bump-version.py X.Y.Z`: rewrites `pyproject.toml` `version`, `src/mtrtk/__init__.py` `__version__`, `web/package.json` `version`, `ros2/mtrtk_msgs/package.xml` and `ros2/mtrtk_bridge/package.xml` `<version>`, and moves the `## [Unreleased]` section of `CHANGELOG.md` under `## [X.Y.Z] - YYYY-MM-DD`.
- `test_version_sync.py`: all five version sources agree.
- `release.yml` on `push: tags: ['v*']`: check that the tag equals the package version; `uv run pytest tests/unit`; buildx multi-arch (`linux/amd64,linux/arm64`) push `ghcr.io/<owner>/mtrtk:{version,latest}`; ROS images `ghcr.io/<owner>/mtrtk-ros2:{version-humble,version-jazzy,humble,jazzy}`; `softprops/action-gh-release` with the changelog section as body and `mtrtk-<version>-plans.tar.gz` (docs) as an asset. Uses `GITHUB_TOKEN` with `packages: write`.
- `ci.yml`: on `main` push, after tests, build+push `ghcr.io/<owner>/mtrtk:edge` (amd64+arm64). PR builds stay `push: false`.

- [ ] **Step 1: Version sync test**

`tests/unit/test_version_sync.py`:
```python
import json
import re
import tomllib
from pathlib import Path

import mtrtk

ROOT = Path(__file__).resolve().parents[2]


def test_all_version_sources_agree() -> None:
    py = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    web = json.loads((ROOT / "web/package.json").read_text())["version"]
    versions = {"pyproject": py, "__init__": mtrtk.__version__, "web": web}
    for pkg in ("mtrtk_msgs", "mtrtk_bridge"):
        xml = (ROOT / "ros2" / pkg / "package.xml").read_text()
        versions[pkg] = re.search(r"<version>([^<]+)</version>", xml).group(1)
    assert len(set(versions.values())) == 1, versions


def test_changelog_has_unreleased_section() -> None:
    assert "## [Unreleased]" in (ROOT / "CHANGELOG.md").read_text()
```
Run → FAIL (no CHANGELOG; versions may differ).

- [ ] **Step 2: `CHANGELOG.md`** (Keep a Changelog format)

```markdown
# Changelog

All notable changes to mtrtk are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

### Added
- Base station: raw UBX logging, NTRIP caster (v1/v2), survey-in and fixed sites, RINEX export presets, PPP result import.
- Web UI: dashboard, satellites, receiver, corrections, site, logs, history, events, settings.
- Rover: u-blox driver, NTRIP client, NMEA/JSON outputs, sessions and survey points, RTK and Survey pages.
- ROS 2 bridge (Humble, Jazzy), PPK pipeline with camera events, exposure profiles, native install, doctor, backup/restore.
```

- [ ] **Step 3: `scripts/bump-version.py`**

```python
#!/usr/bin/env python3
"""Usage: scripts/bump-version.py 0.2.0 — update every version source and cut the changelog section."""
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
version = sys.argv[1]
if not re.fullmatch(r"\d+\.\d+\.\d+", version):
    sys.exit("version must be MAJOR.MINOR.PATCH")


def sub(path: Path, pattern: str, repl: str) -> None:
    text = path.read_text()
    new, n = re.subn(pattern, repl, text, count=1, flags=re.M)
    if n != 1:
        sys.exit(f"pattern not found in {path}: {pattern}")
    path.write_text(new)


sub(ROOT / "pyproject.toml", r'^version = "[^"]+"', f'version = "{version}"')
sub(ROOT / "src/mtrtk/__init__.py", r'^__version__ = "[^"]+"', f'__version__ = "{version}"')
pkg = ROOT / "web/package.json"
data = json.loads(pkg.read_text())
data["version"] = version
pkg.write_text(json.dumps(data, indent=2) + "\n")
for name in ("mtrtk_msgs", "mtrtk_bridge"):
    sub(ROOT / "ros2" / name / "package.xml", r"<version>[^<]+</version>", f"<version>{version}</version>")
today = dt.date.today().isoformat()
sub(ROOT / "CHANGELOG.md", r"^## \[Unreleased\]\n", f"## [Unreleased]\n\n## [{version}] - {today}\n")
print(f"bumped to {version}; now: git commit -am 'chore: release v{version}' && git tag v{version} && git push --tags")
```
`chmod +x scripts/bump-version.py`. Run it once with the current version (`0.1.0`) so all sources agree (ROS `package.xml` files were created in Phase 7 with `0.1.0`; verify). Then `git checkout CHANGELOG.md` to undo the section cut (the first release cuts it for real).

- [ ] **Step 4: `release.yml`**

```yaml
name: release
on:
  push:
    tags: ["v*"]
permissions:
  contents: write
  packages: write
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --frozen
      - name: tag matches package version
        run: |
          v=$(uv run python -c 'import mtrtk; print(mtrtk.__version__)')
          [ "v$v" = "${GITHUB_REF_NAME}" ] || { echo "tag $GITHUB_REF_NAME != v$v"; exit 1; }
      - run: uv run pytest tests/unit -q
  images:
    needs: verify
    runs-on: ubuntu-latest
    strategy:
      matrix:
        include:
          - {name: mtrtk, file: docker/Dockerfile, context: ., suffix: ""}
          - {name: mtrtk-ros2, file: ros2/Dockerfile, context: ., suffix: "-humble", args: "ROS_DISTRO=humble"}
          - {name: mtrtk-ros2, file: ros2/Dockerfile, context: ., suffix: "-jazzy", args: "ROS_DISTRO=jazzy"}
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-qemu-action@v3
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with: {registry: ghcr.io, username: "${{ github.actor }}", password: "${{ secrets.GITHUB_TOKEN }}"}
      - id: meta
        run: |
          v="${GITHUB_REF_NAME#v}"; owner="${GITHUB_REPOSITORY_OWNER,,}"
          tags="ghcr.io/$owner/${{ matrix.name }}:$v${{ matrix.suffix }}"
          if [ -z "${{ matrix.suffix }}" ]; then tags="$tags,ghcr.io/$owner/${{ matrix.name }}:latest"; else tags="$tags,ghcr.io/$owner/${{ matrix.name }}:${{ matrix.suffix }}"; tags="${tags//:-/:}"; fi
          echo "tags=$tags" >> "$GITHUB_OUTPUT"
      - uses: docker/build-push-action@v6
        with:
          context: ${{ matrix.context }}
          file: ${{ matrix.file }}
          platforms: linux/amd64,linux/arm64
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          build-args: ${{ matrix.args }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
  github-release:
    needs: images
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: changelog section
        run: |
          v="${GITHUB_REF_NAME#v}"
          awk -v v="$v" '/^## \[/{p=($0 ~ "\\["v"\\]")} p' CHANGELOG.md > body.md
          tar czf "mtrtk-$v-docs.tar.gz" docs README.md
      - uses: softprops/action-gh-release@v2
        with:
          body_path: body.md
          files: mtrtk-*-docs.tar.gz
```
The ROS `Dockerfile` must accept `ARG ROS_DISTRO` (Phase 7 built it that way; verify). `ci.yml`: add a job `edge-image` with `if: github.ref == 'refs/heads/main' && github.event_name == 'push'` that pushes `ghcr.io/<owner>/mtrtk:edge` using the same buildx steps.

- [ ] **Step 5: Verify locally and commit**

`uv run pytest tests/unit/test_version_sync.py -q` → `2 passed`; `uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/release.yml'))"` (PyYAML is available via dependencies; if not, `uvx yamllint`). Update `docs/setup.md` "Updating" to mention `ghcr.io` tags (`latest`, `edge`, `x.y.z`) and add a `## Releasing` section to `CONTRIBUTING.md` (create if absent: dev setup, tests, plan/spec workflow, releasing with `bump-version.py`).
```bash
git add .github/workflows scripts/bump-version.py CHANGELOG.md CONTRIBUTING.md tests/unit/test_version_sync.py web/package.json ros2 pyproject.toml src/mtrtk/__init__.py docs/setup.md
git commit -m "ci: release workflow publishing multi-arch images to ghcr and GitHub releases; version bump script

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Fresh-host milestone and acceptance checklist

**Files:**
- Create: `docs/acceptance.md`
- Modify: `docs/superpowers/plans/2026-09-19-phase9-exposure-hardening.md` (record results in the ledger, not the plan)

**Milestone (spec Phase 9):** fresh Pi (or a fresh Ubuntu VM if no Pi is at hand — say which in the report): `git clone` → edit `.env` → `docker compose up -d` → RTK from a phone NTRIP client over Tailscale, then over the Cloudflare domain.

- [ ] **Step 1: Write `docs/acceptance.md`** — the end-to-end checklist from the spec's *Verification* section, as a table with columns *Step*, *Command / action*, *Expected*, *Result (date, host, firmware)*. Rows: 1 compose up on dev box (UI, hourly files, `str2str` receives 1005/1077…); 2 survey-in → site → fixed (fixType 5, 1005 matches within 0.1 mm); 3 24 h RINEX export → CSRS-PPP → import → fixed; 4 rover role → RTK FIXED → NMEA in QGIS → points exported → `ros2 topic echo /mtrtk/fix`; 5 PPK zero-baseline ≥ 95 % fixed + real session; 6 fresh host: clone/`.env`/up → phone RTK over Tailscale → over Cloudflare; 7 `install.sh` native path on the same host after `docker compose down`; 8 `mtrtk backup` on host A → `restore` on host B → sites present; 9 power-cycle the receiver while running → reconnect, profile re-applied, logging resumes within 30 s; 10 reboot host → container up, Tailscale bind retried until ready, no 0.0.0.0 fallback.

- [ ] **Step 2: Run rows 6–10 now** (rows 1–5 were exercised in their phases; re-run 1 quickly for regression).

Fresh host procedure:
```bash
# on the fresh host
curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER && newgrp docker
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
git clone <repo> && cd mtrtk && cp .env.example .env
$EDITOR .env    # ROLE=base NTRIP_PASSWORD=… STATION_ID=… WEB_PASSWORD=… TUNNEL_TOKEN=… WEB_BIND=lan NTRIP_BIND=lan
docker compose --profile cloudflare up -d
docker compose exec mtrtk mtrtk doctor
```
Phone (mobile data, Tailscale app on): SW Maps NTRIP → `<tailscale-ip>:2101/MTRK` → FIXED. Then Tailscale off → `https://ntrip.<domain>:443/MTRK` → FIXED (record time to fix and any stall). Browser: `https://rtk.<domain>` → login → dashboard live.

- [ ] **Step 3: Record and commit**

Fill the *Result* column for the rows run; anything failing becomes a fix commit in this task (or a ledgered issue if it belongs to another phase).
```bash
git add docs/acceptance.md
git commit -m "docs: end-to-end acceptance checklist with fresh-host results

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Phase 9 exit criteria

- `mtrtk doctor` reports the host's real state; `--json` works; exit code 1 on FAIL only.
- `install.sh` on a fresh Debian-family host yields a running `mtrtk.service` and passing `doctor`.
- Container runs as uid 1000 with dialout access, `cap_drop: ALL`, healthcheck healthy.
- `docker compose --profile cloudflare up -d` gives RTK FIXED on a phone via `https://ntrip.<domain>` and the UI via `https://rtk.<domain>`; `--profile public` serves the UI over Let's Encrypt TLS.
- Docs set complete; all relative links resolve.
- `git tag v0.1.0 && git push --tags` publishes `ghcr.io/<owner>/mtrtk:0.1.0` (amd64+arm64), ROS images and a GitHub Release.
- `docs/acceptance.md` rows 1, 6–10 filled with dated results.

Phase 10 (INS drivers, spec-based) is the last plan: `docs/superpowers/plans/2026-09-19-phase10-ins-drivers.md`.
