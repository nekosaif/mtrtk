import asyncio
import threading
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mtrtk.core.bus import Bus
from mtrtk.rawlog.index import files_for_window, hour_availability, list_logs, parse_log_name
from mtrtk.rawlog.retention import RetentionPolicy
from mtrtk.rawlog.writer import Sidecar, log_path, sidecar_path

Usage = namedtuple("Usage", "total used free")
H0 = datetime(2026, 9, 18, 10, tzinfo=UTC)


def make_log(
    root: Path, hour: datetime, size: int = 1000, keep: bool = False, sidecar: bool = True
) -> Path:
    path = log_path(root, "MTRK", hour)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xb5" * size)
    if sidecar:
        Sidecar(
            "MTRK",
            "base",
            hour.isoformat(),
            end_utc=(hour + timedelta(hours=1)).isoformat(),
            hour_utc=hour.isoformat(),
            bytes=size,
            keep=keep,
            complete=True,
            msg_counts={"RXM-RAWX": 3600},
        ).dump(sidecar_path(path))
    return path


def test_parse_log_name() -> None:
    assert parse_log_name(Path("/x/MTRK_20260918_16.ubx")) == (
        "MTRK",
        datetime(2026, 9, 18, 16, tzinfo=UTC),
    )
    assert parse_log_name(Path("/x/notes.txt")) is None
    assert parse_log_name(Path("/x/MTRK_2026091_16.ubx")) is None


def test_list_logs_sorted_with_and_without_sidecar(tmp_path: Path) -> None:
    make_log(tmp_path, H0 + timedelta(hours=2))
    make_log(tmp_path, H0, keep=True)
    make_log(tmp_path, H0 + timedelta(hours=1), size=5, sidecar=False)
    logs = list_logs(tmp_path)
    assert [lf.hour_utc for lf in logs] == [H0, H0 + timedelta(hours=1), H0 + timedelta(hours=2)]
    assert logs[0].keep is True and logs[0].msg_counts == {"RXM-RAWX": 3600}
    assert logs[1].bytes == 5 and logs[1].complete is False and logs[1].msg_counts == {}
    assert logs[0].hour_end == H0 + timedelta(hours=1)


def test_files_for_window_selects_overlapping_hours(tmp_path: Path) -> None:
    for i in range(5):
        make_log(tmp_path, H0 + timedelta(hours=i))
    sel = files_for_window(
        tmp_path, H0 + timedelta(hours=1, minutes=30), H0 + timedelta(hours=3, minutes=10)
    )
    assert [lf.hour_utc for lf in sel] == [
        H0 + timedelta(hours=1),
        H0 + timedelta(hours=2),
        H0 + timedelta(hours=3),
    ]
    assert files_for_window(tmp_path, H0 + timedelta(hours=1), H0 + timedelta(hours=2)) == [
        list_logs(tmp_path)[1]
    ]


def test_hour_availability_marks_gaps(tmp_path: Path) -> None:
    make_log(tmp_path, H0)
    make_log(tmp_path, H0 + timedelta(hours=2))
    slots = hour_availability(tmp_path, H0, H0 + timedelta(hours=3))
    assert [s.hour_utc for s in slots] == [H0, H0 + timedelta(hours=1), H0 + timedelta(hours=2)]
    assert [s.file is not None for s in slots] == [True, False, True]


def test_prune_deletes_oldest_unkept_until_free(tmp_path: Path) -> None:
    for i in range(4):
        make_log(tmp_path, H0 + timedelta(hours=i), keep=(i == 1))
    free = iter([1.0e9, 1.0e9, 6.0e9, 6.0e9, 6.0e9])
    policy = RetentionPolicy(
        tmp_path, min_free_gb=5.0, disk_usage=lambda p: Usage(10e9, 5e9, next(free))
    )
    bus = Bus()
    pruned_sub = bus.subscribe("rawlog.pruned")
    policy.bus = bus
    deleted = policy.prune_once()
    assert deleted == [log_path(tmp_path, "MTRK", H0)]  # hour 0 gone; hour 1 kept; free now ok
    assert (
        not log_path(tmp_path, "MTRK", H0).exists()
        and not sidecar_path(log_path(tmp_path, "MTRK", H0)).exists()
    )
    assert log_path(tmp_path, "MTRK", H0 + timedelta(hours=1)).exists()
    assert pruned_sub.queue.qsize() == 1


