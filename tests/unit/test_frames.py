from pyubx2 import GET, UBXMessage

from mtrtk.core.frames import Frame, Framer, Proto
from ubxtest import nmea_frame, rtcm_frame, ubx_frame

PVT = ubx_frame(0x01, 0x07, b"\x00" * 92)
SAT_EMPTY = ubx_frame(0x01, 0x35, b"\x00" * 8)
RAWX_EMPTY = ubx_frame(0x02, 0x15, b"\x00" * 16)


def test_ubx_single_feed() -> None:
    frames = Framer().feed(PVT)
    assert len(frames) == 1
    f = frames[0]
    assert f.proto is Proto.UBX
    assert f.identity == "NAV-PVT"
    assert f.ubx_class_id == (0x01, 0x07)
    assert f.payload == b"\x00" * 92


def test_frame_split_across_feeds() -> None:
    framer = Framer()
    assert framer.feed(PVT[:10]) == []
    out = framer.feed(PVT[10:])
    assert len(out) == 1 and out[0].raw == PVT


def test_garbage_is_skipped_and_counted() -> None:
    framer = Framer()
    out = framer.feed(b"\x00\xffjunk" + SAT_EMPTY)
    assert [f.identity for f in out] == ["NAV-SAT"]
    assert framer.stats.garbage_bytes == 6


def test_bad_ubx_checksum_is_dropped() -> None:
    corrupt = bytearray(PVT)
    corrupt[-1] ^= 0xFF
    framer = Framer()
    assert framer.feed(bytes(corrupt)) == []
    assert framer.stats.checksum_errors == 1


def test_stray_sync_byte_before_frame() -> None:
    out = Framer().feed(b"\xb5" + PVT)
    assert len(out) == 1 and out[0].identity == "NAV-PVT"


def test_rtcm_frame_type_and_identity() -> None:
    out = Framer().feed(rtcm_frame(1077, b"\x00" * 20))
    assert out[0].proto is Proto.RTCM3
    assert out[0].rtcm_type == 1077
    assert out[0].identity == "1077"
    assert len(out[0].payload) == 20


def test_bad_rtcm_crc_is_dropped() -> None:
    corrupt = bytearray(rtcm_frame(1005, b"\x00" * 16))
    corrupt[-1] ^= 0x01
    framer = Framer()
    assert framer.feed(bytes(corrupt)) == []
    assert framer.stats.checksum_errors == 1


def test_nmea_frame() -> None:
    out = Framer().feed(
        nmea_frame("GNGGA,123519.00,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,")
    )
    assert out[0].proto is Proto.NMEA
    assert out[0].identity == "GNGGA"


def test_mixed_stream_in_order() -> None:
    stream = (
        PVT
        + rtcm_frame(1005, b"\x00" * 16)
        + nmea_frame("GNGGA,,,,,,0,00,99.99,,,,,,")
        + RAWX_EMPTY
    )
    out = Framer().feed(stream)
    assert [f.proto for f in out] == [Proto.UBX, Proto.RTCM3, Proto.NMEA, Proto.UBX]
    assert out[3].identity == "RXM-RAWX"


def test_stats_count_frames() -> None:
    framer = Framer()
    framer.feed(PVT + PVT + rtcm_frame(1230))
    assert framer.stats.frames == {"ubx": 2, "rtcm3": 1, "nmea": 0}


def test_lazy_parse_is_cached() -> None:
    raw = UBXMessage("NAV", "NAV-EOE", GET, iTOW=1234).serialize()
    frame: Frame = Framer().feed(raw)[0]
    assert frame.parsed().iTOW == 1234
    assert frame.parsed() is frame.parsed()


def test_unknown_ubx_identity_falls_back_to_hex() -> None:
    out = Framer().feed(ubx_frame(0x7E, 0x7F, b"\x00"))
    assert out[0].identity == "UBX-7E-7F"
