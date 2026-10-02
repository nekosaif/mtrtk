"""`state.epoch` fires once per navigation epoch, whether or not the stream carries NAV-EOE.

The receiver profile turns NAV-EOE on, so live units and the base fixture close every epoch with
it. Older recordings (the raw 10 s / 60 s fixtures) and hourly logs written before NAV-EOE was in
the default `LOG_MESSAGES` have none: there the store infers the epoch end from the stream.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from pyubx2 import GET, UBXMessage

from mtrtk.config import DEFAULT_LOG_MESSAGES, Settings
from mtrtk.core.bus import Bus, Policy
from mtrtk.core.frames import Frame, Framer
from mtrtk.core.router import TOPIC_RAW_UBX
from mtrtk.core.state import ReceiverState
from mtrtk.core.statestore import EPOCH_NAV_MESSAGES, StateStore
from mtrtk.daemon import Daemon
from mtrtk.rawlog.writer import RawLogWriter
from mtrtk.store.db import Database
from ubxtest import ubx_frame

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
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
    framer = Framer()
    for at in range(0, len(data), 1 << 16):  # in reads, as a source delivers it (and under
        for frame in framer.feed(data[at : at + (1 << 16)]):  # the framer's buffer cap)
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


def test_nav_eoe_turns_inference_off() -> None:
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


def test_a_stream_that_stops_carrying_nav_eoe_goes_back_to_inference() -> None:
    """A new log, then an older one (or LOG_MESSAGES changed mid-file): once a second epoch in
    a row ends without its NAV-EOE, the stream has stopped carrying it. Inference takes over,
    closing the epoch just finished; only the first epoch of the stretch is lost."""
    data = (
        ubx("NAV-PVT", iTOW=1000, fixType=3)
        + ubx("NAV-EOE", iTOW=1000)
        + ubx("NAV-PVT", iTOW=2000, fixType=3)
        + ubx("NAV-EOE", iTOW=2000)
        + ubx("NAV-PVT", iTOW=3000, fixType=3)  # no NAV-EOE from here on
        + ubx("NAV-PVT", iTOW=4000, fixType=3)  # one missed: it may only have been lost
        + ubx("NAV-PVT", iTOW=5000, fixType=3)  # two in a row: inference again, 4000 closed
        + ubx("NAV-PVT", iTOW=6000, fixType=3)
    )
    store, epochs = replay(data)
    assert [e.time.itow_ms for e in epochs] == [1000, 2000, 4000, 5000, 6000]
    assert store.state.epoch_count == 5


def test_a_nav_eoe_log_followed_by_an_older_one_replays_both() -> None:
    """base_30s (NAV-EOE) then raw_60s (none), files out of date order: the 60 raw seconds
    used to give no epoch at all, so no History, points or NMEA."""
    store, epochs = replay(BASE_30S.read_bytes() + RAW_60S.read_bytes())
    assert store.state.epoch_count == 30 + 59 and len(epochs) == 89  # raw's first is lost
    assert len({e.time.itow_ms for e in epochs}) == 89


def test_a_looped_mixed_replay_publishes_every_pass() -> None:
    """REPLAY_LOOP over raw_60s + base_30s: each later pass rewinds into the raw part after a
    NAV-EOE stretch. Its epochs used to be lost from the second pass on (a 60 s gap a pass);
    now only the first raw epoch of each later pass is, as for any join after NAV-EOE."""
    one_pass = RAW_60S.read_bytes() + BASE_30S.read_bytes()
    store, epochs = replay(one_pass * 3)
    assert store.state.epoch_count == (60 + 30) + 2 * (59 + 30) == len(epochs)
    raw_itows = {e.time.itow_ms for e in replay(RAW_60S.read_bytes())[1]}
    raw_per_pass = sum(1 for e in epochs if e.time.itow_ms in raw_itows)
    assert raw_per_pass == 60 + 2 * 59


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
    await asyncio.wait_for(daemon.run(), 30.0)
    assert daemon.store.state.epoch_count == 10  # the last one closed at end of file


@pytest.mark.parametrize("speed", [0, 5])
async def test_the_epoch_closed_at_end_of_file_reaches_history_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, speed: float
) -> None:
    """The last epoch of a file without NAV-EOE is published as the replay ends. The daemon
    used to tell its consumers to stop at that same moment, and the sampler dropped the
    queued epoch: 10 epochs, 9 History rows."""
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    settings = Settings(
        _env_file=None, mtrtk_source=f"file:{RAW_10S}", replay_speed=speed, data_dir=tmp_path
    )
    daemon = Daemon(settings)
    await asyncio.wait_for(daemon.run(), 30.0)
    assert daemon.store.state.epoch_count == 10
    db = Database(tmp_path / "mtrtk.db")
    await db.open()
    try:
        rows = (await db.fetchone("SELECT COUNT(*) FROM samples_1s"))[0]
    finally:
        await db.close()
    assert rows == 10


async def test_replaying_an_hourly_sized_log_through_the_daemon_keeps_every_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An hourly log is ~6.5 MB; framing it in one feed() kept only the last 1 MiB of it."""
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    filler = ubx_frame(0x02, 0x15, b"\x00" * 1000)  # RXM-RAWX-sized: the bulk of a real log
    n = 1500
    data = b"".join(
        ubx("NAV-PVT", iTOW=1000 * i, fixType=3)
        + ubx("NAV-HPPOSLLH", iTOW=1000 * i, lat=23.0)
        + filler
        for i in range(1, n + 1)
    )
    assert len(data) > 1 << 20
    path = tmp_path / "MTRK_20261002_00.ubx"
    path.write_bytes(data)
    settings = Settings(_env_file=None, mtrtk_source=f"file:{path}", replay_speed=0)
    daemon = Daemon(settings)
    sub = daemon.bus.subscribe("state.epoch", policy=Policy.UNBOUNDED)
    await asyncio.wait_for(daemon.run(), 30.0)
    assert daemon.store.state.epoch_count == n
    assert _drain(sub) == [1000 * i for i in range(1, n + 1)]  # from the file's head, in order


