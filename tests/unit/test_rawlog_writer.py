import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pyubx2 import GET, UBXMessage

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame, Framer
from mtrtk.rawlog.writer import RawLogWriter, Sidecar, log_path, recover_incomplete, sidecar_path
from ubxtest import ubx_frame

RAWX = ubx_frame(0x02, 0x15, b"\x11" * 16)
SFRBX = ubx_frame(0x02, 0x13, b"\x22" * 8)
NAV_SAT = ubx_frame(0x01, 0x35, b"\x33" * 8)  # not in the log filter
MESSAGES = ["RXM-RAWX", "RXM-SFRBX", "NAV-PVT"]


def frames(raw: bytes) -> list[Frame]:
    return Framer().feed(raw)


def pvt(hour: int, minute: int = 0, second: int = 0, valid: int = 1) -> bytes:
    return UBXMessage(
        "NAV",
        "NAV-PVT",
        GET,
        iTOW=1,
        year=2026,
        month=9,
        day=18,
        hour=hour,
        min=minute,
        second=second,
        validDate=valid,
        validTime=valid,
        fixType=3,
    ).serialize()


def make_writer(tmp_path: Path, bus: Bus | None = None) -> RawLogWriter:
    return RawLogWriter(bus or Bus(), tmp_path, "MTRK", MESSAGES, role="base", firmware="HPG 1.13")


def test_log_path_layout(tmp_path: Path) -> None:
    hour = datetime(2026, 9, 18, 16, tzinfo=UTC)
    path = log_path(tmp_path, "MTRK", hour)
    assert path == tmp_path / "ubx" / "2026" / "261" / "MTRK_20260918_16.ubx"
    assert sidecar_path(path) == path.with_suffix(".json")


def test_frames_before_time_is_known_are_buffered_then_written(tmp_path: Path) -> None:
    w = make_writer(tmp_path)
    for f in frames(RAWX + SFRBX):
        w.handle(f)
    assert w.current_path is None
    for f in frames(pvt(16) + RAWX):
        w.handle(f)
    w.close()
    path = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 16, tzinfo=UTC))
    assert path.read_bytes() == RAWX + SFRBX + pvt(16) + RAWX
    sc = Sidecar.load(sidecar_path(path))
    assert sc.msg_counts == {"RXM-RAWX": 2, "RXM-SFRBX": 1, "NAV-PVT": 1}
    assert sc.bytes == path.stat().st_size
    assert sc.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert sc.complete is True and sc.start_utc == "2026-09-18T16:00:00+00:00"
    assert sc.firmware == "HPG 1.13" and sc.station_id == "MTRK" and sc.keep is False


def test_unfiltered_messages_are_not_logged(tmp_path: Path) -> None:
    w = make_writer(tmp_path)
    for f in frames(pvt(16) + NAV_SAT + RAWX):
        w.handle(f)
    w.close()
    path = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 16, tzinfo=UTC))
    assert path.read_bytes() == pvt(16) + RAWX


def test_rotation_on_receiver_hour_boundary(tmp_path: Path) -> None:
    bus = Bus()
    events = bus.subscribe("rawlog.*")
    w = make_writer(tmp_path, bus)
    for f in frames(pvt(16, 59, 59) + RAWX + pvt(17, 0, 0) + RAWX):
        w.handle(f)
    w.close()
    p16 = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 16, tzinfo=UTC))
    p17 = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 17, tzinfo=UTC))
    assert p16.read_bytes() == pvt(16, 59, 59) + RAWX
    assert p17.read_bytes() == pvt(17, 0, 0) + RAWX
    assert Sidecar.load(sidecar_path(p16)).complete is True
    assert Sidecar.load(sidecar_path(p16)).end_utc == "2026-09-18T17:00:00+00:00"
    topics = [t for t, _ in [events.queue.get_nowait() for _ in range(events.queue.qsize())]]
    assert topics == ["rawlog.rotated", "rawlog.closed", "rawlog.rotated", "rawlog.closed"]


def test_invalid_time_does_not_advance_clock(tmp_path: Path) -> None:
    w = make_writer(tmp_path)
    for f in frames(pvt(16) + pvt(18, valid=0) + RAWX):
        w.handle(f)
    w.close()
    assert log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 16, tzinfo=UTC)).exists()
    assert not log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 18, tzinfo=UTC)).exists()


