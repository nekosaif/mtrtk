from pathlib import Path

import pytest
from webtest import client, make_ctx

from mtrtk.base.rtcm1005 import Ecef1005
from mtrtk.config import BaseMode
from mtrtk.core.state import SurveyIn
from mtrtk.store.models import Site
from mtrtk.store.repos import SitesRepo
from mtrtk.web.app import create_app

VALID_SURVEY = SurveyIn(
    active=True,
    valid=True,
    mean_x_m=10.0,
    mean_y_m=20.0,
    mean_z_m=30.0,
    mean_acc_m=1.1,
    dur_s=300,
    obs=300,
)


class FakeBaseMode:
    """A `BaseModeManager` stand-in that records what each apply did.

    `apply_mode` mirrors the real manager rather than the API's view of it: in fixed mode it
    resolves the site itself - activating the named row exactly as `_resolve_site` does - and
    falls back to survey-in when no site resolves. That is what makes `applied` a faithful
    record of what the receiver would have been told.
    """

    def __init__(self, sites: SitesRepo, store) -> None:  # type: ignore[no-untyped-def]
        self.sites, self.store = sites, store
        self.mode = BaseMode.SURVEY_IN
        self.svin_min_duration_s, self.svin_acc_limit_m, self.active_site_name = 300, 2.0, None
        self.applied_site: Site | None = None
        self.verified = False
        self.last_1005 = Ecef1005(0, 1.0, 2.0, 3.0, True, True, True)
        self.applied: list[str] = []
        self.restart_ok = True

    async def _resolve_site(self) -> Site | None:
        if self.active_site_name:
            named = await self.sites.get(self.active_site_name)
            if named is not None:
                return named if named.active else await self.sites.activate(named.name)
        return await self.sites.active()

    async def apply_mode(self) -> None:
        if self.mode is BaseMode.FIXED:
            site = await self._resolve_site()
            if site is None:  # the manager's own fallback, which the API must never provoke
                self.mode, self.applied_site = BaseMode.SURVEY_IN, None
                self.applied.append("survey-in")
                return
            self.applied_site = site
            self.applied.append("fixed:" + site.name)
            return
        self.applied_site = None
        self.applied.append(self.mode.value)

    async def restart_survey_in(self) -> bool:
        self.mode, self.applied_site = BaseMode.SURVEY_IN, None
        self.applied.append("restart" if self.restart_ok else "restart-refused")
        return self.restart_ok

    async def freeze_survey_in(self, name: str) -> Site:
        if not self.store.state.survey_in.valid:
            raise ValueError("survey-in is not valid yet")
        return await self.sites.add(
            Site.from_ecef(name, 10.0, 20.0, 30.0, sigma_m=1.1, source="survey-in")
        )

    async def activate_site(self, name: str) -> Site:
        site = await self.sites.activate(name)
        self.applied_site, self.mode = site, BaseMode.FIXED
        self.applied.append("fixed:" + name)
        return site


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    env = tmp_path / ".env"
    env.write_text("BASE_MODE=survey-in\n")
    c = await make_ctx(tmp_path, mtrtk_env_file=env)
    c.daemon.basemode = FakeBaseMode(SitesRepo(c.db), c.store)
    try:
        yield c
    finally:
        await c.db.close()


