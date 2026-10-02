"""The two ways past Tailscale: the `public` (Caddy TLS) and `cloudflare` (tunnel) compose profiles,
and `scripts/check-exposure.sh`, which proves either one end to end.

The static checks keep the compose services, the Caddyfile and `.env.example` in step. Where Docker
is on the machine, `docker compose config` and `caddy adapt` run on scratch copies (never the
real `.env`); the script runs for real against an in-process NTRIP caster and a `/healthz` stub.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
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


# The Docker probes run lazily, from the tests that need them, and once per session: at import
# they would run on every collection, `-k` runs of unrelated tests included.
@functools.cache
def _image_present(image: str) -> bool:
    if DOCKER is None:
        return False
    try:
        done = subprocess.run(
            [DOCKER, "image", "inspect", image], capture_output=True, check=False, timeout=10
        )
    except subprocess.TimeoutExpired:
        return False
    return done.returncode == 0


@functools.cache
def _compose_present() -> bool:
    if DOCKER is None:
        return False
    try:
        done = subprocess.run(
            [DOCKER, "compose", "version"], capture_output=True, check=False, timeout=10
        )
    except subprocess.TimeoutExpired:
        return False
    return done.returncode == 0


def _need_caddy_image() -> None:
    if not _image_present(CADDY_IMAGE):
        pytest.skip(f"needs docker and {CADDY_IMAGE}")


def _need_compose() -> None:
    if not _compose_present():
        pytest.skip("needs docker compose")


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
    assert "TUNNEL_METRICS: 127.0.0.1:${TUNNEL_METRICS_PORT:-20241}" in svc
    assert 'test: ["CMD", "cloudflared", "tunnel", "ready"]' in svc


def test_env_example_documents_both_profiles_with_empty_defaults() -> None:
    env = _env_example()
    for key in ("PUBLIC_DOMAIN", "ACME_EMAIL", "TUNNEL_TOKEN"):
        assert env.get(key) == "", key
    text = (ROOT / ".env.example").read_text()
    assert "docker compose --profile public up -d" in text
    assert "docker compose --profile cloudflare up -d" in text
    assert "http://127.0.0.1:8080" in text and "http://127.0.0.1:2101" in text
    # The ingress ports follow the daemon's own settings.
    assert "WEB_PORT" in text and "NTRIP_PORT" in text
    assert "TUNNEL_METRICS_PORT" in text
    # TUNNEL_TOKEN is also read by `mtrtk doctor` (P9T1): the section must not deny it.
    assert "cloudflared services, not by Settings" not in text


def test_access_alone_is_documented_with_what_settings_requires_for_it() -> None:
    """Settings refuses a non-tailscale WEB_BIND with no WEB_PASSWORD unless WEB_ALLOW_INSECURE=1,
    which with WEB_BIND=lan would leave the UI open on the LAN."""
    access = "a Cloudflare Access policy plus WEB_BIND=127.0.0.1, WEB_ALLOW_INSECURE=1"
    for path in (ROOT / ".env.example", COMPOSE):
        flat = " ".join(line.lstrip("# ").strip() for line in path.read_text().splitlines())
        assert access in flat, path
        assert "(never with lan)" in flat, path
        # Without a password the daemon answers only names it was given (the rebinding guard).
        assert "WEB_ALLOWED_HOSTS=rtk.<domain>" in flat, path
        assert "(or a Cloudflare Access policy" not in flat, path


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


@pytest.mark.parametrize("email", ["", "ops@example.com"])
def test_caddy_adapts_the_caddyfile_with_or_without_an_acme_email(email: str) -> None:
    _need_caddy_image()
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


def test_plain_compose_config_works_with_the_exposure_variables_left_empty(tmp_path: Path) -> None:
    _need_compose()
    project = _scratch_project(tmp_path, "PUBLIC_DOMAIN=\nACME_EMAIL=\nTUNNEL_TOKEN=\n")
    for profiles in ((), ("public",), ("cloudflare",)):
        done = _compose_config(project, *profiles)
        assert done.returncode == 0, (profiles, done.stderr)


def test_compose_config_resolves_the_public_and_cloudflare_settings(tmp_path: Path) -> None:
    _need_compose()
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
    assert "TUNNEL_METRICS: 127.0.0.1:20241" in tunnel.stdout


def test_the_tunnel_metrics_port_can_move_off_a_host_cloudflared(tmp_path: Path) -> None:
    """A cloudflared already on the host (`cloudflared service install`) holds 127.0.0.1:20241."""
    _need_compose()
    project = _scratch_project(tmp_path, "TUNNEL_TOKEN=eyJhIjoiYiJ9\nTUNNEL_METRICS_PORT=20299\n")
    tunnel = _compose_config(project, "cloudflare")
    assert tunnel.returncode == 0, tunnel.stderr
    assert "TUNNEL_METRICS: 127.0.0.1:20299" in tunnel.stdout


def test_caddy_s_entrypoint_exits_without_a_domain(tmp_path: Path) -> None:
    """The guard that stands in for `${PUBLIC_DOMAIN:?}`, run as compose resolves it."""
    _need_compose()
    _need_caddy_image()
    assert DOCKER is not None
    project = _scratch_project(tmp_path, "PUBLIC_DOMAIN=\n")
    args = [DOCKER, "compose", "--project-directory", str(project), "--profile", "public"]
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp")}
    done = subprocess.run(
        [*args, "config", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    caddy = json.loads(done.stdout)["services"]["caddy"]
    assert caddy["entrypoint"] == ["/bin/sh", "-c"]
    # The JSON keeps compose's `$$` escape, which the container's shell would read as its PID.
    script = [part.replace("$$", "$") for part in caddy["command"]]
    # Named, and removed whatever happens: if the guard ever let caddy start, it would not exit.
    name = f"mtrtk-test-caddy-guard-{os.getpid()}"
    run = [DOCKER, "run", "--rm", "--name", name, "--network", "none", "-e", "PUBLIC_DOMAIN="]
    run += ["--entrypoint", "/bin/sh", CADDY_IMAGE, "-c", *script]
    try:
        guard = subprocess.run(run, capture_output=True, text=True, check=False, timeout=60)
    finally:
        subprocess.run([DOCKER, "rm", "-f", name], capture_output=True, check=False, timeout=60)
    assert guard.returncode == 1, (guard.stdout, guard.stderr)
    assert "set PUBLIC_DOMAIN in .env" in guard.stderr


# ---------------------------------------------------------------- check-exposure.sh


def test_the_check_script_is_executable_bash_that_parses() -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    assert SCRIPT.read_text().startswith("#!/usr/bin/env bash\n")
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True, timeout=30)
    shellcheck = shutil.which("shellcheck")
    if shellcheck:
        done = subprocess.run(
            [shellcheck, str(SCRIPT)], capture_output=True, text=True, check=False, timeout=30
        )
        assert done.returncode == 0, done.stdout


needs_curl = pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl")

HEALTHZ_OK = b'{"status":"ok","role":"base","connected":true,"passive":false}'


async def _http_stub(
    status: str,
    body: bytes,
    *,
    headers: tuple[str, ...] = ("Content-Type: application/json",),
    requests: list[bytes] | None = None,
) -> asyncio.Server:
    """A one-route HTTP/1.1 server standing in for the web UI's `/healthz` (or, with a raw body,
    for an NTRIP endpoint). Each request's head is appended to *requests*."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            request = await reader.readuntil(b"\r\n\r\n")
            if requests is not None:
                requests.append(request)
            head = f"HTTP/1.1 {status}\r\n" + "".join(f"{h}\r\n" for h in headers)
            head += f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            writer.write(head.encode() + body)
            await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


