import asyncio
import json
import math
import os
import tty
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pynmeagps import NMEAReader

from mtrtk.config import NMEA_SENTENCE_NAMES
from mtrtk.core.bus import Bus
from mtrtk.core.crc import nmea_checksum
from mtrtk.core.state import Attitude, FixInfo, ReceiverState, Satellite
from mtrtk.core.statestore import StateStore
from mtrtk.rover import nmea_out
from mtrtk.rover.json_out import JsonUdpPublisher, epoch_json
from mtrtk.rover.nmea_out import (
    ALL_SENTENCES,
    NmeaPublisher,
    build_gga,
    build_gsa,
    build_gst,
    build_gsv,
    build_hdt,
    build_pashr,
    build_rmc,
    build_sentences,
    build_vtg,
    build_zda,
    gga_quality,
)
from mtrtk.rover.sinks import SerialSink


def rover_state() -> ReceiverState:
    s = ReceiverState()
    s.time.utc = datetime(2026, 9, 18, 16, 47, 34, 120000, tzinfo=UTC)
    s.position.lat, s.position.lon = 23.83735067, 90.26255021
    s.position.height_m, s.position.hmsl_m = -36.268, 13.363
    s.accuracy.h_acc_m, s.accuracy.v_acc_m = 0.012, 0.018
    s.fix = FixInfo(
        fix_type=3,
        fix_type_name="3D",
        gnss_fix_ok=True,
        diff_soln=True,
        carr_soln=2,
        carr_soln_name="RTK fixed",
        num_sv=27,
    )
    s.rtk.carr_soln, s.rtk.corr_age_s, s.rtk.ref_station_id = 2, 1.2, 7
    s.dops.p, s.dops.h, s.dops.v = 1.2, 0.7, 1.0
    s.velocity.ground_speed_mps, s.velocity.heading_motion_deg = 1.5, 123.4
    s.sats = [
        Satellite(gnss_id=0, gnss="GPS", sv_id=5, cno=45, elev=72, azim=120, used=True),
        Satellite(gnss_id=0, gnss="GPS", sv_id=12, cno=38, elev=35, azim=210, used=True),
        Satellite(gnss_id=0, gnss="GPS", sv_id=25, cno=22, elev=8, azim=300, used=False),
        Satellite(gnss_id=6, gnss="GLONASS", sv_id=3, cno=40, elev=50, azim=40, used=True),
        Satellite(gnss_id=2, gnss="Galileo", sv_id=4, cno=42, elev=60, azim=180, used=True),
        Satellite(gnss_id=3, gnss="BeiDou", sv_id=21, cno=36, elev=44, azim=260, used=True),
    ]
    return s


def _checksum_ok(raw: bytes) -> bool:
    body, _, tail = raw[1:].partition(b"*")
    return raw.endswith(b"\r\n") and tail[:2] == f"{nmea_checksum(body):02X}".encode()


def test_quality_mapping() -> None:
    assert gga_quality(FixInfo(fix_type=0)) == 0
    assert gga_quality(FixInfo(fix_type=3)) == 1
    assert gga_quality(FixInfo(fix_type=3, diff_soln=True)) == 2
    assert gga_quality(FixInfo(fix_type=3, carr_soln=1)) == 5
    assert gga_quality(FixInfo(fix_type=3, carr_soln=2)) == 4


def test_quality_mapping_dead_reckoning_and_time_only() -> None:
    assert gga_quality(FixInfo(fix_type=1)) == 6  # dead reckoning only: "estimated"
    assert gga_quality(FixInfo(fix_type=4)) == 1  # GNSS + DR is a GNSS fix
    assert gga_quality(FixInfo(fix_type=5)) == 7  # TMODE fixed: the position was entered
    assert gga_quality(FixInfo(fix_type=0, carr_soln=2)) == 0  # no fix wins over a stale flag


def test_gga_high_precision_and_fields() -> None:
    raw = build_gga(rover_state())
    assert raw is not None
    assert raw.startswith(
        b"$GNGGA,164734.12,2350.2410402,N,09015.7530126,E,4,27,0.7,13.363,M,-49.631,M,1.2,0007"
    )
    assert _checksum_ok(raw)
    m = NMEAReader.parse(raw)
    assert m.lat == pytest.approx(23.83735067, abs=1e-9)
    assert m.lon == pytest.approx(90.26255021, abs=1e-9)
    assert m.quality == 4 and m.numSV == 27 and m.alt == 13.363 and m.sep == -49.631


