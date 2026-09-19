import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import aiosqlite
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
    engine, sub, _, clock, _ = env

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
    assert kinds(sub) == []  # one hot sample is not an alert
    clock.t += 61
    await engine.handle("system.stats", stats(20.0, temp=85.0))
    assert kinds(sub) == ["temperature_high"]
    await engine.handle("system.stats", stats(20.0, temp=75.0))
    assert kinds(sub) == []  # hysteresis
    await engine.handle("system.stats", stats(20.0, temp=60.0))
    assert kinds(sub) == ["temperature_high_cleared"]


async def test_one_shot_events_dedup(env) -> None:
    engine, sub, _, clock, _ = env
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


async def test_receiver_error_clears_on_reconnect(env) -> None:
    """A passive or replay run never configures, so `receiver.capabilities` never arrives: the
    reconnection itself has to clear the error, or it stays active for the life of the process."""
    engine, sub, *_ = env
    await engine.handle("receiver.connected", "file:/data/replay.ubx")
    await engine.handle("receiver.error", "link failure: OSError(5, 'Input/output error')")
    assert kinds(sub) == ["receiver_error"]
    await engine.handle("receiver.disconnected", "link failure")
    assert kinds(sub) == ["receiver_disconnected"]
    await engine.handle("receiver.connected", "file:/data/replay.ubx")
    assert kinds(sub) == ["receiver_disconnected_cleared", "receiver_error_cleared"]
    assert engine.active == {}


async def test_concurrent_raises_emit_one_event(env) -> None:
    """`active` is reserved before the awaits, so two callers cannot both raise one condition."""
    engine, sub, http, _, repo = env
    delivered = http.post

    async def slow(url: str, json: dict, timeout: float) -> None:  # noqa: ASYNC109
        await asyncio.sleep(0.01)
        await delivered(url, json, timeout)

    http.post = slow  # type: ignore[method-assign]
    await asyncio.gather(
        engine.handle("receiver.disconnected", "usb unplugged"),
        engine.handle("receiver.disconnected", "usb unplugged"),
    )
    assert kinds(sub) == ["receiver_disconnected"]
    assert len(await repo.list()) == 1
    assert len(http.posts) == 1
    assert "receiver_disconnected" in engine.active


