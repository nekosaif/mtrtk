"""sbgECom log and command-response parsers (layouts from SBG-Systems/sbgECom 5.8.935-stable).

`parse(frame)` returns one of the dataclasses below, or `None` for an id it does not handle or a
payload too short for the mandatory fields. It never raises. Trailing fields that later firmware
versions appended are read while bytes remain; a missing one is `None`. All angles are degrees
(the wire carries radians for the EKF logs and degrees for the GNSS ones).
"""

from __future__ import annotations

import math
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import IntEnum, IntFlag
from typing import Any

from mtrtk.core.frames import Frame
from mtrtk.rover.drivers.sbg.ids import CLASS, CMD, LOG

ACCEL_LSB = 1048576.0  # IMU_SHORT delta velocity: LSB per m/s^2
GYRO_LSB_STD = 67108864.0  # IMU_SHORT delta angle: LSB per rad/s, standard range
GYRO_LSB_HIGH = 12304174.0  # ... high range, flagged per frame by status bit 10
TEMP_LSB = 256.0  # IMU_SHORT temperature: LSB per degC
U8_NA, U16_NA = 0xFF, 0xFFFF


# --- status bitfields -------------------------------------------------------------------------


class GeneralStatus(IntFlag):
    MAIN_POWER_OK = 1 << 0
    IMU_POWER_OK = 1 << 1
    GPS_POWER_OK = 1 << 2
    SETTINGS_OK = 1 << 3
    TEMPERATURE_OK = 1 << 4
    DATALOGGER_OK = 1 << 5
    CPU_OK = 1 << 6


class AidingStatus(IntFlag):
    GPS1_POS_RECV = 1 << 0
    GPS1_VEL_RECV = 1 << 1
    GPS1_HDT_RECV = 1 << 2
    GPS1_UTC_RECV = 1 << 3
    GPS2_POS_RECV = 1 << 4
    GPS2_VEL_RECV = 1 << 5
    GPS2_HDT_RECV = 1 << 6
    GPS2_UTC_RECV = 1 << 7
    MAG_RECV = 1 << 8
    ODO_RECV = 1 << 9


class ClockState(IntEnum):
    ERROR = 0
    FREE_RUNNING = 1
    STEERING = 2
    VALID = 3


class UtcStatus(IntEnum):
    INVALID = 0
    NO_LEAP_SEC = 1
    INITIALIZED = 2


class EkfMode(IntEnum):
    UNINITIALIZED = 0
    VERTICAL_GYRO = 1
    AHRS = 2
    NAV_VELOCITY = 3
    NAV_POSITION = 4


class EkfFlags(IntFlag):
    ATTITUDE_VALID = 1 << 4
    HEADING_VALID = 1 << 5
    VELOCITY_VALID = 1 << 6
    POSITION_VALID = 1 << 7
    VERT_REF_USED = 1 << 8
    MAG_REF_USED = 1 << 9
    GPS1_VEL_USED = 1 << 10
    GPS1_POS_USED = 1 << 11
    GPS1_HDT_USED = 1 << 13
    GPS2_VEL_USED = 1 << 14
    GPS2_POS_USED = 1 << 15
    GPS2_HDT_USED = 1 << 17
    ZUPT_USED = 1 << 26
    ALIGN_VALID = 1 << 27


class GnssSolStatus(IntEnum):
    """Bits 0-5 of the GNSS POS / VEL / HDT status."""

    SOL_COMPUTED = 0
    INSUFFICIENT_OBS = 1
    INTERNAL_ERROR = 2
    HEIGHT_LIMIT = 3  # VEL: velocity limit


class GnssPosType(IntEnum):
    NO_SOLUTION = 0
    UNKNOWN = 1
    SINGLE = 2
    PSRDIFF = 3
    SBAS = 4
    OMNISTAR = 5
    RTK_FLOAT = 6
    RTK_INT = 7
    PPP_FLOAT = 8
    PPP_INT = 9
    FIXED = 10


