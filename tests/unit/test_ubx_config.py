import pytest
from pyubx2 import TXN_NONE, UBXMessage
from pyubx2.ubxtypes_configdb import UBX_CONFIG_DATABASE

from mtrtk.config import Settings
from mtrtk.core.ubx_config import (
    LAYERS_ALL,
    LAYERS_RAM,
    MAX_KEYS_PER_VALSET,
    all_keys,
    base_profile,
    chunked,
    rover_profile,
    tmode_fixed_ecef,
    tmode_off,
    tmode_survey_in,
)


@pytest.fixture
def base_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    return Settings(_env_file=None)


@pytest.fixture
def rover_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ROLE", "rover")
    monkeypatch.setenv("ROVER_NAV_HZ", "5")
    monkeypatch.setenv("ROVER_DYNMODEL", "airborne1g")
    return Settings(_env_file=None)


def test_every_key_exists_in_pyubx2_database(
    base_settings: Settings, rover_settings: Settings
) -> None:
    for profile in (base_profile(base_settings), rover_profile(rover_settings)):
        missing = [k for k in all_keys(profile) if k not in UBX_CONFIG_DATABASE]
        assert missing == [], f"{profile.name}: unknown keys {missing}"
    for items in (tmode_off(), tmode_survey_in(300, 2.0), tmode_fixed_ecef(1.0, 2.0, 3.0, 0.01)):
        assert all(k in UBX_CONFIG_DATABASE for k, _ in items)


def test_every_chunk_serialises_to_a_valset(base_settings: Settings) -> None:
    profile = base_profile(base_settings)
    for group in (profile.core, profile.signals, *profile.optional.values()):
        for chunk in chunked(group):
            assert len(chunk) <= MAX_KEYS_PER_VALSET
            msg = UBXMessage.config_set(LAYERS_ALL, TXN_NONE, chunk)
            assert msg.identity == "CFG-VALSET"


def test_base_profile_contents(base_settings: Settings) -> None:
    core = dict(base_profile(base_settings).core)
    assert core["CFG_RATE_MEAS"] == 1000 and core["CFG_RATE_NAV"] == 1
    assert core["CFG_NAVSPG_DYNMODEL"] == 2
    assert core["CFG_USBOUTPROT_RTCM3X"] == 1 and core["CFG_USBINPROT_RTCM3X"] == 0
    assert core["CFG_USBOUTPROT_NMEA"] == 0
    assert core["CFG_MSGOUT_RTCM_3X_TYPE1005_USB"] == 1
    assert core["CFG_MSGOUT_RTCM_3X_TYPE1077_USB"] == 1
    assert core["CFG_MSGOUT_RTCM_3X_TYPE1074_USB"] == 0
    assert core["CFG_MSGOUT_RTCM_3X_TYPE1230_USB"] == 5
    assert core["CFG_MSGOUT_UBX_NAV_SVIN_USB"] == 1 and core["CFG_MSGOUT_UBX_RXM_RAWX_USB"] == 1
    assert core["CFG_MSGOUT_UBX_NAV_EOE_USB"] == 1
    assert "CFG_TMODE_MODE" not in core  # TMODE is applied separately from the site logic


def test_base_profile_msm4_switch() -> None:
    # RTCM_MSM is Literal[4, 7], which pydantic will not parse from the string "4" an
    # environment variable would carry, so the value is passed in directly here.
    settings = Settings(_env_file=None, ntrip_password="x", rtcm_msm=4)
    core = dict(base_profile(settings).core)
    assert core["CFG_MSGOUT_RTCM_3X_TYPE1074_USB"] == 1
    assert core["CFG_MSGOUT_RTCM_3X_TYPE1077_USB"] == 0


