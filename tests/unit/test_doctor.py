import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from mtrtk import doctor
from mtrtk.cli import main
from mtrtk.config import Settings
from mtrtk.core import exposure
from ubxtest import mon_ver_bytes, nmea_frame


@pytest.fixture
def quiet_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    """A host where every helper doctor shells out to or asks psutil about is patched."""
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    monkeypatch.setattr(doctor, "find_ublox_port", lambda: None)
    monkeypatch.setattr(doctor, "tailscale_ipv4", lambda: "100.100.50.10")
    monkeypatch.setattr(
        doctor.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )
    monkeypatch.setattr(doctor, "_service_active", lambda name: False)
    monkeypatch.setattr(doctor, "_ntp_synchronized", lambda: True)
    monkeypatch.setattr(doctor, "_port_owner", lambda port: None)
    monkeypatch.setattr(doctor, "_udev_rule_present", lambda: False)
    monkeypatch.setattr(doctor, "_command_output", lambda args: "Docker version 29.6.2")
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda path: SimpleNamespace(free=100e9))

    def no_probe(port: str, baud: int) -> str | None:
        raise AssertionError("doctor opened the receiver without --probe")

    monkeypatch.setattr(doctor, "_probe_firmware", no_probe)
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
    assert doctor._udev_rule_present() is False
    (tmp_path / "50-other.rules").write_text('ATTRS{idVendor}=="0403", MODE="0660"\n')
    assert doctor._udev_rule_present() is False
    (tmp_path / "99-mtrtk-ublox.rules").write_text(doctor.UDEV_RULE + "\n")
    assert doctor._udev_rule_present() is True


def test_ports_fail_when_foreign_process_holds_them(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        doctor,
        "_port_owner",
        lambda port: SimpleNamespace(pid=4242, name="str2str") if port == 2101 else None,
    )
    c = by_name(doctor.run_checks(quiet_host))
    assert c["ports"].ok is False and "str2str" in c["ports"].detail
    monkeypatch.setattr(doctor, "_port_owner", lambda port: SimpleNamespace(pid=1, name="mtrtk"))
    c = by_name(doctor.run_checks(quiet_host))
    assert c["ports"].ok is True and "mtrtk" in c["ports"].detail


def test_ports_owner_unknown_is_a_warning(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another user's (or another namespace's) listener has no pid we can see."""
    monkeypatch.setattr(doctor, "_port_owner", lambda port: SimpleNamespace(pid=None, name=None))
    assert by_name(doctor.run_checks(quiet_host))["ports"].ok is None


def test_ports_skips_ephemeral_and_follows_the_role(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[int] = []
    monkeypatch.setattr(doctor, "_port_owner", lambda port: asked.append(port))
    doctor.run_checks(with_(quiet_host, ntrip_port=0))
    assert asked == [8080]
    asked.clear()
    rover = with_(quiet_host, role="rover", ntrip_password="", nmea_tcp_port=10110)
    doctor.run_checks(rover)
    assert asked == [8080, 10110]  # a rover runs no caster


def test_port_owner_finds_a_real_listener() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        owner = doctor._port_owner(sock.getsockname()[1])
    assert owner is not None and owner.pid == os.getpid()


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
        web_password="pw",
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
    # A tunnel publishes whatever listens on localhost: the UI must have a password.
    tunnel = exposure_of(web_bind="lan", web_allow_insecure=True, tunnel_token="abc")
    assert tunnel.ok is False and tunnel.fix and "WEB_PASSWORD" in tunnel.fix
    assert exposure_of(web_password="pw", web_bind="all").ok is True
    # cloudflared forwards to localhost, which a tailnet-only listener is not on.
    unreachable = exposure_of(tunnel_token="abc")
    assert unreachable.ok is None and "cannot reach the web UI" in unreachable.detail
    assert "cannot reach the caster" in unreachable.detail
    assert unreachable.fix and "localhost" in unreachable.fix
    # An anonymous caster on every interface; a rover runs no caster at all.
    assert exposure_of(ntrip_password="", ntrip_bind="all").ok is None
    assert exposure_of(ntrip_password="", ntrip_bind="all", role="rover").ok is True


def test_time_sync_warns(quiet_host: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_ntp_synchronized", lambda: False)
    check = by_name(doctor.run_checks(quiet_host))["time_sync"]
    assert check.ok is None and "host time" in check.detail and check.fix


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


# ----------------------------------------------------------------- the receiver path


def test_a_pty_source_is_checked_without_being_opened(
    quiet_host: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """MTRTK_SOURCE can be a socat PTY (a tunnel to a remote receiver), not a by-id link."""
    monkeypatch.setattr(doctor, "_service_active", lambda name: name == "ModemManager")
    pty = tmp_path / "f9p"
    pty.write_bytes(b"")
    c = by_name(doctor.run_checks(with_(quiet_host, mtrtk_source=str(pty))))
    assert c["receiver"].ok is True and "read/write ok" in c["receiver"].detail
    # ModemManager only grabs local USB serial devices; this one is not.
    assert c["modemmanager"].ok is True and "not a local USB" in c["modemmanager"].detail


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
    monkeypatch.setattr(doctor, "_port_owner", lambda p: SimpleNamespace(pid=7, name="mtrtk"))
    c = by_name(doctor.run_checks(with_(quiet_host, mtrtk_source=str(port)), probe_receiver=True))
    assert c["firmware"].ok is None and "daemon" in c["firmware"].detail


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
    assert any(c["ok"] is False for c in data)
    assert set(data[0]) == {"name", "ok", "detail", "fix"}


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


def test_tailscale_ipv4_is_none_without_the_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exposure.psutil, "net_if_addrs", dict)
    assert exposure.tailscale_ipv4() is None
