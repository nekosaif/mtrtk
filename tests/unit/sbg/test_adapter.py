import logging
import math
import struct
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame, Proto
from mtrtk.core.state import ReceiverState
from mtrtk.core.statestore import StateStore, gps_from_utc, gps_to_utc
from mtrtk.rover.drivers.sbg.adapter import NOT_UBX_MESSAGE, SbgStateAdapter
from mtrtk.rover.drivers.sbg.framer import SbgFramer
from mtrtk.rover.nmea_out import build_gga
from mtrtk.rover.points import PointCollector, PointsRepo
from mtrtk.rover.sessions import SessionsRepo
from mtrtk.store.db import Database

from .helpers import (
    ATT_VALID,
    HDG_VALID,
    NAV_POSITION,
    POS_VALID,
    TS,
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
    rtcm3,
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
FREE_RUNNING_UTC = (1 << 1) | (2 << 6)  # clock FREE_RUNNING (no PPS), UTC INITIALIZED
LOG = "mtrtk.rover.drivers.sbg.adapter"


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
    assert a.state.epoch_count == 2 and topics(sub) == []  # nor are its sections


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
    # The dual-antenna heading and antenna separation are the INS's, never the rover-to-base
    # vector that `rtk.baseline_m`/`heading_deg` mean (NAV-RELPOSNED) on the RTK page and tape.
    ins = a.state.ins
    assert ins is not None
    assert ins.antenna_baseline_m == pytest.approx(1.257, abs=1e-6)
    assert ins.gnss_heading_deg == 265.5 and ins.gnss_heading_valid
    assert ins.gnss_heading_acc_deg == pytest.approx(0.4, abs=1e-6)
    r = a.state.rtk
    assert r.baseline_m is None and r.heading_deg is None and not r.heading_valid
    a.handle(ekf_euler(1.5, -2.0, 10.0, ATT_VALID | 2))  # EKF heading still invalid: HDT kept
    att = a.state.attitude
    assert att is not None and att.heading_deg == 265.5 and att.source == "sbg-gnss-hdt"
    a.handle(ekf_euler(0.5, 0.25, -90.0, ATT_VALID | HDG_VALID | NAV_POSITION))
    att = a.state.attitude
    assert att is not None and att.source == "sbg-ekf"
    assert att.heading_deg == pytest.approx(270.0, abs=1e-4)
    assert att.acc_heading_deg == pytest.approx(2.0, abs=1e-5)
    # All at one instant: the repeat EKF_EULER (same source, same heading validity) is inside
    # the 1 / ATTITUDE_PUBLISH_HZ window and only updates the state; each change publishes.
    assert len(topics(sub)) == 3


def test_attitude_publish_is_decimated_but_changes_go_out_at_once() -> None:
    bus = Bus()
    sub = bus.subscribe("state.attitude")
    a = SbgStateAdapter(bus)
    for i in range(50):  # EKF_EULER at 200 Hz for 0.25 s
        a.handle(ekf_euler(float(i), 0.0, 0.0, ATT_VALID | 2, t_mono=100.0 + i * 0.005))
    published = [att for _, att in items(sub)]
    assert len(published) == 3  # 100.0, 100.1, 100.2: at most ATTITUDE_PUBLISH_HZ
    assert a.state.attitude is not None and a.state.attitude.roll_deg == pytest.approx(49.0)
    a.handle(ekf_euler(1.0, 0.0, 0.0, ATT_VALID | HDG_VALID | 2, t_mono=100.251))
    (att,) = [att for _, att in items(sub)]  # the heading became valid: published at once
    assert att.heading_deg is not None and att.source == "sbg-ekf"
    a.handle(ekf_euler(0.0, 0.0, 0.0, 0, t_mono=100.252))  # attitude lost: published at once
    assert items(sub) == [("state.attitude", None)] and a.state.attitude is None


def test_stale_ekf_roll_pitch_are_not_reused_next_to_a_gnss_heading() -> None:
    a = SbgStateAdapter(Bus())
    a.handle(ekf_euler(3.0, 4.0, 0.0, ATT_VALID | 2, t_mono=10.0))
    a.handle(hdt(120.0, t_mono=11.0))  # EULER 1 s old: roll / pitch still shown with it
    att = a.state.attitude
    assert att is not None and att.roll_deg == pytest.approx(3.0) and att.heading_deg == 120.0
    a.handle(hdt(121.0, t_mono=13.0))  # EULER 3 s old (the EKF log stopped): heading alone
    att = a.state.attitude
    assert att is not None and att.roll_deg is None and att.pitch_deg is None
    assert att.heading_deg == 121.0 and att.source == "sbg-gnss-hdt"


def test_hdt_fallback_needs_a_fresh_computed_solution() -> None:
    a = SbgStateAdapter(Bus())
    a.handle(hdt(100.0, computed=False))
    assert a.state.attitude is None and not (a.state.ins and a.state.ins.gnss_heading_valid)
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
    # The opaque capture is kept off the unit's clock until GPS1_RAW proves not to be UBX: on
    # a UBX stream it would open an empty hour file (and sidecar) at every hour.
    assert writer.utc == [t] and capture.utc == []
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


def test_events_wait_for_a_valid_utc_after_an_invalid_one() -> None:
    a = SbgStateAdapter(Bus())
    a.handle(utc(T0, ts=1_000_000))
    a.handle(utc(T0, ts=2_000_000, status=1 << 1))  # clock FREE_RUNNING: no anchor any more
    a.handle(event("A", 2_500_000))
    assert a.state.time_marks == []
    a.handle(utc(T0 + timedelta(seconds=2), ts=3_000_000))
    (mark,) = a.state.time_marks
    assert mark.rising_utc == T0 + timedelta(seconds=1.5)


def test_events_far_from_the_anchor_wait_for_a_fresh_one() -> None:
    a = SbgStateAdapter(Bus())
    a.handle(utc(T0, ts=1_000_000))
    for s in range(2, 63):  # the stream carries on, UTC_TIME does not (log disabled, say)
        a.handle(imu_short(ts=s * 1_000_000, t_mono=float(s)))
    a.handle(event("D", 62_000_000))  # 61 s after the anchor: held, not extrapolated
    assert a.state.time_marks == []
    a.handle(utc(T0 + timedelta(seconds=61.5), ts=62_500_000))
    (mark,) = a.state.time_marks
    assert mark.channel == 3 and mark.rising_utc == T0 + timedelta(seconds=61)


def test_a_device_time_stamp_jump_drops_the_anchor_and_older_held_marks() -> None:
    """A unit reboot restarts the device time stamp near 0 while UTC is still being acquired:
    an anchor (or a held mark) from before it must not date events after it."""
    a = SbgStateAdapter(Bus())
    a.handle(event("B", 99_000_000))  # held: no UTC yet
    a.handle(utc(T0, ts=100_000_000))  # same timeline: dated
    assert a.state.time_marks[0].rising_utc == T0 - timedelta(seconds=1)
    a.handle(event("C", 100_100_000))  # dated from the anchor
    assert len(a.state.time_marks) == 2
    a.handle(imu_short(ts=2_000_000))  # reboot: the device time stamp restarted
    a.handle(event("A", 2_500_000))  # would be T0 - 97.5 s from the stale anchor: held
    a.handle(event("A", 1_000))  # (behind the stream: not a jump of its own)
    assert len(a.state.time_marks) == 2
    a.handle(utc(T0 + timedelta(seconds=60), ts=3_000_000))
    late = a.state.time_marks[2:]
    assert [m.rising_utc for m in late] == [
        T0 + timedelta(seconds=59.5),
        T0 + timedelta(seconds=60) - timedelta(microseconds=2_999_000),
    ]
    b = SbgStateAdapter(Bus())
    b.handle(event("E", 2_000_000))  # held on the first timeline
    b.handle(imu_short(ts=500_000_000))  # 498 s jump: a new timeline
    b.handle(utc(T0, ts=500_000_000))
    assert b.state.time_marks == []  # no anchor on its timeline: dropped, not misdated


def test_leap_second_event_keeps_the_previous_leap_seconds() -> None:
    a = SbgStateAdapter(Bus())
    before = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
    a.handle(utc(before))
    assert a.state.time.leap_s == 18
    # 23:59:60: the log clamps the second to 59 while its GPS time of week is one second on.
    sow = (before.isoweekday() % 7) * 86400 + 23 * 3600 + 59 * 60 + 60
    a.handle(utc(before, second=60, gps_tow_ms=((sow + 18) % 604800) * 1000))
    assert a.state.time.leap_s == 18 and a.state.time.utc == before


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
    # core.state.SIGNAL_NAMES spelling where the signal has a u-blox equivalent
    assert g.signals[0].name == "L1C/A" and g.signals[1].name == "L2CL"
    assert g.signals[0].pr_used and not g.signals[1].pr_used
    assert (e.gnss_id, e.gnss) == (2, "Galileo") and not e.used and e.signals[0].cno == 0
    assert e.signals[0].name == "E1C"
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
    # Valid checksums: the core Framer yields these as NMEA (and RTCM3) frames, which must
    # not count as UBX. A bad checksum would be dropped and decide nothing.
    gga = b"$GNGGA,103015.25,2343.65,N,09023.55,E,1,12,0.8,12.5,M,-55.2,M,,*6C\r\n"
    nmea = (gga * 65 + rtcm3(1005)) * 2
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


def test_not_ubx_stream_names_the_capture_hour_from_the_units_utc() -> None:
    capture = FakeWriter()
    a = SbgStateAdapter(Bus(), raw_capture=capture)
    a.handle(utc(T0, ts=1_000_000))
    assert capture.utc == []
    nmea = b"$GNGGA,,,,,,0,00,,,M,,M,,*78\r\n" * 300
    for i in range(0, len(nmea), 1000):
        a.handle(gps_raw(nmea[i : i + 1000]))
    assert a.raw_gnss_format == "unknown" and capture.utc == [T0]  # at once, from the anchor
    a.handle(utc(T0 + timedelta(seconds=1), ts=2_000_000))
    assert capture.utc == [T0, T0 + timedelta(seconds=1)]


def test_rtcm_raw_echo_counts_rtcm_frames() -> None:
    bus = Bus()
    sub = bus.subscribe("state.rtk")
    a = SbgStateAdapter(bus)
    stream = rtcm3(1005) + rtcm3(1077, 60)
    a.handle(frame("RTCM_RAW", stream[:30]))  # the unit's chunks may split a frame
    assert a.state.rtk.rtcm_rx_total == 1 and a.rtcm_echo_seen
    a.handle(frame("RTCM_RAW", stream[30:]))
    assert a.state.rtk.rtcm_rx_total == 2 and len(topics(sub)) == 2
    b = SbgStateAdapter(Bus())
    b.handle(frame("RTCM_RAW", b"\x00" * 40))  # no RTCM3 frame in it
    assert b.state.rtk.rtcm_rx_total == 0 and not b.rtcm_echo_seen


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
    aligned), GNSS solving with a valid dual-antenna heading. The capture's arrival times are
    all within a millisecond, so each frame is re-timed from its device time stamp to check
    the decimation on real data."""
    bus = Bus()
    sub = bus.subscribe("state.*")
    a = SbgStateAdapter(bus, nav_hz_cap=5.0)
    t = 0.0
    for fr in SbgFramer().feed((FIXTURES / "ellipse_d_live_1s.sbg").read_bytes()):
        msg: Any = fr.parsed()
        stamp = getattr(msg, "time_stamp_us", None)
        t = stamp / 1e6 if isinstance(stamp, int) else t
        a.handle(replace(fr, t_mono=t))
    s = a.state
    seen = topics(sub)
    assert s.epoch_count == 50 and seen.count("state.epoch") == 5  # 50 Hz EKF_NAV, 5 Hz cap
    # EKF_EULER at 50 Hz, capped at 10 Hz: both ends of the ~1 s slice included
    assert seen.count("state.position") == 5 and seen.count("state.attitude") == 11
    assert s.time.utc is not None and s.time.utc.year == 2026 and s.time.leap_s == 18
    assert s.ins is not None and s.ins.general_ok and all(s.ins.general_ok.values())
    assert s.sats and a.sats_seen and s.imu is not None
    assert a.raw_gnss_format == "unknown-yet"  # the slice carries no GPS1_RAW
    assert s.ins.mode == 1 and s.ins.mode_name == "Vertical gyro"
    assert s.position.invalid_llh and s.fix.fix_type == 0 and not s.fix.gnss_fix_ok
    att = s.attitude
    assert att is not None and att.source == "sbg-gnss-hdt" and att.roll_deg is not None
    assert s.fix.num_sv == 23 and s.ins.gnss_fix == 3 and s.ins.gnss_fix_name == "DGNSS"


def _epochs(sub: Any) -> list[ReceiverState]:
    return [e for t, e in items(sub) if t == "state.epoch"]


def _gps_ms(dt: datetime, leap: float) -> int:
    sow = (dt.isoweekday() % 7) * 86400 + dt.hour * 3600 + dt.minute * 60 + dt.second
    return round(((sow + leap) % 604800) * 1000) + dt.microsecond // 1000


def test_epoch_time_advances_with_the_ekf_between_utc_logs() -> None:
    """UTC_TIME comes at 1 Hz (the default output profile), EKF_NAV far faster: each epoch is
    dated from its own device time stamp against the last UTC, never repeats the last one."""
    bus = Bus()
    sub = bus.subscribe("state.epoch")
    a = SbgStateAdapter(bus, nav_hz_cap=10.0)
    a.handle(utc(T0, ts=1_000_000, t_mono=0.0))
    for i in range(10):
        a.handle(ekf_nav(ts=1_000_000 + i * 100_000, t_mono=i * 0.1))
    epochs = _epochs(sub)
    assert [e.time.utc for e in epochs] == [T0 + timedelta(milliseconds=100 * i) for i in range(10)]
    itow = _gps_ms(T0, 18)
    assert [e.time.itow_ms for e in epochs] == [itow + 100 * i for i in range(10)]
    week, tow = gps_from_utc(T0 + timedelta(milliseconds=900), 18)
    assert epochs[-1].time.gps_week == week and epochs[-1].time.gps_tow_s == pytest.approx(tow)
    assert all(e.time.valid_time and e.time.fully_resolved for e in epochs)


async def test_point_collector_accepts_every_ins_epoch(tmp_path: Path) -> None:
    db = Database(tmp_path / "m.db")
    await db.open()
    try:
        bus = Bus()
        collector = PointCollector(bus, StateStore(bus), PointsRepo(db), SessionsRepo(db))
        sub = bus.subscribe("state.epoch")
        a = SbgStateAdapter(bus, nav_hz_cap=10.0)
        await collector.start("INS-1", epochs=30, fixed_only=False)
        for i in range(30):  # 10 Hz EKF_NAV, 1 Hz UTC_TIME
            ts, t = 1_000_000 + i * 100_000, i * 0.1
            if i % 10 == 0:
                a.handle(utc(T0 + timedelta(seconds=i // 10), ts=ts, t_mono=t))
            a.handle(ekf_nav(ts=ts, t_mono=t))
        for e in _epochs(sub):
            await collector.on_epoch(e)
        st = collector.status
        assert (st.state, st.accepted, st.skipped) == ("done", 30, 0)
    finally:
        await db.close()


def test_epoch_time_follows_the_units_clock_while_it_free_runs() -> None:
    """A GNSS outage: no PPS, the unit's clock leaves VALID and free-runs while the EKF keeps a
    dead-reckoned position. The epochs carry the unit's own UTC, flagged not valid."""
    bus = Bus()
    sub = bus.subscribe("state.epoch")
    a = SbgStateAdapter(bus, nav_hz_cap=5.0)
    a.handle(utc(T0, ts=1_000_000, t_mono=0.0))
    for k in range(1, 6):
        ts = 1_000_000 + k * 1_000_000
        a.handle(utc(T0 + timedelta(seconds=k), ts=ts, status=FREE_RUNNING_UTC, t_mono=k))
        a.handle(ekf_nav(23.7275 + k * 1e-5, ts=ts + 500_000, t_mono=k + 0.5))
    epochs = _epochs(sub)
    assert [e.time.utc for e in epochs] == [T0 + timedelta(seconds=k + 0.5) for k in range(1, 6)]
    assert [e.time.itow_ms for e in epochs] == [
        _gps_ms(T0, 18) + k * 1000 + 500 for k in range(1, 6)
    ]
    assert all(not e.time.valid_time and e.fix.fix_type == 3 for e in epochs)
    a.handle(event("A", 6_600_000))  # events still wait for a vouched-for UTC
    assert a.state.time_marks == []


