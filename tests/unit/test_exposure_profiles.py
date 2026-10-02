"""The two ways past Tailscale: the `public` (Caddy TLS) and `cloudflare` (tunnel) compose profiles,
and `scripts/check-exposure.sh`, which proves either one end to end.

The static checks keep the compose services, the Caddyfile and `.env.example` in step. Where Docker
is on the machine, `docker compose config` and `caddy adapt` run on scratch copies (never the
real `.env`); the script runs for real against an in-process NTRIP caster and a `/healthz` stub.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import stat
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mtrtk.base.ntrip_caster import CasterConfig, NtripCaster
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from ubxtest import rtcm_frame

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
CADDYFILE = ROOT / "docker" / "Caddyfile"
SCRIPT = ROOT / "scripts" / "check-exposure.sh"
CADDY_IMAGE = "caddy:2-alpine"
DOCKER = shutil.which("docker")


def _compose_service(name: str) -> str:
    """The text of one service block in docker-compose.yml (two-space indented, under services)."""
    match = re.search(rf"^  {re.escape(name)}:\n((?:    .*\n|\s*\n)+)", COMPOSE.read_text(), re.M)
    assert match, f"docker-compose.yml has no {name} service"
    return match.group(1)


def _without_comments(text: str) -> str:
    """YAML with its comment lines and trailing comments dropped (none of ours quote a #)."""
    lines = (line.split(" #", 1)[0] for line in text.splitlines())
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def _env_example() -> dict[str, str]:
    values = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key] = value.split("#", 1)[0].strip()
    return values


def _image_present(image: str) -> bool:
    if DOCKER is None:
        return False
    done = subprocess.run(
        [DOCKER, "image", "inspect", image], capture_output=True, check=False, timeout=30
    )
    return done.returncode == 0


# ---------------------------------------------------------------- compose: static


def test_the_public_profile_runs_caddy_on_the_host_network_with_its_own_volumes() -> None:
    svc = _compose_service("caddy")
    assert 'profiles: ["public"]' in svc
    assert "image: caddy:2-alpine" in svc
    assert "network_mode: host" in svc
    assert "restart: unless-stopped" in svc
    assert "depends_on: [mtrtk]" in svc
    for mount in (
        "./docker/Caddyfile:/etc/caddy/Caddyfile:ro",
        "caddy_data:/data",
        "caddy_config:/config",
    ):
        assert f"- {mount}" in svc
    # Both named volumes are declared, so the certificates outlive the container.
    top = re.search(r"^volumes:\n((?:  .*\n)+)", COMPOSE.read_text(), re.M)
    assert top, "docker-compose.yml declares no top-level volumes"
    assert {"caddy_data", "caddy_config"} <= set(re.findall(r"^  (\w+):", top.group(1), re.M))


def test_caddy_refuses_to_start_without_a_domain_and_proxies_the_configured_web_port() -> None:
    svc = _compose_service("caddy")
    assert "PUBLIC_DOMAIN: ${PUBLIC_DOMAIN:-}" in svc
    assert "WEB_PORT: ${WEB_PORT:-8080}" in svc
    # The guard is in the container, not in compose: see the next test.
    assert '[ -n "$$PUBLIC_DOMAIN" ]' in svc
    assert "exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile" in svc


def test_no_profile_makes_plain_compose_up_fail_on_an_unset_variable() -> None:
    """Compose interpolates every service before it looks at profiles, so a `${VAR:?}` in the
    `public` or `cloudflare` service would make `docker compose up` itself fail for everyone
    who left that variable empty - every Tailscale-only install."""
    assert ":?" not in _without_comments(COMPOSE.read_text())