def test_rover_profile_contents(rover_settings: Settings) -> None:
    profile = rover_profile(rover_settings)
    core = dict(profile.core)
    assert core["CFG_RATE_MEAS"] == 200 and profile.nav_hz == 5
    assert core["CFG_NAVSPG_DYNMODEL"] == 6
    assert core["CFG_USBINPROT_RTCM3X"] == 1 and core["CFG_USBOUTPROT_RTCM3X"] == 0
    assert core["CFG_MSGOUT_UBX_NAV_RELPOSNED_USB"] == 1
    assert core["CFG_MSGOUT_UBX_RXM_RTCM_USB"] == 1
    assert core["CFG_MSGOUT_UBX_TIM_TM2_USB"] == 1 and core["CFG_NAVHPG_DGNSSMODE"] == 3
    assert core["CFG_TMODE_MODE"] == 0


def test_signals_are_l1_l2_only_with_sbas_off(base_settings: Settings) -> None:
    signals = dict(base_profile(base_settings).signals)
    assert signals["CFG_SIGNAL_SBAS_ENA"] == 0
    assert signals["CFG_SIGNAL_GPS_L2C_ENA"] == 1 and signals["CFG_SIGNAL_GAL_E5B_ENA"] == 1
    assert not any("L5" in k or "E5A" in k for k in signals)


def test_bitfield_keys_carry_raw_bytes(base_settings: Settings) -> None:
    # CFG_INFMSG_UBX_USB is an X1 bitfield; pyubx2 rejects an int for an X-type key.
    assert dict(base_profile(base_settings).core)["CFG_INFMSG_UBX_USB"] == b"\x00"


def test_optional_features(base_settings: Settings) -> None:
    optional = base_profile(base_settings).optional
    assert set(optional) == {"MON-SPAN", "MON-COMMS", "NAV-TIMELS"}


def test_profiles_do_not_share_mutable_state(base_settings: Settings) -> None:
    first, second = base_profile(base_settings), base_profile(base_settings)
    first.core.clear()
    first.signals.clear()
    first.optional["MON-SPAN"].clear()
    del first.optional["MON-COMMS"]
    assert second.core and second.signals
    assert second.optional["MON-SPAN"] == [("CFG_MSGOUT_UBX_MON_SPAN_USB", 5)]
    assert "MON-COMMS" in second.optional


def test_tmode_helpers() -> None:
    assert tmode_off() == [("CFG_TMODE_MODE", 0)]
    assert tmode_survey_in(300, 2.0) == [
        ("CFG_TMODE_MODE", 1),
        ("CFG_TMODE_SVIN_MIN_DUR", 300),
        ("CFG_TMODE_SVIN_ACC_LIMIT", 20000),
    ]
    fixed = dict(tmode_fixed_ecef(1234.56789, -2.0, 0.0002, 0.0123))
    assert fixed["CFG_TMODE_MODE"] == 2 and fixed["CFG_TMODE_POS_TYPE"] == 0
    assert fixed["CFG_TMODE_ECEF_X"] == 123456 and fixed["CFG_TMODE_ECEF_X_HP"] == 79
    assert fixed["CFG_TMODE_ECEF_Y"] == -200 and fixed["CFG_TMODE_ECEF_Y_HP"] == 0
    assert fixed["CFG_TMODE_ECEF_Z"] == 0 and fixed["CFG_TMODE_ECEF_Z_HP"] == 2
    assert fixed["CFG_TMODE_FIXED_POS_ACC"] == 123


def test_tmode_fixed_ecef_hp_parts_stay_in_range() -> None:
    for value in (0.0, 1.0, -1.0, 1234.56789, -4517590.0123, 0.00009, -0.00009):
        items = dict(tmode_fixed_ecef(value, value, value, 0.01))
        for axis in ("X", "Y", "Z"):
            assert -99 <= items[f"CFG_TMODE_ECEF_{axis}_HP"] <= 99


def test_chunked_splits_at_64() -> None:
    items = [(f"K{i}", i) for i in range(130)]
    parts = chunked(items)
    assert [len(p) for p in parts] == [64, 64, 2]
    assert LAYERS_RAM == 1 and LAYERS_ALL == 7
