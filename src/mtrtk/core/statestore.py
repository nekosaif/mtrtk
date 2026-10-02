"""Applies parsed frames to a ReceiverState and publishes changed sections on the bus."""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame, Proto
from mtrtk.core.state import (
    ANT_POWER_NAMES,
    ANT_STATUS_NAMES,
    CARR_SOLN_NAMES,
    CORR_AGE_CODE_S,
    FIX_TYPE_NAMES,
    GNSS_NAMES,
    JAMMING_STATE_NAMES,
    MAX_TIME_MARKS,
    Dops,
    Firmware,
    Hardware,
    PortStats,
    ReceiverState,
    RfBlock,
    RtcmMsgStats,
    RtcmRxStats,
    Satellite,
    SatSummary,
    Signal,
    Spectrum,
    SurveyIn,
    TimeMark,
    signal_name,
)

log = logging.getLogger(__name__)

Handler = Callable[[Any], set[str]]

RTCM_RATE_WINDOW_S = 5.0
# The NAV-* messages that carry the epoch's iTOW, for a stream with no NAV-EOE to close it.
# Epochs in a row that end without a NAV-EOE before a stream that had it is taken to have
# stopped carrying it: one is a lost frame, a second a stream without NAV-EOE.
EOE_MISSED_LIMIT = 2
EPOCH_NAV_MESSAGES = frozenset(
    {
        "NAV-PVT",
        "NAV-HPPOSLLH",
        "NAV-HPPOSECEF",
        "NAV-DOP",
        "NAV-STATUS",
        "NAV-CLOCK",
        "NAV-TIMEGPS",
        "NAV-TIMELS",
        "NAV-TIMEUTC",
        "NAV-SAT",
        "NAV-SIG",
        "NAV-SVIN",
        "NAV-RELPOSNED",
    }
)
GPS_EPOCH = datetime(1980, 1, 6, tzinfo=UTC)
DEFAULT_LEAP_S = 18  # GPS-UTC since 2017-01-01, used until the receiver reports its own


def gps_to_utc(week: int, tow_s: float, leap_s: int | None) -> datetime:
    """GPS week + time of week -> UTC. `leap_s` None falls back to DEFAULT_LEAP_S."""
    leap = leap_s if leap_s is not None else DEFAULT_LEAP_S
    return GPS_EPOCH + timedelta(weeks=week, seconds=tow_s - leap)


def gps_from_utc(utc: datetime, leap_s: int | None) -> tuple[int, float]:
    """UTC -> (GPS week, time of week in s), the inverse of `gps_to_utc`. `leap_s` None falls
    back to DEFAULT_LEAP_S. A naive datetime is taken as UTC."""
    leap = leap_s if leap_s is not None else DEFAULT_LEAP_S
    aware = utc if utc.tzinfo is not None else utc.replace(tzinfo=UTC)
    week, rest = divmod(aware - GPS_EPOCH + timedelta(seconds=leap), timedelta(weeks=1))
    return week, rest.total_seconds()


def _cstr(value: object) -> str:
    """Decode a fixed-width, NUL-padded u-blox string field."""
    if isinstance(value, bytes):
        return value.split(b"\x00", 1)[0].decode("ascii", "replace")
    return str(value).split("\x00", 1)[0]


