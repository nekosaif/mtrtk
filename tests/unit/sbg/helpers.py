"""Encoders for sbgECom logs that return a parsed-ready `Frame` (built through `SbgFramer`).

Every helper takes `t_mono` so a test controls the adapter's clock (decimation, freshness).
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

from mtrtk.core.crc import ubx_checksum
from mtrtk.core.frames import Frame
from mtrtk.rover.drivers.sbg.framer import SbgFramer, encode
from mtrtk.rover.drivers.sbg.ids import LOG

ATT_VALID, HDG_VALID, VEL_VALID, POS_VALID = 1 << 4, 1 << 5, 1 << 6, 1 << 7
NAV_POSITION = 4
UTC_OK = (3 << 1) | (1 << 5) | (2 << 6)  # clock VALID, UTC synced, UTC INITIALIZED
TS = 1_000_000


def frame(name: str, payload: bytes, t_mono: float = 0.0) -> Frame:
    (fr,) = SbgFramer().feed(encode(0, LOG[name], payload))
    return replace(fr, t_mono=t_mono)


def ekf_nav(
    lat: float = 23.7275,
    lon: float = 90.3925,
    alt: float = 12.5,
    undulation: float = -55.2,
    status: int = POS_VALID | VEL_VALID | NAV_POSITION,
    *,
    vel: tuple[float, float, float] = (3.0, 4.0, -0.5),
    vel_std: tuple[float, float, float] = (0.1, 0.2, 0.2),
    pos_std: tuple[float, float, float] = (0.3, 0.4, 0.6),
    ts: int = TS,
    t_mono: float = 0.0,
) -> Frame:
    p = struct.pack("<I3f3f3df3fI", ts, *vel, *vel_std, lat, lon, alt, undulation, *pos_std, status)
    return frame("EKF_NAV", p, t_mono)


def gps_pos(
    pos_type: int,
    num_sv: int = 18,
    diff_age: float | None = 1.2,
    *,
    base_id: int = 7,
    tracked: int = 24,
    t_mono: float = 0.0,
) -> Frame:
    p = struct.pack(
        "<IIIdddffff", TS, pos_type << 6, 1000, 23.7, 90.4, 10.0, -55.0, 0.02, 0.02, 0.05
    )
    age = 0xFFFF if diff_age is None else round(diff_age * 100)
    p += struct.pack("<BHH", num_sv, base_id, age) + struct.pack("<BI", tracked, 0)
    return frame("GPS1_POS", p, t_mono)


def gps_vel(vel: tuple[float, float, float] = (1.0, 1.0, 0.0), course: float = 45.0) -> Frame:
    p = struct.pack("<III3f3fff", TS, 0, 1000, *vel, 0.1, 0.1, 0.2, course, 1.0)
    return frame("GPS1_VEL", p)


def utc(
    dt: datetime,
    *,
    ts: int = TS,
    gps_tow_ms: int | None = None,
    status: int = UTC_OK,
    t_mono: float = 0.0,
) -> Frame:
    if gps_tow_ms is None:  # 18 s ahead of UTC, as the unit reports it
        sow = (dt.isoweekday() % 7) * 86400 + dt.hour * 3600 + dt.minute * 60 + dt.second
        gps_tow_ms = ((sow + 18) % 604800) * 1000 + dt.microsecond // 1000
    p = struct.pack(
        "<IHHbbbbbiI",
        ts,
        status,
        dt.year,
        dt.month,
        dt.day,
        dt.hour,
        dt.minute,
        dt.second,
        dt.microsecond * 1000,
        gps_tow_ms,
    )
    return frame("UTC_TIME", p, t_mono)


def event(ch: str, ts_us: int, offsets: Sequence[int] = (), *, overflow: bool = False) -> Frame:
    status = int(overflow) | sum(1 << (i + 1) for i in range(len(offsets)))
    padded = [*offsets, 0, 0, 0, 0][:4]
    return frame(f"EVENT_{ch}", struct.pack("<IHHHHH", ts_us, status, *padded))


def ekf_euler(
    r: float, p: float, y: float, status: int, *, std: tuple[float, float, float] = (1, 1, 2)
) -> Frame:
    """Angles and 1-sigma in degrees (the wire carries radians)."""
    rad = [math.radians(v) for v in (r, p, y, *std)]
    return frame("EKF_EULER", struct.pack("<I3f3fI", TS, *rad, status))


def hdt(
    heading: float, baseline: float = 1.25, *, computed: bool = True, t_mono: float = 0.0
) -> Frame:
    status = (0 if computed else 1) | (1 << 6)
    p = struct.pack("<IHIffff", TS, status, 1000, heading, 0.4, -1.0, 0.5)
    p += struct.pack("<fBB", baseline, 20, 14)
    return frame("GPS1_HDT", p, t_mono)


def sat(
    sv: int, constellation: int, signals: Sequence[tuple[int, int, int | None]], *, used: bool
) -> bytes:
    """`signals`: (signal id, tracking status, snr or None)."""
    tracking = 5 if used else 3
    flags = tracking | (1 << 3) | (constellation << 7)
    out = struct.pack("<BbHHB", sv, 45, 180, flags, len(signals))
    for sig_id, sig_tracking, snr in signals:
        sig_flags = sig_tracking | (1 << 3) | ((1 << 5) if snr is not None else 0)
        out += struct.pack("<BBB", sig_id, sig_flags, snr or 0)
    return out


def sat_list(*sats: bytes) -> Frame:
    return frame("GPS1_SAT", struct.pack("<IIB", TS, 0, len(sats)) + b"".join(sats))


def gps_raw(payload: bytes) -> Frame:
    return frame("GPS1_RAW", payload)


def status_log(general: int = 0x7F, aiding: int = 0x0F) -> Frame:
    return frame("STATUS", struct.pack("<IHHIIIHIB", TS, general, 0, 97, aiding, 0, 0, 3600, 42))


def imu_short(ts: int = TS, *, t_mono: float = 0.0) -> Frame:
    p = struct.pack("<IH3i3ih", ts, 0, 1048576, 0, -2097152, 67108864, 0, 0, 256 * 25)
    return frame("IMU_SHORT", p, t_mono)


def ubx(msg_class: int, msg_id: int, payload: bytes) -> bytes:
    body = bytes([msg_class, msg_id]) + struct.pack("<H", len(payload)) + payload
    return b"\xb5\x62" + body + ubx_checksum(body)


def topics(sub: Any) -> list[str]:
    q = sub.queue
    return [q.get_nowait()[0] for _ in range(q.qsize())]


def items(sub: Any) -> list[tuple[str, Any]]:
    q = sub.queue
    return [q.get_nowait() for _ in range(q.qsize())]