class GnssSignals(IntFlag):
    """Bits 12-29 of the GNSS POS status: signals used in the solution."""

    GPS_L1 = 1 << 12
    GPS_L2 = 1 << 13
    GPS_L5 = 1 << 14
    GLO_L1 = 1 << 15
    GLO_L2 = 1 << 16
    GLO_L3 = 1 << 17
    GAL_E1 = 1 << 18
    GAL_E5A = 1 << 19
    GAL_E5B = 1 << 20
    GAL_E5ALT = 1 << 21
    GAL_E6 = 1 << 22
    BDS_B1 = 1 << 23
    BDS_B2 = 1 << 24
    BDS_B3 = 1 << 25
    QZSS_L1 = 1 << 26
    QZSS_L2 = 1 << 27
    QZSS_L5 = 1 << 28
    QZSS_L6 = 1 << 29


class Constellation(IntEnum):
    UNKNOWN = 0
    GPS = 1
    GLONASS = 2
    GALILEO = 3
    BEIDOU = 4
    QZSS = 5
    SBAS = 6
    IRNSS = 7
    LBAND = 8


class SatTracking(IntEnum):
    UNKNOWN = 0
    SEARCHING = 1
    TRACKING_UNKNOWN = 2
    TRACKING_NOT_USED = 3
    TRACKING_REJECTED = 4
    TRACKING_USED = 5


class SatHealth(IntEnum):
    UNKNOWN = 0
    HEALTHY = 1
    UNHEALTHY = 2


IMU_GYROS_USE_HIGH_SCALE = 1 << 10
HDT_BASELINE_VALID = 1 << 6
SIGNAL_SNR_VALID = 1 << 5
EVENT_OVERFLOW = 1 << 0
_SIGNALS_MASK = 0x3FFFF000  # GNSS POS status bits 12-29
VERSION_SOFT_SCHEME = 1 << 31
_QUALIFIERS = ("dev", "alpha", "beta", "rc", "stable", "hotfix")


def ekf_mode(status: int) -> int:
    """Solution mode (bits 0-3 of an EKF status), an `EkfMode` value."""
    return status & 0x0F


def gnss_sol_status(status: int) -> int:
    return status & 0x3F


def gnss_pos_type(status: int) -> int:
    """Position type (bits 6-11 of a GNSS POS status), a `GnssPosType` value."""
    return (status >> 6) & 0x3F


def decode_version(v: int) -> str:
    """sbgVersion: bit 31 set = software scheme `major.minor.build-qualifier` (6/6/16 bits,
    qualifier in bits 28-30), else basic scheme `major.minor.rev.build` (one byte each)."""
    if v & VERSION_SOFT_SCHEME:
        q = (v >> 28) & 0x07
        qual = _QUALIFIERS[q] if q < len(_QUALIFIERS) else f"q{q}"
        return f"{(v >> 22) & 0x3F}.{(v >> 16) & 0x3F}.{v & 0xFFFF}-{qual}"
    return f"{(v >> 24) & 0xFF}.{(v >> 16) & 0xFF}.{(v >> 8) & 0xFF}.{v & 0xFF}"


def _deg(rad: float) -> float:
    return math.degrees(rad)


def _deg3(v: tuple[float, ...]) -> tuple[float, float, float]:
    return (_deg(v[0]), _deg(v[1]), _deg(v[2]))


def _f3(v: tuple[Any, ...]) -> tuple[float, float, float]:
    return (float(v[0]), float(v[1]), float(v[2]))


def _na(v: int, sentinel: int) -> int | None:
    return None if v == sentinel else v


# --- payload reader ---------------------------------------------------------------------------


class _Short(Exception):
    """Payload ended inside a mandatory field."""


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.d = data
        self.o = 0

    def left(self) -> int:
        return len(self.d) - self.o

    def take(self, fmt: str) -> tuple[Any, ...]:
        size = struct.calcsize("<" + fmt)
        if self.left() < size:
            raise _Short
        v = struct.unpack_from("<" + fmt, self.d, self.o)
        self.o += size
        return v

    def opt(self, fmt: str) -> tuple[Any, ...] | None:
        return self.take(fmt) if self.left() >= struct.calcsize("<" + fmt) else None


# --- logs -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SbgStatus:
    time_stamp_us: int
    general_status: int
    com_status2: int
    com_status: int
    aiding_status: int
    uptime_s: int | None
    cpu_usage: int | None  # percent

    @property
    def general(self) -> GeneralStatus:
        return GeneralStatus(self.general_status & 0x7F)

    @property
    def aiding(self) -> AidingStatus:
        return AidingStatus(self.aiding_status & 0x3FF)


