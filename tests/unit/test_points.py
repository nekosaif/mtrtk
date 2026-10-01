import asyncio
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.geo import ecef_to_enu, llh_to_ecef
from mtrtk.core.state import ReceiverState
from mtrtk.core.statestore import StateStore
from mtrtk.rover.exports import to_csv
from mtrtk.rover.points import PointCollector, PointsRepo
from mtrtk.rover.sessions import SessionsRepo
from mtrtk.store.db import Database
from mtrtk.store.models import Point

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
    s.fix.gnss_fix_ok = True
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
    collector, _, db = env
    await collector.start("a", epochs=3)
    await collector.on_epoch(epoch(0))
    collector.cancel()
    assert collector.status.state == "aborted" and collector.status.reason == "cancelled"
    await collector.start("b", epochs=1, fixed_only=False)
    await collector.on_epoch(epoch(1, carr=0))
    assert collector.status.state == "done"
    # the cancelled collection stored nothing: "b" is the only point
    assert [p.name for p in await PointsRepo(db).list()] == ["b"]


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
    saved = bus.subscribe("points.saved")
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(stop))
    await collector.start("bus", epochs=2, fixed_only=False)
    bus.publish("state.epoch", epoch(0))
    bus.publish("state.epoch", epoch(1))
    # deterministic: wait for the stored point, not for a wall-clock interval
    _, point = await asyncio.wait_for(saved.queue.get(), 2.0)
    assert point.name == "bus" and collector.status.state == "done"
    stop.set()
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


# ------------------------------------------------------------------ fix round 1


def at(lat_off: float, dh: float, i: int) -> ReceiverState:
    s = epoch(i, lat_off=lat_off)
    assert s.position.height_m is not None
    s.position.height_m += dh
    return s


async def test_mean_position_is_the_average_not_the_first_epoch(env) -> None:
    collector, _, db = env
    await collector.start("avg", epochs=3)
    for i, (off, dh) in enumerate([(0.0, 0.0), (2e-7, 0.02), (4e-7, 0.04)]):
        await collector.on_epoch(at(off, dh, i))
    p = (await PointsRepo(db).list())[0]
    assert p.lat == pytest.approx(23.8373506 + 2e-7, abs=1e-10)
    assert p.lon == pytest.approx(90.2625502, abs=1e-10)
    assert p.height_m == pytest.approx(-36.248, abs=1e-4)
    st = collector.status
    assert st.mean_lat == pytest.approx(p.lat, abs=1e-12) and st.mean_h == pytest.approx(
        p.height_m, abs=1e-9
    )


async def test_sd_is_the_sample_sd_and_zero_for_one_epoch(env) -> None:
    collector, _, db = env
    offsets = [0.0, 1e-7, -1e-7, 2e-7, -2e-7]
    await collector.start("sd", epochs=5)
    for i, off in enumerate(offsets):
        await collector.on_epoch(epoch(i, lat_off=off))
    ref = (23.8373506, 90.2625502, -36.268)
    north = [ecef_to_enu(*ref, *llh_to_ecef(ref[0] + off, ref[1], ref[2]))[1] for off in offsets]
    p = (await PointsRepo(db).list())[0]
    assert p.sd_n == pytest.approx(statistics.stdev(north), rel=1e-6)
    await collector.start("one", epochs=1)
    await collector.on_epoch(epoch(10))
    one = (await PointsRepo(db).list())[0]
    assert one.name == "one" and one.sd_n == one.sd_e == one.sd_u == 0.0


async def test_quality_is_the_worst_of_the_accepted_epochs(env) -> None:
    collector, _, db = env
    await collector.start("mix", epochs=30, fixed_only=False)
    for i in range(29):
        float_epoch = epoch(i, carr=1)
        float_epoch.accuracy.h_acc_m, float_epoch.accuracy.v_acc_m = 0.5, 0.8
        await collector.on_epoch(float_epoch)
    await collector.on_epoch(epoch(29, carr=2))  # one fixed epoch at cm level, last
    p = (await PointsRepo(db).list())[0]
    assert p.carr_soln == 1 and p.h_acc_m == 0.5 and p.v_acc_m == 0.8
    assert "RTK float" in to_csv([p]) and "RTK fixed" not in to_csv([p])
    # GNSS + dead reckoning is accepted, and it ranks below a plain 3D fix
    await collector.start("dr", epochs=2, fixed_only=False)
    dr = epoch(40)
    dr.fix.fix_type = 4
    await collector.on_epoch(dr)
    await collector.on_epoch(epoch(41))
    p = (await PointsRepo(db).list())[0]
    assert p.name == "dr" and p.n_epochs == 2 and p.fix_type == 4


async def test_epochs_without_gnss_fix_ok_are_skipped(env) -> None:
    collector, *_ = env
    await collector.start("ok", epochs=1, fixed_only=False)
    not_ok = epoch(0)
    not_ok.fix.gnss_fix_ok = False
    await collector.on_epoch(not_ok)
    assert collector.status.accepted == 0 and collector.status.skipped == 1


