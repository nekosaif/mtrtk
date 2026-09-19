"""Builders for synthetic wire frames used across the test-suite."""

from __future__ import annotations

import struct

from pyubx2.ubxtypes_configdb import UBX_CONFIG_DATABASE

from mtrtk.core.bus import Bus
from mtrtk.core.crc import crc24q, nmea_checksum, ubx_checksum
from mtrtk.core.router import Router
from mtrtk.core.ubx_config import CfgValue

ACK_ACK = (0x05, 0x01)
ACK_NAK = (0x05, 0x00)
CFG_VALSET = (0x06, 0x8A)
CFG_VALGET = (0x06, 0x8B)
MON_VER = (0x0A, 0x04)

_SIZE = {
    "L001": 1,
    "U001": 1,
    "I001": 1,
    "E001": 1,
    "X001": 1,
    "U002": 2,
    "I002": 2,
    "X002": 2,
    "U004": 4,
    "I004": 4,
    "X004": 4,
    "R004": 4,
    "U008": 8,
    "I008": 8,
    "R008": 8,
}
_KEY_BY_ID = {kid: name for name, (kid, _) in UBX_CONFIG_DATABASE.items()}


def ubx_frame(cls: int, msg_id: int, payload: bytes) -> bytes:
    body = bytes((cls, msg_id)) + len(payload).to_bytes(2, "little") + payload
    return b"\xb5\x62" + body + ubx_checksum(body)


def rtcm_frame(msg_type: int, payload_rest: bytes = b"\x00" * 10) -> bytes:
    """RTCM3 frame whose first 12 payload bits carry *msg_type*."""
    first = bytes(((msg_type >> 4) & 0xFF, ((msg_type & 0x0F) << 4) | (payload_rest[1] & 0x0F)))
    payload = first + payload_rest[2:]
    head = bytes((0xD3, (len(payload) >> 8) & 0x03, len(payload) & 0xFF))
    return head + payload + crc24q(head + payload).to_bytes(3, "big")


def nmea_frame(body: str) -> bytes:
    raw = body.encode("ascii")
    return b"$" + raw + b"*%02X\r\n" % nmea_checksum(raw)


def mon_ver_bytes(fw: str = "HPG 1.13", protver: str = "27.12") -> bytes:
    def cstr(text: str, size: int) -> bytes:
        return text.encode().ljust(size, b"\x00")

    payload = cstr("EXT CORE 1.00 (f10c36)", 30) + cstr("00190000", 10)
    for ext in (
        "ROM BASE 0x118B2060",
        f"FWVER={fw}",
        f"PROTVER={protver}",
        "MOD=ZED-F9P",
        "GPS;GLO;GAL;BDS",
        "SBAS;QZSS",
    ):
        payload += cstr(ext, 30)
    return ubx_frame(*MON_VER, payload)


class FakeReceiver:
    """ByteSource stand-in: answers VALSET/VALGET/polls immediately via the bus."""

    name = "fake"
    ends_at_eof = True  # like a replay file: read() returning b"" is a clean end

    def __init__(self, bus: Bus) -> None:
        self.router = Router(bus)
        self.config: dict[str, CfgValue] = {}
        self.valset_nak_keys: set[str] = set()
        self.unsupported_polls: set[tuple[int, int]] = set()
        self.silent = False  # never answer (timeout tests)
        self.mon_ver = mon_ver_bytes()
        self.writes: list[bytes] = []
        self.valsets: list[tuple[int, dict[str, CfgValue]]] = []

    async def open(self) -> None:
        return None

    async def read(self) -> bytes:
        return b""

    async def close(self) -> None:
        return None

    def inject(self, data: bytes) -> None:
        self.router.feed(data)

    async def write(self, data: bytes) -> None:
        self.writes.append(data)
        if self.silent:
            return
        cls, mid = data[2], data[3]
        payload = data[6:-2]
        if (cls, mid) == CFG_VALSET:
            layers = payload[1]
            items = self._decode_items(payload[4:])
            self.valsets.append((layers, items))
            if set(items) & self.valset_nak_keys:
                self.inject(ubx_frame(*ACK_NAK, bytes(CFG_VALSET)))
            else:
                self.config.update(items)
                self.inject(ubx_frame(*ACK_ACK, bytes(CFG_VALSET)))
        elif (cls, mid) == CFG_VALGET:
            keys = [struct.unpack_from("<I", payload, i)[0] for i in range(4, len(payload), 4)]
            names = [_KEY_BY_ID[k] for k in keys]
            if any(n not in self.config for n in names):
                self.inject(ubx_frame(*ACK_NAK, bytes(CFG_VALGET)))
                return
            body = bytes([1, 0, 0, 0])
            for n in names:
                kid, typ = UBX_CONFIG_DATABASE[n]
                body += struct.pack("<I", kid) + _encode_value(self.config[n], typ)
            # Like a real F9P: the data frame and its ACK-ACK arrive in one burst.
            self.inject(ubx_frame(*CFG_VALGET, body) + ubx_frame(*ACK_ACK, bytes(CFG_VALGET)))
        elif len(payload) == 0:  # poll
            if (cls, mid) in self.unsupported_polls:
                self.inject(ubx_frame(*ACK_NAK, bytes((cls, mid))))
            elif (cls, mid) == MON_VER:
                self.inject(self.mon_ver)
            else:
                self.inject(ubx_frame(cls, mid, b"\x00" * 8))  # generic empty-ish answer

    @staticmethod
    def _decode_items(data: bytes) -> dict[str, CfgValue]:
        items: dict[str, CfgValue] = {}
        i = 0
        while i + 4 <= len(data):
            kid = struct.unpack_from("<I", data, i)[0]
            name = _KEY_BY_ID[kid]
            typ = UBX_CONFIG_DATABASE[name][1]
            size = _SIZE[typ]
            raw = data[i + 4 : i + 4 + size]
            # X-type (bitfield) keys keep their raw bytes, as pyubx2 emits and returns them
            items[name] = (
                raw
                if typ.startswith("X")
                else int.from_bytes(raw, "little", signed=typ.startswith("I"))
            )
            i += 4 + size
        return items


def _encode_value(value: CfgValue, typ: str) -> bytes:
    number = int.from_bytes(value, "little") if isinstance(value, bytes) else int(value)
    return number.to_bytes(_SIZE[typ], "little", signed=typ.startswith("I"))
