from pyubx2 import GET, UBXMessage

from mtrtk.core.frames import Framer
from mtrtk.core.statestore import StateStore


def frame(msg: UBXMessage):
    return Framer().feed(msg.serialize())[0]


def nav_sat(itow: int) -> UBXMessage:
    return UBXMessage(
        "NAV",
        "NAV-SAT",
        GET,
        iTOW=itow,
        version=1,
        numSvs=3,
        gnssId_01=0,
        svId_01=5,
        cno_01=40,
        elev_01=45,
        azim_01=120,
        prRes_01=0.5,
        qualityInd_01=7,
        svUsed_01=1,
        health_01=1,
        ephAvail_01=1,
        almAvail_01=1,
        gnssId_02=6,
        svId_02=3,
        cno_02=30,
        elev_02=20,
        azim_02=300,
        prRes_02=-1.2,
        qualityInd_02=4,
        svUsed_02=0,
        health_02=1,
        gnssId_03=0,
        svId_03=12,
        cno_03=0,
        elev_03=-91,
        azim_03=0,
        qualityInd_03=1,
        svUsed_03=0,
        health_03=0,
    )


def nav_sig(itow: int) -> UBXMessage:
    return UBXMessage(
        "NAV",
        "NAV-SIG",
        GET,
        iTOW=itow,
        version=0,
        numSigs=3,
        gnssId_01=0,
        svId_01=5,
        sigId_01=0,
        freqId_01=0,
        prRes_01=0.4,
        cno_01=40,
        qualityInd_01=7,
        corrSource_01=0,
        ionoModel_01=0,
        health_01=1,
        prUsed_01=1,
        crUsed_01=1,
        doUsed_01=1,
        gnssId_02=0,
        svId_02=5,
        sigId_02=3,
        freqId_02=0,
        prRes_02=0.2,
        cno_02=36,
        qualityInd_02=7,
        health_02=1,
        prUsed_02=1,
        crUsed_02=1,
        doUsed_02=0,
        gnssId_03=6,
        svId_03=3,
        sigId_03=0,
        freqId_03=5,
        cno_03=30,
        qualityInd_03=4,
        health_03=1,
    )


def test_nav_sat_builds_satellite_list_and_summary() -> None:
    store = StateStore()
    assert store.apply(frame(nav_sat(1000))) == {"sats", "sat_summary"}
    sats = store.state.sats
    assert [(s.gnss, s.sv_id) for s in sats] == [("GPS", 5), ("GPS", 12), ("GLONASS", 3)]
    g5 = sats[0]
    assert (
        g5.cno == 40 and g5.elev == 45 and g5.azim == 120 and g5.used is True and g5.pr_res_m == 0.5
    )
    assert g5.eph_avail is True and g5.alm_avail is True and g5.quality_ind == 7
    assert sats[1].elev is None  # -91 = unknown elevation
    summary = store.state.sat_summary
    assert summary.tracked == 3 and summary.used == 1
    assert summary.per_gnss == {
        "GPS": {"tracked": 2, "used": 1},
        "GLONASS": {"tracked": 1, "used": 0},
    }


def test_nav_sig_merges_signals_into_satellites() -> None:
    store = StateStore()
    store.apply(frame(nav_sat(1000)))
    assert store.apply(frame(nav_sig(1000))) == {"sats", "sat_summary"}
    g5 = next(s for s in store.state.sats if s.key == (0, 5))
    assert [sig.name for sig in g5.signals] == ["L1C/A", "L2CL"]
    assert (
        g5.signals[1].cno == 36 and g5.signals[1].pr_used is True and g5.signals[1].do_used is False
    )
    r3 = next(s for s in store.state.sats if s.key == (6, 3))
    assert r3.signals[0].name == "L1OF" and r3.signals[0].freq_id == 5
    assert store.state.sat_summary.tracked == 3  # SIG did not add satellites


def test_nav_sig_before_nav_sat_still_merges() -> None:
    store = StateStore()
    store.apply(frame(nav_sig(1000)))
    assert len(store.state.sats) == 2  # created from SIG with unknown elevation
    store.apply(frame(nav_sat(1000)))
    g5 = next(s for s in store.state.sats if s.key == (0, 5))
    assert g5.elev == 45 and len(g5.signals) == 2


def test_new_itow_resets_satellite_set() -> None:
    store = StateStore()
    store.apply(frame(nav_sat(1000)))
    store.apply(frame(nav_sig(1000)))
    store.apply(frame(nav_sat(2000)))
    assert all(s.signals == [] for s in store.state.sats)
    store.apply(frame(nav_sig(2000)))
    assert any(s.signals for s in store.state.sats)
