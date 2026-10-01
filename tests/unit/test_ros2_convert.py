"""The ROS 2 bridge's pure conversions: mtrtk WebSocket JSON in, ROS message field values out.

No ROS here. The bridge package is put on the path by hand, and the message files are parsed as
text so the field names the conversions return are checked against `mtrtk_msgs` itself.
"""

import math
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ros2" / "mtrtk_bridge"))

from mtrtk.core.state import Attitude, ReceiverState, TimeMark  # noqa: E402
from mtrtk.rover.ntrip_client import NtripClientStatus  # noqa: E402
from mtrtk.web import ws  # noqa: E402
from mtrtk_bridge.convert import (  # noqa: E402
    MAX_PENDING_TIME_MARKS,
    UNKNOWN_ANGLE_VARIANCE,
    UNKNOWN_VARIANCE,
    EpochAccumulator,
    imu_fields,
    navsat_fix_fields,
    navsat_status,
    rtk_status_fields,
    stamp_from_iso,
    time_mark_fields,
    twist_fields,
)

MSG_DIR = ROOT / "ros2" / "mtrtk_msgs" / "msg"

PVT: dict[str, Any] = {
    "position": {"lat": 23.8373506, "lon": 90.2625502, "height_m": -36.268, "hmsl_m": 13.363},
    "accuracy": {"h_acc_m": 0.02, "v_acc_m": 0.03, "s_acc_mps": 0.1},
    "fix": {"fix_type": 3, "carr_soln": 2, "diff_soln": True, "num_sv": 27, "gnss_fix_ok": True},
    "velocity": {"vel_n_mps": 1.0, "vel_e_mps": 2.0, "vel_d_mps": -0.5},
    "time": {"utc": "2026-09-18T16:47:34.250000+00:00"},
    "dops": {"p": 1.2, "h": 0.7},
}
RTK: dict[str, Any] = {
    "carr_soln": 2,
    "corr_age_s": 1.2,
    "corr_age_receiver_s": 2,
    "baseline_m": 1234.5,
    "rel_pos_n_m": 1000.0,
    "rel_pos_e_m": -700.0,
    "rel_pos_d_m": 5.0,
    "heading_deg": 91.0,
    "heading_valid": True,
    "rel_pos_valid": True,
    "ref_station_id": 7,
    "rtcm_rx_total": 240,
    "rtcm_crc_failed": 1,
}
MARK: dict[str, Any] = {
    "channel": 0,
    "count": 42,
    "rising_week": 2436,
    "rising_tow_s": 492472.123456,
    "time_base": 1,
    "rising_utc": "2026-09-18T16:47:34.123456+00:00",
    "acc_est_ns": 25,
}


def msg_fields(name: str) -> set[str]:
    """The field names of a .msg file: constants (`NAME=value`) and comments left out."""
    fields = set()
    for line in (MSG_DIR / name).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" in line:
            continue
        fields.add(line.split()[1])
    return fields


def test_stamp() -> None:
    assert stamp_from_iso("2026-09-18T16:47:34.250000+00:00") == (1789750054, 250000000)
    assert stamp_from_iso(None) is None
    assert stamp_from_iso("") is None


def test_stamp_is_exact_integer_arithmetic() -> None:
    # A float timestamp of x.999999 is one rounding away from x+1 s; the stamp must never be.
    assert stamp_from_iso("2026-09-18T16:47:34.999999+00:00") == (1789750054, 999999000)
    assert stamp_from_iso("2026-09-18T16:47:34Z") == (1789750054, 0)
    assert stamp_from_iso("2026-09-18T22:47:34+06:00") == (1789750054, 0)
    assert stamp_from_iso("2026-09-18T16:47:34") == (1789750054, 0)  # naive is UTC


def test_navsat_status() -> None:
    assert navsat_status({"fix_type": 0, "carr_soln": 0, "diff_soln": False}) == -1
    assert navsat_status({"fix_type": 3, "carr_soln": 0, "diff_soln": False}) == 0
    assert navsat_status({"fix_type": 3, "carr_soln": 0, "diff_soln": True}) == 1
    assert navsat_status({"fix_type": 3, "carr_soln": 1, "diff_soln": True}) == 2
    assert navsat_status({"fix_type": 3, "carr_soln": 2, "diff_soln": True}) == 2