def test_gga_southern_western_hemisphere_and_no_corrections() -> None:
    s = rover_state()
    s.position.lat, s.position.lon = -33.85678912, -151.21529871
    s.fix = FixInfo(fix_type=3, gnss_fix_ok=True, num_sv=8)
    s.rtk.corr_age_s, s.rtk.ref_station_id = None, None
    s.dops.h = None
    raw = build_gga(s)
    assert raw is not None
    assert b",S," in raw and b",W," in raw and b",1,08,,13.363,M,-49.631,M,,*" in raw
    m = NMEAReader.parse(raw)
    assert m.lat == pytest.approx(-33.85678912, abs=1e-9)
    assert m.lon == pytest.approx(-151.21529871, abs=1e-9)


def test_gga_age_and_station_only_for_a_differential_fix() -> None:
    s = rover_state()
    s.fix = FixInfo(fix_type=3, gnss_fix_ok=True, num_sv=8)  # quality 1 ...
    assert s.rtk.corr_age_s == 1.2 and s.rtk.ref_station_id == 7  # ... with stale RTK fields
    raw = build_gga(s)
    assert raw is not None and b",1,08," in raw and raw.split(b"*")[0].endswith(b",M,,")


def test_fix_the_receiver_flags_invalid_goes_out_as_no_fix() -> None:
    s = rover_state()
    s.fix = FixInfo(fix_type=3, gnss_fix_ok=False, diff_soln=True, carr_soln=2, num_sv=5)
    s.attitude = Attitude(heading_deg=10.0)
    gga = NMEAReader.parse(build_gga(s))
    assert gga.quality == 0 and gga.lat == "" and gga.lon == ""
    rmc = NMEAReader.parse(build_rmc(s))
    assert rmc.status == "V" and rmc.posMode == "N" and rmc.lat == ""
    assert NMEAReader.parse(build_vtg(s)).posMode == "N"
    pashr = build_pashr(s)
    assert pashr is not None and pashr.split(b"*")[0].endswith(b",0,1")


def test_lost_fix_blanks_the_last_position() -> None:
    s = rover_state()
    s.fix = FixInfo(fix_type=0, carr_soln=2)  # a stale carrier flag after the fix went
    raw = build_gga(s)
    assert raw is not None
    assert raw.startswith(b"$GNGGA,164734.12,,,,,0,00,0.7,,M,,M,,*")
    pashr = build_pashr(s.model_copy(update={"attitude": Attitude(heading_deg=1.0)}))
    assert pashr is not None and pashr.split(b"*")[0].endswith(b",0,1")  # agrees with GGA
    s = rover_state()
    s.position.invalid_llh = True  # NAV-PVT says the lat/lon it carries are not valid
    gga = NMEAReader.parse(build_gga(s))
    assert gga.quality == 0 and gga.lat == ""


def test_gga_minutes_never_round_up_to_sixty() -> None:
    s = rover_state()
    s.position.lat = 23.99999999999  # 59.9999999994 min would print as 60.0000000 naively
    raw = build_gga(s)
    assert raw is not None and raw.split(b",")[2] == b"2400.0000000"


def test_gga_ellipsoid_height_only() -> None:
    s = rover_state()
    s.position.hmsl_m = None
    raw = build_gga(s)
    assert raw is not None and b",-36.268,M,0.0,M," in raw


def test_gga_none_without_position() -> None:
    assert build_gga(ReceiverState()) is None


def test_rmc_gst_vtg_zda() -> None:
    s = rover_state()
    rmc = NMEAReader.parse(build_rmc(s))
    assert rmc.status == "A" and str(rmc.date) == "2026-09-18" and rmc.posMode == "R"
    assert rmc.spd == pytest.approx(1.5 * 1.943844, abs=1e-3)
    gst = NMEAReader.parse(build_gst(s))
    assert gst.stdLat == pytest.approx(0.012 / 2**0.5, abs=1e-4) and gst.stdAlt == 0.018
    vtg = NMEAReader.parse(build_vtg(s))
    assert vtg.cogt == 123.4 and vtg.sogk == pytest.approx(5.4, abs=1e-3) and vtg.posMode == "R"
    zda = build_zda(s)
    assert zda is not None
    assert zda == b"$GNZDA,164734.12,18,09,2026,00,00*4F\r\n"[:-6] + zda[-6:]
    assert _checksum_ok(zda)
    assert NMEAReader.parse(zda).month == 9