def test_the_cloudflare_profile_runs_a_token_tunnel_without_the_token_on_the_command_line() -> None:
    svc = _compose_service("cloudflared")
    assert 'profiles: ["cloudflare"]' in svc
    assert "image: cloudflare/cloudflared:latest" in svc
    assert "network_mode: host" in svc
    assert "restart: unless-stopped" in svc
    assert "depends_on: [mtrtk]" in svc
    assert "command: tunnel --no-autoupdate run\n" in svc
    # cloudflared reads TUNNEL_TOKEN itself; on the command line it would show in `ps` on the host.
    assert "--token" not in _without_comments(svc)
    assert "TUNNEL_TOKEN: ${TUNNEL_TOKEN:-}" in svc
    # Its metrics server binds every interface by default inside a container, which on the host
    # network means the host's own - keep it on loopback, where the healthcheck asks it.
    assert "TUNNEL_METRICS: 127.0.0.1:20241" in svc
    assert 'test: ["CMD", "cloudflared", "tunnel", "ready"]' in svc


def test_env_example_documents_both_profiles_with_empty_defaults() -> None:
    env = _env_example()
    for key in ("PUBLIC_DOMAIN", "ACME_EMAIL", "TUNNEL_TOKEN"):
        assert env.get(key) == "", key
    text = (ROOT / ".env.example").read_text()
    assert "docker compose --profile public up -d" in text
    assert "docker compose --profile cloudflare up -d" in text
    assert "http://127.0.0.1:8080" in text and "http://127.0.0.1:2101" in text


# ---------------------------------------------------------------- Caddyfile: static


def test_caddyfile_proxies_the_domain_to_the_local_web_port() -> None:
    text = CADDYFILE.read_text()
    assert re.search(r"^\{\$PUBLIC_DOMAIN\} \{$", text, re.M)
    assert re.search(r"^\treverse_proxy 127\.0\.0\.1:\{\$WEB_PORT:8080\}$", text, re.M)
    assert re.search(r"^\tencode zstd gzip$", text, re.M)
    assert re.search(r'^\theader Strict-Transport-Security "max-age=31536000"$', text, re.M)
    # With host networking the admin API would be the host's localhost:2019, open to every
    # local process.
    assert re.search(r"^\tadmin off$", text, re.M)
    # `email {$ACME_EMAIL}` is a syntax error when ACME_EMAIL is empty; compose builds the line.
    assert re.search(r"^\t\{\$ACME_EMAIL_OPTION\}$", text, re.M)
    assert "ACME_EMAIL_OPTION: ${ACME_EMAIL:+email ${ACME_EMAIL}}" in _compose_service("caddy")


@pytest.mark.skipif(not _image_present(CADDY_IMAGE), reason=f"needs docker and {CADDY_IMAGE}")
@pytest.mark.parametrize("email", ["", "ops@example.com"])
def test_caddy_adapts_the_caddyfile_with_or_without_an_acme_email(email: str) -> None:
    assert DOCKER is not None
    # Docker creates a missing bind-mount source as a root-owned directory: never let it.
    assert CADDYFILE.is_file()
    option = f"email {email}" if email else ""
    args = [DOCKER, "run", "--rm", "--network", "none", "-e", "PUBLIC_DOMAIN=rtk.example.com"]
    args += ["-e", f"ACME_EMAIL_OPTION={option}", "-e", "WEB_PORT=8081"]
    args += ["-v", f"{CADDYFILE}:/etc/caddy/Caddyfile:ro", CADDY_IMAGE]
    args += ["caddy", "adapt", "--config", "/etc/caddy/Caddyfile"]
    done = subprocess.run(args, capture_output=True, text=True, check=False, timeout=120)
    assert done.returncode == 0, done.stderr
    assert '"dial":"127.0.0.1:8081"' in done.stdout
    assert '"host":["rtk.example.com"]' in done.stdout
    assert '"admin":{"disabled":true}' in done.stdout
    assert '"Strict-Transport-Security":["max-age=31536000"]' in done.stdout
    assert ('"email":"ops@example.com"' in done.stdout) is bool(email)


# ---------------------------------------------------------------- compose: docker compose config


def _scratch_project(tmp_path: Path, env: str) -> Path:
    """docker-compose.yml and the files it names, in a directory with a throwaway .env."""
    shutil.copy(COMPOSE, tmp_path / "docker-compose.yml")
    (tmp_path / "docker").mkdir()
    shutil.copy(CADDYFILE, tmp_path / "docker" / "Caddyfile")
    (tmp_path / ".env").write_text(env)
    return tmp_path


