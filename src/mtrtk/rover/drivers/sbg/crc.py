"""CRC-16/KERMIT as used by sbgECom (`sbgCrc16Compute`): reflected poly 0x8408, init 0, no final
xor."""

from __future__ import annotations


def _table() -> list[int]:
    out = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
        out.append(crc)
    return out


_TABLE = _table()


def crc16_kermit(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = _TABLE[(b ^ crc) & 0xFF] ^ (crc >> 8)
    return crc
