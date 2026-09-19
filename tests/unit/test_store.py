import asyncio
import sqlite3
import threading
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from mtrtk.rawlog.retention import RetentionPolicy
from mtrtk.rawlog.writer import Sidecar, log_path, sidecar_path
from mtrtk.store import db as store_db
from mtrtk.store.db import Database
from mtrtk.store.models import Site, SystemStats
from mtrtk.store.repos import EventsRepo, LogFilesRepo, NtripLogRepo, SitesRepo

Usage = namedtuple("Usage", "total used free")


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "mtrtk.db")
    await database.open()
    try:
        yield database
    finally:
        await database.close()


async def test_open_creates_schema_and_is_idempotent(tmp_path: Path) -> None:
    db1 = Database(tmp_path / "m.db")
    await db1.open()
    tables = {
        r["name"] for r in await db1.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "samples_1s",
        "samples_1m",
        "sites",
        "sessions",
        "points",
        "events",
        "log_files",
        "ntrip_clients_log",
        "jobs",
    } <= tables
    assert db1.user_version == 2
    mode = (await db1.fetchone("PRAGMA journal_mode"))[0]
    assert mode == "wal"
    await db1.close()
    db2 = Database(tmp_path / "m.db")
    await db2.open()
    assert db2.user_version == 2
    await db2.close()


def test_site_from_ecef_fills_llh() -> None:
    site = Site.from_ecef(
        "roof", 1234567.8912, 5000000.0, 3000000.0, sigma_m=0.005, source="csrs-ppp"
    )
    assert site.lat is not None and site.lon is not None and site.height_m is not None
    assert site.sigma_x == site.sigma_y == site.sigma_z == 0.005
    assert site.frame == "ITRF2020" and site.active is False


async def test_sites_repo_crud_and_single_active(db: Database) -> None:
    repo = SitesRepo(db)
    a = await repo.add(Site.from_ecef("a", 1.0, 2.0, 3.0, source="survey-in"))
    b = await repo.add(Site.from_ecef("b", 4.0, 5.0, 6.0, source="manual"))
    assert a.id is not None and b.id is not None
    assert [s.name for s in await repo.list()] == ["a", "b"]
    with pytest.raises(ValueError, match="exists"):
        await repo.add(Site.from_ecef("a", 0, 0, 0, source="manual"))
    await repo.activate("b")
    assert (await repo.active()).name == "b"
    await repo.activate("a")
    active = [s.name for s in await repo.list() if s.active]
    assert active == ["a"]
    with pytest.raises(KeyError):
        await repo.activate("zzz")
    await repo.delete("b")
    assert await repo.get("b") is None
    assert (await repo.get("a")).x == 1.0


async def test_delete_refuses_the_active_site(db: Database) -> None:
    """The base is broadcasting that ARP: dropping the row would leave nothing to name it.

    The guard lives here rather than in the CLI so every caller - `mtrtk sites delete`, the API,
    a future job - is held to it, and so the check and the DELETE are one transaction.
    """
    repo = SitesRepo(db)
    await repo.add(Site.from_ecef("a", 1.0, 2.0, 3.0, source="manual"))
    await repo.add(Site.from_ecef("b", 4.0, 5.0, 6.0, source="manual"))
    await repo.activate("a")
    with pytest.raises(ValueError, match="a is the active site"):
        await repo.delete("a")
    assert await repo.get("a") is not None
    await repo.delete("b")  # an inactive row still goes
    await repo.delete("zzz")  # and an unknown name is still a no-op, not an error
    assert [s.name for s in await repo.list()] == ["a"]


async def test_events_repo(db: Database) -> None:
    repo = EventsRepo(db)
    e1 = await repo.add("warning", "jamming", "jam_ind 210", {"jam_ind": 210})
    await repo.add("info", "survey_in_valid", "survey-in complete")
    assert e1.id is not None and e1.meta == {"jam_ind": 210}
    assert [e.kind for e in await repo.list()] == ["survey_in_valid", "jamming"]  # newest first
    assert [e.kind for e in await repo.list(level="warning")] == ["jamming"]
    await repo.ack(e1.id)
    assert (await repo.list(level="warning"))[0].acked is True


