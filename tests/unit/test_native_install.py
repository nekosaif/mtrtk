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


def test_unit_comments_carry_no_placeholders() -> None:
    """render_unit substitutes the whole file: a placeholder in a comment became the clone path
    in the installed unit's header."""
    comments = [line for line in UNIT.read_text().splitlines() if line.startswith("#")]
    assert not any("__REPO__" in line or "__USER__" in line for line in comments)


def test_unit_hardening() -> None:
    """Files the daemon creates (exports, a restored database, logs) are not world-readable, and
    the costless sandboxing is on. ProtectClock is not: it implies a DeviceAllow= list, which
    would close every serial port."""
    lines = set(UNIT.read_text().splitlines())
    for directive in (
        "UMask=0027",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        "SystemCallArchitectures=native",
        "ProtectHostname=true",
        "RestrictNamespaces=true",
        "RestrictRealtime=true",
        "ProtectKernelLogs=true",
        "ProtectProc=invisible",
    ):
        assert directive in lines, directive
    assert not any(
        line.startswith(("ProtectClock=", "DeviceAllow=", "PrivateDevices=")) for line in lines
    )


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


# ---------------------------------------------------------------- install.sh, run hermetically
# The script runs against a copy of the few clone files it reads, with PATH holding only the
# tools it needs and stubs for everything that would touch the system (sudo, systemctl, apt...).
# Only --dry-run and the print-only modes are run: nothing here may change this machine.

_REAL_TOOLS = (
    "bash", "env", "dirname", "id", "cut", "sed", "grep", "tail", "head", "tr", "cat",
    "mkdir", "date", "mktemp", "rm", "cp", "getent",
)  # fmt: skip
_STUBS = {
    "sudo": 'echo "sudo $*" >>"$STUB_LOG"; exit 0',
    "uv": 'echo "uv $*" >>"$STUB_LOG"; exit 0',
    "systemctl": 'echo "systemctl $*" >>"$STUB_LOG"; exit 0',
    "udevadm": 'echo "udevadm $*" >>"$STUB_LOG"; exit 0',
    "apt-get": 'echo "apt-get $*" >>"$STUB_LOG"; exit 0',
    "tailscale": "exit 1",
    "dpkg-query": "printf 'install ok installed'",
    "curl": 'echo "curl $*" >>"$STUB_LOG"; exit 0',
}


def _clone(tmp_path: Path, name: str = "mtrtk") -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True)
    for f in ("pyproject.toml", ".env.example", "install.sh"):
        shutil.copy2(ROOT / f, repo / f)
    for d in ("systemd", "udev"):
        shutil.copytree(ROOT / d, repo / d)
    return repo


def _path(tmp_path: Path, *, without: tuple[str, ...] = (), root: bool = False) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in _REAL_TOOLS:
        real = shutil.which(tool)
        assert real is not None, tool
        (bin_dir / tool).symlink_to(real)
    for name, body in _STUBS.items():
        if name not in without:
            (bin_dir / name).write_text(f"#!{shutil.which('bash')}\n{body}\n")
            (bin_dir / name).chmod(0o755)
    if root:
        (bin_dir / "id").unlink()
        real_id = shutil.which("id")
        fake_root = '[ "$1" = -u ] && { echo 0; exit 0; }'
        (bin_dir / "id").write_text(f'#!{shutil.which("bash")}\n{fake_root}\nexec {real_id} "$@"\n')
        (bin_dir / "id").chmod(0o755)
    return bin_dir


