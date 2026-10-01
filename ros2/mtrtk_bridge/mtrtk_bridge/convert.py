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
import re
from collections import deque
from datetime import datetime, timezone
from typing import Any

NAN = float("nan")
SERVICE_ALL = 1 | 2 | 4 | 8  # NavSatStatus SERVICE_GPS | GLONASS | COMPASS | GALILEO
STATUS_NO_FIX, STATUS_FIX, STATUS_SBAS_FIX, STATUS_GBAS_FIX = -1, 0, 1, 2
COVARIANCE_TYPE_UNKNOWN, COVARIANCE_TYPE_DIAGONAL_KNOWN = 0, 2
# u-blox fixType values that are a GNSS position: 2D, 3D, GNSS+DR. Not 1 (dead reckoning only)
# and not 5 (time only: TMODE fixed, the position is the one the operator entered).
GNSS_FIX_TYPES = frozenset({2, 3, 4})
# `timezone.utc`, not `datetime.UTC`: Humble runs the bridge on Python 3.10, which has no UTC.
_UTC = timezone.utc
_EPOCH = datetime(1970, 1, 1, tzinfo=_UTC)
_ISO_FRACTION = re.compile(r"\.(\d+)")
_PVT_SECTIONS = ("position", "accuracy", "dops", "fix", "velocity", "time")
# A covariance has no "unknown" flag of its own (TwistWithCovariance has no covariance_type, and
# an Imu's -1 is for "no orientation at all"), and 0 reads as "known exactly". So an unknown
# variance is a huge finite one: a filter that fuses it learns nothing, and none sees a negative.
UNKNOWN_VARIANCE = 1e6  # (m/s)^2 or (rad/s)^2
UNKNOWN_ANGLE_VARIANCE = math.pi**2  # rad^2: an angle anywhere in +-180 deg
MAX_PENDING_TIME_MARKS = 256  # queued for ROS but not popped yet; the oldest go first
_MARK_COUNT_MOD = 1 << 16  # TIM-TM2 `count` is a u2 that wraps


def iso_for_fromisoformat(iso: str) -> str:
    """*iso* in the one form Python 3.10's `datetime.fromisoformat` accepts.

    pydantic writes UTC as `Z`, which 3.10 rejects, and a fraction must have exactly 6 digits
    there: `Z` becomes `+00:00` and the fraction is padded or cut (not rounded) to microseconds.
    """
    if iso[-1:] in ("Z", "z"):
        iso = iso[:-1] + "+00:00"
    return _ISO_FRACTION.sub(lambda m: "." + m[1][:6].ljust(6, "0"), iso, count=1)


def stamp_from_iso(iso: str | None) -> tuple[int, int] | None:
    """ISO-8601 -> (sec, nanosec) since the Unix epoch, in exact integer arithmetic.

    A naive datetime is taken as UTC, as everywhere else in mtrtk.
    """
    if not iso:
        return None
    dt = datetime.fromisoformat(iso_for_fromisoformat(iso))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
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
    """NED velocity -> ENU linear twist, with sAcc as the variance of each linear axis.

    The angular rate is not measured, so its variances are `UNKNOWN_VARIANCE`, as are the linear
    ones when sAcc is unknown - never 0, which would claim the motion is known exactly.
    """
    v, a = pvt.get("velocity") or {}, pvt.get("accuracy") or {}
    s = _f(a.get("s_acc_mps"))
    linear = s * s if not math.isnan(s) else UNKNOWN_VARIANCE
    cov = [0.0] * 36
    cov[0] = cov[7] = cov[14] = linear
    cov[21] = cov[28] = cov[35] = UNKNOWN_VARIANCE
    down = _f(v.get("vel_d_mps"))
    return {
        "linear_x": _f(v.get("vel_e_mps")),
        "linear_y": _f(v.get("vel_n_mps")),
        "linear_z": -down if not math.isnan(down) else NAN,
        "covariance": cov,
        "stamp": _pvt_stamp(pvt),
    }