async def test_ntrip_log_repo(db: Database) -> None:
    repo = NtripLogRepo(db)
    row = await repo.connected("100.100.50.12", "MTRK", "NTRIP SWMaps/1.0", "rover")
    await repo.disconnected(
        row, bytes_sent=12345, last_lat=23.8, last_lon=90.2, reason="client closed"
    )
    recent = await repo.recent()
    assert len(recent) == 1
    assert recent[0].bytes_sent == 12345 and recent[0].last_lat == 23.8
    assert recent[0].disconnected_utc is not None


async def test_log_files_repo(db: Database, tmp_path: Path) -> None:
    repo = LogFilesRepo(db)
    sc = Sidecar(
        "MTRK",
        "base",
        "2026-09-18T16:00:00+00:00",
        hour_utc="2026-09-18T16:00:00+00:00",
        bytes=10,
        msg_counts={"RXM-RAWX": 2},
        complete=True,
    )
    path = tmp_path / "MTRK_20260918_16.ubx"
    await repo.upsert(path, sc)
    sc.bytes = 20
    await repo.upsert(path, sc)
    rows = await repo.list()
    assert len(rows) == 1 and rows[0]["bytes"] == 20 and rows[0]["keep"] == 0
    await repo.set_keep(path, True)
    assert (await repo.list())[0]["keep"] == 1
    await repo.delete(path)
    assert await repo.list() == []


def test_system_stats_model_defaults() -> None:
    s = SystemStats(cpu_pct=1.0, mem_pct=2.0, disk_free_gb=3.0, disk_used_pct=4.0, uptime_s=5.0)
    assert s.temp_c is None and s.load1 is None


async def test_activate_is_atomic_under_concurrency(db: Database) -> None:
    repo = SitesRepo(db)
    await repo.add(Site.from_ecef("a", 1.0, 2.0, 3.0, source="manual"))
    await repo.add(Site.from_ecef("b", 4.0, 5.0, 6.0, source="manual"))
    await asyncio.gather(repo.activate("a"), repo.activate("b"))
    assert [s.name for s in await repo.list() if s.active] in (["a"], ["b"])


async def test_add_is_atomic_under_concurrency(db: Database) -> None:
    repo = SitesRepo(db)
    results = await asyncio.gather(
        repo.add(Site.from_ecef("a", 1.0, 2.0, 3.0, source="manual")),
        repo.add(Site.from_ecef("a", 4.0, 5.0, 6.0, source="manual")),
        return_exceptions=True,
    )
    assert len(await repo.list()) == 1
    assert sum(isinstance(r, Site) for r in results) == 1
    assert [type(r) for r in results].count(ValueError) == 1


async def test_concurrent_writes_never_observe_zero_active_sites(db: Database) -> None:
    sites, events = SitesRepo(db), EventsRepo(db)
    await sites.add(Site.from_ecef("a", 1.0, 2.0, 3.0, source="manual"))
    await sites.add(Site.from_ecef("b", 4.0, 5.0, 6.0, source="manual"))
    await sites.activate("a")
    observed: list[int] = []

    async def flipper() -> None:
        for name in ["b", "a"] * 8:
            await sites.activate(name)

    async def watcher() -> None:
        for i in range(40):
            await events.add("info", "tick", f"tick {i}")
            row = await db.fetchone("SELECT COUNT(*) AS n FROM sites WHERE active = 1")
            assert row is not None
            observed.append(row["n"])

    await asyncio.gather(flipper(), watcher())
    assert set(observed) == {1}


