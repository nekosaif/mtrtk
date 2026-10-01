import math
import struct
from datetime import date
from typing import Any

import pytest

from mtrtk.rover.drivers.sbg import logs
from mtrtk.rover.drivers.sbg.framer import SbgFramer, encode
from mtrtk.rover.drivers.sbg.ids import CLASS, CMD, LOG

F32 = 1e-6  # f32 fields: compare against the float64 literal with a tolerance
DEG = 1e-5  # ... and f32 radians converted to degrees


def parse(msg_id: int, payload: bytes, msg_class: int = 0) -> Any:
    frame = SbgFramer().feed(encode(msg_class, msg_id, payload))[0]
    return frame.parsed()


def test_ekf_nav() -> None:
    p = struct.pack(
        "<I3f3f3df3fI",
        1000,
        1.0,
        2.0,
        -0.5,
        0.1,
        0.15,
        0.2,
        23.7275,
        90.3925,
        12.5,
        -55.2,
        0.3,
        0.4,
        0.6,
        4 | (1 << 7),
    )
    nav = parse(LOG["EKF_NAV"], p)
    assert isinstance(nav, logs.SbgEkfNav)
    assert nav.lat == 23.7275 and nav.lon == 90.3925 and nav.altitude_msl == 12.5
    assert nav.height_hae == pytest.approx(12.5 - 55.2, abs=F32)
    assert nav.vel_ned == (1.0, 2.0, -0.5)
    assert nav.vel_std_ned == pytest.approx((0.1, 0.15, 0.2), abs=F32)
    assert nav.pos_std == pytest.approx((0.3, 0.4, 0.6), abs=F32)
    assert logs.ekf_mode(nav.status) == 4 and nav.mode == logs.EkfMode.NAV_POSITION
    assert nav.position_valid and not nav.heading_valid and not nav.velocity_valid


def test_ekf_euler_radians_to_degrees_and_optional_tail() -> None:
    p = struct.pack("<I3f3fI", 1, 0.1, -0.2, math.pi / 2, 0.01, 0.01, 0.02, (1 << 4) | (1 << 5) | 4)
    e = parse(LOG["EKF_EULER"], p)
    assert isinstance(e, logs.SbgEkfEuler)
    assert e.roll_deg == pytest.approx(math.degrees(0.1), abs=DEG)
    assert e.pitch_deg == pytest.approx(math.degrees(-0.2), abs=DEG)
    assert e.heading_deg == pytest.approx(90.0, abs=DEG) and e.mag_declination is None
    assert e.attitude_valid and e.heading_valid
    std = (math.degrees(0.01), math.degrees(0.01), math.degrees(0.02))
    assert e.euler_std_deg == pytest.approx(std, abs=DEG)
    tail = parse(LOG["EKF_EULER"], p + struct.pack("<ff", math.radians(-1.5), math.radians(30)))
    assert tail.mag_declination == pytest.approx(-1.5, abs=DEG)
    assert tail.mag_inclination == pytest.approx(30.0, abs=DEG)


def test_ekf_heading_is_wrapped_to_0_360() -> None:
    p = struct.pack("<I3f3fI", 1, 0.0, 0.0, -math.pi / 2, 0.0, 0.0, 0.0, 0)
    assert parse(LOG["EKF_EULER"], p).heading_deg == pytest.approx(270.0, abs=DEG)
    tiny = struct.pack("<I3f3fI", 1, 0.0, 0.0, -1e-38, 0.0, 0.0, 0.0, 0)  # % 360 rounds to 360
    assert parse(LOG["EKF_EULER"], tiny).heading_deg == 0.0


def test_ekf_quat() -> None:
    p = struct.pack("<I4f3fI", 7, 1.0, 0.0, 0.0, 0.0, 0.01, 0.01, 0.02, (1 << 4) | 2)
    q = parse(LOG["EKF_QUAT"], p)
    assert isinstance(q, logs.SbgEkfQuat)
    assert q.quat == (1.0, 0.0, 0.0, 0.0) and q.mode == logs.EkfMode.AHRS and q.attitude_valid
    assert q.mag_declination is None
    std = (math.degrees(0.01), math.degrees(0.01), math.degrees(0.02))
    assert q.euler_std_deg == pytest.approx(std, abs=DEG)