@pytest.mark.parametrize(
    ("clock", "anchored"), [(0, False), (1, True), (2, True)], ids=["error", "free", "steering"]
)
def test_only_a_free_running_or_steering_clock_dates_the_epochs(clock: int, anchored: bool) -> None:
    """A clock in ERROR may still report UTC INITIALIZED: its UTC dates nothing, and the epochs
    carry on from the last usable anchor."""
    a = SbgStateAdapter(Bus(), nav_hz_cap=5.0)
    a.handle(utc(T0, ts=1_000_000))
    odd = T0 + timedelta(hours=1)  # what that clock claims
    a.handle(utc(odd, ts=2_000_000, status=(clock << 1) | (2 << 6)))
    a.handle(ekf_nav(ts=2_500_000, t_mono=2.5))
    expected = odd if anchored else T0 + timedelta(seconds=1)
    assert a.state.time.utc == expected + timedelta(seconds=0.5)
    assert not a.state.time.valid_time


def test_epoch_time_is_cleared_once_the_units_clock_is_stale() -> None:
    """No usable UTC_TIME for more than ANCHOR_MAX_S (UTC not initialised, or the log
    stopped): the epochs stop carrying a time rather than stamp an old one on a new fix."""
    a = SbgStateAdapter(Bus(), nav_hz_cap=5.0)
    a.handle(utc(T0, ts=1_000_000))
    a.handle(utc(datetime(2000, 1, 1, tzinfo=UTC), ts=2_000_000, status=1 << 1))  # not init.
    for k in range(2, 62):  # the stream carries on at 1 Hz, without jumps
        a.handle(ekf_nav(ts=k * 1_000_000, t_mono=float(k)))
    t = a.state.time
    assert t.utc == T0 + timedelta(seconds=60) and not t.valid_time  # carried on device time
    assert build_gga(a.state) is not None
    a.handle(ekf_nav(ts=62_000_000, t_mono=62.0))  # 61 s from the last usable UTC
    t = a.state.time
    assert t.utc is None and t.itow_ms is None and t.gps_tow_s is None
    assert build_gga(a.state) is None and a.state.position.lat is not None
    b = SbgStateAdapter(Bus())
    b.handle(utc(T0, ts=100_000_000))
    b.handle(ekf_nav(ts=2_000_000))  # a reboot: the device time stamp restarted
    assert b.state.time.utc is None