# ------------------------------------------------------------- live links
def _feed(store: StateStore, data: bytes) -> None:
    for frame in Framer().feed(data):
        store.apply(frame)


def _drain(sub: Any) -> list[int]:
    itows = []
    while not sub.queue.empty():
        itows.append(sub.queue.get_nowait()[1].time.itow_ms)
    return itows


def test_a_reconnect_drops_the_epoch_it_cut_and_the_tail_it_joins() -> None:
    """No NAV-EOE: neither the epoch a disconnect cut nor the mid-epoch tail after it counts."""
    bus = Bus()
    sub = bus.subscribe("state.epoch", policy=Policy.UNBOUNDED)
    store = StateStore(bus)
    _feed(
        store,
        ubx("NAV-PVT", iTOW=1000, fixType=3)
        + ubx("NAV-HPPOSLLH", iTOW=1000)
        + ubx("NAV-PVT", iTOW=2000, fixType=3),  # closes 1000; 2000 is cut by the disconnect
    )
    store.reset_epoch_inference()
    _feed(
        store,
        ubx("NAV-HPPOSLLH", iTOW=5000)  # reconnected mid-epoch: a tail
        + ubx("NAV-PVT", iTOW=6000, fixType=3)
        + ubx("NAV-HPPOSLLH", iTOW=6000)
        + ubx("NAV-PVT", iTOW=7000, fixType=3),
    )
    assert _drain(sub) == [1000, 6000]