class FlakyEventsDatabase(Database):
    """Refuses every event insert, the way a full or read-only disk would."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.attempts = 0

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        if sql.startswith("INSERT INTO events"):
            self.attempts += 1
            raise sqlite3.OperationalError("database or disk is full")
        return await super().execute(sql, params)


async def test_handler_failures_are_rate_limited(tmp_path: Path, caplog) -> None:
    """A failing events table fails on every sample: one traceback, then one line a minute."""
    db = FlakyEventsDatabase(tmp_path / "m.db")
    await db.open()
    bus = Bus()
    clock = Clock()
    engine = AlertEngine(bus, EventsRepo(db), role="base", host="pi", min_free_gb=5.0, clock=clock)
    stop = asyncio.Event()
    task = asyncio.create_task(engine.run(stop))
    try:
        with caplog.at_level(logging.INFO, logger="mtrtk.alerts"):
            for i in range(5):
                bus.publish("receiver.disconnected", f"usb unplugged {i}")
            await asyncio.sleep(0.05)
            assert db.attempts == 5  # every sample retries: `active` never took the kind
            assert _levels(caplog) == ["ERROR"]  # one traceback, the other four suppressed
            clock.t += 61
            bus.publish("receiver.disconnected", "usb unplugged again")
            await asyncio.sleep(0.05)
            assert _levels(caplog) == ["ERROR", "WARNING"]
            assert "5 suppressed" in caplog.records[-1].getMessage()
            bus.publish("receiver.disconnected", "source ended")  # a message that cannot fail
            await asyncio.sleep(0.05)
            assert _levels(caplog) == ["ERROR", "WARNING", "INFO"]
    finally:
        stop.set()
        engine.stop()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(task, 1.0)
        await db.close()


def _levels(caplog) -> list[str]:
    return [r.levelname for r in caplog.records if r.name == "mtrtk.alerts"]


async def test_disk_low_clears_on_its_own_band(env) -> None:
    """Retention holds the disk just above the floor, so a `disk_low` that only clears at 1.5x
    the floor would stay raised for ever in exactly the state retention maintains."""
    engine, sub, *_ = env

    def stats(free: float) -> SystemStats:
        return SystemStats(
            cpu_pct=0, mem_pct=0, disk_free_gb=free, disk_used_pct=0, uptime_s=0, temp_c=40.0
        )

    await engine.handle("system.stats", stats(4.0))
    assert sorted(kinds(sub)) == ["disk_low", "disk_warning"]
    await engine.handle("system.stats", stats(5.1))  # over the floor, inside the 10% hysteresis
    assert kinds(sub) == []
    assert set(engine.active) == {"disk_low", "disk_warning"}
    await engine.handle("system.stats", stats(5.6))  # >= 1.1x the floor: the low band clears
    assert kinds(sub) == ["disk_low_cleared"]
    assert set(engine.active) == {"disk_warning"}
    await engine.handle("system.stats", stats(7.5))  # >= 1.5x: the warning band clears too
    assert kinds(sub) == ["disk_warning_cleared"]
    assert engine.active == {}


async def test_temperature_needs_a_sustained_minute(env) -> None:
    engine, sub, _, clock, _ = env

    def stats(temp: float) -> SystemStats:
        return SystemStats(
            cpu_pct=0, mem_pct=0, disk_free_gb=50.0, disk_used_pct=0, uptime_s=0, temp_c=temp
        )

    await engine.handle("system.stats", stats(85.0))
    clock.t += 30
    await engine.handle("system.stats", stats(85.0))
    assert kinds(sub) == []
    await engine.handle("system.stats", stats(79.0))  # one cool sample restarts the count
    clock.t += 31
    await engine.handle("system.stats", stats(85.0))
    assert kinds(sub) == []
    clock.t += 61
    await engine.handle("system.stats", stats(85.0))
    assert kinds(sub) == ["temperature_high"]
    await engine.handle("system.stats", stats(74.9))  # below threshold - 5 degrees
    assert kinds(sub) == ["temperature_high_cleared"]


async def test_backpressure_is_stateful_and_clears_when_the_logger_drains(env) -> None:
    engine, sub, _, clock, _ = env
    await engine.handle("rawlog.backpressure", {"queued": 2100})
    await engine.handle("rawlog.backpressure", {"queued": 2400})
    assert kinds(sub) == ["logger_backpressure"]  # one condition, not one event per report
    assert engine.active["logger_backpressure"].level == "warning"
    clock.t += 301
    await engine.handle("rawlog.backpressure", {"queued": 2600})
    assert kinds(sub) == []  # still the same condition, however long it lasts
    await engine.handle("rawlog.drained", {"queued": 900})
    assert kinds(sub) == ["logger_backpressure_cleared"]
    assert engine.active == {}


async def test_webhook_failures_never_log_the_url_path(env, caplog) -> None:
    """The webhook URL is the credential for ntfy, Discord and Slack: a topic or token in the
    path must not reach the log."""
    engine, sub, http, clock, _ = env
    engine.webhook_url = "https://ntfy.sh/mtrtk-secret-topic"

    async def boom(url: str, json: dict, timeout: float) -> None:  # noqa: ASYNC109
        raise OSError(
            "Client error '401 Unauthorized' for url 'https://ntfy.sh/mtrtk-secret-topic'"
        )

    http.post = boom  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="mtrtk.alerts"):
        await engine.handle("receiver.error", "boom")
        clock.t += 61
        await engine.handle("receiver.disconnected", "usb unplugged")
    text = caplog.text
    assert "mtrtk-secret-topic" not in text
    assert "https://ntfy.sh" in text


async def test_hardware_rule_reads_one_snapshot_of_the_sample(env) -> None:
    """`state.hardware` republishes the live object: a value read back after an await is the
    next sample's, so an antenna fault could be attributed to the sample before it."""
    engine, sub, http, clock, _ = env
    hw = Hardware(jam_ind=250, ant_status=2)
    await engine.handle("state.hardware", hw)
    clock.t += 31

    async def mutate(url: str, json: dict, timeout: float) -> None:  # noqa: ASYNC109
        hw.jam_ind, hw.ant_status = 5, 4  # the receiver moves on while the event is written

    http.post = mutate  # type: ignore[method-assign]
    await engine.handle("state.hardware", hw)
    assert kinds(sub) == ["jamming"]
    assert "antenna_fault" not in engine.active
