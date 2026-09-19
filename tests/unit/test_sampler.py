import asyncio
import logging
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from mtrtk.core.bus import Bus, Subscription
from mtrtk.core.state import Hardware, ReceiverState, Satellite
from mtrtk.store.db import Database
from mtrtk.store.models import SystemStats
from mtrtk.store.sampler import SAMPLE_COLUMNS, Sampler

T0 = datetime(2026, 9, 18, 16, 0, 0, tzinfo=UTC)


class Clock:
    """A hand-wound `time.monotonic` so the rate limiter is tested without sleeping."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class FlakyDatabase(Database):
    """Refuses the first `failures` sample inserts, the way a full or read-only disk would."""

    def __init__(self, path: Path, failures: int) -> None:
        super().__init__(path)
        self.failures = failures

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        if self.failures and sql.startswith("INSERT OR REPLACE INTO samples_1s"):
            self.failures -= 1
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return await super().execute(sql, params)


def drain(sub: Subscription) -> list[str]:
    topics = []
    while not sub.queue.empty():
        topics.append(sub.queue.get_nowait()[0])
    return topics


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "m.db")
    await database.open()
    try:
        yield database
    finally:
        await database.close()


def state_at(
    t: datetime, h_acc: float = 1.0, fix: int = 3, cno: tuple[int, ...] = (40, 30)
) -> ReceiverState:
    s = ReceiverState()
    s.time.utc = t
    s.position.lat, s.position.lon, s.position.height_m, s.position.hmsl_m = 23.8, 90.2, -36.2, 13.3
    s.accuracy.h_acc_m, s.accuracy.v_acc_m = h_acc, h_acc * 1.5
    s.fix.fix_type, s.fix.carr_soln = fix, 0
    s.dops.p, s.dops.h, s.dops.v = 1.2, 0.8, 0.9
    s.sats = [
        Satellite(gnss_id=0, gnss="GPS", sv_id=i + 1, cno=c, used=True) for i, c in enumerate(cno)
    ]
    s.sats.append(Satellite(gnss_id=6, gnss="GLONASS", sv_id=9, cno=0, used=False))
    s.sat_summary.tracked, s.sat_summary.used = len(s.sats), len(cno)
    s.hardware = Hardware(jam_ind=12, agc_cnt=3000, noise_per_ms=90)
    s.rtcm_out.bytes_per_s = 1900.0
    return s


def test_sample_row_maps_state() -> None:
    sampler = Sampler(Bus(), None)  # type: ignore[arg-type]
    sys_stats = SystemStats(
        cpu_pct=10.0,
        mem_pct=20.0,
        disk_free_gb=30.0,
        disk_used_pct=40.0,
        uptime_s=50.0,
        temp_c=55.0,
    )
    row = sampler.sample_row(state_at(T0), sys_stats, ntrip_clients=2)
    assert row is not None
    assert row["ts"] == T0.timestamp() and row["lat"] == 23.8 and row["h_acc_m"] == 1.0
    assert row["nsat_used"] == 2 and row["nsat_tracked"] == 3
    assert row["cno_mean"] == 35.0  # only used satellites with cno > 0
    assert row["jam_ind"] == 12 and row["rtcm_bytes_per_s"] == 1900.0 and row["ntrip_clients"] == 2
    assert row["cpu_pct"] == 10.0 and row["temp_c"] == 55.0
    assert set(row) == set(SAMPLE_COLUMNS)


def test_sample_row_none_without_receiver_time() -> None:
    assert Sampler(Bus(), None).sample_row(ReceiverState(), None, 0) is None  # type: ignore[arg-type]


async def test_sample_columns_match_the_schema(db: Database) -> None:
    rows = await db.fetchall("PRAGMA table_info(samples_1s)")
    assert tuple(r["name"] for r in rows) == SAMPLE_COLUMNS


async def test_insert_and_history(db: Database) -> None:
    sampler = Sampler(Bus(), db)
    for i in range(3):
        row = sampler.sample_row(state_at(T0 + timedelta(seconds=i), h_acc=1.0 + i), None, 0)
        assert row is not None
        await sampler.insert(row)
    rows = await sampler.history(
        "samples_1s", T0.timestamp(), (T0 + timedelta(seconds=10)).timestamp(), ["ts", "h_acc_m"]
    )
    assert [r["h_acc_m"] for r in rows] == [1.0, 2.0, 3.0]
    await sampler.insert(sampler.sample_row(state_at(T0, h_acc=9.0), None, 0))  # same ts replaces
    rows = await sampler.history(
        "samples_1s", T0.timestamp(), (T0 + timedelta(seconds=1)).timestamp(), ["h_acc_m"]
    )
    assert [r["h_acc_m"] for r in rows] == [9.0]


async def test_history_rejects_unknown_tables_and_columns(db: Database) -> None:
    sampler = Sampler(Bus(), db)
    with pytest.raises(ValueError, match="unknown table"):
        await sampler.history("sites; DROP TABLE samples_1s", 0, 4e9, ["ts"])
    with pytest.raises(ValueError, match="unknown column"):
        await sampler.history("samples_1s", 0, 4e9, ["ts, (SELECT name FROM sites)"])
    with pytest.raises(ValueError, match="unknown column"):
        await sampler.history("samples_1m", 0, 4e9, ["h_acc_m"])  # a 1 s column, not a rollup one


async def test_rollup_minute_aggregates(db: Database) -> None:
    sampler = Sampler(Bus(), db)
    for i in range(60):
        await sampler.insert(
            sampler.sample_row(
                state_at(T0 + timedelta(seconds=i), h_acc=1.0 + i % 2, fix=3 if i else 2),
                None,
                i % 3,
            )
        )
    await sampler.rollup_minute(T0.timestamp())
    row = (
        await sampler.history(
            "samples_1m",
            T0.timestamp(),
            T0.timestamp() + 60,
            ["n", "h_acc_avg", "h_acc_max", "fix_type_min", "ntrip_clients_max", "nsat_used_min"],
        )
    )[0]
    assert row["n"] == 60 and row["h_acc_avg"] == 1.5 and row["h_acc_max"] == 2.0
    assert row["fix_type_min"] == 2 and row["ntrip_clients_max"] == 2 and row["nsat_used_min"] == 2


async def test_rollup_of_an_empty_minute_writes_nothing(db: Database) -> None:
    sampler = Sampler(Bus(), db)
    await sampler.rollup_minute(T0.timestamp())
    assert await sampler.history("samples_1m", 0, 4e9, ["ts"]) == []


async def test_prune(db: Database) -> None:
    sampler = Sampler(Bus(), db, keep_1s_h=1, keep_1m_d=1)
    old = T0 - timedelta(hours=2)
    await sampler.insert(sampler.sample_row(state_at(old), None, 0))
    await sampler.insert(sampler.sample_row(state_at(T0), None, 0))
    await sampler.rollup_minute(old.timestamp())
    await sampler.rollup_minute(T0.timestamp())
    await db.execute(
        "UPDATE samples_1m SET ts = ? WHERE ts = ?",
        ((T0 - timedelta(days=2)).timestamp(), old.timestamp()),
    )
    await sampler.prune(now_ts=T0.timestamp() + 1)
    assert len(await sampler.history("samples_1s", 0, 4e9, ["ts"])) == 1
    assert len(await sampler.history("samples_1m", 0, 4e9, ["ts"])) == 1


async def test_run_consumes_bus_and_rolls_up_on_minute_change(db: Database) -> None:
    bus = Bus()
    sampler = Sampler(bus, db)
    stop = asyncio.Event()
    task = asyncio.create_task(sampler.run(stop))
    bus.publish(
        "system.stats",
        SystemStats(cpu_pct=1, mem_pct=2, disk_free_gb=3, disk_used_pct=4, uptime_s=5),
    )
    bus.publish("ntrip.clients", [object(), object()])
    for i in range(3):
        bus.publish("state.epoch", state_at(T0 + timedelta(seconds=58 + i)))  # crosses 16:01:00
    await asyncio.sleep(0.1)
    sampler.stop()
    await asyncio.wait_for(task, 2.0)
    rows = await sampler.history("samples_1s", 0, 4e9, ["ts", "ntrip_clients", "cpu_pct"])
    assert len(rows) == 3 and rows[0]["ntrip_clients"] == 2 and rows[0]["cpu_pct"] == 1.0
    minutes = [r["ts"] for r in await sampler.history("samples_1m", 0, 4e9, ["ts"])]
    assert minutes == [T0.timestamp(), T0.timestamp() + 60]  # 16:00 on the change, 16:01 at stop


async def test_run_stops_on_the_stop_event_without_further_traffic(db: Database) -> None:
    bus = Bus()
    sampler = Sampler(bus, db)
    stop = asyncio.Event()
    task = asyncio.create_task(sampler.run(stop))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, 2.0)
    assert bus.subscriber_count == 0


async def test_run_survives_a_write_error(db: Database) -> None:
    bus = Bus()
    sampler = Sampler(bus, db)
    task = asyncio.create_task(sampler.run(asyncio.Event()))
    bus.publish("state.epoch", state_at(T0))  # a minute is now in progress
    await asyncio.sleep(0.05)
    await db.execute("DROP TABLE samples_1s")
    bus.publish("state.epoch", state_at(T0 + timedelta(seconds=1)))
    await asyncio.sleep(0.05)
    assert not task.done()
    sampler.stop()
    await asyncio.wait_for(task, 2.0)  # the rollup of the final minute fails without raising


async def test_rows_key_on_the_whole_receiver_second(db: Database) -> None:
    """NAV-PVT dates a fix to the nanosecond; a history row is one per receiver second."""
    sampler = Sampler(Bus(), db)
    for us, h_acc in ((-17, 1.0), (200_000, 2.0), (999_983, 3.0)):
        await sampler.insert(
            sampler.sample_row(state_at(T0 + timedelta(microseconds=us), h_acc=h_acc), None, 0)
        )
    rows = await sampler.history("samples_1s", 0, 4e9, ["ts", "h_acc_m"])
    assert [r["ts"] for r in rows] == [T0.timestamp(), (T0 + timedelta(seconds=1)).timestamp()]
    assert [r["h_acc_m"] for r in rows] == [2.0, 3.0]  # the last epoch of a second wins


async def test_write_failures_log_once_then_rate_limit_and_announce_recovery(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database = FlakyDatabase(tmp_path / "m.db", failures=5)
    await database.open()
    bus = Bus()
    signals = bus.subscribe("sampler.error", "sampler.recovered")
    clock = Clock()
    sampler = Sampler(bus, database, clock=clock)
    task = asyncio.create_task(sampler.run(asyncio.Event()))
    caplog.set_level(logging.INFO, logger="mtrtk.store.sampler")
    try:
        for i in range(3):  # the outage starts: one traceback, then silence
            bus.publish("state.epoch", state_at(T0 + timedelta(seconds=i)))
        await asyncio.sleep(0.05)
        assert [r.levelname for r in caplog.records] == ["ERROR"]
        assert drain(signals) == ["sampler.error"]

        clock.t = 61.0  # a minute on: one warning carrying the suppressed count
        for i in range(3, 5):
            bus.publish("state.epoch", state_at(T0 + timedelta(seconds=i)))
        await asyncio.sleep(0.05)
        assert [r.levelname for r in caplog.records] == ["ERROR", "WARNING"]
        assert "3 suppressed" in caplog.records[-1].getMessage()
        assert drain(signals) == []

        bus.publish("state.epoch", state_at(T0 + timedelta(seconds=5)))  # the disk comes back
        await asyncio.sleep(0.05)
        assert drain(signals) == ["sampler.recovered"]
        assert len(await sampler.history("samples_1s", 0, 4e9, ["ts"])) == 1
    finally:
        sampler.stop()
        await asyncio.wait_for(task, 2.0)
        await database.close()


async def test_the_minute_in_progress_is_rolled_up_at_shutdown(db: Database) -> None:
    bus = Bus()
    sampler = Sampler(bus, db)
    task = asyncio.create_task(sampler.run(asyncio.Event()))
    for i in range(3):
        bus.publish("state.epoch", state_at(T0 + timedelta(seconds=i)))
    await asyncio.sleep(0.05)
    sampler.stop()
    await asyncio.wait_for(task, 2.0)
    rows = await sampler.history("samples_1m", T0.timestamp(), T0.timestamp() + 60, ["ts", "n"])
    assert [r["n"] for r in rows] == [3]  # the unfinished minute leaves no hole in the 1 m series


async def test_a_bad_payload_is_skipped_and_the_loop_keeps_recording(db: Database) -> None:
    bus = Bus()
    sampler = Sampler(bus, db)
    task = asyncio.create_task(sampler.run(asyncio.Event()))
    bus.publish("ntrip.clients", 3)  # not a list: len() raises
    bus.publish("state.epoch", state_at(T0))
    await asyncio.sleep(0.05)
    assert not task.done()
    sampler.stop()
    await asyncio.wait_for(task, 2.0)
    assert len(await sampler.history("samples_1s", 0, 4e9, ["ts"])) == 1