@dataclass(frozen=True, slots=True)
class SbgUtcTime:
    time_stamp_us: int
    status: int
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int
    nanosecond: int
    gps_tow_ms: int
    clk_bias_std: float | None
    clk_sf_error_std: float | None
    clk_residual_error: float | None
    utc: datetime | None  # None when the fields do not form a date (unit not yet initialised)
    leap_second_event: bool  # second == 60 on the wire (clamped to 59 in `utc`)

    @property
    def clock_stable_input(self) -> bool:
        return bool(self.status & 1)

    @property
    def clock_state(self) -> int:
        return (self.status >> 1) & 0x0F

    @property
    def utc_sync(self) -> bool:
        return bool(self.status & (1 << 5))

    @property
    def utc_status(self) -> int:
        return (self.status >> 6) & 0x0F


@dataclass(frozen=True, slots=True)
class SbgImuShort:
    time_stamp_us: int
    status: int
    accel_mps2: tuple[float, float, float]
    gyro_radps: tuple[float, float, float]
    temperature_c: float

    @property
    def high_range(self) -> bool:
        return bool(self.status & IMU_GYROS_USE_HIGH_SCALE)


@dataclass(frozen=True, slots=True)
class SbgImuLegacy:
    time_stamp_us: int
    status: int
    accel_mps2: tuple[float, float, float]
    gyro_radps: tuple[float, float, float]
    temperature_c: float
    delta_vel: tuple[float, float, float]
    delta_angle: tuple[float, float, float]


class _EkfStatus:
    __slots__ = ()
    status: int

    @property
    def mode(self) -> int:
        return ekf_mode(self.status)

    @property
    def attitude_valid(self) -> bool:
        return bool(self.status & EkfFlags.ATTITUDE_VALID)

    @property
    def heading_valid(self) -> bool:
        return bool(self.status & EkfFlags.HEADING_VALID)

    @property
    def velocity_valid(self) -> bool:
        return bool(self.status & EkfFlags.VELOCITY_VALID)

    @property
    def position_valid(self) -> bool:
        return bool(self.status & EkfFlags.POSITION_VALID)

    @property
    def flags(self) -> EkfFlags:
        return EkfFlags(self.status & ~0x0F)  # bits 0-3 are the mode


@dataclass(frozen=True, slots=True)
class SbgEkfEuler(_EkfStatus):
    time_stamp_us: int
    roll_deg: float
    pitch_deg: float
    heading_deg: float  # yaw, wrapped to [0, 360)
    euler_std_deg: tuple[float, float, float]  # 1 sigma roll, pitch, yaw
    status: int
    mag_declination: float | None  # degrees
    mag_inclination: float | None  # degrees


@dataclass(frozen=True, slots=True)
class SbgEkfQuat(_EkfStatus):
    time_stamp_us: int
    quat: tuple[float, float, float, float]  # w, x, y, z
    euler_std_deg: tuple[float, float, float]
    status: int
    mag_declination: float | None
    mag_inclination: float | None


@dataclass(frozen=True, slots=True)
class SbgEkfNav(_EkfStatus):
    time_stamp_us: int
    vel_ned: tuple[float, float, float]  # m/s
    vel_std_ned: tuple[float, float, float]
    lat: float  # degrees
    lon: float
    altitude_msl: float  # m
    undulation: float  # m, geoid above ellipsoid
    height_hae: float  # altitude_msl + undulation
    pos_std: tuple[float, float, float]  # 1 sigma lat, lon, alt (m)
    status: int


@dataclass(frozen=True, slots=True)
class SbgGnssPos:
    gnss: int  # 1 or 2
    time_stamp_us: int
    status: int
    tow_ms: int
    lat: float
    lon: float
    altitude_msl: float
    undulation: float
    height_hae: float
    lat_acc: float  # 1 sigma m
    lon_acc: float
    alt_acc: float
    num_sv_used: int | None
    base_station_id: int | None
    diff_age_s: float | None
    num_sv_tracked: int | None
    status_ext: int | None
    nr_diag_reboots: int | None
    uptime_s: int | None

    @property
    def sol_status(self) -> int:
        return gnss_sol_status(self.status)

    @property
    def solution_computed(self) -> bool:
        return self.sol_status == GnssSolStatus.SOL_COMPUTED

    @property
    def pos_type(self) -> int:
        return gnss_pos_type(self.status)

    @property
    def rtk_fixed(self) -> bool:
        return self.pos_type == GnssPosType.RTK_INT

    @property
    def rtk_float(self) -> bool:
        return self.pos_type == GnssPosType.RTK_FLOAT

    @property
    def signals(self) -> GnssSignals:
        return GnssSignals(self.status & _SIGNALS_MASK)