def test_pending_cap_falls_back_to_host_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mtrtk.rawlog import writer as writer_mod

    monkeypatch.setattr(writer_mod, "PENDING_CAP_BYTES", 40)
    fixed_now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return fixed_now

    monkeypatch.setattr(writer_mod, "datetime", FixedDatetime)
    w = make_writer(tmp_path)
    for f in frames(RAWX + RAWX + RAWX):
        w.handle(f)
    w.close()
    path = log_path(tmp_path, "MTRK", datetime(2026, 1, 2, 3, tzinfo=UTC))
    assert path.read_bytes() == RAWX * 3
    assert Sidecar.load(sidecar_path(path)).time_source == "host"


def test_tick_flushes_and_updates_sidecar(tmp_path: Path) -> None:
    w = make_writer(tmp_path)
    for f in frames(pvt(16) + RAWX):
        w.handle(f)
    path = w.current_path
    assert path is not None
    w._last_sidecar = -1e9  # force the 60 s sidecar refresh on this tick
    w.tick(now_mono=1000.0)
    assert path.stat().st_size == len(pvt(16)) + len(RAWX)  # flushed to disk
    sc = Sidecar.load(sidecar_path(path))
    assert sc.complete is False and sc.bytes == path.stat().st_size
    w.close()


def test_recover_incomplete_finalizes_orphans(tmp_path: Path) -> None:
    path = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 12, tzinfo=UTC))
    path.parent.mkdir(parents=True)
    path.write_bytes(RAWX * 5)
    Sidecar("MTRK", "base", "2026-09-18T12:00:00+00:00", hour_utc="2026-09-18T12:00:00+00:00").dump(
        sidecar_path(path)
    )
    orphan_json = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 13, tzinfo=UTC)).with_suffix(
        ".json"
    )
    Sidecar("MTRK", "base", None).dump(orphan_json)  # sidecar without data file
    recovered = recover_incomplete(tmp_path)
    assert recovered == [path]
    sc = Sidecar.load(sidecar_path(path))
    assert sc.complete is True and sc.recovered is True
    assert sc.bytes == len(RAWX) * 5 and sc.sha256 == hashlib.sha256(RAWX * 5).hexdigest()
    # the host clock is not the receiver clock: say the end is unknown rather than invent one
    assert sc.end_utc is None and sc.end_utc_source == "unknown"
    assert not orphan_json.exists()


async def test_run_consumes_bus_and_reports_backpressure(tmp_path: Path) -> None:
    import asyncio

    bus = Bus()
    pressure = bus.subscribe("rawlog.backpressure")
    w = RawLogWriter(bus, tmp_path, "MTRK", MESSAGES, role="base")
    w.sub.high_water = 2
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    for f in frames(pvt(16) + RAWX + RAWX + RAWX):
        bus.publish("raw.ubx", f)
    await asyncio.sleep(0.05)
    w.stop()
    await asyncio.wait_for(task, 2.0)
    path = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 16, tzinfo=UTC))
    assert path.read_bytes() == pvt(16) + RAWX * 3
    assert pressure.queue.qsize() >= 1


def test_sidecar_json_is_plain_and_loadable(tmp_path: Path) -> None:
    sc = Sidecar("MTRK", "base", "2026-09-18T16:00:00+00:00", msg_counts={"RXM-RAWX": 3})
    p = tmp_path / "x.json"
    sc.dump(p)
    assert json.loads(p.read_text())["msg_counts"] == {"RXM-RAWX": 3}
    assert Sidecar.load(p) == sc


def test_receiver_time_after_host_fallback_restores_receiver_naming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mtrtk.rawlog import writer as writer_mod

    monkeypatch.setattr(writer_mod, "PENDING_CAP_BYTES", 40)
    fixed_now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return fixed_now

    monkeypatch.setattr(writer_mod, "datetime", FixedDatetime)
    w = make_writer(tmp_path)
    for f in frames(RAWX + RAWX):  # trips the cap: named by the host clock
        w.handle(f)
    for f in frames(pvt(16) + RAWX):  # receiver time arrives
        w.handle(f)
    w.close()
    host_path = log_path(tmp_path, "MTRK", datetime(2026, 1, 2, 3, tzinfo=UTC))
    rx_path = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 16, tzinfo=UTC))
    assert Sidecar.load(sidecar_path(host_path)).time_source == "host"
    assert Sidecar.load(sidecar_path(rx_path)).time_source == "receiver"
    assert rx_path.read_bytes() == pvt(16) + RAWX