def test_prune_never_deletes_kept_or_newest(tmp_path: Path) -> None:
    make_log(tmp_path, H0, keep=True)
    make_log(tmp_path, H0 + timedelta(hours=1))  # newest = probably being written
    policy = RetentionPolicy(tmp_path, min_free_gb=5.0, disk_usage=lambda p: Usage(10e9, 9e9, 1e9))
    assert policy.prune_once() == []
    assert len(list_logs(tmp_path)) == 2


def test_prune_removes_empty_day_directories(tmp_path: Path) -> None:
    old = make_log(tmp_path, H0 - timedelta(days=3))
    make_log(tmp_path, H0)
    calls = iter([1e9, 9e9])
    RetentionPolicy(tmp_path, 5.0, disk_usage=lambda p: Usage(10e9, 1e9, next(calls))).prune_once()
    assert not old.parent.exists()


async def test_run_prunes_on_interval(tmp_path: Path) -> None:
    make_log(tmp_path, H0)
    make_log(tmp_path, H0 + timedelta(hours=1))
    policy = RetentionPolicy(tmp_path, 5.0, disk_usage=lambda p: Usage(10e9, 9e9, 9e9))
    stop = asyncio.Event()
    task = asyncio.create_task(policy.run(stop, interval_s=0.01))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 1.0)
    assert policy.runs >= 1


class _ThreadStampBus(Bus):
    """Records the thread each publish ran on."""

    def __init__(self) -> None:
        super().__init__()
        self.publish_threads: list[threading.Thread] = []

    def publish(self, topic: str, item: object) -> None:
        self.publish_threads.append(threading.current_thread())
        super().publish(topic, item)


async def test_run_publishes_pruned_on_the_loop_thread(tmp_path: Path) -> None:
    """`rawlog.pruned` reaches subscribers from the loop thread. Waking a subscriber parked on
    an asyncio.Queue is a `call_soon`, so a pass running off the loop would corrupt that waiter
    (and, under debug mode, raise outright)."""
    asyncio.get_running_loop().set_debug(True)
    loop_thread = threading.current_thread()
    make_log(tmp_path, H0)
    make_log(tmp_path, H0 + timedelta(hours=1))
    frees = [1e9]
    bus = _ThreadStampBus()
    sub = bus.subscribe("rawlog.pruned")
    policy = RetentionPolicy(
        tmp_path,
        5.0,
        bus=bus,
        disk_usage=lambda p: Usage(10e9, 9e9, frees.pop(0) if frees else 9e9),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(policy.run(stop, interval_s=0.01))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 1.0)
    assert sub.queue.get_nowait() == ("rawlog.pruned", log_path(tmp_path, "MTRK", H0))
    assert bus.publish_threads == [loop_thread]