def test_navsat_status_no_fix_unless_the_receiver_has_a_gnss_position() -> None:
    assert navsat_status({"fix_type": 1, "carr_soln": 0, "diff_soln": False}) == -1  # DR only
    assert navsat_status({"fix_type": 5, "carr_soln": 0, "diff_soln": False}) == -1  # time only
    assert navsat_status({"fix_type": 2, "carr_soln": 0, "diff_soln": False}) == 0
    assert navsat_status({"fix_type": 4, "carr_soln": 2, "diff_soln": True}) == 2  # GNSS+DR
    # gnssFixOK clear: outside the receiver's DOP/accuracy masks, as NMEA quality 0 says too
    assert navsat_status({"fix_type": 3, "carr_soln": 2, "gnss_fix_ok": False}) == -1


def test_navsat_fix_fields() -> None:
    f = navsat_fix_fields(PVT)
    assert f["latitude"] == 23.8373506 and f["longitude"] == 90.2625502
    assert f["altitude"] == -36.268  # NavSatFix altitude is above the WGS 84 ellipsoid
    assert f["status"] == 2 and f["service"] == 15 and f["position_covariance_type"] == 2
    cov = f["position_covariance"]
    assert cov[0] == cov[4] == 0.02**2 / 2 and cov[8] == 0.03**2 and len(cov) == 9
    assert f["stamp"] == (1789750054, 250000000)


def test_navsat_fix_invalid_llh_and_unknowns() -> None:
    f = navsat_fix_fields({**PVT, "position": {**PVT["position"], "invalid_llh": True}})
    assert f["status"] == -1
    empty = navsat_fix_fields({"position": {}, "accuracy": {}, "fix": {}, "time": {}})
    assert math.isnan(empty["latitude"]) and math.isnan(empty["longitude"])
    assert math.isnan(empty["altitude"]) and empty["status"] == -1
    assert empty["position_covariance"] == [0.0] * 9 and empty["position_covariance_type"] == 0
    assert empty["stamp"] is None
    # DIAGONAL_KNOWN needs both: half a covariance would put a NaN variance on the diagonal
    for acc in ({"h_acc_m": 0.02, "v_acc_m": None}, {"h_acc_m": None, "v_acc_m": 0.03}):
        half = navsat_fix_fields({**PVT, "accuracy": acc})
        assert half["position_covariance_type"] == 0, acc
        assert half["position_covariance"] == [0.0] * 9, acc


def test_stamp_only_from_a_valid_receiver_time() -> None:
    stale = {**PVT, "time": {"utc": PVT["time"]["utc"], "valid_date": True, "valid_time": False}}
    assert navsat_fix_fields(stale)["stamp"] is None
    no_date = {**PVT, "time": {"utc": PVT["time"]["utc"], "valid_date": False, "valid_time": True}}
    assert navsat_fix_fields(no_date)["stamp"] is None
    good = {**PVT, "time": {"utc": PVT["time"]["utc"], "valid_date": True, "valid_time": True}}
    assert navsat_fix_fields(good)["stamp"] == (1789750054, 250000000)


def test_twist_is_enu() -> None:
    t = twist_fields(PVT)
    assert (t["linear_x"], t["linear_y"], t["linear_z"]) == (2.0, 1.0, 0.5)
    # linear diagonal from sAcc; the angular rate is not measured, so it is a huge variance,
    # never 0 (which would claim the rover is known to be exactly not turning)
    expected = [0.0] * 36
    expected[0] = expected[7] = expected[14] = 0.1**2
    expected[21] = expected[28] = expected[35] = UNKNOWN_VARIANCE
    assert t["covariance"] == expected
    assert t["stamp"] == (1789750054, 250000000)


def test_twist_unknowns_are_nan() -> None:
    t = twist_fields({"velocity": {}, "accuracy": {}, "time": {}})
    assert all(math.isnan(t[k]) for k in ("linear_x", "linear_y", "linear_z"))
    expected = [0.0] * 36
    for i in (0, 7, 14, 21, 28, 35):
        expected[i] = UNKNOWN_VARIANCE
    assert t["covariance"] == expected and t["stamp"] is None