def test_ekf_nav_sections_follow_the_epoch_rate() -> None:
    bus = Bus()
    sub = bus.subscribe("state.*")
    a = SbgStateAdapter(bus, nav_hz_cap=5.0)
    for i in range(50):  # EKF_NAV at 50 Hz for 1 s; the EKF mode changes at 10.46 s
        mode = NAV_POSITION if i < 23 else 3
        a.handle(
            ekf_nav(status=POS_VALID | VEL_VALID | mode, ts=TS + i * 20_000, t_mono=10 + i * 0.02)
        )
    got = items(sub)
    seen = [t for t, _ in got]
    assert a.state.epoch_count == 50 and seen.count("state.epoch") == 5
    assert seen.count("state.position") == 5 and seen.count("state.fix") == 5
    # The mode change between two epochs goes out with the next one (state.ins), as the
    # epochs show: 10.0, 10.2, 10.4 in NAV_POSITION, 10.6 and 10.8 after the change.
    assert seen.count("state.ins") == 2
    assert [e.ins.mode for t, e in got if t == "state.epoch"] == [NAV_POSITION] * 3 + [3, 3]
    assert a.state.ins is not None and a.state.ins.mode == 3


def test_a_held_attitude_goes_out_with_the_next_epoch() -> None:
    bus = Bus()
    sub = bus.subscribe("state.attitude")
    a = SbgStateAdapter(bus)
    for i in range(10):  # EKF_EULER at 200 Hz for 50 ms, then it stops
        a.handle(ekf_euler(float(i), 0.0, 0.0, ATT_VALID | 2, t_mono=100.0 + i * 0.005))
    assert len(items(sub)) == 1  # 100.0; the other nine updated the state only
    a.handle(ekf_nav(t_mono=100.2))
    (att,) = [x for _, x in items(sub)]
    assert att is not None and att.roll_deg == pytest.approx(9.0)
    a.handle(ekf_nav(t_mono=100.4))
    assert items(sub) == []  # nothing new was held back


