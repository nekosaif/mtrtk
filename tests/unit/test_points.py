import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mtrtk.core.bus import Bus
from mtrtk.core.state import ReceiverState
from mtrtk.core.statestore import StateStore
from mtrtk.rover.points import PointCollector, PointsRepo
from mtrtk.rover.sessions import SessionsRepo
from mtrtk.store.db import Database

T0 = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)


def epoch(i: int, lat_off: float = 0.0, carr: int = 2) -> ReceiverState:
    s = ReceiverState()
    s.time.utc = T0 + timedelta(seconds=i)
    s.position.lat, s.position.lon, s.position.height_m, s.position.hmsl_m = (
        23.8373506 + lat_off,
        90.2625502,
        -36.268,
        13.363,
    )
    s.accuracy.h_acc_m, s.accuracy.v_acc_m = 0.012, 0.018
    s.fix.fix_type, s.fix.carr_soln = 3, carr
    return s


@pytest.fixture
async def env(tmp_path: Path):
    db = Database(tmp_path / "m.db")
    await db.open()
    bus = Bus()
    store = StateStore(bus)
    collector = PointCollector(
        bus, store, PointsRepo(db), SessionsRepo(db), default_epochs=5, default_fixed_only=True
    )
    try:
        yield collector, bus, db
    finally:
        await db.close()


async def test_sessions_start_stop_current(env) -> None:
    _, _, db = env
    repo = SessionsRepo(db)
    a = await repo.start("field-1", "rover")
    assert (await repo.current()).id == a.id and a.end_utc is None
    b = await repo.start("field-2", "rover", notes="second")
    assert (await repo.current()).id == b.id
    assert (await repo.list())[1].end_utc is not None  # field-1 was closed automatically
    stopped = await repo.stop()
    assert stopped is not None and stopped.id == b.id and await repo.current() is None


async def test_collect_averages_and_stores_point(env) -> None:
    collector, bus, db = env
    progress = bus.subscribe("points.*")
    session = await SessionsRepo(db).start("s", "rover")
    await collector.start("BM-1", code="BM", note="brass disk", epochs=5)
    offsets = [0.0, 1e-7, -1e-7, 2e-7, -2e-7]  # ~ ±1-2 cm north
    for i, off in enumerate(offsets):
        await collector.on_epoch(epoch(i, lat_off=off))
    st = collector.status
    assert st.state == "done" and st.accepted == 5 and st.point_id is not None
    points = await PointsRepo(db).list()
    assert len(points) == 1
    p = points[0]
    assert p.name == "BM-1" and p.code == "BM" and p.session_id == session.id and p.n_epochs == 5
    assert p.lat == pytest.approx(23.8373506, abs=1e-8) and p.height_m == pytest.approx(
        -36.268, abs=1e-4
    )
    assert (
        0.01 < p.sd_n < 0.03
        and p.sd_e == pytest.approx(0.0, abs=1e-6)
        and p.carr_soln == 2
        and p.h_acc_m == 0.012
    )
    kinds = [t for t, _ in [progress.queue.get_nowait() for _ in range(progress.queue.qsize())]]
    assert kinds.count("points.progress") >= 5 and kinds[-1] == "points.saved"


async def test_fixed_only_skips_float_epochs_and_aborts_after_too_many(env) -> None:
    collector, bus, db = env
    await collector.start("x", epochs=2, fixed_only=True)
    await collector.on_epoch(epoch(0, carr=1))
    await collector.on_epoch(epoch(1, carr=2))
    assert collector.status.accepted == 1 and collector.status.skipped == 1
    for i in range(2, 13):
        await collector.on_epoch(epoch(i, carr=1))
    assert collector.status.state == "aborted" and "fixed" in collector.status.reason.lower()
    assert await PointsRepo(db).list() == []


async def test_cancel_and_restart(env) -> None:
    collector, *_ = env
    await collector.start("a", epochs=3)
    await collector.on_epoch(epoch(0))
    collector.cancel()
    assert collector.status.state == "aborted" and collector.status.reason == "cancelled"
    await collector.start("b", epochs=1, fixed_only=False)
    await collector.on_epoch(epoch(1, carr=0))
    assert collector.status.state == "done"


async def test_start_while_collecting_is_an_error(env) -> None:
    collector, *_ = env
    await collector.start("a", epochs=3)
    with pytest.raises(RuntimeError, match="already"):
        await collector.start("b")


async def test_points_repo_update_delete(env) -> None:
    collector, _, db = env
    await collector.start("p", epochs=1, fixed_only=False)
    await collector.on_epoch(epoch(0))
    repo = PointsRepo(db)
    p = (await repo.list())[0]
    updated = await repo.update(p.id, name="P1", note="renamed")
    assert updated.name == "P1" and updated.note == "renamed" and updated.code == p.code
    await repo.delete(p.id)
    assert await repo.get(p.id) is None


async def test_run_consumes_epochs_from_bus(env) -> None:
    collector, bus, _ = env
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(stop))
    await collector.start("bus", epochs=2, fixed_only=False)
    bus.publish("state.epoch", epoch(0))
    bus.publish("state.epoch", epoch(1))
    await asyncio.sleep(0.05)
    assert collector.status.state == "done"
    collector.stop()
    await asyncio.wait_for(task, 1.0)


async def test_start_rejects_bad_arguments(env) -> None:
    collector, *_ = env
    with pytest.raises(ValueError, match="name"):
        await collector.start("  ")
    with pytest.raises(ValueError, match="epochs"):
        await collector.start("x", epochs=0)
    assert collector.status.state == "idle"


async def test_epochs_without_a_3d_fix_are_skipped_even_when_float_is_allowed(env) -> None:
    collector, *_ = env
    await collector.start("x", epochs=1, fixed_only=False)
    no_fix = epoch(0)
    no_fix.fix.fix_type = 2  # 2D: the height is not measured
    await collector.on_epoch(no_fix)
    invalid = epoch(1)
    invalid.position.invalid_llh = True
    await collector.on_epoch(invalid)
    assert collector.status.state == "collecting" and collector.status.skipped == 2
    for _ in range(3):
        await collector.on_epoch(no_fix)
    assert collector.status.state == "aborted" and "3D fix" in (collector.status.reason or "")


async def test_progress_items_are_snapshots(env) -> None:
    collector, bus, _ = env
    sub = bus.subscribe("points.progress")
    await collector.start("snap", epochs=2, fixed_only=False)
    await collector.on_epoch(epoch(0))
    await collector.on_epoch(epoch(1))
    accepted = [item.accepted for _, item in [sub.queue.get_nowait() for _ in range(3)]]
    assert accepted == [0, 1, 2]


async def test_delete_reports_whether_a_point_existed(env) -> None:
    _, _, db = env
    assert await PointsRepo(db).delete(999) is False