def test_rtk_status_fields_and_nans() -> None:
    r = rtk_status_fields(PVT, RTK, ntrip_connected=True)
    assert r["carr_soln"] == 2 and r["fix_type"] == 3 and r["num_sv"] == 27
    assert r["baseline"] == 1234.5 and r["rel_pos_heading"] == 91.0
    assert r["rel_pos_n"] == 1000.0 and r["rel_pos_e"] == -700.0 and r["rel_pos_d"] == 5.0
    assert r["ref_station_id"] == 7 and r["rtcm_rx_total"] == 240 and r["rtcm_crc_failed"] == 1
    assert r["ntrip_connected"] is True and r["corr_age"] == 1.2 and r["corr_age_receiver"] == 2
    assert r["pdop"] == 1.2 and r["hdop"] == 0.7 and r["h_acc"] == 0.02 and r["v_acc"] == 0.03
    assert r["diff_soln"] is True and r["stamp"] == (1789750054, 250000000)
    empty = rtk_status_fields(PVT, {"carr_soln": 0, "heading_valid": False}, ntrip_connected=False)
    assert math.isnan(empty["baseline"]) and math.isnan(empty["rel_pos_heading"])
    assert math.isnan(empty["corr_age"]) and empty["ref_station_id"] == 0
    # a heading the receiver flags invalid is not a heading
    assert math.isnan(
        rtk_status_fields(PVT, {**RTK, "heading_valid": False}, True)["rel_pos_heading"]
    )
    # carr_soln is the RTK section's, not the fix section's
    pvt_fixed = {**PVT, "fix": {**PVT["fix"], "carr_soln": 2}}
    assert rtk_status_fields(pvt_fixed, {**RTK, "carr_soln": 1}, True)["carr_soln"] == 1


def test_rtk_status_relative_position_only_when_relposned_says_valid() -> None:
    """A rover with no base yet still gets RELPOSNED, with relPosValid=0 and zeros in it."""
    no_base = {
        **RTK,
        "rel_pos_valid": False,
        "heading_valid": False,
        "baseline_m": 0.0,
        "rel_pos_n_m": 0.0,
        "rel_pos_e_m": 0.0,
        "rel_pos_d_m": 0.0,
    }
    r = rtk_status_fields(PVT, no_base, ntrip_connected=False)
    for key in ("baseline", "rel_pos_n", "rel_pos_e", "rel_pos_d", "rel_pos_heading"):
        assert math.isnan(r[key]), key


def test_rtk_status_empty_epoch_is_nan_everywhere_unknown() -> None:
    """No RTCM, no RELPOSNED, no DOPs, no accuracy: every float the .msg calls NaN is NaN."""
    blank_pvt: dict[str, Any] = {"position": {}, "accuracy": {}, "fix": {}, "time": {}}
    r = rtk_status_fields(blank_pvt, {}, ntrip_connected=False)
    for key in (
        "h_acc",
        "v_acc",
        "pdop",
        "hdop",
        "corr_age",
        "corr_age_receiver",
        "baseline",
        "rel_pos_n",
        "rel_pos_e",
        "rel_pos_d",
        "rel_pos_heading",
    ):
        assert math.isnan(r[key]), key
    assert r["carr_soln"] == r["fix_type"] == r["num_sv"] == 0
    assert r["ref_station_id"] == r["rtcm_rx_total"] == r["rtcm_crc_failed"] == 0
    assert r["diff_soln"] is False and r["ntrip_connected"] is False and r["stamp"] is None
    # what the snapshot of a fresh receiver really carries: explicit nulls, not missing keys
    nulls = {"carr_soln": 0, "baseline_m": None, "ref_station_id": None, "corr_age_s": None}
    r = rtk_status_fields(blank_pvt, nulls, ntrip_connected=False)
    assert math.isnan(r["baseline"]) and math.isnan(r["corr_age"]) and r["ref_station_id"] == 0


