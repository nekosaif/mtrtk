"""Decode RTCM 1005 (stationary reference station ARP) into ECEF metres."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from mtrtk.core.frames import Frame, Proto

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Ecef1005:
    """The antenna reference point a base broadcasts, in ECEF metres."""

    station_id: int
    x: float
    y: float
    z: float
    gps: bool
    glonass: bool
    galileo: bool


def decode_1005(frame: Frame) -> Ecef1005 | None:
    """The ARP carried by *frame*, or None when it is not a readable 1005."""
    if frame.proto is not Proto.RTCM3 or frame.rtcm_type != 1005:
        return None
    try:
        m = frame.parsed()  # pyrtcm scales DF025/26/27 (0.0001 m units) to metres
        return Ecef1005(
            station_id=int(m.DF003),
            x=float(m.DF025),
            y=float(m.DF026),
            z=float(m.DF027),
            gps=bool(m.DF022),
            glonass=bool(m.DF023),
            galileo=bool(m.DF024),
        )
    except Exception as exc:  # a corrupt-but-CRC-valid 1005 must not kill the caller
        log.warning("ignoring unreadable RTCM 1005 frame: %r", exc)
        return None
