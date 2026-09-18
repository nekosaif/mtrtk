from pyubx2 import GET, UBXMessage

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.core.statestore import StateStore
from ubxtest import mon_ver_bytes


def frame(msg: UBXMessage):
    return Framer().feed(msg.serialize())[0]


def test_mon_hw() -> None:
    store = StateStore()
    msg = UBXMessage(
        "MON",
        "MON-HW",
        GET,
        noisePerMS=90,
        agcCnt=3000,
        aStatus=2,
        aPower=1,
        jamInd=12,
        jammingState=1,
        rtcCalib=1,
        safeBoot=0,
        xtalAbsent=0,
    )
    assert store.apply(frame(msg)) == {"hardware"}
    hw = store.state.hardware
    assert hw is not None
    assert hw.ant_status_name == "OK" and hw.ant_power_name == "On"
    assert hw.noise_per_ms == 90 and hw.agc_cnt == 3000 and hw.jam_ind == 12
    assert hw.jamming_state_name == "OK" and hw.rtc_calib is True


def test_mon_rf_two_blocks() -> None:
    store = StateStore()
    msg = UBXMessage(
        "MON",
        "MON-RF",
        GET,
        version=0,
        nBlocks=2,
        blockId_01=0,
        jammingState_01=1,
        antStatus_01=2,
        antPower_01=1,
        postStatus_01=0,
        noisePerMS_01=80,
        agcCnt_01=4000,
        jamInd_01=10,
        ofsI_01=1,
        magI_01=100,
        ofsQ_01=-1,
        magQ_01=99,
        blockId_02=1,
        jammingState_02=2,
        antStatus_02=2,
        antPower_02=1,
        postStatus_02=0,
        noisePerMS_02=70,
        agcCnt_02=5000,
        jamInd_02=5,
    )
    assert store.apply(frame(msg)) == {"rf"}
    rf = store.state.rf
    assert [b.block_id for b in rf] == [0, 1]
    assert rf[0].jamming_state_name == "OK" and rf[1].jamming_state_name == "Warning"
    assert rf[0].mag_i == 100 and rf[1].agc_cnt == 5000


def test_mon_span() -> None:
    store = StateStore()
    msg = UBXMessage(
        "MON",
        "MON-SPAN",
        GET,
        version=0,
        numRfBlocks=1,
        span_01=100_000_000,
        res_01=390_625,
        center_01=1_580_000_000,
        pga_01=20,
    )
    assert store.apply(frame(msg)) == {"spectrum"}
    sp = store.state.spectrum[0]
    assert (
        sp.block_id == 0
        and sp.span_hz == 100_000_000
        and sp.center_hz == 1_580_000_000
        and sp.pga_db == 20
    )
    assert len(sp.bins) == 256


def test_mon_comms() -> None:
    store = StateStore()
    msg = UBXMessage(
        "MON",
        "MON-COMMS",
        GET,
        version=0,
        nPorts=1,
        portId_01=0x0300,
        txPending_01=10,
        txBytes_01=123456,
        txUsage_01=5,
        txPeakUsage_01=40,
        rxPending_01=0,
        rxBytes_01=999,
        rxUsage_01=1,
        rxPeakUsage_01=3,
        overrunErrs_01=2,
        skipped_01=7,
    )
    assert store.apply(frame(msg)) == {"ports"}
    p = store.state.ports[0]
    assert (
        p.port_id == 0x0300
        and p.tx_bytes == 123456
        and p.tx_peak_usage == 40
        and p.overrun_errs == 2
        and p.skipped == 7
    )


def test_mon_ver_extracts_firmware_fields() -> None:
    store = StateStore()
    assert store.apply(Framer().feed(mon_ver_bytes())[0]) == {"firmware"}
    fw = store.state.firmware
    assert fw.sw_version == "EXT CORE 1.00 (f10c36)" and fw.hw_version == "00190000"
    assert fw.fw_version == "HPG 1.13" and fw.protver == "27.12" and fw.module == "ZED-F9P"
    assert "GPS;GLO;GAL;BDS" in fw.extensions


def test_nav_svin_units() -> None:
    store = StateStore()
    msg = UBXMessage(
        "NAV",
        "NAV-SVIN",
        GET,
        iTOW=1,
        dur=120,
        meanX=123456789,
        meanY=-98765432,
        meanZ=55555555,
        meanXHP=12,
        meanYHP=-34,
        meanZHP=0,
        meanAcc=15000,
        obs=118,
        valid=0,
        active=1,
    )
    assert store.apply(frame(msg)) == {"survey_in"}
    sv = store.state.survey_in
    assert sv.active is True and sv.valid is False and sv.dur_s == 120 and sv.obs == 118
    assert abs(sv.mean_x_m - 1234567.8912) < 1e-9
    assert abs(sv.mean_y_m - (-987654.3234)) < 1e-9
    assert sv.mean_acc_m == 1.5


def test_nav_eoe_counts_epoch_and_publishes_snapshot() -> None:
    bus = Bus()
    sub = bus.subscribe("state.epoch")
    store = StateStore(bus)
    store.apply(frame(UBXMessage("NAV", "NAV-EOE", GET, iTOW=5000)))
    store.apply(frame(UBXMessage("NAV", "NAV-EOE", GET, iTOW=6000)))
    assert store.state.epoch_count == 2 and store.state.last_epoch_mono is not None
    assert sub.queue.qsize() == 2
    topic, snapshot = sub.queue.get_nowait()
    assert topic == "state.epoch" and snapshot is store.state


def test_rtcm_counters() -> None:
    from ubxtest import rtcm_frame

    store = StateStore()
    framer = Framer()
    for raw in (
        rtcm_frame(1005, b"\x00" * 16),
        rtcm_frame(1077, b"\x00" * 200),
        rtcm_frame(1077, b"\x00" * 200),
    ):
        for f in framer.feed(raw):
            assert store.apply(f) == {"rtcm_out"}
    st = store.state.rtcm_out
    assert st.messages[1077].count == 2 and st.messages[1005].count == 1
    assert st.total_count == 3 and st.total_bytes == 22 + 2 * 206
    assert st.bytes_per_s > 0
