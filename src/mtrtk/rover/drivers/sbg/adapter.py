"""Ellipse-D sbgECom logs -> `ReceiverState`.

- EKF_NAV drives the navigation epoch: position (HAE and MSL), 1-sigma accuracies, NED velocity,
  `fix` on the UBX scale (3 when the EKF position is valid, 2 in NAV_VELOCITY mode, else 0) and
  the EKF mode in `ins`. An invalid EKF position or velocity is not copied: an unaligned unit
  reports a nonsense one (`position.invalid_llh` says so, the last valid fix is kept). Its
  sections are published with the decimated `state.epoch` (at most `nav_hz_cap`), not at the
  INS output rate, and each epoch is dated from its own device time stamp (see UTC_TIME).
- GPS1_POS is the GNSS-only solution: `fix.carr_soln` / `diff_soln` / `num_sv`, `rtk` (carrier
  solution, base station, correction age) and `ins.gnss_fix`, so the UI can show "INS 3D / GNSS
  RTK fixed" while NMEA / JSON publish the EKF position. GPS1_VEL is kept for the INS panel only.
- EKF_EULER -> `attitude` (source "sbg-ekf"); while the EKF heading is not valid a fresh GPS1_HDT
  dual-antenna heading stands in (source "sbg-gnss-hdt"). GPS1_HDT also fills `rtk.heading*` and
  the baseline length. `state.attitude` is published at most `ATTITUDE_PUBLISH_HZ`, and at once
  when its source or heading validity changes.
- UTC_TIME -> `time` (valid only when the unit vouches for it), leap seconds from the GPS time
  of week it carries, the raw writers' clock (`note_utc`) and the anchors that date epochs and
  events. UTC_TIME comes at 1 Hz, EKF_NAV far faster: an epoch's time is the last UTC_TIME
  carried to the epoch's device time stamp. While the clock free-runs or steers (no PPS, a GNSS
  outage) with UTC still initialised, the unit's own UTC dates the epochs, flagged not valid.
  With no usable UTC_TIME within `ANCHOR_MAX_S` (or after a device time stamp jump) the epochs
  carry no time at all, so NMEA / JSON never stamp a stale time on a moving position.
- EVENT_A..E -> `TimeMark`s, one per edge in the log's window, dated from the device time stamp
  against the last valid UTC. A mark is held while there is no anchor it can be dated from: no
  valid UTC yet, the last UTC_TIME not valid, the anchor more than `ANCHOR_MAX_S` away, or the
  device time stamp jumped (a unit reboot restarts it near 0). A held mark is dated from the
  next valid UTC within `HELD_MAX_S`, flagged `utc_based=False` with an accuracy that grows
  with the gap (`HELD_DRIFT`); a farther one is dropped, never extrapolated.
- RTCM_RAW: the unit's echo of the corrections it received; its RTCM3 frames count into
  `rtk.rtcm_rx_total` (and prove the unit takes the RTCM the driver injects).
- GPS1_SAT -> `sats` / `sat_summary` on the u-blox gnssId scale the UI uses.
- STATUS -> `ins` health and aiding flags; IMU_SHORT / IMU_DATA -> `imu` (published at most
  `IMU_PUBLISH_HZ`).
- GPS1_RAW: the internal receiver's own stream, cut into chunks without regard to its framing.
  It is reassembled through a UBX `Framer`; each UBX frame is published on `raw.ubx` +
  `ubx.<NAME>`, so `RawLogWriter` logs it (RINEX-convertible). The format is decided once per
  stream: UBX on the first valid UBX frame, "unknown" after `RAW_DETECT_BYTES` without one (then
  `receiver.error` once, and the stream goes to the opaque raw capture if one is configured).
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Protocol

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame, Framer, FrameSplitter, Proto
from mtrtk.core.router import topics_for
from mtrtk.core.state import (
    CARR_SOLN_NAMES,
    FIX_TYPE_NAMES,
    GNSS_NAMES,
    Attitude,
    ImuSample,
    InsStatus,
    Satellite,
    SatSummary,
    Signal,
    TimeMark,
    Velocity,
)
from mtrtk.core.statestore import gps_from_utc
from mtrtk.rover.drivers.ins_common import StateAdapter
from mtrtk.rover.drivers.sbg.logs import (
    AidingStatus,
    Constellation,
    EkfMode,
    GeneralStatus,
    GnssPosType,
    SbgEkfEuler,
    SbgEkfNav,
    SbgEvent,
    SbgGnssHdt,
    SbgGnssPos,
    SbgGnssRaw,
    SbgGnssVel,
    SbgImuLegacy,
    SbgImuShort,
    SbgSatList,
    SbgStatus,
    SbgUtcTime,
    UtcStatus,
)
from mtrtk.rover.drivers.sbg.signals import signal_name

log = logging.getLogger(__name__)

VENDOR = "sbg"
IMU_PUBLISH_HZ = 10.0
ATTITUDE_PUBLISH_HZ = 10.0
HDT_FRESH_S = 2.0  # a GNSS heading older than this no longer stands in for the EKF one
EULER_FRESH_S = 2.0  # EKF roll / pitch older than this are not reused next to a GNSS heading
# Bytes of GPS1_RAW without a valid UBX frame before the stream is declared "not UBX". The
# biggest frame of the bench unit's stream (RXM-RAWX, ~1 kB at 30 signals) fits several times.
RAW_DETECT_BYTES = 8192
MAX_HELD_MARKS = 100  # events seen before the first valid UTC, dated once it arrives
# An event further than this from the UTC anchor (device time) is held for the next valid UTC:
# UTC_TIME normally arrives every second, so a farther anchor is stale.
ANCHOR_MAX_S = 60.0
# A held mark further than this from the anchor that would date it is dropped. A mark is held
# across a stretch where the clock was not valid (before sync, a GNSS outage), so dating it
# extrapolates the raw device time stamp over that stretch: keep it short.
HELD_MAX_S = 300.0
# Assumed oscillator scale-factor bound over that extrapolation (20 ppm): a held mark's
# `acc_est_ns` is its distance from the anchor times this. VERIFY against UTC_TIME's own
# clk_sf_error_std once its unit is confirmed on hardware.
HELD_DRIFT = 20e-6
# A device time stamp this far from the last one (either way) starts a new timeline: the unit
# rebooted (the stamp restarts near 0) or the link was down; earlier anchors and held marks no
# longer relate to it.
TIMELINE_JUMP_S = 5.0
WEEK_S = 7 * 86400
U32 = 1 << 32
NOT_UBX_MESSAGE = (
    "GPS1_RAW does not look like UBX; raw GNSS capture disabled (see docs/ins-drivers.md)"
)

RTK_POS_TYPES = frozenset((GnssPosType.RTK_FLOAT, GnssPosType.RTK_INT))
DIFF_POS_TYPES = frozenset(
    (
        GnssPosType.PSRDIFF,
        GnssPosType.SBAS,
        GnssPosType.RTK_FLOAT,
        GnssPosType.RTK_INT,
        GnssPosType.PPP_FLOAT,
        GnssPosType.PPP_INT,
    )
)
POS_TYPE_NAMES: dict[int, str] = {
    GnssPosType.NO_SOLUTION: "No solution",
    GnssPosType.UNKNOWN: "Unknown",
    GnssPosType.SINGLE: "Single",
    GnssPosType.PSRDIFF: "DGNSS",
    GnssPosType.SBAS: "SBAS",
    GnssPosType.OMNISTAR: "OmniSTAR",
    GnssPosType.RTK_FLOAT: "RTK float",
    GnssPosType.RTK_INT: "RTK fixed",
    GnssPosType.PPP_FLOAT: "PPP float",
    GnssPosType.PPP_INT: "PPP fixed",
    GnssPosType.FIXED: "Fixed position",
}
EKF_MODE_NAMES: dict[int, str] = {
    EkfMode.UNINITIALIZED: "Uninitialized",
    EkfMode.VERTICAL_GYRO: "Vertical gyro",
    EkfMode.AHRS: "AHRS",
    EkfMode.NAV_VELOCITY: "Nav velocity",
    EkfMode.NAV_POSITION: "Nav position",
}
# sbgECom constellation -> u-blox gnssId (core.state.GNSS_NAMES). L-band has none.
GNSS_ID: dict[int, int] = {
    Constellation.GPS: 0,
    Constellation.SBAS: 1,
    Constellation.GALILEO: 2,
    Constellation.BEIDOU: 3,
    Constellation.QZSS: 5,
    Constellation.GLONASS: 6,
    Constellation.IRNSS: 7,
}
SBAS_PRN_OFFSET = 100  # VERIFY: the bench unit lists GAGAN PRN 127 as id 27
GENERAL_FLAGS: dict[str, GeneralStatus] = {
    "main_power": GeneralStatus.MAIN_POWER_OK,
    "imu_power": GeneralStatus.IMU_POWER_OK,
    "gps_power": GeneralStatus.GPS_POWER_OK,
    "settings": GeneralStatus.SETTINGS_OK,
    "temperature": GeneralStatus.TEMPERATURE_OK,
    "datalogger": GeneralStatus.DATALOGGER_OK,
    "cpu": GeneralStatus.CPU_OK,
}
AIDING_FLAGS: dict[str, AidingStatus] = {
    "gps1_pos": AidingStatus.GPS1_POS_RECV,
    "gps1_vel": AidingStatus.GPS1_VEL_RECV,
    "gps1_hdt": AidingStatus.GPS1_HDT_RECV,
    "gps1_utc": AidingStatus.GPS1_UTC_RECV,
    "gps2_pos": AidingStatus.GPS2_POS_RECV,
    "gps2_vel": AidingStatus.GPS2_VEL_RECV,
    "gps2_hdt": AidingStatus.GPS2_HDT_RECV,
    "gps2_utc": AidingStatus.GPS2_UTC_RECV,
    "mag": AidingStatus.MAG_RECV,
    "odo": AidingStatus.ODO_RECV,
}


class UtcSink(Protocol):
    """`RawLogWriter`: rotation keys on the unit's UTC when its UBX carries no NAV-PVT."""

    def note_utc(self, dt: datetime) -> None: ...


