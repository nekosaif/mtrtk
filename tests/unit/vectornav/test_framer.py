from pathlib import Path

import pytest

from mtrtk.core.frames import Proto
from mtrtk.rover.drivers.vectornav import fields
from mtrtk.rover.drivers.vectornav.checksum import finalize_ascii
from mtrtk.rover.drivers.vectornav.framer import VnFramer
from mtrtk.rover.drivers.vectornav.parse import VnAscii, VnBinary

from .helpers import (
    ATTITUDE,
    GPS,
    IMU,
    INS,
    TIME,
    att_payload,
    binary,
    build_binary,
    gps_payload,
    imu_payload,
    ins_payload,
    time_group_payload,
)

FIXTURE = Path(__file__).parents[2] / "fixtures" / "ins" / "vn_frames.hex"


def test_binary_frame_roundtrip() -> None:
    raw = binary((TIME, time_group_payload()), (INS, ins_payload()))
    framer = VnFramer()
    out = framer.feed(raw)
    assert len(out) == 1
    assert out[0].proto is Proto.VN and out[0].identity == "VN-BIN" and out[0].raw == raw
    assert fields.binary_length(raw) == len(raw)
    assert framer.stats.frames == 1 and framer.stats.bytes_skipped == 0


def test_variable_satinfo_and_rawmeas_length() -> None:
    sats = [(0, 1, 0x1F, 40, 7, 30, 10), (0, 2, 0x1F, 41, 7, 31, 11), (2, 3, 0x1F, 42, 7, 32, 12)]
    mask, data, ext = gps_payload(sats=sats, meas=2)
    raw = build_binary({GPS: (mask, data, ext)})
    # sync + groups + field + ext, then the fixed GPS fields of 0x7ABA, then the CRC
    fixed = 1 + 1 + 2 + 2 + (8 + 1 + 1 + 24 + 12 + 12 + 4 + 2 + 28) + 2
    assert fields.binary_length(raw) == fixed + 2 + 24 + 12 + 56 == len(raw)
    framer = VnFramer()
    third = len(raw) // 3
    assert framer.feed(raw[:third]) == []
    assert framer.feed(raw[third : 2 * third]) == []
    out = framer.feed(raw[2 * third :])
    assert len(out) == 1 and out[0].raw == raw


def test_binary_length_needs_more_bytes() -> None:
    """None until the bytes that fix the length (the RawMeas count, last) are in; then exact."""
    raw = binary((GPS, gps_payload(meas=3)))
    got = [fields.binary_length(raw[:cut]) for cut in range(len(raw) + 1)]
    first = got.index(len(raw))
    assert all(g is None for g in got[:first]) and all(g == len(raw) for g in got[first:])
    raw_meas_off = [o for g, b, o, _ in fields.field_layout(raw) if b == fields.RAWMEAS_BIT][0]
    assert first == raw_meas_off + fields.RAWMEAS_COUNT_OFFSET + 1


def test_field_layout_offsets() -> None:
    raw = binary((TIME, time_group_payload()), (INS, ins_payload()))
    layout = fields.field_layout(raw)
    # header: FA, groups, time field u16, ins field u16 -> payload at 6
    assert layout[0] == (1, 1, 6, 8)  # Time/TimeGps
    assert [(g, b) for g, b, _, _ in layout][-1] == (5, 10)  # INS/VelU last
    offset_end = layout[-1][2] + layout[-1][3]
    assert offset_end == len(raw) - 2


def test_invalid_header_resyncs() -> None:
    framer = VnFramer()
    out = framer.feed(b"\xfa\x80\x00\x00\x00\x00" + b"\xfa\x02\x00\x04\x00\x00\x00\x00")
    assert out == []
    assert framer.stats.invalid_headers == 2
    assert framer.stats.bytes_skipped > 0 and framer.stats.resyncs >= 1


def test_implausible_counts_are_invalid() -> None:
    mask, data, ext = gps_payload()
    raw = bytearray(build_binary({GPS: (mask, data, ext)}))
    sat_off = [o for g, b, o, _ in fields.field_layout(bytes(raw)) if (g, b) == (3, 14)][0]
    raw[sat_off] = 65  # numSats > 64
    with pytest.raises(ValueError, match="numSats"):
        fields.binary_length(bytes(raw))


