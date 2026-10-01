"""Incremental VectorNav framer: binary output frames (`FA ...`) and ASCII lines (`$VN...`).

Both come out as `Frame(Proto.VN, raw)` ("VN-BIN" / "VN-ASCII" identities). A binary frame's
length is computed from its own header and variable-length fields (`fields.binary_length`)
and its CRC-16 must verify; an ASCII line must be printable, end in CR LF within
`ASCII_MAX_LEN` bytes and carry a valid XOR-8 or CRC-16 checksum. On any failure the framer
drops one byte and resyncs on the next `FA` or `$`. Importing this module registers the VN
parser with `Frame.parsed()`.

Known limit: frames are stamped (`t_mono`) when they are emitted. A stray `FA` inside a
payload can parse as a header claiming up to `MAX_PENDING` bytes; the framer then waits for
that many bytes before its CRC fails, and the real frames buffered behind it come out late,
by up to `MAX_PENDING` bytes of line time (about 0.7 s at 115200 baud). The cap stays at the
largest frame the protocol allows (64 satellites, 200 measurements), because the framer does
not know which profile the unit was given.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from mtrtk.core.frames import Frame, Proto, register_parser
from mtrtk.rover.drivers.vectornav.checksum import crc16_xmodem, verify_ascii
from mtrtk.rover.drivers.vectornav.fields import binary_length
from mtrtk.rover.drivers.vectornav.parse import parse

BIN_SYNC = 0xFA
ASCII_START = 0x24  # '$'
ASCII_MAX_LEN = 512
MAX_PENDING = 8 * 1024  # a binary header that claims more than this is not waited on
MAX_BUFFER = 1 << 20
_SYNC = frozenset((BIN_SYNC, ASCII_START))


@dataclass
class VnFramerStats:
    frames: int = 0  # every frame emitted, binary and ASCII
    ascii_frames: int = 0
    crc_failed: int = 0  # binary CRC or ASCII checksum mismatches
    resyncs: int = 0  # runs of dropped bytes
    bytes_skipped: int = 0
    invalid_headers: int = 0


Result = Frame | Literal[False] | None


class VnFramer:
    """Feed bytes in any chunking; get back complete, checksum-verified VN frames."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.stats = VnFramerStats()
        self._skipping = False

    def feed(self, data: bytes) -> list[Frame]:
        self._buf += data
        if len(self._buf) > MAX_BUFFER:
            self._skip(len(self._buf) - MAX_BUFFER)
        out: list[Frame] = []
        while self._buf:
            first = self._buf[0]
            if first not in _SYNC:
                self._skip(self._next_sync())
                continue
            result = self._try_binary() if first == BIN_SYNC else self._try_ascii()
            if result is None:
                break
            if result is False:
                self._skip(1)
                continue
            out.append(result)
        return out

    def _next_sync(self) -> int:
        for i in range(1, len(self._buf)):
            if self._buf[i] in _SYNC:
                return i
        return len(self._buf)

    def _skip(self, n: int) -> None:
        del self._buf[:n]
        self.stats.bytes_skipped += n
        if not self._skipping:
            self._skipping = True
            self.stats.resyncs += 1

    def _emit(self, length: int) -> Frame:
        raw = bytes(self._buf[:length])
        del self._buf[:length]
        self._skipping = False
        self.stats.frames += 1
        return Frame(Proto.VN, raw, time.monotonic(), time.time())

    def _try_binary(self) -> Result:
        buf = self._buf
        try:
            total = binary_length(buf)
        except ValueError:
            self.stats.invalid_headers += 1
            return False
        if total is None:
            return False if len(buf) > MAX_PENDING else None
        if total > MAX_PENDING:
            self.stats.invalid_headers += 1
            return False
        if len(buf) < total:
            return None
        if crc16_xmodem(memoryview(buf)[1:total]) != 0:
            self.stats.crc_failed += 1
            return False
        return self._emit(total)

    def _try_ascii(self) -> Result:
        buf = self._buf
        prefix = bytes(buf[:3])
        if not b"$VN".startswith(prefix):
            return False
        if len(prefix) < 3:
            return None
        limit = min(len(buf), ASCII_MAX_LEN + 2)
        for i in range(3, limit):
            b = buf[i]
            if b == 0x0D:
                if i + 1 >= len(buf):
                    return None
                if buf[i + 1] != 0x0A:
                    return False
                line = bytes(buf[: i + 2])
                if not verify_ascii(line):
                    self.stats.crc_failed += 1
                    return False
                frame = self._emit(i + 2)
                self.stats.ascii_frames += 1
                return frame
            if not 0x20 <= b <= 0x7E:  # binary bytes: this '$' was not a line start
                return False
        return False if len(buf) >= ASCII_MAX_LEN + 2 else None


register_parser(Proto.VN, parse)
