from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
import serial
from click.testing import CliRunner
from pyubx2 import POLL, UBXMessage

from doctorhost import REAL, patch_host_probes
from mtrtk import doctor
from mtrtk.cli import main
from mtrtk.config import Settings
from mtrtk.core import exposure
from ubxtest import mon_ver_bytes, nmea_frame


@pytest.fixture(autouse=True)
def hermetic_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here shells out, asks psutil or reads the host's udev rules unless it says so."""
    patch_host_probes(monkeypatch)


@pytest.fixture
def quiet_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    """A host with Tailscale up, docker present and no receiver plugged in."""
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    monkeypatch.setattr(doctor, "find_ublox_port", lambda: None)
    monkeypatch.setattr(doctor, "tailscale_ipv4", lambda: "100.100.50.10")
    monkeypatch.setattr(
        doctor.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )
    # The suite-wide fixture binds loopback on port 0; doctor is checked on the real defaults.
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        ntrip_bind="tailscale",
        web_bind="tailscale",
        ntrip_port=2101,
        web_port=8080,
    )


def by_name(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {c.name: c for c in checks}


def with_(settings: Settings, **values: object) -> Settings:
    data = settings.model_dump()
    data.update(values)
    return Settings(_env_file=None, **data)


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
    monkeypatch.setattr(doctor, "_udev_rule_present", lambda: True)
    assert by_name(doctor.run_checks(quiet_host))["modemmanager"].ok is True


def test_udev_rule_is_found_in_any_rules_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(doctor, "UDEV_RULES_DIR", tmp_path)
    present = REAL["_udev_rule_present"]
    assert present() is False
    (tmp_path / "50-other.rules").write_text('ATTRS{idVendor}=="0403", MODE="0660"\n')
    assert present() is False
    (tmp_path / "99-mtrtk-ublox.rules").write_text(doctor.UDEV_RULE + "\n")
    assert present() is True


def test_ports_fail_when_foreign_process_holds_them(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        doctor,
        "_port_owner",
        lambda port, hosts=None: (
            SimpleNamespace(pid=4242, name="str2str") if port == 2101 else None
        ),
    )
    c = by_name(doctor.run_checks(quiet_host))
    assert c["ports"].ok is False and "str2str" in c["ports"].detail
    monkeypatch.setattr(
        doctor, "_port_owner", lambda port, hosts=None: SimpleNamespace(pid=1, name="mtrtk")
    )
    c = by_name(doctor.run_checks(quiet_host))
    assert c["ports"].ok is True and "mtrtk" in c["ports"].detail


def test_ports_owner_unknown_is_a_warning(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another user's (or another namespace's) listener has no pid we can see."""
    monkeypatch.setattr(
        doctor, "_port_owner", lambda port, hosts=None: SimpleNamespace(pid=None, name=None)
    )
    assert by_name(doctor.run_checks(quiet_host))["ports"].ok is None


def test_ports_skips_ephemeral_and_follows_the_role(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[int] = []
    monkeypatch.setattr(doctor, "_port_owner", lambda port, hosts=None: asked.append(port))
    doctor.run_checks(with_(quiet_host, ntrip_port=0))
    assert asked == [8080]
    asked.clear()
    rover = with_(quiet_host, role="rover", ntrip_password="", nmea_tcp_port=10110)
    doctor.run_checks(rover)
    assert asked == [8080, 10110]  # a rover runs no caster


def test_port_owner_finds_a_real_listener() -> None:
    port_owner = REAL["_port_owner"]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        owner = port_owner(port)
        # A bind to another specific address does not clash with it; a wildcard one does.
        elsewhere = port_owner(port, frozenset({"100.100.50.10"}))
        same = port_owner(port, frozenset({"127.0.0.1"}))
        wildcard = port_owner(port, frozenset({"0.0.0.0"}))
    assert owner is not None and owner.pid == os.getpid()
    assert owner.name and owner.cmdline
    assert elsewhere is None
    assert same is not None and wildcard is not None


@pytest.mark.parametrize(
    ("listener", "hosts", "clash"),
    [
        ("127.0.0.1", {"100.100.50.10"}, False),
        ("127.0.0.1", {"127.0.0.1"}, True),
        ("0.0.0.0", {"100.100.50.10"}, True),
        ("::", {"100.100.50.10"}, True),
        ("127.0.0.1", {"0.0.0.0"}, True),
        ("::1", {"127.0.0.1"}, False),
    ],
)
def test_addresses_clash(listener: str, hosts: set[str], clash: bool) -> None:
    assert doctor._addresses_clash(listener, frozenset(hosts)) is clash


def test_ports_are_checked_against_the_configured_bind(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listener on 127.0.0.1:8080 does not stop a daemon binding 100.x:8080."""
    asked: dict[int, object] = {}

    def owner(port: int, hosts: frozenset[str] | None = None) -> None:
        asked[port] = hosts

    monkeypatch.setattr(doctor, "_port_owner", owner)
    doctor.run_checks(quiet_host)
    assert asked == {2101: frozenset({"100.100.50.10"}), 8080: frozenset({"100.100.50.10"})}
    doctor.run_checks(with_(quiet_host, web_bind="lan", web_password="pw"))
    assert asked[8080] == frozenset({"0.0.0.0"})
    # No tailnet address yet: any listener on the port may be in the way.
    monkeypatch.setattr(doctor, "tailscale_ipv4", lambda: None)
    doctor.run_checks(quiet_host)
    assert asked[2101] is None


@pytest.mark.parametrize(
    ("owner", "ours"),
    [
        (doctor.PortOwner(9, "mtrtk"), True),
        (doctor.PortOwner(9, "python3", ("/usr/bin/python3", "-m", "mtrtk", "run")), True),
        (doctor.PortOwner(9, "python3", ("/u/.venv/bin/python3", "/u/bin/mtrtk", "base")), True),
        (doctor.PortOwner(9, "node", ("node", "/u/mtrtk/web/node_modules/.bin/vite")), False),
        (doctor.PortOwner(9, "python3", ("/u/mtrtk/.venv/bin/python", "-m", "http.server")), False),
        (doctor.PortOwner(9, "str2str", ("str2str", "-out", "file:///u/mtrtk/raw.ubx")), False),
    ],
)
def test_only_the_mtrtk_program_counts_as_ours(
    quiet_host: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    owner: doctor.PortOwner,
    ours: bool,
) -> None:
    """`mtrtk` in a path is not the daemon: a dev server started from the repo is a conflict."""
    monkeypatch.setattr(doctor, "_port_owner", lambda port, hosts=None: owner)
    monkeypatch.setattr(doctor, "_probe_firmware", lambda p, baud: "HPG 1.51")
    port = tmp_path / "ttyACM0"
    port.write_bytes(b"")
    c = by_name(doctor.run_checks(with_(quiet_host, mtrtk_source=str(port)), probe_receiver=True))
    assert c["ports"].ok is ours
    probed = c["firmware"].ok is True
    assert probed is not ours  # the daemon's own port is never probed beside it


# A WEB_PASSWORD long enough for a login the internet can reach.
LONG = "x" * doctor.MIN_PUBLIC_PASSWORD


def test_exposure_rules(tmp_path: Path, quiet_host: Settings) -> None:
    insecure = Settings(
        _env_file=None,
        data_dir=tmp_path,
        ntrip_password="",
        ntrip_bind="all",
        web_bind="all",
        web_allow_insecure=True,
    )
    c = by_name(doctor.run_checks(insecure))
    assert c["exposure"].ok is False and "WEB_PASSWORD" in c["exposure"].detail
    assert "anonymous" in c["exposure"].detail
    tunnel = Settings(
        _env_file=None,
        data_dir=tmp_path,
        ntrip_password="pw",
        web_password=LONG,
        web_bind="lan",
        tunnel_token="abc",
    )
    c = by_name(doctor.run_checks(tunnel))
    assert c["exposure"].ok is None and "NTRIP v1" in c["exposure"].detail


def test_exposure_of_each_bind(quiet_host: Settings) -> None:
    def exposure_of(**values: object) -> doctor.Check:
        return by_name(doctor.run_checks(with_(quiet_host, **values)))["exposure"]

    # Loopback and a tailnet address are as private as the tailscale mode.
    assert exposure_of(web_bind="127.0.0.1", web_allow_insecure=True).ok is True
    assert exposure_of(web_bind="100.100.50.10", web_allow_insecure=True).ok is True
    # The operator accepted an open UI on a trusted LAN: a warning, not a failure.
    lan = exposure_of(web_bind="lan", web_allow_insecure=True)
    assert lan.ok is None and "WEB_ALLOW_INSECURE" in lan.detail
    assert exposure_of(web_bind="192.168.1.20", web_allow_insecure=True).ok is None
    assert exposure_of(web_bind="1.2.3.4", web_allow_insecure=True).ok is False
    assert exposure_of(web_bind="0.0.0.0", web_allow_insecure=True).ok is False
    assert exposure_of(web_bind="fd7a:115c:a1e0::1", web_allow_insecure=True).ok is True
    # A tunnel publishes whatever listens on localhost. Without WEB_PASSWORD only Cloudflare
    # Access (which doctor cannot see) protects the UI: the operator accepted that.
    tunnel = exposure_of(web_bind="127.0.0.1", web_allow_insecure=True, tunnel_token="abc")
    assert tunnel.ok is None and "Cloudflare Access" in tunnel.detail
    assert tunnel.fix and "WEB_PASSWORD" in tunnel.fix and "WEB_ALLOWED_HOSTS" in tunnel.fix
    assert exposure_of(web_password="pw", web_bind="all").ok is True
    # An anonymous caster reachable through the tunnel.
    anon = exposure_of(ntrip_password="", ntrip_bind="lan", web_password=LONG, tunnel_token="abc")
    assert anon.ok is None and "published through the tunnel" in anon.detail
    assert anon.fix and "set NTRIP_PASSWORD" in anon.fix
    all_anon = exposure_of(ntrip_password="", ntrip_bind="all")
    assert all_anon.fix and "set NTRIP_PASSWORD" in all_anon.fix
    # cloudflared forwards to localhost, which a tailnet-only listener is not on.
    unreachable = exposure_of(tunnel_token="abc")
    assert unreachable.ok is None and "cannot reach the web UI" in unreachable.detail
    assert "cannot reach the caster" in unreachable.detail
    assert unreachable.fix and "localhost" in unreachable.fix
    # An anonymous caster on every interface; a rover runs no caster at all.
    assert exposure_of(ntrip_password="", ntrip_bind="all").ok is None
    assert exposure_of(ntrip_password="", ntrip_bind="all", role="rover").ok is True


def test_tunnel_without_password_or_the_insecure_flag_fails(quiet_host: Settings) -> None:
    """`Settings` refuses this; a caller that bypasses it still gets the hard failure."""
    values = quiet_host.model_dump() | {
        "web_bind": "lan",
        "web_allow_insecure": False,
        "tunnel_token": "abc",
    }
    check = doctor._exposure_check(Settings.model_construct(**values))
    assert check.ok is False and check.fix == "set WEB_PASSWORD"


@pytest.mark.parametrize("web_bind", ["127.0.0.1", "lan"])
def test_public_domain_without_password_fails(quiet_host: Settings, web_bind: str) -> None:
    """The `public` profile's Caddy proxies PUBLIC_DOMAIN to 127.0.0.1:8080, open to anyone."""
    settings = with_(
        quiet_host, web_bind=web_bind, web_allow_insecure=True, public_domain="rtk.example.com"
    )
    check = by_name(doctor.run_checks(settings))["exposure"]
    assert check.ok is False and "PUBLIC_DOMAIN" in check.detail
    assert check.fix == "set WEB_PASSWORD"
    protected = with_(settings, web_password=LONG)
    assert by_name(doctor.run_checks(protected))["exposure"].ok is True


def test_a_short_password_fails_once_the_login_is_on_the_internet(quiet_host: Settings) -> None:
    """Found by review: `WEB_PASSWORD=cat` passed every check on the public profiles."""

    def exposure_of(**values: object) -> doctor.Check:
        return by_name(doctor.run_checks(with_(quiet_host, **values)))["exposure"]

    for published in ({"public_domain": "rtk.example.com"}, {"tunnel_token": "abc"}):
        short = exposure_of(web_bind="127.0.0.1", web_password="cat", **published)
        assert short.ok is False and "WEB_PASSWORD is 3 characters" in short.detail
        assert short.fix and "openssl rand" in short.fix
        assert exposure_of(web_bind="127.0.0.1", web_password=LONG, **published).ok in (True, None)
    # On the tailnet or a LAN nobody outside can guess at it.
    assert exposure_of(web_bind="lan", web_password="cat").ok is True


def test_access_mode_on_lan_fails(quiet_host: Settings) -> None:
    """compose and .env.example say Access mode is never with lan: the UI is open on the LAN."""
    check = by_name(
        doctor.run_checks(
            with_(quiet_host, web_bind="lan", web_allow_insecure=True, tunnel_token="abc")
        )
    )["exposure"]
    assert check.ok is False and "LAN and the tailnet" in check.detail
    assert check.fix and "WEB_BIND=127.0.0.1" in check.fix


def test_the_public_profile_forwards_an_anonymous_caster(quiet_host: Settings) -> None:
    check = by_name(
        doctor.run_checks(
            with_(
                quiet_host,
                ntrip_password="",
                ntrip_bind="lan",
                web_bind="127.0.0.1",
                web_password=LONG,
                public_domain="rtk.example.com",
            )
        )
    )["exposure"]
    assert check.ok is None and "forwards 2101" in check.detail
    assert check.fix and "set NTRIP_PASSWORD" in check.fix


def test_the_template_ntrip_password_on_an_exposed_caster_warns(quiet_host: Settings) -> None:
    exposed = with_(quiet_host, ntrip_password="change-me", ntrip_bind="all")
    check = by_name(doctor.run_checks(exposed))["exposure"]
    assert check.ok is None and "'change-me'" in check.detail
    # On the tailnet it is still a placeholder, but nobody else can reach it.
    tailnet = with_(quiet_host, ntrip_password="change-me")
    assert by_name(doctor.run_checks(tailnet))["exposure"].ok is True


def test_lan_on_a_host_with_a_public_address_is_all(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found by review: `lan` binds 0.0.0.0, and on a VPS that is the internet."""
    monkeypatch.setattr(doctor, "_public_addresses", lambda: ["203.0.113.7"])
    open_ui = with_(quiet_host, web_bind="lan", web_allow_insecure=True)
    check = by_name(doctor.run_checks(open_ui))["exposure"]
    assert check.ok is False and "203.0.113.7" in check.detail
    # An explicit private address stays on the LAN whatever else the host has.
    private = with_(quiet_host, web_bind="192.168.1.20", web_allow_insecure=True)
    assert by_name(doctor.run_checks(private))["exposure"].ok is None
    # ... and a short password on that public address is a FAIL too.
    short = with_(quiet_host, web_bind="lan", web_password="cat")
    assert by_name(doctor.run_checks(short))["exposure"].ok is False


def test_public_addresses_skip_private_cgnat_and_tailnet(monkeypatch: pytest.MonkeyPatch) -> None:
    addr = lambda a: SimpleNamespace(address=a)  # noqa: E731
    interfaces = {
        "lo": [addr("127.0.0.1"), addr("::1")],
        "eth0": [addr("192.168.1.20"), addr("aa:bb:cc:dd:ee:ff"), addr("fe80::1%eth0")],
        "tailscale0": [addr("100.100.50.10"), addr("fd7a:115c:a1e0::1")],
        "wan": [addr("93.184.216.34"), addr("2001:db8::1"), addr("2606:4700::1")],
    }
    monkeypatch.setattr(doctor.psutil, "net_if_addrs", lambda: interfaces)
    assert REAL["_public_addresses"]() == ["93.184.216.34", "2606:4700::1"]


def test_public_domain_with_a_tailnet_only_ui_is_unreachable(quiet_host: Settings) -> None:
    check = by_name(doctor.run_checks(with_(quiet_host, public_domain="rtk.example.com")))
    assert check["exposure"].ok is None and "Caddy cannot reach" in check["exposure"].detail
    assert check["exposure"].fix and "localhost" in check["exposure"].fix


def test_time_sync_warns(quiet_host: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_ntp_synchronized", lambda: False)
    check = by_name(doctor.run_checks(quiet_host))["time_sync"]
    assert check.ok is None and "host time" in check.detail and check.fix


def test_unknown_service_state_is_not_reported_as_fine(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside the container there is no systemd: doctor cannot see the host's ModemManager."""
    monkeypatch.setattr(doctor, "_service_active", lambda name: None)
    monkeypatch.setattr(doctor, "_ntp_synchronized", lambda: None)
    c = by_name(doctor.run_checks(quiet_host))
    assert c["modemmanager"].ok is None and "cannot tell" in c["modemmanager"].detail
    assert c["modemmanager"].fix and "host" in c["modemmanager"].fix
    assert c["time_sync"].ok is None and "cannot tell" in c["time_sync"].detail


def test_service_active_tells_unknown_from_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    out = {"stdout": ""}
    monkeypatch.setattr(doctor, "_command_output", lambda args: out["stdout"])
    service_active = REAL["_service_active"]
    for stdout, state in [("active", True), ("inactive", False), ("failed", False), ("", None)]:
        out["stdout"] = stdout
        assert service_active("ModemManager") is state


def test_ntp_synchronized_parses_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    timedatectl = {"out": "yes"}
    services: dict[str, bool | None] = {}

    def command_output(args: list[str]) -> str:
        return timedatectl["out"] if args[0] == "timedatectl" else ""

    monkeypatch.setattr(doctor, "_command_output", command_output)
    monkeypatch.setattr(doctor, "_service_active", lambda name: services.get(name, False))
    synced = REAL["_ntp_synchronized"]
    assert synced() is True
    timedatectl["out"] = "no"
    assert synced() is False
    timedatectl["out"] = ""  # no timedatectl: a running time daemon is the next best
    services["chronyd"] = True
    assert synced() is True
    services.clear()
    assert synced() is False
    monkeypatch.setattr(doctor, "_service_active", lambda name: None)
    assert synced() is None  # no systemctl either: nothing to go on


def test_tailscale_fails_when_a_bind_needs_it(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "tailscale_ipv4", lambda: None)
    check = by_name(doctor.run_checks(quiet_host))["tailscale"]
    assert check.ok is False and check.fix == "sudo tailscale up"


def test_docker_absent_is_informational(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    check = by_name(doctor.run_checks(quiet_host))["docker"]
    assert check.ok is None and "not installed" in check.detail


def test_data_dir_not_writable_fails(quiet_host: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    real_access = doctor.os.access
    data_dir = str(quiet_host.data_dir)
    monkeypatch.setattr(
        doctor.os,
        "access",
        lambda path, mode, **kw: False if str(path) == data_dir else real_access(path, mode),
    )
    check = by_name(doctor.run_checks(quiet_host))["data_dir"]
    assert check.ok is False and "NOT writable" in check.detail


def test_data_dir_low_space_fails(quiet_host: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda path: SimpleNamespace(free=1e9))
    check = by_name(doctor.run_checks(with_(quiet_host, min_free_gb=5)))["data_dir"]
    assert check.ok is False and "1.0 GB free" in check.detail
    assert check.fix and "MIN_FREE_GB" in check.fix


def test_data_dir_missing_or_unreadable(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fresh = by_name(doctor.run_checks(with_(quiet_host, data_dir=tmp_path / "new")))["data_dir"]
    assert fresh.ok is None and "does not exist yet" in fresh.detail

    def broken(path: object) -> None:
        raise OSError("I/O error")

    monkeypatch.setattr(doctor.shutil, "disk_usage", broken)
    check = by_name(doctor.run_checks(quiet_host))["data_dir"]
    assert check.ok is False and "cannot read free space" in check.detail


# ----------------------------------------------------------------- the receiver path


def test_a_pty_source_is_checked_without_being_opened(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """MTRTK_SOURCE can be a socat PTY (a tunnel to a remote receiver), not a by-id link.

    The live setup is a symlink to /dev/pts/N; doctor follows it but never opens it.
    """
    monkeypatch.setattr(doctor, "_service_active", lambda name: name == "ModemManager")
    master, slave = os.openpty()
    try:
        link = tmp_path / "f9p"
        link.symlink_to(os.ttyname(slave))
        c = by_name(doctor.run_checks(with_(quiet_host, mtrtk_source=str(link))))
    finally:
        os.close(master)
        os.close(slave)
    assert c["receiver"].ok is True and "(pty, read/write ok)" in c["receiver"].detail
    # ModemManager only grabs local USB serial devices; this one is not.
    assert c["modemmanager"].ok is True and "not a local USB" in c["modemmanager"].detail


def test_a_udev_alias_of_a_usb_tty_is_usb(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """/dev/f9p -> ttyACM0 is a local USB device ModemManager can grab."""
    alias = tmp_path / "f9p"
    alias.write_bytes(b"")
    real_realpath = doctor.os.path.realpath
    monkeypatch.setattr(
        doctor.os.path,
        "realpath",
        lambda p, **kw: "/dev/ttyACM0" if str(p) == str(alias) else real_realpath(p, **kw),
    )
    assert doctor._is_usb_serial(str(alias)) is True
    monkeypatch.setattr(doctor, "_service_active", lambda name: name == "ModemManager")
    c = by_name(doctor.run_checks(with_(quiet_host, mtrtk_source=str(alias))))
    assert "(USB serial, read/write ok)" in c["receiver"].detail
    assert c["modemmanager"].ok is None and c["modemmanager"].fix


def test_a_missing_source_path_fails_with_link_advice(quiet_host: Settings, tmp_path: Path) -> None:
    gone = tmp_path / "f9p"
    check = by_name(doctor.run_checks(with_(quiet_host, mtrtk_source=str(gone))))["receiver"]
    assert check.ok is False and "does not exist" in check.detail
    assert check.fix and "socat" in check.fix
    by_id = "/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_GNSS_receiver-if00-nope"
    check = by_name(doctor.run_checks(with_(quiet_host, mtrtk_source=by_id)))["receiver"]
    assert check.ok is False and check.fix and "USB" in check.fix


def test_an_unwritable_source_points_at_dialout(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    port = tmp_path / "ttyACM9"
    port.write_bytes(b"")
    real_access = doctor.os.access
    monkeypatch.setattr(
        doctor.os,
        "access",
        lambda path, mode, **kw: False if str(path) == str(port) else real_access(path, mode),
    )
    check = by_name(doctor.run_checks(with_(quiet_host, mtrtk_source=str(port))))["receiver"]
    assert check.ok is False and check.fix and "dialout" in check.fix


def test_a_replay_file_source(quiet_host: Settings, tmp_path: Path) -> None:
    capture = tmp_path / "cap.ubx"
    capture.write_bytes(b"")
    c = by_name(doctor.run_checks(with_(quiet_host, mtrtk_source=f"file:{capture}")))
    assert c["receiver"].ok is True and "replay" in c["receiver"].detail


def test_probe_reports_firmware_and_warns_when_old(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    port = tmp_path / "ttyACM0"
    port.write_bytes(b"")
    settings = with_(quiet_host, mtrtk_source=str(port))
    probed: list[tuple[str, int]] = []

    def probe(p: str, baud: int, fw: str = "HPG 1.13") -> str:
        probed.append((p, baud))
        return fw

    monkeypatch.setattr(doctor, "_probe_firmware", probe)
    c = by_name(doctor.run_checks(settings, probe_receiver=True))
    assert probed == [(str(port), 115200)]
    assert "HPG 1.13" in c["receiver"].detail
    assert c["firmware"].ok is None and "1.32" in c["firmware"].detail
    monkeypatch.setattr(doctor, "_probe_firmware", lambda p, baud: "HPG 1.51")
    assert by_name(doctor.run_checks(settings, probe_receiver=True))["firmware"].ok is True
    monkeypatch.setattr(doctor, "_probe_firmware", lambda p, baud: None)
    silent = by_name(doctor.run_checks(settings, probe_receiver=True))["firmware"]
    assert silent.ok is None and "no MON-VER" in silent.detail


def test_probe_is_skipped_while_the_daemon_runs(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Polling the port beside the daemon would split its byte stream with the daemon's reader."""
    port = tmp_path / "ttyACM0"
    port.write_bytes(b"")
    monkeypatch.setattr(
        doctor, "_port_owner", lambda p, hosts=None: SimpleNamespace(pid=7, name="mtrtk")
    )
    c = by_name(doctor.run_checks(with_(quiet_host, mtrtk_source=str(port)), probe_receiver=True))
    assert c["firmware"].ok is None and "daemon" in c["firmware"].detail


def test_probe_is_skipped_beside_a_daemon_this_user_cannot_see(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A root container (or another user's unit) holds the ports with a pid we cannot see."""
    port = tmp_path / "ttyACM0"
    port.write_bytes(b"")
    monkeypatch.setattr(
        doctor, "_port_owner", lambda p, hosts=None: SimpleNamespace(pid=None, name=None)
    )
    # The autouse no_probe raises if doctor opens the receiver.
    c = by_name(doctor.run_checks(with_(quiet_host, mtrtk_source=str(port)), probe_receiver=True))
    assert c["firmware"].ok is None and "cannot see" in c["firmware"].detail
    assert c["firmware"].fix and "inside the container" in c["firmware"].fix


def test_probe_that_cannot_open_the_port_says_why(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    port = tmp_path / "ttyACM0"
    port.write_bytes(b"")

    def busy(p: str, baud: int) -> str | None:
        raise doctor.ProbeError(f"could not open {p}: [Errno 16] Device or resource busy")

    monkeypatch.setattr(doctor, "_probe_firmware", busy)
    c = by_name(doctor.run_checks(with_(quiet_host, mtrtk_source=str(port)), probe_receiver=True))
    assert c["firmware"].ok is None and "resource busy" in c["firmware"].detail
    assert "BAUD" not in (c["firmware"].fix or "")


class FakeSerial:
    """Records what doctor writes; replies with one MON-VER. No device is opened."""

    instances: list[FakeSerial] = []

    def __init__(self, port: str, baud: int, timeout: float) -> None:
        self.args = (port, baud)
        self.written: list[bytes] = []
        self.replies = [mon_ver_bytes(fw="HPG 1.13")]
        FakeSerial.instances.append(self)

    def __enter__(self) -> FakeSerial:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def reset_input_buffer(self) -> None:
        return None

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data)

    def read(self, size: int) -> bytes:
        return self.replies.pop(0) if self.written and self.replies else b""


def test_probe_firmware_writes_only_a_mon_ver_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """The only bytes doctor ever sends a receiver: one MON-VER poll, never a CFG message."""
    FakeSerial.instances.clear()
    monkeypatch.setattr(serial, "Serial", FakeSerial)
    assert REAL["_probe_firmware"]("/dev/fake-f9p", 38400) == "HPG 1.13"
    (ser,) = FakeSerial.instances
    assert ser.args == ("/dev/fake-f9p", 38400)
    assert ser.written == [UBXMessage("MON", "MON-VER", POLL).serialize()]


def test_probe_firmware_reports_open_and_read_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(port: str, baud: int, timeout: float) -> None:
        raise serial.SerialException(f"[Errno 13] could not open port {port}: Permission denied")

    monkeypatch.setattr(serial, "Serial", refuse)
    with pytest.raises(doctor.ProbeError, match="could not open /dev/fake-f9p"):
        REAL["_probe_firmware"]("/dev/fake-f9p", 115200)

    class Unplugged(FakeSerial):
        def read(self, size: int) -> bytes:
            raise serial.SerialException("device reports readiness to read but returned no data")

    monkeypatch.setattr(serial, "Serial", Unplugged)
    with pytest.raises(doctor.ProbeError, match="reading /dev/fake-f9p failed"):
        REAL["_probe_firmware"]("/dev/fake-f9p", 115200)


def test_read_firmware_finds_mon_ver_in_the_stream() -> None:
    ver = mon_ver_bytes(fw="HPG 1.13")
    chunks = [b"\x00\xffnoise" + nmea_frame("GNZDA,,,,,,"), ver[:20], ver[20:]]
    assert doctor._read_firmware(lambda: chunks.pop(0) if chunks else b"", 1.0) == "HPG 1.13"
    assert doctor._read_firmware(lambda: b"", 0.05) is None


@pytest.mark.parametrize(
    ("fw", "parsed"),
    [("HPG 1.13", (1, 13)), ("HPG 1.51", (1, 51)), ("HPGL1L5 1.40", (1, 40)), ("odd", None)],
)
def test_parse_fw(fw: str, parsed: tuple[int, int] | None) -> None:
    assert doctor._parse_fw(fw) == parsed


# ----------------------------------------------------------------- output and the CLI


def test_format_table_shows_fixes_only_for_problems() -> None:
    table = doctor.format_table(
        [
            doctor.Check("python", True, "3.12.3", fix="never shown"),
            doctor.Check("tailscale", False, "no IPv4", fix="sudo tailscale up"),
            doctor.Check("docker", None, "Docker version 29.6.2"),
            doctor.Check("rtklib", None, "missing: convbin"),
        ]
    )
    assert "[OK  ] python" in table and "never shown" not in table
    # docker is information, not a problem: it has no WARN mark (and no exit code effect).
    assert "[INFO] docker" in table and "[WARN] rtklib" in table
    assert "[FAIL] tailscale" in table and "fix: sudo tailscale up" in table


def test_cli_json_and_exit_code(quiet_host: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(quiet_host.data_dir))
    r = CliRunner().invoke(main, ["doctor", "--json"])
    assert r.exit_code == 1  # receiver missing
    data = json.loads(r.output)
    assert {c["name"] for c in data} >= {"python", "receiver", "tailscale", "exposure"}
    assert [c["name"] for c in data if c["ok"] is False] == ["receiver"]
    assert set(data[0]) == {"name", "ok", "detail", "fix"}


def test_cli_probe_polls_the_receiver(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    port = tmp_path / "ttyACM0"
    port.write_bytes(b"")
    monkeypatch.setenv("MTRTK_SOURCE", str(port))
    monkeypatch.setattr(doctor, "_probe_firmware", lambda p, baud: "HPG 1.13")
    r = CliRunner().invoke(main, ["doctor", "--probe", "--json"])
    assert r.exit_code == 0, r.output
    firmware = next(c for c in json.loads(r.output) if c["name"] == "firmware")
    assert firmware["ok"] is None and "HPG 1.13" in firmware["detail"]


def test_cli_exits_zero_on_warnings_only(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    port = tmp_path / "ttyACM0"
    port.write_bytes(b"")
    monkeypatch.setenv("MTRTK_SOURCE", str(port))
    r = CliRunner().invoke(main, ["doctor"])
    assert r.exit_code == 0, r.output
    assert "[WARN] rtklib" in r.output


def test_cli_reports_an_unset_ntrip_password(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The base refuses to start without NTRIP_PASSWORD; doctor says so instead of hiding it."""
    monkeypatch.delenv("NTRIP_PASSWORD")
    r = CliRunner().invoke(main, ["doctor", "--json"])
    assert r.exit_code == 1
    config = next(c for c in json.loads(r.output) if c["name"] == "config")
    assert config["ok"] is False and "NTRIP_PASSWORD" in config["detail"]


def test_cli_unset_ntrip_password_is_not_reported_as_anonymous(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset password is not an anonymous caster: the rest is checked as configured."""
    monkeypatch.delenv("NTRIP_PASSWORD")
    monkeypatch.setenv("NTRIP_BIND", "all")
    data = json.loads(CliRunner().invoke(main, ["doctor", "--json"]).output)
    exposure = next(c for c in data if c["name"] == "exposure")
    assert "anonymous" not in exposure["detail"]


def test_cli_reports_an_invalid_configuration_as_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAUD", "fast")
    r = CliRunner().invoke(main, ["doctor", "--json"])
    assert r.exit_code == 1
    data = json.loads(r.output)
    assert data[0]["name"] == "config" and data[0]["ok"] is False and "baud" in data[0]["detail"]


def test_tailscale_is_a_warning_when_nothing_binds_to_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "find_ublox_port", lambda: None)
    monkeypatch.setattr(doctor, "tailscale_ipv4", lambda: None)
    settings = Settings(
        _env_file=None,
        ntrip_password="",
        ntrip_bind="lan",
        web_bind="lan",
        web_allow_insecure=True,
    )
    checks = {c.name: c for c in doctor.run_checks(settings)}
    assert checks["tailscale"].ok is None
    assert "tailscaled" in checks["tailscale"].detail


def test_tailscale_up_is_ok_when_nothing_binds_to_it(quiet_host: Settings) -> None:
    """Found in acceptance: a `lan` / loopback station with Tailscale up printed `[WARN]
    tailscale 100.100.50.10` with no fix line - a warning about nothing."""
    settings = with_(quiet_host, ntrip_bind="127.0.0.1", web_bind="127.0.0.1")
    check = by_name(doctor.run_checks(settings))["tailscale"]
    assert check.ok is True and check.detail == "100.100.50.10" and check.fix is None


def test_tailscale_fails_when_an_explicit_tailnet_address_needs_it(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NTRIP_BIND=100.101.1.2 is bound as given: with tailscale0 down the bind fails."""
    monkeypatch.setattr(doctor, "tailscale_ipv4", lambda: None)
    settings = with_(quiet_host, ntrip_bind="100.101.1.2", web_bind="127.0.0.1")
    check = by_name(doctor.run_checks(settings))["tailscale"]
    assert check.ok is False and check.fix == "sudo tailscale up"


def test_tailscale_fails_when_an_explicit_tailnet_address_is_not_its_own(
    quiet_host: Settings,
) -> None:
    """Tailscale up with another address: binding the old literal fails with EADDRNOTAVAIL."""
    settings = with_(quiet_host, ntrip_bind="100.101.1.2", web_bind="127.0.0.1")
    check = by_name(doctor.run_checks(settings))["tailscale"]
    assert check.ok is False
    assert check.detail == "tailscale0 is 100.100.50.10, NTRIP_BIND is 100.101.1.2"
    assert check.fix is not None and "NTRIP_BIND=tailscale" in check.fix
    same = with_(quiet_host, ntrip_bind="100.100.50.10", web_bind="127.0.0.1")
    assert by_name(doctor.run_checks(same))["tailscale"].ok is True


def test_tailscale_fails_when_the_rover_nmea_bind_needs_it(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "tailscale_ipv4", lambda: None)
    rover = with_(
        quiet_host,
        role="rover",
        ntrip_password="",
        ntrip_bind="127.0.0.1",
        web_bind="127.0.0.1",
        nmea_tcp_bind="tailscale",
        nmea_tcp_port=10110,
    )
    assert by_name(doctor.run_checks(rover))["tailscale"].ok is False
    any_port = with_(rover, nmea_tcp_port=0)  # 0 = any free port: it still listens
    assert by_name(doctor.run_checks(any_port))["tailscale"].ok is False
    nmea_off = with_(rover, nmea_tcp_port=-1)
    assert by_name(doctor.run_checks(nmea_off))["tailscale"].ok is None


def test_a_rover_is_not_judged_on_the_casters_bind(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rover runs no caster: the default NTRIP_BIND=tailscale must not make Tailscale a FAIL
    on a field rover that pulls corrections from a public caster over LTE."""
    monkeypatch.setattr(doctor, "tailscale_ipv4", lambda: None)
    rover = with_(
        quiet_host,
        role="rover",
        ntrip_password="",
        ntrip_bind="tailscale",
        web_bind="127.0.0.1",
        web_password="pw",
        nmea_tcp_port=-1,
    )
    assert by_name(doctor.run_checks(rover))["tailscale"].ok is None
    # ... and a stale tailnet literal in NTRIP_BIND is not the rover's problem either.
    monkeypatch.setattr(doctor, "tailscale_ipv4", lambda: "100.64.0.9")
    stale = with_(rover, ntrip_bind="100.64.0.5")
    assert by_name(doctor.run_checks(stale))["tailscale"].ok is True


def test_tailscale_ipv4_is_none_without_the_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exposure.psutil, "net_if_addrs", dict)
    assert exposure.tailscale_ipv4() is None
