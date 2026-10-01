"""Synthesize NMEA 0183 sentences from ReceiverState (the receiver itself emits UBX only).

Every sentence is built by hand rather than through pynmeagps' serializer: field widths, the
high-precision 7-decimal minutes and empty-versus-zero fields are part of what consumers
parse, so they are spelled out here. pynmeagps is what the tests parse the output back with.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Callable, Iterable
from datetime import datetime

from mtrtk.core.bus import Bus, Subscription
from mtrtk.core.crc import nmea_checksum
from mtrtk.core.state import FixInfo, ReceiverState, Satellite
from mtrtk.core.statestore import StateStore
from mtrtk.rover.sinks import NmeaSink

log = logging.getLogger(__name__)

MPS_TO_KNOTS = 1.943844
MPS_TO_KMH = 3.6
# system name -> (talker, NMEA 4.11 system id, signal id of the L1-band signal reported in GSV)
SYSTEM_TALKER: dict[str, tuple[str, int, int]] = {
    "GPS": ("GP", 1, 1),  # L1 C/A
    "GLONASS": ("GL", 2, 1),  # G1 C/A
    "Galileo": ("GA", 3, 7),  # E1
    "BeiDou": ("GB", 4, 1),  # B1I
    "QZSS": ("GQ", 5, 1),  # L1 C/A
}
# GGA quality -> RMC/VTG mode indicator
POS_MODE: dict[int, str] = {0: "N", 1: "A", 2: "D", 4: "R", 5: "F", 6: "E", 7: "M"}
SLOW_SENTENCES = frozenset({"GSA", "GSV", "ZDA"})
# Built from the attitude alone: a dual-antenna INS has a GNSS heading before its EKF aligns
# and reports a position, and a heading consumer (an autopilot) needs it then too.
ATTITUDE_SENTENCES = frozenset({"HDT", "PASHR"})
# Output order within an epoch: position first, the per-satellite blocks and ZDA last.
ALL_SENTENCES = ("GGA", "RMC", "GST", "VTG", "HDT", "PASHR", "GSA", "GSV", "ZDA")
SINK_RETRY_S = 10.0
GLONASS_MAX_SLOT = 32  # NAV-SAT reports 255 for a GLONASS satellite whose slot is unknown


def gga_quality(fix: FixInfo) -> int:
    """NMEA GGA quality indicator for a NAV-PVT fix.

    0 no fix, 1 GNSS, 2 differential, 4 RTK fixed, 5 RTK float, 6 dead reckoning only,
    7 time-only (TMODE fixed: the position is the one the operator entered).
    """
    if fix.fix_type == 0:
        return 0
    if fix.fix_type == 1:
        return 6
    if fix.fix_type == 5:
        return 7
    if fix.carr_soln == 2:
        return 4
    if fix.carr_soln == 1:
        return 5
    return 2 if fix.diff_soln else 1


def _quality(state: ReceiverState) -> int:
    """The quality the sentences report: `gga_quality`, but 0 when the receiver itself says the
    fix is not valid - `gnssFixOK` clear (outside its DOP/accuracy masks) or `invalidLlh` set.
    u-blox's own NMEA says quality 0 / status V there too."""
    if not state.fix.gnss_fix_ok or state.position.invalid_llh:
        return 0
    return gga_quality(state.fix)


def has_valid_fix(state: ReceiverState) -> bool:
    """A fix the receiver vouches for: what GGA would report with a quality above 0."""
    return _quality(state) > 0


def _sentence(body: str) -> bytes:
    return f"${body}*{nmea_checksum(body.encode('ascii')):02X}\r\n".encode("ascii")


def _dec(value: float | None, dp: int) -> str:
    """`value` to at most `dp` decimals, trailing zeros trimmed (`0.70` -> `0.7`); None -> ""."""
    if value is None or not math.isfinite(value):
        return ""
    text = f"{value:.{dp}f}"
    if "." in text:
        text = text.rstrip("0")
        if text.endswith("."):
            text += "0"
    return "0.0" if text in ("-0.0", "-0") else text


def _fixed(value: float | None, dp: int) -> str:
    return "" if value is None or not math.isfinite(value) else f"{value:.{dp}f}"


