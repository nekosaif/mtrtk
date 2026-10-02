"""`state.epoch` fires once per navigation epoch, whether or not the stream carries NAV-EOE.

The receiver profile turns NAV-EOE on, so live units and the base fixture close every epoch with
it. Older recordings (the raw 10 s / 60 s fixtures) and hourly logs written before NAV-EOE was in
the default `LOG_MESSAGES` have none: there the store infers the epoch end from the stream.
"""

from pathlib import Path
from typing import Any

import pytest
from pyubx2 import GET, UBXMessage

from mtrtk.config import DEFAULT_LOG_MESSAGES, Settings
from mtrtk.core.bus import Bus, Policy
from mtrtk.core.frames import Frame, Framer
from mtrtk.core.state import ReceiverState
from mtrtk.core.statestore import StateStore
from mtrtk.daemon import Daemon
from mtrtk.rawlog.writer import RawLogWriter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RAW_10S = FIXTURES / "f9p_hpg113_raw_10s.ubx"
RAW_60S = FIXTURES / "f9p_hpg113_raw_60s.ubx"
BASE_30S = FIXTURES / "f9p_hpg113_base_30s.ubx"
NAV_EOE = b"\xb5\x62\x01\x61"
# The default list before NAV-EOE joined it: what every existing hourly log was written with.
OLD_LOG_MESSAGES = [m for m in DEFAULT_LOG_MESSAGES if m != "NAV-EOE"]


def ubx(identity: str, **fields: Any) -> bytes:
    return UBXMessage("NAV", identity, GET, **fields).serialize()


def replay(data: bytes, *, end: bool = True) -> tuple[StateStore, list[ReceiverState]]:
    """Feed *data* through a StateStore; return it and every `state.epoch` it published."""
    bus = Bus()
    sub = bus.subscribe("state.epoch", policy=Policy.UNBOUNDED)
    store = StateStore(bus)
    for frame in Framer().feed(data):
        store.apply(frame)
    if end:
        store.end_of_stream()
    epochs: list[ReceiverState] = []
    while not sub.queue.empty():
        _, item = sub.queue.get_nowait()
        epochs.append(item)
    return store, epochs


# ------------------------------------------------------------------ fixtures
def test_raw_10s_fixture_yields_one_epoch_per_second() -> None:
    store, epochs = replay(RAW_10S.read_bytes())
    assert NAV_EOE not in RAW_10S.read_bytes()  # the premise: nothing closes these epochs
    assert store.state.epoch_count == 10 and len(epochs) == 10
    assert [e.time.itow_ms for e in epochs] == [505_392_000 + 1000 * i for i in range(10)]
    assert store.state.last_epoch_mono is not None
    # Closed once the whole epoch is in: each copy carries that epoch's satellites and fix.
    assert all(e.fix.fix_type == 3 and e.sat_summary.tracked > 20 for e in epochs)
    assert all(any(sat.signals for sat in e.sats) for e in epochs)


def test_the_last_epoch_of_a_file_waits_for_the_end_of_the_stream() -> None:
    store, epochs = replay(RAW_10S.read_bytes(), end=False)
    assert store.state.epoch_count == 9 and len(epochs) == 9  # nothing has followed epoch 10
    store.end_of_stream()
    assert store.state.epoch_count == 10
    store.end_of_stream()  # idempotent: the epoch is closed once
    assert store.state.epoch_count == 10


def test_raw_60s_fixture_yields_sixty_epochs() -> None:
    store, epochs = replay(RAW_60S.read_bytes())
    assert store.state.epoch_count == 60 and len(epochs) == 60
    assert len({e.time.itow_ms for e in epochs}) == 60


def test_base_fixture_with_nav_eoe_never_double_fires() -> None:
    store, epochs = replay(BASE_30S.read_bytes())
    assert store.state.epoch_count == 30 and len(epochs) == 30  # one per NAV-EOE, no more
    assert len({e.time.itow_ms for e in epochs}) == 30


# ----------------------------------------------------------------- synthetic
def test_an_inferred_epoch_is_closed_before_the_next_one_is_applied() -> None:
    data = (
        ubx("NAV-PVT", iTOW=1000, lat=23.1, fixType=3)
        + ubx("NAV-HPPOSLLH", iTOW=1000, lat=23.2)
        + ubx("NAV-PVT", iTOW=2000, lat=23.3, fixType=3)
    )
    store, epochs = replay(data, end=False)
    assert len(epochs) == 1
    assert epochs[0].time.itow_ms == 1000
    assert abs(epochs[0].position.lat - 23.2) < 1e-6  # HPPOSLLH of 1000, not PVT of 2000
    assert store.state.position.lat is not None and abs(store.state.position.lat - 23.3) < 1e-6


