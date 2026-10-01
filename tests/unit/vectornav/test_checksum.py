from mtrtk.rover.drivers.vectornav.checksum import crc16_xmodem, finalize_ascii, verify_ascii, xor8


def test_check_values() -> None:
    assert crc16_xmodem(b"123456789") == 0x31C3
    assert xor8(b"VNRRG,01,VN-200") == 0x59


def test_ascii_finalize_and_verify_both_modes() -> None:
    line = finalize_ascii("VNRRG,01")
    assert line == b"$VNRRG,01*" + f"{xor8(b'VNRRG,01'):02X}".encode() + b"\r\n"
    assert verify_ascii(line)
    crc_line = finalize_ascii("VNRRG,01", crc=True)
    assert len(crc_line.split(b"*")[1].strip()) == 4 and verify_ascii(crc_line)
    assert not verify_ascii(b"$VNRRG,01*00\r\n")


def test_binary_crc_residue() -> None:
    body = b"\x01\x02\x03"
    frame = body + crc16_xmodem(body).to_bytes(2, "big")
    assert crc16_xmodem(frame) == 0


def test_verify_ascii_rejects_malformed_lines() -> None:
    assert not verify_ascii(b"VNRRG,01*59\r\n")  # no '$'
    assert not verify_ascii(b"$VNRRG,01\r\n")  # no '*'
    assert not verify_ascii(b"$VNRRG,01*ZZ\r\n")  # not hex
    assert not verify_ascii(b"$VNRRG,01*123\r\n")  # three digits: neither mode
    # lowercase hex is accepted on receive; a line without CR/LF verifies too
    good = finalize_ascii("VNRRG,01")
    assert verify_ascii(good.lower().replace(b"$vnrrg", b"$VNRRG"))
    assert verify_ascii(good.rstrip(b"\r\n"))