async def test_the_daemon_resets_inference_on_a_live_reconnect_in_stream_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The link events ride the state loop's own queue, so they land between the right frames,
    and a live shutdown does not publish the epoch it cut."""
    monkeypatch.setenv("NTRIP_PASSWORD", "x")

    def no_source() -> Any:
        raise AssertionError("the test never opens a source")

    settings = Settings(_env_file=None, mtrtk_source="/nonexistent/ttyTEST")
    daemon = Daemon(settings, source_factory=no_source)
    sub = daemon.bus.subscribe("state.epoch", policy=Policy.UNBOUNDED)
    loop = asyncio.create_task(daemon._state_loop())

    def send(data: bytes) -> None:
        for frame in Framer().feed(data):
            daemon.bus.publish(TOPIC_RAW_UBX, frame)

    daemon.bus.publish("receiver.connected", "serial:/nonexistent/ttyTEST")
    send(ubx("NAV-PVT", iTOW=1000, fixType=3) + ubx("NAV-PVT", iTOW=2000, fixType=3))
    daemon.bus.publish("receiver.disconnected", "link failure")  # cuts 2000
    daemon.bus.publish("receiver.connected", "serial:/nonexistent/ttyTEST")
    send(
        ubx("NAV-HPPOSLLH", iTOW=5000)  # a tail
        + ubx("NAV-PVT", iTOW=6000, fixType=3)
        + ubx("NAV-PVT", iTOW=7000, fixType=3)  # closes 6000; 7000 is cut by the shutdown
    )
    daemon._raw_sub.close()
    await asyncio.wait_for(loop, 5.0)
    assert _drain(sub) == [1000, 6000]


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("source ended", [1000, 2000]),  # EOF: the last epoch is whole, so it is published
        ("stopped", [1000]),  # SIGINT / stop mid-file: 2000 has only its NAV-PVT so far
        ("link failure: no data for 5.0 s", [1000]),  # rx_timeout reconnect: likewise cut
    ],
)
async def test_a_replay_publishes_its_last_epoch_only_when_the_file_ended(
    monkeypatch: pytest.MonkeyPatch, reason: str, expected: list[int]
) -> None:
    """A paced replay sleeps at the next NAV-PVT before delivering the rest of its chunk, so a
    stop lands with the open epoch's NAV-SAT/SIG/HPPOSLLH still undelivered. Closing it then
    published it with the epoch before's satellites, and the sampler wrote that mixed row."""
    monkeypatch.setenv("NTRIP_PASSWORD", "x")

    def no_source() -> Any:
        raise AssertionError("the test never opens a source")

    settings = Settings(_env_file=None, mtrtk_source=f"file:{RAW_10S}")
    daemon = Daemon(settings, source_factory=no_source)
    sub = daemon.bus.subscribe("state.epoch", policy=Policy.UNBOUNDED)
    loop = asyncio.create_task(daemon._state_loop())
    daemon.bus.publish("receiver.connected", f"file:{RAW_10S}")
    data = (
        ubx("NAV-PVT", iTOW=1000, fixType=3)
        + ubx("NAV-HPPOSLLH", iTOW=1000)
        + ubx("NAV-PVT", iTOW=2000, fixType=3)  # closes 1000; the rest of 2000 is not in yet
    )
    for frame in Framer().feed(data):
        daemon.bus.publish(TOPIC_RAW_UBX, frame)
    daemon.bus.publish("receiver.disconnected", reason)
    daemon._raw_sub.close()  # the shutdown drain: its end_of_stream must not close 2000 either
    await asyncio.wait_for(loop, 5.0)
    assert _drain(sub) == expected


def test_an_inferred_epoch_is_stamped_with_its_own_last_frame_time() -> None:
    """Not with the arrival of the next epoch's first frame, one nav interval later."""
    bus = Bus()
    sub = bus.subscribe("state.epoch", policy=Policy.UNBOUNDED)
    store = StateStore(bus)
    frames = Framer().feed(
        ubx("NAV-PVT", iTOW=1000, fixType=3)
        + ubx("NAV-HPPOSLLH", iTOW=1000)
        + ubx("NAV-PVT", iTOW=2000, fixType=3)
        + ubx("NAV-HPPOSLLH", iTOW=2000)
    )
    store.note_rtcm_injected(9.5)
    for frame, mono in zip(frames, (10.0, 10.2, 11.0, 11.3), strict=True):
        store.apply(frame, now_mono=mono)
    _, first = sub.queue.get_nowait()
    assert first.last_epoch_mono == 10.2
    assert first.rtk.corr_age_s == pytest.approx(0.7)
    store.end_of_stream()
    _, last = sub.queue.get_nowait()
    assert last.last_epoch_mono == 11.3
    assert last.rtk.corr_age_s == pytest.approx(1.8)


# --------------------------------------------------------------- edge cases
def test_inference_logs_once_per_stream(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="mtrtk.core.statestore"):
        replay(RAW_10S.read_bytes())
    assert sum("no NAV-EOE in the stream" in r.getMessage() for r in caplog.records) == 1
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="mtrtk.core.statestore"):
        replay(BASE_30S.read_bytes())
    assert not any("no NAV-EOE in the stream" in r.getMessage() for r in caplog.records)


