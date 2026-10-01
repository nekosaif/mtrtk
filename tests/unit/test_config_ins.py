import pytest
from pydantic import ValidationError

from mtrtk.config import Settings


def make(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setenv("NTRIP_PASSWORD", "secret")
    # A bench shell may export INS_BAUD=921600 or ROVER_DRIVER=sbg_ellipse: the defaults
    # under test must come from the code, not from the developer's environment.
    for key in [
        "ROLE",
        "ROVER_DRIVER",
        *(f.upper() for f in Settings.model_fields if f.startswith("ins_")),
    ]:
        monkeypatch.delenv(key, raising=False)
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
        ("INS_BAUD", "4000001"),
    ],
)
def test_ins_rejects_bad_values(monkeypatch: pytest.MonkeyPatch, key: str, value: str) -> None:
    with pytest.raises(ValidationError, match=key.lower()):  # rejected for this field
        make(monkeypatch, **{key: value})


def test_ins_baud_accepts_its_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(monkeypatch, INS_BAUD="1200").ins_baud == 1200
    assert make(monkeypatch, INS_BAUD="4000000").ins_baud == 4_000_000


def test_ins_defaults_ignore_a_bench_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INS_BAUD", "921600")
    monkeypatch.setenv("ROVER_DRIVER", "sbg_ellipse")
    s = make(monkeypatch)
    assert s.ins_baud == 115200 and s.rover_driver == "ublox"


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


def test_env_example_leaves_the_units_motion_profile_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A .env copied from .env.example must not make the SBG driver write a motion profile:
    `INS_MOTION_PROFILE` is managed only when it is set, so the template leaves it unset."""
    from pathlib import Path

    from mtrtk.rover.drivers.sbg.config import sbg_profile

    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    template = Path(__file__).resolve().parents[2] / ".env.example"
    s = Settings(
        _env_file=template,
        ntrip_password="secret",
        role="rover",
        rover_driver="sbg_ellipse",
        ins_port="/dev/ttyUSB0",
    )
    assert s.ins_motion_profile == "general"
    assert sbg_profile(s).motion_profile is None


def test_an_ins_driver_replaying_a_file_needs_no_ins_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """`MTRTK_SOURCE=file:...` replays a capture through the INS stack: no unit, no port."""
    monkeypatch.setenv("MTRTK_SOURCE", "file:tests/fixtures/ins/sbg_frames.bin")
    s = make(monkeypatch, ROLE="rover", ROVER_DRIVER="sbg_ellipse")
    assert s.ins_port is None and s.source_is_file
    monkeypatch.setenv("MTRTK_SOURCE", "auto")
    with pytest.raises(ValidationError, match="INS_PORT is required"):
        make(monkeypatch, ROLE="rover", ROVER_DRIVER="vectornav")
