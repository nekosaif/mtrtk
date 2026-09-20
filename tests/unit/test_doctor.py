import pytest

from mtrtk import doctor
from mtrtk.config import Settings
from mtrtk.core import exposure


def test_run_checks_reports_each_area(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    monkeypatch.setattr(doctor, "find_ublox_port", lambda: None)
    monkeypatch.setattr(doctor, "tailscale_ipv4", lambda: "100.100.50.10")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    # Explicit: the suite-wide fixture puts both binds on loopback, and the tailscale check is
    # only a pass/fail (rather than a warning) when some bind actually needs the tailnet.
    settings = Settings(_env_file=None, data_dir=tmp_path, web_bind="tailscale")
    checks = {c.name: c for c in doctor.run_checks(settings)}
    assert checks["receiver"].ok is False
    assert checks["tailscale"].ok is True and "100.100.50.10" in checks["tailscale"].detail
    assert checks["rtklib"].ok is None  # warning: only needed from Phase 5
    assert checks["data_dir"].ok is True
    assert checks["python"].ok is True


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
