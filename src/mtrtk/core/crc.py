"""Checksums used by the GNSS wire protocols we frame."""

from __future__ import annotations


def ubx_checksum(data: bytes) -> bytes:
    """8-bit Fletcher checksum over UBX class, id, length and payload -> bytes((CK_A, CK_B))."""
    ck_a = ck_b = 0
    for byte in data:
        ck_a = (ck_a + byte) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return bytes((ck_a, ck_b))


def _build_crc24q_table() -> list[int]:
    table: list[int] = []
    for i in range(256):
        crc = i << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
        table.append(crc & 0xFFFFFF)
    return table


_CRC24Q_TABLE = _build_crc24q_table()


def crc24q(data: bytes) -> int:
    """CRC-24Q as used by RTCM 3 (poly 0x1864CFB, init 0), returned as a 24-bit int."""
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFF) ^ _CRC24Q_TABLE[((crc >> 16) ^ byte) & 0xFF]
    return crc


def nmea_checksum(body: bytes) -> int:
    """XOR of every byte between '$' and '*' (both exclusive)."""
    result = 0
    for byte in body:
        result ^= byte
    return result
