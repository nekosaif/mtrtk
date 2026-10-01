"""INS alert rules: alignment, GNSS loss under the INS, configuration mismatch, IMU error."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from mtrtk.alerts import AlertEngine
from mtrtk.core.bus import Bus
from mtrtk.core.state import InsStatus
from mtrtk.store.db import Database
from mtrtk.store.repos import EventsRepo


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
async def env(tmp_path: Path):  # type: ignore[no-untyped-def]
    db = Database(tmp_path / "m.db")
    await db.open()
    bus = Bus()
    clock = Clock()
    engine = AlertEngine(bus, EventsRepo(db), role="rover", host="pi", min_free_gb=5.0, clock=clock)
    sub = bus.subscribe("events.new")
    try:
        yield engine, sub, clock
    finally:
        await db.close()


def events(sub) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    out = [sub.queue.get_nowait()[1] for _ in range(sub.queue.qsize())]
    return [(e.kind, e.level) for e in out]


def sbg(mode: int, gnss_fix: int | None = 7, imu_power: bool = True) -> InsStatus:
    return InsStatus(
        vendor="sbg",
        mode=mode,
        gnss_fix=gnss_fix,
        general_ok={"main_power": True, "imu_power": imu_power},
    )


def vn(mode: int, gnss_fix: int | None = 3, imu_error: bool = False) -> InsStatus:
    return InsStatus(vendor="vectornav", mode=mode, gnss_fix=gnss_fix, errors={"imu": imu_error})


async def test_not_aligned_warns_after_a_minute_and_clears_on_alignment(env) -> None:  # type: ignore[no-untyped-def]
    engine, sub, clock = env
    await engine.handle("receiver.connected", "serial:/dev/ttyUSB0")
    await engine.handle("state.ins", sbg(1))  # vertical gyro: not navigating yet
    clock.t += 59
    await engine.handle("state.ins", sbg(2))
    assert events(sub) == []
    clock.t += 2
    await engine.handle("state.ins", sbg(3))
    assert events(sub) == [("ins_not_aligned", "warning")]
    await engine.handle("state.ins", sbg(4))  # NAV_POSITION
    assert events(sub) == [("ins_not_aligned_cleared", "info")]


async def test_alignment_clock_restarts_on_reconnect(env) -> None:  # type: ignore[no-untyped-def]
    engine, sub, clock = env
    await engine.handle("state.ins", vn(1))
    clock.t += 50
    await engine.handle("receiver.connected", "serial:/dev/ttyUSB0")
    await engine.handle("state.ins", vn(1))
    clock.t += 30
    await engine.handle("state.ins", vn(0))
    assert events(sub) == []  # 30 s since the reconnect, not 80
    clock.t += 31
    await engine.handle("state.ins", vn(1))
    assert events(sub) == [("ins_not_aligned", "warning")]
    await engine.handle("state.ins", vn(2))  # VectorNav: 2 = tracking
    assert events(sub) == [("ins_not_aligned_cleared", "info")]


async def test_gnss_lost_under_the_ins(env) -> None:  # type: ignore[no-untyped-def]
    engine, sub, clock = env
    await engine.handle("state.ins", sbg(4, gnss_fix=0))  # never had a fix: nothing lost
    clock.t += 30
    await engine.handle("state.ins", sbg(4, gnss_fix=0))
    assert events(sub) == []
    await engine.handle("state.ins", sbg(4, gnss_fix=7))
    await engine.handle("state.ins", sbg(4, gnss_fix=0))
    clock.t += 9
    await engine.handle("state.ins", sbg(4, gnss_fix=0))
    assert events(sub) == []
    clock.t += 2
    await engine.handle("state.ins", sbg(4, gnss_fix=0))
    assert events(sub) == [("ins_gnss_lost", "error")]
    await engine.handle("state.ins", sbg(4, gnss_fix=2))
    assert events(sub) == [("ins_gnss_lost_cleared", "info")]


async def test_config_mismatch_from_the_report(env) -> None:  # type: ignore[no-untyped-def]
    engine, sub, _ = env
    await engine.handle("ins.config", SimpleNamespace(mismatched=[], errors=[]))
    assert events(sub) == []
    await engine.handle("ins.config", SimpleNamespace(mismatched=["motion_profile"]))
    assert events(sub) == [("ins_config_mismatch", "error")]
    await engine.handle("ins.config", SimpleNamespace(mismatched=[]))
    assert events(sub) == [("ins_config_mismatch_cleared", "info")]


@pytest.mark.parametrize(
    ("bad", "good"),
    [(sbg(4, imu_power=False), sbg(4)), (vn(2, imu_error=True), vn(2))],
)
async def test_imu_error(env, bad: InsStatus, good: InsStatus) -> None:  # type: ignore[no-untyped-def]
    engine, sub, _ = env
    await engine.handle("state.ins", bad)
    await engine.handle("state.ins", bad)
    assert events(sub) == [("imu_error", "error")]
    await engine.handle("state.ins", good)
    assert events(sub) == [("imu_error_cleared", "info")]


async def test_a_unit_without_the_fields_raises_nothing(env) -> None:  # type: ignore[no-untyped-def]
    engine, sub, clock = env
    await engine.handle("state.ins", InsStatus(vendor="other"))
    clock.t += 120
    await engine.handle("state.ins", InsStatus(vendor="other"))
    assert events(sub) == []
