import asyncio
import logging
from pathlib import Path

import pytest

from mtrtk.alerts import AlertEngine
from mtrtk.core.bus import Bus
from mtrtk.core.state import FixInfo, Hardware, SurveyIn
from mtrtk.store.db import Database
from mtrtk.store.models import SystemStats
from mtrtk.store.repos import EventsRepo


class FakeHttp:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, json: dict, timeout: float) -> None:  # noqa: ASYNC109
        self.posts.append((url, json))


class Clock:
    """A hand-wound `time.monotonic` so grace periods are tested without sleeping."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
async def env(tmp_path: Path):
    db = Database(tmp_path / "m.db")
    await db.open()
    bus = Bus()
    http = FakeHttp()
    clock = Clock()
    engine = AlertEngine(
        bus,
        EventsRepo(db),
        role="base",
        host="pi",
        min_free_gb=5.0,
        webhook_url="https://ntfy.sh/x",
        http=http,
        clock=clock,
    )
    sub = bus.subscribe("events.new")
    try:
        yield engine, sub, http, clock, EventsRepo(db)
    finally:
        await db.close()


def kinds(sub) -> list[str]:
    return [item.kind for _, item in [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]]


async def test_disconnect_raises_once_and_clears_on_connect(env) -> None:
    engine, sub, http, _, repo = env
    await engine.handle("receiver.disconnected", "no data from receiver for 5s")
    await engine.handle("receiver.disconnected", "no data from receiver for 5s")
    assert kinds(sub) == ["receiver_disconnected"]
    assert "receiver_disconnected" in engine.active
    await engine.handle("receiver.connected", "serial:/dev/ttyACM0")
    assert kinds(sub) == ["receiver_disconnected_cleared"]
    assert engine.active == {}
    assert [e.level for e in await repo.list()] == ["info", "error"]
    assert http.posts[0][1] == {
        "level": "error",
        "kind": "receiver_disconnected",
        "message": "receiver disconnected: no data from receiver for 5s",
        "ts": http.posts[0][1]["ts"],
        "host": "pi",
        "role": "base",
    }


async def test_source_ended_is_not_an_alert(env) -> None:
    engine, sub, *_ = env
    await engine.handle("receiver.disconnected", "source ended")
    assert kinds(sub) == []


async def test_fix_lost_needs_prior_3d_and_grace(env) -> None:
    engine, sub, _, clock, _ = env
    await engine.handle("state.fix", FixInfo(fix_type=0))  # never had a fix: no alert
    await engine.handle("state.fix", FixInfo(fix_type=3))
    await engine.handle("state.fix", FixInfo(fix_type=2))
    clock.t += 5
    await engine.handle("state.fix", FixInfo(fix_type=2))
    assert kinds(sub) == []
    clock.t += 6
    await engine.handle("state.fix", FixInfo(fix_type=2))
    assert kinds(sub) == ["fix_lost"]
    await engine.handle("state.fix", FixInfo(fix_type=3))
    assert kinds(sub) == ["fix_lost_cleared"]


async def test_jamming_sustained_and_antenna_fault(env) -> None:
    engine, sub, _, clock, _ = env
    await engine.handle("state.hardware", Hardware(jam_ind=250, ant_status=2))
    clock.t += 31
    await engine.handle("state.hardware", Hardware(jam_ind=250, ant_status=2))
    assert kinds(sub) == ["jamming"]
    await engine.handle("state.hardware", Hardware(jam_ind=10, ant_status=4))
    assert sorted(kinds(sub)) == ["antenna_fault", "jamming_cleared"]
    assert engine.active["antenna_fault"].level == "error"
    await engine.handle("state.hardware", Hardware(jam_ind=10, ant_status=2))
    assert kinds(sub) == ["antenna_fault_cleared"]


async def test_disk_and_temperature_thresholds(env) -> None:
    engine, sub, *_ = env

    def stats(free: float, temp: float | None = 40.0) -> SystemStats:
        return SystemStats(
            cpu_pct=0, mem_pct=0, disk_free_gb=free, disk_used_pct=0, uptime_s=0, temp_c=temp
        )

    await engine.handle("system.stats", stats(20.0))
    assert kinds(sub) == []
    await engine.handle("system.stats", stats(6.0))
    assert kinds(sub) == ["disk_warning"]
    await engine.handle("system.stats", stats(4.0))
    assert kinds(sub) == ["disk_low"]
    await engine.handle("system.stats", stats(9.0))
    assert sorted(kinds(sub)) == ["disk_low_cleared", "disk_warning_cleared"]
    await engine.handle("system.stats", stats(20.0, temp=85.0))
    assert kinds(sub) == ["temperature_high"]
    await engine.handle("system.stats", stats(20.0, temp=75.0))
    assert kinds(sub) == []  # hysteresis
    await engine.handle("system.stats", stats(20.0, temp=60.0))
    assert kinds(sub) == ["temperature_high_cleared"]


async def test_one_shot_events_dedup(env) -> None:
    engine, sub, _, clock, _ = env
    await engine.handle("rawlog.backpressure", 25000)
    await engine.handle("rawlog.backpressure", 26000)
    assert kinds(sub) == ["logger_backpressure"]
    clock.t += 301
    await engine.handle("rawlog.backpressure", 27000)
    assert kinds(sub) == ["logger_backpressure"]
    await engine.handle("rawlog.pruned", Path("/data/ubx/2026/261/MTRK_20260918_10.ubx"))
    assert kinds(sub) == ["log_pruned"]
    await engine.handle(
        "daemon.consumer_failed", {"name": "ntrip", "error": "OSError: address in use"}
    )
    assert kinds(sub) == ["consumer_failed_ntrip"]


async def test_survey_in_valid_and_site_verification(env) -> None:
    engine, sub, *_ = env
    await engine.handle("state.survey_in", SurveyIn(active=True, valid=False, mean_acc_m=3.0))
    await engine.handle("state.survey_in", SurveyIn(active=True, valid=True, mean_acc_m=1.2))
    await engine.handle("state.survey_in", SurveyIn(active=True, valid=True, mean_acc_m=1.1))
    assert kinds(sub) == ["survey_in_valid"]
    await engine.handle("base.site_mismatch", {"site": "roof", "dx": 0.5})
    await engine.handle("base.site_verified", {"site": "roof"})
    assert kinds(sub) == ["site_mismatch", "site_mismatch_cleared", "site_verified"]


async def test_webhook_failure_is_swallowed(env) -> None:
    engine, sub, http, *_ = env

    async def boom(url: str, json: dict, timeout: float) -> None:  # noqa: ASYNC109
        raise OSError("network down")

    http.post = boom  # type: ignore[method-assign]
    await engine.handle("receiver.error", "receiver rejected core config keys: ['X']")
    assert kinds(sub) == ["receiver_error"]


async def test_run_consumes_bus(env) -> None:
    engine, sub, *_ = env
    stop = asyncio.Event()
    task = asyncio.create_task(engine.run(stop))
    engine.bus.publish("receiver.disconnected", "usb unplugged")
    await asyncio.sleep(0.05)
    engine.stop()
    await asyncio.wait_for(task, 1.0)
    assert kinds(sub) == ["receiver_disconnected"]


# ------------------------------------------------- rules beyond the brief (controller ruling)


async def test_raw_log_write_failure_is_one_shot(env) -> None:
    engine, sub, _, clock, repo = env
    await engine.handle("rawlog.error", "write: OSError(28, 'No space left on device')")
    await engine.handle("rawlog.error", "write: OSError(28, 'No space left on device')")
    assert kinds(sub) == ["logger_error"]
    assert [e.level for e in await repo.list()] == ["error"]
    clock.t += 301
    await engine.handle("rawlog.error", "flush: OSError(5, 'Input/output error')")
    assert kinds(sub) == ["logger_error"]


async def test_sampler_failure_raises_once_and_recovers(env) -> None:
    engine, sub, *_ = env
    await engine.handle("sampler.error", "insert: OSError(28, 'No space left on device')")
    await engine.handle("sampler.error", "insert: OSError(28, 'No space left on device')")
    assert kinds(sub) == ["sampler_failing"]
    assert engine.active["sampler_failing"].level == "warning"
    await engine.handle("sampler.recovered", "sampler writing again")
    assert kinds(sub) == ["sampler_failing_cleared"]
    assert engine.active == {}
    await engine.handle("sampler.recovered", "sampler writing again")
    assert kinds(sub) == []  # nothing active: no second recovery event


# --------------------------------------------------------------------------- run loop


async def test_run_stops_on_stop_event_and_unsubscribes(env) -> None:
    """A silent receiver must not wedge shutdown: the stop event alone ends `run()`."""
    engine, *_ = env
    before = engine.bus.subscriber_count
    stop = asyncio.Event()
    task = asyncio.create_task(engine.run(stop))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, 1.0)
    assert engine.bus.subscriber_count == before - 1


async def test_a_failing_rule_does_not_end_the_loop(env, caplog) -> None:
    engine, sub, *_ = env
    stop = asyncio.Event()
    task = asyncio.create_task(engine.run(stop))
    with caplog.at_level(logging.ERROR, logger="mtrtk.alerts"):
        engine.bus.publish("state.fix", "not a FixInfo")  # a rule blows up on the payload
        engine.bus.publish("receiver.disconnected", "usb unplugged")
        await asyncio.sleep(0.05)
    engine.stop()
    await asyncio.wait_for(task, 1.0)
    assert kinds(sub) == ["receiver_disconnected"]
    assert "alert rule failed" in caplog.text