async def test_run_exits_when_the_stop_event_is_set(tmp_path: Path) -> None:
    import asyncio

    bus = Bus()
    w = RawLogWriter(bus, tmp_path, "MTRK", MESSAGES, role="base")
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    for f in frames(pvt(16) + RAWX):
        bus.publish("raw.ubx", f)
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, 2.0)  # the stop event alone must end run()
    path = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 16, tzinfo=UTC))
    assert path.read_bytes() == pvt(16) + RAWX  # queued frames drained before the exit
    assert Sidecar.load(sidecar_path(path)).complete is True
    assert bus.subscriber_count == 0  # unsubscribed, not just closed


async def test_run_survives_a_write_error_and_keeps_logging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from mtrtk.rawlog import writer as writer_mod

    real_write = writer_mod._OpenLog.write
    calls = {"n": 0}

    def flaky(self: object, raw: bytes, identity: str) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # ENOSPC on one frame
            raise OSError(28, "No space left on device")
        real_write(self, raw, identity)

    monkeypatch.setattr(writer_mod._OpenLog, "write", flaky)
    bus = Bus()
    errors = bus.subscribe("rawlog.error")
    w = RawLogWriter(bus, tmp_path, "MTRK", MESSAGES, role="base")
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    for f in frames(pvt(16) + RAWX + SFRBX):
        bus.publish("raw.ubx", f)
    await asyncio.sleep(0.05)
    assert errors.queue.qsize() == 1  # reported, once
    assert bus.subscriber_count >= 2  # still subscribed: the log did not end
    stop.set()
    await asyncio.wait_for(task, 2.0)
    path = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 16, tzinfo=UTC))
    assert path.read_bytes() == pvt(16) + SFRBX  # the frame after the failure still lands
    assert Sidecar.load(sidecar_path(path)).complete is True


async def test_run_reports_a_flush_error_and_stays_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from mtrtk.rawlog import writer as writer_mod

    monkeypatch.setattr(writer_mod, "FLUSH_INTERVAL_S", 0.01)

    def boom(self: object) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(writer_mod._OpenLog, "flush", boom)
    bus = Bus()
    errors = bus.subscribe("rawlog.error")
    w = RawLogWriter(bus, tmp_path, "MTRK", MESSAGES, role="base")
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    for f in frames(pvt(16) + RAWX):
        bus.publish("raw.ubx", f)
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 2.0)
    assert errors.queue.qsize() >= 1
    path = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 16, tzinfo=UTC))
    assert path.read_bytes() == pvt(16) + RAWX  # close() still fsynced and finalized
    assert Sidecar.load(sidecar_path(path)).complete is True


def test_leap_second_does_not_kill_the_clock(tmp_path: Path) -> None:
    w = make_writer(tmp_path)
    for f in frames(pvt(16, 59, 60)):  # u-blox documents NAV-PVT sec as 0..60
        w.handle(f)
    assert w.receiver_utc == datetime(2026, 9, 18, 16, 59, 59, tzinfo=UTC)
    w.close()
    assert log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 16, tzinfo=UTC)).exists()


