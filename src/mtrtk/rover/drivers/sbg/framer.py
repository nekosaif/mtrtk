"""sbgECom binary framing: FF 5A | id | class | len(le16) | payload | crc(le16) | 33.

The CRC (CRC-16/KERMIT) covers id .. end of payload. LEN above 4086 is an invalid header. A class
with bit 7 set is an extended (paged) frame whose LEN spans a 5-byte paging header (transferId,
pageIndex, nrPages) before the data; those carry settings import/export only, so this framer
counts and drops them rather than reassembling (sbgEComProtocol.c, tag 5.8.935-stable).

Importing this module registers the SBG namer and parser with `mtrtk.core.frames`.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from mtrtk.core.frames import Frame, Proto, register_namer, register_parser
from mtrtk.rover.drivers.sbg import logs
from mtrtk.rover.drivers.sbg.crc import crc16_kermit
from mtrtk.rover.drivers.sbg.ids import CLASS, CMD_NAME, LOG_NAME

SYNC = b"\xff\x5a"
ETX = 0x33
MAX_PAYLOAD = 4086
EXTENDED_CLASS = 0x80
HEADER = 6  # sync(2) id class len(2)
TRAILER = 3  # crc(2) etx


# Classes a frame can carry on the wire (bit 7, the extended flag, masked off). A sync pair
# followed by anything else is not a header, so it is dropped before waiting for its LEN.
WIRE_CLASSES = frozenset(v for k, v in CLASS.items() if k != "LOG_ALL")


@dataclass
class FramerStats:
    frames: int = 0  # emitted
    crc_failed: int = 0  # fully buffered candidates rejected by a bad ETX or CRC (corruption)
    resyncs: int = 0  # times a candidate frame was rejected and the scan moved one byte on
    extended_dropped: int = 0
    bytes_skipped: int = 0  # bytes discarded outside valid frames


def encode(msg_class: int, msg_id: int, payload: bytes = b"") -> bytes:
    """One standard frame. Extended (paged) frames are not supported."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload exceeds {MAX_PAYLOAD} bytes (extended frames are not supported)")
    if msg_class & EXTENDED_CLASS:
        raise ValueError("class bit 7 marks an extended frame, which encode() does not build")
    body = bytes([msg_id, msg_class]) + struct.pack("<H", len(payload)) + payload
    return SYNC + body + struct.pack("<H", crc16_kermit(body)) + bytes([ETX])


def name_for(raw: bytes) -> str:
    msg_id, msg_class = raw[2], raw[3]
    if msg_class == CLASS["LOG_ECOM_0"] and msg_id in LOG_NAME:
        return LOG_NAME[msg_id]
    if msg_class == CLASS["CMD_0"]:
        return "CMD-" + CMD_NAME.get(msg_id, f"{msg_id:02X}")
    return f"SBG-{msg_class:02X}-{msg_id:02X}"


class SbgFramer:
    """Feed bytes in any chunking; get back CRC-verified standard frames.

    Resync policy: on a bad class, LEN, ETX or CRC drop the first byte and rescan (a corrupt
    header's LEN is never trusted to skip ahead). A bad class or LEN is rejected as soon as the
    header is in; a bad ETX or CRC also counts in `crc_failed` (a byte lost on the link moves the
    ETX, so most corruption shows up there before the CRC is computed).

    `Frame.t_mono` / `t_host` are the release time (when the last byte arrived and `feed` ran),
    so frames released by one call share them; the device `time_stamp_us` is authoritative.
    """

    def __init__(self) -> None:
        self.buf = bytearray()
        self.stats = FramerStats()

    def feed(self, data: bytes) -> list[Frame]:
        self.buf += data
        out: list[Frame] = []
        buf = self.buf
        while buf:
            start = buf.find(SYNC)
            if start < 0:  # keep a trailing FF: it may pair with the next chunk's 5A
                keep = 1 if buf[-1] == SYNC[0] else 0
                self.stats.bytes_skipped += len(buf) - keep
                del buf[: len(buf) - keep]
                break
            if start:
                self.stats.bytes_skipped += start
                del buf[:start]
            if len(buf) < HEADER:
                break
            length = buf[4] | (buf[5] << 8)
            if length > MAX_PAYLOAD or (buf[3] & ~EXTENDED_CLASS) not in WIRE_CLASSES:
                self._skip_one()
                continue
            total = HEADER + length + TRAILER
            if len(buf) < total:
                break
            crc = buf[HEADER + length] | (buf[HEADER + length + 1] << 8)
            if buf[total - 1] != ETX or crc != crc16_kermit(bytes(buf[2 : HEADER + length])):
                self.stats.crc_failed += 1
                self._skip_one()
                continue
            raw = bytes(buf[:total])
            del buf[:total]
            if raw[3] & EXTENDED_CLASS:
                self.stats.extended_dropped += 1
                continue
            self.stats.frames += 1
            out.append(Frame(Proto.SBG, raw, time.monotonic(), time.time()))
        return out

    def _skip_one(self) -> None:
        self.stats.resyncs += 1
        self.stats.bytes_skipped += 1
        del self.buf[:1]


register_namer(Proto.SBG, name_for)
register_parser(Proto.SBG, logs.parse)