def test_held_marks_are_flagged_and_bounded() -> None:
    """A held mark is dated after the fact across a stretch where the unit's clock was not
    valid: it says so (`utc_based` False, an accuracy that grows with the gap), and one
    further than HELD_MAX_S from the anchor is dropped instead."""
    a = SbgStateAdapter(Bus())
    a.handle(event("A", 1_000_000))  # 400 s before the anchor: dropped
    for k in range(2, 402):
        if k == 201:
            a.handle(event("B", 201_000_000))  # 200 s before the anchor: dated, flagged
        a.handle(imu_short(ts=k * 1_000_000, t_mono=float(k)))
    a.handle(utc(T0, ts=401_000_000))
    (mark,) = a.state.time_marks
    assert mark.channel == 1 and mark.rising_utc == T0 - timedelta(seconds=200)
    assert not mark.utc_based and mark.acc_est_ns == 4_000_000  # 200 s x 20 ppm
    a.handle(event("C", 401_500_000))  # live: dated from the anchor at once
    live = a.state.time_marks[-1]
    assert live.channel == 2 and live.utc_based and live.acc_est_ns == 0


def test_held_mark_overflow_is_logged_once_per_hold(caplog: pytest.LogCaptureFixture) -> None:
    a = SbgStateAdapter(Bus())
    with caplog.at_level(logging.WARNING, logger=LOG):
        for i in range(150):
            a.handle(event("A", 10 + i))
        lost = [r.getMessage() for r in caplog.records if "oldest" in r.getMessage()]
        assert len(lost) == 1 and a.held_marks_lost == 50
        a.handle(utc(T0, ts=0))
        a.handle(utc(T0, ts=500, status=1 << 1))  # clock not valid: events held again
        for i in range(101):
            a.handle(event("A", 1_000 + i))
        lost = [r.getMessage() for r in caplog.records if "oldest" in r.getMessage()]
        assert len(lost) == 2 and a.held_marks_lost == 51


