import struct
from pathlib import Path

import pytest

from mtrtk.core.frames import Proto
from mtrtk.rover.drivers.sbg.crc import crc16_kermit
from mtrtk.rover.drivers.sbg.framer import MAX_PAYLOAD, SbgFramer, encode
from mtrtk.rover.drivers.sbg.ids import CLASS, LOG
from sbgtest import ekf_nav_payload, golden_hex

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ins"
GOLDEN = [
    "EKF_NAV",
    "EKF_EULER",
    "UTC_TIME",
    "GPS1_POS",
    "GPS1_HDT",
    "GPS1_SAT",
    "EVENT_B",
    "IMU_SHORT",
    "STATUS",
    "CMD-ACK",
]


def raw_frame(msg_class: int, msg_id: int, payload: bytes) -> bytes:
    """Bypasses `encode` (which refuses large and paged frames)."""
    body = bytes([msg_id, msg_class]) + struct.pack("<H", len(payload)) + payload
    return b"\xff\x5a" + body + struct.pack("<H", crc16_kermit(body)) + b"\x33"


def test_encode_roundtrip_and_identity() -> None:
    raw = encode(CLASS["LOG_ECOM_0"], LOG["EKF_NAV"], ekf_nav_payload())
    assert raw[:2] == b"\xff\x5a" and raw[-1] == 0x33 and raw[2] == 8 and raw[3] == 0
    frames = SbgFramer().feed(raw)
    assert len(frames) == 1 and frames[0].proto is Proto.SBG and frames[0].identity == "EKF_NAV"
    assert frames[0].payload == ekf_nav_payload()


def test_byte_at_a_time() -> None:
    raw = encode(0, LOG["EKF_NAV"], ekf_nav_payload()) * 2
    f = SbgFramer()
    out = [fr for b in raw for fr in f.feed(bytes([b]))]
    assert [x.identity for x in out] == ["EKF_NAV", "EKF_NAV"] and f.stats.bytes_skipped == 0


def test_split_stream_garbage_and_crc_failure() -> None:
    good = encode(0, LOG["UTC_TIME"], bytes(21))
    bad = bytearray(good)
    bad[10] ^= 0xFF  # corrupt payload -> CRC mismatch
    f = SbgFramer()
    out = f.feed(b"\x00\x11" + bytes(bad) + good[:7])
    out += f.feed(good[7:] + b"\xff")  # a lone sync byte stays buffered
    assert [x.identity for x in out] == ["UTC_TIME"]
    assert f.stats.crc_failed == 1 and f.stats.frames == 1 and f.stats.resyncs == 1
    assert f.stats.bytes_skipped == 2 + len(bad)  # the garbage, then the rejected frame
    assert f.feed(b"\x5a") == [] and bytes(f.buf) == b"\xff\x5a"  # ... and pairs with the 5A
    assert [x.identity for x in f.feed(good[2:])] == ["UTC_TIME"]


def test_bad_etx_counts_as_corrupt_and_resyncs_one_byte_later() -> None:
    good = encode(0, LOG["STATUS"], bytes(22))
    bad = bytearray(good)
    bad[-1] = 0x34
    f = SbgFramer()
    assert [x.identity for x in f.feed(bytes(bad) + good)] == ["STATUS"]
    assert f.stats.crc_failed == 1 and f.stats.resyncs == 1
    assert f.stats.bytes_skipped == len(bad)


def test_dropped_byte_counts_as_corrupt() -> None:
    """A byte lost on the link moves the ETX: the frame is counted in crc_failed, not only as a
    resync, so `crc_failed` tracks a lossy cable (Task 7's troubleshooting text)."""
    good = encode(0, LOG["UTC_TIME"], bytes(range(21)))
    lossy = good[:12] + good[13:]
    f = SbgFramer()
    assert [x.identity for x in f.feed(lossy + good)] == ["UTC_TIME"]
    assert f.stats.crc_failed == 1 and f.stats.frames == 1


def test_false_sync_does_not_swallow_following_frames() -> None:
    """A corrupt header's LEN is never trusted: frames inside its span still come out."""
    status, utc = encode(0, LOG["STATUS"], bytes(22)), encode(0, LOG["UTC_TIME"], bytes(21))
    f = SbgFramer()
    bogus = b"\xff\x5a\x01\x00" + struct.pack("<H", 32)  # ETX lands inside UTC_TIME
    out = f.feed(bogus + status + utc + bytes(64))
    assert [x.identity for x in out] == ["STATUS", "UTC_TIME"]
    assert f.stats.crc_failed == 1 and f.stats.resyncs == 1


