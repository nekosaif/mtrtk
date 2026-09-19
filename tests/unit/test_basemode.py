import asyncio
from pathlib import Path

import pytest

from mtrtk.base.basemode import SITE_TOLERANCE_M, BaseModeManager
from mtrtk.config import BaseMode
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.core.state import FixInfo, SurveyIn
from mtrtk.core.statestore import StateStore
from mtrtk.core.ubx_config import LAYERS_ALL, tmode_fixed_ecef, tmode_off, tmode_survey_in
from mtrtk.store.db import Database
from mtrtk.store.models import Site
from mtrtk.store.repos import SitesRepo
from ubxtest import rtcm_frame

# (1234567.8912, -987654.3234, 5555555.0)
RTCM_1005 = bytes.fromhex("d300133ed7fd0382dfdc1c403db34fe8fe0cef5e6b30bd2e23")


class FakeController:
    def __init__(self) -> None:
        self.applied: list[tuple[list[tuple[str, int]], int]] = []
        self.ok = True

    async def apply_items(self, items, layers=LAYERS_ALL):
        self.applied.append((list(items), layers))
        return self.ok


class Clock:
    t = 100.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
async def env(tmp_path: Path):
    db = Database(tmp_path / "m.db")
    await db.open()
    bus = Bus()
    store = StateStore(bus)
    ctrl = FakeController()
    clock = Clock()
    sub = bus.subscribe("base.*")

    def make(mode: BaseMode, active: str | None = None) -> BaseModeManager:
        return BaseModeManager(
            bus,
            ctrl,
            SitesRepo(db),
            store,
            base_mode=mode,
            svin_min_duration_s=300,
            svin_acc_limit_m=2.0,
            active_site_name=active,
            clock=clock,
        )

    try:
        yield make, ctrl, SitesRepo(db), store, sub, clock
    finally:
        await db.close()


def drain(sub) -> list[tuple[str, dict]]:
    return [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]


async def test_apply_survey_in(env) -> None:
    make, ctrl, *_, sub, _ = env
    await make(BaseMode.SURVEY_IN).apply_mode()
    assert ctrl.applied == [(tmode_survey_in(300, 2.0), LAYERS_ALL)]
    assert drain(sub) == [("base.mode", {"mode": "survey-in", "site": None, "reason": None})]


async def test_apply_off(env) -> None:
    make, ctrl, *_ = env
    await make(BaseMode.OFF).apply_mode()
    assert ctrl.applied == [(tmode_off(), LAYERS_ALL)]


async def test_apply_fixed_uses_active_site(env) -> None:
    make, ctrl, sites, _, sub, _ = env
    await sites.add(
        Site.from_ecef(
            "roof", 1234567.8912, -987654.3234, 5555555.0, sigma_m=0.004, source="csrs-ppp"
        )
    )
    await sites.activate("roof")
    mgr = make(BaseMode.FIXED)
    await mgr.apply_mode()
    assert ctrl.applied == [
        (tmode_fixed_ecef(1234567.8912, -987654.3234, 5555555.0, 0.004 * 3**0.5), LAYERS_ALL)
    ]
    assert mgr.applied_site is not None and mgr.applied_site.name == "roof"
    assert drain(sub)[0][1]["site"] == "roof"


async def test_apply_fixed_by_name_from_settings(env) -> None:
    make, ctrl, sites, *_ = env
    await sites.add(Site.from_ecef("field", 1.0, 2.0, 3.0, source="manual"))
    await make(BaseMode.FIXED, active="field").apply_mode()
    assert ctrl.applied[0][0][0] == ("CFG_TMODE_MODE", 2)
    assert (await sites.active()).name == "field"  # settings name gets activated in the DB


async def test_apply_fixed_without_site_falls_back_to_survey_in(env) -> None:
    make, ctrl, _, _, sub, _ = env
    mgr = make(BaseMode.FIXED)
    await mgr.apply_mode()
    assert ctrl.applied == [(tmode_survey_in(300, 2.0), LAYERS_ALL)]
    assert mgr.mode is BaseMode.SURVEY_IN
    assert drain(sub) == [
        (
            "base.mode",
            {
                "mode": "survey-in",
                "site": None,
                "reason": "no active site; falling back to survey-in",
            },
        )
    ]


