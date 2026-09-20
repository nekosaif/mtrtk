import logging

import pytest

from mtrtk.base.rtcm1005 import decode_1005
from mtrtk.core.frames import Framer
from ubxtest import rtcm_frame, ubx_frame

RTCM_1005_HEX = "d300133ed7fd0382dfdc1c403db34fe8fe0cef5e6b30bd2e23"


def test_decode_1005_vector() -> None:
    frame = Framer().feed(bytes.fromhex(RTCM_1005_HEX))[0]
    ecef = decode_1005(frame)
    assert ecef is not None
    assert ecef.station_id == 2045
    assert (ecef.x, ecef.y, ecef.z) == (1234567.8912, -987654.3234, 5555555.0)
    assert ecef.gps and ecef.glonass and ecef.galileo


def test_decode_1005_ignores_other_frames() -> None:
    framer = Framer()
    other_rtcm, ubx = framer.feed(
        rtcm_frame(1077, b"\x00" * 20) + ubx_frame(0x01, 0x07, b"\x00" * 92)
    )
    assert decode_1005(other_rtcm) is None
    assert decode_1005(ubx) is None


def test_decode_1005_returns_none_for_a_truncated_1005(caplog: pytest.LogCaptureFixture) -> None:
    """A corrupt-but-CRC-valid 1005 must not take the caller down."""
    frame = Framer().feed(rtcm_frame(1005, b"\x00" * 4))[0]
    with caplog.at_level(logging.WARNING):
        assert decode_1005(frame) is None
    assert "1005" in caplog.text