async def test_prune_scans_the_tree_once_and_off_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One listing per pass, built in a worker thread: a full rescan per deleted file blocks
    the caster and the state loop for as long as the directory walk takes."""
    from mtrtk.rawlog import retention as retention_mod

    for i in range(4):
        make_log(tmp_path, H0 + timedelta(hours=i))
    scans: list[threading.Thread] = []
    real = retention_mod.list_logs

    def spy(root: Path) -> list:
        scans.append(threading.current_thread())
        return real(root)

    monkeypatch.setattr(retention_mod, "list_logs", spy)
    frees = iter([1e9, 1e9, 1e9, 9e9])
    bus = Bus()
    pruned = bus.subscribe("rawlog.pruned")
    policy = RetentionPolicy(
        tmp_path, 5.0, bus=bus, disk_usage=lambda p: Usage(10e9, 1e9, next(frees))
    )
    deleted = await policy.prune()
    assert deleted == [
        log_path(tmp_path, "MTRK", H0),
        log_path(tmp_path, "MTRK", H0 + timedelta(hours=1)),
    ]
    assert len(scans) == 1 and scans[0] is not threading.current_thread()
    assert pruned.queue.qsize() == 2


# ------------------------------------------- derived RINEX goes with the raw hours it came from


async def test_prune_reclaims_the_exports_of_an_hour_before_the_hour_itself(
    tmp_path: Path,
) -> None:
    for i in range(3):
        make_log(tmp_path, H0 + timedelta(hours=i))
    asked: list[datetime] = []
    free = {"v": 1e9}

    async def reclaim(ended_by: datetime) -> None:
        asked.append(ended_by)
        if ended_by == H0 + timedelta(hours=2):
            free["v"] = 9e9  # that hour's export was big enough to clear the floor

    policy = RetentionPolicy(
        tmp_path, 5.0, disk_usage=lambda p: Usage(10e9, 1e9, free["v"]), reclaim=reclaim
    )
    deleted = await policy.prune()
    # Hour 0's exports go, then hour 0; hour 1's exports go and are enough - hour 1 stays.
    assert asked == [H0 + timedelta(hours=1), H0 + timedelta(hours=2)]
    assert deleted == [log_path(tmp_path, "MTRK", H0)]
    assert log_path(tmp_path, "MTRK", H0 + timedelta(hours=1)).exists()


async def test_prune_counts_an_export_in_progress_as_free(tmp_path: Path) -> None:
    """A temporary staging directory must not cost a week of the oldest raw history."""
    for i in range(3):
        make_log(tmp_path, H0 + timedelta(hours=i))
    stage = tmp_path / "jobs" / "abc" / ".export-x1"
    stage.mkdir(parents=True)
    (stage / "spliced.ubx").write_bytes(b"\0" * 3000)
    work = tmp_path / "tmp" / "export-y2" / "out"
    work.mkdir(parents=True)
    (work / "o.rnx").write_bytes(b"\0" * 1000)
    floor_gb = 5000 / 1e9
    disk = lambda p: Usage(10e9, 1e9, 2000)  # noqa: E731 - 2000 B free, 4000 B of it staging
    assert await RetentionPolicy(tmp_path, floor_gb, disk_usage=disk).prune() == []
    assert RetentionPolicy(tmp_path, floor_gb, disk_usage=disk).prune_once() == []
    # Without the staging the same card is below the floor and an hour goes.
    import shutil

    shutil.rmtree(tmp_path / "jobs")
    shutil.rmtree(tmp_path / "tmp")
    frees = iter([2000, 2000, 9e9])
    policy = RetentionPolicy(tmp_path, floor_gb, disk_usage=lambda p: Usage(10e9, 1e9, next(frees)))
    assert await policy.prune() == [log_path(tmp_path, "MTRK", H0)]


async def test_a_reclaim_that_fails_does_not_stop_raw_pruning(tmp_path: Path) -> None:
    """A database error while dropping an hour's exports must not leave the card filling up:
    the raw hour still goes, and the next hours are still looked at."""
    import sqlite3

    for i in range(3):
        make_log(tmp_path, H0 + timedelta(hours=i))
    frees = iter([1e9, 1e9, 1e9, 1e9, 9e9])

    async def reclaim(ended_by: datetime) -> None:
        raise sqlite3.OperationalError("database is locked")

    policy = RetentionPolicy(
        tmp_path, 5.0, disk_usage=lambda p: Usage(10e9, 1e9, next(frees)), reclaim=reclaim
    )
    deleted = await policy.prune()
    assert deleted == [
        log_path(tmp_path, "MTRK", H0),
        log_path(tmp_path, "MTRK", H0 + timedelta(hours=1)),
    ]