def _hms(t: datetime) -> str:
    return f"{t:%H%M%S}.{t.microsecond // 10000:02d}"  # centiseconds, truncated like the receiver


def _angle(value: float, deg_width: int) -> str:
    """Degrees -> NMEA `dddmm.mmmmmmm` (7-decimal minutes, ~0.2 mm); the caller adds N/S/E/W."""
    value = abs(value)
    deg = int(value)
    minutes = round((value - deg) * 60.0, 7)
    if minutes >= 60.0:  # 59.99999999 min rounds to 60: carry into the degrees
        deg, minutes = deg + 1, 0.0
    return f"{deg:0{deg_width}d}{minutes:010.7f}"


def _lat_lon(lat: float, lon: float, quality: int) -> str:
    """Empty with no valid fix: StateStore keeps the last coordinates, which are not current."""
    if not quality:
        return ",,,"
    ns, ew = "N" if lat >= 0 else "S", "E" if lon >= 0 else "W"
    return f"{_angle(lat, 2)},{ns},{_angle(lon, 3)},{ew}"


def _heading(deg: float) -> str:
    return _fixed(round(deg, 2) % 360.0, 2)  # rounded first: 359.996 is 0.00, never 360.00


def _has_position(state: ReceiverState) -> bool:
    p = state.position
    return p.lat is not None and p.lon is not None and state.time.utc is not None


def build_gga(state: ReceiverState) -> bytes | None:
    p, f, r = state.position, state.fix, state.rtk
    if p.lat is None or p.lon is None or state.time.utc is None:
        return None
    quality = _quality(state)
    if not quality:
        alt = sep = ""
    elif p.hmsl_m is not None:
        alt = _dec(p.hmsl_m, 3)
        sep = _dec(p.height_m - p.hmsl_m, 3) if p.height_m is not None else ""
    else:  # no geoid model: report the ellipsoidal height with a zero separation
        alt = _dec(p.height_m, 3)
        sep = "0.0" if p.height_m is not None else ""
    differential = quality in (2, 4, 5)
    age = _dec(r.corr_age_s, 1) if differential else ""
    station = f"{r.ref_station_id:04d}" if differential and r.ref_station_id is not None else ""
    return _sentence(
        f"GNGGA,{_hms(state.time.utc)},{_lat_lon(p.lat, p.lon, quality)},{quality},"
        f"{min(f.num_sv, 99):02d},{_dec(state.dops.h, 2)},{alt},M,{sep},M,{age},{station}"
    )


def build_rmc(state: ReceiverState) -> bytes | None:
    p, v, t = state.position, state.velocity, state.time.utc
    if p.lat is None or p.lon is None or t is None:
        return None
    quality = _quality(state)
    speed = v.ground_speed_mps * MPS_TO_KNOTS if v.ground_speed_mps is not None else None
    return _sentence(
        f"GNRMC,{_hms(t)},{'A' if quality else 'V'},{_lat_lon(p.lat, p.lon, quality)},"
        f"{_dec(speed, 3)},{_dec(v.heading_motion_deg, 2)},{t:%d%m%y},,,{POS_MODE[quality]},V"
    )


def build_gst(state: ReceiverState) -> bytes | None:
    a, t = state.accuracy, state.time.utc
    if a.h_acc_m is None or t is None:
        return None
    std_h = a.h_acc_m / math.sqrt(2.0)  # hAcc is the horizontal (2-axis) figure
    residuals = [s.pr_res_m for s in state.sats if s.used]
    rms = math.sqrt(sum(r * r for r in residuals) / len(residuals)) if residuals else None
    return _sentence(
        f"GNGST,{_hms(t)},{_dec(rms, 4)},{_dec(std_h, 4)},{_dec(std_h, 4)},0.0,"
        f"{_dec(std_h, 4)},{_dec(std_h, 4)},{_dec(a.v_acc_m, 4)}"
    )


def build_vtg(state: ReceiverState) -> bytes | None:
    v = state.velocity
    if v.ground_speed_mps is None:
        return None
    mode = POS_MODE[_quality(state)]
    knots = _dec(v.ground_speed_mps * MPS_TO_KNOTS, 3)
    kmh = _dec(v.ground_speed_mps * MPS_TO_KMH, 3)
    return _sentence(f"GNVTG,{_dec(v.heading_motion_deg, 2)},T,,M,{knots},N,{kmh},K,{mode}")