@dataclass(frozen=True, slots=True)
class SbgGnssVel:
    gnss: int
    time_stamp_us: int
    status: int
    tow_ms: int
    vel_ned: tuple[float, float, float]
    vel_acc: tuple[float, float, float]
    course_deg: float
    course_acc_deg: float

    @property
    def solution_computed(self) -> bool:
        return gnss_sol_status(self.status) == GnssSolStatus.SOL_COMPUTED

    @property
    def vel_type(self) -> int:
        """0 NO_SOLUTION, 1 UNKNOWN, 2 DOPPLER, 3 DIFFERENTIAL."""
        return (self.status >> 6) & 0x3F


@dataclass(frozen=True, slots=True)
class SbgGnssHdt:
    gnss: int
    time_stamp_us: int
    status: int
    tow_ms: int
    heading_deg: float  # true
    heading_acc_deg: float
    pitch_deg: float
    pitch_acc_deg: float
    baseline_m: float | None  # meaningful only when `baseline_valid`
    num_sv_tracked: int | None
    num_sv_used: int | None

    @property
    def solution_computed(self) -> bool:
        return gnss_sol_status(self.status) == GnssSolStatus.SOL_COMPUTED

    @property
    def baseline_valid(self) -> bool:
        return bool(self.status & HDT_BASELINE_VALID)


@dataclass(frozen=True, slots=True)
class SbgEvent:
    channel: str  # "A".."E", "OUT_A", "OUT_B"
    timestamp_us: int
    status: int
    offsets_us: list[int]  # of the 2nd..5th events in the window, only the valid ones

    @property
    def overflow(self) -> bool:
        return bool(self.status & EVENT_OVERFLOW)


@dataclass(frozen=True, slots=True)
class SbgSignal:
    id: int
    tracking: int
    health: int
    snr: int | None  # dB-Hz, None when SNR_VALID is clear
    snr_valid: bool

    @property
    def used(self) -> bool:
        return self.tracking == SatTracking.TRACKING_USED


@dataclass(frozen=True, slots=True)
class SbgSat:
    id: int
    constellation: int
    elevation: int  # degrees
    azimuth: int  # degrees
    tracking: int
    health: int
    elevation_status: int  # 0 unknown, 1 setting, 2 rising
    signals: list[SbgSignal] = field(default_factory=list)

    @property
    def used(self) -> bool:
        return self.tracking == SatTracking.TRACKING_USED


@dataclass(frozen=True, slots=True)
class SbgSatList:
    gnss: int
    time_stamp_us: int
    sats: list[SbgSat]


@dataclass(frozen=True, slots=True)
class SbgGnssRaw:
    """GPS1_RAW / GPS2_RAW: an opaque slice of the internal receiver's native stream."""

    gnss: int
    data: bytes

    @property
    def is_ubx(self) -> bool:
        return b"\xb5\x62" in self.data


@dataclass(frozen=True, slots=True)
class SbgAck:
    msg_id: int
    msg_class: int
    error_code: int  # 0 = SBG_NO_ERROR

    @property
    def ok(self) -> bool:
        return self.error_code == 0


@dataclass(frozen=True, slots=True)
class SbgInfo:
    product_code: str
    serial_number: int
    calibration_rev: int
    calibration_date: date | None
    hardware_rev: str
    firmware_rev: str
    hardware_rev_raw: int
    firmware_rev_raw: int


# --- parsers ----------------------------------------------------------------------------------


def _status(r: _Reader) -> SbgStatus:
    ts, general, com2, com, aiding, _r2, _r3 = r.take("IHHIIIH")
    uptime = r.opt("I")
    cpu = r.opt("B")
    return SbgStatus(
        ts, general, com2, com, aiding, uptime[0] if uptime else None, cpu[0] if cpu else None
    )


def _utc(r: _Reader) -> SbgUtcTime:
    ts, status, year, month, day, hour, minute, second, ns, tow = r.take("IHHbbbbbiI")
    clk = r.opt("fff")
    try:
        utc: datetime | None = datetime(
            year, month, day, hour, minute, min(second, 59), max(ns, 0) // 1000, tzinfo=UTC
        )
    except ValueError:
        utc = None
    return SbgUtcTime(
        ts,
        status,
        year,
        month,
        day,
        hour,
        minute,
        second,
        ns,
        tow,
        clk[0] if clk else None,
        clk[1] if clk else None,
        clk[2] if clk else None,
        utc,
        second == 60,
    )