def test_a_partial_first_epoch_is_not_an_epoch() -> None:
    """Joining mid-epoch (a live connect) must not invent an epoch from its tail."""
    data = (
        ubx("NAV-HPPOSLLH", iTOW=1000, lat=23.2)  # the tail of an epoch whose PVT we missed
        + ubx("NAV-PVT", iTOW=2000, lat=23.3, fixType=3)
        + ubx("NAV-HPPOSLLH", iTOW=2000, lat=23.3)
        + ubx("NAV-PVT", iTOW=3000, lat=23.4, fixType=3)
    )
    _, epochs = replay(data, end=False)
    assert [e.time.itow_ms for e in epochs] == [2000]


def test_nav_eoe_turns_inference_off_for_good() -> None:
    """A stream that starts carrying NAV-EOE (the profile applied mid-run) fires each once."""
    data = (
        ubx("NAV-PVT", iTOW=1000, fixType=3)
        + ubx("NAV-PVT", iTOW=2000, fixType=3)  # closes 1000 by inference
        + ubx("NAV-EOE", iTOW=2000)  # closes 2000; the stream has NAV-EOE from here on
        + ubx("NAV-PVT", iTOW=3000, fixType=3)
        + ubx("NAV-PVT", iTOW=4000, fixType=3)  # no NAV-EOE for 3000: it was lost, not inferred
        + ubx("NAV-EOE", iTOW=4000)
    )
    store, epochs = replay(data)
    assert [e.time.itow_ms for e in epochs] == [1000, 2000, 4000]
    assert store.state.epoch_count == 3


def test_nav_eoe_for_the_pending_epoch_fires_it_once() -> None:
    data = ubx("NAV-PVT", iTOW=1000, fixType=3) + ubx("NAV-EOE", iTOW=1000)
    store, epochs = replay(data)  # end_of_stream must not close 1000 a second time
    assert [e.time.itow_ms for e in epochs] == [1000]


def test_a_looping_replay_closes_the_last_epoch_when_itow_jumps_back() -> None:
    one = ubx("NAV-PVT", iTOW=1000, fixType=3) + ubx("NAV-PVT", iTOW=2000, fixType=3)
    _, epochs = replay(one + one, end=False)
    assert [e.time.itow_ms for e in epochs] == [1000, 2000, 1000]


# ------------------------------------------------------------------ raw logs
def _write_log(tmp_path: Path, messages: list[str], data: bytes) -> bytes:
    writer = RawLogWriter(Bus(), tmp_path, "MTRK", messages, role="base")
    frames: list[Frame] = Framer().feed(data)
    for frame in frames:
        writer.handle(frame)
    path = writer.current_path
    writer.close()
    assert path is not None
    return path.read_bytes()


def test_the_default_log_messages_keep_nav_eoe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    monkeypatch.delenv("LOG_MESSAGES", raising=False)
    assert "NAV-EOE" in DEFAULT_LOG_MESSAGES
    assert "NAV-EOE" in Settings(_env_file=None).log_messages


def test_a_raw_log_written_with_the_defaults_contains_nav_eoe(tmp_path: Path) -> None:
    log = _write_log(tmp_path, list(DEFAULT_LOG_MESSAGES), BASE_30S.read_bytes())
    assert log.count(NAV_EOE) == 30
    store, epochs = replay(log)
    assert store.state.epoch_count == 30 and len(epochs) == 30


def test_a_raw_log_written_before_nav_eoe_was_logged_still_replays_epochs(
    tmp_path: Path,
) -> None:
    log = _write_log(tmp_path, OLD_LOG_MESSAGES, BASE_30S.read_bytes())
    assert NAV_EOE not in log
    store, epochs = replay(log)
    assert store.state.epoch_count == 30 and len(epochs) == 30
    assert all(e.position.lat is not None for e in epochs)


# -------------------------------------------------------------------- daemon
async def test_replaying_a_raw_fixture_through_the_daemon_fills_every_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug as the user saw it: `mtrtk replay` of a raw capture never produced an epoch."""
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    settings = Settings(_env_file=None, mtrtk_source=f"file:{RAW_10S}", replay_speed=0)
    daemon = Daemon(settings)
    await daemon.run()
    assert daemon.store.state.epoch_count == 10  # the last one closed at end of file
