"""How the mtrtk image runs: non-root behind docker/entrypoint.sh, with a healthcheck and the
compose service's capability and logging limits.

No Docker here: building the image and running it (`docker inspect` health, `id` inside the
container) is the real check. These tests keep the Dockerfile, the entrypoint, the compose
service and `.env.example` in step, and run the entrypoint and the image's `mtrtk` wrapper for
real: as non-root directly, and their root paths against stub `id`/`find`/`setpriv`/`tini`.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "docker" / "Dockerfile"
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"


def _runtime_stage() -> str:
    text = DOCKERFILE.read_text()
    start = text.index("AS runtime")
    return text[start:]


def _compose_service(name: str) -> str:
    """The text of one service block in docker-compose.yml (two-space indented, under services)."""
    text = (ROOT / "docker-compose.yml").read_text()
    match = re.search(rf"^  {re.escape(name)}:\n((?:    .*\n|\s*\n)+)", text, re.M)
    assert match, f"docker-compose.yml has no {name} service"
    return match.group(1)


def test_the_image_drops_to_uid_1000_through_the_entrypoint() -> None:
    stage = _runtime_stage()
    assert "useradd -u 1000 -g 1000 -G dialout" in stage
    assert "groupadd -g 1000 mtrtk" in stage
    assert "COPY --chmod=0755 docker/entrypoint.sh /entrypoint.sh" in stage
    assert 'ENTRYPOINT ["/entrypoint.sh"]' in stage
    assert re.search(r"apt-get install .*\btini\b", stage)
    assert 'CMD ["mtrtk", "run"]' in stage
    assert "STOPSIGNAL SIGTERM" in stage
    # No `USER`: the entrypoint must start as root to fix /data's ownership, then drops itself.
    assert not re.search(r"^USER ", stage, re.M)
    # /app is root-owned, so the settings file the web UI rewrites has to live in the volume.
    assert "ENV MTRTK_ENV_FILE=/data/.env" in stage


def test_the_image_carries_its_own_healthcheck() -> None:
    assert (
        "HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 "
        'CMD ["mtrtk", "healthcheck"]'
    ) in _runtime_stage()


def test_the_entrypoint_takes_data_then_drops_with_the_containers_groups() -> None:
    script = ENTRYPOINT.read_text()
    assert script.startswith("#!/usr/bin/env bash\n")
    assert re.search(r"^set -euo pipefail$", script, re.M)
    assert '-exec chown -h "$APP_UID:$APP_GID" {} +' in script
    assert "could not take ownership" in script  # a failed chown warns, it does not abort
    assert 'MTRTK_RUN_AS_ROOT:-0}" = "1"' in script
    # setpriv, not gosu: gosu resets the supplementary groups from /etc/group, which would drop
    # the host dialout gid compose's group_add hands the container.
    assert "exec setpriv --reuid=" in script and '--groups="$groups"' in script
    assert "grep -vx 0" in script  # never keep the root group
    # tini after the drop, as the daemon's own user: a root tini with no CAP_KILL cannot signal
    # a uid-1000 child, so `docker stop` would kill the daemon instead of stopping it.
    assert "init=(tini -s --)" in script
    assert script.rstrip().endswith('-- "${init[@]}" "$@"')
    assert "exec gosu" not in script


@pytest.mark.parametrize("script", ["docker/entrypoint.sh", "docker/mtrtk-as-daemon.sh"])
def test_the_entrypoint_is_executable_in_git(script: str) -> None:
    assert os.access(ROOT / script, os.X_OK)
    try:
        out = subprocess.run(
            ["git", "ls-files", "-s", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    if not out:
        pytest.skip(f"{script} is not committed yet")
    assert out.startswith("100755 "), out


def _entrypoint(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    path = f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"
    return subprocess.run(
        [str(ENTRYPOINT), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PATH": path, **env},
        check=False,
    )


needs_non_root = pytest.mark.skipif(os.geteuid() == 0, reason="the root path chowns /data")


@needs_non_root
def test_entrypoint_runs_a_program_unchanged_when_not_root() -> None:
    result = _entrypoint("echo", "hello")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "hello\n"


@needs_non_root
def test_entrypoint_treats_an_option_or_unknown_word_as_an_mtrtk_subcommand() -> None:
    """`docker run IMAGE --version` / `docker run IMAGE doctor` worked when the entrypoint was
    `mtrtk` itself; they still do."""
    version = _entrypoint("--version")
    assert version.returncode == 0, version.stderr
    assert version.stdout.startswith("mtrtk, version ")
    unknown = _entrypoint("no-such-subcommand")
    assert unknown.returncode == 2
    assert "No such command 'no-such-subcommand'" in unknown.stderr


def test_compose_service_is_hardened() -> None:
    svc = _compose_service("mtrtk")
    assert 'group_add: ["${DIALOUT_GID:-20}"]' in svc
    assert "tmpfs: [/tmp]" in svc
    assert "security_opt: [no-new-privileges:true]" in svc
    assert "cap_drop: [ALL]" in svc
    # Only what the entrypoint needs as root; the daemon itself ends up with none.
    assert "cap_add: [CHOWN, DAC_OVERRIDE, SETUID, SETGID]" in svc
    assert "driver: json-file" in svc
    assert 'options: {max-size: "10m", max-file: "5"}' in svc
    assert "stop_grace_period: 30s" in svc


def test_env_example_documents_dialout_gid_and_log_level() -> None:
    text = (ROOT / ".env.example").read_text()
    assert re.search(r"^DIALOUT_GID=20$", text, re.M)
    assert re.search(r"^LOG_LEVEL=INFO\s", text, re.M)


@needs_non_root
def test_entrypoint_runs_export_as_the_mtrtk_subcommand_not_the_shell_builtin() -> None:
    """`command -v export` finds bash's builtin; `docker compose run --rm mtrtk export ...`
    must still reach `mtrtk export` (docs/ppp-workflow.md), as it did when the entrypoint was
    `mtrtk` itself."""
    result = _entrypoint("export", "--help")
    assert result.returncode == 0, result.stderr
    assert "Usage: mtrtk export" in result.stdout


# --- the root paths, run for real against stub `id`/`find`/`setpriv`/`tini` --------------------

_STUBS = {
    # The container's root: `id -u` 0, the image's mtrtk user in 1000 + dialout (20), and the
    # container started with root's group plus compose's group_add (20 and a host gid 4242).
    "id": """#!/bin/sh
