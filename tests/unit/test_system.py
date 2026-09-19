import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from mtrtk.core.bus import Bus
from mtrtk.system import SystemMonitor, read_temperature


def fake_psutil(temps: dict | None = None, boot: float = 1000.0):
    return SimpleNamespace(
        cpu_percent=lambda interval=None: 12.5,
        virtual_memory=lambda: SimpleNamespace(percent=43.0),
        disk_usage=lambda p: SimpleNamespace(total=100e9, used=60e9, free=40e9, percent=60.0),
        boot_time=lambda: boot,
        sensors_temperatures=lambda: temps if temps is not None else {},
    )


def test_read_temperature_prefers_cpu_thermal() -> None:
    temps = {
        "acpitz": [SimpleNamespace(label="", current=33.0)],
        "cpu_thermal": [SimpleNamespace(label="", current=51.2)],
    }
    assert read_temperature(fake_psutil(temps)) == 51.2
    assert (
        read_temperature(fake_psutil({"whatever": [SimpleNamespace(label="x", current=29.0)]}))
        == 29.0
    )
    assert read_temperature(fake_psutil({})) is None

    class NoSensors:
        pass

    assert read_temperature(NoSensors()) is None  # platforms without sensors_temperatures


def test_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mtrtk.system.time.time", lambda: 1360.0)
    mon = SystemMonitor(
        Bus(),
        tmp_path,
        psutil_module=fake_psutil({"cpu_thermal": [SimpleNamespace(label="", current=50.0)]}),
        loadavg=lambda: (0.5, 0.4, 0.3),
    )
    s = mon.snapshot()
    assert s.cpu_pct == 12.5 and s.mem_pct == 43.0
    assert s.disk_free_gb == 40.0 and s.disk_used_pct == 60.0
    assert s.uptime_s == 360.0 and s.temp_c == 50.0 and s.load1 == 0.5


def test_snapshot_uses_parent_when_data_dir_missing(tmp_path: Path) -> None:
    seen: list[Path] = []

    def disk_usage(p):
        seen.append(Path(p))
        return SimpleNamespace(total=1, used=0, free=1, percent=0.0)

    ps = fake_psutil()
    ps.disk_usage = disk_usage
    SystemMonitor(Bus(), tmp_path / "missing", psutil_module=ps).snapshot()
    assert seen == [tmp_path]


async def test_run_survives_a_psutil_failure(tmp_path: Path) -> None:
    """A sensor read that raises costs that one sample, never the monitor."""
    bus = Bus()
    sub = bus.subscribe("system.stats")
    ps = fake_psutil()
    calls = {"n": 0}

    def flaky_cpu(interval=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("/proc read failed")
        return 12.5

    ps.cpu_percent = flaky_cpu
    mon = SystemMonitor(bus, tmp_path, interval_s=0.01, psutil_module=ps)
    stop = asyncio.Event()
    task = asyncio.create_task(mon.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 1.0)
    assert calls["n"] >= 2
    assert sub.queue.qsize() >= 1  # the failed pass is skipped, later passes still publish


async def test_run_publishes_periodically(tmp_path: Path) -> None:
    bus = Bus()
    sub = bus.subscribe("system.stats")
    mon = SystemMonitor(bus, tmp_path, interval_s=0.01, psutil_module=fake_psutil())
    stop = asyncio.Event()
    task = asyncio.create_task(mon.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 1.0)
    assert sub.queue.qsize() >= 2
    assert (sub.queue.get_nowait()[1]).cpu_pct == 12.5


async def test_run_publishes_once_immediately_and_stops_mid_interval(tmp_path: Path) -> None:
    """The first sample lands before the first sleep, and `stop` need not wait it out."""
    bus = Bus()
    sub = bus.subscribe("system.stats")
    mon = SystemMonitor(bus, tmp_path, interval_s=600.0, psutil_module=fake_psutil())
    stop = asyncio.Event()
    task = asyncio.create_task(mon.run(stop))
    await asyncio.sleep(0)
    assert sub.queue.qsize() == 1
    stop.set()
    await asyncio.wait_for(task, 0.5)  # a 600 s interval must not delay shutdown
    assert sub.queue.qsize() == 1