def test_false_sync_with_good_etx_but_bad_crc() -> None:
    status = encode(0, LOG["STATUS"], bytes(22))
    bogus = b"\xff\x5a\x01\x00" + struct.pack("<H", len(status) - 3)  # ETX = STATUS's 0x33
    f = SbgFramer()
    assert [x.identity for x in f.feed(bogus + status)] == ["STATUS"]
    assert f.stats.crc_failed == 1 and f.stats.resyncs == 1 and f.stats.bytes_skipped == 6


def test_unknown_class_header_is_rejected_without_waiting() -> None:
    """A false FF 5A whose class is not an on-wire class is dropped at once rather than holding
    back the real frames behind it until its (plausible) LEN has arrived."""
    imu = encode(0, LOG["IMU_SHORT"], bytes(32))
    f = SbgFramer()
    out = f.feed(b"\xff\x5a\x01\x42" + struct.pack("<H", 4000) + imu * 50)
    assert len(out) == 50 and f.stats.resyncs == 1 and f.stats.crc_failed == 0
    assert not f.buf


@pytest.mark.parametrize("cls", [0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x10, 0x90])
def test_known_classes_still_wait_for_their_length(cls: int) -> None:
    f = SbgFramer()
    assert f.feed(b"\xff\x5a\x01" + bytes([cls]) + struct.pack("<H", 100)) == []
    assert f.stats.resyncs == 0 and len(f.buf) == 6


def test_implausible_length_does_not_stall() -> None:
    """LEN > 4086 is an invalid header (sbgECom drops it); never wait for 64 KiB."""
    header = b"\xff\x5a\x08\x00" + struct.pack("<H", MAX_PAYLOAD + 1)
    good = encode(0, LOG["EKF_NAV"], ekf_nav_payload())
    f = SbgFramer()
    assert [x.identity for x in f.feed(header + good)] == ["EKF_NAV"]
    assert f.stats.resyncs == 1


def test_largest_standard_frame() -> None:
    raw = encode(0, LOG["GPS1_RAW"], bytes(MAX_PAYLOAD))
    assert [x.identity for x in SbgFramer().feed(raw)] == ["GPS1_RAW"]
    with pytest.raises(ValueError):
        encode(0, LOG["GPS1_RAW"], bytes(MAX_PAYLOAD + 1))


def test_extended_frame_is_skipped() -> None:
    """sbgECom 5.x: class bit 7 marks an extended (paged) frame; LEN spans its 5-byte paging
    header (transferId u8, pageIndex u16, nrPages u16). This phase reassembles nothing."""
    payload = b"\x01" + struct.pack("<HH", 0, 2) + bytes(100)
    raw = raw_frame(0x80 | CLASS["CMD_0"], 3, payload)
    good = encode(0, LOG["STATUS"], bytes(22))
    f = SbgFramer()
    assert [x.identity for x in f.feed(raw + good)] == ["STATUS"]
    assert f.stats.extended_dropped == 1 and f.stats.frames == 1 and f.stats.bytes_skipped == 0


def test_corrupt_extended_frame_is_not_counted_as_extended() -> None:
    """The CRC is checked before class bit 7 is believed (or the frame's LEN is skipped)."""
    raw = bytearray(
        raw_frame(0x80 | CLASS["CMD_0"], 3, b"\x01" + struct.pack("<HH", 0, 2) + bytes(40))
    )
    raw[20] ^= 0x01
    good = encode(0, LOG["STATUS"], bytes(22))
    f = SbgFramer()
    assert [x.identity for x in f.feed(bytes(raw) + good)] == ["STATUS"]
    assert f.stats.extended_dropped == 0 and f.stats.crc_failed == 1 and f.stats.resyncs == 1


def test_command_identity() -> None:
    raw = encode(CLASS["CMD_0"], 0, b"\x1e\x10\x00\x00")
    assert SbgFramer().feed(raw)[0].identity == "CMD-ACK"


def test_other_class_identity() -> None:
    assert SbgFramer().feed(encode(0x03, 2, b"$PASHR"))[0].identity == "SBG-03-02"
    assert SbgFramer().feed(encode(0, 0xEE, b""))[0].identity == "SBG-00-EE"
    assert SbgFramer().feed(encode(CLASS["CMD_0"], 0xEE, b""))[0].identity == "CMD-EE"


def test_golden_fixture_is_reproducible() -> None:
    """`uv run python tests/sbgtest.py` rebuilds the golden file from the encoders."""
    assert (FIXTURES / "sbg_frames.hex").read_text() == golden_hex()


