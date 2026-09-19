from pathlib import Path

from mtrtk.core.frames import Framer
from mtrtk.core.statestore import StateStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE = FIXTURES / "f9p_hpg113_raw_10s.ubx"
FIXTURE_60S = FIXTURES / "f9p_hpg113_raw_60s.ubx"
BASE_FIXTURE = FIXTURES / "f9p_hpg113_base_30s.ubx"


def test_fixture_drives_state_to_a_3d_fix_with_satellites() -> None:
    store = StateStore()
    for frame in Framer().feed(FIXTURE.read_bytes()):
        store.apply(frame)
    s = store.state
    assert s.fix.fix_type == 3
    assert s.position.lat is not None and 23.0 < s.position.lat < 24.5  # recorded in Dhaka
    assert s.position.lon is not None and 90.0 < s.position.lon < 91.0
    assert s.time.utc is not None and s.time.utc.year == 2026
    assert s.sat_summary.tracked > 20 and s.sat_summary.used > 10
    assert any(sat.signals for sat in s.sats)
    assert s.raw_epochs >= 8


def test_60s_fixture_replays_every_epoch_and_leaves_unseen_sections_at_defaults() -> None:
    store = StateStore()
    for frame in Framer().feed(FIXTURE_60S.read_bytes()):
        store.apply(frame)
    s = store.state
    assert s.raw_epochs == 60  # one RXM-RAWX per second, none of them parsed
    assert s.fix.fix_type == 3 and s.fix.fix_type_name == "3D"
    assert s.sat_summary.tracked == 54 and s.sat_summary.used > 20
    assert set(s.sat_summary.per_gnss) == {"GPS", "Galileo", "BeiDou", "QZSS", "GLONASS"}
    # the capture predates mtrtk configuring the receiver: no NAV-EOE, MON-* or NAV-SVIN in it
    assert s.epoch_count == 0 and s.last_epoch_mono is None
    assert s.hardware is None and s.rf == [] and s.spectrum == [] and s.ports == []
    assert s.firmware.fw_version == "" and s.survey_in.active is False


def test_base_fixture_populates_every_monitored_section() -> None:
    """A real 30 s base capture must fill the MON-*, NAV-SVIN, NAV-EOE and RTCM sections."""
    store = StateStore()
    for frame in Framer().feed(BASE_FIXTURE.read_bytes()):
        store.apply(frame)
    s = store.state

    assert s.hardware is not None
    assert s.hardware.jamming_state_name == "OK" and s.hardware.rtc_calib is True
    assert 0 < s.hardware.noise_per_ms < 1000 and s.hardware.agc_cnt > 0

    assert len(s.rf) == 2  # the F9P's two front ends
    assert all(b.jamming_state_name == "OK" and b.agc_cnt > 0 for b in s.rf)
    assert {b.noise_per_ms for b in s.rf} == {93, 48}  # read per block, not copied

    assert [len(x.bins) for x in s.spectrum] == [256, 256]
    assert all(x.span_hz == 128_000_000 and x.res_hz == 500_000 for x in s.spectrum)
    assert all(0 <= level <= 255 for x in s.spectrum for level in x.bins)
    assert s.spectrum[0].center_hz > s.spectrum[1].center_hz  # L1 above L2
    assert [x.block_id for x in s.spectrum] == [0, 1]

    assert len(s.ports) == 4
    assert {p.port_id for p in s.ports} == {0x100, 0x200, 0x300, 0x101}
    assert all(p.tx_bytes > 0 for p in s.ports) and all(p.overrun_errs == 0 for p in s.ports)

    # TMODE was off for this capture: no survey and therefore no mean position
    assert s.survey_in.active is False and s.survey_in.valid is False
    assert s.survey_in.mean_acc_m is None and s.survey_in.mean_x_m is None

    assert s.epoch_count == 30 and s.last_epoch_mono is not None  # one NAV-EOE per second

    assert set(s.rtcm_out.messages) == {1077, 1087, 1097, 1127, 1230}  # MSM7 + GLONASS biases
    assert all(m.count > 0 and m.bytes > 0 for m in s.rtcm_out.messages.values())
    assert s.rtcm_out.messages[1077].count == 30 and s.rtcm_out.messages[1230].count == 6
    assert s.rtcm_out.total_count == 126 and s.rtcm_out.bytes_per_s > 0
