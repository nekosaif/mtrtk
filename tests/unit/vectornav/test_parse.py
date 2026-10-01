from datetime import UTC, datetime

import pytest

from mtrtk.rover.drivers.vectornav.checksum import finalize_ascii
from mtrtk.rover.drivers.vectornav.parse import VnAscii, VnBinary, VnSat, parse

from .helpers import (
    ATTITUDE,
    GPS,
    IMU,
    INS,
    TIME,
    att_payload,
    binary,
    frame,
    gps_payload,
    imu_payload,
    ins_payload,
    ins_status,
    raw_meas,
    time_group_payload,
)


def _bin(raw: bytes) -> VnBinary:
    parsed = parse(frame(raw))
    assert isinstance(parsed, VnBinary)
    return parsed


def test_time_group() -> None:
    p = _bin(binary((TIME, time_group_payload(gps_tow_ns=345_600_250_000_000, gps_week=2385))))
    t = p.groups["time"]
    assert t["utc"] == datetime(2026, 9, 19, 10, 30, 15, 250000, tzinfo=UTC)
    assert t["gps_week"] == 2385
    assert t["gps_tow"] == 345_600_250_000_000 and t["gps_tow_s"] == 345_600_250_000_000 / 1e9
    assert t["time_status"] == {"time_ok": True, "date_ok": True, "utc_time_valid": True}
    assert t["sync_in_cnt"] == 0 and t["time_sync_in"] == 0


def test_time_group_invalid_utc_is_none() -> None:
    p = _bin(binary((TIME, time_group_payload(utc=(0, 0, 0, 0, 0, 0, 0), time_status=0))))
    assert p.groups["time"]["utc"] is None
    assert p.groups["time"]["time_status"]["utc_time_valid"] is False


def test_gps_group_with_satinfo_and_dop() -> None:
    p = _bin(binary((GPS, gps_payload(fix=3, num_sats=12))))
    g = p.groups["gps"]
    assert g["fix"] == 3 and g["num_sats"] == 12
    assert g["pos_lla"] == pytest.approx((23.78, 90.40, 12.0))
    assert g["pos_u"] == pytest.approx((1.5, 1.6, 3.0))
    assert g["dop"] == pytest.approx((1.9, 1.7, 0.9, 1.4, 0.8, 0.6, 0.5))
    assert g["time_info"] == {"status": 0x07, "leap_secs": 18}
    assert g["tow_s"] == pytest.approx(123_456.0)
    sats = g["sat_info"]
    assert len(sats) == 2 and all(isinstance(s, VnSat) for s in sats)
    gps5, gal11 = sats
    assert (gps5.sys, gps5.sv_id, gps5.cno, gps5.used, gps5.el, gps5.az) == (
        0,
        5,
        44,
        True,
        45,
        120,
    )
    assert (gal11.sys, gal11.sv_id, gal11.used, gal11.az) == (2, 11, False, -170)
    assert gal11.healthy and gal11.az_el_valid and not gal11.diff_corr


def test_ins_and_attitude_groups() -> None:
    p = _bin(binary((ATTITUDE, att_payload()), (INS, ins_payload())))
    ins = p.groups["ins"]
    st = ins["ins_status"]
    assert st.mode == 2 and st.gps_fix and not st.imu_error and st.raw == ins_status()
    assert ins["pos_lla"] == pytest.approx((23.7806, 90.4071, 12.5))
    assert ins["pos_u"] == pytest.approx(0.02) and ins["vel_u"] == pytest.approx(0.05)
    assert ins["vel_ned"] == pytest.approx((1.0, 0.5, -0.1))
    att = p.groups["attitude"]
    assert att["ypr"] == pytest.approx((91.0, -1.0, 0.5))
    assert att["ypr_u"] == pytest.approx((0.8, 0.1, 0.12))


def test_ins_status_error_bits() -> None:
    raw = ins_status(mode=1, gps_fix=False, imu_error=True, gps_error=True) | 0x0300
    p = _bin(binary((INS, ins_payload(status=raw))))
    st = p.groups["ins"]["ins_status"]
    assert st.mode == 1 and not st.gps_fix and st.imu_error and st.gps_error
    assert not st.time_error and not st.mag_pres_error
    assert st.gps_heading_ins and st.gps_compass


def test_imu_group() -> None:
    p = _bin(binary((IMU, imu_payload())))
    imu = p.groups["imu"]
    assert imu["temp"] == pytest.approx(31.5)
    assert imu["accel"] == pytest.approx((0.1, -0.2, -9.81))
    assert imu["angular_rate"] == pytest.approx((0.001, 0.002, -0.003))


def test_raw_meas_kept_as_bytes() -> None:
    p = _bin(binary((GPS, gps_payload(meas=2))))
    assert p.groups["gps"]["raw_meas"] == raw_meas(2)
    assert p.groups["gps"]["raw_meas_count"] == 2


def test_unnamed_field_kept_raw() -> None:
    """INS bit 11 has a size (68) but no decoder: its bytes survive, the frame still parses."""
    raw = binary((INS, (0x0800, bytes(range(68)))))
    p = _bin(raw)
    assert p.groups["ins"]["bit11"] == bytes(range(68))


def test_ascii_parse() -> None:
    echo = parse(frame(finalize_ascii("VNWRG,75,1,80,3A,2DE,FABA,1")))
    assert isinstance(echo, VnAscii)
    assert echo.cmd == "WRG" and echo.register == 75
    assert echo.fields == ["1", "80", "3A", "2DE", "FABA", "1"] and echo.error is None
    err = parse(frame(finalize_ascii("VNERR,08")))
    assert isinstance(err, VnAscii) and err.cmd == "ERR" and err.error == 8
    rrg = parse(frame(finalize_ascii("VNRRG,01,VN-200T-CR")))
    assert isinstance(rrg, VnAscii) and rrg.register == 1 and rrg.fields == ["VN-200T-CR"]
    wnv = parse(frame(finalize_ascii("VNWNV")))
    assert isinstance(wnv, VnAscii) and wnv.cmd == "WNV" and wnv.fields == []
    hexerr = parse(frame(finalize_ascii("VNERR,0C")))
    assert isinstance(hexerr, VnAscii) and hexerr.error == 12


def test_parse_garbage_returns_none() -> None:
    assert parse(frame(b"\xfa\x80\x00\x00")) is None
    assert parse(frame(b"$VNRRG,xx*00\r\n")) is None