def test_rmc_void_without_fix() -> None:
    s = rover_state()
    s.fix = FixInfo(fix_type=0)
    rmc = NMEAReader.parse(build_rmc(s))
    assert rmc.status == "V" and rmc.posMode == "N" and rmc.lat == "" and rmc.lon == ""


def test_gst_range_rms_from_the_used_satellites_residuals() -> None:
    s = rover_state()
    for sat, res in zip(s.sats, [0.3, -0.4, 9.0, 1.2, -0.5, 0.0], strict=True):
        sat.pr_res_m = res  # sats[2] is not used: its 9.0 must not count
    expected = math.sqrt((0.3**2 + 0.4**2 + 1.2**2 + 0.5**2 + 0.0**2) / 5)
    gst = NMEAReader.parse(build_gst(s))
    assert gst.rangeRms == pytest.approx(expected, abs=1e-4)
    s.sats = []
    gst = build_gst(s)
    assert gst is not None and gst.startswith(b"$GNGST,164734.12,,")  # no satellites: empty


def test_gsa_and_gsv_per_system() -> None:
    s = rover_state()
    gsa = [NMEAReader.parse(x) for x in build_gsa(s)]
    assert [m.talker for m in gsa] == ["GP", "GL", "GA", "GB"]
    gp = gsa[0]
    # pynmeagps keeps the hex systemId field as its text
    assert int(gp.systemId, 16) == 1 and gp.svid_01 == 5 and gp.svid_02 == 12 and gp.PDOP == 1.2
    gl = gsa[1]
    assert int(gl.systemId, 16) == 2 and gl.svid_01 == 67  # GLONASS slot 3 -> 67
    gsv = [NMEAReader.parse(x) for x in build_gsv(s)]
    assert [m.talker for m in gsv] == ["GP", "GL", "GA", "GB"]
    assert gsv[0].numSV == 3 and gsv[0].svid_01 == 5 and gsv[0].elv_01 == 72
    assert gsv[0].cno_03 == 22
    signal_ids = [raw.split(b"*")[0].rsplit(b",", 1)[1] for raw in build_gsv(s)]
    assert signal_ids == [b"1", b"1", b"7", b"1"]  # NMEA 4.11: Galileo E1 is signal 7


def test_gsv_blanks_out_of_range_elevation_and_azimuth() -> None:
    s = rover_state()
    s.sats = [
        Satellite(gnss_id=0, gnss="GPS", sv_id=1, cno=30, elev=-5, azim=100, used=False),
        Satellite(gnss_id=0, gnss="GPS", sv_id=2, cno=30, elev=91, azim=100, used=False),
        Satellite(gnss_id=0, gnss="GPS", sv_id=3, cno=30, elev=10, azim=360, used=True),
        Satellite(gnss_id=0, gnss="GPS", sv_id=4, cno=30, elev=10, azim=-1, used=True),
    ]
    (raw,) = build_gsv(s)
    assert b",01,,,30,02,,,30,03,10,000,30,04,10,,30," in raw
    assert _checksum_ok(raw)
    NMEAReader.parse(raw)


def test_gsv_splits_in_groups_of_four() -> None:
    s = rover_state()
    s.sats = [
        Satellite(gnss_id=0, gnss="GPS", sv_id=i, cno=30, elev=10, azim=i * 10, used=True)
        for i in range(1, 10)
    ]
    msgs = build_gsv(s)
    assert len(msgs) == 3
    parsed = [NMEAReader.parse(m) for m in msgs]
    assert [m.msgNum for m in parsed] == [1, 2, 3] and all(m.numMsg == 3 for m in parsed)
    assert parsed[2].svid_01 == 9


def test_gsa_caps_at_twelve_and_skips_unknown_glonass_slot() -> None:
    s = rover_state()
    s.sats = [
        Satellite(gnss_id=0, gnss="GPS", sv_id=i, cno=30, elev=10, azim=0, used=True)
        for i in range(1, 16)
    ] + [Satellite(gnss_id=6, gnss="GLONASS", sv_id=255, cno=30, elev=10, azim=0, used=True)]
    gsa = build_gsa(s)
    assert len(gsa) == 1  # the unknown-slot GLONASS satellite has no NMEA number
    assert gsa[0].count(b",") == 18 and _checksum_ok(gsa[0])
    assert len(build_gsv(s)) == 4