async def test_set_keep_marks_the_sidecar_so_retention_skips_the_file(
    db: Database, tmp_path: Path
) -> None:
    repo = LogFilesRepo(db)
    hour = datetime(2026, 9, 18, 16, tzinfo=UTC)
    path = log_path(tmp_path, "MTRK", hour)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xb5" * 100)
    sc = Sidecar("MTRK", "base", hour.isoformat(), hour_utc=hour.isoformat(), bytes=100)
    sc.dump(sidecar_path(path))
    await repo.upsert(path, sc)
    newer = log_path(tmp_path, "MTRK", hour + timedelta(hours=1))  # the newest hour is never cut
    newer.write_bytes(b"\xb5" * 100)

    await repo.set_keep(path, True)

    assert Sidecar.load(sidecar_path(path)).keep is True
    assert (await repo.list())[0]["keep"] == 1
    policy = RetentionPolicy(tmp_path, 5.0, disk_usage=lambda p: Usage(10e9, 9e9, 1e9))
    assert policy.prune_once() == []
    assert path.exists()


async def test_set_keep_writes_the_sidecar_even_with_no_row_to_update(
    db: Database, tmp_path: Path
) -> None:
    """The flag is the sidecar's; the row is a mirror, and `set_keep` is an UPDATE.

    An hour written before this daemon started has no row yet, so anything that wants the
    database to agree - `PATCH /api/logs/{name}` does - has to upsert one first.
    """
    repo = LogFilesRepo(db)
    hour = datetime(2026, 9, 18, 16, tzinfo=UTC)
    path = log_path(tmp_path, "MTRK", hour)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xb5" * 10)
    Sidecar("MTRK", "base", hour.isoformat(), hour_utc=hour.isoformat(), bytes=10).dump(
        sidecar_path(path)
    )

    await repo.set_keep(path, True)

    assert Sidecar.load(sidecar_path(path)).keep is True
    assert await repo.list() == []  # no row was invented, and none was updated


async def test_upsert_refreshes_role_and_site(db: Database, tmp_path: Path) -> None:
    repo = LogFilesRepo(db)
    sc = Sidecar("MTRK", "base", "2026-09-18T16:00:00+00:00", hour_utc="2026-09-18T16:00:00+00:00")
    path = tmp_path / "MTRK_20260918_16.ubx"
    await repo.upsert(path, sc)
    assert (await repo.list())[0]["site"] is None
    sc.site, sc.role = "roof", "rover"
    await repo.upsert(path, sc)
    row = (await repo.list())[0]
    assert row["site"] == "roof" and row["role"] == "rover"


