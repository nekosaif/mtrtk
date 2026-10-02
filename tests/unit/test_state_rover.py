from datetime import UTC, datetime

from pyubx2 import GET, UBXMessage

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.core.statestore import StateStore, gps_to_utc


def frame(msg: UBXMessage):
    return Framer().feed(msg.serialize())[0]


def test_relposned_maps_units_and_flags() -> None:
    store = StateStore()
    msg = UBXMessage(
        "NAV",
        "NAV-RELPOSNED",
        GET,
        version=1,
        refStationID=7,
        iTOW=1000,
        relPosN=123456,
        relPosE=-2345,
        relPosD=678,
        relPosLength=123478,
        relPosHeading=91.12345,
        accN=12.0,
        accE=15.0,
        accD=30.0,
        accLength=14.0,
        accHeading=0.75,  # 0.5 would serialize as 49999e-5 (pyubx2 truncates)
        gnssFixOK=1,
        diffSoln=1,
        relPosValid=1,
        carrSoln=2,
        isMoving=0,
        refPosMiss=0,
        refObsMiss=0,
        relPosHeadingValid=1,
        relPosNormalized=0,
    )
    assert store.apply(frame(msg)) == {"rtk"}
    r = store.state.rtk
    assert r.rel_pos_n_m == 1234.56 and r.rel_pos_e_m == -23.45 and r.rel_pos_d_m == 6.78
    assert r.baseline_m == 1234.78 and r.heading_deg == 91.12345 and r.heading_valid is True
    assert r.acc_n_m == 0.012 and r.acc_length_m == 0.014 and r.acc_heading_deg == 0.75
    assert (
        r.ref_station_id == 7
        and r.carr_soln == 2
        and r.carr_soln_name == "RTK fixed"
        and r.rel_pos_valid is True
    )


def test_rxm_rtcm_counts_used_and_crc_failures() -> None:
    store = StateStore()
    store.apply(
        frame(
            UBXMessage(
                "RXM",
                "RXM-RTCM",
                GET,
                version=2,
                crcFailed=0,
                msgUsed=2,
                subType=0,
                refStation=7,
                msgType=1077,
            )
        )
    )
    store.apply(
        frame(
            UBXMessage(
                "RXM",
                "RXM-RTCM",
                GET,
                version=2,
                crcFailed=0,
                msgUsed=1,
                subType=0,
                refStation=7,
                msgType=1077,
            )
        )
    )
    store.apply(
        frame(
            UBXMessage(
                "RXM",
                "RXM-RTCM",
                GET,
                version=2,
                crcFailed=1,
                msgUsed=0,
                subType=0,
                refStation=7,
                msgType=1005,
            )
        )
    )
    r = store.state.rtk
    assert r.rtcm_rx[1077].count == 2 and r.rtcm_rx[1077].used == 1
    # A frame that failed its CRC has no trustworthy type: counted in the totals, but it opens
    # no per-type row (1005 was never received intact).
    assert 1005 not in r.rtcm_rx
    assert r.rtcm_rx_total == 3 and r.rtcm_crc_failed == 1
    assert r.ref_station_id == 7


def test_pvt_correction_age_code_and_injection_age() -> None:
    store = StateStore()
    store.apply(
        frame(
            UBXMessage(
                "NAV",
                "NAV-PVT",
                GET,
                iTOW=1,
                fixType=3,
                carrSoln=1,
                diffSoln=1,
                lastCorrectionAge=3,
            )
        )
    )
    assert (
        store.state.rtk.corr_age_receiver_s == 5
        and store.state.rtk.carr_soln_name == "RTK float"
        and store.state.rtk.diff_soln is True
    )
    store.note_rtcm_injected(now_mono=100.0)
    store.apply(frame(UBXMessage("NAV", "NAV-EOE", GET, iTOW=1)), now_mono=103.5)
    assert store.state.rtk.corr_age_s == 3.5


def test_gps_to_utc() -> None:
    # GPS week 2436, TOW 492472 s, 18 leap s -> 2026-09-18 16:47:34 UTC (the base fixture)
    assert gps_to_utc(2436, 492472.0, 18) == datetime(2026, 9, 18, 16, 47, 34, tzinfo=UTC)


