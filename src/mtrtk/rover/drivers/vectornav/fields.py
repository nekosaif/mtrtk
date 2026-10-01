"""VectorNav binary output layout: groups, field sizes and names, and frame length.

A binary frame is `FA | groups u8 | per set group bit (0..6, in order): field u16le
[+ extension u16le when field bit 15 is set, GPS groups only] | payload | CRC16 big-endian`.
Payload values are little-endian, groups in bit order, fields in bit order within a group,
and a GPS group's extension fields follow its standard fields.

Sizes are from vnproglib 1.2 (`packet.cpp`) and the VN-200 user manual. Two GPS fields vary
in length: SatInfo (bit 14) is a 2-byte header (numSats, reserved) plus 8 bytes per satellite,
RawMeas (extension bit 0) a 12-byte header (tow f64, week u16, numMeas u8, reserved) plus 28
bytes per measurement. Both counts are read from the bytes themselves.
"""

from __future__ import annotations

GROUP_BITS: dict[str, int] = {
    "common": 0,
    "time": 1,
    "imu": 2,
    "gps": 3,
    "attitude": 4,
    "ins": 5,
    "gps2": 6,
}
GROUP_NAMES: dict[int, str] = {bit: name for name, bit in GROUP_BITS.items()}
GPS_GROUPS = frozenset((GROUP_BITS["gps"], GROUP_BITS["gps2"]))

# Bit index 0..14 -> size in bytes (0 = unknown / invalid in a header).
FIELD_SIZES: dict[int, list[int]] = {
    0: [8, 8, 8, 12, 16, 12, 24, 12, 12, 24, 20, 28, 2, 4, 8],
    1: [8, 8, 8, 2, 8, 8, 8, 4, 4, 1, 0, 0, 0, 0, 0],
    2: [2, 12, 12, 12, 4, 4, 16, 12, 12, 12, 12, 2, 40, 0, 0],
    3: [8, 8, 2, 1, 1, 24, 24, 12, 12, 12, 4, 4, 2, 28, 2],  # bit 14 SatInfo: + 8 * numSats
    4: [2, 12, 16, 36, 12, 12, 12, 12, 12, 12, 28, 24, 12, 0, 0],
    5: [2, 24, 24, 12, 12, 12, 12, 12, 12, 4, 4, 68, 64, 0, 0],
    6: [8, 8, 2, 1, 1, 24, 24, 12, 12, 12, 4, 4, 2, 28, 2],
}

# Bit index -> field name (snake_case). Sized bits without a name here are kept as raw bytes
# under "bit<N>" by the parser.
_GPS_NAMES = [
    "utc",
    "tow",
    "week",
    "num_sats",
    "fix",
    "pos_lla",
    "pos_ecef",
    "vel_ned",
    "vel_ecef",
    "pos_u",
    "vel_u",
    "time_u",
    "time_info",
    "dop",
    "sat_info",
]
FIELD_NAMES: dict[int, list[str]] = {
    0: [
        "time_startup",
        "time_gps",
        "time_sync_in",
        "ypr",
        "quaternion",
        "angular_rate",
        "position",
        "velocity",
        "accel",
        "imu",
        "mag_pres",
        "delta_theta",
        "ins_status",
        "sync_in_cnt",
        "time_gps_pps",
    ],
    1: [
        "time_startup",
        "time_gps",
        "gps_tow",
        "gps_week",
        "time_sync_in",
        "time_gps_pps",
        "utc",
        "sync_in_cnt",
        "sync_out_cnt",
        "time_status",
    ],
    2: [
        "imu_status",
        "uncomp_mag",
        "uncomp_accel",
        "uncomp_gyro",
        "temp",
        "pres",
        "delta_theta",
        "delta_vel",
        "mag",
        "accel",
        "angular_rate",
        "sens_sat",
    ],
    3: _GPS_NAMES,
    4: [
        "vpe_status",
        "ypr",
        "quaternion",
        "dcm",
        "mag_ned",
        "accel_ned",
        "linear_accel_body",
        "linear_accel_ned",
        "ypr_u",
    ],
    5: [
        "ins_status",
        "pos_lla",
        "pos_ecef",
        "vel_body",
        "vel_ned",
        "vel_ecef",
        "mag_ecef",
        "accel_ecef",
        "linear_accel_ecef",
        "pos_u",
        "vel_u",
    ],
    6: _GPS_NAMES,
}

