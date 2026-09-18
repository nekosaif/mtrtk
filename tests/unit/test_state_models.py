from mtrtk.core.state import (
    FIX_TYPE_NAMES,
    GNSS_NAMES,
    ReceiverState,
    Satellite,
    Signal,
    signal_name,
)


def test_default_state_is_empty_but_valid() -> None:
    s = ReceiverState()
    assert s.connected is False
    assert s.position.lat is None
    assert s.fix.fix_type_name == "No fix"
    assert s.sats == []
    assert s.rtcm_out.total_bytes == 0
    assert s.model_dump()["dops"]["p"] is None


def test_name_tables() -> None:
    assert GNSS_NAMES[0] == "GPS" and GNSS_NAMES[6] == "GLONASS"
    assert FIX_TYPE_NAMES[5] == "Time only"
    assert signal_name(0, 3) == "L2CL"
    assert signal_name(2, 5) == "E5bI"
    assert signal_name(9, 9) == "sig9"


def test_satellite_key_and_json_roundtrip() -> None:
    sat = Satellite(
        gnss_id=0,
        gnss="GPS",
        sv_id=5,
        cno=40,
        used=True,
        signals=[Signal(sig_id=0, name="L1C/A", cno=40)],
    )
    assert sat.key == (0, 5)
    assert Satellite.model_validate_json(sat.model_dump_json()) == sat