def test_an_event_behind_the_stream_does_not_move_the_timeline() -> None:
    a = SbgStateAdapter(Bus())
    a.handle(utc(T0, ts=100_000_000))
    a.handle(event("A", 96_000_000))  # 4 s behind the stream: late, not a jump; dated
    a.handle(imu_short(ts=102_000_000))  # 2 s on from the stream, 6 s on from the late event
    a.handle(event("B", 102_500_000))
    assert [m.rising_utc for m in a.state.time_marks] == [
        T0 - timedelta(seconds=4),
        T0 + timedelta(seconds=2.5),
    ]


def test_leap_seconds_follow_the_units_clocks() -> None:
    a = SbgStateAdapter(Bus())
    a.handle(utc(T0, ts=1_000_000, gps_tow_ms=_gps_ms(T0, 19)))
    t = a.state.time
    assert t.leap_s == 19 and t.gps_week == gps_from_utc(T0, 19)[0]
    a.handle(event("A", 1_500_000))
    tow = gps_from_utc(T0 + timedelta(seconds=0.5), 19)[1]
    assert a.state.time_marks[-1].rising_tow_s == pytest.approx(tow)
    a.handle(ekf_nav(ts=1_200_000))
    assert a.state.time.itow_ms == _gps_ms(T0, 19) + 200
    t1 = T0 + timedelta(seconds=1)
    a.handle(utc(t1, ts=2_000_000, gps_tow_ms=_gps_ms(t1, 17.5)))  # clocks disagree
    assert a.state.time.leap_s == 19
    t2 = T0 + timedelta(seconds=2)
    a.handle(utc(t2, ts=3_000_000, gps_tow_ms=_gps_ms(t2, 17)))
    assert a.state.time.leap_s == 17


