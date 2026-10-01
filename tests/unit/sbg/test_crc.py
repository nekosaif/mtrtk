from mtrtk.rover.drivers.sbg.crc import crc16_kermit


def test_kermit_check_value() -> None:
    assert crc16_kermit(b"123456789") == 0x2189
    assert crc16_kermit(b"") == 0