def _url(server: asyncio.Server, path: str = "") -> str:
    return f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}{path}"


@contextlib.asynccontextmanager
async def _serving(server: asyncio.Server) -> AsyncIterator[asyncio.Server]:
    try:
        yield server
    finally:
        server.close()
        await server.wait_closed()


class Exposure:
    """A caster and a healthz stub on ephemeral loopback ports, and a feeder of RTCM frames."""

    def __init__(
        self, caster: NtripCaster, web: asyncio.Server, bus: Bus, web_requests: list[bytes]
    ) -> None:
        self.caster, self.web, self.bus = caster, web, bus
        self.web_requests = web_requests
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
    status, body, *extra = getattr(request, "param", ("200 OK", HEALTHZ_OK))
    headers = ("Content-Type: application/json", *extra)
    bus = Bus()
    config = CasterConfig(
        mountpoint="MTRK", username="rover", password="secret", station_id="MTRK", country="BGD"
    )
    caster = NtripCaster(bus, config, host="127.0.0.1", port=0)
    await caster.start()
    web_requests: list[bytes] = []
    web = await _http_stub(status, body, headers=headers, requests=web_requests)
    exp = Exposure(caster, web, bus, web_requests)
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


async def _run(
    *args: str, timeout_s: str = "3", env_extra: dict[str, str] | None = None
) -> tuple[int, str]:
    env = {**os.environ, "CHECK_TIMEOUT_S": timeout_s}
    for key in ("CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET", "CHECK_BYTES"):
        env.pop(key, None)
    env.update(env_extra or {})
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
    assert "401" in out and "check the NTRIP user and password" in out


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