def build_zda(state: ReceiverState) -> bytes | None:
    t = state.time.utc
    if t is None:
        return None
    return _sentence(f"GNZDA,{_hms(t)},{t:%d},{t:%m},{t:%Y},00,00")


def build_hdt(state: ReceiverState) -> bytes | None:
    att = state.attitude
    if att is None or att.heading_deg is None:
        return None
    return _sentence(f"GNHDT,{_heading(att.heading_deg)},T")


def build_pashr(state: ReceiverState) -> bytes | None:
    att, t = state.attitude, state.time.utc
    if att is None or t is None or att.heading_deg is None:
        return None
    quality = _quality(state)  # the same fix status GGA reports in this epoch
    flag = 2 if quality == 4 else 1 if quality else 0
    return _sentence(
        f"PASHR,{_hms(t)},{_heading(att.heading_deg)},T,{_fixed(att.roll_deg, 2)},"
        f"{_fixed(att.pitch_deg, 2)},0.00,{_fixed(att.acc_roll_deg, 3)},"
        f"{_fixed(att.acc_pitch_deg, 3)},{_fixed(att.acc_heading_deg, 3)},{flag},1"
    )


def _nmea_sv(sat: Satellite) -> int | None:
    if sat.gnss == "GLONASS":
        return sat.sv_id + 64 if 1 <= sat.sv_id <= GLONASS_MAX_SLOT else None
    return sat.sv_id


def _by_system(sats: Iterable[Satellite]) -> dict[str, list[tuple[int, Satellite]]]:
    groups: dict[str, list[tuple[int, Satellite]]] = {}
    for sat in sats:
        sv = _nmea_sv(sat)
        if sat.gnss in SYSTEM_TALKER and sv is not None:
            groups.setdefault(sat.gnss, []).append((sv, sat))
    return {name: groups[name] for name in SYSTEM_TALKER if name in groups}


def build_gsa(state: ReceiverState) -> list[bytes]:
    out: list[bytes] = []
    f, d = state.fix, state.dops
    nav_mode = 3 if f.fix_type >= 3 else 2 if f.fix_type == 2 else 1
    dops = f"{_dec(d.p, 2)},{_dec(d.h, 2)},{_dec(d.v, 2)}"
    for system, sats in _by_system(state.sats).items():
        talker, system_id, _ = SYSTEM_TALKER[system]
        used = [f"{sv:02d}" for sv, sat in sats if sat.used][:12]
        ids = ",".join(used + [""] * (12 - len(used)))
        out.append(_sentence(f"{talker}GSA,A,{nav_mode},{ids},{dops},{system_id:X}"))
    return out


def _gsv_angles(sat: Satellite) -> str:
    """`elev,azim` for GSV: elevation 00-90 and azimuth 000-359, empty when unknown. NAV-SAT
    elevation is signed, and its azimuth means nothing once the elevation is out of range."""
    if sat.elev is None or not 0 <= sat.elev <= 90:
        return ","
    if sat.azim is None or not 0 <= sat.azim <= 360:
        return f"{sat.elev:02d},"
    return f"{sat.elev:02d},{sat.azim % 360:03d}"


def build_gsv(state: ReceiverState) -> list[bytes]:
    out: list[bytes] = []
    for system, sats in _by_system(state.sats).items():
        talker, _, signal_id = SYSTEM_TALKER[system]
        chunks = [sats[i : i + 4] for i in range(0, len(sats), 4)]
        for n, chunk in enumerate(chunks, start=1):
            groups = "".join(
                f",{sv:02d},{_gsv_angles(s)},{f'{s.cno:02d}' if s.cno else ''}" for sv, s in chunk
            )
            out.append(
                _sentence(f"{talker}GSV,{len(chunks)},{n},{len(sats):02d}{groups},{signal_id:X}")
            )
    return out


_SINGLE: dict[str, Callable[[ReceiverState], bytes | None]] = {
    "GGA": build_gga,
    "RMC": build_rmc,
    "GST": build_gst,
    "VTG": build_vtg,
    "ZDA": build_zda,
    "HDT": build_hdt,
    "PASHR": build_pashr,
}
_MULTI: dict[str, Callable[[ReceiverState], list[bytes]]] = {"GSA": build_gsa, "GSV": build_gsv}