def test_rtk_status_fields_are_exactly_the_message_fields() -> None:
    """The node `setattr`s every key but `stamp` onto an RtkStatus: a stray key would raise."""
    keys = set(rtk_status_fields(PVT, RTK, ntrip_connected=True)) - {"stamp"}
    assert keys == msg_fields("RtkStatus.msg") - {"header"}


def test_time_mark_fields() -> None:
    m = time_mark_fields(MARK)
    assert m["count"] == 42 and m["week"] == 2436 and m["tow"] == 492472.123456
    assert m["time_base"] == 1 and m["channel"] == 0 and m["acc_est_ns"] == 25
    assert m["time_valid"] is True and m["stamp"] == (1789750054, 123456000)
    unknown = time_mark_fields(
        {
            "channel": 0,
            "count": 1,
            "rising_week": None,
            "rising_tow_s": None,
            "rising_utc": None,
            "acc_est_ns": 0,
        }
    )
    assert unknown["time_valid"] is False and unknown["stamp"] is None
    assert unknown["week"] == 0 and math.isnan(unknown["tow"]) and unknown["time_base"] == 0
    # receiver time base: week/tow are there, but no UTC could be worked out for them
    local = time_mark_fields({**MARK, "time_base": 0, "rising_utc": None})
    assert local["time_valid"] is False and local["stamp"] is None
    assert local["week"] == 2436 and local["tow"] == 492472.123456 and local["time_base"] == 0


def test_time_mark_fields_are_exactly_the_message_fields() -> None:
    keys = set(time_mark_fields(MARK)) - {"stamp"}
    assert keys == msg_fields("TimeMark.msg") - {"header"}


def test_imu_orientation_from_attitude() -> None:
    q = imu_fields(
        {
            "roll_deg": 0.0,
            "pitch_deg": 0.0,
            "heading_deg": 90.0,
            "acc_roll_deg": 0.1,
            "acc_pitch_deg": 0.1,
            "acc_heading_deg": 0.5,
        }
    )
    # heading 90° true (east) in ENU yaw = 0 rad -> identity quaternion
    assert q["orientation"] == (0.0, 0.0, 0.0, 1.0)
    north = imu_fields(
        {
            "roll_deg": 0.0,
            "pitch_deg": 0.0,
            "heading_deg": 0.0,
            "acc_roll_deg": None,
            "acc_pitch_deg": None,
            "acc_heading_deg": None,
        }
    )
    assert north["orientation"][2] == round(math.sin(math.pi / 4), 12)
    assert north["orientation"][3] == round(math.cos(math.pi / 4), 12)
    r01, r05 = math.radians(0.1) ** 2, math.radians(0.5) ** 2
    assert q["orientation_covariance"] == [r01, 0.0, 0.0, 0.0, r01, 0.0, 0.0, 0.0, r05]
    # an orientation with no 1-sigmas is still an orientation: huge variances, never -1
    big = UNKNOWN_ANGLE_VARIANCE
    assert north["orientation_covariance"] == [big, 0.0, 0.0, 0.0, big, 0.0, 0.0, 0.0, big]


def test_imu_covariance_is_never_negative_and_minus_one_means_no_orientation() -> None:
    """sensor_msgs/Imu: -1 is only element 0, only for "no orientation estimate at all"."""
    big = UNKNOWN_ANGLE_VARIANCE
    good = {"roll_deg": 1.0, "pitch_deg": 2.0, "heading_deg": 30.0}
    sig = {"acc_roll_deg": 0.1, "acc_pitch_deg": 0.2, "acc_heading_deg": 0.5}
    r01, r02, r05 = (math.radians(x) ** 2 for x in (0.1, 0.2, 0.5))
    no_pitch_sigma = imu_fields({**good, **sig, "acc_pitch_deg": None})["orientation_covariance"]
    assert no_pitch_sigma == [r01, 0.0, 0.0, 0.0, big, 0.0, 0.0, 0.0, r05]
    no_roll_sigma = imu_fields({**good, **sig, "acc_roll_deg": None})["orientation_covariance"]
    assert no_roll_sigma == [big, 0.0, 0.0, 0.0, r02, 0.0, 0.0, 0.0, r05]
    # a roll or pitch the INS never gave is published level, so it must say it does not know
    no_pitch = imu_fields({**good, **sig, "pitch_deg": None})["orientation_covariance"]
    assert no_pitch == [r01, 0.0, 0.0, 0.0, big, 0.0, 0.0, 0.0, r05]
    nothing = imu_fields({"roll_deg": None, "pitch_deg": None, "heading_deg": None, **sig})
    assert nothing["orientation_covariance"] == [-1.0] + [0.0] * 8