def test_tim_tm2_appends_time_mark_and_publishes() -> None:
    bus = Bus()
    sub = bus.subscribe("state.time_mark")
    store = StateStore(bus)
    store.state.time.leap_s = 18
    msg = UBXMessage(
        "TIM",
        "TIM-TM2",
        GET,
        ch=0,
        mode=1,
        run=1,
        newFallingEdge=0,
        timeBase=1,
        utc=0,
        time=1,
        newRisingEdge=1,
        count=42,
        wnR=2436,
        wnF=2436,
        towMsR=492472123,
        towSubMsR=456000,
        towMsF=492472000,
        towSubMsF=0,
        accEst=25,
    )
    assert store.apply(frame(msg)) == {"time_marks"}
    tm = store.state.time_marks[-1]
    assert tm.count == 42 and tm.new_rising is True and tm.rising_week == 2436
    assert abs(tm.rising_tow_s - 492472.123456) < 1e-9
    assert tm.rising_utc == datetime(2026, 9, 18, 16, 47, 34, 123456, tzinfo=UTC)
    assert sub.queue.qsize() == 1
    for i in range(120):
        store.apply(
            frame(
                UBXMessage(
                    "TIM",
                    "TIM-TM2",
                    GET,
                    ch=0,
                    newRisingEdge=1,
                    count=43 + i,
                    wnR=2436,
                    towMsR=492473000 + i,
                )
            )
        )
    assert len(store.state.time_marks) == 100 and store.state.time_marks[-1].count == 162


def test_tm2_without_new_edge_does_not_append() -> None:
    store = StateStore()
    assert (
        store.apply(
            frame(
                UBXMessage("TIM", "TIM-TM2", GET, ch=0, newRisingEdge=0, newFallingEdge=0, count=1)
            )
        )
        == set()
    )
    assert store.state.time_marks == []


def test_tm2_utc_time_base_skips_leap_correction() -> None:
    store = StateStore()
    store.state.time.leap_s = 18
    msg = UBXMessage(
        "TIM",
        "TIM-TM2",
        GET,
        ch=1,
        timeBase=2,
        utc=1,
        time=1,
        newRisingEdge=1,
        count=1,
        wnR=2436,
        towMsR=492454000,
    )
    store.apply(frame(msg))
    tm = store.state.time_marks[-1]
    assert tm.time_base == 2 and tm.utc_based is True and tm.channel == 1
    assert tm.rising_utc == datetime(2026, 9, 18, 16, 47, 34, tzinfo=UTC)


def test_epoch_without_injection_leaves_corr_age_unset() -> None:
    bus = Bus()
    sub = bus.subscribe("state.rtk")
    store = StateStore(bus)
    store.apply(frame(UBXMessage("NAV", "NAV-EOE", GET, iTOW=1)), now_mono=5.0)
    assert store.state.rtk.corr_age_s is None and store.state.last_epoch_mono == 5.0
    assert sub.queue.qsize() == 0


# ------------------------------------------------- parked minors (2026-10-02): untrusted fields


def _rtcm(msg_type: int, station: int, crc_failed: int = 0, used: int = 2) -> object:
    return frame(
        UBXMessage(
            "RXM",
            "RXM-RTCM",
            GET,
            version=2,
            crcFailed=crc_failed,
            msgUsed=used,
            subType=0,
            refStation=station,
            msgType=msg_type,
        )
    )


def test_a_corrupted_rtcm_frame_changes_no_station_and_opens_no_type() -> None:
    store = StateStore()
    store.apply(_rtcm(1077, 7), now_mono=1.0)
    store.apply(_rtcm(1077, 3001, crc_failed=1, used=0), now_mono=2.0)  # garbage station id
    store.apply(_rtcm(3333, 7, crc_failed=1, used=0), now_mono=3.0)  # garbage type
    r = store.state.rtk
    assert r.ref_station_id == 7
    assert set(r.rtcm_rx) == {1077}
    assert r.rtcm_rx[1077].count == 2 and r.rtcm_rx[1077].crc_failed == 1  # a known type: counted
    assert r.rtcm_rx[1077].last_seen_mono == 2.0
    assert r.rtcm_rx_total == 3 and r.rtcm_crc_failed == 2


