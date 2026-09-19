import asyncio
import threading
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