async def test_freeze_survey_in(env) -> None:
    make, _, sites, store, *_ = env
    mgr = make(BaseMode.SURVEY_IN)
    store.state.survey_in = SurveyIn(
        active=True, valid=False, mean_x_m=1.0, mean_y_m=2.0, mean_z_m=3.0, mean_acc_m=5.0
    )
    with pytest.raises(ValueError, match="not valid"):
        await mgr.freeze_survey_in("roof")
    store.state.survey_in = SurveyIn(
        active=True,
        valid=True,
        dur_s=600,
        mean_x_m=1234567.8912,
        mean_y_m=-987654.3234,
        mean_z_m=5555555.0,
        mean_acc_m=1.2,
    )
    site = await mgr.freeze_survey_in("roof")
    assert site.source == "survey-in" and site.sigma_x == 1.2 and site.frame == "WGS84 (receiver)"
    assert (await sites.get("roof")).x == 1234567.8912


async def test_freeze_survey_in_without_a_mean_position(env) -> None:
    make, _, _, store, *_ = env
    mgr = make(BaseMode.SURVEY_IN)
    store.state.survey_in = SurveyIn(active=False, valid=True)  # means are None when inactive
    with pytest.raises(ValueError, match="not valid"):
        await mgr.freeze_survey_in("roof")


async def test_activate_and_verify_against_1005(env) -> None:
    make, ctrl, sites, _, sub, _ = env
    await sites.add(
        Site.from_ecef(
            "roof", 1234567.8912, -987654.3234, 5555555.0, sigma_m=0.004, source="csrs-ppp"
        )
    )
    mgr = make(BaseMode.SURVEY_IN)
    await mgr.activate_site("roof")
    assert mgr.mode is BaseMode.FIXED and ctrl.applied[-1][0][0] == ("CFG_TMODE_MODE", 2)
    drain(sub)
    mgr.on_1005(Framer().feed(RTCM_1005)[0])
    mgr.on_1005(Framer().feed(RTCM_1005)[0])  # verified only once
    published = drain(sub)
    assert [t for t, _ in published] == ["base.site_verified"]
    assert published[0][1]["site"] == "roof" and abs(published[0][1]["dx"]) <= SITE_TOLERANCE_M
    assert mgr.verified is True and mgr.last_1005 is not None


async def test_mismatching_1005_is_reported_once(env) -> None:
    make, _, sites, _, sub, _ = env
    await sites.add(
        Site.from_ecef(
            "roof", 1234567.8912 + 0.5, -987654.3234, 5555555.0, sigma_m=0.004, source="manual"
        )
    )
    mgr = make(BaseMode.SURVEY_IN)
    await mgr.activate_site("roof")
    drain(sub)
    mgr.on_1005(Framer().feed(RTCM_1005)[0])
    mgr.on_1005(Framer().feed(RTCM_1005)[0])
    published = drain(sub)
    assert [t for t, _ in published] == ["base.site_mismatch"]
    assert published[0][1]["dx"] == pytest.approx(-0.5, abs=1e-6)
    assert mgr.verified is False


async def test_on_1005_ignores_frames_that_are_not_1005(env) -> None:
    make, _, sites, _, sub, _ = env
    await sites.add(Site.from_ecef("roof", 1.0, 2.0, 3.0, source="manual"))
    mgr = make(BaseMode.SURVEY_IN)
    await mgr.activate_site("roof")
    drain(sub)
    mgr.on_1005(Framer().feed(rtcm_frame(1077, b"\x00" * 20))[0])
    assert mgr.last_1005 is None and drain(sub) == []