def finite_twist(tw: dict[str, Any]) -> dict[str, Any]:
    """`twist_fields` made publishable: `linear` (x, y, z) with no NaN in it, and `covariance`.

    A filter fed a NaN velocity is poisoned for good, so an axis the receiver did not report is
    0.0 with `UNKNOWN_VARIANCE` instead - never 0.0 with the small sAcc variance, which would
    claim the rover is known to be standing still on that axis. The input is not modified.
    """
    linear = (tw["linear_x"], tw["linear_y"], tw["linear_z"])
    cov = list(tw["covariance"])
    for axis, value in enumerate(linear):
        if math.isnan(value):
            cov[axis * 7] = UNKNOWN_VARIANCE
    return {
        "linear": tuple(0.0 if math.isnan(v) else v for v in linear),
        "covariance": cov,
    }


def rtk_status_fields(
    pvt: dict[str, Any], rtk: dict[str, Any], ntrip_connected: bool
) -> dict[str, Any]:
    """Exactly the `mtrtk_msgs/RtkStatus` fields (bar `header`), plus `stamp`.

    `carr_soln` is the RTK section's. The relative position is NaN unless RELPOSNED says it is
    valid: a rover with no base yet still gets RELPOSNED, full of zeros, and a 0 m baseline is
    not "no baseline". Its heading likewise needs its own valid flag.
    """
    f, a, d = pvt.get("fix") or {}, pvt.get("accuracy") or {}, pvt.get("dops") or {}
    rel = bool(rtk.get("rel_pos_valid"))
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
        "baseline": _f(rtk.get("baseline_m")) if rel else NAN,
        "rel_pos_n": _f(rtk.get("rel_pos_n_m")) if rel else NAN,
        "rel_pos_e": _f(rtk.get("rel_pos_e_m")) if rel else NAN,
        "rel_pos_d": _f(rtk.get("rel_pos_d_m")) if rel else NAN,
        "rel_pos_heading": (
            _f(rtk.get("heading_deg")) if rel and rtk.get("heading_valid") else NAN
        ),
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
    (counter-clockwise from east). A missing roll or pitch is taken as level and a missing
    heading as east - the node publishes an Imu only when the heading is known - and every axis
    whose angle or 1-sigma is missing gets `UNKNOWN_ANGLE_VARIANCE`, never a negative variance.
    Only with no angle at all is element 0 the sensor_msgs/Imu "no orientation estimate" -1.
    """
    angles = (att.get("roll_deg"), att.get("pitch_deg"), att.get("heading_deg"))
    sigmas = (att.get("acc_roll_deg"), att.get("acc_pitch_deg"), att.get("acc_heading_deg"))
    roll, pitch, heading = angles
    yaw = math.radians(90.0 - float(heading)) if heading is not None else 0.0
    q = _quat_from_euler(
        math.radians(float(roll)) if roll is not None else 0.0,
        -math.radians(float(pitch)) if pitch is not None else 0.0,
        yaw,
    )
    cov = [0.0] * 9
    if all(angle is None for angle in angles):
        cov[0] = -1.0
        return {"orientation": q, "orientation_covariance": cov}
    for i, (angle, sigma) in enumerate(zip(angles, sigmas, strict=True)):
        if angle is None or sigma is None:
            cov[i * 4] = UNKNOWN_ANGLE_VARIANCE
        else:
            cov[i * 4] = math.radians(float(sigma)) ** 2
    return {"orientation": q, "orientation_covariance": cov}


def _mark_key(mark: dict[str, Any]) -> tuple[int, int]:
    return _i(mark.get("channel")), _i(mark.get("count"))


def _mark_after(count: int, last: int) -> bool:
    """Whether TIM-TM2 `count` comes after `last`, allowing for the 16-bit wrap."""
    return 0 < (count - last) % _MARK_COUNT_MOD < _MARK_COUNT_MOD // 2


class EpochAccumulator:
    """Keeps the newest pvt/rtk/attitude from snapshot, epoch and update messages.

    `attitude` is the last one known; `attitude_fresh` says whether the latest epoch carried
    it. The node publishes orientation only from `fresh_attitude`, so a snapshot's attitude (or
    an old epoch's) is never restamped as current. The daemon sends the attitude in the epoch's
    `ins` bundle (`{ins, imu, attitude}`, topic `ins`); a top-level `attitude` is read as well.

    Time marks are queued for `pop_time_marks`, at most `MAX_PENDING_TIME_MARKS` of them
    (`time_marks_dropped` counts the overflow). A reconnect snapshot's `time_marks` hold the
    marks raised while the socket was down: those after the last one taken on each channel are
    queued too. The first snapshot's marks are history and are not.
    """

    def __init__(self) -> None:
        self.pvt: dict[str, Any] | None = None
        self.rtk: dict[str, Any] = {}
        self.attitude: dict[str, Any] | None = None
        self.attitude_fresh = False
        self.ntrip_connected = False
        self.epochs = 0
        self.time_marks_dropped = 0
        self._marks: deque[dict[str, Any]] = deque(maxlen=MAX_PENDING_TIME_MARKS)
        self._last_mark: dict[int, int] = {}  # channel -> count of the newest mark taken
        self._synced = False  # a snapshot was seen: a later one is a reconnect
        self._snapshot_marks: set[tuple[int, int]] = set()  # in the latest snapshot

    @property
    def fresh_attitude(self) -> dict[str, Any] | None:
        """The attitude the latest epoch carried, or None."""
        return self.attitude if self.attitude_fresh else None

    def ingest(self, msg: dict[str, Any]) -> bool:
        """Returns True when an epoch with a pvt section arrived (time to publish fix/vel/rtk)."""
        kind = msg.get("type")
        if kind == "snapshot":
            # A (re)connect: everything is re-read from the snapshot. The NTRIP client's status
            # is not in it, so it is unknown - disconnected - until its next update says so.
            state = msg.get("state") or {}
            self.pvt = {k: state.get(k) or {} for k in _PVT_SECTIONS}
            self.rtk = state.get("rtk") or {}
            self.attitude = state.get("attitude")
            self.attitude_fresh = False
            self.ntrip_connected = False
            self._recover_marks(state.get("time_marks") or [])
            return False
        if kind == "epoch":
            if msg.get("pvt"):
                self.pvt = msg["pvt"]
            if msg.get("rtk") is not None:
                self.rtk = msg["rtk"]
            ins = msg.get("ins")
            carrier = ins if isinstance(ins, dict) and "attitude" in ins else msg
            if "attitude" in carrier:  # only an epoch that carries the section can clear it
                self.attitude = carrier["attitude"]
            self.attitude_fresh = carrier.get("attitude") is not None
            self.epochs += 1
            return bool(msg.get("pvt"))
        if kind == "update" and msg.get("topic") == "rtk":
            data = msg.get("data") or {}
            if msg.get("source") == "ntrip_client.status":
                self.ntrip_connected = bool(data.get("connected"))
            elif msg.get("source") == "state.time_mark":
                if _mark_key(data) in self._snapshot_marks:
                    return False  # already taken from (or history in) the snapshot
                self._snapshot_marks.clear()  # updates arrive in order: the overlap is over
                self._take_mark(data)
        return False

    def _take_mark(self, mark: dict[str, Any]) -> None:
        if len(self._marks) == self._marks.maxlen:
            self.time_marks_dropped += 1
        self._marks.append(mark)
        channel, count = _mark_key(mark)
        self._last_mark[channel] = count

    def _recover_marks(self, marks: list[dict[str, Any]]) -> None:
        rising = [m for m in marks if m.get("new_rising")]  # a falling-edge-only report is not
        self._snapshot_marks = {_mark_key(m) for m in rising}
        history = (
            set() if self._synced else {_mark_key(m)[0] for m in rising} - set(self._last_mark)
        )
        for mark in rising:  # oldest first
            channel, count = _mark_key(mark)
            if channel in history:
                self._last_mark[channel] = count
                continue
            last = self._last_mark.get(channel)
            if last is None or _mark_after(count, last):
                self._take_mark(mark)
        self._synced = True

    def pop_time_marks(self) -> list[dict[str, Any]]:
        marks = list(self._marks)
        self._marks.clear()
        return marks
