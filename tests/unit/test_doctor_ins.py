"""`mtrtk doctor` on an INS rover: the INS port instead of a u-blox receiver, and link advice."""

from pathlib import Path

import pytest

from doctorhost import patch_host_probes
from mtrtk import doctor
from mtrtk.config import Settings


@pytest.fixture(autouse=True)
def hermetic_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """No systemctl, timedatectl, docker, psutil or host udev rules: none decide these tests."""
    patch_host_probes(monkeypatch)


def _checks(monkeypatch: pytest.MonkeyPatch, **values: object) -> dict[str, doctor.Check]:
    monkeypatch.setattr(doctor, "find_ublox_port", lambda: None)
    monkeypatch.setattr(doctor, "tailscale_ipv4", lambda: None)
    base: dict[str, object] = {"role": "rover", "ntrip_password": "", "ins_raw_gnss": False}
    base.update(values)
    settings = Settings(_env_file=None, **base)  # type: ignore[arg-type]
    return {c.name: c for c in doctor.run_checks(settings)}


def test_ins_port_replaces_the_ublox_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    port = tmp_path / "ttyUSB0"
    port.write_bytes(b"")
    checks = _checks(monkeypatch, rover_driver="sbg_ellipse", ins_port=str(port), ins_baud=921600)
    assert "receiver" not in checks
    assert checks["ins_port"].ok is True and "read/write ok" in checks["ins_port"].detail
    assert "ins_baud" not in checks and "ins_rtcm" not in checks


def test_missing_and_unreadable_ins_ports_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = _checks(monkeypatch, rover_driver="vectornav", ins_port=str(tmp_path / "nope"))
    assert missing["ins_port"].ok is False and "does not exist" in missing["ins_port"].detail
    locked = tmp_path / "locked"
    locked.write_bytes(b"")
    # A file mode would not do: root (a container CI job) reads and writes a mode-0 file.
    real_access = doctor.os.access
    monkeypatch.setattr(
        doctor.os,
        "access",
        lambda path, mode, **kw: False if str(path) == str(locked) else real_access(path, mode),
    )
    checks = _checks(monkeypatch, rover_driver="vectornav", ins_port=str(locked))
    assert checks["ins_port"].ok is False and "dialout" in checks["ins_port"].detail


@pytest.mark.parametrize(
    ("values", "warned"),
    [
        ({"ins_output_hz": 100, "ins_baud": 115200}, True),
        ({"ins_raw_gnss": True, "ins_baud": 230400}, True),
        ({"ins_output_hz": 50, "ins_baud": 115200}, False),
        ({"ins_output_hz": 200, "ins_baud": 460800, "ins_raw_gnss": True}, False),
    ],
)
def test_baud_advice(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, object], warned: bool
) -> None:
    checks = _checks(monkeypatch, rover_driver="sbg_ellipse", ins_port="/dev/null", **values)
    if warned:
        assert checks["ins_baud"].ok is None and "460800" in checks["ins_baud"].detail
    else:
        assert "ins_baud" not in checks


def test_rtcm_on_the_sbg_main_port_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "ntrip://u:p@caster:2101/MTRK"
    same_port = _checks(
        monkeypatch, rover_driver="sbg_ellipse", ins_port="/dev/null", ntrip_url=url
    )
    assert same_port["ins_rtcm"].ok is None
    assert "RTCM on same port unverified" in same_port["ins_rtcm"].detail
    port_b = _checks(
        monkeypatch,
        rover_driver="sbg_ellipse",
        ins_port="/dev/null",
        ins_rtcm_port="/dev/null",
        ntrip_url=url,
    )
    assert "ins_rtcm" not in port_b
    vn = _checks(monkeypatch, rover_driver="vectornav", ins_port="/dev/null", ntrip_url=url)
    assert "ins_rtcm" not in vn


def test_a_base_still_checks_the_ublox_receiver(monkeypatch: pytest.MonkeyPatch) -> None:
    checks = _checks(monkeypatch, role="base", rover_driver="sbg_ellipse", ins_port="/dev/null")
    assert checks["receiver"].ok is False and "ins_port" not in checks


def test_an_ins_replay_checks_the_file_not_ins_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`MTRTK_SOURCE=file:` replays through the INS stack with no INS_PORT (config allows it)."""
    capture = tmp_path / "ellipse.sbg"
    capture.write_bytes(b"")
    checks = _checks(
        monkeypatch,
        rover_driver="sbg_ellipse",
        mtrtk_source=f"file:{capture}",
        ins_raw_gnss=True,
        ins_baud=115200,
    )
    assert "ins_port" not in checks and "ins_baud" not in checks
    assert checks["receiver"].ok is True and str(capture) in checks["receiver"].detail
    gone = _checks(monkeypatch, rover_driver="vectornav", mtrtk_source=f"file:{tmp_path / 'x'}")
    assert gone["receiver"].ok is False