def _rotate(q: tuple[float, float, float, float], v: tuple[float, float, float]) -> list[float]:
    """v rotated by the unit quaternion q = (x, y, z, w): body axis -> ENU world."""
    x, y, z, w = q
    m = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    return [sum(m[i][j] * v[j] for j in range(3)) for i in range(3)]


def _close(a: list[float], b: tuple[float, float, float]) -> bool:
    return all(abs(p - q) < 1e-9 for p, q in zip(a, b, strict=True))


def test_imu_orientation_is_rep103_enu_flu() -> None:
    """INS attitude is NED/FRD (roll right-wing-down, pitch nose-up, heading clockwise from
    north); a ROS Imu orientation is the FLU body in ENU. Check the body axes land right."""
    c, s = math.cos(math.radians(10)), math.sin(math.radians(10))
    nose_up = imu_fields({"roll_deg": 0.0, "pitch_deg": 10.0, "heading_deg": 0.0})["orientation"]
    assert _close(_rotate(nose_up, (1, 0, 0)), (0.0, c, s))  # forward: north and up
    right_down = imu_fields({"roll_deg": 10.0, "pitch_deg": 0.0, "heading_deg": 0.0})
    assert _close(_rotate(right_down["orientation"], (0, 1, 0)), (-c, 0.0, s))  # left wing up
    east = imu_fields({"roll_deg": 0.0, "pitch_deg": 0.0, "heading_deg": 90.0})["orientation"]
    assert _close(_rotate(east, (1, 0, 0)), (1.0, 0.0, 0.0))
    # heading 30, pitch 5 nose up: forward points 30° east of north and climbs
    q = imu_fields({"roll_deg": 0.0, "pitch_deg": 5.0, "heading_deg": 30.0})["orientation"]
    cp, sp = math.cos(math.radians(5)), math.sin(math.radians(5))
    fwd = (cp * math.sin(math.radians(30)), cp * math.cos(math.radians(30)), sp)
    assert _close(_rotate(q, (1, 0, 0)), fwd)


def test_accumulator_merges_messages() -> None:
    acc = EpochAccumulator()
    acc.ingest(
        {
            "type": "snapshot",
            "role": "rover",
            "topics": [],
            "state": {**PVT, "rtk": RTK, "attitude": None, "time_marks": []},
        }
    )
    assert acc.pvt is not None and acc.pvt["fix"]["carr_soln"] == 2
    assert acc.rtk["baseline_m"] == 1234.5 and acc.ntrip_connected is False
    new_epoch = acc.ingest(
        {
            "type": "epoch",
            "t": 1.0,
            "pvt": {**PVT, "accuracy": {"h_acc_m": 0.5, "v_acc_m": 0.6}},
            "rtk": {**RTK, "carr_soln": 1},
        }
    )
    assert new_epoch is True
    assert acc.pvt["accuracy"]["h_acc_m"] == 0.5 and acc.rtk["carr_soln"] == 1 and acc.epochs == 1
    ntrip = {"type": "update", "topic": "rtk", "source": "ntrip_client.status"}
    assert acc.ingest({**ntrip, "data": {"connected": True}}) is False
    assert acc.ntrip_connected is True
    mark = {"type": "update", "topic": "rtk", "source": "state.time_mark"}
    acc.ingest({**mark, "data": {"channel": 0, "count": 5}})
    assert acc.pop_time_marks() == [{"channel": 0, "count": 5}] and acc.pop_time_marks() == []
    pvt, rtk = acc.pvt, acc.rtk
    acc.ingest({**mark, "data": {"channel": 0, "count": 6}})
    rf = {"type": "update", "topic": "rf", "source": "state.hardware", "data": {"connected": False}}
    assert acc.ingest(rf) is False  # another topic: changes nothing
    assert acc.pvt is pvt and acc.rtk is rtk and acc.ntrip_connected is True and acc.epochs == 1
    assert acc.pop_time_marks() == [{"channel": 0, "count": 6}]


