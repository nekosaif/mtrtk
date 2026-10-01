"""VN-200 binary output -> `ReceiverState`.

Groups map as follows (all from the one binary output frame, so the Time group is applied
first and the others can use its UTC):

- Time: `time.utc` when TimeStatus says UTC is valid (also `RawCapture.note_utc`), GPS
  week / time of week, and a SyncIn count increase -> `TimeMark(channel 0)` dated
  `frame UTC - TimeSyncIn` (the time since the last SyncIn edge).
- GPS: the unit's own GNSS fix (`fix.num_sv`, `fix.carr_soln` from Fix 7/8, DOPs, leap
  seconds, `ins.gnss_fix`), SatInfo -> `sats`, RawMeas -> raw capture as
  `b"VNRM" + len u16le + field`.
- Attitude: yaw/pitch/roll and their 1-sigma -> `attitude` (source "vn-ins").
- INS: the navigation solution (position HAE, velocity NED, 1-sigma) and InsStatus ->
  `fix.fix_type`, `ins`; every INS-bearing frame ends a navigation epoch.
- IMU: `imu`, published at most `IMU_PUBLISH_HZ`.

The Common and GPS2 groups are not mapped (the default profile does not request them).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame
from mtrtk.core.state import (
    CARR_SOLN_NAMES,
    FIX_TYPE_NAMES,
    GNSS_NAMES,
    Attitude,
    ImuSample,
    InsStatus,
    Satellite,
    SatSummary,
    TimeMark,
)
from mtrtk.rover.drivers.ins_common import RawCapture, StateAdapter
from mtrtk.rover.drivers.vectornav.parse import (
    GNSS_FIX_NAMES,
    INS_MODE_NAMES,
    VnBinary,
    VnInsStatus,
    VnSat,
)

log = logging.getLogger(__name__)

IMU_PUBLISH_HZ = 10.0
RAW_MEAS_TAG = b"VNRM"
GPS_WEEK_S = 7 * 86400
FIX_RTK_FLOAT, FIX_RTK_FIXED = 7, 8  # VERIFY: RTK fix codes on a VN-200
DIFF_FIXES = frozenset((4, FIX_RTK_FLOAT, FIX_RTK_FIXED))


def _tuple3(value: Any) -> tuple[float, float, float] | None:
    if isinstance(value, tuple) and len(value) == 3:
        return (float(value[0]), float(value[1]), float(value[2]))
    return None


class VnStateAdapter(StateAdapter):
    def __init__(
        self, bus: Bus, *, nav_hz_cap: float = 5.0, raw_capture: RawCapture | None = None
    ) -> None:
        super().__init__(bus, nav_hz_cap=nav_hz_cap)
        self.raw_capture = raw_capture
        self.raw_meas_seen = False
        self.sats_seen = False
        self.rtk_fix_seen = False  # GPS Fix 7/8 observed: the unit does use RTCM
        self._sync_in_cnt: int | None = None
        self._last_imu_pub: float | None = None

    # ------------------------------------------------------------- dispatch
    def apply(self, frame: Frame) -> set[str]:
        parsed = frame.parsed()
        if not isinstance(parsed, VnBinary):
            return set()  # ASCII replies belong to `VnRegisters`
        groups = parsed.groups
        changed: set[str] = set()
        frame_utc: datetime | None = None
        if "time" in groups:
            frame_utc = self._time(groups["time"], changed)
        if "gps" in groups:
            self._gps(groups["gps"], changed)
        if "attitude" in groups:
            self._attitude(groups["attitude"], changed)
        if "imu" in groups:
            self._imu(groups["imu"], frame.t_mono)
        if "ins" in groups:
            self._ins(groups["ins"], changed)
        if "time" in groups:
            self._sync_in(groups["time"], frame_utc)
        if "ins" in groups:
            self.publish_sections(changed)
            changed = set()
            self.end_epoch(frame.t_mono)
        return changed

    def _ins_state(self) -> InsStatus:
        if self.state.ins is None:
            self.state.ins = InsStatus(vendor="vectornav")
        return self.state.ins

    # ------------------------------------------------------------- groups
    def _time(self, t: dict[str, Any], changed: set[str]) -> datetime | None:
        ti = self.state.time
        status = t.get("time_status") or {}
        if "time_status" in t:
            ti.valid_time = bool(status.get("time_ok"))
            ti.valid_date = bool(status.get("date_ok"))
            ti.valid_utc = bool(status.get("utc_time_valid"))
        if "gps_week" in t:
            ti.gps_week = int(t["gps_week"])
        if "gps_tow_s" in t:
            ti.gps_tow_s = float(t["gps_tow_s"])
            ti.itow_ms = int(t["gps_tow"] // 1_000_000)
        changed.add("time")
        utc = t.get("utc")
        if utc is None or not status.get("utc_time_valid"):
            return None
        ti.utc = utc
        ti.fully_resolved = ti.valid_time and ti.valid_date
        if self.raw_capture is not None:
            self.raw_capture.note_utc(utc)
        return utc  # type: ignore[no-any-return]

    def _sync_in(self, t: dict[str, Any], frame_utc: datetime | None) -> None:
        count = t.get("sync_in_cnt")
        if count is None:
            return
        previous, self._sync_in_cnt = self._sync_in_cnt, int(count)
        if previous is None or count == previous:
            return  # the first reading is a baseline, not an edge
        if count < previous:  # counter reset (unit restart): a new baseline
            return
        if count - previous > 1:
            log.debug(
                "VN SyncIn: %d edges in one output interval, dating the last", count - previous
            )
        since_s = t.get("time_sync_in", 0) / 1e9
        mark = TimeMark(channel=0, count=int(count), new_rising=True, time_base=1)
        if frame_utc is not None:
            mark.rising_utc = frame_utc - timedelta(seconds=since_s)
            mark.utc_based = True
            mark.time_base = 2
        week, tow = t.get("gps_week"), t.get("gps_tow_s")
        if week is not None and tow is not None:
            tow_s = float(tow) - since_s
            if tow_s < 0:
                week, tow_s = week - 1, tow_s + GPS_WEEK_S
            mark.rising_week, mark.rising_tow_s = int(week), tow_s
        self.push_time_mark(mark)

    def _gps(self, g: dict[str, Any], changed: set[str]) -> None:
        s = self.state
        if "num_sats" in g:
            s.fix.num_sv = int(g["num_sats"])
            changed.add("fix")
        if "fix" in g:
            code = int(g["fix"])
            carr = 2 if code == FIX_RTK_FIXED else 1 if code == FIX_RTK_FLOAT else 0
            if carr:
                self.rtk_fix_seen = True
            s.fix.carr_soln = s.rtk.carr_soln = carr
            s.fix.carr_soln_name = s.rtk.carr_soln_name = CARR_SOLN_NAMES[carr]
            s.fix.diff_soln = s.rtk.diff_soln = code in DIFF_FIXES
            ins = self._ins_state()
            ins.gnss_fix = code
            ins.gnss_fix_name = GNSS_FIX_NAMES.get(code, f"Fix {code}")
            changed |= {"fix", "rtk", "ins"}
        if "dop" in g:
            gd, pd, td, vd, hd, nd, ed = (float(v) for v in g["dop"])
            d = s.dops
            d.g, d.p, d.t, d.v, d.h, d.n, d.e = gd, pd, td, vd, hd, nd, ed
            changed.add("dops")
        if "week" in g:
            s.time.gps_week = int(g["week"])
            changed.add("time")
        if "tow_s" in g:
            s.time.gps_tow_s = float(g["tow_s"])
            changed.add("time")
        if "time_info" in g:
            s.time.leap_s = int(g["time_info"]["leap_secs"])
            changed.add("time")
        if "time_u" in g:
            s.time.t_acc_ns = int(float(g["time_u"]) * 1e9)
            changed.add("time")
        if "sat_info" in g:
            self._sats(g["sat_info"])
            changed |= {"sats", "sat_summary"}
        raw = g.get("raw_meas")
        if raw is not None:
            self.raw_meas_seen = True
            s.raw_epochs += 1
            if self.raw_capture is not None:
                self.raw_capture.write(RAW_MEAS_TAG + len(raw).to_bytes(2, "little") + raw)

    def _sats(self, sats: list[VnSat]) -> None:
        self.sats_seen = True
        out: list[Satellite] = []
        summary = SatSummary()
        for v in sats:
            gnss = GNSS_NAMES.get(v.sys, f"sys{v.sys}")
            out.append(
                Satellite(
                    gnss_id=v.sys,
                    gnss=gnss,
                    sv_id=v.sv_id,
                    cno=v.cno,
                    elev=v.el if v.az_el_valid else None,
                    azim=v.az % 360 if v.az_el_valid else None,
                    quality_ind=v.qi,
                    used=v.used,
                    health=1 if v.healthy else 2,
                    diff_corr=v.diff_corr,
                    eph_avail=v.ephemeris,
                    alm_avail=v.almanac,
                )
            )
            per = summary.per_gnss.setdefault(gnss, {"tracked": 0, "used": 0})
            per["tracked"] += 1
            summary.tracked += 1
            if v.used:
                per["used"] += 1
                summary.used += 1
        self.state.sats = out
        self.state.sat_summary = summary

    def _attitude(self, a: dict[str, Any], changed: set[str]) -> None:
        ypr = _tuple3(a.get("ypr"))
        if ypr is None:
            return
        yaw, pitch, roll = ypr
        acc = _tuple3(a.get("ypr_u"))
        self.state.attitude = Attitude(
            roll_deg=roll,
            pitch_deg=pitch,
            heading_deg=yaw % 360.0,
            acc_roll_deg=acc[2] if acc else None,
            acc_pitch_deg=acc[1] if acc else None,
            acc_heading_deg=acc[0] if acc else None,
            source="vn-ins",
        )
        changed.add("attitude")
        if acc:
            self.state.accuracy.head_acc_deg = acc[0]
            changed.add("accuracy")

    def _imu(self, m: dict[str, Any], now: float) -> None:
        if not ({"accel", "angular_rate", "temp"} & m.keys()):
            return
        temp = m.get("temp")
        self.state.imu = ImuSample(
            accel_mps2=_tuple3(m.get("accel")),
            gyro_radps=_tuple3(m.get("angular_rate")),
            temperature_c=float(temp) if temp is not None else None,
        )
        last = self._last_imu_pub
        if last is not None and now - last < 1.0 / IMU_PUBLISH_HZ - 1e-6:
            return
        self._last_imu_pub = now
        self.publish_sections({"imu"})

    def _ins(self, n: dict[str, Any], changed: set[str]) -> None:
        s = self.state
        st = n.get("ins_status")
        if isinstance(st, VnInsStatus):
            ins = self._ins_state()
            ins.mode = st.mode
            ins.mode_name = INS_MODE_NAMES.get(st.mode, "Unknown")
            ins.errors = {
                "time": st.time_error,
                "imu": st.imu_error,
                "mag_pres": st.mag_pres_error,
                "gps": st.gps_error,
            }
            ins.aiding = {
                "gps_fix": st.gps_fix,
                "gps_heading_ins": st.gps_heading_ins,
                "gps_compass": st.gps_compass,
            }
            fix_type = 3 if st.mode == 2 and st.gps_fix else (2 if st.gps_fix else 0)
            s.fix.fix_type = fix_type
            s.fix.fix_type_name = FIX_TYPE_NAMES[fix_type]
            s.fix.gnss_fix_ok = st.gps_fix
            changed |= {"fix", "ins"}
        lla = _tuple3(n.get("pos_lla"))
        if lla is not None:
            s.position.lat, s.position.lon, s.position.height_m = lla
            s.position.hmsl_m = None  # VN INS altitude is height above the ellipsoid
            changed.add("position")
        if "pos_u" in n:
            s.accuracy.h_acc_m = float(n["pos_u"])
            changed.add("accuracy")
        vel = _tuple3(n.get("vel_ned"))
        if vel is not None:
            vn, ve, vd = vel
            v = s.velocity
            v.vel_n_mps, v.vel_e_mps, v.vel_d_mps = vn, ve, vd
            v.ground_speed_mps = math.hypot(vn, ve)
            v.heading_motion_deg = math.degrees(math.atan2(ve, vn)) % 360.0
            changed.add("velocity")
        if "vel_u" in n:
            s.accuracy.s_acc_mps = float(n["vel_u"])
            changed.add("accuracy")
