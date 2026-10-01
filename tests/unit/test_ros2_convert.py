"""The ROS 2 bridge's pure conversions: mtrtk WebSocket JSON in, ROS message field values out.

No ROS here. The bridge package is put on the path by hand, and the message files are parsed as
text so the field names the conversions return are checked against `mtrtk_msgs` itself.
"""

import math
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ros2" / "mtrtk_bridge"))

from mtrtk_bridge.convert import (  # noqa: E402
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


def test_stamp_only_from_a_valid_receiver_time() -> None:
    stale = {**PVT, "time": {"utc": PVT["time"]["utc"], "valid_date": True, "valid_time": False}}
    assert navsat_fix_fields(stale)["stamp"] is None
    good = {**PVT, "time": {"utc": PVT["time"]["utc"], "valid_date": True, "valid_time": True}}
    assert navsat_fix_fields(good)["stamp"] == (1789750054, 250000000)


def test_twist_is_enu() -> None:
    t = twist_fields(PVT)
    assert (t["linear_x"], t["linear_y"], t["linear_z"]) == (2.0, 1.0, 0.5)
    cov = t["covariance"]
    assert cov[0] == cov[7] == cov[14] == 0.1**2 and len(cov) == 36
    assert t["stamp"] == (1789750054, 250000000)


def test_twist_unknowns_are_nan() -> None:
    t = twist_fields({"velocity": {}, "accuracy": {}, "time": {}})
    assert all(math.isnan(t[k]) for k in ("linear_x", "linear_y", "linear_z"))
    assert t["covariance"] == [0.0] * 36 and t["stamp"] is None


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
    assert q["orientation_covariance"][8] == math.radians(0.5) ** 2
    assert q["orientation_covariance"][0] == math.radians(0.1) ** 2
    assert north["orientation_covariance"][0] == -1.0


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
    acc.ingest({"type": "update", "topic": "rf", "source": "state.hardware", "data": {}})  # ignored


def test_accumulator_snapshot_is_not_an_epoch_and_attitude_follows_epochs() -> None:
    acc = EpochAccumulator()
    att = {"roll_deg": 1.0, "pitch_deg": 2.0, "heading_deg": 3.0}
    snapshot = {"type": "snapshot", "state": {**PVT, "rtk": RTK, "attitude": att}}
    assert acc.ingest(snapshot) is False and acc.epochs == 0 and acc.attitude == att
    # an epoch that carries no attitude section leaves the last one; one that does replaces it
    assert acc.ingest({"type": "epoch", "t": 1.0, "pvt": PVT}) is True and acc.attitude == att
    acc.ingest({"type": "epoch", "t": 2.0, "pvt": PVT, "attitude": None})
    assert acc.attitude is None
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