def _tm2(time_base: int, **kw: int) -> object:
    fields = dict(ch=0, newRisingEdge=1, count=1, wnR=2436, towMsR=492472000, time=1)
    fields.update(kw)
    return frame(UBXMessage("TIM", "TIM-TM2", GET, timeBase=time_base, **fields))


def test_a_mark_on_the_receiver_time_base_has_no_utc() -> None:
    """timeBase 0 is the receiver's own clock: its week/tow are not GPS time."""
    store = StateStore()
    store.state.time.leap_s = 18
    store.apply(_tm2(0))
    tm = store.state.time_marks[-1]
    assert tm.time_base == 0 and tm.rising_week == 2436 and tm.rising_utc is None
    store.apply(_tm2(1, count=2))  # the GNSS time base (the default GPS time grid) still is
    assert store.state.time_marks[-1].rising_utc == datetime(2026, 9, 18, 16, 47, 34, tzinfo=UTC)


def test_a_falling_edge_only_mark_is_kept_but_not_published() -> None:
    bus = Bus()
    sub = bus.subscribe("state.time_mark")
    store = StateStore(bus)
    store.apply(
        _tm2(1, newRisingEdge=0, newFallingEdge=1, wnF=2436, towMsF=492472500, towSubMsF=250000)
    )
    tm = store.state.time_marks[-1]
    assert tm.new_falling and not tm.new_rising
    assert tm.falling_week == 2436 and abs(tm.falling_tow_s - 492472.50025) < 1e-9
    assert tm.rising_week is None and tm.rising_tow_s is None and tm.rising_utc is None
    assert sub.queue.qsize() == 0


def test_a_mark_uses_the_receivers_leap_seconds() -> None:
    store = StateStore()
    store.state.time.leap_s = 17
    store.apply(_tm2(1))
    assert store.state.time_marks[-1].rising_utc == datetime(2026, 9, 18, 16, 47, 35, tzinfo=UTC)


def test_relposned_flags_map_one_to_one() -> None:
    """Distinct flag values and per-axis accuracies, so a swapped mapping fails."""
    store = StateStore()
    flags = dict(isMoving=1, refPosMiss=0, refObsMiss=1, relPosNormalized=1, diffSoln=0)
    msg = UBXMessage(
        "NAV",
        "NAV-RELPOSNED",
        GET,
        version=1,
        refStationID=3,
        accN=12.0,
        accE=15.0,
        accD=30.0,
        carrSoln=1,
        relPosValid=1,
        **flags,
    )
    store.apply(frame(msg))
    r = store.state.rtk
    assert (r.is_moving, r.ref_pos_missing, r.ref_obs_missing) == (True, False, True)
    assert r.normalized is True and r.diff_soln is False
    assert (r.acc_n_m, r.acc_e_m, r.acc_d_m) == (0.012, 0.015, 0.03)
    assert r.carr_soln_name == "RTK float"


def test_an_epoch_after_an_injection_publishes_the_correction_age() -> None:
    bus = Bus()
    sub = bus.subscribe("state.rtk")
    store = StateStore(bus)
    store.note_rtcm_injected(now_mono=100.0)
    store.apply(frame(UBXMessage("NAV", "NAV-EOE", GET, iTOW=1)), now_mono=103.5)
    assert sub.queue.qsize() == 1
    assert sub.queue.get_nowait()[1].corr_age_s == 3.5
    # An injection stamped after the epoch's clock reading (two clocks, one tick apart): 0, not
    # a negative age.
    store.note_rtcm_injected(now_mono=110.0)
    store.apply(frame(UBXMessage("NAV", "NAV-EOE", GET, iTOW=2)), now_mono=109.0)
    assert store.state.rtk.corr_age_s == 0.0
