from datetime import UTC, datetime
from typing import Any

import pytest

from mtrtk.core.bus import Bus
from mtrtk.rover.drivers.vectornav.adapter import VnStateAdapter

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


class FakeCapture:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.utc: list[datetime] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def note_utc(self, dt: datetime) -> None:
        self.utc.append(dt)


def topics(sub: Any) -> list[str]:
    q = sub.queue
    return [q.get_nowait()[0] for _ in range(q.qsize())]


def test_ins_frame_drives_state_and_epoch() -> None:
    bus = Bus()
    sub = bus.subscribe("state.*")
    adapter = VnStateAdapter(bus, nav_hz_cap=5.0, raw_capture=None)
    raw = binary((TIME, time_group_payload()), (ATTITUDE, att_payload()), (INS, ins_payload()))
    adapter.handle(frame(raw, t_mono=10.0))
    s = adapter.state
    assert s.position.lat == pytest.approx(23.7806) and s.position.lon == pytest.approx(90.4071)
    assert s.position.height_m == pytest.approx(12.5) and s.position.hmsl_m is None
    assert s.accuracy.h_acc_m == pytest.approx(0.02) and s.accuracy.s_acc_mps == pytest.approx(0.05)
    assert s.velocity.vel_n_mps == pytest.approx(1.0)
    assert s.velocity.ground_speed_mps == pytest.approx((1.0**2 + 0.5**2) ** 0.5)
    assert s.fix.fix_type == 3 and s.fix.fix_type_name == "3D" and s.fix.gnss_fix_ok
    assert s.attitude is not None
    assert s.attitude.heading_deg == pytest.approx(91.0) and s.attitude.source == "vn-ins"
    assert s.attitude.roll_deg == pytest.approx(0.5) and s.attitude.pitch_deg == pytest.approx(-1)
    assert s.attitude.acc_heading_deg == pytest.approx(0.8)
    assert s.time.utc == datetime(2026, 9, 19, 10, 30, 15, 250000, tzinfo=UTC)
    assert s.time.valid_utc and s.time.gps_week == 2385
    assert s.ins is not None and s.ins.vendor == "vectornav" and s.ins.mode == 2
    assert s.ins.mode_name == "Tracking"
    published = topics(sub)
    assert published.count("state.epoch") == 1 and s.epoch_count == 1
    assert {"state.position", "state.attitude", "state.time", "state.fix", "state.ins"} <= set(
        published
    )


def test_fix_type_follows_ins_mode_and_gps_fix() -> None:
    adapter = VnStateAdapter(Bus(), nav_hz_cap=5.0, raw_capture=None)
    adapter.handle(frame(binary((INS, ins_payload(status=ins_status(mode=1, gps_fix=True))))))
    assert adapter.state.fix.fix_type == 2
    adapter.handle(frame(binary((INS, ins_payload(status=ins_status(mode=2, gps_fix=False))))))
    assert adapter.state.fix.fix_type == 0 and not adapter.state.fix.gnss_fix_ok
    assert adapter.state.ins is not None and adapter.state.ins.mode == 2


def test_ins_status_errors_in_state() -> None:
    adapter = VnStateAdapter(Bus(), nav_hz_cap=5.0, raw_capture=None)
    st = ins_status(mode=2, gps_fix=True, imu_error=True)
    adapter.handle(frame(binary((INS, ins_payload(status=st)))))
    ins = adapter.state.ins
    assert ins is not None and ins.errors["imu"] is True and ins.errors["gps"] is False


def test_epoch_decimated_to_nav_cap() -> None:
    bus = Bus()
    sub = bus.subscribe("state.epoch")
    adapter = VnStateAdapter(bus, nav_hz_cap=5.0, raw_capture=None)
    raw = binary((INS, ins_payload()))
    for i in range(40):  # 40 epochs at 40 Hz = 1 s
        adapter.handle(frame(raw, t_mono=100.0 + i * 0.025))
    assert adapter.state.epoch_count == 40
    assert 4 <= sub.queue.qsize() <= 6


def test_gps_group_rtk_fix_maps_to_carr_soln() -> None:
    adapter = VnStateAdapter(Bus(), nav_hz_cap=5.0, raw_capture=None)
    adapter.handle(frame(binary((GPS, gps_payload(fix=8)))))
    s = adapter.state
    assert s.fix.carr_soln == 2 and s.fix.carr_soln_name == "RTK fixed"
    assert s.rtk.carr_soln == 2
    assert s.fix.num_sv == 12 and s.dops.h == pytest.approx(0.8) and s.dops.p == pytest.approx(1.7)
    assert s.time.leap_s == 18 and s.time.gps_tow_s == pytest.approx(123_456.0)
    assert s.ins is not None and s.ins.gnss_fix == 8 and s.ins.gnss_fix_name == "RTK fixed"
    assert adapter.rtk_fix_seen
    adapter.handle(frame(binary((GPS, gps_payload(fix=7)))))
    assert adapter.state.fix.carr_soln == 1
    adapter.handle(frame(binary((GPS, gps_payload(fix=3)))))
    assert adapter.state.fix.carr_soln == 0 and adapter.state.fix.carr_soln_name == "None"


