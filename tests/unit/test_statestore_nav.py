import logging
import time
from datetime import UTC, datetime

import pytest
from pyubx2 import GET, UBXMessage

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame, Framer, Proto
from mtrtk.core.statestore import RTCM_RATE_WINDOW_S, StateStore
from ubxtest import nmea_frame, rtcm_frame


def frame(msg: UBXMessage):
    return Framer().feed(msg.serialize())[0]


def test_nav_pvt_populates_position_fix_time_velocity() -> None:
    store = StateStore()
    msg = UBXMessage(
        "NAV",
        "NAV-PVT",
        GET,
        iTOW=492472000,
        year=2026,
        month=9,
        day=18,
        hour=16,
        min=47,
        second=34,
        nano=-250_000_000,
        validDate=1,
        validTime=1,
        fullyResolved=1,
        fixType=3,
        gnssFixOk=1,
        diffSoln=0,
        carrSoln=2,
        numSV=30,
        lon=90.2625502,
        lat=23.8373506,
        height=-36268,
        hMSL=13363,
        hAcc=1071,
        vAcc=1219,
        velN=100,
        velE=-200,
        velD=50,
        gSpeed=224,
        headMot=123.45,
        sAcc=90,
        headAcc=1.5,
        pDOP=1.09,
    )
    changed = store.apply(frame(msg))
    s = store.state
    assert changed == {"position", "accuracy", "dops", "fix", "velocity", "time"}
    assert s.position.lat == 23.8373506 and s.position.lon == 90.2625502
    assert s.position.height_m == -36.268 and s.position.hmsl_m == 13.363
    assert s.accuracy.h_acc_m == 1.071 and s.accuracy.v_acc_m == 1.219
    assert s.accuracy.s_acc_mps == 0.09 and s.accuracy.head_acc_deg == 1.5
    assert s.dops.p == 1.09
    assert s.fix.fix_type == 3 and s.fix.fix_type_name == "3D"
    assert s.fix.carr_soln == 2 and s.fix.carr_soln_name == "RTK fixed"
    assert s.fix.num_sv == 30 and s.fix.gnss_fix_ok is True
    assert (
        s.velocity.vel_n_mps == 0.1
        and s.velocity.vel_e_mps == -0.2
        and s.velocity.ground_speed_mps == 0.224
    )
    assert s.velocity.heading_motion_deg == 123.45
    assert s.time.utc == datetime(2026, 9, 18, 16, 47, 33, 750000, tzinfo=UTC)
    assert s.time.itow_ms == 492472000 and s.time.fully_resolved is True


def test_nav_pvt_without_valid_time_leaves_utc_none() -> None:
    store = StateStore()
    store.apply(
        frame(UBXMessage("NAV", "NAV-PVT", GET, iTOW=1, validDate=0, validTime=0, fixType=0))
    )
    assert store.state.time.utc is None
    assert store.state.fix.fix_type_name == "No fix"


def test_hpposllh_overrides_with_high_precision() -> None:
    store = StateStore()
    # pyubx2 only accepts the base and HP components separately when building a message;
    # it merges them back into lat/lon/height/hMSL on parse.
    msg = UBXMessage(
        "NAV",
        "NAV-HPPOSLLH",
        GET,
        iTOW=1,
        lon=90.2625502,
        lat=23.8373506,
        _HPlon=1e-8,
        _HPlat=7e-8,
        height=-36268,
        _HPheight=-0.4,
        hMSL=13363,
        _HPhMSL=0.1,
        hAcc=12.3,
        vAcc=45.6,
    )
    assert store.apply(frame(msg)) == {"position", "accuracy"}
    assert abs(store.state.position.lat - 23.83735067) < 1e-7  # pyubx2 merges the 1e-9 HP part
    assert abs(store.state.position.height_m - (-36.2684)) < 1e-3
    assert abs(store.state.accuracy.h_acc_m - 0.0123) < 1e-6


def test_hpposllh_invalid_flag_is_ignored() -> None:
    store = StateStore()
    store.apply(
        frame(UBXMessage("NAV", "NAV-HPPOSLLH", GET, iTOW=1, invalidLlh=1, lat=1.0, lon=2.0))
    )
    assert store.state.position.lat is None


def test_hpposllh_invalid_flag_syncs_invalid_llh() -> None:
    store = StateStore()
    store.apply(frame(UBXMessage("NAV", "NAV-HPPOSLLH", GET, iTOW=1, lat=1.5, lon=2.5)))
    assert store.state.position.invalid_llh is False
    invalid = UBXMessage("NAV", "NAV-HPPOSLLH", GET, iTOW=2, invalidLlh=1, lat=9.0, lon=9.0)
    assert store.apply(frame(invalid)) == {"position"}
    assert store.state.position.invalid_llh is True
    assert store.state.position.lat == 1.5  # the invalid solution must not overwrite the last good


def test_rtcm_rate_window_uses_frame_capture_time() -> None:
    store = StateStore()
    raw = rtcm_frame(1077, b"\x00" * 200)
    now = time.monotonic()

    def rtcm_at(t_mono: float) -> Frame:
        return Frame(proto=Proto.RTCM3, raw=raw, t_mono=t_mono, t_host=0.0)

    store.apply(rtcm_at(now - 600))
    assert store.state.rtcm_out.messages[1077].last_seen_mono == now - 600
    store.apply(rtcm_at(now))
    # the 600 s old frame is outside the window, so only the fresh one counts towards the rate
    assert store.state.rtcm_out.bytes_per_s == len(raw) / RTCM_RATE_WINDOW_S
    assert store.state.rtcm_out.total_count == 2