async def test_run_unsubscribes_when_cancelled(tmp_path: Path) -> None:
    import asyncio

    bus = Bus()
    w = RawLogWriter(bus, tmp_path, "MTRK", MESSAGES, role="base")
    assert bus.subscriber_count == 1
    task = asyncio.create_task(w.run(asyncio.Event()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bus.subscriber_count == 0  # no orphaned UNBOUNDED queue behind


def test_restart_inside_the_hour_carries_the_sidecar_forward(tmp_path: Path) -> None:
    first = make_writer(tmp_path)
    for f in frames(pvt(16) + RAWX):
        first.handle(f)
    first.close()
    path = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 16, tzinfo=UTC))
    assert Sidecar.load(sidecar_path(path)).recovered is False

    second = make_writer(tmp_path)  # crash + restart inside the same hour
    for f in frames(pvt(16, 30) + SFRBX):
        second.handle(f)
    second.close()
    sc = Sidecar.load(sidecar_path(path))
    assert path.read_bytes() == pvt(16) + RAWX + pvt(16, 30) + SFRBX
    assert sc.msg_counts == {"NAV-PVT": 2, "RXM-RAWX": 1, "RXM-SFRBX": 1}
    assert sc.start_utc == "2026-09-18T16:00:00+00:00"  # the first writer's start, kept
    assert sc.end_utc == "2026-09-18T16:30:00+00:00"
    assert sc.recovered is True and sc.complete is True
    assert sc.bytes == path.stat().st_size
    assert sc.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_tick_closes_the_hour_when_the_receiver_goes_quiet(tmp_path: Path) -> None:
    """A base whose NAV-PVT stops mid-hour must not leave the hour open for ever: the 1 s
    tick projects the last receiver UTC forward and closes the file at the boundary."""
    bus = Bus()
    events = bus.subscribe("rawlog.*")
    w = make_writer(tmp_path, bus)
    for f in frames(pvt(16, 59, 30) + RAWX):
        w.handle(f)
    path = w.current_path
    assert path is not None
    base = w._utc_mono
    assert base is not None
    w.tick(base + 29.0)  # 16:59:59 projected: still inside the hour
    assert w.current_path == path
    w.tick(base + 31.0)  # 17:00:01 projected: the hour is over
    assert w.current_path is None
    sc = Sidecar.load(sidecar_path(path))
    assert sc.complete is True
    assert sc.end_utc == "2026-09-18T16:59:30+00:00"  # the receiver clock, not the host's
    topics = [t for t, _ in [events.queue.get_nowait() for _ in range(events.queue.qsize())]]
    assert topics == ["rawlog.rotated", "rawlog.closed"]


def test_tick_rotation_does_not_fire_inside_the_hour(tmp_path: Path) -> None:
    w = make_writer(tmp_path)
    for f in frames(pvt(16, 0, 0) + RAWX):
        w.handle(f)
    base = w._utc_mono
    assert base is not None
    w.tick(base + 3599.0)
    assert w.current_path is not None
    w.close()


async def test_backpressure_fires_once_and_clears_when_the_queue_drains(tmp_path: Path) -> None:
    import asyncio

    bus = Bus()
    events = bus.subscribe("rawlog.backpressure", "rawlog.drained")
    w = RawLogWriter(bus, tmp_path, "MTRK", MESSAGES, role="base")
    w.sub.high_water = 4
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    for f in frames(pvt(16) + RAWX * 8):
        bus.publish("raw.ubx", f)
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 2.0)
    seen = [events.queue.get_nowait() for _ in range(events.queue.qsize())]
    assert [t for t, _ in seen] == ["rawlog.backpressure", "rawlog.drained"]
    assert seen[0][1]["queued"] >= 4
    assert seen[1][1]["queued"] < 4 // 2


async def test_backpressure_repeats_at_most_once_a_minute(tmp_path: Path) -> None:
    from mtrtk.rawlog import writer as writer_mod

    bus = Bus()
    events = bus.subscribe("rawlog.backpressure")
    w = RawLogWriter(bus, tmp_path, "MTRK", MESSAGES, role="base")
    w.sub.high_water = 2
    for f in frames(RAWX * 4):
        bus.publish("raw.ubx", f)  # queued on the writer's own subscription
    w._check_pressure()
    w._check_pressure()
    assert events.queue.qsize() == 1  # still above, inside the window: silent
    w._backpressure_last -= writer_mod.BACKPRESSURE_REPEAT_S + 1.0
    w._check_pressure()
    assert events.queue.qsize() == 2
    w.stop()


def test_default_high_water_is_two_thousand_frames(tmp_path: Path) -> None:
    assert make_writer(tmp_path).sub.high_water == 2000