def _imu_short(r: _Reader) -> SbgImuShort:
    ts, status, dv0, dv1, dv2, da0, da1, da2, temp = r.take("IH3i3ih")
    gyro_lsb = GYRO_LSB_HIGH if status & IMU_GYROS_USE_HIGH_SCALE else GYRO_LSB_STD
    return SbgImuShort(
        ts,
        status,
        (dv0 / ACCEL_LSB, dv1 / ACCEL_LSB, dv2 / ACCEL_LSB),
        (da0 / gyro_lsb, da1 / gyro_lsb, da2 / gyro_lsb),
        temp / TEMP_LSB,
    )


def _imu_legacy(r: _Reader) -> SbgImuLegacy:
    v = r.take("IH3f3ff3f3f")
    return SbgImuLegacy(v[0], v[1], _f3(v[2:5]), _f3(v[5:8]), v[8], _f3(v[9:12]), _f3(v[12:15]))


def _mag_tail(r: _Reader) -> tuple[float | None, float | None]:
    tail = r.opt("ff")
    return (_deg(tail[0]), _deg(tail[1])) if tail else (None, None)


def _ekf_euler(r: _Reader) -> SbgEkfEuler:
    ts, roll, pitch, yaw, s0, s1, s2, status = r.take("I3f3fI")
    decl, incl = _mag_tail(r)
    return SbgEkfEuler(
        ts, _deg(roll), _deg(pitch), _deg(yaw) % 360.0, _deg3((s0, s1, s2)), status, decl, incl
    )


def _ekf_quat(r: _Reader) -> SbgEkfQuat:
    ts, w, x, y, z, s0, s1, s2, status = r.take("I4f3fI")
    decl, incl = _mag_tail(r)
    return SbgEkfQuat(ts, (w, x, y, z), _deg3((s0, s1, s2)), status, decl, incl)


def _ekf_nav(r: _Reader) -> SbgEkfNav:
    v = r.take("I3f3f3df3fI")
    alt, und = v[9], v[10]
    return SbgEkfNav(
        v[0], _f3(v[1:4]), _f3(v[4:7]), v[7], v[8], alt, und, alt + und, _f3(v[11:14]), v[14]
    )


def _gnss_pos(gnss: int, r: _Reader) -> SbgGnssPos:
    ts, status, tow, lat, lon, alt, und, lat_acc, lon_acc, alt_acc = r.take("IIIdddffff")
    sv_used = base_id = age = sv_tracked = status_ext = reboots = uptime = None
    v40 = r.opt("BHH")
    if v40:
        sv_used, base_id = _na(v40[0], U8_NA), _na(v40[1], U16_NA)
        age = None if v40[2] == U16_NA else v40[2] / 100
        v45 = r.opt("BI")
        if v45:
            sv_tracked, status_ext = _na(v45[0], U8_NA), v45[1]
            v56 = r.opt("BI")
            if v56:
                reboots, uptime = v56
    return SbgGnssPos(
        gnss,
        ts,
        status,
        tow,
        lat,
        lon,
        alt,
        und,
        alt + und,
        lat_acc,
        lon_acc,
        alt_acc,
        sv_used,
        base_id,
        age,
        sv_tracked,
        status_ext,
        reboots,
        uptime,
    )


def _gnss_vel(gnss: int, r: _Reader) -> SbgGnssVel:
    v = r.take("III3f3fff")
    return SbgGnssVel(gnss, v[0], v[1], v[2], _f3(v[3:6]), _f3(v[6:9]), v[9], v[10])


def _gnss_hdt(gnss: int, r: _Reader) -> SbgGnssHdt:
    ts, status, tow, hdg, hdg_acc, pitch, pitch_acc = r.take("IHIffff")
    baseline = r.opt("f")
    tracked = r.opt("B")
    used = r.opt("B")
    return SbgGnssHdt(
        gnss,
        ts,
        status,
        tow,
        hdg,
        hdg_acc,
        pitch,
        pitch_acc,
        baseline[0] if baseline else None,
        _na(tracked[0], U8_NA) if tracked else None,
        _na(used[0], U8_NA) if used else None,
    )