def _compose_config(project: Path, *profile: str) -> subprocess.CompletedProcess[str]:
    assert DOCKER is not None
    args = [DOCKER, "compose", "--project-directory", str(project)]
    for name in profile:
        args += ["--profile", name]
    # Only PATH and HOME: a PUBLIC_DOMAIN or WEB_PORT in the test's own environment (conftest sets
    # WEB_PORT=0) would win over the scratch .env.
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp")}
    return subprocess.run(
        [*args, "config"], capture_output=True, text=True, check=False, timeout=60, env=env
    )


needs_compose = pytest.mark.skipif(
    DOCKER is None
    or subprocess.run(
        [DOCKER, "compose", "version"], capture_output=True, check=False, timeout=30
    ).returncode
    != 0,
    reason="needs docker compose",
)


@needs_compose
def test_plain_compose_config_works_with_the_exposure_variables_left_empty(tmp_path: Path) -> None:
    project = _scratch_project(tmp_path, "PUBLIC_DOMAIN=\nACME_EMAIL=\nTUNNEL_TOKEN=\n")
    for profiles in ((), ("public",), ("cloudflare",)):
        done = _compose_config(project, *profiles)
        assert done.returncode == 0, (profiles, done.stderr)


@needs_compose
def test_compose_config_resolves_the_public_and_cloudflare_settings(tmp_path: Path) -> None:
    project = _scratch_project(
        tmp_path,
        "PUBLIC_DOMAIN=rtk.example.com\nACME_EMAIL=ops@example.com\nWEB_PORT=8443\n"
        "TUNNEL_TOKEN=eyJhIjoiYiJ9\n",
    )
    public = _compose_config(project, "public")
    assert public.returncode == 0, public.stderr
    assert "PUBLIC_DOMAIN: rtk.example.com" in public.stdout
    assert "ACME_EMAIL_OPTION: email ops@example.com" in public.stdout
    assert 'WEB_PORT: "8443"' in public.stdout
    tunnel = _compose_config(project, "cloudflare")
    assert tunnel.returncode == 0, tunnel.stderr
    assert "TUNNEL_TOKEN: eyJhIjoiYiJ9" in tunnel.stdout
    assert "mtrtk-caddy" not in tunnel.stdout and "mtrtk-cloudflared" in tunnel.stdout


# ---------------------------------------------------------------- check-exposure.sh


def test_the_check_script_is_executable_bash_that_parses() -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    assert SCRIPT.read_text().startswith("#!/usr/bin/env bash\n")
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True, timeout=30)
    shellcheck = shutil.which("shellcheck")
    if shellcheck:
        done = subprocess.run(
            [shellcheck, str(SCRIPT)], capture_output=True, text=True, check=False
        )
        assert done.returncode == 0, done.stdout


needs_curl = pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl")

HEALTHZ_OK = b'{"status":"ok","role":"base","connected":true,"passive":false}'


