from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from mtrtk.config import OPTIONAL_FIELDS, DynModel, Role, Settings

REPO_ROOT = Path(__file__).resolve().parents[2]


def make(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setenv("NTRIP_PASSWORD", "secret")
    # The suite-wide fixture points these at loopback so no test binds a real interface; here
    # the declared defaults themselves are under test, so the environment must not supply them.
    for key in ("NTRIP_BIND", "NTRIP_PORT", "WEB_BIND", "WEB_PORT", "WEB_ALLOW_INSECURE"):
        monkeypatch.delenv(key, raising=False)
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


def test_rtcm_msm_accepts_numeric_env_string(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make(monkeypatch, RTCM_MSM="4")
    assert s.rtcm_msm == 4


def test_empty_optional_env_values_are_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """`KEY=` in a .env file means "not set", not "empty string" (and not a parse error)."""
    s = make(monkeypatch, ACTIVE_SITE="", JSON_UDP_PORT="", NTRIP_URL="   ")
    assert s.active_site is None
    assert s.json_udp_port is None
    assert s.ntrip_url is None


def test_optional_fields_list_covers_every_nullable_field() -> None:
    """Guard against a new `X | None` field silently missing the empty-is-unset validator."""
    nullable = {
        name
        for name, field in Settings.model_fields.items()
        if type(None) in get_args(field.annotation)
    }
    # ntrip_password is deliberately excluded: there "" means anonymous, not unset.
    assert nullable - {"ntrip_password"} == set(OPTIONAL_FIELDS)


def test_env_example_loads_to_code_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """.env.example documents the code defaults, so loading it must reproduce them exactly."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)

    from_template = Settings(_env_file=REPO_ROOT / ".env.example").model_dump()
    defaults = Settings(_env_file=None, ntrip_password="change-me").model_dump()

    assert from_template == defaults