EXT_FLAG = 0x8000  # field word bit 15: an extension word follows (GPS groups)
EXT_RAWMEAS = 0x0001  # extension bit 0: RawMeas
RAWMEAS_BIT = 16  # how `field_layout` names the extension's RawMeas field
SATINFO_BIT = 14
SATINFO_HEADER, SATINFO_RECORD = 2, 8
RAWMEAS_HEADER, RAWMEAS_RECORD = 12, 28
RAWMEAS_COUNT_OFFSET = 10  # numMeas inside the RawMeas header
MAX_SATS, MAX_MEAS = 64, 200

Layout = list[tuple[int, int, int, int]]  # (group bit, field bit, offset in frame, size)


def _walk(buf: bytes | bytearray) -> tuple[Layout, int] | None:
    """The field layout and total frame length, or None when more bytes are needed."""
    if len(buf) < 2:
        return None
    groups = buf[1]
    if groups == 0 or groups & 0x80:
        raise ValueError(f"invalid groups byte 0x{groups:02X}")
    pos = 2
    headers: list[tuple[int, int, int]] = []  # (group, field mask, ext mask)
    for g in range(7):
        if not groups & (1 << g):
            continue
        if len(buf) < pos + 2:
            return None
        mask = int.from_bytes(buf[pos : pos + 2], "little")
        pos += 2
        ext = 0
        if mask & EXT_FLAG:
            if g not in GPS_GROUPS:
                raise ValueError(f"extension bit outside the GPS groups (group {g})")
            if len(buf) < pos + 2:
                return None
            ext = int.from_bytes(buf[pos : pos + 2], "little")
            pos += 2
            if ext & ~EXT_RAWMEAS:
                raise ValueError(f"unknown GPS extension field 0x{ext:04X}")
        if mask & ~EXT_FLAG == 0 and not ext:
            raise ValueError(f"group {g} selected with no fields")
        headers.append((g, mask & ~EXT_FLAG, ext))
    layout: Layout = []
    for g, mask, ext in headers:
        sizes = FIELD_SIZES[g]
        for bit in range(15):
            if not mask & (1 << bit):
                continue
            size = sizes[bit]
            if size == 0:
                raise ValueError(f"unknown field bit {bit} in group {g}")
            if g in GPS_GROUPS and bit == SATINFO_BIT:
                if len(buf) < pos + 1:
                    return None
                n = buf[pos]
                if n > MAX_SATS:
                    raise ValueError(f"implausible numSats {n}")
                size = SATINFO_HEADER + SATINFO_RECORD * n
            layout.append((g, bit, pos, size))
            pos += size
        if ext & EXT_RAWMEAS:
            if len(buf) < pos + RAWMEAS_COUNT_OFFSET + 1:
                return None
            m = buf[pos + RAWMEAS_COUNT_OFFSET]
            if m > MAX_MEAS:
                raise ValueError(f"implausible numMeas {m}")
            size = RAWMEAS_HEADER + RAWMEAS_RECORD * m
            layout.append((g, RAWMEAS_BIT, pos, size))
            pos += size
    return layout, pos + 2  # + CRC


def binary_length(buf: bytes | bytearray) -> int | None:
    """Total frame length (sync to CRC) once the bytes present determine it; None = need more.

    Raises `ValueError` for a header no VN unit sends: groups byte 0 or bit 7 set, a field bit
    of size 0, an extension outside the GPS groups, or counts above 64 sats / 200 measurements.
    """
    walked = _walk(buf)
    return None if walked is None else walked[1]


def field_layout(buf: bytes | bytearray) -> Layout:
    """`(group, bit, offset, size)` for every field of a complete frame, in payload order.
    The RawMeas extension field is reported as bit `RAWMEAS_BIT` (16)."""
    walked = _walk(buf)
    if walked is None:
        raise ValueError("incomplete VN binary frame")
    return walked[0]