def _install(
    repo: Path, *args: str, without: tuple[str, ...] = (), root: bool = False
) -> subprocess.CompletedProcess[str]:
    tmp = repo.parent
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": str(_path(tmp, without=without, root=root)),
        "HOME": str(home),
        "LC_ALL": "C",
        "STUB_LOG": str(tmp / "stub.log"),
    }
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(
        [bash, str(repo / "install.sh"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _stub_log(repo: Path) -> str:
    log = repo.parent / "stub.log"
    return log.read_text() if log.exists() else ""


def test_install_dry_run_plans_every_step_and_changes_nothing(tmp_path: Path) -> None:
    repo = _clone(tmp_path)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    r = _install(repo, "--dry-run", "--no-web")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert f"+ uv --directory {repo} sync --frozen --no-dev" in out
    assert f"+ cp .env.example .env (DATA_DIR={repo}/data" in out
    assert f"+ mkdir -p {repo}/data" in out
    rule = "99-mtrtk-ublox.rules"
    assert f"+ sudo install -D -m 0644 {repo}/udev/{rule} /etc/udev/rules.d/{rule}" in out
    assert f"__REPO__={repo}" in out and "/etc/systemd/system/mtrtk.service" in out
    assert f"+ {repo}/.venv/bin/mtrtk doctor" in out
    # Nothing ran: no stub was called and no file appeared (the PATH stubs and HOME are ours).
    assert _stub_log(repo) == ""
    after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    assert [p for p in after if p not in before and p.parts[0] not in ("bin", "home")] == []


def test_install_renders_the_unit_for_this_clone_and_user(tmp_path: Path) -> None:
    repo = _clone(tmp_path)
    r = _install(repo, "--render-unit")
    assert r.returncode == 0, r.stderr
    user = subprocess.run(["id", "-un"], check=True, capture_output=True, text=True).stdout
    expected = UNIT.read_text().replace("__REPO__", str(repo)).replace("__USER__", user.strip())
    assert r.stdout == expected
    assert "__" not in r.stdout.replace("__init__", "")
    assert f"ReadWritePaths={repo}" in r.stdout
    assert _stub_log(repo) == ""


def test_install_new_env_is_the_example_with_this_clone_and_a_password(tmp_path: Path) -> None:
    repo = _clone(tmp_path)
    r = _install(repo, "--print-env")
    assert r.returncode == 0, r.stderr
    example = (ROOT / ".env.example").read_text().splitlines()
    lines = r.stdout.splitlines()
    assert len(lines) == len(example)
    for want, got in zip(example, lines, strict=True):
        if want.startswith("DATA_DIR="):
            assert got == f"DATA_DIR={repo}/data"
        elif want.startswith("NTRIP_PASSWORD=change-me"):
            m = re.fullmatch(r"NTRIP_PASSWORD=([A-Za-z0-9]{20})(\s+#.*)?", got)
            assert m is not None, got
            assert (m.group(2) or "") == want.removeprefix("NTRIP_PASSWORD=change-me")
        else:
            assert got == want
    assert not (repo / ".env").exists()


container_data_dir = pytest.mark.skipif(
    os.access("/data", os.W_OK), reason="/data is writable here, so DATA_DIR=/data would work"
)


@container_data_dir
@pytest.mark.parametrize(
    "line",
    [
        "DATA_DIR=/data\n",
        'export DATA_DIR="/data"   # the container path\n',
        "DATA_DIR='/data'\n",
        "DATA_DIR=\n",
        "",  # no DATA_DIR at all: the default is /data
    ],
    ids=["bare", "export-quoted-comment", "single-quoted", "empty", "absent"],
)
def test_install_points_a_container_data_dir_at_the_clone(tmp_path: Path, line: str) -> None:
    """`cp .env.example .env` (DATA_DIR=/data), or moving from Docker: the service, running as
    this user, could not create /data and would never start."""
    repo = _clone(tmp_path)
    env = repo / ".env"
    env.write_text(f"STATION_ID=MTRK\n{line}NTRIP_PASSWORD=x\n")
    r = _install(repo, "--dry-run", "--no-web", "--rtklib", "skip")
    assert r.returncode == 0, r.stderr
    assert f"setting it to {repo}/data" in r.stdout
    assert f"+ set DATA_DIR={repo}/data in .env" in r.stdout
    assert env.read_text() == f"STATION_ID=MTRK\n{line}NTRIP_PASSWORD=x\n"  # dry run


def test_install_leaves_a_clone_data_dir_alone(tmp_path: Path) -> None:
    repo = _clone(tmp_path)
    (repo / ".env").write_text(f"DATA_DIR='{repo}/data'  # native\n")
    r = _install(repo, "--dry-run", "--no-web", "--rtklib", "skip")
    assert r.returncode == 0, r.stderr
    assert ".env exists; left as it is" in r.stdout
    assert "DATA_DIR" not in r.stdout + r.stderr


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--rtklib", "bogus"), "--rtklib takes source, apt or skip (got 'bogus')"),
        (("--rtklib",), "--rtklib takes source, apt or skip"),
        (("--rtklib=",), "--rtklib takes source, apt or skip (got '')"),
        (("--frobnicate",), "unknown option: --frobnicate"),
    ],
)
def test_install_rejects_bad_options(tmp_path: Path, args: tuple[str, ...], message: str) -> None:
    repo = _clone(tmp_path)
    r = _install(repo, "--dry-run", *args)
    assert r.returncode == 1 and message in r.stderr
    assert _stub_log(repo) == ""


def test_install_refuses_a_clone_path_systemd_cannot_carry(tmp_path: Path) -> None:
    repo = _clone(tmp_path, "my clone")
    r = _install(repo, "--dry-run")
    assert r.returncode == 1 and "has a space" in r.stderr


def test_install_refuses_root(tmp_path: Path) -> None:
    repo = _clone(tmp_path)
    r = _install(repo, "--dry-run", root=True)
    assert r.returncode == 1 and "not as root" in r.stderr


def test_install_without_dpkg_uses_the_curl_it_finds(tmp_path: Path) -> None:
    """A host with another package manager: curl present is enough."""
    repo = _clone(tmp_path)
    r = _install(
        repo, "--dry-run", "--no-web", "--rtklib", "skip", without=("dpkg-query", "apt-get")
    )
    assert r.returncode == 0, r.stderr
    assert "curl and ca-certificates are needed" not in r.stderr


def test_install_without_dpkg_or_curl_says_what_is_missing(tmp_path: Path) -> None:
    repo = _clone(tmp_path)
    r = _install(repo, "--dry-run", "--no-web", without=("dpkg-query", "apt-get", "curl"))
    assert r.returncode == 1 and "curl is needed" in r.stderr
