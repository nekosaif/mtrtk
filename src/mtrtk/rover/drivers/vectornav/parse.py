"""Decode VectorNav frames: binary output groups into dicts, ASCII lines into `VnAscii`.

`parse(frame)` returns `VnBinary(groups)` with one dict per present group keyed by snake_case
field name (`groups["ins"]["pos_lla"]`, `groups["gps"]["fix"]`, `groups["time"]["utc"]`), a
`VnAscii` for a `$VN...` line, or None for bytes that are neither (never raises). Angles stay
in degrees as the unit sends them. Multi-value fields are tuples, single values scalars.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from mtrtk.core.frames import Frame
from mtrtk.rover.drivers.vectornav import fields as F

log = logging.getLogger(__name__)

# (group, bit) -> struct format of the plain numeric fields. Special layouts are decoded below.
_FORMATS: dict[int, dict[int, str]] = {
    0: {
        0: "Q",
        1: "Q",
        2: "Q",
        3: "3f",
        4: "4f",
        5: "3f",
        6: "3d",
        7: "3f",
        8: "3f",
        9: "6f",
        10: "5f",
        11: "7f",
        12: "H",
        13: "I",
        14: "Q",
    },
    1: {0: "Q", 1: "Q", 2: "Q", 3: "H", 4: "Q", 5: "Q", 7: "I", 8: "I"},
    2: {
        0: "H",
        1: "3f",
        2: "3f",
        3: "3f",
        4: "f",
        5: "f",
        6: "4f",
        7: "3f",
        8: "3f",
        9: "3f",
        10: "3f",
        11: "H",
    },
    3: {
        1: "Q",
        2: "H",
        3: "B",
        4: "B",
        5: "3d",
        6: "3d",
        7: "3f",
        8: "3f",
        9: "3f",
        10: "f",
        11: "f",
        13: "7f",
    },
    4: {0: "H", 1: "3f", 2: "4f", 3: "9f", 4: "3f", 5: "3f", 6: "3f", 7: "3f", 8: "3f"},
    5: {
        0: "H",
        1: "3d",
        2: "3d",
        3: "3f",
        4: "3f",
        5: "3f",
        6: "3f",
        7: "3f",
        8: "3f",
        9: "f",
        10: "f",
    },
}
_FORMATS[6] = _FORMATS[3]

INS_MODE_NAMES: dict[int, str] = {0: "Not tracking", 1: "Aligning", 2: "Tracking", 3: "Unknown"}
# GPS `Fix`. 0-3 per vnproglib; 4 SBAS and 7/8 RTK per VectorNav RTK-capable units.
GNSS_FIX_NAMES: dict[int, str] = {
    0: "No fix",
    1: "Time only",
    2: "2D",
    3: "3D",
    4: "SBAS",  # VERIFY on the VN-200
    7: "RTK float",  # VERIFY: the VN-200 is not documented as RTK-capable
    8: "RTK fixed",  # VERIFY
}


@dataclass(frozen=True)
class VnInsStatus:
    """The InsStatus word (INS group bit 0 / Common bit 12)."""

    raw: int

    @property
    def mode(self) -> int:  # 0 not tracking, 1 aligning / insufficient motion, 2 tracking
        return self.raw & 0x3

    @property
    def gps_fix(self) -> bool:
        return bool(self.raw & 0x0004)

    @property
    def time_error(self) -> bool:
        return bool(self.raw & 0x0008)

    @property
    def imu_error(self) -> bool:
        return bool(self.raw & 0x0010)

    @property
    def mag_pres_error(self) -> bool:
        return bool(self.raw & 0x0020)

    @property
    def gps_error(self) -> bool:
        return bool(self.raw & 0x0040)

    @property
    def gps_heading_ins(self) -> bool:
        return bool(self.raw & 0x0100)

    @property
    def gps_compass(self) -> bool:
        return bool(self.raw & 0x0200)


@dataclass(frozen=True)
class VnSat:
    """One SatInfo entry. `sys` is on the u-blox gnssId scale (0 GPS, 1 SBAS, 2 Galileo,
    3 BeiDou, 5 QZSS, 6 GLONASS) per vnproglib. VERIFY the system numbering and flag bits
    against a VN-200's output."""

    sys: int
    sv_id: int
    flags: int
    cno: int
    qi: int
    el: int
    az: int

    @property
    def healthy(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def almanac(self) -> bool:
        return bool(self.flags & 0x02)

    @property
    def ephemeris(self) -> bool:
        return bool(self.flags & 0x04)

    @property
    def diff_corr(self) -> bool:
        return bool(self.flags & 0x08)

    @property
    def used(self) -> bool:
        return bool(self.flags & 0x10)

    @property
    def az_el_valid(self) -> bool:
        return bool(self.flags & 0x20)

    @property
    def used_rtk(self) -> bool:
        return bool(self.flags & 0x40)


@dataclass(frozen=True)
class VnBinary:
    groups: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class VnAscii:
    """`$VN<cmd>,fields*CS`. `register` for RRG/WRG replies, `error` for `$VNERR` (hex code)."""

    cmd: str
    register: int | None
    fields: list[str]
    error: int | None


def parse(frame: Frame) -> VnBinary | VnAscii | None:
    raw = frame.raw
    try:
        if raw[:1] == b"\xfa":
            return _parse_binary(raw)
        if raw[:3] == b"$VN":
            return _parse_ascii(raw)
    except (ValueError, struct.error, IndexError):
        log.debug("unparseable VN frame %r", raw[:32])
    return None


# ------------------------------------------------------------------ binary
def _parse_binary(raw: bytes) -> VnBinary:
    groups: dict[str, dict[str, Any]] = {}
    for g, bit, off, size in F.field_layout(raw):
        out = groups.setdefault(F.GROUP_NAMES[g], {})
        chunk = raw[off : off + size]
        names = F.FIELD_NAMES[g]
        if bit == F.RAWMEAS_BIT:
            tow, week, n = struct.unpack_from("<dHB", chunk)
            out.update(raw_meas=chunk, raw_meas_count=n, raw_meas_tow=tow, raw_meas_week=week)
            continue
        name = names[bit] if bit < len(names) else f"bit{bit}"
        fmt = _FORMATS.get(g, {}).get(bit)
        if fmt is not None:
            values = struct.unpack("<" + fmt, chunk)
            value: Any = values[0] if len(values) == 1 else values
            out[name] = value
            if name == "ins_status":
                out[name] = VnInsStatus(value)
            elif name == "gps_tow" or (g in F.GPS_GROUPS and name == "tow"):
                out[f"{name}_s"] = value / 1e9
            continue
        if name == "utc":
            out["utc"] = _utc(chunk)
        elif name == "time_status":
            out["time_status"] = {
                "time_ok": bool(chunk[0] & 0x01),
                "date_ok": bool(chunk[0] & 0x02),
                "utc_time_valid": bool(chunk[0] & 0x04),
            }
        elif name == "time_info":
            status, leap = struct.unpack("<Bb", chunk)
            out["time_info"] = {"status": status, "leap_secs": leap}
        elif name == "sat_info":
            out["sat_info"] = _sat_info(chunk)
        else:
            out[name] = chunk  # sized but not decoded: kept for capture / debugging
    return VnBinary(groups)


def _utc(chunk: bytes) -> datetime | None:
    year, month, day, hour, minute, sec, ms = struct.unpack("<bBBBBBH", chunk)
    try:
        return datetime(2000 + year, month, day, hour, minute, sec, ms * 1000, tzinfo=UTC)
    except ValueError:  # all zero before the unit has a time, or out of range
        return None


def _sat_info(chunk: bytes) -> list[VnSat]:
    n = chunk[0]
    return [
        VnSat(*struct.unpack_from("<bBBBBbh", chunk, F.SATINFO_HEADER + F.SATINFO_RECORD * i))
        for i in range(n)
    ]


# ------------------------------------------------------------------ ASCII
def _parse_ascii(raw: bytes) -> VnAscii:
    text = raw.rstrip(b"\r\n").decode("ascii")
    star = text.rfind("*")
    body = text[1:star] if star >= 0 else text[1:]
    head, *rest = body.split(",")
    cmd = head[2:]
    register: int | None = None
    error: int | None = None
    if cmd in ("RRG", "WRG"):
        if not rest:
            raise ValueError("register reply without a register number")
        register = int(rest[0])
        rest = rest[1:]
    elif cmd == "ERR":
        if not rest:
            raise ValueError("error reply without a code")
        error = int(rest[0], 16)
    return VnAscii(cmd=cmd, register=register, fields=rest, error=error)
