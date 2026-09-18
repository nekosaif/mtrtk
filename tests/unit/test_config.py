import pytest
from pydantic import ValidationError

from mtrtk.config import DynModel, Role, Settings


def make(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setenv("NTRIP_PASSWORD", "secret")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make(monkeypatch)
    assert s.role is Role.BASE
    assert s.station_id == "MTRK"
    assert s.ntrip_bind == "tailscale"
    assert s.rtcm_msm == 7
    assert s.source_is_file is False
    assert s.ntrip_anonymous is False


def test_env_override_and_role(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make(monkeypatch, ROLE="rover", ROVER_NAV_HZ="8", ROVER_DYNMODEL="automotive")
    assert s.role is Role.ROVER
    assert s.rover_nav_hz == 8
    assert s.rover_dynmodel is DynModel.AUTOMOTIVE
    assert s.dynmodel_code == 4


def test_file_source(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make(monkeypatch, MTRTK_SOURCE="file:tests/fixtures/x.ubx")
    assert s.source_is_file is True
    assert str(s.source_path) == "tests/fixtures/x.ubx"


def test_public_web_bind_requires_password(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="WEB_PASSWORD"):
        make(monkeypatch, WEB_BIND="all")


def test_public_web_bind_allowed_when_insecure_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make(monkeypatch, WEB_BIND="all", WEB_ALLOW_INSECURE="1")
    assert s.web_bind == "all"


def test_bind_accepts_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make(monkeypatch, NTRIP_BIND="100.100.50.10")
    assert s.ntrip_bind == "100.100.50.10"


def test_bind_rejects_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="NTRIP_BIND"):
        make(monkeypatch, NTRIP_BIND="everywhere")


def test_csv_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make(
        monkeypatch,
        NMEA_UDP_TARGETS="192.168.1.5:10110, 10.0.0.2:5000",
        LOG_MESSAGES="RXM-RAWX,RXM-SFRBX",
    )
    assert s.nmea_udp_targets == ["192.168.1.5:10110", "10.0.0.2:5000"]
    assert s.log_messages == ["RXM-RAWX", "RXM-SFRBX"]


def test_base_requires_ntrip_password_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NTRIP_PASSWORD", raising=False)
    with pytest.raises(ValidationError, match="NTRIP_PASSWORD"):
        Settings(_env_file=None)


def test_empty_ntrip_password_means_anonymous(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make(monkeypatch, NTRIP_PASSWORD="")
    assert s.ntrip_anonymous is True


def test_rover_does_not_need_ntrip_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NTRIP_PASSWORD", raising=False)
    monkeypatch.setenv("ROLE", "rover")
    assert Settings(_env_file=None).role is Role.ROVER


def test_station_id_must_be_four_uppercase(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        make(monkeypatch, STATION_ID="abc")


def test_rtcm_msm_only_4_or_7(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        make(monkeypatch, RTCM_MSM="5")