def _event(channel: str, r: _Reader) -> SbgEvent:
    ts, status, *offsets = r.take("IHHHHH")
    valid = [off for i, off in enumerate(offsets) if status & (1 << (i + 1))]
    return SbgEvent(channel, ts, status, valid)


def _sat_list(gnss: int, r: _Reader) -> SbgSatList:
    ts, _reserved, count = r.take("IIB")
    sats = []
    for _ in range(count):
        sid, elev, azim, flags, nsig = r.take("BbHHB")
        signals = []
        for _ in range(nsig):
            gid, gflags, snr = r.take("BBB")
            snr_valid = bool(gflags & SIGNAL_SNR_VALID)
            signals.append(
                SbgSignal(
                    gid, gflags & 0x07, (gflags >> 3) & 0x03, snr if snr_valid else None, snr_valid
                )
            )
        sats.append(
            SbgSat(
                sid,
                (flags >> 7) & 0x0F,
                elev,
                azim,
                flags & 0x07,
                (flags >> 3) & 0x03,
                (flags >> 5) & 0x03,
                signals,
            )
        )
    return SbgSatList(gnss, ts, sats)


def _ack(r: _Reader) -> SbgAck:
    return SbgAck(*r.take("BBH"))


def _info(r: _Reader) -> SbgInfo:
    (code,) = r.take("32s")
    serial, cal_rev, year, month, day, hw, fw = r.take("IIHBBII")
    try:
        cal: date | None = date(year, month, day)
    except ValueError:
        cal = None
    return SbgInfo(
        code.split(b"\x00", 1)[0].decode("ascii", "replace").strip(),
        serial,
        cal_rev,
        cal,
        decode_version(hw),
        decode_version(fw),
        hw,
        fw,
    )


def _bind(fn: Callable[[Any, _Reader], Any], arg: Any) -> Callable[[_Reader], Any]:
    return lambda r: fn(arg, r)


LOG_PARSERS: dict[int, Callable[[_Reader], Any]] = {
    LOG["STATUS"]: _status,
    LOG["UTC_TIME"]: _utc,
    LOG["IMU_DATA"]: _imu_legacy,
    LOG["IMU_SHORT"]: _imu_short,
    LOG["EKF_EULER"]: _ekf_euler,
    LOG["EKF_QUAT"]: _ekf_quat,
    LOG["EKF_NAV"]: _ekf_nav,
    LOG["GPS1_POS"]: _bind(_gnss_pos, 1),
    LOG["GPS2_POS"]: _bind(_gnss_pos, 2),
    LOG["GPS1_VEL"]: _bind(_gnss_vel, 1),
    LOG["GPS2_VEL"]: _bind(_gnss_vel, 2),
    LOG["GPS1_HDT"]: _bind(_gnss_hdt, 1),
    LOG["GPS2_HDT"]: _bind(_gnss_hdt, 2),
    LOG["GPS1_SAT"]: _bind(_sat_list, 1),
    LOG["GPS2_SAT"]: _bind(_sat_list, 2),
    LOG["GPS1_RAW"]: lambda r: SbgGnssRaw(1, r.d),
    LOG["GPS2_RAW"]: lambda r: SbgGnssRaw(2, r.d),
    **{LOG[f"EVENT_{c}"]: _bind(_event, c) for c in "ABCDE"},
    LOG["EVENT_OUT_A"]: _bind(_event, "OUT_A"),
    LOG["EVENT_OUT_B"]: _bind(_event, "OUT_B"),
}
CMD_PARSERS: dict[int, Callable[[_Reader], Any]] = {CMD["ACK"]: _ack, CMD["INFO"]: _info}
_BY_CLASS = {CLASS["LOG_ECOM_0"]: LOG_PARSERS, CLASS["CMD_0"]: CMD_PARSERS}


def parse_payload(msg_class: int, msg_id: int, payload: bytes) -> Any:
    """Parse one sbgECom payload; `None` for an unhandled id or a truncated mandatory part."""
    fn = _BY_CLASS.get(msg_class, {}).get(msg_id)
    if fn is None:
        return None
    try:
        return fn(_Reader(payload))
    except (_Short, struct.error):
        return None


def parse(frame: Frame) -> Any:
    """The `register_parser` entry point for `Proto.SBG` frames."""
    return parse_payload(frame.raw[3], frame.raw[2], frame.payload)