def build_sentences(state: ReceiverState, wanted: set[str], include_slow: bool) -> list[bytes]:
    """The selected sentences for one epoch. Until there is a position only the attitude ones
    (HDT, PASHR) can go out, on an epoch whose attitude has a heading."""
    positioned = _has_position(state)
    out: list[bytes] = []
    for name in ALL_SENTENCES:
        if name not in wanted or (name in SLOW_SENTENCES and not include_slow):
            continue
        if not positioned and name not in ATTITUDE_SENTENCES:
            continue
        if name in _MULTI:
            out += _MULTI[name](state)
        else:
            sentence = _SINGLE[name](state)
            if sentence is not None:
                out.append(sentence)
    return out


class NmeaPublisher:
    """Per epoch: build the selected sentences and write them to every sink.

    GSA/GSV/ZDA go out at most every `slow_interval_s`. A sink that fails to start or to write
    is set aside and retried every `SINK_RETRY_S`; the others keep receiving. `run()` can be
    called again after it returns (the daemon's supervisor restarts consumers).
    """

    def __init__(
        self,
        bus: Bus,
        store: StateStore,
        sinks: list[NmeaSink],
        sentences: Iterable[str],
        slow_interval_s: float = 1.0,
    ) -> None:
        self.bus = bus
        self.store = store
        self.sinks = sinks
        self.sentences = {s.strip().upper() for s in sentences}
        self.slow_interval_s = slow_interval_s
        self.sub: Subscription = bus.subscribe("state.epoch", maxsize=20)
        self._sub_closed = False
        self._last_slow: float | None = None
        self._active: list[NmeaSink] = []
        self._failed: dict[int, float] = {}  # id(sink) -> monotonic time of the next retry
        self.sent = 0

    async def run(self, stop: asyncio.Event) -> None:
        if self._sub_closed:
            self.sub = self.bus.subscribe("state.epoch", maxsize=20)
            self._sub_closed = False
        self._active, self._failed = [], {}
        for sink in self.sinks:
            await self._start(sink)
        waiter = asyncio.create_task(self._close_on(stop), name="nmea-stop")
        try:
            async for _, state in self.sub:
                if stop.is_set():
                    break
                await self._publish(state)
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            self.stop()
            for sink in self._active:  # a failed sink was closed when it failed
                with contextlib.suppress(Exception):
                    await sink.close()
            self._active = []

    async def _close_on(self, stop: asyncio.Event) -> None:
        """A silent receiver must not wedge `run()`: closing the subscription ends the loop."""
        await stop.wait()
        self.stop()

    async def _publish(self, state: ReceiverState) -> None:
        now = time.monotonic()
        await self._retry_failed(now)
        slow = self._last_slow is None or now - self._last_slow >= self.slow_interval_s
        payload = b"".join(build_sentences(state, self.sentences, include_slow=slow))
        if not payload:
            return
        if slow:
            self._last_slow = now
        for sink in list(self._active):
            try:
                await sink.write(payload)
            except Exception as exc:
                log.warning("NMEA sink %s failed (%s); retrying in %.0fs", sink, exc, SINK_RETRY_S)
                self._active.remove(sink)
                self._failed[id(sink)] = now + SINK_RETRY_S
                with contextlib.suppress(Exception):
                    await sink.close()
        self.sent += 1

    async def _start(self, sink: NmeaSink) -> None:
        try:
            await sink.start()
        except Exception as exc:
            log.warning(
                "NMEA sink %s did not start (%s); retrying in %.0fs", sink, exc, SINK_RETRY_S
            )
            self._failed[id(sink)] = time.monotonic() + SINK_RETRY_S
            return
        self._failed.pop(id(sink), None)
        self._active.append(sink)

    async def _retry_failed(self, now: float) -> None:
        if not self._failed:
            return
        for sink in self.sinks:
            due = self._failed.get(id(sink))
            if due is not None and now >= due:
                await self._start(sink)

    def stop(self) -> None:
        if not self._sub_closed:
            self._sub_closed = True
            self.bus.unsubscribe(self.sub)
