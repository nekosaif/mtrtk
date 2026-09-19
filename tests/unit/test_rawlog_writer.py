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
    assert sc.end_utc is not None
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