def test_golden_fixture_parses() -> None:
    lines = (FIXTURES / "sbg_frames.hex").read_text().split()
    frames = SbgFramer().feed(b"".join(bytes.fromhex(x) for x in lines))
    assert [x.identity for x in frames] == GOLDEN
    assert all(x.parsed() is not None for x in frames)


def test_golden_fixture_pins_fields() -> None:
    lines = (FIXTURES / "sbg_frames.hex").read_text().split()
    parsed = {
        f.identity: f.parsed() for f in SbgFramer().feed(b"".join(bytes.fromhex(x) for x in lines))
    }
    assert parsed["EKF_NAV"].lat == 23.7275 and parsed["EKF_NAV"].position_valid
    assert parsed["GPS1_POS"].uptime_s == 3600 and parsed["GPS1_POS"].diff_age_s == 1.2
    assert parsed["UTC_TIME"].utc.isoformat() == "2026-09-19T10:30:15.250000+00:00"
    assert parsed["GPS1_HDT"].num_sv_used == 14 and parsed["EVENT_B"].offsets_us == (100, 250)
    assert parsed["STATUS"].cpu_usage == 42 and parsed["CMD-ACK"].ok
    assert [s.id for s in parsed["GPS1_SAT"].sats] == [12, 3]


def test_live_ellipse_d_slice() -> None:
    """One 1 Hz cycle captured read-only from a real Ellipse-D (firmware as shipped, 921600 baud)
    on 2026-10-01. GPS1_RAW frames were removed and the GPS1_POS / EKF_NAV lat/lon moved by a
    fixed offset (those frames re-encoded); everything else is byte-for-byte as received.

    Accepted residual risk: altitude, geoid undulation, the UTC time and GPS1_SAT elevation /
    azimuth are real, so the antenna's region (not its site) can be recovered from them. That is
    the same metro area the stand-in coordinates already name; no serial number is included."""
    f = SbgFramer()
    frames = f.feed((FIXTURES / "ellipse_d_live_1s.sbg").read_bytes())
    assert f.stats.crc_failed == 0 and f.stats.bytes_skipped == 0 and f.stats.resyncs == 0
    counts: dict[str, int] = {}
    for fr in frames:
        counts[fr.identity] = counts.get(fr.identity, 0) + 1
    assert counts == {
        "STATUS": 1,
        "UTC_TIME": 1,
        "GPS1_SAT": 1,
        "IMU_SHORT": 200,
        "EKF_EULER": 50,
        "EKF_NAV": 50,
        "GPS1_POS": 5,
        "GPS1_VEL": 5,
        "GPS1_HDT": 5,
    }
    lengths = {fr.identity: len(fr.payload) for fr in frames if fr.identity != "GPS1_SAT"}
    assert lengths == {
        "STATUS": 27,  # with uptime + cpuUsage
        "UTC_TIME": 33,  # with the three clock floats
        "IMU_SHORT": 32,
        "EKF_EULER": 40,  # with magnetic declination / inclination
        "EKF_NAV": 72,
        "GPS1_POS": 62,  # up to numSvTracked/statusExt (pre-5.6 tail)
        "GPS1_VEL": 44,
        "GPS1_HDT": 32,  # baseline + numSvTracked + numSvUsed
    }
    assert all(fr.parsed() is not None for fr in frames)
    by = {fr.identity: fr.parsed() for fr in frames}
    status, utc, imu = by["STATUS"], by["UTC_TIME"], by["IMU_SHORT"]
    assert status.general == 0x7F and status.aiding & 0x0F == 0x0F  # all OK, GPS1 pos/vel/hdt/utc
    assert utc.utc is not None and utc.utc.year == 2026 and utc.clock_state == 3
    assert utc.utc_status == 2 and utc.utc_sync
    # UTC + 18 leap seconds == GPS time of week
    assert (utc.gps_tow_ms // 1000 - 18) % 86400 == utc.hour * 3600 + utc.minute * 60 + utc.second
    assert not imu.high_range and -10.1 < imu.accel_mps2[2] < -9.5 and 0 < imu.temperature_c < 80
    pos, hdt, nav = by["GPS1_POS"], by["GPS1_HDT"], by["EKF_NAV"]
    assert pos.solution_computed and pos.num_sv_used and pos.num_sv_tracked >= pos.num_sv_used
    assert pos.undulation == nav.undulation  # same geoid model on both logs
    assert hdt.solution_computed and hdt.baseline_valid and 1.0 < hdt.baseline_m < 1.5
    sats = by["GPS1_SAT"].sats
    assert {s.constellation for s in sats} >= {1, 2, 3, 4} and all(s.signals for s in sats)