class StateStore:
    def __init__(self, bus: Bus | None = None) -> None:
        self.bus = bus
        self.state = ReceiverState()
        self._rtcm_window: deque[tuple[float, int]] = deque()
        self._sat_epoch: dict[tuple[int, int], Satellite] = {}
        self._sat_itow: int | None = None
        self._clamped_second = False
        self._now_mono = time.monotonic()
        # Epoch-end inference, for streams that carry no NAV-EOE (see `_infer_epoch_end`).
        self._saw_eoe = False
        self._eoe_itow: int | None = None  # the iTOW the last NAV-EOE closed
        self._missed_eoe = 0  # epochs in a row that ended without their NAV-EOE
        self._open_itow: int | None = None  # the iTOW of the epoch being assembled
        self._open_whole = False  # its first message was seen (not joined mid-epoch)
        self._open_mono = self._now_mono  # when its latest NAV-* frame arrived
        self._inferred = False  # an epoch has been closed by inference (logged once)
        self._handlers: dict[str, Handler] = {
            "NAV-PVT": self._nav_pvt,
            "NAV-HPPOSLLH": self._nav_hpposllh,
            "NAV-HPPOSECEF": self._nav_hpposecef,
            "NAV-DOP": self._nav_dop,
            "NAV-STATUS": self._nav_status,
            "NAV-CLOCK": self._nav_clock,
            "NAV-TIMEGPS": self._nav_timegps,
            "NAV-TIMELS": self._nav_timels,
            "NAV-TIMEUTC": self._nav_timeutc,
            "NAV-SAT": self._nav_sat,
            "NAV-SIG": self._nav_sig,
            "NAV-SVIN": self._nav_svin,
            "NAV-EOE": self._nav_eoe,
            "NAV-RELPOSNED": self._nav_relposned,
            "RXM-RTCM": self._rxm_rtcm,
            "TIM-TM2": self._tim_tm2,
            "MON-HW": self._mon_hw,
            "MON-RF": self._mon_rf,
            "MON-SPAN": self._mon_span,
            "MON-COMMS": self._mon_comms,
            "MON-VER": self._mon_ver,
        }

    # ------------------------------------------------------------------ public
    def apply(self, frame: Frame, now_mono: float | None = None) -> set[str]:
        """Apply one frame. `now_mono` overrides the clock (tests, replays); default monotonic."""
        self._now_mono = now_mono if now_mono is not None else time.monotonic()
        if frame.proto is Proto.RTCM3:
            return self._rtcm(frame)
        if frame.proto is not Proto.UBX:
            return set()
        identity = frame.identity
        if identity == "RXM-RAWX":  # never parsed on the hot path
            self.state.raw_epochs += 1
            return set()
        handler = self._handlers.get(identity)
        if handler is None:
            return set()
        try:
            msg = frame.parsed()
            if identity in EPOCH_NAV_MESSAGES:
                self._infer_epoch_end(msg.iTOW, identity)
            changed = handler(msg)
        except Exception:  # a malformed message must never kill the daemon
            log.exception("failed to apply %s", identity)
            return set()
        for section in changed:
            self._publish(f"state.{section}", getattr(self.state, section))
        return changed

    def end_of_stream(self) -> None:
        """The stream ended (a replay reached EOF): close the epoch still being assembled.

        Only an inferred epoch is ever pending - with NAV-EOE in the stream there is nothing
        to close, so this is a no-op for a live receiver and for every NAV-EOE recording.
        """
        if not self._saw_eoe and self._open_itow is not None and self._open_whole:
            self._close_epoch(self._open_mono)
        self.reset_epoch_inference()

    def reset_epoch_inference(self) -> None:
        """The link dropped or came back: forget the epoch being assembled, unpublished.

        A disconnect cuts it mid-way, and a reconnect lands mid-epoch, so neither half is a
        whole epoch. The next one counts only if it opens with NAV-PVT, as on a first connect.
        """
        self._open_itow, self._open_whole = None, False
        self._missed_eoe = 0

    def note_rtcm_injected(self, now_mono: float | None = None) -> None:
        """Record that RTCM corrections were just written to the receiver (the NTRIP client)."""
        self.state.rtk.last_rtcm_mono = now_mono if now_mono is not None else time.monotonic()

    def _publish(self, topic: str, item: Any) -> None:
        if self.bus is not None:
            self.bus.publish(topic, item)

    # -------------------------------------------------------- nav / time handlers
    def _nav_pvt(self, m: Any) -> set[str]:
        s = self.state
        s.position.invalid_llh = bool(m.invalidLlh)
        if not m.invalidLlh:
            s.position.lat = m.lat
            s.position.lon = m.lon
            s.position.height_m = m.height / 1000
            s.position.hmsl_m = m.hMSL / 1000
        s.accuracy.h_acc_m = m.hAcc / 1000
        s.accuracy.v_acc_m = m.vAcc / 1000
        s.accuracy.t_acc_ns = m.tAcc
        s.accuracy.s_acc_mps = m.sAcc / 1000
        s.accuracy.head_acc_deg = m.headAcc
        s.dops.p = m.pDOP
        s.fix.fix_type = m.fixType
        s.fix.fix_type_name = FIX_TYPE_NAMES.get(m.fixType, f"fix{m.fixType}")
        s.fix.gnss_fix_ok = bool(m.gnssFixOk)
        s.fix.diff_soln = bool(m.diffSoln)
        s.fix.carr_soln = m.carrSoln
        s.fix.carr_soln_name = CARR_SOLN_NAMES.get(m.carrSoln, f"carr{m.carrSoln}")
        s.fix.num_sv = m.numSV
        s.fix.last_correction_age = m.lastCorrectionAge
        s.fix.psm_state = m.psmState
        s.rtk.carr_soln = m.carrSoln
        s.rtk.carr_soln_name = s.fix.carr_soln_name
        s.rtk.diff_soln = bool(m.diffSoln)
        s.rtk.corr_age_receiver_s = CORR_AGE_CODE_S.get(m.lastCorrectionAge)
        s.velocity.vel_n_mps = m.velN / 1000
        s.velocity.vel_e_mps = m.velE / 1000
        s.velocity.vel_d_mps = m.velD / 1000
        s.velocity.ground_speed_mps = m.gSpeed / 1000
        s.velocity.heading_motion_deg = m.headMot
        s.time.itow_ms = m.iTOW
        s.time.valid_date = bool(m.validDate)
        s.time.valid_time = bool(m.validTime)
        s.time.fully_resolved = bool(m.fullyResolved)
        if m.validDate and m.validTime:
            second = self._clamp_second(m.second)
            base = datetime(m.year, m.month, m.day, m.hour, m.min, second, tzinfo=UTC)
            s.time.utc = base + timedelta(microseconds=round(m.nano / 1000))
        return {"position", "accuracy", "dops", "fix", "velocity", "time", "rtk"}

    def _clamp_second(self, second: int) -> int:
        """u-blox documents NAV-PVT `sec` as 0..60: a leap second must not drop the epoch.

        `datetime` has no second 60, and the `ValueError` used to be caught by `apply()`, which
        threw the *whole* message away - position, fix and velocity with it.
        """
        if 0 <= second <= 59:
            return second
        if not self._clamped_second:
            self._clamped_second = True
            log.info("NAV-PVT second=%d outside 0..59 (leap second?); clamping", second)
        return min(max(second, 0), 59)

    def _nav_hpposllh(self, m: Any) -> set[str]:
        s = self.state
        s.position.invalid_llh = bool(m.invalidLlh)
        if m.invalidLlh:
            return {"position"}
        s.position.lat = m.lat
        s.position.lon = m.lon
        s.position.height_m = m.height / 1000
        s.position.hmsl_m = m.hMSL / 1000
        s.accuracy.h_acc_m = m.hAcc / 1000
        s.accuracy.v_acc_m = m.vAcc / 1000
        return {"position", "accuracy"}

    def _nav_hpposecef(self, m: Any) -> set[str]:
        if m.invalidEcef:
            return set()
        s = self.state
        s.position.ecef_x_m = m.ecefX / 100
        s.position.ecef_y_m = m.ecefY / 100
        s.position.ecef_z_m = m.ecefZ / 100
        s.accuracy.p_acc_m = m.pAcc / 1000
        return {"position", "accuracy"}

    def _nav_dop(self, m: Any) -> set[str]:
        self.state.dops = Dops(g=m.gDOP, p=m.pDOP, t=m.tDOP, v=m.vDOP, h=m.hDOP, n=m.nDOP, e=m.eDOP)
        return {"dops"}

    def _nav_status(self, m: Any) -> set[str]:
        s = self.state.fix
        s.ttff_ms = m.ttff
        s.uptime_ms = m.msss
        s.spoof_det_state = m.spoofDetState
        return {"fix"}

    def _nav_clock(self, m: Any) -> set[str]:
        t = self.state.time
        t.clk_bias_ns = m.clkB
        t.clk_drift_nsps = m.clkD
        t.t_acc_ns = m.tAcc
        t.f_acc_psps = m.fAcc
        return {"time"}

    def _nav_timegps(self, m: Any) -> set[str]:
        t = self.state.time
        if m.weekValid:
            t.gps_week = m.week
        if m.towValid:
            t.gps_tow_s = m.iTOW / 1000 + m.fTOW * 1e-9
        if m.leapSValid:
            t.leap_s = m.leapS
        t.t_acc_ns = m.tAcc
        return {"time"}

    def _nav_timels(self, m: Any) -> set[str]:
        t = self.state.time
        if m.validCurrLs:
            t.leap_s = m.currLs
            t.leap_source = m.srcOfCurrLs
        if m.validTimeToLsEvent:
            t.time_to_leap_event_s = m.timeToLsEvent
            t.leap_change = m.lsChange
        return {"time"}

    def _nav_timeutc(self, m: Any) -> set[str]:
        t = self.state.time
        t.valid_utc = bool(m.validUTC)
        t.utc_standard = m.utcStandard
        return {"time"}

    # ------------------------------------------------------------- satellites
    def _epoch_sats(self, itow: int) -> dict[tuple[int, int], Satellite]:
        if itow != self._sat_itow:
            self._sat_epoch = {}
            self._sat_itow = itow
        return self._sat_epoch

    def _sat_for(
        self, sats: dict[tuple[int, int], Satellite], gnss_id: int, sv_id: int
    ) -> Satellite:
        key = (gnss_id, sv_id)
        sat = sats.get(key)
        if sat is None:
            sat = Satellite(
                gnss_id=gnss_id, gnss=GNSS_NAMES.get(gnss_id, f"gnss{gnss_id}"), sv_id=sv_id
            )
            sats[key] = sat
        return sat

    def _nav_sat(self, m: Any) -> set[str]:
        sats = self._epoch_sats(m.iTOW)
        for i in range(1, m.numSvs + 1):
            sfx = f"_{i:02d}"
            sat = self._sat_for(sats, getattr(m, "gnssId" + sfx), getattr(m, "svId" + sfx))
            sat.cno = getattr(m, "cno" + sfx)
            elev = getattr(m, "elev" + sfx)
            azim = getattr(m, "azim" + sfx)
            sat.elev = elev if -90 <= elev <= 90 else None
            sat.azim = azim if 0 <= azim <= 360 else None
            sat.pr_res_m = getattr(m, "prRes" + sfx)
            sat.quality_ind = getattr(m, "qualityInd" + sfx)
            sat.used = bool(getattr(m, "svUsed" + sfx))
            sat.health = getattr(m, "health" + sfx)
            sat.diff_corr = bool(getattr(m, "diffCorr" + sfx))
            sat.smoothed = bool(getattr(m, "smoothed" + sfx))
            sat.orbit_source = getattr(m, "orbitSource" + sfx)
            sat.eph_avail = bool(getattr(m, "ephAvail" + sfx))
            sat.alm_avail = bool(getattr(m, "almAvail" + sfx))
        self._finalize_sats()
        return {"sats", "sat_summary"}

    def _nav_sig(self, m: Any) -> set[str]:
        sats = self._epoch_sats(m.iTOW)
        for i in range(1, m.numSigs + 1):
            sfx = f"_{i:02d}"
            gnss_id = getattr(m, "gnssId" + sfx)
            sig_id = getattr(m, "sigId" + sfx)
            sat = self._sat_for(sats, gnss_id, getattr(m, "svId" + sfx))
            sig = Signal(
                sig_id=sig_id,
                name=signal_name(gnss_id, sig_id),
                freq_id=getattr(m, "freqId" + sfx),
                cno=getattr(m, "cno" + sfx),
                pr_res_m=getattr(m, "prRes" + sfx),
                quality_ind=getattr(m, "qualityInd" + sfx),
                corr_source=getattr(m, "corrSource" + sfx),
                iono_model=getattr(m, "ionoModel" + sfx),
                health=getattr(m, "health" + sfx),
                pr_used=bool(getattr(m, "prUsed" + sfx)),
                cr_used=bool(getattr(m, "crUsed" + sfx)),
                do_used=bool(getattr(m, "doUsed" + sfx)),
            )
            sat.signals = sorted(
                [s for s in sat.signals if s.sig_id != sig_id] + [sig], key=lambda s: s.sig_id
            )
        self._finalize_sats()
        return {"sats", "sat_summary"}

    def _finalize_sats(self) -> None:
        sats = sorted(self._sat_epoch.values(), key=lambda s: (s.gnss_id, s.sv_id))
        per: dict[str, dict[str, int]] = {}
        for sat in sats:
            bucket = per.setdefault(sat.gnss, {"tracked": 0, "used": 0})
            bucket["tracked"] += 1
            bucket["used"] += int(sat.used)
        self.state.sats = sats
        self.state.sat_summary = SatSummary(
            tracked=len(sats), used=sum(int(s.used) for s in sats), per_gnss=per
        )

    # ------------------------------------------------------ survey-in / epochs
    def _nav_svin(self, m: Any) -> set[str]:
        # An idle TMODE still fills meanX/Y/Z and meanAcc, with sentinels (zeros and a ~95 km
        # accuracy). Reporting those as a mean position would put the base at the earth centre,
        # so they are only a position once the survey is running or has completed.
        started = bool(m.active) or bool(m.valid)
        self.state.survey_in = SurveyIn(
            active=bool(m.active),
            valid=bool(m.valid),
            dur_s=m.dur,
            obs=m.obs,
            mean_x_m=m.meanX / 100 + m.meanXHP / 10000 if started else None,
            mean_y_m=m.meanY / 100 + m.meanYHP / 10000 if started else None,
            mean_z_m=m.meanZ / 100 + m.meanZHP / 10000 if started else None,
            mean_acc_m=m.meanAcc / 10000 if started else None,
        )
        return {"survey_in"}

    def _nav_eoe(self, m: Any) -> set[str]:
        # From the first NAV-EOE on the stream closes its own epochs: inference stops, so an
        # epoch is never fired twice (once inferred, once by its NAV-EOE). It resumes only once
        # the stream has plainly stopped carrying NAV-EOE (see `_infer_epoch_end`).
        if not self._saw_eoe:
            self._saw_eoe = True
            self.reset_epoch_inference()
        self._eoe_itow = m.iTOW
        self._missed_eoe = 0
        self._close_epoch(self._now_mono)
        return set()

    def _infer_epoch_end(self, itow: int, identity: str) -> None:
        """A stream with no NAV-EOE (yet): a NAV-* message with a new iTOW closes the last epoch.

        Called before the new message is applied, so the copy published is the finished
        previous epoch - every NAV-* the receiver sent for it, not just its NAV-PVT. The very
        first epoch counts only when it opened with NAV-PVT, the first NAV message u-blox emits
        per epoch: a stream joined mid-epoch must not turn the tail it caught into an epoch.
        A recording with no NAV-PVT at all therefore loses its first epoch.

        This also runs on a live receiver until its first NAV-EOE (one not yet given the
        profile, or one that never is): its epochs are then published one epoch late, when the
        next one starts, but stamped with the time their own last NAV-* frame arrived.
        """
        if itow != self._open_itow:
            if self._open_itow is None:
                self._open_whole = identity == "NAV-PVT"
            elif self._saw_eoe:
                # NAV-EOE closes the epochs. One that ends without it was most likely lost (a
                # dropped frame) and is not fired. A second in a row means the stream no longer
                # carries NAV-EOE (a newer log joined to an older one, LOG_MESSAGES changed):
                # inference takes over, starting with the epoch just finished.
                if self._open_itow == self._eoe_itow:
                    self._missed_eoe = 0
                else:
                    self._missed_eoe += 1
                    if self._missed_eoe >= EOE_MISSED_LIMIT:
                        self._saw_eoe = False
                        self._missed_eoe = 0
                        log.info("NAV-EOE stopped coming: inferring epoch ends from the NAV-* iTOW")
                        if self._open_whole:
                            self._close_epoch(self._open_mono)
                self._open_whole = True
            elif self._open_whole:
                if not self._inferred:
                    self._inferred = True
                    log.info("no NAV-EOE in the stream: inferring epoch ends from the NAV-* iTOW")
                self._close_epoch(self._open_mono)
            else:
                self._open_whole = True  # every epoch after the first is seen from its start
            self._open_itow = itow
        self._open_mono = self._now_mono

    def _close_epoch(self, now_mono: float) -> None:
        """Publish the finished epoch; *now_mono* is when its last frame arrived."""
        self.state.epoch_count += 1
        self.state.last_epoch_mono = now_mono
        rtk = self.state.rtk
        if rtk.last_rtcm_mono is not None:
            rtk.corr_age_s = max(0.0, now_mono - rtk.last_rtcm_mono)
            self._publish("state.rtk", rtk)
        # A deep copy, not the live state: consumers (the Phase 2 sampler, the WS snapshot)
        # queue the epoch and read it later, by which time `self.state` has moved on.
        self._publish("state.epoch", self.state.model_copy(deep=True))

    # ------------------------------------------------------------- rover / RTK
    def _nav_relposned(self, m: Any) -> set[str]:
        # pyubx2 folds the 0.1 mm `relPosHP*` parts into `relPos*` (cm), so /100 keeps them.
        r = self.state.rtk
        r.rel_pos_n_m, r.rel_pos_e_m, r.rel_pos_d_m = (
            m.relPosN / 100,
            m.relPosE / 100,
            m.relPosD / 100,
        )
        r.baseline_m = m.relPosLength / 100
        r.heading_deg = m.relPosHeading
        r.heading_valid = bool(m.relPosHeadingValid)
        r.acc_n_m, r.acc_e_m, r.acc_d_m = m.accN / 1000, m.accE / 1000, m.accD / 1000
        r.acc_length_m = m.accLength / 1000
        r.acc_heading_deg = m.accHeading
        r.ref_station_id = m.refStationID
        r.rel_pos_valid = bool(m.relPosValid)
        r.is_moving = bool(m.isMoving)
        r.ref_pos_missing = bool(m.refPosMiss)
        r.ref_obs_missing = bool(m.refObsMiss)
        r.normalized = bool(m.relPosNormalized)
        r.carr_soln = m.carrSoln
        r.carr_soln_name = CARR_SOLN_NAMES.get(m.carrSoln, f"carr{m.carrSoln}")
        r.diff_soln = bool(m.diffSoln)
        return {"rtk"}

    def _rxm_rtcm(self, m: Any) -> set[str]:
        r = self.state.rtk
        r.rtcm_rx_total += 1
        if m.crcFailed:
            # The type and station fields of a frame that failed its CRC are as corrupt as the
            # rest: count it under a type already received intact, never open a row for it
            # (up to 4096 bogus types) and never take its station id.
            r.rtcm_crc_failed += 1
            known = r.rtcm_rx.get(int(m.msgType))
            if known is not None:
                known.count += 1
                known.crc_failed += 1
                known.last_seen_mono = self._now_mono
            return {"rtk"}
        st = r.rtcm_rx.setdefault(int(m.msgType), RtcmRxStats())
        st.count += 1
        st.last_seen_mono = self._now_mono
        if m.msgUsed == 2:  # 0 unknown, 1 not used, 2 used
            st.used += 1
        if m.refStation:
            r.ref_station_id = int(m.refStation)
        return {"rtk"}

    def _tim_tm2(self, m: Any) -> set[str]:
        if not (m.newRisingEdge or m.newFallingEdge):
            return set()
        rising_tow = m.towMsR / 1000 + m.towSubMsR / 1e9 if m.newRisingEdge else None
        falling_tow = m.towMsF / 1000 + m.towSubMsF / 1e9 if m.newFallingEdge else None
        # timeBase 2 means the week/tow are already UTC: no leap-second correction then.
        leap = 0 if m.timeBase == 2 else self.state.time.leap_s
        mark = TimeMark(
            channel=m.ch,
            count=m.count,
            rising_week=m.wnR if m.newRisingEdge else None,
            rising_tow_s=rising_tow,
            falling_week=m.wnF if m.newFallingEdge else None,
            falling_tow_s=falling_tow,
            new_rising=bool(m.newRisingEdge),
            new_falling=bool(m.newFallingEdge),
            time_base=m.timeBase,
            utc_based=bool(m.utc),
            acc_est_ns=m.accEst,
            # timeBase 0 is the receiver's own clock, not GNSS time: no UTC. timeBase 1 follows
            # CFG-TP-TIMEGRID_TP1, read as the default GPS grid (mtrtk never changes it).
            rising_utc=(
                gps_to_utc(m.wnR, rising_tow, leap)
                if rising_tow is not None and m.time and m.timeBase in (1, 2)
                else None
            ),
        )
        marks = self.state.time_marks
        marks.append(mark)
        if len(marks) > MAX_TIME_MARKS:
            del marks[: len(marks) - MAX_TIME_MARKS]
        if mark.new_rising:
            self._publish("state.time_mark", mark)  # the event: one per new rising edge
        # `state.time_marks` publishes the live list, trimmed in place by later marks: a consumer
        # that wants this mark subscribes to `state.time_mark`, not to the list.
        return {"time_marks"}

    # ---------------------------------------------------------------- monitor
    def _mon_hw(self, m: Any) -> set[str]:
        self.state.hardware = Hardware(
            ant_status=m.aStatus,
            ant_status_name=ANT_STATUS_NAMES.get(m.aStatus, str(m.aStatus)),
            ant_power=m.aPower,
            ant_power_name=ANT_POWER_NAMES.get(m.aPower, str(m.aPower)),
            noise_per_ms=m.noisePerMS,
            agc_cnt=m.agcCnt,
            jam_ind=m.jamInd,
            jamming_state=m.jammingState,
            jamming_state_name=JAMMING_STATE_NAMES.get(m.jammingState, str(m.jammingState)),
            rtc_calib=bool(m.rtcCalib),
            safe_boot=bool(m.safeBoot),
            xtal_absent=bool(m.xtalAbsent),
        )
        return {"hardware"}

    def _mon_rf(self, m: Any) -> set[str]:
        blocks: list[RfBlock] = []
        for i in range(1, m.nBlocks + 1):
            g = lambda name, i=i: getattr(m, f"{name}_{i:02d}")  # noqa: E731
            blocks.append(
                RfBlock(
                    block_id=g("blockId"),
                    jamming_state=g("jammingState"),
                    jamming_state_name=JAMMING_STATE_NAMES.get(
                        g("jammingState"), str(g("jammingState"))
                    ),
                    ant_status=g("antStatus"),
                    ant_status_name=ANT_STATUS_NAMES.get(g("antStatus"), str(g("antStatus"))),
                    ant_power=g("antPower"),
                    ant_power_name=ANT_POWER_NAMES.get(g("antPower"), str(g("antPower"))),
                    post_status=g("postStatus"),
                    noise_per_ms=g("noisePerMS"),
                    agc_cnt=g("agcCnt"),
                    jam_ind=g("jamInd"),
                    ofs_i=g("ofsI"),
                    mag_i=g("magI"),
                    ofs_q=g("ofsQ"),
                    mag_q=g("magQ"),
                )
            )
        self.state.rf = blocks
        return {"rf"}

    def _mon_span(self, m: Any) -> set[str]:
        spectra: list[Spectrum] = []
        for i in range(1, m.numRfBlocks + 1):
            sfx = f"_{i:02d}"
            spectra.append(
                Spectrum(
                    block_id=i - 1,
                    span_hz=getattr(m, "span" + sfx),
                    res_hz=getattr(m, "res" + sfx),
                    center_hz=getattr(m, "center" + sfx),
                    pga_db=getattr(m, "pga" + sfx),
                    bins=list(getattr(m, "spectrum" + sfx)),
                )
            )
        self.state.spectrum = spectra
        return {"spectrum"}

    def _mon_comms(self, m: Any) -> set[str]:
        ports: list[PortStats] = []
        for i in range(1, m.nPorts + 1):
            sfx = f"_{i:02d}"
            ports.append(
                PortStats(
                    port_id=getattr(m, "portId" + sfx),
                    tx_pending=getattr(m, "txPending" + sfx),
                    tx_bytes=getattr(m, "txBytes" + sfx),
                    tx_usage=getattr(m, "txUsage" + sfx),
                    tx_peak_usage=getattr(m, "txPeakUsage" + sfx),
                    rx_pending=getattr(m, "rxPending" + sfx),
                    rx_bytes=getattr(m, "rxBytes" + sfx),
                    rx_usage=getattr(m, "rxUsage" + sfx),
                    rx_peak_usage=getattr(m, "rxPeakUsage" + sfx),
                    overrun_errs=getattr(m, "overrunErrs" + sfx),
                    skipped=getattr(m, "skipped" + sfx),
                )
            )
        self.state.ports = ports
        return {"ports"}

    def _mon_ver(self, m: Any) -> set[str]:
        extensions = [_cstr(v) for k, v in sorted(m.__dict__.items()) if k.startswith("extension_")]

        def tagged(prefix: str) -> str:
            return next((e[len(prefix) :] for e in extensions if e.startswith(prefix)), "")

        self.state.firmware = Firmware(
            sw_version=_cstr(m.swVersion),
            hw_version=_cstr(m.hwVersion),
            fw_version=tagged("FWVER="),
            protver=tagged("PROTVER="),
            module=tagged("MOD="),
            extensions=extensions,
        )
        return {"firmware"}

    # --------------------------------------------------------------- rtcm out
    def _rtcm(self, frame: Frame) -> set[str]:
        st = self.state.rtcm_out
        per = st.messages.setdefault(frame.rtcm_type, RtcmMsgStats())
        per.count += 1
        per.bytes += len(frame.raw)
        per.last_seen_mono = frame.t_mono
        st.total_count += 1
        st.total_bytes += len(frame.raw)
        # Framing time, not a recorded timestamp: the capture carries none. A replay at
        # speed 0 therefore reports the rate it is being replayed at, not the recorded one.
        now = frame.t_mono
        self._rtcm_window.append((now, len(frame.raw)))
        while self._rtcm_window and now - self._rtcm_window[0][0] > RTCM_RATE_WINDOW_S:
            self._rtcm_window.popleft()
        st.bytes_per_s = sum(n for _, n in self._rtcm_window) / RTCM_RATE_WINDOW_S
        self._publish("state.rtcm_out", st)
        return {"rtcm_out"}
