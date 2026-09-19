from pathlib import Path

import pytest

from mtrtk.rawlog.writer import Sidecar
from mtrtk.store.db import Database
from mtrtk.store.models import Site, SystemStats
from mtrtk.store.repos import EventsRepo, LogFilesRepo, NtripLogRepo, SitesRepo


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
    assert db1.user_version == 1
    mode = (await db1.fetchone("PRAGMA journal_mode"))[0]
    assert mode == "wal"
    await db1.close()
    db2 = Database(tmp_path / "m.db")
    await db2.open()
    assert db2.user_version == 1
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