def test_the_nav_eoe_flip_logs_at_info_once_then_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    """A REPLAY_LOOP over a mixed log flips back to inference once a pass, forever; a live
    receiver whose raw queue drops two NAV-EOE in a row does too. Only the first is news."""
    one_pass = RAW_60S.read_bytes() + BASE_30S.read_bytes()
    with caplog.at_level(logging.DEBUG, logger="mtrtk.core.statestore"):
        replay(one_pass * 4)
    flips = [r for r in caplog.records if "NAV-EOE stopped coming" in r.getMessage()]
    assert [r.levelno for r in flips] == [logging.INFO] + [logging.DEBUG] * 2
    assert "(3 times so far)" in flips[-1].getMessage()


def test_a_stream_that_ends_inside_a_partial_first_epoch_has_no_epochs() -> None:
    _, epochs = replay(ubx("NAV-HPPOSLLH", iTOW=1000, lat=23.2))
    assert epochs == []


def test_a_nav_eoe_stream_joined_mid_epoch_fires_as_it_always_did() -> None:
    """A live connect: the tail's own NAV-EOE fires it, then NAV-EOE closes every epoch."""
    data = (
        ubx("NAV-HPPOSLLH", iTOW=1000, lat=23.1)
        + ubx("NAV-EOE", iTOW=1000)
        + ubx("NAV-PVT", iTOW=2000, fixType=3)
        + ubx("NAV-HPPOSLLH", iTOW=2000, lat=23.2)
        + ubx("NAV-EOE", iTOW=2000)
        + ubx("NAV-PVT", iTOW=3000, fixType=3)
    )
    store, epochs = replay(data)
    assert store.state.epoch_count == 2 and len(epochs) == 2
    assert [round(e.position.lat or 0, 6) for e in epochs] == [23.1, 23.2]  # 1000, then 2000
    assert epochs[1].time.itow_ms == 2000  # the tail had no NAV-PVT, so no time of its own


# Spelled out, not read from EPOCH_NAV_MESSAGES: a member dropped there must fail a case here.
EPOCH_NAV = (
    "NAV-PVT",
    "NAV-HPPOSLLH",
    "NAV-HPPOSECEF",
    "NAV-DOP",
    "NAV-STATUS",
    "NAV-CLOCK",
    "NAV-TIMEGPS",
    "NAV-TIMELS",
    "NAV-TIMEUTC",
    "NAV-SAT",
    "NAV-SIG",
    "NAV-SVIN",
    "NAV-RELPOSNED",
)


def test_the_epoch_nav_messages_are_the_ones_pinned_here() -> None:
    assert frozenset(EPOCH_NAV) == EPOCH_NAV_MESSAGES


@pytest.mark.parametrize("identity", EPOCH_NAV)
def test_every_epoch_nav_message_closes_an_epoch_on_its_own(identity: str) -> None:
    """A log whose LOG_MESSAGES kept any one of them still replays epochs.

    Without NAV-PVT the first epoch cannot be told whole from a tail, so it is not counted.
    """
    extra = {"version": 1} if identity == "NAV-RELPOSNED" else {}  # the F9P's layout
    data = b"".join(ubx(identity, iTOW=itow, **extra) for itow in (1000, 2000, 3000))
    store, epochs = replay(data)
    expected = 3 if identity == "NAV-PVT" else 2
    assert store.state.epoch_count == expected and len(epochs) == expected


def test_a_log_without_nav_pvt_replays_its_epochs_after_the_first() -> None:
    data = (
        ubx("NAV-HPPOSLLH", iTOW=1000)
        + ubx("NAV-SVIN", iTOW=1000)
        + ubx("NAV-HPPOSLLH", iTOW=2000)
        + ubx("NAV-SVIN", iTOW=2000)
        + ubx("NAV-HPPOSLLH", iTOW=3000)
    )
    store, epochs = replay(data, end=False)
    assert store.state.epoch_count == 1 and len(epochs) == 1  # 2000; 1000 may be a tail
    store.end_of_stream()
    assert store.state.epoch_count == 2


def test_env_example_log_messages_match_the_default() -> None:
    """`.env.example` is what a new `.env` is copied from: it must not drop NAV-EOE again."""
    lines = (ROOT / ".env.example").read_text().splitlines()
    values = [line.split("=", 1)[1] for line in lines if line.startswith("LOG_MESSAGES=")]
    assert values == [",".join(DEFAULT_LOG_MESSAGES)]
