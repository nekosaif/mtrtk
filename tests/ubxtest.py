"""Builders for synthetic wire frames used across the test-suite."""

from __future__ import annotations

from mtrtk.core.crc import crc24q, nmea_checksum, ubx_checksum


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