def test_sat_info_becomes_satellites() -> None:
    bus = Bus()
    sub = bus.subscribe("state.*")
    adapter = VnStateAdapter(bus, nav_hz_cap=5.0, raw_capture=None)
    assert not adapter.sats_seen
    adapter.handle(frame(binary((GPS, gps_payload()))))
    sats = adapter.state.sats
    assert [(s.gnss, s.sv_id) for s in sats] == [("GPS", 5), ("Galileo", 11)]
    assert sats[0].used and not sats[1].used and sats[0].cno == 44
    assert sats[0].elev == 45 and sats[1].azim == -170 % 360
    assert adapter.state.sat_summary.tracked == 2 and adapter.state.sat_summary.used == 1
    assert adapter.sats_seen
    assert {"state.sats", "state.sat_summary"} <= set(topics(sub))


def test_sync_in_event_becomes_time_mark() -> None:
    bus = Bus()
    marks = bus.subscribe("state.time_mark")
    adapter = VnStateAdapter(bus, nav_hz_cap=5.0, raw_capture=None)
    first = time_group_payload(sync_in_cnt=4, utc=(26, 9, 19, 9, 59, 59, 900))
    adapter.handle(frame(binary((TIME, first))))
    assert adapter.state.time_marks == []
    second = time_group_payload(
        sync_in_cnt=5,
        time_sync_in_ns=2_000_000,
        utc=(26, 9, 19, 10, 0, 1, 0),
        gps_tow_ns=345_618_000_000_000,
    )
    adapter.handle(frame(binary((TIME, second))))
    (mark,) = adapter.state.time_marks
    assert mark.channel == 0 and mark.count == 5 and mark.new_rising
    assert mark.rising_utc == datetime(2026, 9, 19, 10, 0, 0, 998000, tzinfo=UTC)
    assert mark.rising_week == 2385 and mark.rising_tow_s == pytest.approx(345_617.998)
    assert marks.queue.qsize() == 1


def test_utc_ignored_when_not_valid() -> None:
    cap = FakeCapture()
    adapter = VnStateAdapter(Bus(), nav_hz_cap=5.0, raw_capture=cap)  # type: ignore[arg-type]
    adapter.handle(frame(binary((TIME, time_group_payload(time_status=0x03)))))
    assert adapter.state.time.utc is None and cap.utc == []


def test_raw_meas_captured_with_record_header() -> None:
    cap = FakeCapture()
    adapter = VnStateAdapter(Bus(), nav_hz_cap=5.0, raw_capture=cap)  # type: ignore[arg-type]
    assert not adapter.raw_meas_seen
    adapter.handle(frame(binary((TIME, time_group_payload()), (GPS, gps_payload(meas=2)))))
    body = raw_meas(2)
    assert cap.written == [b"VNRM" + len(body).to_bytes(2, "little") + body]
    assert adapter.raw_meas_seen is True
    assert cap.utc == [datetime(2026, 9, 19, 10, 30, 15, 250000, tzinfo=UTC)]
    assert adapter.state.raw_epochs == 1


def test_imu_publish_decimated() -> None:
    bus = Bus()
    sub = bus.subscribe("state.imu")
    adapter = VnStateAdapter(bus, nav_hz_cap=5.0, raw_capture=None)
    raw = binary((IMU, imu_payload()))
    for i in range(80):
        adapter.handle(frame(raw, t_mono=50.0 + i * 0.1 / 80))
    imu = adapter.state.imu
    assert imu is not None and imu.temperature_c == pytest.approx(31.5)
    assert imu.accel_mps2 == pytest.approx((0.1, -0.2, -9.81))
    assert 1 <= sub.queue.qsize() <= 2
    assert adapter.state.epoch_count == 0  # IMU-only frames are not navigation epochs


def test_ascii_and_garbage_frames_are_ignored() -> None:
    bus = Bus()
    sub = bus.subscribe("*")
    adapter = VnStateAdapter(bus, nav_hz_cap=5.0, raw_capture=None)
    adapter.handle(frame(b"$VNRRG,01,VN-200*4B\r\n"))
    adapter.handle(frame(b"\xfa\x80\x00\x00"))
    assert sub.queue.qsize() == 0