def test_gnss_pos_versions() -> None:
    base = struct.pack(
        "<IIIdddffff", 1, (7 << 6) | 0 | (1 << 12), 1000, 23.0, 90.0, 10.0, -55.0, 0.02, 0.02, 0.05
    )
    short = parse(LOG["GPS1_POS"], base)
    assert isinstance(short, logs.SbgGnssPos) and short.gnss == 1
    assert short.pos_type == 7 and short.num_sv_used is None and short.rtk_fixed
    assert not short.rtk_float and short.solution_computed and short.height_hae == -45.0
    assert logs.gnss_pos_type(short.status) == 7
    assert short.signals == logs.GnssSignals.GPS_L1
    full = parse(
        LOG["GPS1_POS"], base + struct.pack("<BHH", 18, 7, 120) + struct.pack("<BI", 24, 0)
    )
    assert full.num_sv_used == 18 and full.base_station_id == 7 and full.diff_age_s == 1.2
    assert full.num_sv_tracked == 24 and full.status_ext == 0 and full.nr_diag_reboots is None
    na = parse(LOG["GPS1_POS"], base + struct.pack("<BHH", 0xFF, 0xFFFF, 0xFFFF))
    assert na.num_sv_used is None and na.diff_age_s is None and na.base_station_id is None
    v56 = parse(
        LOG["GPS2_POS"],
        base
        + struct.pack("<BHH", 18, 7, 120)
        + struct.pack("<BI", 24, 0)
        + struct.pack("<BI", 2, 99),
    )
    assert v56.gnss == 2 and v56.nr_diag_reboots == 2 and v56.uptime_s == 99
    na45 = parse(
        LOG["GPS1_POS"], base + struct.pack("<BHH", 18, 7, 120) + struct.pack("<BI", 0xFF, 3)
    )
    assert na45.num_sv_tracked is None and na45.status_ext == 3


def test_utc_time_status_decode() -> None:
    p = struct.pack(
        "<IHHbbbbbiI",
        5,
        (3 << 1) | (1 << 5) | (2 << 6),
        2026,
        9,
        19,
        10,
        30,
        15,
        250_000_000,
        37815250,
    )
    t = parse(LOG["UTC_TIME"], p)
    assert isinstance(t, logs.SbgUtcTime)
    assert t.utc is not None and t.utc.isoformat() == "2026-09-19T10:30:15.250000+00:00"
    assert t.clock_state == logs.ClockState.VALID and t.utc_status == logs.UtcStatus.INITIALIZED
    assert t.utc_sync and not t.clock_stable_input and t.gps_tow_ms == 37815250
    assert t.clk_bias_std is None and not t.leap_second_event
    assert t.utc_valid


@pytest.mark.parametrize(
    "status",
    [
        (3 << 1) | (0 << 6),  # clock VALID, UTC INVALID
        (3 << 1) | (1 << 6),  # clock VALID, leap seconds not known
        (1 << 1) | (2 << 6),  # UTC INITIALIZED, clock FREE_RUNNING
        (0 << 1) | (2 << 6),  # ... clock ERROR
    ],
)
def test_utc_not_valid_unless_initialised_and_clock_valid(status: int) -> None:
    """An uninitialised unit sends a well-formed but wrong date: `utc` is set, `utc_valid` not."""
    p = struct.pack("<IHHbbbbbiI", 5, status, 2000, 1, 1, 0, 0, 3, 0, 0)
    t = parse(LOG["UTC_TIME"], p)
    assert t.utc is not None and not t.utc_valid


def test_utc_negative_nanoseconds_clamp_to_zero() -> None:
    p = struct.pack("<IHHbbbbbiI", 5, 0, 2026, 9, 19, 10, 30, 15, -5, 0)
    t = parse(LOG["UTC_TIME"], p)
    assert t.utc is not None and t.utc.microsecond == 0 and t.nanosecond == -5


def test_utc_leap_second_60_clamps() -> None:
    p = struct.pack("<IHHbbbbbiI", 5, 0, 2026, 12, 31, 23, 59, 60, 0, 0)
    t = parse(LOG["UTC_TIME"], p)
    assert t.utc.second == 59 and t.leap_second_event