def test_metadata_set_after_the_file_opened_lands_in_the_open_sidecar(tmp_path: Path) -> None:
    w = make_writer(tmp_path)
    for f in frames(pvt(16) + RAWX):
        w.handle(f)
    path = w.current_path
    assert path is not None
    w.site = "ROOF"  # what the daemon's track_metadata() does on base.mode
    w.firmware = "HPG 1.32"
    w._last_sidecar = -1e9
    w.tick(now_mono=1000.0)
    sc = Sidecar.load(sidecar_path(path))
    assert sc.site == "ROOF" and sc.firmware == "HPG 1.32"
    w.close()


def test_restart_carries_every_sidecar_field_forward(tmp_path: Path) -> None:
    first = make_writer(tmp_path)
    first.site = "ROOF"
    for f in frames(pvt(16) + RAWX):
        first.handle(f)
    path = first.current_path
    assert path is not None
    first.close()
    sc_path = sidecar_path(path)
    sc = Sidecar.load(sc_path)
    sc.keep = True  # what the (Phase 3) API marks on an hour worth keeping
    sc.time_source = "host"
    sc.dump(sc_path)

    second = RawLogWriter(Bus(), tmp_path, "MTRK", MESSAGES, role="base")  # no firmware, no site
    for f in frames(pvt(16, 30) + SFRBX):
        second.handle(f)
    second.close()
    sc = Sidecar.load(sc_path)
    assert sc.keep is True
    assert sc.firmware == "HPG 1.13"
    assert sc.site == "ROOF"
    assert sc.time_source == "host"
    assert sc.recovered is True


async def test_ticker_fsyncs_in_a_worker_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading

    from mtrtk.rawlog import writer as writer_mod

    monkeypatch.setattr(writer_mod, "FLUSH_INTERVAL_S", 0.01)
    threads: list[threading.Thread] = []
    real = writer_mod._fsync_fd

    def spy(fd: int) -> None:
        threads.append(threading.current_thread())
        real(fd)

    monkeypatch.setattr(writer_mod, "_fsync_fd", spy)
    bus = Bus()
    w = RawLogWriter(bus, tmp_path, "MTRK", MESSAGES, role="base", fsync_interval_s=0)
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    for f in frames(pvt(16) + RAWX):
        bus.publish("raw.ubx", f)
    await asyncio.sleep(0.05)
    during = list(threads)  # the fsync `close()` does on the loop is a different, final one
    stop.set()
    await asyncio.wait_for(task, 2.0)
    assert during, "the ticker never fsynced"
    assert all(t is not threading.current_thread() for t in during)


async def test_only_one_fsync_is_in_flight_per_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import time

    from mtrtk.rawlog import writer as writer_mod

    calls: list[int] = []
    monkeypatch.setattr(writer_mod, "_fsync_fd", lambda fd: calls.append(fd))
    w = RawLogWriter(Bus(), tmp_path, "MTRK", MESSAGES, role="base", fsync_interval_s=0)
    for f in frames(pvt(16) + RAWX):
        w.handle(f)
    now = time.monotonic()
    await asyncio.gather(w._maybe_fsync(now + 1.0), w._maybe_fsync(now + 2.0))
    assert len(calls) == 1
    w.close()
    w.stop()


def test_sidecar_dump_fsyncs_the_tmp_file_before_replacing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mtrtk.rawlog import writer as writer_mod

    path = tmp_path / "x.json"
    tmp = path.with_suffix(".json.tmp")
    seen: list[tuple[bool, bool]] = []
    real = writer_mod._fsync_sidecar

    def spy(fh: object) -> None:
        seen.append((tmp.exists(), path.exists()))
        real(fh)

    monkeypatch.setattr(writer_mod, "_fsync_sidecar", spy)
    Sidecar("MTRK", "base", None).dump(path)
    assert seen == [(True, False)]  # durable before the rename, and only then replaced
    assert path.exists() and not tmp.exists()


def test_recover_incomplete_drops_stray_tmp_sidecars(tmp_path: Path) -> None:
    path = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 12, tzinfo=UTC))
    path.parent.mkdir(parents=True)
    path.write_bytes(RAWX)
    Sidecar("MTRK", "base", None).dump(sidecar_path(path))
    stray = path.with_suffix(".json.tmp")
    stray.write_text("{half written")  # crash between write and os.replace
    recover_incomplete(tmp_path)
    assert not stray.exists()
    assert sidecar_path(path).exists()


