"""The native install files: syntax, the unit template, and agreement with the Docker image."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = [ROOT / "install.sh", ROOT / "uninstall.sh"]
UNIT = ROOT / "systemd" / "mtrtk.service"
RULE = ROOT / "udev" / "99-mtrtk-ublox.rules"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_parse_and_are_executable(script: Path) -> None:
    assert os.access(script, os.X_OK), f"chmod +x {script.name}"
    bash = shutil.which("bash")
    assert bash is not None
    subprocess.run([bash, "-n", str(script)], check=True)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_have_a_dry_run_and_help(script: Path) -> None:
    text = script.read_text()
    assert "--dry-run" in text and "set -euo pipefail" in text
    out = subprocess.run([str(script), "--help"], check=True, capture_output=True, text=True).stdout
    assert "usage:" in out and "--dry-run" in out


def test_unit_template_is_rendered_from_the_clone_and_the_dotenv() -> None:
    unit = UNIT.read_text()
    assert "User=__USER__" in unit
    assert "WorkingDirectory=__REPO__" in unit
    assert "ExecStart=__REPO__/.venv/bin/mtrtk run" in unit
    assert "Environment=MTRTK_ENV_FILE=__REPO__/.env" in unit
    # A variable set by systemd outranks .env, so a setting changed in the web UI would never
    # take effect after a restart; and systemd's EnvironmentFile parser is not python-dotenv's.
    settings = [line for line in unit.splitlines() if not line.startswith("#")]
    assert not any(line.startswith("EnvironmentFile=") for line in settings)
    assert not any(line.startswith("Environment=DATA_DIR") for line in settings)
    assert "Restart=always" in unit


def test_unit_stop_timeout_matches_the_compose_grace() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    grace = re.search(r"stop_grace_period:\s*(\d+)s", compose)
    unit = re.search(r"^TimeoutStopSec=(\d+)$", UNIT.read_text(), re.M)
    assert grace is not None and unit is not None
    assert unit.group(1) == grace.group(1)


def test_install_builds_the_rtklib_tag_the_image_builds() -> None:
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text()
    image = re.search(r"^ARG RTKLIB_TAG=(\S+)$", dockerfile, re.M)
    native = re.search(r'^RTKLIB_TAG="([^"]+)"', (ROOT / "install.sh").read_text(), re.M)
    assert image is not None and native is not None
    assert native.group(1) == image.group(1)


def test_udev_rule_keeps_modemmanager_off_ublox_ports() -> None:
    rules = [line for line in RULE.read_text().splitlines() if line and not line.startswith("#")]
    assert rules and all('ATTRS{idVendor}=="1546"' in line for line in rules)
    assert any('SUBSYSTEM=="tty"' in r and 'ENV{ID_MM_DEVICE_IGNORE}="1"' in r for r in rules)
    assert any('GROUP="dialout"' in r and 'MODE="0660"' in r for r in rules)


def test_install_and_uninstall_agree_on_the_installed_paths() -> None:
    install = (ROOT / "install.sh").read_text()
    uninstall = (ROOT / "uninstall.sh").read_text()
    for path in ("/etc/systemd/system/", "/etc/udev/rules.d/"):
        assert path in install and path in uninstall
    assert RULE.name in install and RULE.name in uninstall