class OpaqueSink(UtcSink, Protocol):
    """`RawCapture`: the GPS1_RAW stream as-is when it is not UBX."""

    def write(self, data: bytes) -> None: ...


Handler = Callable[[Any, Frame], set[str]]


def _stamp_us(msg: Any) -> int | None:
    """The device time stamp of a parsed log (EVENT logs spell it `timestamp_us`)."""
    stamp = getattr(msg, "time_stamp_us", None)
    if stamp is None:
        stamp = getattr(msg, "timestamp_us", None)
    return stamp if isinstance(stamp, int) else None


def _signed_us(delta: int) -> int:
    """A difference of two u32 microsecond time stamps (they wrap every ~71.6 min)."""
    return (delta + U32 // 2) % U32 - U32 // 2


def _leap_seconds(utc: datetime, gps_tow_ms: int) -> int | None:
    """GPS - UTC from one UTC_TIME log, which carries both clocks; None if they disagree."""
    _, utc_tow = gps_from_utc(utc, 0)
    diff = (gps_tow_ms / 1000 - utc_tow) % WEEK_S
    if diff > WEEK_S / 2:
        diff -= WEEK_S
    leap = round(diff)
    return leap if 0 <= leap <= 60 and abs(diff - leap) < 0.01 else None


class SbgStateAdapter(StateAdapter):
    def __init__(
        self,
        bus: Bus,
        *,
        nav_hz_cap: float = 5.0,
        raw_writer: UtcSink | None = None,
        raw_capture: OpaqueSink | None = None,
        raw_gnss: bool = True,
        ubx_framer_factory: Callable[[], FrameSplitter] = Framer,
    ) -> None:
        super().__init__(bus, nav_hz_cap=nav_hz_cap)
        self.raw_writer = raw_writer
        self.raw_capture = raw_capture
        self.raw_gnss = raw_gnss
        self.raw_gnss_format = "unknown-yet"  # then "ubx" | "unknown", decided once per stream
        self.sats_seen = False  # a GPS1_SAT log arrived: `sats` is the unit's own list
        self.rtk_seen = False  # GPS1_POS reported RTK float/fixed: the unit uses RTCM
        self.rtcm_echo_seen = False  # RTCM_RAW echoed an RTCM3 frame: the unit receives RTCM
        self._ubx = ubx_framer_factory()
        self._rtcm_echo = Framer()  # RTCM_RAW payloads, which may split a frame
        self._probe = bytearray()  # GPS1_RAW bytes while the format is still undecided
        self._utc_anchor: tuple[int, datetime] | None = None  # (device time stamp us, UTC)
        # The same for the epochs' time: also the unit's free-running UTC, flagged not valid.
        self._clock_anchor: tuple[int, datetime] | None = None
        self._last_stamp: int | None = None  # latest device time stamp seen
        self._timeline = 0  # bumped when the device time stamp jumps (reboot, outage)
        # (timeline, channel, count, device time stamp) of marks waiting for an anchor
        self._held_marks: deque[tuple[int, int, int, int]] = deque(maxlen=MAX_HELD_MARKS)
        self.held_marks_lost = 0  # evicted from a full hold buffer (counts in `count` skip)
        self._held_overflow_warned = False
        self._event_counts: dict[int, int] = {}
        self._euler: tuple[SbgEkfEuler, float] | None = None  # last valid EKF_EULER, its time
        self._hdt: tuple[SbgGnssHdt, float] | None = None  # last computed GPS1_HDT, its time
        self._last_imu_pub: float | None = None
        self._last_att_pub: float | None = None
        self._last_att_key: tuple[str | None, bool] | None = None
        self._att_held = False  # the latest attitude was not published (rate cap)
        self._pending_nav: set[str] = set()  # EKF_NAV sections waiting for the next epoch
        self._handlers: dict[str, Handler] = {
            "EKF_NAV": self._ekf_nav,
            "EKF_EULER": self._ekf_euler,
            "GPS1_POS": self._gps_pos,
            "GPS1_VEL": self._gps_vel,
            "GPS1_HDT": self._gps_hdt,
            "UTC_TIME": self._utc,
            "STATUS": self._status,
            "IMU_SHORT": self._imu,
            "IMU_DATA": self._imu,
            "GPS1_SAT": self._sats,
            "GPS1_RAW": self._gps_raw,
            **{f"EVENT_{c}": self._event for c in "ABCDE"},
        }

    def apply(self, frame: Frame) -> set[str]:
        identity = frame.identity
        if identity == "RTCM_RAW":  # no parsed model: the payload is the RTCM stream itself
            return self._rtcm_raw(frame.payload)
        handler = self._handlers.get(identity)
        if handler is None:
            return set()
        msg = frame.parsed()
        if msg is None:
            return set()
        stamp = _stamp_us(msg)
        if stamp is not None:
            self._note_stamp(stamp)
        return handler(msg, frame)

    def _note_stamp(self, stamp: int) -> None:
        last = self._last_stamp
        if last is None:
            self._last_stamp = stamp
            return
        delta = _signed_us(stamp - last)
        if abs(delta) > TIMELINE_JUMP_S * 1e6:
            log.warning("device time stamp jumped %.1f s: events re-anchored", delta / 1e6)
            self._timeline += 1
            self._utc_anchor = self._clock_anchor = None
            self._last_stamp = stamp
        elif delta > 0:  # EVENT logs date their first edge, a little behind the stream
            self._last_stamp = stamp

    def _ins(self) -> InsStatus:
        if self.state.ins is None:
            self.state.ins = InsStatus(vendor=VENDOR)
        return self.state.ins

    # ------------------------------------------------------------- navigation
    def _ekf_nav(self, m: SbgEkfNav, frame: Frame) -> set[str]:
        s = self.state
        p, a, v = s.position, s.accuracy, s.velocity
        p.invalid_llh = not m.position_valid
        if m.position_valid:
            p.lat, p.lon = m.lat, m.lon
            p.height_m, p.hmsl_m = m.height_hae, m.altitude_msl
        a.h_acc_m = math.hypot(m.pos_std[0], m.pos_std[1])
        a.v_acc_m = m.pos_std[2]
        a.s_acc_mps = math.hypot(*m.vel_std_ned)
        if m.velocity_valid:
            vn, ve, vd = m.vel_ned
            v.vel_n_mps, v.vel_e_mps, v.vel_d_mps = vn, ve, vd
            v.ground_speed_mps = math.hypot(vn, ve)
            v.heading_motion_deg = math.degrees(math.atan2(ve, vn)) % 360.0
        else:
            s.velocity = Velocity()
        fix_type = 3 if m.position_valid else (2 if m.mode >= EkfMode.NAV_VELOCITY else 0)
        s.fix.fix_type = fix_type
        s.fix.fix_type_name = FIX_TYPE_NAMES[fix_type]
        s.fix.gnss_fix_ok = m.position_valid
        changed = {"position", "accuracy", "velocity", "fix"}
        if self._carry_time(m.time_stamp_us):
            changed.add("time")
        ins = self._ins()
        if ins.mode != m.mode:
            ins.mode = m.mode
            ins.mode_name = EKF_MODE_NAMES.get(m.mode, f"mode{m.mode}")
            changed.add("ins")
        # EKF_NAV runs at the INS output rate (up to 200 Hz): its sections go out with the
        # decimated epoch, a change in between (the EKF mode, say) with the next one.
        self._pending_nav |= changed
        now = frame.t_mono
        if self._epoch_due(now):
            sections, self._pending_nav = self._pending_nav, set()
            if self._att_held and self._attitude_due(now):  # EKF_EULER stopped meanwhile
                self._last_att_pub, self._att_held = now, False
                sections.add("attitude")
            self.publish_sections(sections)  # sections first, then the epoch that includes them
        self.end_epoch(now)
        return set()

    def _epoch_due(self, now: float) -> bool:
        """Whether `end_epoch(now)` publishes `state.epoch` (the base class's decimation)."""
        last = self._last_epoch_pub
        return last is None or now - last >= 1.0 / self.nav_hz_cap - 1e-6

    def _gps_pos(self, m: SbgGnssPos, frame: Frame) -> set[str]:
        s = self.state
        pt = m.pos_type
        carr = 2 if pt == GnssPosType.RTK_INT else 1 if pt == GnssPosType.RTK_FLOAT else 0
        name = CARR_SOLN_NAMES[carr]
        diff = pt in DIFF_POS_TYPES
        s.fix.carr_soln, s.fix.carr_soln_name, s.fix.diff_soln = carr, name, diff
        r = s.rtk
        r.carr_soln, r.carr_soln_name, r.diff_soln = carr, name, diff
        r.ref_station_id = m.base_station_id
        r.corr_age_receiver_s = m.diff_age_s
        changed = {"fix", "rtk", "ins"}
        if m.num_sv_used is not None:
            s.fix.num_sv = m.num_sv_used
        if not self.sats_seen and (m.num_sv_used is not None or m.num_sv_tracked is not None):
            summary = s.sat_summary
            if m.num_sv_used is not None:
                summary.used = m.num_sv_used
            if m.num_sv_tracked is not None:
                summary.tracked = m.num_sv_tracked
            changed.add("sat_summary")
        ins = self._ins()
        ins.gnss_fix = pt
        ins.gnss_fix_name = POS_TYPE_NAMES.get(pt, f"type{pt}")
        if pt in RTK_POS_TYPES:
            self.rtk_seen = True
        return changed

    def _gps_vel(self, m: SbgGnssVel, frame: Frame) -> set[str]:
        vn, ve, vd = m.vel_ned
        self._ins().gnss_vel = (
            Velocity(
                vel_n_mps=vn,
                vel_e_mps=ve,
                vel_d_mps=vd,
                ground_speed_mps=math.hypot(vn, ve),
                heading_motion_deg=m.course_deg,
            )
            if m.solution_computed
            else None
        )
        return {"ins"}

    # ------------------------------------------------------------- attitude
    def _ekf_euler(self, m: SbgEkfEuler, frame: Frame) -> set[str]:
        self._euler = (m, frame.t_mono) if m.attitude_valid else None
        return self._compose_attitude(frame.t_mono)

    def _gps_hdt(self, m: SbgGnssHdt, frame: Frame) -> set[str]:
        r = self.state.rtk
        r.heading_deg = m.heading_deg
        r.acc_heading_deg = m.heading_acc_deg
        r.heading_valid = m.solution_computed
        r.baseline_m = m.baseline_m if m.baseline_valid else None
        self._hdt = (m, frame.t_mono) if m.solution_computed else None
        return {"rtk"} | self._compose_attitude(frame.t_mono)

    def _compose_attitude(self, now: float) -> set[str]:
        e = self._euler[0] if self._euler and now - self._euler[1] <= EULER_FRESH_S else None
        h = self._hdt[0] if self._hdt and now - self._hdt[1] <= HDT_FRESH_S else None
        att: Attitude | None = None
        if e is not None:
            att = Attitude(
                roll_deg=e.roll_deg,
                pitch_deg=e.pitch_deg,
                acc_roll_deg=e.euler_std_deg[0],
                acc_pitch_deg=e.euler_std_deg[1],
                source="sbg-ekf",
            )
            if e.heading_valid:
                att.heading_deg, att.acc_heading_deg = e.heading_deg, e.euler_std_deg[2]
        if h is not None and (att is None or att.heading_deg is None):
            att = att if att is not None else Attitude()
            att.heading_deg, att.acc_heading_deg = h.heading_deg, h.heading_acc_deg
            att.source = "sbg-gnss-hdt"
        if att is None and self.state.attitude is None:
            return set()
        self.state.attitude = att
        # EKF_EULER runs at the INS output rate (up to 200 Hz): publish at most
        # ATTITUDE_PUBLISH_HZ, but a change of source or heading validity at once.
        # A held one goes out with the next EKF_NAV epoch if no newer EKF_EULER comes.
        key = (att.source, att.heading_deg is not None) if att is not None else None
        if key == self._last_att_key and not self._attitude_due(now):
            self._att_held = True
            return set()
        self._last_att_pub, self._last_att_key, self._att_held = now, key, False
        return {"attitude"}

    def _attitude_due(self, now: float) -> bool:
        last = self._last_att_pub
        return last is None or now - last >= 1.0 / ATTITUDE_PUBLISH_HZ - 1e-6

    # ------------------------------------------------------------- time
    def _utc(self, m: SbgUtcTime, frame: Frame) -> set[str]:
        t = self.state.time
        t.valid_time = t.valid_date = t.fully_resolved = m.utc_valid
        t.valid_utc = m.utc_sync
        if not m.utc_valid or m.utc is None:
            # Events wait for the next valid UTC: after a reboot the device time stamp restarts
            # while UTC is still being acquired, so an older anchor would misdate them.
            self._utc_anchor = None
            if m.utc is not None and m.utc_status == UtcStatus.INITIALIZED:
                # The clock free-runs or steers (no PPS: a GNSS outage) while the EKF may still
                # navigate: its UTC keeps advancing and dates the epochs, flagged not valid.
                self._clock_anchor = (m.time_stamp_us, m.utc)
            self._carry_time(m.time_stamp_us)
            return {"time"}
        utc = m.utc
        t.utc = utc
        t.itow_ms = m.gps_tow_ms
        t.gps_tow_s = m.gps_tow_ms / 1000
        # During a leap second the log's second 60 is clamped to 59: GPS - UTC reads one too
        # many for that second, so keep the previous value.
        leap = None if m.leap_second_event else _leap_seconds(utc, m.gps_tow_ms)
        if leap is not None:
            t.leap_s = leap
        t.gps_week = gps_from_utc(utc, t.leap_s)[0]
        self._utc_anchor = self._clock_anchor = (m.time_stamp_us, utc)
        if self.raw_writer is not None:
            self.raw_writer.note_utc(utc)
        if self.raw_capture is not None and self.raw_gnss_format == "unknown":
            # Only an opaque stream is captured: on a UBX stream RawCapture would open (and
            # sidecar) an empty hour file at every hour.
            self.raw_capture.note_utc(utc)
        dropped = 0
        while self._held_marks:
            timeline, channel, count, stamp = self._held_marks.popleft()
            if timeline == self._timeline and self._near_anchor(stamp, HELD_MAX_S):
                self.push_time_mark(self._mark(channel, count, stamp, held=True))
            else:
                dropped += 1
        self._held_overflow_warned = False
        if dropped:
            log.warning(
                "%d held event(s) dropped: no anchor within %.0f s on their device timeline",
                dropped,
                HELD_MAX_S,
            )
        return {"time"}

    def _carry_time(self, stamp_us: int) -> bool:
        """Date the epoch at `stamp_us` from the unit's clock: the last usable UTC_TIME carried
        forward on the device time stamp. With none within `ANCHOR_MAX_S` the time is cleared
        rather than left stale. True when `time` changed."""
        t = self.state.time
        anchor = self._clock_anchor
        if anchor is not None:
            delta_us = _signed_us(stamp_us - anchor[0])
            if abs(delta_us) <= ANCHOR_MAX_S * 1e6:
                utc = anchor[1] + timedelta(microseconds=delta_us)
                week, tow = gps_from_utc(utc, t.leap_s)
                t.utc, t.gps_week, t.gps_tow_s, t.itow_ms = utc, week, tow, round(tow * 1000)
                return True
        if t.utc is None and t.itow_ms is None:
            return False
        t.utc = t.itow_ms = t.gps_tow_s = None
        t.valid_time = t.valid_date = t.fully_resolved = False
        return True

    def _near_anchor(self, stamp_us: int, bound_s: float) -> bool:
        return self._utc_anchor is not None and (
            abs(_signed_us(stamp_us - self._utc_anchor[0])) <= bound_s * 1e6
        )

    def _event(self, m: SbgEvent, frame: Frame) -> set[str]:
        channel = ord(m.channel) - ord("A")
        if m.overflow:
            log.warning("EVENT_%s: events lost (window overflow)", m.channel)
        for offset in (0, *m.offsets_us):  # VERIFY: offsets are from the log's own time stamp
            count = self._event_counts.get(channel, 0) + 1
            self._event_counts[channel] = count
            stamp = (m.timestamp_us + offset) % U32
            if self._near_anchor(stamp, ANCHOR_MAX_S):
                self.push_time_mark(self._mark(channel, count, stamp))
                continue
            if len(self._held_marks) == MAX_HELD_MARKS:  # the deque evicts the oldest
                self.held_marks_lost += 1
                if not self._held_overflow_warned:
                    self._held_overflow_warned = True
                    log.warning(
                        "more than %d events held without a UTC anchor: the oldest are dropped",
                        MAX_HELD_MARKS,
                    )
            self._held_marks.append((self._timeline, channel, count, stamp))
        return set()

    def _mark_utc(self, stamp_us: int) -> datetime:
        assert self._utc_anchor is not None
        anchor_us, anchor_utc = self._utc_anchor
        return anchor_utc + timedelta(microseconds=_signed_us(stamp_us - anchor_us))

    def _mark(self, channel: int, count: int, stamp_us: int, *, held: bool = False) -> TimeMark:
        """A held mark was dated after the fact, across a stretch with no valid UTC: it is not
        `utc_based` (UTC was not available when the edge happened) and its accuracy grows with
        its distance from the anchor."""
        assert self._utc_anchor is not None
        rising = self._mark_utc(stamp_us)
        week, tow = gps_from_utc(rising, self.state.time.leap_s)
        gap_s = abs(_signed_us(stamp_us - self._utc_anchor[0])) / 1e6
        return TimeMark(
            channel=channel,
            count=count,
            rising_week=week,
            rising_tow_s=tow,
            new_rising=True,
            time_base=1,  # week / tow are GPS time
            utc_based=not held,
            acc_est_ns=round(gap_s * HELD_DRIFT * 1e9) if held else 0,
            rising_utc=rising,
        )

    # ------------------------------------------------------------- satellites
    def _sats(self, m: SbgSatList, frame: Frame) -> set[str]:
        sats: list[Satellite] = []
        for sv in m.sats:
            gnss_id = GNSS_ID.get(sv.constellation)
            if gnss_id is None:
                continue
            sv_id = sv.id
            if gnss_id == 1 and sv_id < SBAS_PRN_OFFSET:
                sv_id += SBAS_PRN_OFFSET
            signals = sorted(
                (
                    Signal(
                        # SBG's own signal numbering (signals.py), not the u-blox sigId the UBX
                        # path carries: consumers key on `name`, which is the core spelling.
                        sig_id=g.id,
                        name=signal_name(g.id),
                        cno=g.snr or 0,
                        health=g.health,
                        pr_used=g.used,
                    )
                    for g in sv.signals
                ),
                key=lambda g: g.sig_id,
            )
            sats.append(
                Satellite(
                    gnss_id=gnss_id,
                    gnss=GNSS_NAMES[gnss_id],
                    sv_id=sv_id,
                    cno=max((g.cno for g in signals), default=0),
                    elev=sv.elevation if -90 <= sv.elevation <= 90 else None,
                    azim=sv.azimuth if 0 <= sv.azimuth <= 360 else None,
                    used=sv.used,
                    health=sv.health,
                    signals=signals,
                )
            )
        sats.sort(key=lambda x: (x.gnss_id, x.sv_id))
        per: dict[str, dict[str, int]] = {}
        for x in sats:
            bucket = per.setdefault(x.gnss, {"tracked": 0, "used": 0})
            bucket["tracked"] += 1
            bucket["used"] += int(x.used)
        self.state.sats = sats
        self.state.sat_summary = SatSummary(
            tracked=len(sats), used=sum(int(x.used) for x in sats), per_gnss=per
        )
        self.sats_seen = True
        return {"sats", "sat_summary"}

    # ------------------------------------------------------------- health / IMU
    def _status(self, m: SbgStatus, frame: Frame) -> set[str]:
        ins = self._ins()
        general, aiding = m.general, m.aiding
        ins.general_ok = {k: bool(general & flag) for k, flag in GENERAL_FLAGS.items()}
        ins.aiding = {k: bool(aiding & flag) for k, flag in AIDING_FLAGS.items()}
        ins.uptime_s = m.uptime_s
        ins.cpu_pct = m.cpu_usage
        ins.com_status = m.com_status
        return {"ins"}

    def _imu(self, m: SbgImuShort | SbgImuLegacy, frame: Frame) -> set[str]:
        self.state.imu = ImuSample(
            accel_mps2=m.accel_mps2,
            gyro_radps=m.gyro_radps,
            temperature_c=m.temperature_c,
            timestamp_us=m.time_stamp_us,
        )
        now, last = frame.t_mono, self._last_imu_pub
        if last is not None and now - last < 1.0 / IMU_PUBLISH_HZ - 1e-6:
            return set()
        self._last_imu_pub = now
        return {"imu"}

    # ------------------------------------------------------------- raw GNSS
    def _gps_raw(self, m: SbgGnssRaw, frame: Frame) -> set[str]:
        if not self.raw_gnss:
            return set()
        if self.raw_gnss_format == "unknown":
            if self.raw_capture is not None:
                self.raw_capture.write(m.data)
            return set()
        deciding = self.raw_gnss_format == "unknown-yet"
        if deciding:
            self._probe += m.data
        ubx = [f for f in self._ubx.feed(m.data) if f.proto is Proto.UBX]
        if deciding:
            if ubx:
                self.raw_gnss_format = "ubx"
                self._probe.clear()
            elif len(self._probe) >= RAW_DETECT_BYTES:
                self._not_ubx()
                return set()
        for u in ubx:
            if u.identity == "RXM-RAWX":
                self.state.raw_epochs += 1
            for topic in topics_for(u):
                self.bus.publish(topic, u)
        return set()

    def _not_ubx(self) -> None:
        self.raw_gnss_format = "unknown"
        log.warning("%s (first bytes %s)", NOT_UBX_MESSAGE, bytes(self._probe[:16]).hex())
        self.bus.publish("receiver.error", NOT_UBX_MESSAGE)
        if self.raw_capture is not None:
            self.raw_capture.write(bytes(self._probe))
            last = self._last_stamp
            if last is not None and self._near_anchor(last, ANCHOR_MAX_S):
                # Name the hour now from a recent valid UTC, carried to the latest time stamp.
                self.raw_capture.note_utc(self._mark_utc(last))
        self._probe = bytearray()

    # ------------------------------------------------------------- RTCM echo
    def _rtcm_raw(self, payload: bytes) -> set[str]:
        frames = sum(1 for f in self._rtcm_echo.feed(payload) if f.proto is Proto.RTCM3)
        if not frames:
            return set()
        self.state.rtk.rtcm_rx_total += frames
        self.rtcm_echo_seen = True
        return {"rtk"}