async def _http_stub(status: str, body: bytes) -> asyncio.Server:
    """A one-route HTTP/1.1 server standing in for the web UI's `/healthz`."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            await reader.readuntil(b"\r\n\r\n")
            head = f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
            head += f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            writer.write(head.encode() + body)
            await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


class Exposure:
    """A caster and a healthz stub on ephemeral loopback ports, and a feeder of RTCM frames."""

    def __init__(self, caster: NtripCaster, web: asyncio.Server, bus: Bus) -> None:
        self.caster, self.web, self.bus = caster, web, bus
        self.frames: list[bytes] = []

    @property
    def web_url(self) -> str:
        return f"http://127.0.0.1:{self.web.sockets[0].getsockname()[1]}"

    @property
    def ntrip_url(self) -> str:
        return f"http://127.0.0.1:{self.caster.port}/MTRK"

    async def feed(self) -> None:
        while True:
            for raw in self.frames:
                for frame in Framer().feed(raw):
                    self.bus.publish("raw.rtcm", frame)
            await asyncio.sleep(0.05)


@pytest.fixture
async def exposure(request: pytest.FixtureRequest) -> AsyncIterator[Exposure]:
    status, body = getattr(request, "param", ("200 OK", HEALTHZ_OK))
    bus = Bus()
    config = CasterConfig(
        mountpoint="MTRK", username="rover", password="secret", station_id="MTRK", country="BGD"
    )
    caster = NtripCaster(bus, config, host="127.0.0.1", port=0)
    await caster.start()
    web = await _http_stub(status, body)
    exp = Exposure(caster, web, bus)
    feeder = asyncio.create_task(exp.feed())
    try:
        yield exp
    finally:
        feeder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feeder
        web.close()
        await web.wait_closed()
        await caster.stop()


async def _run(*args: str, timeout_s: str = "3") -> tuple[int, str]:
    env = {**os.environ, "CHECK_TIMEOUT_S": timeout_s}
    for key in ("CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET"):
        env.pop(key, None)
    proc = await asyncio.create_subprocess_exec(
        str(SCRIPT),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), 30)
    assert proc.returncode is not None
    return proc.returncode, out.decode()


# A 1005 is 25 bytes; an MSM7 epoch for one constellation runs to several hundred, so its second
# header byte is 0x01-0x03, not 0x00 - a check that only looked for `d3 00` would miss it.
RTCM_1005 = bytes.fromhex("d300133ed7fd0382dfdc1c403db34fe8fe0cef5e6b30bd2e23")
RTCM_1077_LONG = rtcm_frame(1077, b"\x00" * 400)


@needs_curl
async def test_rtcm_arriving_over_ntrip_v2_passes(exposure: Exposure) -> None:
    exposure.frames = [RTCM_1005, RTCM_1077_LONG]
    code, out = await _run(exposure.web_url, exposure.ntrip_url, "rover", "secret")
    assert code == 0, out
    assert '"status":"ok"' in out
    assert re.search(r"OK: \d+ RTCM3 frames", out), out
    assert "first byte after" in out


@needs_curl
async def test_long_msm_frames_alone_are_recognised(exposure: Exposure) -> None:
    exposure.frames = [RTCM_1077_LONG]
    assert RTCM_1077_LONG[1] != 0  # the length's high bits are set
    code, out = await _run(exposure.web_url, exposure.ntrip_url, "rover", "secret")
    assert code == 0, out


@needs_curl
async def test_a_wrong_password_fails_and_names_the_status(exposure: Exposure) -> None:
    exposure.frames = [RTCM_1005]
    code, out = await _run(exposure.web_url, exposure.ntrip_url, "rover", "wrong")
    assert code == 1, out
    assert "FAIL" in out and "401" in out


@needs_curl
async def test_a_url_without_a_mountpoint_fails_with_a_hint(exposure: Exposure) -> None:
    exposure.frames = [RTCM_1005]
    url = exposure.ntrip_url.removesuffix("/MTRK")
    code, out = await _run(exposure.web_url, url, "rover", "secret")
    assert code == 1, out
    assert "sourcetable" in out


@needs_curl
async def test_a_silent_stream_fails_after_the_timeout(exposure: Exposure) -> None:
    exposure.frames = []
    code, out = await _run(exposure.web_url, exposure.ntrip_url, "rover", "secret", timeout_s="2")
    assert code == 1, out
    assert "FAIL" in out and "no RTCM3" in out


@needs_curl
@pytest.mark.parametrize(
    "exposure",
    [("200 OK", b"<html>Sign in with Cloudflare Access</html>"), ("502 Bad Gateway", b"{}")],
    indirect=True,
)
async def test_a_healthz_that_is_not_the_daemon_fails_before_ntrip(exposure: Exposure) -> None:
    exposure.frames = [RTCM_1005]
    code, out = await _run(exposure.web_url, exposure.ntrip_url, "rover", "secret")
    assert code == 1, out
    assert "FAIL" in out and "NTRIP v2" not in out


@needs_curl
async def test_an_unreachable_web_url_fails(exposure: Exposure) -> None:
    web_url = exposure.web_url
    exposure.web.close()
    await exposure.web.wait_closed()
    code, out = await _run(web_url, exposure.ntrip_url, "rover", "secret")
    assert code == 1, out
    assert "FAIL" in out


async def test_missing_arguments_print_the_usage() -> None:
    code, out = await _run()
    assert code == 2
    assert "Usage: scripts/check-exposure.sh" in out