def test_a_frozen_receiver_clock_does_not_churn_the_hour(tmp_path: Path) -> None:
    """NAV-PVT stops carrying valid time while RXM-RAWX keeps flowing.

    The ticker closes the elapsed hour, but if the projected clock is left at the stale
    reading the very next frame names that same hour again: `handle()` reopens the file just
    finalised, reads and re-hashes all of it, and the next tick closes it again - once a
    second for as long as the receiver clock stays put.
    """
    bus = Bus()
    events = bus.subscribe("rawlog.*")
    w = make_writer(tmp_path, bus)
    for f in frames(pvt(16, 59, 30) + RAWX):
        w.handle(f)
    first = w.current_path
    assert first is not None
    base = w._utc_mono
    assert base is not None
    for i in range(5):
        w.tick(base + 31.0 + i)
        for f in frames(RAWX):  # data still flows; the receiver just stopped stamping time
            w.handle(f)
    w.close()
    topics = [t for t, _ in [events.queue.get_nowait() for _ in range(events.queue.qsize())]]
    assert topics.count("rawlog.rotated") == 2  # hour 16 and hour 17, not one per tick
    assert topics.count("rawlog.closed") == 2  # the elapsed hour, then the final close
    second = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 17, tzinfo=UTC))
    assert first.read_bytes() == pvt(16, 59, 30) + RAWX
    assert second.read_bytes() == RAWX * 5
    assert Sidecar.load(sidecar_path(first)).complete is True


def test_a_repeated_receiver_timestamp_cannot_reopen_a_closed_hour(tmp_path: Path) -> None:
    """Some receivers keep stamping the same UTC instead of dropping validity; that must not
    reopen - and re-hash and re-finalise - the hour the ticker has already closed."""
    w = make_writer(tmp_path)
    for f in frames(pvt(16, 59, 30) + RAWX):
        w.handle(f)
    first = w.current_path
    assert first is not None
    base = w._utc_mono
    assert base is not None
    w.tick(base + 31.0)
    assert w.current_path is None
    for f in frames(pvt(16, 59, 30) + RAWX):  # the very same stale stamp, again
        w.handle(f)
    assert w.current_path == log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 17, tzinfo=UTC))
    assert first.read_bytes() == pvt(16, 59, 30) + RAWX  # untouched since it was finalised
    assert Sidecar.load(sidecar_path(first)).complete is True
    w.close()