ACCESS_LOGIN = "https://team.cloudflareaccess.com/cdn-cgi/access/login/rtk.example.com"


@needs_curl
@pytest.mark.parametrize(
    ("exposure", "expected"),
    [
        (("200 OK", b"<html>Sign in with Cloudflare Access</html>"), ["not as mtrtk"]),
        (("502 Bad Gateway", b"{}"), ["answered HTTP 502", "WEB_BIND"]),
        # Access without a service token redirects to the team's login page.
        (
            ("302 Found", b"", f"Location: {ACCESS_LOGIN}"),
            ["answered HTTP 302", ACCESS_LOGIN, "CF_ACCESS_CLIENT_ID"],
        ),
        (("403 Forbidden", b"denied"), ["answered HTTP 403", "CF_ACCESS_CLIENT_ID"]),
    ],
    indirect=["exposure"],
)
async def test_a_healthz_that_is_not_the_daemon_fails_before_ntrip(
    exposure: Exposure, expected: list[str]
) -> None:
    exposure.frames = [RTCM_1005]
    code, out = await _run(exposure.web_url, exposure.ntrip_url, "rover", "secret")
    assert code == 1, out
    assert "FAIL" in out and "NTRIP v2" not in out
    for text in expected:
        assert text in out, (text, out)


@needs_curl
async def test_an_access_service_token_is_sent_with_the_healthz_request(
    exposure: Exposure,
) -> None:
    exposure.frames = [RTCM_1005, RTCM_1077_LONG]
    tokens = {"CF_ACCESS_CLIENT_ID": "id.access", "CF_ACCESS_CLIENT_SECRET": "s3cret"}
    code, out = await _run(
        exposure.web_url, exposure.ntrip_url, "rover", "secret", env_extra=tokens
    )
    assert code == 0, out
    head = exposure.web_requests[0].decode().lower()
    assert "cf-access-client-id: id.access\r\n" in head
    assert "cf-access-client-secret: s3cret\r\n" in head


@needs_curl
async def test_an_unreachable_web_url_fails(exposure: Exposure) -> None:
    web_url = exposure.web_url
    exposure.web.close()
    await exposure.web.wait_closed()
    code, out = await _run(web_url, exposure.ntrip_url, "rover", "secret")
    assert code == 1, out
    assert "FAIL" in out and "is unreachable" in out


async def test_missing_arguments_print_the_usage() -> None:
    code, out = await _run()
    assert code == 2
    assert "Usage: scripts/check-exposure.sh" in out


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("CHECK_TIMEOUT_S", "2.5"),
        ("CHECK_TIMEOUT_S", "0"),
        ("CHECK_BYTES", "abc"),
        ("CHECK_BYTES", "-5"),
    ],
)
async def test_a_bad_timeout_or_byte_cap_is_a_usage_error(key: str, value: str) -> None:
    """Not bash's arithmetic error after curl has started (exit 1), nor a silently lost cap."""
    code, out = await _run("http://127.0.0.1:9", "http://127.0.0.1:9/MTRK", env_extra={key: value})
    assert code == 2, out
    assert f"{key} must be a positive whole number" in out
    assert "Usage: scripts/check-exposure.sh" in out


