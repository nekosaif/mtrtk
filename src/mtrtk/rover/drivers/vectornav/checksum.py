"""VectorNav checksums: CRC-16/XMODEM for binary frames, XOR-8 or CRC-16 for ASCII lines.

Binary: CRC-16/CCITT (poly 0x1021, init 0, no reflection, "XMODEM") over everything after the
0xFA sync byte, appended big-endian, so running it over the frame including the CRC gives 0.
ASCII: `$VN...*CS\\r\\n` where CS covers the bytes between `$` and `*`: two hex digits of an
8-bit XOR, or four hex digits of the same CRC-16 when register 30 selects it. Either is
accepted on receive.
"""

from __future__ import annotations


def _crc_table() -> list[int]:
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
        table.append(crc & 0xFFFF)
    return table


_CRC_TABLE = _crc_table()


def crc16_xmodem(data: bytes | bytearray | memoryview) -> int:
    crc = 0
    for b in bytes(data):
        crc = ((crc << 8) & 0xFFFF) ^ _CRC_TABLE[((crc >> 8) ^ b) & 0xFF]
    return crc


def xor8(data: bytes | bytearray) -> int:
    cs = 0
    for b in data:
        cs ^= b
    return cs


def verify_ascii(line: bytes | bytearray) -> bool:
    """True when *line* (`$...*XX` or `$...*XXXX`, CR/LF optional) carries a valid checksum."""
    text = bytes(line).rstrip(b"\r\n")
    if not text.startswith(b"$"):
        return False
    star = text.rfind(b"*")
    if star < 0:
        return False
    body, given = text[1:star], text[star + 1 :]
    try:
        value = int(given, 16)
    except ValueError:
        return False
    if len(given) == 2:
        return value == xor8(body)
    if len(given) == 4:
        return value == crc16_xmodem(body)
    return False


def finalize_ascii(body: str, *, crc: bool = False) -> bytes:
    """`"VNRRG,01"` -> `b"$VNRRG,01*XX\\r\\n"` (XOR-8), or with a 4-digit CRC-16 when *crc*."""
    raw = body.encode("ascii")
    cs = f"{crc16_xmodem(raw):04X}" if crc else f"{xor8(raw):02X}"
    return b"$" + raw + b"*" + cs.encode("ascii") + b"\r\n"
