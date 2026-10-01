import math
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame, Proto
from mtrtk.core.statestore import gps_from_utc, gps_to_utc
from mtrtk.rover.drivers.sbg.adapter import NOT_UBX_MESSAGE, SbgStateAdapter
from mtrtk.rover.drivers.sbg.framer import SbgFramer

from .helpers import (
    ATT_VALID,
    HDG_VALID,
    NAV_POSITION,
    VEL_VALID,
    ekf_euler,
    ekf_nav,
    event,
    frame,
    gps_pos,
    gps_raw,
    gps_vel,
    hdt,
    imu_short,
    items,
    sat,
    sat_list,
    status_log,
    topics,
    ubx,
    utc,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ins"
T0 = datetime(2026, 9, 19, 10, 0, 0, tzinfo=UTC)
NAV_PVT = ubx(0x01, 0x07, bytes(92))


class FakeWriter:
    """What the adapter needs of `RawLogWriter` / `RawCapture`."""

    def __init__(self) -> None:
        self.utc: list[datetime] = []
        self.written: list[bytes] = []

    def note_utc(self, dt: datetime) -> None:
        self.utc.append(dt)

    def write(self, data: bytes) -> None:
        self.written.append(data)


def test_gps_from_utc_inverts_gps_to_utc() -> None:
    week, tow = gps_from_utc(datetime(2026, 9, 18, 16, 47, 34, tzinfo=UTC), 18)
    assert (week, tow) == (2436, 492472.0)
    t = datetime(2026, 9, 19, 23, 59, 50, 250_000, tzinfo=UTC)  # crosses the week end (+18 s)
    week, tow = gps_from_utc(t, None)
    assert week == 2437 and tow == pytest.approx(8.25)
    assert gps_to_utc(week, tow, 18) == t
    assert gps_from_utc(t.replace(tzinfo=None), 18) == (week, tow)  # naive = UTC


def test_ekf_nav_drives_epoch_and_position() -> None:
    bus = Bus()
    sub = bus.subscribe("state.*")
    a = SbgStateAdapter(bus, nav_hz_cap=5.0)
    a.handle(utc(T0))
    a.handle(ekf_nav(t_mono=10.0))
    s = a.state
    assert s.position.lat == 23.7275 and s.position.lon == 90.3925
    assert s.position.height_m == pytest.approx(12.5 - 55.2, abs=1e-5)
    assert s.position.hmsl_m == 12.5 and not s.position.invalid_llh
    assert s.accuracy.h_acc_m == pytest.approx(0.5, abs=1e-6)  # hypot(0.3, 0.4)
    assert s.accuracy.v_acc_m == pytest.approx(0.6, abs=1e-6)
    assert s.accuracy.s_acc_mps == pytest.approx(math.sqrt(0.01 + 0.04 + 0.04), abs=1e-6)
    assert s.velocity.vel_n_mps == 3.0 and s.velocity.vel_e_mps == 4.0
    assert s.velocity.vel_d_mps == -0.5 and s.velocity.ground_speed_mps == pytest.approx(5.0)
    assert s.velocity.heading_motion_deg == pytest.approx(math.degrees(math.atan2(4, 3)))
    assert s.fix.fix_type == 3 and s.fix.fix_type_name == "3D" and s.fix.gnss_fix_ok
    assert s.time.utc == T0 and s.epoch_count == 1 and s.last_epoch_mono == 10.0
    assert s.ins is not None and s.ins.vendor == "sbg" and s.ins.mode == NAV_POSITION
    assert s.ins.mode_name == "Nav position"
    seen = topics(sub)
    assert seen.count("state.epoch") == 1
    assert seen.index("state.position") < seen.index("state.epoch")  # sections first
    a.handle(ekf_nav(t_mono=10.1))  # inside 1 / nav_hz_cap: counted, not published
    assert a.state.epoch_count == 2 and "state.epoch" not in topics(sub)


def test_ekf_nav_invalid_position_is_not_published_as_a_fix() -> None:
    """An unaligned Ellipse-D (live, indoors) streams EKF_NAV in VERTICAL_GYRO mode with a
    nonsense position (lat -1.6, lon 76.2, 500 km up) and 1000 m/s velocity sigma."""
    a = SbgStateAdapter(Bus())
    a.handle(ekf_nav(-1.59, 76.2, 500049.9, -49.5, 1, pos_std=(1e5, 1e5, 1e5)))
    s = a.state
    assert s.position.lat is None and s.position.lon is None and s.position.invalid_llh
    assert s.velocity.vel_n_mps is None and s.velocity.ground_speed_mps is None
    assert s.fix.fix_type == 0 and not s.fix.gnss_fix_ok and s.accuracy.h_acc_m > 1e4
    assert s.ins is not None and s.ins.mode == 1 and s.epoch_count == 1
    a.handle(ekf_nav(status=VEL_VALID | 3))  # NAV_VELOCITY: 2D per the UBX fix scale
    assert a.state.fix.fix_type == 2 and a.state.fix.fix_type_name == "2D"
    a.handle(ekf_nav())
    a.handle(ekf_nav(0.0, 0.0, 0.0, 0.0, 1))  # validity lost: the last good fix is kept
    assert a.state.position.lat == 23.7275 and a.state.position.invalid_llh


def test_gps_pos_sets_carr_soln_and_rtk() -> None:
    a = SbgStateAdapter(Bus())
    a.handle(gps_pos(7))
    f, r = a.state.fix, a.state.rtk
    assert f.carr_soln == 2 and f.carr_soln_name == "RTK fixed" and f.diff_soln
    assert f.num_sv == 18 and r.carr_soln == 2 and r.carr_soln_name == "RTK fixed"
    assert r.corr_age_receiver_s == 1.2 and r.ref_station_id == 7 and r.diff_soln
    assert a.state.sat_summary.used == 18 and a.state.sat_summary.tracked == 24
    assert a.state.ins is not None and a.state.ins.gnss_fix == 7
    assert a.state.ins.gnss_fix_name == "RTK fixed" and a.rtk_seen
    a.handle(gps_pos(6))
    assert a.state.fix.carr_soln == 1 and a.state.fix.carr_soln_name == "RTK float"
    a.handle(gps_pos(2, diff_age=None))
    f, r = a.state.fix, a.state.rtk
    assert f.carr_soln == 0 and f.carr_soln_name == "None" and not f.diff_soln
    assert r.corr_age_receiver_s is None and not r.diff_soln
    assert a.state.ins is not None and a.state.ins.gnss_fix_name == "Single"
    a.handle(gps_pos(3))  # PSRDIFF: differential, no carrier solution
    assert a.state.fix.diff_soln and a.state.fix.carr_soln == 0


def test_gps_pos_does_not_override_sat_list_summary() -> None:
    a = SbgStateAdapter(Bus())
    a.handle(sat_list(sat(5, 1, [(14, 5, 40)], used=True)))
    a.handle(gps_pos(7, num_sv=18, tracked=24))
    assert a.state.sat_summary.used == 1 and a.state.sat_summary.tracked == 1


def test_gps_vel_goes_to_the_ins_panel_not_velocity() -> None:
    a = SbgStateAdapter(Bus())
    a.handle(ekf_nav())
    a.handle(gps_vel())
    assert a.state.velocity.vel_n_mps == 3.0  # the EKF wins
    assert a.state.ins is not None and a.state.ins.gnss_vel is not None
    assert a.state.ins.gnss_vel.vel_n_mps == 1.0 and a.state.ins.gnss_vel.heading_motion_deg == 45
    assert a.state.ins.gnss_vel.ground_speed_mps == pytest.approx(math.sqrt(2))


def test_attitude_from_ekf_and_fallback_hdt() -> None:
    bus = Bus()
    sub = bus.subscribe("state.attitude")
    a = SbgStateAdapter(bus)
    a.handle(ekf_euler(1.5, -2.0, 10.0, ATT_VALID | 2))
    att = a.state.attitude
    assert att is not None and att.heading_deg is None and att.source == "sbg-ekf"
    assert att.roll_deg == pytest.approx(1.5, abs=1e-5) and att.pitch_deg == pytest.approx(-2.0)
    assert att.acc_roll_deg == pytest.approx(1.0, abs=1e-5) and att.acc_heading_deg is None
    a.handle(hdt(265.5, 1.257))
    att = a.state.attitude
    assert att is not None and att.heading_deg == 265.5 and att.source == "sbg-gnss-hdt"
    assert att.roll_deg == pytest.approx(1.5, abs=1e-5)  # roll/pitch still from the EKF
    assert att.acc_heading_deg == pytest.approx(0.4, abs=1e-6)
    r = a.state.rtk
    assert r.baseline_m == pytest.approx(1.257, abs=1e-6) and r.heading_deg == 265.5
    assert r.heading_valid and r.acc_heading_deg == pytest.approx(0.4, abs=1e-6)
    a.handle(ekf_euler(1.5, -2.0, 10.0, ATT_VALID | 2))  # EKF heading still invalid: HDT kept
    att = a.state.attitude
    assert att is not None and att.heading_deg == 265.5 and att.source == "sbg-gnss-hdt"
    a.handle(ekf_euler(0.5, 0.25, -90.0, ATT_VALID | HDG_VALID | NAV_POSITION))
    att = a.state.attitude
    assert att is not None and att.source == "sbg-ekf"
    assert att.heading_deg == pytest.approx(270.0, abs=1e-4)
    assert att.acc_heading_deg == pytest.approx(2.0, abs=1e-5)
    assert len(topics(sub)) == 4


def test_hdt_fallback_needs_a_fresh_computed_solution() -> None:
    a = SbgStateAdapter(Bus())
    a.handle(hdt(100.0, computed=False))
    assert a.state.attitude is None and not a.state.rtk.heading_valid
    a.handle(hdt(100.0, t_mono=50.0))  # no EKF attitude at all: GNSS heading alone
    att = a.state.attitude
    assert att is not None and att.heading_deg == 100.0 and att.roll_deg is None
    assert att.source == "sbg-gnss-hdt"
    late = ekf_euler(1.0, 1.0, 0.0, ATT_VALID)
    late.t_mono = 53.0  # 3 s after the last HDT: too old to stand in for the EKF heading
    a.handle(late)
    att = a.state.attitude
    assert att is not None and att.heading_deg is None and att.source == "sbg-ekf"
    gone = ekf_euler(0.0, 0.0, 0.0, 0)  # attitude invalid and no fresh HDT
    gone.t_mono = 54.0
    a.handle(gone)
    assert a.state.attitude is None


def test_utc_time_fills_time_and_forwards_to_the_writers() -> None:
    writer, capture = FakeWriter(), FakeWriter()
    a = SbgStateAdapter(Bus(), raw_writer=writer, raw_capture=capture)
    t = datetime(2026, 10, 1, 18, 15, 55, 250_000, tzinfo=UTC)
    a.handle(utc(t))
    ti = a.state.time
    assert ti.utc == t and ti.valid_time and ti.valid_date and ti.fully_resolved and ti.valid_utc
    assert ti.leap_s == 18 and ti.itow_ms == 411373250 and ti.gps_tow_s == 411373.25
    assert ti.gps_week == 2438  # GPS week 2438 starts on Sunday 2026-09-27
    assert writer.utc == [t] and capture.utc == [t]
    unsynced = utc(datetime(2000, 1, 1, 0, 0, 3, tzinfo=UTC), status=1 << 1)  # FREE_RUNNING
    a.handle(unsynced)
    ti = a.state.time
    assert ti.utc == t and not ti.valid_time and not ti.fully_resolved and not ti.valid_utc
    assert writer.utc == [t]  # an unsynced clock never names a raw-log hour


def test_events_become_time_marks() -> None:
    bus = Bus()
    sub = bus.subscribe("state.time_mark")
    a = SbgStateAdapter(bus)
    a.handle(utc(T0, ts=1_000_000))
    a.handle(event("B", 1_500_000, [100, 250]))
    marks = a.state.time_marks
    assert [m.rising_utc for m in marks] == [
        T0 + timedelta(microseconds=500_000),
        T0 + timedelta(microseconds=500_100),
        T0 + timedelta(microseconds=500_250),
    ]
    assert {m.channel for m in marks} == {1} and [m.count for m in marks] == [1, 2, 3]
    assert all(m.new_rising and m.utc_based and m.time_base == 1 for m in marks)
    week, tow = gps_from_utc(T0 + timedelta(microseconds=500_000), 18)
    assert marks[0].rising_week == week and marks[0].rising_tow_s == pytest.approx(tow)
    assert len(topics(sub)) == 3
    a.handle(event("A", 900_000))  # before the anchor
    assert a.state.time_marks[-1].channel == 0 and a.state.time_marks[-1].count == 1
    assert a.state.time_marks[-1].rising_utc == T0 - timedelta(microseconds=100_000)


def test_event_timestamps_wrap_at_32_bits() -> None:
    a = SbgStateAdapter(Bus())
    a.handle(utc(T0, ts=0xFFFF_FF00))
    a.handle(event("C", 0x0000_0100))  # 512 us later, after the u32 microsecond counter wrapped
    assert a.state.time_marks[-1].rising_utc == T0 + timedelta(microseconds=512)


def test_events_before_the_first_utc_are_held_then_dated() -> None:
    bus = Bus()
    sub = bus.subscribe("state.time_mark")
    a = SbgStateAdapter(bus)
    a.handle(event("E", 2_000_000))
    assert a.state.time_marks == [] and topics(sub) == []
    a.handle(utc(T0, ts=3_000_000))
    (mark,) = a.state.time_marks
    assert mark.channel == 4 and mark.rising_utc == T0 - timedelta(seconds=1)
    assert len(topics(sub)) == 1


def test_held_events_are_bounded() -> None:
    a = SbgStateAdapter(Bus())
    for i in range(150):
        a.handle(event("A", 10 + i))
    a.handle(utc(T0, ts=0))
    marks = a.state.time_marks
    assert len(marks) == 100 and marks[0].count == 51 and marks[-1].count == 150


def test_sat_list_maps_to_satellites() -> None:
    bus = Bus()
    sub = bus.subscribe("state.*")
    a = SbgStateAdapter(bus)
    gps = sat(12, 1, [(14, 5, 44), (19, 3, 38)], used=True)
    gal = sat(3, 3, [(61, 3, None)], used=False)
    a.handle(sat_list(gps, gal))
    s = a.state
    assert len(s.sats) == 2 and a.sats_seen
    g, e = s.sats
    assert (g.gnss_id, g.gnss, g.sv_id) == (0, "GPS", 12) and g.used and g.cno == 44
    assert g.elev == 45 and g.azim == 180 and g.health == 1
    assert [sig.sig_id for sig in g.signals] == [14, 19] and g.signals[0].cno == 44
    assert g.signals[0].name == "L1 C/A" and g.signals[1].name == "L2C L"
    assert g.signals[0].pr_used and not g.signals[1].pr_used
    assert (e.gnss_id, e.gnss) == (2, "Galileo") and not e.used and e.signals[0].cno == 0
    assert e.signals[0].name == "E1 C"
    assert s.sat_summary.used == 1 and s.sat_summary.tracked == 2
    assert s.sat_summary.per_gnss == {
        "GPS": {"tracked": 1, "used": 1},
        "Galileo": {"tracked": 1, "used": 0},
    }
    assert {"state.sats", "state.sat_summary"} <= set(topics(sub))


def test_sat_list_constellation_mapping() -> None:
    a = SbgStateAdapter(Bus())
    a.handle(
        sat_list(
            sat(8, 2, [(41, 5, 40)], used=True),  # GLONASS slot
            sat(23, 4, [(101, 5, 40)], used=True),  # BeiDou
            sat(2, 5, [(153, 5, 40)], used=True),  # QZSS
            sat(27, 6, [(180, 5, 40)], used=True),  # SBAS (GAGAN PRN 127 on the bench unit)
            sat(5, 8, [(220, 5, 40)], used=False),  # L-band: no u-blox gnssId, dropped
        )
    )
    got = [(x.gnss_id, x.gnss, x.sv_id) for x in a.state.sats]
    assert got == [(1, "SBAS", 127), (3, "BeiDou", 23), (5, "QZSS", 2), (6, "GLONASS", 8)]


def test_gps_raw_reframed_to_ubx_and_time_forwarded() -> None:
    bus = Bus()
    sub = bus.subscribe("raw.ubx", "ubx.*", "receiver.error")
    writer = FakeWriter()
    a = SbgStateAdapter(bus, raw_writer=writer)
    sfrbx = ubx(0x02, 0x13, bytes(40))
    # The unit cuts the receiver stream without regard to UBX framing: the first chunk starts
    # inside a frame, and NAV-PVT is split across two GPS1_RAW payloads.
    a.handle(gps_raw(sfrbx[7:] + NAV_PVT[:30]))
    assert a.raw_gnss_format == "unknown-yet" and items(sub) == []
    a.handle(gps_raw(NAV_PVT[30:] + sfrbx))
    got = items(sub)
    assert [t for t, _ in got] == ["raw.ubx", "ubx.NAV-PVT", "raw.ubx", "ubx.RXM-SFRBX"]
    pvt = got[0][1]
    assert isinstance(pvt, Frame) and pvt.proto is Proto.UBX and pvt.raw == NAV_PVT
    assert a.raw_gnss_format == "ubx"
    a.handle(gps_raw(ubx(0x02, 0x15, bytes(16))))
    assert [t for t, _ in items(sub)] == ["raw.ubx", "ubx.RXM-RAWX"] and a.state.raw_epochs == 1
    a.handle(utc(T0))
    assert writer.utc == [T0]


def test_gps_raw_not_ubx_disables_capture() -> None:
    bus = Bus()
    sub = bus.subscribe("receiver.error", "raw.*")
    capture = FakeWriter()
    a = SbgStateAdapter(bus, raw_capture=capture)
    nmea = b"$GNGGA,103015.25,2343.65,N,09023.55,E,1,12,0.8,12.5,M,-55.2,M,,*4B\r\n" * 130
    chunks = [nmea[i : i + 1000] for i in range(0, len(nmea), 1000)]
    for chunk in chunks[:3]:
        a.handle(gps_raw(chunk))
    assert a.raw_gnss_format == "unknown-yet" and capture.written == []  # still deciding
    for chunk in chunks[3:]:
        a.handle(gps_raw(chunk))
    assert a.raw_gnss_format == "unknown"
    assert items(sub) == [("receiver.error", NOT_UBX_MESSAGE)]
    a.handle(gps_raw(NAV_PVT))  # decided once per stream: no more re-framing
    assert items(sub) == []
    assert b"".join(capture.written) == nmea + NAV_PVT  # nothing lost to the opaque capture


def test_gps_raw_ignored_when_raw_gnss_disabled() -> None:
    bus = Bus()
    sub = bus.subscribe("raw.ubx", "receiver.error")
    capture = FakeWriter()
    a = SbgStateAdapter(bus, raw_capture=capture, raw_gnss=False)
    a.handle(gps_raw(NAV_PVT))
    assert items(sub) == [] and capture.written == [] and a.raw_gnss_format == "unknown-yet"


def test_status_and_imu_sections() -> None:
    bus = Bus()
    sub = bus.subscribe("state.ins", "state.imu")
    a = SbgStateAdapter(bus)
    a.handle(status_log(general=0x7F & ~(1 << 5), aiding=0b1011))
    ins = a.state.ins
    assert ins is not None and ins.general_ok["main_power"] is True
    assert ins.general_ok["datalogger"] is False and ins.general_ok["cpu"] is True
    assert ins.aiding["gps1_pos"] and not ins.aiding["gps1_hdt"] and ins.aiding["gps1_utc"]
    assert ins.uptime_s == 3600 and ins.cpu_pct == 42 and ins.com_status == 97
    assert topics(sub) == ["state.ins"]
    for i in range(50):  # 500 Hz for 0.1 s
        a.handle(imu_short(ts=1_000_000 + i * 2000, t_mono=100.0 + i * 0.002))
    imu = a.state.imu
    assert imu is not None and imu.timestamp_us == 1_000_000 + 49 * 2000
    assert imu.accel_mps2 == (1.0, 0.0, -2.0) and imu.gyro_radps == (1.0, 0.0, 0.0)
    assert imu.temperature_c == 25.0
    assert 1 <= topics(sub).count("state.imu") <= 2


def test_imu_data_legacy_log() -> None:
    a = SbgStateAdapter(Bus())
    p = struct.pack("<IH3f3ff3f3f", 7, 0, 0.0, 0.0, -9.75, 0.0, 0.0, 0.5, 31.5, *[0.0] * 6)
    a.handle(frame("IMU_DATA", p))
    assert a.state.imu is not None and a.state.imu.accel_mps2 == (0.0, 0.0, -9.75)
    assert a.state.imu.temperature_c == 31.5 and a.state.imu.timestamp_us == 7


def test_malformed_and_unknown_frames_are_ignored() -> None:
    a = SbgStateAdapter(Bus())
    a.handle(frame("EKF_NAV", b"\x00\x01"))  # truncated: parser returns None
    a.handle(frame("MAG", bytes(30)))  # not mapped
    assert a.state.epoch_count == 0 and a.state.position.lat is None


def test_live_ellipse_d_slice_maps_to_state() -> None:
    """The committed 1 s capture of the bench Ellipse-D: EKF in VERTICAL_GYRO (indoors, not
    aligned), GNSS solving with a valid dual-antenna heading."""
    bus = Bus()
    sub = bus.subscribe("state.epoch")
    a = SbgStateAdapter(bus, nav_hz_cap=5.0)
    for fr in SbgFramer().feed((FIXTURES / "ellipse_d_live_1s.sbg").read_bytes()):
        a.handle(fr)
    s = a.state
    assert s.epoch_count == 50 and 1 <= len(topics(sub)) <= 50
    assert s.time.utc is not None and s.time.utc.year == 2026 and s.time.leap_s == 18
    assert s.ins is not None and s.ins.general_ok and all(s.ins.general_ok.values())
    assert s.sats and a.sats_seen and s.imu is not None
    assert s.attitude is not None and s.attitude.roll_deg is not None
    if s.ins.mode is not None and s.ins.mode < NAV_POSITION:
        assert s.position.invalid_llh and s.fix.fix_type in (0, 2)
    assert s.fix.num_sv > 0 and s.ins.gnss_fix is not None
