"""Pure conversions from mtrtk WebSocket JSON to ROS message field values.

No rclpy here, so all of it is unit-testable anywhere. The inputs are the daemon's WebSocket
messages (`snapshot`, `epoch`, `update`; see docs/api.md) and the `ReceiverState` sections they
carry, already JSON: datetimes are ISO-8601 strings and unknown values are `null`.

Conventions, per REP 103/105: velocity is ENU (x east, y north, z up); orientation is the FLU
body in ENU. Every float the message files document as "NaN when unknown" is NaN here when the
receiver did not say, never 0.0 (which a message defaults to and which reads as a real value).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

NAN = float("nan")
SERVICE_ALL = 1 | 2 | 4 | 8  # NavSatStatus SERVICE_GPS | GLONASS | COMPASS | GALILEO
STATUS_NO_FIX, STATUS_FIX, STATUS_SBAS_FIX, STATUS_GBAS_FIX = -1, 0, 1, 2
COVARIANCE_TYPE_UNKNOWN, COVARIANCE_TYPE_DIAGONAL_KNOWN = 0, 2
# u-blox fixType values that are a GNSS position: 2D, 3D, GNSS+DR. Not 1 (dead reckoning only)
# and not 5 (time only: TMODE fixed, the position is the one the operator entered).
GNSS_FIX_TYPES = frozenset({2, 3, 4})
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_PVT_SECTIONS = ("position", "accuracy", "dops", "fix", "velocity", "time")


def stamp_from_iso(iso: str | None) -> tuple[int, int] | None:
    """ISO-8601 -> (sec, nanosec) since the Unix epoch, in exact integer arithmetic.

    A naive datetime is taken as UTC, as everywhere else in mtrtk.
    """
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = dt - _EPOCH
    return delta.days * 86_400 + delta.seconds, delta.microseconds * 1000


def _f(value: Any) -> float:
    return NAN if value is None else float(value)


def _i(value: Any) -> int:
    return 0 if value is None else int(value)


def _pvt_stamp(pvt: dict[str, Any]) -> tuple[int, int] | None:
    """The epoch's UTC, unless the receiver says its date or time is not valid (yet)."""
    t = pvt.get("time") or {}
    if t.get("valid_date") is False or t.get("valid_time") is False:
        return None
    return stamp_from_iso(t.get("utc"))


def navsat_status(fix: dict[str, Any]) -> int:
    """NavSatStatus.status for a `FixInfo`: RTK float/fixed -> GBAS, differential -> SBAS.

    No fix unless the receiver has a GNSS position (`GNSS_FIX_TYPES`) and `gnssFixOK` is not
    clear - the same rule the NMEA output uses for quality 0.
    """
    if _i(fix.get("fix_type")) not in GNSS_FIX_TYPES or fix.get("gnss_fix_ok") is False:
        return STATUS_NO_FIX
    if _i(fix.get("carr_soln")) >= 1:
        return STATUS_GBAS_FIX
    return STATUS_SBAS_FIX if fix.get("diff_soln") else STATUS_FIX


def navsat_fix_fields(pvt: dict[str, Any]) -> dict[str, Any]:
    p, a, f = pvt.get("position") or {}, pvt.get("accuracy") or {}, pvt.get("fix") or {}
    h, v = _f(a.get("h_acc_m")), _f(a.get("v_acc_m"))
    cov = [0.0] * 9
    known = not (math.isnan(h) or math.isnan(v))
    if known:  # hAcc is the 1-sigma horizontal radius: split it evenly between east and north
        cov[0] = cov[4] = h * h / 2
        cov[8] = v * v
    status = STATUS_NO_FIX if p.get("invalid_llh") else navsat_status(f)
    return {
        "latitude": _f(p.get("lat")),
        "longitude": _f(p.get("lon")),
        "altitude": _f(p.get("height_m")),  # NavSatFix altitude is above the WGS 84 ellipsoid
        "status": status,
        "service": SERVICE_ALL,
        "position_covariance": cov,
        "position_covariance_type": (
            COVARIANCE_TYPE_DIAGONAL_KNOWN if known else COVARIANCE_TYPE_UNKNOWN
        ),
        "stamp": _pvt_stamp(pvt),
    }


def twist_fields(pvt: dict[str, Any]) -> dict[str, Any]:
    """NED velocity -> ENU linear twist, with sAcc as the variance of each linear axis."""
    v, a = pvt.get("velocity") or {}, pvt.get("accuracy") or {}
    s = _f(a.get("s_acc_mps"))
    cov = [0.0] * 36
    if not math.isnan(s):
        cov[0] = cov[7] = cov[14] = s * s
    down = _f(v.get("vel_d_mps"))
    return {
        "linear_x": _f(v.get("vel_e_mps")),
        "linear_y": _f(v.get("vel_n_mps")),
        "linear_z": -down if not math.isnan(down) else NAN,
        "covariance": cov,
        "stamp": _pvt_stamp(pvt),
    }