def test_utc_invalid_date_is_none_not_an_exception() -> None:
    p = struct.pack("<IHHbbbbbiI", 5, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    assert parse(LOG["UTC_TIME"], p).utc is None


def test_gnss_hdt_and_vel() -> None:
    h = parse(
        LOG["GPS1_HDT"],
        struct.pack("<IHIffff", 1, 0 | (1 << 6), 1000, 91.25, 0.2, -1.0, 0.3)
        + struct.pack("<f", 1.02),
    )
    assert isinstance(h, logs.SbgGnssHdt)
    assert h.heading_deg == 91.25 and h.baseline_m == pytest.approx(1.02, abs=F32)
    assert h.heading_acc_deg == pytest.approx(0.2, abs=F32) and h.pitch_deg == -1.0
    assert h.pitch_acc_deg == pytest.approx(0.3, abs=F32)
    assert h.solution_computed and h.baseline_valid and h.num_sv_used is None
    full = parse(
        LOG["GPS1_HDT"],
        struct.pack("<IHIffff", 1, 1, 1000, 91.25, 0.2, -1.0, 0.3)
        + struct.pack("<fBB", 1.0, 20, 14),
    )
    assert full.num_sv_tracked == 20 and full.num_sv_used == 14 and not full.solution_computed
    na = parse(
        LOG["GPS2_HDT"],
        struct.pack("<IHIffff", 1, 1, 1000, 91.25, 0.2, -1.0, 0.3)
        + struct.pack("<fBB", 1.0, 0xFF, 0xFF),
    )
    assert na.gnss == 2 and na.num_sv_tracked is None and na.num_sv_used is None
    vel = struct.pack("<IIIffffffff", 1, 2 << 6, 1000, 1.0, 0.0, 0.0, 0.1, 0.2, 0.3, 0.0, 1.5)
    v = parse(LOG["GPS1_VEL"], vel)
    assert isinstance(v, logs.SbgGnssVel) and v.gnss == 1
    assert v.vel_ned == (1.0, 0.0, 0.0) and v.course_deg == 0.0 and v.vel_type == 2
    assert v.vel_acc == pytest.approx((0.1, 0.2, 0.3), abs=F32) and v.course_acc_deg == 1.5
    assert v.solution_computed
    assert parse(LOG["GPS2_VEL"], vel).gnss == 2


def test_event_offsets() -> None:
    ev = parse(LOG["EVENT_B"], struct.pack("<IHHHHH", 5_000_000, 0b00110, 100, 250, 0, 0))
    assert isinstance(ev, logs.SbgEvent)
    assert ev.channel == "B" and ev.timestamp_us == 5_000_000 and ev.offsets_us == (100, 250)
    assert not ev.overflow
    out = parse(LOG["EVENT_OUT_A"], struct.pack("<IHHHHH", 1, 1, 0, 0, 0, 0))
    assert out.channel == "OUT_A" and out.overflow and out.offsets_us == ()


def test_sat_list_parsing() -> None:
    sat1 = (
        struct.pack("<BbHHB", 12, 45, 180, 5 | (1 << 3) | (1 << 7), 2)
        + struct.pack("<BBB", 14, 5 | (1 << 3) | (1 << 5), 44)
        + struct.pack("<BBB", 18, 3 | (1 << 3) | (1 << 5), 38)
    )
    sat2 = struct.pack("<BbHHB", 3, 10, 90, 3 | (1 << 3) | (2 << 5) | (3 << 7), 1) + struct.pack(
        "<BBB", 60, 3 | (1 << 3), 0
    )
    p = struct.pack("<IIB", 1, 0, 2) + sat1 + sat2
    lst = parse(LOG["GPS1_SAT"], p)
    assert isinstance(lst, logs.SbgSatList) and len(lst.sats) == 2
    g = lst.sats[0]
    assert g.constellation == 1 and g.id == 12 and g.elevation == 45 and g.azimuth == 180 and g.used
    assert g.health == 1 and g.tracking == 5 and g.elevation_status == 0
    assert [s.health for s in g.signals] == [1, 1] and [s.id for s in g.signals] == [14, 18]
    assert lst.sats[1].elevation_status == 2 and lst.sats[1].health == 1
    assert [s.snr for s in g.signals] == [44, 38] and g.signals[0].used and not g.signals[1].used
    assert lst.sats[1].constellation == 3 and lst.sats[1].signals[0].snr is None  # SNR_VALID clear
    assert not lst.sats[1].signals[0].snr_valid
    assert parse(LOG["GPS1_SAT"], p[:-2]) is None  # truncated list: malformed, not an exception
    assert parse(LOG["GPS2_SAT"], p).gnss == 2
    assert isinstance(lst.sats, tuple) and isinstance(g.signals, tuple)
    assert hash(lst) == hash(parse(LOG["GPS1_SAT"], p))  # frozen and hashable all the way down


def test_imu_short_scaling() -> None:
    p = struct.pack("<IH3i3ih", 1, 0, 1048576, 0, -2097152, 67108864, 0, 0, 256 * 25)
    imu = parse(LOG["IMU_SHORT"], p)
    assert isinstance(imu, logs.SbgImuShort)
    assert imu.accel_mps2 == (1.0, 0.0, -2.0) and imu.gyro_radps == (1.0, 0.0, 0.0)
    assert imu.temperature_c == 25.0 and not imu.high_range


def test_imu_short_high_range_gyro_scale() -> None:
    """Status bit 10 (SBG_ECOM_IMU_GYROS_USE_HIGH_SCALE) switches the gyro LSB to 1/12304174."""
    p = struct.pack("<IH3i3ih", 1, 1 << 10, 0, 0, 0, 12304174, 0, 0, 0)
    imu = parse(LOG["IMU_SHORT"], p)
    assert imu.high_range and imu.gyro_radps == (1.0, 0.0, 0.0)


def test_imu_legacy() -> None:
    p = struct.pack("<IH3f3ff3f3f", 2, 0, 0.0, 0.0, -9.75, 0.0, 0.0, 0.5, 31.5, 0, 0, 0, 0, 0, 0)
    imu = parse(LOG["IMU_DATA"], p)
    assert isinstance(imu, logs.SbgImuLegacy)
    assert imu.accel_mps2 == (0.0, 0.0, -9.75) and imu.gyro_radps == (0.0, 0.0, 0.5)
    assert imu.temperature_c == 31.5


def test_status() -> None:
    general = 0b1011111  # everything OK except the datalogger
    p = struct.pack("<IHHIIIH", 9, general, 0, 0, 0b1111, 0, 0)
    s = parse(LOG["STATUS"], p)
    assert isinstance(s, logs.SbgStatus) and s.uptime_s is None and s.cpu_usage is None
    assert s.general & logs.GeneralStatus.MAIN_POWER_OK
    assert not s.general & logs.GeneralStatus.DATALOGGER_OK
    assert s.aiding & logs.AidingStatus.GPS1_HDT_RECV
    full = parse(LOG["STATUS"], p + struct.pack("<IB", 3600, 42))
    assert full.uptime_s == 3600 and full.cpu_usage == 42


def test_ack_and_unknown() -> None:
    ack = SbgFramer().feed(encode(0x10, 0, struct.pack("<BBH", 30, 0x10, 0)))[0].parsed()
    assert isinstance(ack, logs.SbgAck) and ack.msg_id == 30 and ack.ok
    nak = parse(CMD["ACK"], struct.pack("<BBH", 30, 0x10, 4), CLASS["CMD_0"])
    assert not nak.ok and nak.error_code == 4
    assert parse(LOG["MAG"], bytes(10)) is None
    assert parse(5, b"$GPGGA", 0x02) is None


def test_short_payloads_never_raise() -> None:
    for name in ("STATUS", "UTC_TIME", "IMU_SHORT", "EKF_EULER", "EKF_NAV", "GPS1_POS", "GPS1_SAT"):
        assert parse(LOG[name], b"\x01\x02") is None
    assert parse(CMD["ACK"], b"\x01", CLASS["CMD_0"]) is None


def test_info() -> None:
    fw = (1 << 31) | (4 << 28) | (5 << 22) | (3 << 16) | 1234  # software scheme 5.3.1234 stable
    hw = (1 << 24) | (2 << 16) | (0 << 8) | 7  # basic scheme 1.2.0.7
    p = b"ELLIPSE2-D-G4A2-B1".ljust(32, b"\x00") + struct.pack(
        "<IIHBBII", 4100123, 0x01000000, 2024, 3, 14, hw, fw
    )
    info = parse(CMD["INFO"], p, CLASS["CMD_0"])
    assert isinstance(info, logs.SbgInfo)
    assert info.product_code == "ELLIPSE2-D-G4A2-B1" and info.serial_number == 4100123
    assert info.calibration_date == date(2024, 3, 14)
    assert info.hardware_rev == "1.2.0.7" and info.firmware == "5.3.1234-stable"
    assert (
        info.firmware_raw == fw and info.hardware_rev_raw == hw and info.calibration_rev == 1 << 24
    )


def test_version_schemes() -> None:
    assert logs.decode_version((3 << 24) | (1 << 16) | (2 << 8) | 4) == "3.1.2.4"
    assert logs.decode_version((1 << 31) | (2 << 28) | (1 << 22) | (6 << 16) | 9) == "1.6.9-beta"


def test_gnss_raw_is_opaque() -> None:
    raw = parse(LOG["GPS1_RAW"], b"\xb5\x62\x02\x15")
    assert isinstance(raw, logs.SbgGnssRaw) and raw.gnss == 1 and raw.data == b"\xb5\x62\x02\x15"
    assert raw.has_ubx_sync and not hasattr(raw, "is_ubx")
    tail = parse(LOG["GPS2_RAW"], b"\x00\x01\x02\x03")  # a chunk from inside a UBX frame
    assert tail.gnss == 2 and not tail.has_ubx_sync