def test_attitude_sentences_only_with_attitude() -> None:
    s = rover_state()
    assert build_hdt(s) is None and build_pashr(s) is None
    s.attitude = Attitude(
        roll_deg=1.5,
        pitch_deg=-2.25,
        heading_deg=91.2,
        acc_roll_deg=0.1,
        acc_pitch_deg=0.1,
        acc_heading_deg=0.2,
        source="test",
    )
    assert NMEAReader.parse(build_hdt(s)).headingT == 91.2
    pashr = build_pashr(s)
    assert pashr is not None
    assert pashr.startswith(b"$PASHR,164734.12,91.20,T,1.50,-2.25,0.00,0.100,0.100,0.200,2,1*")
    assert _checksum_ok(pashr)


def test_heading_is_wrapped_after_rounding() -> None:
    s = rover_state()
    cases = [(370.0, b"10.00"), (359.996, b"0.00"), (-0.001, b"0.00"), (-90.0, b"270.00")]
    for heading, text in cases:
        s.attitude = Attitude(heading_deg=heading)
        hdt, pashr = build_hdt(s), build_pashr(s)
        assert hdt is not None and hdt.split(b",")[1] == text, heading
        assert pashr is not None and pashr.split(b",")[2] == text, heading


def test_pashr_with_heading_only_leaves_the_rest_empty() -> None:
    s = rover_state()
    s.attitude = Attitude(heading_deg=91.2)
    pashr = build_pashr(s)
    assert pashr is not None
    assert pashr.startswith(b"$PASHR,164734.12,91.20,T,,,0.00,,,,2,1*")


def test_build_sentences_respects_selection_and_slow_flag() -> None:
    s = rover_state()
    fast = build_sentences(s, {"GGA", "RMC", "GSA", "GSV"}, include_slow=False)
    assert [x[3:6] for x in fast] == [b"GGA", b"RMC"]
    slow = build_sentences(s, {"GGA", "GSA", "GSV", "ZDA"}, include_slow=True)
    assert [x[3:6] for x in slow] == [
        b"GGA",
        b"GSA",
        b"GSA",
        b"GSA",
        b"GSA",
        b"GSV",
        b"GSV",
        b"GSV",
        b"GSV",
        b"ZDA",
    ]


def test_every_sentence_round_trips() -> None:
    s = rover_state()
    s.attitude = Attitude(heading_deg=10.0, roll_deg=0.0, pitch_deg=0.0)
    out = build_sentences(s, set(ALL_SENTENCES), include_slow=True)
    assert {x[1:].split(b",")[0][-3:] for x in out} >= {b"GGA", b"RMC", b"HDT", b"SHR"}
    for raw in out:
        assert _checksum_ok(raw), raw
        if not raw.startswith(b"$PASHR"):
            NMEAReader.parse(raw)


def test_config_names_match_builders() -> None:
    assert set(NMEA_SENTENCE_NAMES) == set(ALL_SENTENCES)


class MemorySink:
    def __init__(self, fail_write: bool = False, fail_start: bool = False) -> None:
        self.data: list[bytes] = []
        self.fail_write, self.fail_start = fail_write, fail_start
        self.started = self.closed = 0

    async def start(self) -> None:
        self.started += 1
        if self.fail_start:
            raise OSError("no such device")

    async def write(self, data: bytes) -> None:
        if self.fail_write:
            raise OSError("unplugged")
        self.data.append(data)

    async def close(self) -> None:
        self.closed += 1


async def _wait_for(cond: Callable[[], bool]) -> None:
    deadline = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < deadline:
        if cond():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never became true")


async def test_publisher_fans_out_and_survives_a_broken_sink() -> None:
    bus = Bus()
    good, broken, absent = MemorySink(), MemorySink(fail_write=True), MemorySink(fail_start=True)
    pub = NmeaPublisher(bus, StateStore(), [good, broken, absent], ["gga", "zda"], 60.0)
    stop = asyncio.Event()
    task = asyncio.create_task(pub.run(stop))
    await asyncio.sleep(0)
    bus.publish("state.epoch", rover_state())
    bus.publish("state.epoch", rover_state())
    await _wait_for(lambda: len(good.data) == 2)
    assert good.data[0][3:6] == b"GGA" and b"ZDA" in good.data[0]  # first epoch carries slow
    assert b"ZDA" not in good.data[1]  # the slow interval has not elapsed again
    assert pub.sent == 2
    assert absent.started == 1 and absent.data == [] and absent.closed == 0
    assert broken.started == 1 and broken.data == []
    stop.set()
    await asyncio.wait_for(task, 1.0)  # a stop with no further epochs still ends run()
    assert good.closed == 1 and broken.closed == 1
    assert absent.closed == 0  # it never started, so there was nothing to close


