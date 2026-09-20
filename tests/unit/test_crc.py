from pyrtcm.rtcmhelpers import calc_crc24q
from pyubx2.ubxhelpers import calc_checksum

from mtrtk.core.crc import crc24q, nmea_checksum, ubx_checksum


def test_crc24q_known_vector() -> None:
    # CRC-24/LTE-A == RTCM CRC-24Q: poly 0x864CFB, init 0, check value for "123456789"
    assert crc24q(b"123456789") == 0xCDE703


def test_crc24q_matches_pyrtcm() -> None:
    data = bytes(range(256)) * 3
    assert crc24q(data) == calc_crc24q(data)


def test_ubx_checksum_matches_pyubx2() -> None:
    body = b"\x01\x07\x04\x00\xde\xad\xbe\xef"
    assert ubx_checksum(body) == calc_checksum(body)


def test_nmea_checksum_textbook_gga() -> None:
    assert nmea_checksum(b"GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,") == 0x47
