"""How the mtrtk image runs: non-root behind docker/entrypoint.sh, with a healthcheck and the
compose service's capability and logging limits.

No Docker here: building the image and running it (`docker inspect` health, `id` inside the
container) is the real check. These tests keep the Dockerfile, the entrypoint, the compose
service and `.env.example` in step, and run the entrypoint's non-root path for real.
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
    assert 'chown -R "$APP_UID:$APP_GID" /data' in script
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


def test_the_entrypoint_is_executable_in_git() -> None:
    assert os.access(ENTRYPOINT, os.X_OK)
    try:
        out = subprocess.run(
            ["git", "ls-files", "-s", "docker/entrypoint.sh"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    if not out:
        pytest.skip("docker/entrypoint.sh is not committed yet")
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