async def test_a_v1_database_is_upgraded_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upgrade path 002 exists for: a card written by a daemon that only had 001."""
    first = [m for m in store_db._migrations() if m[0] == 1]
    monkeypatch.setattr(store_db, "_migrations", lambda: first)
    old = Database(tmp_path / "m.db")
    await old.open()
    assert old.user_version == 1
    await old.execute(
        "INSERT INTO jobs (id, kind, status, created_utc, progress) VALUES (?,?,?,?,?)",
        ("keepme", "export", "done", "2026-09-18T00:00:00+00:00", 1.0),
    )
    await old.commit()
    await old.close()

    monkeypatch.undo()
    upgraded = Database(tmp_path / "m.db")
    await upgraded.open()
    try:
        assert upgraded.user_version == 2
        row = await upgraded.fetchone("SELECT id, message FROM jobs WHERE id = 'keepme'")
        assert row is not None and row["message"] is None  # the row survived; the column is new
        indexes = {
            r["name"]
            for r in await upgraded.fetchall("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert "events_level_id" in indexes
    finally:
        await upgraded.close()


async def test_failed_migration_rolls_back_and_keeps_the_previous_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = (resources.files("mtrtk.store.schema") / "001_init.sql").read_text(encoding="utf-8")
    bad = "CREATE TABLE half_applied (id INTEGER);\nCREATE TABLE oops (;\n"
    monkeypatch.setattr(store_db, "_migrations", lambda: [(1, good), (2, bad)])
    database = Database(tmp_path / "m.db")
    with pytest.raises(sqlite3.OperationalError):
        await database.open()
    # the failed open closed what it had opened, so the file is inspected through a new one
    monkeypatch.setattr(store_db, "_migrations", lambda: [(1, good)])
    database = Database(tmp_path / "m.db")
    await database.open()
    try:
        row = await database.fetchone("PRAGMA user_version")
        assert row is not None and row[0] == 1
        assert (
            await database.fetchone("SELECT name FROM sqlite_master WHERE name = 'half_applied'")
        ) is None
        await database.execute(
            "INSERT INTO events (ts_utc, level, kind, message) VALUES (?,?,?,?)",
            ("2026-09-18T16:00:00+00:00", "info", "k", "m"),
        )
        await database.commit()
        assert len(await database.fetchall("SELECT id FROM events")) == 1
    finally:
        await database.close()


async def test_transaction_refuses_to_nest(db: Database) -> None:
    async with db.transaction():
        with pytest.raises(RuntimeError, match="already open"):
            async with db.transaction():
                pass
    assert db.user_version == 2  # the outer unit still commits cleanly


async def test_failed_begin_leaves_the_task_free_to_open_the_next_transaction(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BEGIN that fails must not pin the task: that would disable serialising for it."""
    real = db.conn.execute
    failed = False

    async def flaky(sql: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal failed
        if sql == "BEGIN IMMEDIATE" and not failed:
            failed = True
            raise sqlite3.OperationalError("database is locked")
        return await real(sql, *args, **kwargs)

    monkeypatch.setattr(db.conn, "execute", flaky)
    with pytest.raises(sqlite3.OperationalError):
        async with db.transaction():
            pass
    assert db._tx_task is None

    async with db.transaction():
        await db.execute(
            "INSERT INTO events (ts_utc, level, kind, message) VALUES (?,?,?,?)",
            ("2026-09-18T16:00:00+00:00", "info", "k", "m"),
        )
    assert len(await db.fetchall("SELECT id FROM events")) == 1


async def test_executemany_applies_the_whole_batch_or_none_of_it(db: Database) -> None:
    rows = [
        ("a", 1.0, 2.0, 3.0, "manual", "2026-09-18T16:00:00+00:00"),
        ("b", 4.0, 5.0, 6.0, "manual", "2026-09-18T16:00:00+00:00"),
        ("a", 7.0, 8.0, 9.0, "manual", "2026-09-18T16:00:00+00:00"),  # UNIQUE(name) violation
    ]
    with pytest.raises(sqlite3.IntegrityError):
        await db.executemany(
            "INSERT INTO sites (name, x, y, z, source, created_utc) VALUES (?,?,?,?,?,?)", rows
        )
    assert await db.fetchall("SELECT id FROM sites") == []


async def test_commit_inside_the_owning_transaction_does_not_end_it(db: Database) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with db.transaction():
            await db.execute(
                "INSERT INTO events (ts_utc, level, kind, message) VALUES (?,?,?,?)",
                ("2026-09-18T16:00:00+00:00", "info", "k", "m"),
            )
            await db.commit()  # a repo's trailing commit must not end the caller's unit
            raise RuntimeError("boom")
    assert await db.fetchall("SELECT id FROM events") == []


async def test_open_twice_keeps_the_same_connection(tmp_path: Path) -> None:
    database = Database(tmp_path / "m.db")
    await database.open()
    first = database.conn
    await database.open()  # a second open used to leak the first connection and its thread
    assert database.conn is first
    await database.close()


async def test_a_failed_open_leaves_nothing_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_db, "_migrations", lambda: [(1, "CREATE TABLE oops (;\n")])
    threads = threading.active_count()
    database = Database(tmp_path / "m.db")
    with pytest.raises(sqlite3.OperationalError):
        await database.open()
    with pytest.raises(RuntimeError, match="not open"):
        _ = database.conn
    assert threading.active_count() == threads  # the connection was closed, not just dropped
    await database.close()  # still safe to call


async def test_event_meta_survives_values_json_cannot_encode(db: Database) -> None:
    repo = EventsRepo(db)
    event = await repo.add("warning", "logger_error", "boom", {"path": Path("/data/x.ubx")})
    assert event.id is not None
    assert (await repo.list())[0].meta == {"path": "/data/x.ubx"}