async def test_get_mode(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/base/mode")).json()
    assert body["mode"] == "survey-in" and body["site"] is None and body["available"] is True
    assert body["last_1005"] == {"station_id": 0, "x": 1.0, "y": 2.0, "z": 3.0}
    assert body["svin"] == {"min_duration_s": 300, "acc_limit_m": 2.0}
    assert body["verified"] is False


async def test_put_mode_survey_in_with_params_persists(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.put(
            "/api/base/mode",
            json={"mode": "survey-in", "svin_min_duration_s": 900, "svin_acc_limit_m": 0.5},
        )
    assert r.status_code == 200 and r.json()["svin"] == {"min_duration_s": 900, "acc_limit_m": 0.5}
    assert ctx.daemon.basemode.applied == ["survey-in"]
    assert ctx.settings.svin_min_duration_s == 900
    assert "SVIN_MIN_DURATION_S=900" in ctx.settings.mtrtk_env_file.read_text()


async def test_put_mode_fixed_requires_site(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        assert (await c.put("/api/base/mode", json={"mode": "fixed"})).status_code == 409
        await c.post(
            "/api/base/sites",
            json={"name": "roof", "x": 1.0, "y": 2.0, "z": 3.0, "source": "manual"},
        )
        r = await c.put("/api/base/mode", json={"mode": "fixed", "site": "roof"})
    assert r.status_code == 200 and r.json()["mode"] == "fixed" and r.json()["site"] == "roof"
    assert ctx.daemon.basemode.applied == ["fixed:roof"]
    env = ctx.settings.mtrtk_env_file.read_text()
    assert "BASE_MODE=fixed" in env and "ACTIVE_SITE=roof" in env

    """`.env` must never record `BASE_MODE=fixed` while the base would fall back to surveying."""


async def test_a_refused_fixed_mode_writes_nothing(ctx) -> None:  # type: ignore[no-untyped-def]
    """The 409 has to land before the `.env` write, never after: see the test body."""
    before = ctx.settings.mtrtk_env_file.read_bytes()
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/base/mode", json={"mode": "fixed", "site": "ghost"})
    assert r.status_code == 409 and "site" in r.json()["detail"]
    assert ctx.settings.mtrtk_env_file.read_bytes() == before
    assert ctx.daemon.basemode.applied == [] and ctx.settings.base_mode is BaseMode.SURVEY_IN


async def test_an_unchanged_mode_is_a_no_op(ctx) -> None:  # type: ignore[no-untyped-def]
    """Re-sending the running mode must not reconfigure the receiver; /survey/restart does that."""
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/base/mode", json={"mode": "survey-in"})
    assert r.status_code == 200 and ctx.daemon.basemode.applied == []


async def test_survey_and_freeze(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        assert (await c.get("/api/base/survey")).json()["valid"] is False
        r = await c.post("/api/base/survey/freeze", json={"name": "roof"})
        assert r.status_code == 409 and "no survey-in is running" in r.json()["detail"]
        ctx.store.state.survey_in = VALID_SURVEY
        r = await c.post("/api/base/survey/freeze", json={"name": "roof", "activate": True})
        assert r.status_code == 200 and r.json()["name"] == "roof" and r.json()["active"] is True
        assert (await c.post("/api/base/survey/freeze", json={"name": "roof"})).status_code == 409
    assert ctx.daemon.basemode.applied == ["fixed:roof"]


async def test_freezing_an_unfinished_survey_says_how_far_it_got(ctx) -> None:  # type: ignore[no-untyped-def]
    """The refusal is the operator's progress report: how long, how many, how good so far."""
    ctx.store.state.survey_in = SurveyIn(active=True, valid=False, dur_s=42, obs=41, mean_acc_m=3.5)
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/base/survey/freeze", json={"name": "roof"})
        assert (await c.get("/api/base/sites")).json() == []  # nothing was stored
    detail = r.json()["detail"]
    assert r.status_code == 409 and "42s" in detail and "41 observations" in detail
    assert "3.500 m" in detail


async def test_freeze_without_activate_leaves_the_mode_alone(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.store.state.survey_in = VALID_SURVEY
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/base/survey/freeze", json={"name": "roof"})
        assert r.status_code == 200 and r.json()["active"] is False
        assert (await c.get("/api/base/mode")).json()["mode"] == "survey-in"
    assert ctx.daemon.basemode.applied == []


async def test_restart_survey_restarts_the_survey_in(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/base/survey/restart")
    assert r.status_code == 200 and r.json()["mode"] == "survey-in"
    assert ctx.daemon.basemode.applied == ["restart"]


async def test_restart_survey_in_another_mode_is_409(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.daemon.basemode.mode = BaseMode.FIXED
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/base/survey/restart")
    assert r.status_code == 409 and "survey-in" in r.json()["detail"]
    assert ctx.daemon.basemode.applied == []


async def test_a_refused_restart_is_409(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.daemon.basemode.restart_ok = False
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/base/survey/restart")
    assert r.status_code == 409 and "receiver" in r.json()["detail"]


async def test_sites_crud_and_activate(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.post(
            "/api/base/sites",
            json={
                "name": "llh",
                "lat": 23.8373506,
                "lon": 90.2625502,
                "height_m": -36.268,
                "sigma_m": 0.01,
            },
        )
        assert r.status_code == 200 and abs(r.json()["x"] - 1.0) > 1000
        r = await c.post(
            "/api/base/sites",
            json={
                "name": "ecef",
                "x": 1234567.8912,
                "y": -987654.3234,
                "z": 5555555.0,
                "source": "csrs-ppp",
                "frame": "ITRF2020",
                "epoch": "2026.71",
            },
        )
        assert r.status_code == 200 and r.json()["source"] == "csrs-ppp"
        assert (await c.post("/api/base/sites", json={"name": "bad"})).status_code == 422
        assert (
            await c.post("/api/base/sites", json={"name": "ecef", "x": 1, "y": 2, "z": 3})
        ).status_code == 409
        names = [s["name"] for s in (await c.get("/api/base/sites")).json()]
        assert names == ["ecef", "llh"]
        r = await c.post("/api/base/sites/ecef/activate")
        assert r.status_code == 200 and r.json()["active"] is True
        assert ctx.daemon.basemode.applied[-1] == "fixed:ecef"
        assert (await c.post("/api/base/sites/nope/activate")).status_code == 404
        assert (await c.delete("/api/base/sites/llh")).status_code == 200
        assert [s["name"] for s in (await c.get("/api/base/sites")).json()] == ["ecef"]


async def test_deleting_the_active_site_is_409(ctx) -> None:  # type: ignore[no-untyped-def]
    """The base is broadcasting that position; deleting the row would leave an unnamed ARP."""
    async with client(create_app(ctx)) as c:
        await c.post("/api/base/sites", json={"name": "roof", "x": 1.0, "y": 2.0, "z": 3.0})
        await c.post("/api/base/sites/roof/activate")
        r = await c.delete("/api/base/sites/roof")
        assert r.status_code == 409 and "active site" in r.json()["detail"]
        assert [s["name"] for s in (await c.get("/api/base/sites")).json()] == ["roof"]


async def test_deleting_an_unknown_site_is_404(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.delete("/api/base/sites/nope")
    assert r.status_code == 404 and "nope" in r.json()["detail"]


async def test_openapi_documents_the_failure_codes(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        paths = (await c.get("/api/openapi.json")).json()["paths"]
    assert "409" in paths["/api/base/mode"]["put"]["responses"]
    assert "409" in paths["/api/base/survey/freeze"]["post"]["responses"]
    assert "409" in paths["/api/base/survey/restart"]["post"]["responses"]
    assert {"404", "409"} <= set(paths["/api/base/sites/{name}"]["delete"]["responses"])
    assert "404" in paths["/api/base/sites/{name}/activate"]["post"]["responses"]


async def test_routes_require_auth(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path, web_password="secret")
    ctx.daemon.basemode = FakeBaseMode(SitesRepo(ctx.db), ctx.store)
    try:
        async with client(create_app(ctx)) as c:
            assert (await c.get("/api/base/mode")).status_code == 401
            assert (await c.put("/api/base/mode", json={"mode": "off"})).status_code == 401
            assert (await c.get("/api/base/survey")).status_code == 401
            assert (await c.post("/api/base/survey/freeze", json={"name": "x"})).status_code == 401
            assert (await c.post("/api/base/survey/restart")).status_code == 401
            assert (await c.get("/api/base/sites")).status_code == 401
            assert (
                await c.post("/api/base/sites", json={"name": "x", "x": 1, "y": 2, "z": 3})
            ).status_code == 401
            assert (await c.delete("/api/base/sites/x")).status_code == 401
            assert (await c.post("/api/base/sites/x/activate")).status_code == 401
        assert ctx.daemon.basemode.applied == []
    finally:
        await ctx.db.close()


async def test_without_basemode_sites_work_but_mode_is_409(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path)
    env_file = ctx.settings.mtrtk_env_file
    try:
        async with client(create_app(ctx)) as c:
            body = (await c.get("/api/base/mode")).json()
            assert body["available"] is False and body["mode"] == "survey-in"
            assert body["last_1005"] is None and body["site"] is None
            assert (await c.put("/api/base/mode", json={"mode": "off"})).status_code == 409
            assert (await c.post("/api/base/survey/freeze", json={"name": "x"})).status_code == 409
            assert (await c.post("/api/base/survey/restart")).status_code == 409
            assert (await c.get("/api/base/survey")).status_code == 200
            assert (
                await c.post("/api/base/sites", json={"name": "a", "x": 1, "y": 2, "z": 3})
            ).status_code == 200
            # DB only: a base daemon applies it on its next poll, or at its next start.
            r = await c.post("/api/base/sites/a/activate")
            assert r.status_code == 200 and r.json()["active"] is True
        assert not env_file.exists()  # a refused mode change writes nothing at all
    finally:
        await ctx.db.close()
