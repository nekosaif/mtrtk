from pathlib import Path

from mtrtk.core.frames import Framer
from mtrtk.core.statestore import StateStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE = FIXTURES / "f9p_hpg113_raw_10s.ubx"
FIXTURE_60S = FIXTURES / "f9p_hpg113_raw_60s.ubx"


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