def test_accumulator_snapshot_is_not_an_epoch_and_attitude_follows_epochs() -> None:
    acc = EpochAccumulator()
    att = {"roll_deg": 1.0, "pitch_deg": 2.0, "heading_deg": 3.0}
    snapshot = {"type": "snapshot", "state": {**PVT, "rtk": RTK, "attitude": att}}
    assert acc.ingest(snapshot) is False and acc.epochs == 0 and acc.attitude == att
    # the snapshot's attitude is the last known one, not this epoch's: never fresh
    assert acc.attitude_fresh is False and acc.fresh_attitude is None
    # an epoch that carries no attitude section leaves the last one, but not as fresh
    assert acc.ingest({"type": "epoch", "t": 1.0, "pvt": PVT}) is True and acc.attitude == att
    assert acc.attitude_fresh is False and acc.fresh_attitude is None
    att2 = {**att, "heading_deg": 200.0}
    acc.ingest({"type": "epoch", "t": 2.0, "pvt": PVT, "attitude": att2})
    assert acc.attitude_fresh is True and acc.fresh_attitude == att2
    acc.ingest({"type": "epoch", "t": 3.0, "pvt": PVT})  # the next one without: stale again
    assert acc.attitude == att2 and acc.fresh_attitude is None
    acc.ingest({"type": "epoch", "t": 4.0, "pvt": PVT, "attitude": None})
    assert acc.attitude is None and acc.attitude_fresh is False
    # an epoch without a pvt section has no new fix to publish, even with an old one in hand
    assert acc.ingest({"type": "epoch", "t": 5.0, "rtk": RTK}) is False and acc.epochs == 5
    # an epoch without pvt before any pvt at all has nothing to publish
    fresh = EpochAccumulator()
    assert fresh.ingest({"type": "epoch", "t": None, "rtk": RTK}) is False and fresh.epochs == 1


def test_accumulator_reconnect_resets_the_ntrip_flag_to_the_snapshot() -> None:
    """A new WebSocket session starts from its snapshot: a stale `connected` must not survive."""
    acc = EpochAccumulator()
    acc.ingest(
        {
            "type": "update",
            "topic": "rtk",
            "source": "ntrip_client.status",
            "data": {"connected": True},
        }
    )
    acc.ingest(
        {"type": "update", "topic": "rtk", "source": "state.time_mark", "data": {"count": 9}}
    )
    acc.ingest({"type": "snapshot", "state": {**PVT, "rtk": RTK, "attitude": None}})
    assert acc.ntrip_connected is False
    assert acc.pop_time_marks() == [{"count": 9}]  # already received: still owed to ROS


def test_msg_field_parser_sees_the_real_files() -> None:
    assert re.search(r"\bcarr_soln\b", (MSG_DIR / "RtkStatus.msg").read_text())
    assert {"header", "carr_soln", "rel_pos_heading"} <= msg_fields("RtkStatus.msg")
    assert "CARR_FIXED" not in msg_fields("RtkStatus.msg")


def test_accumulator_pending_time_marks_are_bounded() -> None:
    acc = EpochAccumulator()
    mark = {"type": "update", "topic": "rtk", "source": "state.time_mark"}
    for n in range(MAX_PENDING_TIME_MARKS + 3):
        acc.ingest({**mark, "data": {"channel": 0, "count": n}})
    popped = acc.pop_time_marks()
    assert len(popped) == MAX_PENDING_TIME_MARKS and acc.time_marks_dropped == 3
    assert popped[0]["count"] == 3 and popped[-1]["count"] == MAX_PENDING_TIME_MARKS + 2


def _snapshot(marks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "snapshot", "state": {**PVT, "rtk": RTK, "attitude": None, "time_marks": marks}}


def _tm(count: int, channel: int = 0, rising: bool = True) -> dict[str, Any]:
    return {"channel": channel, "count": count, "new_rising": rising}