def test_crc_failure_counted() -> None:
    good = binary((TIME, time_group_payload()), (INS, ins_payload()))
    bad = bytearray(good)
    bad[10] ^= 0xFF
    framer = VnFramer()
    out = framer.feed(bytes(bad) + good)
    assert framer.stats.crc_failed == 1
    assert len(out) == 1 and out[0].raw == good


def test_ascii_frames_and_mixed_stream() -> None:
    ascii_line = finalize_ascii("VNRRG,01,VN-200T-CR")
    bin_frame = binary((TIME, time_group_payload()))
    bad_ascii = b"$VNRRG,01,VN-200T-CR*00\r\n"
    err = finalize_ascii("VNERR,07")
    framer = VnFramer()
    out = framer.feed(b"\x00\x13" + ascii_line + bin_frame + bad_ascii + err)
    assert [f.identity for f in out] == ["VN-ASCII", "VN-BIN", "VN-ASCII"]
    assert framer.stats.crc_failed == 1
    assert framer.stats.ascii_frames == 2 and framer.stats.frames == 3
    parsed = out[2].parsed()
    assert isinstance(parsed, VnAscii) and parsed.error == 7


def test_ascii_split_across_chunks_and_crc16_mode() -> None:
    line = finalize_ascii("VNRRG,04,2.0.0.0", crc=True)
    framer = VnFramer()
    assert framer.feed(line[:7]) == []
    out = framer.feed(line[7:])
    assert len(out) == 1 and out[0].identity == "VN-ASCII"


def test_overlong_ascii_discarded() -> None:
    framer = VnFramer()
    junk = b"$VN" + b"A" * 600
    good = finalize_ascii("VNRRG,01,VN-200")
    out = framer.feed(junk + good)
    assert [f.raw for f in out] == [good]
    assert framer.stats.bytes_skipped >= 600


def test_dollar_inside_binary_garbage_does_not_stall() -> None:
    """A stray '$' followed by non-printable bytes is not an ASCII line: skip at once."""
    framer = VnFramer()
    bin_frame = binary((TIME, time_group_payload()))
    out = framer.feed(b"$VN\x00\x01" + bin_frame)
    assert [f.raw for f in out] == [bin_frame]


def test_parser_registered() -> None:
    raw = binary((TIME, time_group_payload()))
    (frame,) = VnFramer().feed(raw)
    assert isinstance(frame.parsed(), VnBinary)


def _fixture_bytes() -> bytes:
    lines = [ln.split("#", 1)[0].strip() for ln in FIXTURE.read_text().splitlines()]
    return bytes.fromhex("".join(ln for ln in lines if ln))


def test_golden_fixture() -> None:
    framer = VnFramer()
    out = framer.feed(_fixture_bytes())
    assert [f.identity for f in out] == [
        "VN-BIN",
        "VN-BIN",
        "VN-ASCII",
        "VN-ASCII",
        "VN-ASCII",
    ]
    assert framer.stats.crc_failed == 0 and framer.stats.bytes_skipped == 0
    parsed = [f.parsed() for f in out]
    assert all(p is not None for p in parsed)
    full, with_raw = parsed[0], parsed[1]
    assert isinstance(full, VnBinary) and set(full.groups) == {
        "time",
        "imu",
        "gps",
        "attitude",
        "ins",
    }
    assert isinstance(with_raw, VnBinary) and with_raw.groups["gps"]["raw_meas_count"] == 2
    assert [p.cmd for p in parsed[2:]] == ["RRG", "WRG", "ERR"]  # type: ignore[union-attr]


def test_golden_fixture_is_current() -> None:
    """The fixture is regenerated from the helpers; this keeps them from drifting apart."""
    assert _fixture_bytes() == golden_bytes()


def golden_bytes() -> bytes:
    full = binary(
        (TIME, time_group_payload()),
        (IMU, imu_payload()),
        (GPS, gps_payload()),
        (ATTITUDE, att_payload()),
        (INS, ins_payload()),
    )
    with_raw = binary((TIME, time_group_payload()), (GPS, gps_payload(meas=2)))
    return (
        full
        + with_raw
        + finalize_ascii("VNRRG,01,VN-200T-CR")
        + finalize_ascii("VNWRG,75,1,80,3E,2DE,611,FABA,1,103,613")
        + finalize_ascii("VNERR,07")
    )