async def test_publisher_retries_failed_sinks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nmea_out, "SINK_RETRY_S", 0.0)  # every epoch is past the retry time
    bus = Bus()
    good, broken, absent = MemorySink(), MemorySink(fail_write=True), MemorySink(fail_start=True)
    pub = NmeaPublisher(bus, StateStore(), [good, broken, absent], ["GGA"])
    stop = asyncio.Event()
    task = asyncio.create_task(pub.run(stop))
    try:
        await asyncio.sleep(0)
        bus.publish("state.epoch", rover_state())
        await _wait_for(lambda: len(good.data) == 1)
        assert broken.closed == 1 and broken.data == []
        assert absent.started == 2 and absent.data == []  # retried on the epoch, still absent
        broken.fail_write = absent.fail_start = False  # replugged / came up
        bus.publish("state.epoch", rover_state())
        await _wait_for(lambda: len(good.data) == 2)
        assert broken.started == 2 and absent.started == 3  # both were started again
        assert len(broken.data) == 1 and len(absent.data) == 1  # ... and got this epoch
        bus.publish("state.epoch", rover_state())
        await _wait_for(lambda: len(good.data) == 3)
        assert broken.started == 2 and absent.started == 3  # a working sink is not restarted
        assert good.started == 1
    finally:
        stop.set()
        await asyncio.wait_for(task, 1.0)


def _raw_pty() -> tuple[int, int]:
    master, slave = os.openpty()
    tty.setraw(slave)
    os.set_blocking(master, False)
    return master, slave


def _read_all(fd: int) -> bytes:
    try:
        return os.read(fd, 65536)
    except BlockingIOError:
        return b""