case "$*" in
  -u) echo 0 ;;
  "-G mtrtk") echo "1000 20" ;;
  -G) echo "0 20 4242" ;;
  *) exit 1 ;;
esac
""",
    # Records each call. `-print` answers with $STUB_STRANGER (a file not owned by 1000); the
    # `-exec chown` call exits with $STUB_CHOWN_RC, or sleeps for $STUB_CHOWN_SLEEP first.
    "find": """#!/bin/sh
echo "find $*" >> "$STUB_LOG"
case " $* " in
  *" -print "*) [ -n "${STUB_STRANGER:-}" ] && echo "$STUB_STRANGER" ;;
  *" -exec "*)
    if [ -n "${STUB_CHOWN_SLEEP:-}" ]; then
      echo "pid $$" >> "$STUB_LOG"
      exec sleep "$STUB_CHOWN_SLEEP"
    fi
    exit "${STUB_CHOWN_RC:-0}" ;;
esac
exit 0
""",
    "setpriv": """#!/bin/sh
echo "setpriv $*"
""",
    "tini": """#!/bin/sh
while [ "$1" != "--" ]; do shift; done
shift
exec "$@"
""",
}


def _stub_dir(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "stubs"
    bin_dir.mkdir()
    for name, body in _STUBS.items():
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(0o755)
    return bin_dir


def _root_env(tmp_path: Path, **env: str) -> dict[str, str]:
    stubs = _stub_dir(tmp_path)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    path = f"{stubs}{os.pathsep}{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"
    base = {k: v for k, v in os.environ.items() if k != "MTRTK_RUN_AS_ROOT"}
    return {
        **base,
        "PATH": path,
        "STUB_LOG": str(tmp_path / "find.log"),
        "MTRTK_DATA_ROOT": str(data),
        **env,
    }


def _as_root(
    script: Path, tmp_path: Path, *args: str, **env: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=_root_env(tmp_path, **env),
        check=False,
    )


def _find_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "find.log"
    return log.read_text().splitlines() if log.exists() else []


DROP = "setpriv --reuid=1000 --regid=1000 --groups=20,1000,4242 --inh-caps=-all --no-new-privs --"


def test_entrypoint_as_root_drops_with_mtrtks_and_the_containers_groups_but_not_root(
    tmp_path: Path,
) -> None:
    """The drop keeps compose's group_add gid (4242 here), the image's dialout, and never 0."""
    result = _as_root(ENTRYPOINT, tmp_path, "echo", "hi")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{DROP} tini -s -- echo hi\n"


def test_entrypoint_as_root_leaves_data_alone_when_the_daemon_owns_all_of_it(
    tmp_path: Path,
) -> None:
    result = _as_root(ENTRYPOINT, tmp_path, "echo", "hi")
    assert result.returncode == 0, result.stderr
    calls = _find_calls(tmp_path)
    assert calls == [f"find {tmp_path / 'data'} ! -user 1000 -print -quit"]
    assert "taking ownership" not in result.stderr


def test_entrypoint_as_root_takes_ownership_of_only_what_the_daemon_does_not_own(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    result = _as_root(ENTRYPOINT, tmp_path, "echo", "hi", STUB_STRANGER=f"{data}/mtrtk.db")
    assert result.returncode == 0, result.stderr
    assert f"taking ownership of {data} for uid 1000 (found {data}/mtrtk.db)" in result.stderr
    assert _find_calls(tmp_path)[1] == f"find {data} ! -user 1000 -exec chown -h 1000:1000 {{}} +"
    assert result.stdout == f"{DROP} tini -s -- echo hi\n"


def test_entrypoint_as_root_warns_and_still_starts_when_the_chown_fails(tmp_path: Path) -> None:
    result = _as_root(ENTRYPOINT, tmp_path, "echo", "hi", STUB_STRANGER="x", STUB_CHOWN_RC="1")
    assert result.returncode == 0, result.stderr
    assert "could not take ownership" in result.stderr
    assert result.stdout == f"{DROP} tini -s -- echo hi\n"


def test_entrypoint_run_as_root_skips_both_the_chown_and_the_drop(tmp_path: Path) -> None:
    result = _as_root(ENTRYPOINT, tmp_path, "echo", "hi", STUB_STRANGER="x", MTRTK_RUN_AS_ROOT="1")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "hi\n"
    assert _find_calls(tmp_path) == []


def test_entrypoint_stops_a_long_chown_on_sigterm(tmp_path: Path) -> None:
    """As PID 1, bash ignores an untrapped SIGTERM: `docker stop` during a chown of months of
    raw logs would wait out the grace period and end in SIGKILL. The trap ends the entrypoint
    and the chown with it."""
    import signal
    import time

    env = _root_env(tmp_path, STUB_STRANGER="x", STUB_CHOWN_SLEEP="30")
    proc = subprocess.Popen(
        [str(ENTRYPOINT), "echo", "hi"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    chown_pid = None
    while chown_pid is None and time.monotonic() < deadline:
        for line in _find_calls(tmp_path):
            if line.startswith("pid "):
                chown_pid = int(line.split()[1])
        time.sleep(0.05)
    assert chown_pid is not None, "the stub chown never started"
    proc.send_signal(signal.SIGTERM)
    out, _err = proc.communicate(timeout=5)
    assert proc.returncode == 143
    assert "setpriv" not in out  # it stopped, it did not go on to start the daemon
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(chown_pid, 0)  # the chown did not outlive the entrypoint


# --- /usr/local/mtrtk/bin/mtrtk: `docker compose exec mtrtk mtrtk ...` runs as the daemon -------

WRAPPER = ROOT / "docker" / "mtrtk-as-daemon.sh"


def test_the_image_puts_the_mtrtk_wrapper_first_on_path() -> None:
    """`docker exec` skips the entrypoint and runs as root: without the wrapper, `mtrtk doctor`
    there would check the receiver and /data with root's DAC_OVERRIDE and call a port the daemon
    cannot open 'read/write ok', and `mtrtk export --out /data/...` would leave root-owned files."""
    stage = _runtime_stage()
    assert "COPY --chmod=0755 docker/mtrtk-as-daemon.sh /usr/local/mtrtk/bin/mtrtk" in stage
    assert 'PATH="/usr/local/mtrtk/bin:/app/.venv/bin:${PATH}"' in DOCKERFILE.read_text()
    assert "/app/.venv/bin/mtrtk" in WRAPPER.read_text()


def test_the_wrapper_as_root_runs_mtrtk_as_the_daemons_user_and_groups(tmp_path: Path) -> None:
    result = _as_root(WRAPPER, tmp_path, "doctor", MTRTK_REAL_BIN="/app/.venv/bin/mtrtk")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{DROP} /app/.venv/bin/mtrtk doctor\n"


def test_the_wrapper_honours_run_as_root_and_runs_mtrtk_directly(tmp_path: Path) -> None:
    result = _as_root(WRAPPER, tmp_path, "hi", MTRTK_REAL_BIN="echo", MTRTK_RUN_AS_ROOT="1")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "hi\n"


@needs_non_root
def test_the_wrapper_as_the_daemons_user_runs_mtrtk_directly() -> None:
    result = _entrypoint_like(WRAPPER, "--version")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("mtrtk, version ")


def _entrypoint_like(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    path = f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"
    real = str(Path(sys.executable).parent / "mtrtk")
    return subprocess.run(
        [str(script), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PATH": path, "MTRTK_REAL_BIN": real},
        check=False,
    )