async def test_a_missing_curl_is_exit_2(tmp_path: Path) -> None:
    for tool in ("bash", "od", "awk"):
        found = shutil.which(tool)
        assert found, tool
        (tmp_path / tool).symlink_to(found)
    code, out = await _run(
        "http://127.0.0.1:9", "http://127.0.0.1:9/MTRK", env_extra={"PATH": str(tmp_path)}
    )
    assert code == 2, out
    assert "curl is not installed" in out


# ---------------------------------------------------------------- the RTCM3 frame counter
# These serve a fixed NTRIP body from a stub, so the count is exact.

# A 1005 is followed by a frame whose payload is full of `d3 00 00 ...`, each of which looks like
# an empty frame followed by another preamble: only skipping a counted frame's bytes avoids them.
RTCM_INNER_PREAMBLES = rtcm_frame(1005, b"\x00\x00" + b"\xd3\x00\x00\x01\x02\x03" * 8)
CORRUPT_1005 = RTCM_1005[:-1] + bytes((RTCM_1005[-1] ^ 0xFF,))
STRAY_D3 = (b"\xd3\x00\x05" + b"junkjunkjunk") * 50


@needs_curl
@pytest.mark.parametrize(
    ("body", "code_", "expected"),
    [
        # Stray 0xD3 bytes whose length points at no preamble are not frames.
        (STRAY_D3, 1, f"FAIL: {len(STRAY_D3)} bytes in"),
        # The last frame ends exactly at the end of what arrived: counted on its CRC.
        (RTCM_1005 + RTCM_INNER_PREAMBLES + RTCM_1077_LONG, 0, "OK: 3 RTCM3 frames in"),
        (RTCM_1005, 0, "OK: 1 RTCM3 frames in"),
        # A cut-off frame at the end is not counted; the one before it is.
        (RTCM_1005 + RTCM_1005[:10], 0, "OK: 1 RTCM3 frames in"),
        # A complete-looking last frame with a bad CRC is not.
        (CORRUPT_1005, 1, "but no RTCM3 frames"),
        (RTCM_1005 + CORRUPT_1005, 0, "OK: 1 RTCM3 frames in"),
    ],
    ids=["stray-d3", "three-frames", "one-frame", "cut-off-tail", "bad-crc", "bad-crc-tail"],
)
async def test_the_frame_counter_counts_only_whole_rtcm3_frames(
    exposure: Exposure, body: bytes, code_: int, expected: str
) -> None:
    stub = await _http_stub("200 OK", body, headers=("Content-Type: gnss/data",))
    async with _serving(stub):
        code, out = await _run(exposure.web_url, _url(stub, "/MTRK"), "rover", "secret")
    assert code == code_, out
    assert expected in out, out
    if code_:
        assert "but no RTCM3 frames" in out


@needs_curl
async def test_an_ntrip_error_from_the_tunnel_names_the_hostname(exposure: Exposure) -> None:
    stub = await _http_stub(
        "502 Bad Gateway", b"bad gateway", headers=("Content-Type: text/plain",)
    )
    async with _serving(stub):
        code, out = await _run(exposure.web_url, _url(stub, "/MTRK"), "rover", "secret")
    assert code == 1, out
    assert "502" in out and "check the tunnel's NTRIP hostname" in out


@needs_curl
async def test_a_tunnel_that_withholds_the_headers_gets_the_buffering_hint(
    exposure: Exposure,
) -> None:
    async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            await reader.read()  # never answers: the edge or cloudflared holds the response
        writer.close()

    stub = await asyncio.start_server(hold, "127.0.0.1", 0)
    async with _serving(stub):
        code, out = await _run(
            exposure.web_url, _url(stub, "/MTRK"), "rover", "secret", timeout_s="1"
        )
    assert code == 1, out
    assert "no HTTP answer" in out and "use the public-IP path" in out


@needs_curl
async def test_a_refused_ntrip_port_gets_no_buffering_hint(exposure: Exposure) -> None:
    closed = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    url = _url(closed, "/MTRK")
    closed.close()
    await closed.wait_closed()
    code, out = await _run(exposure.web_url, url, "rover", "secret")
    assert code == 1, out
    assert "no HTTP answer" in out and "public-IP path" not in out