def test_accumulator_recovers_time_marks_raised_while_disconnected() -> None:
    acc = EpochAccumulator()
    # first connect: the snapshot's marks are history, not news
    acc.ingest(_snapshot([_tm(1), _tm(2), _tm(3, channel=1)]))
    assert acc.pop_time_marks() == []
    mark = {"type": "update", "topic": "rtk", "source": "state.time_mark"}
    acc.ingest({**mark, "data": _tm(5)})
    assert [m["count"] for m in acc.pop_time_marks()] == [5]
    # dropped, reconnected: 6 and 7 (and 4 on channel 1) happened in the gap; a falling-edge-only
    # report of 7 is not a new mark
    acc.ingest(_snapshot([_tm(3, 1), _tm(5), _tm(6), _tm(4, 1), _tm(7), _tm(7, rising=False)]))
    assert [(m["channel"], m["count"]) for m in acc.pop_time_marks()] == [(0, 6), (1, 4), (0, 7)]
    # an update that repeats a mark the snapshot already gave is not published twice
    acc.ingest({**mark, "data": _tm(7)})
    acc.ingest({**mark, "data": _tm(8)})
    assert [m["count"] for m in acc.pop_time_marks()] == [8]
    # the u-blox count is 16 bits: 65535 -> 0 is the next mark, not an old one
    acc.ingest({**mark, "data": _tm(65535)})
    acc.pop_time_marks()
    acc.ingest(_snapshot([_tm(65535), _tm(0), _tm(1)]))
    assert [m["count"] for m in acc.pop_time_marks()] == [0, 1]
    # a channel first seen after the first connect: everything on it is news
    acc.ingest(_snapshot([_tm(0), _tm(1), _tm(9, channel=2)]))
    assert [(m["channel"], m["count"]) for m in acc.pop_time_marks()] == [(2, 9)]


def test_contract_with_the_daemon_state_and_websocket_messages() -> None:
    """Inputs built by the daemon's own code, not by hand: a field renamed in state.py or a
    section renamed in ws.py must fail here instead of turning silently into NaN/0/None."""
    state = ReceiverState()
    state.position.lat, state.position.lon, state.position.height_m = 23.83, 90.26, -36.25
    state.accuracy.h_acc_m, state.accuracy.v_acc_m, state.accuracy.s_acc_mps = 0.02, 0.03, 0.1
    state.fix.fix_type, state.fix.carr_soln, state.fix.num_sv = 3, 2, 27
    state.fix.diff_soln = state.fix.gnss_fix_ok = True
    state.velocity.vel_n_mps, state.velocity.vel_e_mps, state.velocity.vel_d_mps = 1.0, 2.0, -0.5
    state.dops.p = 1.2
    state.time.utc = datetime(2026, 9, 18, 16, 47, 34, 250000, tzinfo=UTC)
    state.time.valid_date = state.time.valid_time = True
    state.rtk.carr_soln, state.rtk.baseline_m, state.rtk.rel_pos_n_m = 2, 1234.5, 1000.0
    state.rtk.heading_deg, state.rtk.heading_valid, state.rtk.rel_pos_valid = 91.0, True, True
    state.rtk.ref_station_id, state.rtk.corr_age_s = 7, 1.5
    utc = datetime(2026, 9, 18, 16, 47, 34, 123456, tzinfo=UTC)
    old = TimeMark(channel=0, count=4, new_rising=True, rising_week=2436, rising_utc=utc)
    state.time_marks = [old]
    state.attitude = Attitude(roll_deg=1.0, pitch_deg=2.0, heading_deg=30.0)

    acc = EpochAccumulator()
    acc.ingest({"type": "snapshot", "role": "rover", "state": state.model_dump(mode="json")})
    assert acc.attitude is not None and acc.attitude["heading_deg"] == 30.0
    assert acc.ingest(ws.epoch_message(state, ws.TOPICS)) is True
    assert acc.pvt is not None
    f = navsat_fix_fields(acc.pvt)
    assert f["latitude"] == 23.83 and f["status"] == 2 and f["position_covariance_type"] == 2
    assert f["stamp"] == (1789750054, 250000000)
    t = twist_fields(acc.pvt)
    assert (t["linear_x"], t["linear_y"], t["linear_z"]) == (2.0, 1.0, 0.5)
    ntrip = ws._json(NtripClientStatus(connected=True))
    acc.ingest({"type": "update", "topic": "rtk", "source": "ntrip_client.status", "data": ntrip})
    r = rtk_status_fields(acc.pvt, acc.rtk, acc.ntrip_connected)
    assert r["carr_soln"] == 2 and r["baseline"] == 1234.5 and r["rel_pos_n"] == 1000.0
    assert r["rel_pos_heading"] == 91.0 and r["ref_station_id"] == 7 and r["corr_age"] == 1.5
    assert r["ntrip_connected"] is True and r["pdop"] == 1.2
    new = TimeMark(
        channel=0, count=5, new_rising=True, rising_week=2436, rising_tow_s=1.5, rising_utc=utc
    )
    acc.ingest(
        {"type": "update", "topic": "rtk", "source": "state.time_mark", "data": ws._json(new)}
    )
    (mark,) = acc.pop_time_marks()  # the snapshot's mark 4 is history
    m = time_mark_fields(mark)
    assert m["count"] == 5 and m["week"] == 2436 and m["tow"] == 1.5
    assert m["time_valid"] is True and m["stamp"] == (1789750054, 123456000)
    # `ws.TOPICS` includes `ins`: the epoch carried the attitude, so it is this epoch's
    fresh = acc.fresh_attitude
    assert fresh is not None and fresh["heading_deg"] == 30.0 and fresh["roll_deg"] == 1.0