async def test_fix_type_deadline_reports_mismatch(env) -> None:
    make, _, sites, _, sub, clock = env
    await sites.add(Site.from_ecef("roof", 1.0, 2.0, 3.0, source="manual"))
    mgr = make(BaseMode.SURVEY_IN)
    await mgr.activate_site("roof")
    drain(sub)
    mgr.on_fix(FixInfo(fix_type=3))
    assert drain(sub) == []
    clock.t += 31
    mgr.on_fix(FixInfo(fix_type=3))
    published = drain(sub)
    assert published[0][0] == "base.site_mismatch" and "fixType" in published[0][1]["reason"]
    mgr.on_fix(FixInfo(fix_type=3))
    assert drain(sub) == []


async def test_time_only_fix_never_reports_a_mismatch(env) -> None:
    make, _, sites, _, sub, clock = env
    await sites.add(Site.from_ecef("roof", 1.0, 2.0, 3.0, source="manual"))
    mgr = make(BaseMode.SURVEY_IN)
    await mgr.activate_site("roof")
    drain(sub)
    clock.t += 31
    mgr.on_fix(FixInfo(fix_type=5))
    assert drain(sub) == []


async def test_poll_active_site_picks_up_cli_activation(env) -> None:
    make, ctrl, sites, *_ = env
    await sites.add(Site.from_ecef("a", 1.0, 2.0, 3.0, source="manual"))
    mgr = make(BaseMode.SURVEY_IN)
    await mgr.apply_mode()
    await sites.activate("a")  # as `mtrtk sites activate a` would
    await mgr.poll_active_site()
    assert mgr.mode is BaseMode.FIXED and mgr.applied_site.name == "a"
    n = len(ctrl.applied)
    await mgr.poll_active_site()
    assert len(ctrl.applied) == n  # unchanged: no re-apply


async def test_a_nak_is_announced_not_raised(env) -> None:
    make, ctrl, *_, sub, _ = env
    ctrl.ok = False
    mgr = make(BaseMode.SURVEY_IN)
    await mgr.apply_mode()
    topic, meta = drain(sub)[0]
    assert topic == "base.mode" and meta["mode"] == "survey-in" and "reject" in meta["reason"]


async def test_a_nak_on_a_fixed_site_leaves_the_mode_alone(env) -> None:
    make, ctrl, sites, _, sub, _ = env
    await sites.add(Site.from_ecef("roof", 1.0, 2.0, 3.0, source="manual"))
    mgr = make(BaseMode.SURVEY_IN)
    ctrl.ok = False
    await mgr.activate_site("roof")
    assert mgr.mode is BaseMode.SURVEY_IN and mgr.applied_site is None
    topic, meta = drain(sub)[0]
    assert topic == "base.mode" and meta["site"] == "roof" and "reject" in meta["reason"]

    n = len(ctrl.applied)
    await mgr.poll_active_site()
    assert len(ctrl.applied) == n  # the poll loop does not hammer a rejected site

    ctrl.ok = True
    await mgr.apply_mode()  # a reconfigure clears the sticky failure
    await mgr.poll_active_site()
    assert mgr.mode is BaseMode.FIXED and mgr.applied_site.name == "roof"


async def test_run_applies_on_capabilities_and_routes_frames(env) -> None:
    make, ctrl, _, _, sub, _ = env
    mgr = make(BaseMode.SURVEY_IN)
    stop = asyncio.Event()
    task = asyncio.create_task(mgr.run(stop, poll_s=0.01))
    mgr.bus.publish("receiver.capabilities", object())
    await asyncio.sleep(0.05)
    assert ctrl.applied == [(tmode_survey_in(300, 2.0), LAYERS_ALL)]
    mgr.stop()
    await asyncio.wait_for(task, 1.0)


async def test_run_stops_on_the_stop_event(env) -> None:
    make, *_ = env
    mgr = make(BaseMode.SURVEY_IN)
    stop = asyncio.Event()
    task = asyncio.create_task(mgr.run(stop, poll_s=0.01))
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, 1.0)
    assert mgr.bus.subscriber_count == 1  # only the test's own base.* subscription is left