def test_hpposecef_sets_ecef_metres_and_pacc() -> None:
    store = StateStore()
    msg = UBXMessage(
        "NAV",
        "NAV-HPPOSECEF",
        GET,
        iTOW=1,
        ecefX=123456789,
        ecefY=-98765432,
        ecefZ=55555555,
        pAcc=250.0,
    )
    assert store.apply(frame(msg)) == {"position", "accuracy"}
    assert store.state.position.ecef_x_m == 1234567.89
    assert store.state.position.ecef_y_m == -987654.32
    assert store.state.accuracy.p_acc_m == 0.25


def test_nav_dop() -> None:
    store = StateStore()
    msg = UBXMessage(
        "NAV",
        "NAV-DOP",
        GET,
        iTOW=1,
        gDOP=1.5,
        pDOP=1.2,
        tDOP=0.8,
        vDOP=1.0,
        hDOP=0.7,
        nDOP=0.5,
        eDOP=0.4,
    )
    assert store.apply(frame(msg)) == {"dops"}
    assert store.state.dops.model_dump() == {
        "g": 1.5,
        "p": 1.2,
        "t": 0.8,
        "v": 1.0,
        "h": 0.7,
        "n": 0.5,
        "e": 0.4,
    }


def test_nav_status_clock_timegps_timels_timeutc() -> None:
    store = StateStore()
    store.apply(
        frame(
            UBXMessage(
                "NAV", "NAV-STATUS", GET, iTOW=1, gpsFix=3, ttff=2500, msss=123456, spoofDetState=1
            )
        )
    )
    store.apply(
        frame(UBXMessage("NAV", "NAV-CLOCK", GET, iTOW=1, clkB=1500, clkD=-7, tAcc=20, fAcc=300))
    )
    store.apply(
        frame(
            UBXMessage(
                "NAV",
                "NAV-TIMEGPS",
                GET,
                iTOW=492472000,
                fTOW=-123456,
                week=2436,
                leapS=18,
                towValid=1,
                weekValid=1,
                leapSValid=1,
                tAcc=25,
            )
        )
    )
    store.apply(
        frame(
            UBXMessage(
                "NAV",
                "NAV-TIMELS",
                GET,
                iTOW=1,
                srcOfCurrLs=2,
                currLs=18,
                srcOfLsChange=2,
                lsChange=0,
                timeToLsEvent=100000,
                validCurrLs=1,
                validTimeToLsEvent=1,
            )
        )
    )
    store.apply(frame(UBXMessage("NAV", "NAV-TIMEUTC", GET, iTOW=1, validUTC=1, utcStandard=3)))
    t = store.state.time
    assert (
        store.state.fix.ttff_ms == 2500
        and store.state.fix.uptime_ms == 123456
        and store.state.fix.spoof_det_state == 1
    )
    assert (
        t.clk_bias_ns == 1500
        and t.clk_drift_nsps == -7
        and t.t_acc_ns == 25
        and t.f_acc_psps == 300
    )
    assert t.gps_week == 2436 and abs(t.gps_tow_s - 492471.999876544) < 1e-6 and t.leap_s == 18
    assert t.leap_source == 2 and t.time_to_leap_event_s == 100000 and t.leap_change == 0
    assert t.valid_utc is True and t.utc_standard == 3


def test_unknown_or_rawx_frames_change_nothing_but_count_epochs() -> None:
    store = StateStore()
    rawx = UBXMessage("RXM", "RXM-RAWX", GET, rcvTow=1.0, week=2436, leapS=18, numMeas=0)
    assert store.apply(frame(rawx)) == set()
    assert store.state.raw_epochs == 1
    assert store.apply(frame(UBXMessage("NAV", "NAV-VELECEF", GET, iTOW=1))) == set()


def test_nmea_frames_are_ignored() -> None:
    store = StateStore()
    raw = nmea_frame("GNGGA,102030.00,2350.24104,N,09015.75301,E,4,30,0.70,13.4,M,-49.6,M,1.0,0000")
    assert store.apply(Framer().feed(raw)[0]) == set()
    assert store.state.position.lat is None


def test_handler_exception_is_logged_and_does_not_propagate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = StateStore()

    def boom(_m: object) -> set[str]:
        raise ValueError("malformed")

    store._handlers["NAV-DOP"] = boom
    with caplog.at_level(logging.ERROR, logger="mtrtk.core.statestore"):
        assert store.apply(frame(UBXMessage("NAV", "NAV-DOP", GET, iTOW=1, pDOP=2.0))) == set()
    assert "failed to apply NAV-DOP" in caplog.text
    assert store.state.dops.p is None


def test_sections_are_published_on_bus() -> None:
    bus = Bus()
    sub = bus.subscribe("state.*")
    store = StateStore(bus)
    store.apply(frame(UBXMessage("NAV", "NAV-DOP", GET, iTOW=1, pDOP=2.0)))
    topic, item = sub.queue.get_nowait()
    assert topic == "state.dops" and item.p == 2.0
