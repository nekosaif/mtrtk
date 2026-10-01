import pytest
from pydantic import ValidationError

from mtrtk.config import Settings


def make(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setenv("NTRIP_PASSWORD", "secret")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_ins_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make(monkeypatch)
    assert s.ins_port is None and s.ins_baud == 115200 and s.ins_rtcm_port is None
    assert s.ins_output_hz == 10 and s.ins_apply_config is False and s.ins_raw_gnss is True
    assert s.ins_lever_arm_gnss1 is None and s.ins_lever_arm_gnss2 is None
    assert s.ins_imu_lever_arm is None and s.ins_init_position is None
    assert s.ins_imu_axis == "xyz" and s.ins_motion_profile == "general"


def test_ins_driver_requires_a_port(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="INS_PORT is required for ROVER_DRIVER=sbg_ellipse"):
        make(monkeypatch, ROLE="rover", ROVER_DRIVER="sbg_ellipse")
    with pytest.raises(ValidationError, match="INS_PORT is required for ROVER_DRIVER=vectornav"):
        make(monkeypatch, ROLE="rover", ROVER_DRIVER="vectornav", INS_PORT="")
    s = make(
        monkeypatch,
        ROLE="rover",
        ROVER_DRIVER="sbg_ellipse",
        INS_PORT="/dev/ttyUSB0",
        INS_BAUD="921600",
    )
    assert s.ins_port == "/dev/ttyUSB0" and s.ins_baud == 921600


def test_ins_vectors_parse_from_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make(
        monkeypatch,
        INS_LEVER_ARM_GNSS1="0.10, -0.25,1.5",
        INS_LEVER_ARM_GNSS2="",
        INS_IMU_LEVER_ARM="0,0,0",
        INS_INIT_POSITION="23.78,90.41,12",
    )
    assert s.ins_lever_arm_gnss1 == (0.10, -0.25, 1.5)
    assert s.ins_lever_arm_gnss2 is None
    assert s.ins_imu_lever_arm == (0.0, 0.0, 0.0)
    assert s.ins_init_position == (23.78, 90.41, 12.0)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("INS_LEVER_ARM_GNSS1", "1,2"),
        ("INS_LEVER_ARM_GNSS1", "1,2,x"),
        ("INS_LEVER_ARM_GNSS1", "1,nan,3"),
        ("INS_INIT_POSITION", "91,0,0"),
        ("INS_INIT_POSITION", "0,181,0"),
        ("INS_OUTPUT_HZ", "0"),
        ("INS_OUTPUT_HZ", "201"),
        ("INS_MOTION_PROFILE", "submarine"),
        ("INS_BAUD", "0"),
    ],
)
def test_ins_rejects_bad_values(monkeypatch: pytest.MonkeyPatch, key: str, value: str) -> None:
    with pytest.raises(ValidationError):
        make(monkeypatch, **{key: value})


def test_ins_vectors_survive_a_settings_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PUT /api/config` re-validates `model_dump()`: a parsed tuple must load back as itself."""
    from mtrtk.web.envfile import to_env_value

    s = make(monkeypatch, INS_LEVER_ARM_GNSS1="0.1,0.2,0.3")
    again = Settings(_env_file=None, **s.model_dump())  # type: ignore[call-arg]
    assert again.ins_lever_arm_gnss1 == (0.1, 0.2, 0.3)
    assert to_env_value(s.ins_lever_arm_gnss1) == "0.1,0.2,0.3"
    assert make(monkeypatch, INS_LEVER_ARM_GNSS1="0.1,0.2,0.3").ins_lever_arm_gnss1 == (
        0.1,
        0.2,
        0.3,
    )