def test_invalid_gnss_velocity_heading_baseline_and_sky_position() -> None:
    a = SbgStateAdapter(Bus())
    a.handle(gps_vel())
    assert a.state.ins is not None and a.state.ins.gnss_vel is not None
    a.handle(gps_vel(computed=False))
    assert a.state.ins.gnss_vel is None
    a.handle(hdt(100.0, 1.25))
    assert a.state.ins.antenna_baseline_m == pytest.approx(1.25)
    a.handle(hdt(100.0, 1.25, baseline_valid=False))
    assert a.state.ins.antenna_baseline_m is None and a.state.ins.gnss_heading_valid
    assert a.state.rtk.baseline_m is None
    a.handle(
        sat_list(
            sat(1, 1, [(14, 5, 40)], used=True, elev=127, azim=400),
            sat(2, 1, [(14, 5, 40)], used=True, elev=-100, azim=360),
        )
    )
    s1, s2 = a.state.sats
    assert (s1.elev, s1.azim) == (None, None) and (s2.elev, s2.azim) == (None, 360)


def test_not_ubx_stream_leaves_the_capture_hour_alone_when_the_utc_is_old() -> None:
    capture = FakeWriter()
    a = SbgStateAdapter(Bus(), raw_capture=capture)
    a.handle(utc(T0, ts=1_000_000))
    for k in range(2, 63):  # the stream carries on, UTC_TIME does not
        a.handle(imu_short(ts=k * 1_000_000, t_mono=float(k)))
    nmea = b"$GNGGA,,,,,,0,00,,,M,,M,,*78\r\n" * 300
    for i in range(0, len(nmea), 1000):
        a.handle(gps_raw(nmea[i : i + 1000]))
    assert a.raw_gnss_format == "unknown" and capture.utc == []  # 61 s old: not named from it
