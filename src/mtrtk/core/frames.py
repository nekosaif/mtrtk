"""Incremental framer: splits a mixed UBX / RTCM3 / NMEA byte stream into frames."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

import pyubx2
from pynmeagps import NMEAReader
from pyrtcm import RTCMReader
from pyubx2 import UBXReader

from mtrtk.core.crc import crc24q, nmea_checksum, ubx_checksum

UBX_SYNC1 = 0xB5
UBX_SYNC2 = 0x62
RTCM_PREAMBLE = 0xD3
NMEA_START = 0x24  # '$'
UBX_MAX_PAYLOAD = 8192
NMEA_MAX_LEN = 256  # NMEA 0183 caps a sentence at 82, but u-blox PUBX/proprietary run longer
_SYNC_BYTES = frozenset((UBX_SYNC1, RTCM_PREAMBLE, NMEA_START))


class Proto(StrEnum):
    UBX = "ubx"
    RTCM3 = "rtcm3"
    NMEA = "nmea"


@dataclass(eq=False)
class Frame:
    """One complete protocol frame with its raw bytes. Parsing is lazy and cached."""

    proto: Proto
    raw: bytes
    t_mono: float
    t_host: float
    _parsed: Any = field(default=None, repr=False)

    @property
    def ubx_class_id(self) -> tuple[int, int]:
        if self.proto is not Proto.UBX:
            raise ValueError("not a UBX frame")
        return self.raw[2], self.raw[3]

    @property
    def rtcm_type(self) -> int:
        if self.proto is not Proto.RTCM3:
            raise ValueError("not an RTCM3 frame")
        return (self.raw[3] << 4) | (self.raw[4] >> 4)

    @property
    def identity(self) -> str:
        if self.proto is Proto.UBX:
            name = pyubx2.UBX_MSGIDS.get(self.raw[2:4])
            return name if name else f"UBX-{self.raw[2]:02X}-{self.raw[3]:02X}"
        if self.proto is Proto.RTCM3:
            return str(self.rtcm_type)
        end = self.raw.find(b",")
        return self.raw[1 : end if end > 0 else 6].decode("ascii", "replace")

    @property
    def payload(self) -> bytes:
        if self.proto is Proto.UBX:
            return self.raw[6:-2]
        if self.proto is Proto.RTCM3:
            return self.raw[3:-3]
        return self.raw

    def parsed(self) -> Any:
        """Parse with pyubx2 / pyrtcm / pynmeagps on first call and cache the result."""
        if self._parsed is None:
            if self.proto is Proto.UBX:
                self._parsed = UBXReader.parse(self.raw)
            elif self.proto is Proto.RTCM3:
                self._parsed = RTCMReader.parse(self.raw)
            else:
                self._parsed = NMEAReader.parse(self.raw)
        return self._parsed


@dataclass
class FramerStats:
    frames: dict[str, int] = field(default_factory=lambda: {"ubx": 0, "rtcm3": 0, "nmea": 0})
    garbage_bytes: int = 0
    checksum_errors: int = 0


class Framer:
    """Feed bytes in any chunking; get back complete, checksum-verified frames."""

    def __init__(self, max_buffer: int = 1 << 20) -> None:
        self._buf = bytearray()
        self._max_buffer = max_buffer
        self.stats = FramerStats()

    def feed(self, data: bytes) -> list[Frame]:
        self._buf += data
        if len(self._buf) > self._max_buffer:
            dropped = len(self._buf) - self._max_buffer
            del self._buf[:dropped]
            self.stats.garbage_bytes += dropped

        out: list[Frame] = []
        while self._buf:
            first = self._buf[0]
            if first not in _SYNC_BYTES:
                skip = self._next_sync()
                del self._buf[:skip]
                self.stats.garbage_bytes += skip
                continue
            if first == UBX_SYNC1:
                result = self._try_ubx()
            elif first == RTCM_PREAMBLE:
                result = self._try_rtcm()
            else:
                result = self._try_nmea()
            if result is None:  # incomplete: wait for more bytes
                break
            if result is False:  # invalid at this offset: resync one byte later
                del self._buf[:1]
                self.stats.garbage_bytes += 1
                continue
            out.append(result)
        return out

    def _next_sync(self) -> int:
        for i in range(1, len(self._buf)):
            if self._buf[i] in _SYNC_BYTES:
                return i
        return len(self._buf)

    def _emit(self, proto: Proto, length: int) -> Frame:
        raw = bytes(self._buf[:length])
        del self._buf[:length]
        self.stats.frames[proto.value] += 1
        return Frame(proto, raw, time.monotonic(), time.time())

    def _try_ubx(self) -> Frame | Literal[False] | None:
        buf = self._buf
        if len(buf) < 2:
            return None
        if buf[1] != UBX_SYNC2:
            return False
        if len(buf) < 6:
            return None
        length = buf[4] | (buf[5] << 8)
        if length > UBX_MAX_PAYLOAD:
            return False
        total = 6 + length + 2
        if len(buf) < total:
            return None
        if bytes(buf[total - 2 : total]) != ubx_checksum(bytes(buf[2 : total - 2])):
            self.stats.checksum_errors += 1
            return False
        return self._emit(Proto.UBX, total)

    def _try_rtcm(self) -> Frame | Literal[False] | None:
        buf = self._buf
        if len(buf) < 3:
            return None
        if buf[1] & 0xFC:
            return False
        length = ((buf[1] & 0x03) << 8) | buf[2]
        total = 3 + length + 3
        if len(buf) < total:
            return None
        expected = crc24q(bytes(buf[: 3 + length]))
        got = (buf[total - 3] << 16) | (buf[total - 2] << 8) | buf[total - 1]
        if expected != got:
            self.stats.checksum_errors += 1
            return False
        return self._emit(Proto.RTCM3, total)

    def _try_nmea(self) -> Frame | Literal[False] | None:
        buf = self._buf
        end = buf.find(b"\r\n", 0, NMEA_MAX_LEN + 2)
        if end < 0:
            return None if len(buf) < NMEA_MAX_LEN else False
        star = buf.rfind(b"*", 0, end)
        if star < 0 or end - star != 3:
            return False
        try:
            given = int(bytes(buf[star + 1 : end]), 16)
        except ValueError:
            return False
        if given != nmea_checksum(bytes(buf[1:star])):
            self.stats.checksum_errors += 1
            return False
        return self._emit(Proto.NMEA, end + 2)
