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
    FIX_TYPE_NAMES,
    GNSS_NAMES,
    JAMMING_STATE_NAMES,
    Dops,
    Firmware,
    Hardware,
    PortStats,
    ReceiverState,
    RfBlock,
    RtcmMsgStats,
    Satellite,
    SatSummary,
    Signal,
    Spectrum,
    SurveyIn,
    signal_name,
)

log = logging.getLogger(__name__)

Handler = Callable[[Any], set[str]]

RTCM_RATE_WINDOW_S = 5.0


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
            "MON-HW": self._mon_hw,
            "MON-RF": self._mon_rf,
            "MON-SPAN": self._mon_span,
            "MON-COMMS": self._mon_comms,
            "MON-VER": self._mon_ver,
        }

    # ------------------------------------------------------------------ public
    def apply(self, frame: Frame) -> set[str]:
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
            changed = handler(frame.parsed())
        except Exception:  # a malformed message must never kill the daemon
            log.exception("failed to apply %s", identity)
            return set()
        for section in changed:
            self._publish(f"state.{section}", getattr(self.state, section))
        return changed

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
            base = datetime(m.year, m.month, m.day, m.hour, m.min, m.second, tzinfo=UTC)
            s.time.utc = base + timedelta(microseconds=round(m.nano / 1000))
        return {"position", "accuracy", "dops", "fix", "velocity", "time"}

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
        self.state.epoch_count += 1
        self.state.last_epoch_mono = time.monotonic()
        # A deep copy, not the live state: consumers (the Phase 2 sampler, the WS snapshot)
        # queue the epoch and read it later, by which time `self.state` has moved on.
        self._publish("state.epoch", self.state.model_copy(deep=True))
        return set()

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