async def test_publisher_reopens_a_serial_port_that_was_unplugged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A USB-serial adapter (a pty slave behind a stable symlink, like a udev name) goes away
    and comes back: the publisher sets the sink aside and reopens it, with no daemon restart."""
    monkeypatch.setattr(nmea_out, "SINK_RETRY_S", 0.0)
    link = tmp_path / "ttyNMEA"
    master, slave = _raw_pty()
    link.symlink_to(os.ttyname(slave))
    sink = SerialSink(str(link), 115200)
    starts = 0
    real_start = sink.start

    async def counting_start() -> None:
        nonlocal starts
        starts += 1
        await real_start()

    monkeypatch.setattr(sink, "start", counting_start)
    bus = Bus()
    pub = NmeaPublisher(bus, StateStore(), [sink], ["GGA"])
    stop = asyncio.Event()
    task = asyncio.create_task(pub.run(stop))
    master2 = slave2 = None
    try:
        await asyncio.sleep(0)
        bus.publish("state.epoch", rover_state())
        got = bytearray()
        await _wait_for(lambda: bool(got.extend(_read_all(master)) or got.endswith(b"\r\n")))
        assert got.startswith(b"$GNGGA,")
        os.close(master)  # unplugged
        os.close(slave)
        bus.publish("state.epoch", rover_state())
        await _wait_for(lambda: pub.sent == 2)  # the write failed: the sink is set aside
        master2, slave2 = _raw_pty()  # plugged back in
        link.unlink()
        link.symlink_to(os.ttyname(slave2))
        bus.publish("state.epoch", rover_state())
        got2 = bytearray()
        await _wait_for(lambda: bool(got2.extend(_read_all(master2)) or got2.endswith(b"\r\n")))
        assert got2.startswith(b"$GNGGA,") and starts == 2
    finally:
        stop.set()
        await asyncio.wait_for(task, 1.0)
        for fd in (master2, slave2):
            if fd is not None:
                os.close(fd)


async def test_epoch_without_position_does_not_use_up_the_slow_slot() -> None:
    bus = Bus()
    sink = MemorySink()
    pub = NmeaPublisher(bus, StateStore(), [sink], ["GGA", "ZDA"], 60.0)
    stop = asyncio.Event()
    task = asyncio.create_task(pub.run(stop))
    await asyncio.sleep(0)
    bus.publish("state.epoch", ReceiverState())  # nothing to send yet
    bus.publish("state.epoch", rover_state())
    await _wait_for(lambda: len(sink.data) == 1)
    stop.set()
    await asyncio.wait_for(task, 1.0)
    assert b"ZDA" in sink.data[0]  # the first real epoch still carries the slow sentences


async def test_publisher_skips_epochs_without_position() -> None:
    bus = Bus()
    sink = MemorySink()
    pub = NmeaPublisher(bus, StateStore(), [sink], ["GGA", "RMC"])
    stop = asyncio.Event()
    task = asyncio.create_task(pub.run(stop))
    await asyncio.sleep(0)
    bus.publish("state.epoch", ReceiverState())
    bus.publish("state.epoch", rover_state())
    await _wait_for(lambda: len(sink.data) == 1)
    stop.set()
    await asyncio.wait_for(task, 1.0)
    assert pub.sent == 1


async def test_publisher_can_be_restarted() -> None:
    bus = Bus()
    sink = MemorySink()
    pub = NmeaPublisher(bus, StateStore(), [sink], ["GGA"])
    for _ in range(2):
        stop = asyncio.Event()
        task = asyncio.create_task(pub.run(stop))
        await asyncio.sleep(0)
        bus.publish("state.epoch", rover_state())
        before = len(sink.data)
        await _wait_for(lambda before=before: len(sink.data) == before + 1)
        stop.set()
        await asyncio.wait_for(task, 1.0)
    assert sink.started == 2 and sink.closed == 2


def test_epoch_json_fields() -> None:
    s = rover_state()
    s.velocity.vel_n_mps, s.velocity.vel_e_mps, s.velocity.vel_d_mps = 1.0, 1.1, -0.1
    s.rtk.baseline_m = 1234.5
    doc = epoch_json(s)
    assert set(doc) == {
        "t",
        "lat",
        "lon",
        "height_m",
        "hmsl_m",
        "h_acc_m",
        "v_acc_m",
        "fix_type",
        "carr_soln",
        "num_sv",
        "vel_n_mps",
        "vel_e_mps",
        "vel_d_mps",
        "heading_deg",
        "baseline_m",
        "corr_age_s",
        "attitude",
    }
    assert doc["t"] == "2026-09-18T16:47:34.120000+00:00" and doc["carr_soln"] == 2
    assert doc["baseline_m"] == 1234.5 and doc["attitude"] is None
    json.dumps(doc)


async def test_json_udp_publisher_sends_one_datagram_per_epoch() -> None:
    loop = asyncio.get_running_loop()
    received: list[bytes] = []

    class Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            received.append(data)

    transport, _ = await loop.create_datagram_endpoint(Proto, local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]
    bus = Bus()
    pub = JsonUdpPublisher(bus, [("127.0.0.1", port)])
    stop = asyncio.Event()
    task = asyncio.create_task(pub.run(stop))
    try:
        await _wait_for(lambda: pub.sink.ready)
        bad = rover_state()
        bad.position.lat = math.nan  # not JSON: the epoch is skipped, the feed goes on
        bus.publish("state.epoch", bad)
        bus.publish("state.epoch", rover_state())
        await _wait_for(lambda: len(received) == 1)
        assert json.loads(received[0])["lat"] == 23.83735067
        assert pub.sent == 1
    finally:
        stop.set()
        await asyncio.wait_for(task, 1.0)
        transport.close()


def test_heading_goes_out_before_the_ekf_has_a_position() -> None:
    """A dual-antenna Ellipse-D has a valid GNSS heading while its EKF is still unaligned (no
    position): HDT and PASHR go out on it (an autopilot's heading input), the position
    sentences wait for a position. Seen live: GNSS heading 270.94 in 'Vertical gyro' mode."""
    s = ReceiverState()
    s.time.utc = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)
    s.attitude = Attitude(heading_deg=270.94, acc_heading_deg=2.1, source="sbg-gnss-hdt")
    out = build_sentences(s, set(ALL_SENTENCES), include_slow=True)
    assert [x.split(b",")[0] for x in out] == [b"$GNHDT", b"$PASHR"]
    assert out[0] == b"$GNHDT,270.94,T*13\r\n"
    # no attitude either: still nothing at all
    assert build_sentences(ReceiverState(), set(ALL_SENTENCES), include_slow=True) == []