def rtk_status_fields(
    pvt: dict[str, Any], rtk: dict[str, Any], ntrip_connected: bool
) -> dict[str, Any]:
    """Exactly the `mtrtk_msgs/RtkStatus` fields (bar `header`), plus `stamp`."""
    f, a, d = pvt.get("fix") or {}, pvt.get("accuracy") or {}, pvt.get("dops") or {}
    return {
        "carr_soln": _i(rtk.get("carr_soln")),
        "fix_type": _i(f.get("fix_type")),
        "diff_soln": bool(f.get("diff_soln")),
        "num_sv": _i(f.get("num_sv")),
        "h_acc": _f(a.get("h_acc_m")),
        "v_acc": _f(a.get("v_acc_m")),
        "pdop": _f(d.get("p")),
        "hdop": _f(d.get("h")),
        "corr_age": _f(rtk.get("corr_age_s")),
        "corr_age_receiver": _f(rtk.get("corr_age_receiver_s")),
        "baseline": _f(rtk.get("baseline_m")),
        "rel_pos_n": _f(rtk.get("rel_pos_n_m")),
        "rel_pos_e": _f(rtk.get("rel_pos_e_m")),
        "rel_pos_d": _f(rtk.get("rel_pos_d_m")),
        "rel_pos_heading": _f(rtk.get("heading_deg")) if rtk.get("heading_valid") else NAN,
        "ref_station_id": _i(rtk.get("ref_station_id")),
        "rtcm_rx_total": _i(rtk.get("rtcm_rx_total")),
        "rtcm_crc_failed": _i(rtk.get("rtcm_crc_failed")),
        "ntrip_connected": bool(ntrip_connected),
        "stamp": _pvt_stamp(pvt),
    }


def time_mark_fields(mark: dict[str, Any]) -> dict[str, Any]:
    """A `TimeMark` state entry -> exactly the `mtrtk_msgs/TimeMark` fields, plus `stamp`.

    week/tow stay in the mark's own time base (the receiver's, GNSS or UTC); `stamp` is the
    rising edge in UTC when the daemon could work it out, else None (receive time, node side).
    """
    stamp = stamp_from_iso(mark.get("rising_utc"))
    return {
        "channel": _i(mark.get("channel")),
        "count": _i(mark.get("count")),
        "time_base": _i(mark.get("time_base")),
        "week": _i(mark.get("rising_week")),
        "tow": _f(mark.get("rising_tow_s")),
        "time_valid": stamp is not None,
        "acc_est_ns": _i(mark.get("acc_est_ns")),
        "stamp": stamp,
    }


def _quat_from_euler(roll: float, pitch: float, yaw: float) -> tuple[float, float, float, float]:
    """Intrinsic Z-Y-X (yaw, pitch, roll) Euler angles in radians -> (x, y, z, w)."""
    cr, sr = math.cos(roll / 2), math.sin(roll / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    return (
        round(sr * cp * cy - cr * sp * sy, 12),
        round(cr * sp * cy + sr * cp * sy, 12),
        round(cr * cp * sy - sr * sp * cy, 12),
        round(cr * cp * cy + sr * sp * sy, 12),
    )


def imu_fields(att: dict[str, Any]) -> dict[str, Any]:
    """An `Attitude` (NED/FRD, degrees) -> Imu orientation (FLU body in ENU) and covariance.

    NED/FRD to ENU/FLU keeps roll, negates pitch (nose-up is a negative turn about the FLU
    y-axis, which points left) and turns heading (clockwise from true north) into yaw
    (counter-clockwise from east). A missing roll or pitch is taken as level; a missing heading
    as east - the node publishes an Imu only when the heading is known. A missing 1-sigma marks
    its diagonal entry -1 (element 0 is the REP 145 "no orientation estimate" flag).
    """
    heading = _f(att.get("heading_deg"))
    yaw = math.radians(90.0 - heading) if not math.isnan(heading) else 0.0
    roll = math.radians(_f(att.get("roll_deg"))) if att.get("roll_deg") is not None else 0.0
    pitch = -math.radians(_f(att.get("pitch_deg"))) if att.get("pitch_deg") is not None else 0.0
    cov = [0.0] * 9
    for i, key in enumerate(("acc_roll_deg", "acc_pitch_deg", "acc_heading_deg")):
        sigma = att.get(key)
        cov[i * 4] = math.radians(float(sigma)) ** 2 if sigma is not None else -1.0
    return {"orientation": _quat_from_euler(roll, pitch, yaw), "orientation_covariance": cov}


class EpochAccumulator:
    """Keeps the newest pvt/rtk/attitude from snapshot, epoch and update messages."""

    def __init__(self) -> None:
        self.pvt: dict[str, Any] | None = None
        self.rtk: dict[str, Any] = {}
        self.attitude: dict[str, Any] | None = None
        self.ntrip_connected = False
        self.epochs = 0
        self._marks: list[dict[str, Any]] = []

    def ingest(self, msg: dict[str, Any]) -> bool:
        """Returns True when a new epoch arrived (time to publish fix/vel/rtk)."""
        kind = msg.get("type")
        if kind == "snapshot":
            # A (re)connect: everything is re-read from the snapshot. The NTRIP client's status
            # is not in it, so it is unknown - disconnected - until its next update says so.
            state = msg.get("state") or {}
            self.pvt = {k: state.get(k) or {} for k in _PVT_SECTIONS}
            self.rtk = state.get("rtk") or {}
            self.attitude = state.get("attitude")
            self.ntrip_connected = False
            return False
        if kind == "epoch":
            if msg.get("pvt"):
                self.pvt = msg["pvt"]
            if msg.get("rtk") is not None:
                self.rtk = msg["rtk"]
            if "attitude" in msg:  # only an epoch that carries the section can clear it
                self.attitude = msg["attitude"]
            self.epochs += 1
            return self.pvt is not None
        if kind == "update" and msg.get("topic") == "rtk":
            data = msg.get("data") or {}
            if msg.get("source") == "ntrip_client.status":
                self.ntrip_connected = bool(data.get("connected"))
            elif msg.get("source") == "state.time_mark":
                self._marks.append(data)
        return False

    def pop_time_marks(self) -> list[dict[str, Any]]:
        marks, self._marks = self._marks, []
        return marks