async def test_the_fsync_worker_closes_its_own_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling the ticker must not close the dup'd fd under a worker still inside
    `os.fsync`: by the time that call returns the number may name a different file."""
    import asyncio
    import os
    import threading
    import time

    from mtrtk.rawlog import writer as writer_mod

    inside = threading.Event()
    release = threading.Event()
    seen: list[int] = []

    def slow_fsync(fd: int) -> None:
        seen.append(fd)
        inside.set()
        release.wait(5.0)

    monkeypatch.setattr(writer_mod, "_fsync_fd", slow_fsync)
    w = RawLogWriter(Bus(), tmp_path, "MTRK", MESSAGES, role="base", fsync_interval_s=0)
    for f in frames(pvt(16) + RAWX):
        w.handle(f)
    task = asyncio.create_task(w._maybe_fsync(time.monotonic() + 1.0))
    await asyncio.to_thread(inside.wait, 5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    fd = seen[0]
    os.fstat(fd)  # still the worker's: closing it here would close it under the fsync
    release.set()
    for _ in range(400):
        try:
            os.fstat(fd)
        except OSError:
            break
        await asyncio.sleep(0.005)
    else:
        raise AssertionError("the worker never closed the descriptor it was given")
    w.close()
    w.stop()


async def test_a_writer_that_stops_while_behind_says_it_drained(tmp_path: Path) -> None:
    """The supervisor restarts the raw logger; the new one never publishes `rawlog.drained`,
    so without this the `logger_backpressure` alert stays raised for the whole process."""
    import asyncio

    bus = Bus()
    events = bus.subscribe("rawlog.*")
    w = make_writer(tmp_path, bus)
    w._backpressure_active = True
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    stop.set()
    await asyncio.wait_for(task, 2.0)
    topics = [t for t, _ in [events.queue.get_nowait() for _ in range(events.queue.qsize())]]
    assert "rawlog.drained" in topics


def test_live_metadata_beats_the_resumed_sidecar(tmp_path: Path) -> None:
    """A restart that resumes an hour carries the old sidecar forward, but the site the daemon
    has just told this writer about is the current truth - the stale one must not win."""
    first = make_writer(tmp_path)
    first.site = "old-roof"
    first.firmware = "HPG 1.12"
    for f in frames(pvt(16) + RAWX):
        first.handle(f)
    first.close()
    path = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 16, tzinfo=UTC))

    resumed = RawLogWriter(
        Bus(), tmp_path, "MTRK", MESSAGES, role="base", firmware="HPG 1.13", site="new-roof"
    )
    for f in frames(pvt(16) + RAWX):
        resumed.handle(f)
    resumed.close()
    sc = Sidecar.load(sidecar_path(path))
    assert sc.site == "new-roof" and sc.firmware == "HPG 1.13"

    blind = RawLogWriter(Bus(), tmp_path, "MTRK", MESSAGES, role="base")  # nothing live to say
    for f in frames(pvt(16) + RAWX):
        blind.handle(f)
    blind.close()
    sc = Sidecar.load(sidecar_path(path))
    assert sc.site == "new-roof" and sc.firmware == "HPG 1.13"  # carried over, not lost


def test_a_corrected_earlier_reading_after_a_ticker_close_names_its_own_hour(
    tmp_path: Path,
) -> None:
    """The closed-hour guard is for a receiver that keeps *repeating* the reading the ticker
    closed the hour under. A different reading in that hour - a clock stepped back by a
    correction - names the hour it says: the finalised file is resumed once, instead of every
    later frame being misfiled into the next hour for the rest of the writer's life."""
    w = make_writer(tmp_path)
    for f in frames(pvt(16, 59, 30) + RAWX):
        w.handle(f)
    p16 = w.current_path
    assert p16 is not None
    base = w._utc_mono
    assert base is not None
    w.tick(base + 31.0)  # hour 16 closed on the projected clock
    for f in frames(RAWX):  # named by the projected clock: hour 17
        w.handle(f)
    p17 = w.current_path
    assert p17 == log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 17, tzinfo=UTC))
    for f in frames(pvt(16, 40) + RAWX):  # the receiver clock stepped back
        w.handle(f)
    assert w.current_path == p16
    for f in frames(pvt(16, 50) + RAWX):
        w.handle(f)
    assert w.current_path == p16
    for f in frames(pvt(16, 59, 30) + RAWX):  # the old stale stamp again: no longer guarded
        w.handle(f)
    assert w.current_path == p16
    w.close()
    assert p16.read_bytes() == (
        pvt(16, 59, 30) + RAWX + pvt(16, 40) + RAWX + pvt(16, 50) + RAWX + pvt(16, 59, 30) + RAWX
    )
    assert p17 is not None and p17.read_bytes() == RAWX
    sc = Sidecar.load(sidecar_path(p16))
    assert sc.complete is True and sc.recovered is True
    assert sc.msg_counts == {"NAV-PVT": 4, "RXM-RAWX": 4}


def test_set_keep_marks_the_open_hour_and_dumps_it_at_once(tmp_path: Path) -> None:
    """The API marks the hour being written through the writer, not behind its back."""
    w = make_writer(tmp_path)
    for f in frames(pvt(16) + RAWX):
        w.handle(f)
    path = w.current_path
    assert path is not None
    w.set_keep(True)
    # On disk straight away: the periodic dump is a minute away, and the operator's mark has to
    # survive a power cut in between.
    assert Sidecar.load(sidecar_path(path)).keep is True
    w.close()  # and the finalised sidecar is written from the same in-memory object
    assert Sidecar.load(sidecar_path(path)).keep is True


def test_set_keep_without_an_open_file_does_nothing(tmp_path: Path) -> None:
    w = make_writer(tmp_path)
    w.set_keep(True)
    assert w.current_path is None and list(tmp_path.glob("ubx/**/*")) == []