async def test_a_repeated_epoch_is_not_counted_twice(env) -> None:
    collector, _, db = env
    await collector.start("fresh", epochs=2)
    first = epoch(0)
    first.time.itow_ms = 1000
    await collector.on_epoch(first)
    await collector.on_epoch(first.model_copy(deep=True))  # NAV-PVT lost: the same epoch again
    assert collector.status.accepted == 1 and collector.status.skipped == 1
    collector.cancel()
    stale_utc = epoch(5)  # no iTOW: the UTC time decides
    await collector.start("utc", epochs=2)
    await collector.on_epoch(stale_utc)
    await collector.on_epoch(stale_utc.model_copy(deep=True))
    assert collector.status.accepted == 1 and collector.status.skipped == 1
    await collector.on_epoch(epoch(6))
    assert collector.status.state == "done"
    assert (await PointsRepo(db).list())[0].n_epochs == 2


async def test_session_is_the_one_open_when_collection_started(env) -> None:
    collector, _, db = env
    sessions = SessionsRepo(db)
    first = await sessions.start("first", "rover")
    await collector.start("long", epochs=2)
    await collector.on_epoch(epoch(0))
    await sessions.start("second", "rover")
    await collector.on_epoch(epoch(1))
    assert (await PointsRepo(db).list())[0].session_id == first.id


class FailingPoints(PointsRepo):
    async def add(self, p: Point) -> Point:
        raise OSError("disk full")


class BlockingPoints(PointsRepo):
    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def add(self, p: Point) -> Point:
        self.entered.set()
        await self.release.wait()
        return await super().add(p)


async def test_stop_event_ends_a_silent_run(env) -> None:
    collector, *_ = env
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(stop))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, 1.0)


async def test_a_failed_save_aborts_and_is_not_retried(env) -> None:
    _, bus, db = env
    collector = PointCollector(bus, StateStore(bus), FailingPoints(db), SessionsRepo(db))
    await collector.start("x", epochs=1, fixed_only=False)
    with pytest.raises(OSError):
        await collector.on_epoch(epoch(0))
    assert collector.status.state == "aborted" and collector.status.reason == "save failed"
    await collector.on_epoch(epoch(1))  # no retry with n_epochs = target + 1
    assert collector.status.accepted == 1


async def test_run_survives_errors_and_the_next_start_works(env, monkeypatch) -> None:
    _, bus, db = env
    collector = PointCollector(bus, StateStore(bus), FailingPoints(db), SessionsRepo(db))
    progress = bus.subscribe("points.progress")
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(stop))
    await collector.start("x", epochs=1, fixed_only=False)
    bus.publish("state.epoch", epoch(0))
    while (await asyncio.wait_for(progress.queue.get(), 2.0))[1].state != "aborted":
        pass
    assert collector.status.reason == "save failed" and not task.done()

    def boom(state: ReceiverState) -> bool:
        raise ValueError("bad epoch")

    monkeypatch.setattr(collector, "_usable", boom)
    await collector.start("y", epochs=1, fixed_only=False)
    bus.publish("state.epoch", epoch(1))
    while (await asyncio.wait_for(progress.queue.get(), 2.0))[1].state != "aborted":
        pass
    assert collector.status.reason == "internal error" and not task.done()
    monkeypatch.undo()
    await collector.start("z", epochs=1, fixed_only=False)
    assert collector.status.state == "collecting"
    stop.set()
    await asyncio.wait_for(task, 1.0)


async def test_cancel_while_saving_is_too_late(env) -> None:
    _, bus, db = env
    points = BlockingPoints(db)
    collector = PointCollector(bus, StateStore(bus), points, SessionsRepo(db))
    await collector.start("late", epochs=1, fixed_only=False)
    saving = asyncio.create_task(collector.on_epoch(epoch(0)))
    await asyncio.wait_for(points.entered.wait(), 1.0)
    collector.cancel()
    points.release.set()
    await asyncio.wait_for(saving, 1.0)
    assert collector.status.state == "done" and collector.status.reason is None
    assert [p.name for p in await PointsRepo(db).list()] == ["late"]


async def test_sessions_stop_without_open_session_and_get(env) -> None:
    _, _, db = env
    repo = SessionsRepo(db)
    assert await repo.stop() is None
    s = await repo.start("g", "rover", notes="n")
    got = await repo.get(s.id)
    assert got is not None and got.name == "g" and got.notes == "n" and got.role == "rover"
    assert await repo.get(999) is None


async def test_points_repo_list_filters_by_session_and_limits(env) -> None:
    collector, _, db = env
    sessions, repo = SessionsRepo(db), PointsRepo(db)
    s1 = await sessions.start("s1", "rover")
    for i, name in enumerate(["a", "b"]):
        await collector.start(name, epochs=1)
        await collector.on_epoch(epoch(i))
    await sessions.start("s2", "rover")
    await collector.start("c", epochs=1)
    await collector.on_epoch(epoch(5))
    assert [p.name for p in await repo.list(session_id=s1.id)] == ["b", "a"]
    assert [p.name for p in await repo.list(limit=1)] == ["c"]
    assert [p.name for p in await repo.list(session_id=s1.id, limit=1)] == ["b"]
    with pytest.raises(KeyError):
        await repo.update(999, name="x")


@pytest.mark.parametrize("value", ["0", "3601"])
def test_point_epochs_bounds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str) -> None:
    env_file = tmp_path / "empty.env"
    env_file.write_text("")
    monkeypatch.setenv("MTRTK_ENV_FILE", str(env_file))
    monkeypatch.setenv("ROLE", "rover")
    monkeypatch.setenv("POINT_EPOCHS", value)
    with pytest.raises(ValidationError, match="point_epochs"):
        Settings(_env_file=None)
