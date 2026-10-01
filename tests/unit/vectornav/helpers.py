"""Hand-built VectorNav binary frames, byte for byte per the field table in `fields.py`."""

from __future__ import annotations

import struct
from collections.abc import Iterable, Sequence

from mtrtk.core.frames import Frame, Proto
from mtrtk.rover.drivers.vectornav.checksum import crc16_xmodem

# group bit indices
COMMON, TIME, IMU, GPS, ATTITUDE, INS, GPS2 = range(7)


def build_binary(groups: dict[int, tuple[int, bytes, int | None]]) -> bytes:
    """group bit -> (field mask, payload bytes for that group, GPS extension mask or None).

    The extension mask is written after the field word (whose bit 15 is then set) and the
    caller's payload must already end with the extension fields.
    """
    group_byte = 0
    header = b""
    payload = b""
    for g in sorted(groups):
        mask, data, ext = groups[g]
        group_byte |= 1 << g
        if ext is not None:
            header += struct.pack("<HH", mask | 0x8000, ext)
        else:
            header += struct.pack("<H", mask)
        payload += data
    body = bytes([group_byte]) + header + payload
    return b"\xfa" + body + crc16_xmodem(body).to_bytes(2, "big")


def frame(raw: bytes, t_mono: float = 0.0) -> Frame:
    return Frame(Proto.VN, raw, t_mono, 0.0)


def utc_bytes(year: int, month: int, day: int, hour: int, minute: int, sec: int, ms: int) -> bytes:
    return struct.pack("<bBBBBBH", year, month, day, hour, minute, sec, ms)


def time_group_payload(
    *,
    time_gps_ns: int = 1_000_000_000,
    gps_tow_ns: int = 123_456_789_000,
    gps_week: int = 2385,
    time_sync_in_ns: int = 0,
    utc: tuple[int, int, int, int, int, int, int] = (26, 9, 19, 10, 30, 15, 250),
    sync_in_cnt: int = 0,
    time_status: int = 0x07,
) -> tuple[int, bytes]:
    """The default VN-200 profile's Time group (0x02DE)."""
    data = (
        struct.pack("<QQHQ", time_gps_ns, gps_tow_ns, gps_week, time_sync_in_ns)
        + utc_bytes(*utc)
        + struct.pack("<IB", sync_in_cnt, time_status)
    )
    return 0x02DE, data


def ins_status(mode: int = 2, gps_fix: bool = True, **errors: bool) -> int:
    bits = {"time_error": 3, "imu_error": 4, "mag_pres_error": 5, "gps_error": 6}
    value = (mode & 0x3) | (0x4 if gps_fix else 0)
    for name, on in errors.items():
        if on:
            value |= 1 << bits[name]
    return value


def ins_payload(
    *,
    status: int | None = None,
    pos_lla: tuple[float, float, float] = (23.7806, 90.4071, 12.5),
    vel_ned: tuple[float, float, float] = (1.0, 0.5, -0.1),
    pos_u: float = 0.02,
    vel_u: float = 0.05,
) -> tuple[int, bytes]:
    """The default profile's INS group (0x0613): InsStatus, PosLla, VelNed, PosU, VelU."""
    st = ins_status() if status is None else status
    data = (
        struct.pack("<H", st)
        + struct.pack("<3d", *pos_lla)
        + struct.pack("<3f", *vel_ned)
        + struct.pack("<ff", pos_u, vel_u)
    )
    return 0x0613, data


def att_payload(
    *,
    vpe_status: int = 0,
    ypr: tuple[float, float, float] = (91.0, -1.0, 0.5),
    ypr_u: tuple[float, float, float] = (0.8, 0.1, 0.12),
) -> tuple[int, bytes]:
    """The default profile's Attitude group (0x0103): VpeStatus, YawPitchRoll, YprU."""
    return 0x0103, struct.pack("<H3f3f", vpe_status, *ypr, *ypr_u)


def imu_payload(
    *,
    imu_status: int = 0,
    temp_c: float = 31.5,
    accel: tuple[float, float, float] = (0.1, -0.2, -9.81),
    gyro: tuple[float, float, float] = (0.001, 0.002, -0.003),
) -> tuple[int, bytes]:
    """The default profile's IMU group (0x0611): ImuStatus, Temp, Accel, AngularRate."""
    return 0x0611, struct.pack("<Hf3f3f", imu_status, temp_c, *accel, *gyro)


SatTuple = tuple[int, int, int, int, int, int, int]  # sys, svId, flags, cno, qi, el, az


def sat_info(sats: Sequence[SatTuple]) -> bytes:
    out = struct.pack("<BB", len(sats), 0)
    for sys_, sv, flags, cno, qi, el, az in sats:
        out += struct.pack("<bBBBBbh", sys_, sv, flags, cno, qi, el, az)
    return out


def raw_meas(n: int, *, tow: float = 123456.5, week: int = 2385) -> bytes:
    out = struct.pack("<dHBB", tow, week, n, 0)
    for i in range(n):
        out += struct.pack(
            "<BBBBbBHddf", 0, i + 1, 0, i, 0, 40 + i, 0x0003, 2.1e7 + i, 1.1e8 + i, -12.5
        )
    return out


DEFAULT_SATS: list[SatTuple] = [
    (0, 5, 0x3F, 44, 7, 45, 120),  # GPS 5: healthy, used, az/el valid
    (2, 11, 0x27, 38, 6, 20, -170),  # Galileo 11: healthy, az/el valid, not used
]


def gps_payload(
    *,
    tow_ns: int = 123_456_000_000_000,
    num_sats: int = 12,
    fix: int = 3,
    pos_lla: tuple[float, float, float] = (23.78, 90.40, 12.0),
    vel_ned: tuple[float, float, float] = (1.0, 0.5, -0.1),
    pos_u: tuple[float, float, float] = (1.5, 1.6, 3.0),
    time_u: float = 2e-8,
    time_info: tuple[int, int] = (0x07, 18),
    dop: tuple[float, ...] = (1.9, 1.7, 0.9, 1.4, 0.8, 0.6, 0.5),
    sats: Sequence[SatTuple] | None = None,
    meas: int | None = None,
) -> tuple[int, bytes, int | None]:
    """GPS group 0x7ABA (Tow, NumSats, Fix, PosLla, VelNed, PosU, TimeU, TimeInfo, DOP, SatInfo;
    bit 10 VelU is not in that mask), plus RawMeas through the extension word when *meas*."""
    data = (
        struct.pack("<QBB", tow_ns, num_sats, fix)
        + struct.pack("<3d", *pos_lla)
        + struct.pack("<3f", *vel_ned)
        + struct.pack("<3f", *pos_u)
        + struct.pack("<f", time_u)
        + struct.pack("<Bb", *time_info)
        + struct.pack("<7f", *dop)
        + sat_info(DEFAULT_SATS if sats is None else sats)
    )
    ext = None
    if meas is not None:
        data += raw_meas(meas)
        ext = 0x0001
    return 0x7ABA, data, ext


def binary(*parts: tuple[int, tuple[int, bytes] | tuple[int, bytes, int | None]]) -> bytes:
    """`binary((TIME, time_group_payload()), (INS, ins_payload()))` -> one frame."""
    groups: dict[int, tuple[int, bytes, int | None]] = {}
    for g, spec in parts:
        if len(spec) == 2:
            groups[g] = (spec[0], spec[1], None)
        else:
            groups[g] = spec  # type: ignore[assignment]
    return build_binary(groups)


def concat(frames: Iterable[bytes]) -> bytes:
    return b"".join(frames)