def test_the_bridge_topics_bring_an_ins_rovers_attitude_to_imu_and_heading() -> None:
    """What the bridge really asks for (`link.WS_TOPICS`), built by the daemon's ws.py: an INS
    rover's attitude must come out fresh, or /mtrtk/imu and /mtrtk/heading stay silent."""
    from mtrtk_bridge.link import WS_TOPICS

    state = ReceiverState()
    state.position.lat, state.position.lon = 23.83, 90.26
    state.fix.fix_type = 3
    state.time.utc = datetime(2026, 10, 1, 21, 2, 49, tzinfo=UTC)
    state.attitude = Attitude(
        roll_deg=0.0, pitch_deg=0.0, heading_deg=90.0, acc_heading_deg=0.5, source="sbg-ekf"
    )
    acc = EpochAccumulator()
    assert acc.ingest(ws.epoch_message(state, WS_TOPICS)) is True
    att = acc.fresh_attitude
    assert att is not None and att["heading_deg"] == 90.0 and att["source"] == "sbg-ekf"
    q = imu_fields(att)
    assert q["orientation_covariance"][8] == math.radians(0.5) ** 2
    # level, heading 90 deg (east): yaw 0 in ENU, the identity quaternion
    assert q["orientation"] == (0.0, 0.0, 0.0, 1.0)
    # a u-blox rover (no attitude): the `ins` bundle says so, and nothing is fresh
    state.attitude = None
    acc.ingest(ws.epoch_message(state, WS_TOPICS))
    assert acc.attitude is None and acc.fresh_attitude is None


def test_accumulator_reads_the_attitude_in_the_ins_bundle() -> None:
    acc = EpochAccumulator()
    att = {"roll_deg": 1.0, "pitch_deg": 2.0, "heading_deg": 3.0}
    ins = {"ins": {"vendor": "sbg"}, "imu": None, "attitude": att}
    acc.ingest({"type": "epoch", "t": 1.0, "pvt": PVT, "ins": ins})
    assert acc.fresh_attitude == att
    # an epoch without the `ins` bundle keeps the last attitude, not as fresh
    acc.ingest({"type": "epoch", "t": 2.0, "pvt": PVT})
    assert acc.attitude == att and acc.fresh_attitude is None
    # a bundle whose attitude is null clears it
    acc.ingest({"type": "epoch", "t": 3.0, "pvt": PVT, "ins": {**ins, "attitude": None}})
    assert acc.attitude is None and acc.fresh_attitude is None
