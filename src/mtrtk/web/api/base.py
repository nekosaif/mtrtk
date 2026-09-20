"""Base-station mode, survey-in and fixed sites: /api/base/{mode,survey,sites}."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from mtrtk.base.basemode import RestartResult
from mtrtk.config import BaseMode
from mtrtk.core.geo import llh_to_ecef
from mtrtk.core.state import SurveyIn
from mtrtk.store.models import Site
from mtrtk.store.repos import SitesRepo
from mtrtk.web.api.config import apply_settings_change
from mtrtk.web.context import AppContext

router = APIRouter(prefix="/api/base", tags=["base"])

# Declared so the schema Phase 4 generates its client from carries them; FastAPI only infers
# the 2xx and the validation 422 on its own.
MODE_ERRORS: dict[int | str, dict[str, Any]] = {
    409: {"description": "no base mode manager, or fixed mode with no site to sit on"},
    422: {"description": "a value the settings refuse"},
}
FREEZE_ERRORS: dict[int | str, dict[str, Any]] = {
    409: {"description": "no base mode manager, no valid survey-in, or the name is taken"},
}
RESTART_ERRORS: dict[int | str, dict[str, Any]] = {
    409: {"description": "no base mode manager, not surveying, or the receiver refused"},
}
ADD_SITE_ERRORS: dict[int | str, dict[str, Any]] = {
    409: {"description": "a site of that name already exists"},
}
DELETE_SITE_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "no site of that name"},
    409: {"description": "the site is the active one"},
}
ACTIVATE_SITE_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "no site of that name"},
}

# Every route that reconfigures the receiver needs a manager to reconfigure it. Without one -
# the rover role, a replay source - answering 200 would report a mode change that never left
# this process. The sites table is a different matter: it is durable state a base picks up at
# its next poll or its next start, so sites CRUD works with or without a running base.
NO_MANAGER_DETAIL = (
    "base mode manager not running: this daemon has no base mode (rover role or replay source)"
)
# A restart is two writes, and the two failures leave the base in very different places.
RESTART_DETAILS = {
    RestartResult.STOP_REFUSED: (
        "the receiver refused or did not answer TMODE off, so the survey-in was not restarted: "
        "the survey that was already running is still running and nothing changed"
    ),
    RestartResult.SURVEY_REFUSED: (
        "TMODE off was applied but the receiver refused or did not answer the new survey-in: "
        "the base is in TMODE off and is not surveying - retry to send both steps again"
    ),
}


class ModeBody(BaseModel):
    mode: BaseMode
    svin_min_duration_s: int | None = Field(default=None, ge=1)
    svin_acc_limit_m: float | None = Field(default=None, gt=0)
    site: str | None = None

    @model_validator(mode="after")
    def _site_belongs_to_fixed(self) -> ModeBody:
        # `site` is written to `.env` as ACTIVE_SITE, and a name persisted while the base is off
        # or surveying is a trap: nothing checks it until the next switch to fixed mode.
        if self.site is not None and self.mode is not BaseMode.FIXED:
            raise ValueError(
                'site is only meaningful with mode="fixed"; to change the active site without '
                "changing the mode, use POST /api/base/sites/{name}/activate"
            )
        return self


class FreezeBody(BaseModel):
    name: str = Field(min_length=1)
    activate: bool = False


class SiteBody(BaseModel):
    """A site given either as ECEF metres or as geodetic degrees plus an ellipsoidal height."""

    name: str = Field(min_length=1)
    x: float | None = None
    y: float | None = None
    z: float | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    height_m: float | None = None
    sigma_m: float | None = Field(default=None, ge=0)
    source: str = "manual"
    frame: str = "ITRF2020"
    epoch: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _coords(self) -> SiteBody:
        ecef = (self.x, self.y, self.z)
        llh = (self.lat, self.lon, self.height_m)
        # Half a coordinate is a typo, not a position: taking the other form silently would put
        # the base somewhere nobody asked for.
        if any(v is not None for v in ecef) and None in ecef:
            raise ValueError("x, y and z must be given together (ECEF metres)")
        if any(v is not None for v in llh) and None in llh:
            raise ValueError("lat, lon and height_m must be given together")
        if None in ecef and None in llh:
            raise ValueError("provide x,y,z (ECEF metres) or lat,lon,height_m")
        return self

    def to_site(self) -> Site:
        x, y, z = self.x, self.y, self.z
        if x is None or y is None or z is None:
            lat, lon, height = self.lat, self.lon, self.height_m
            if lat is None or lon is None or height is None:  # `_coords` has ruled this out
                raise ValueError("provide x,y,z (ECEF metres) or lat,lon,height_m")
            x, y, z = llh_to_ecef(lat, lon, height)
        return Site.from_ecef(
            self.name,
            x,
            y,
            z,
            sigma_m=self.sigma_m,
            source=self.source,
            frame=self.frame,
            epoch=self.epoch,
            notes=self.notes,
        )


def _ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


def _manager(request: Request) -> Any:
    """The live `BaseModeManager`, or a 409 saying this daemon has none."""
    manager = _ctx(request).basemode
    if manager is None:
        raise HTTPException(409, NO_MANAGER_DETAIL)
    return manager


def _mode_view(request: Request) -> dict[str, Any]:
    """What the base is doing now: the manager's view, or the configured one where there is none.

    `site` is the site the receiver was actually put on, which is why it stays `null` without a
    manager - the configured name is `active_site` in `GET /api/config`, and the active row is in
    `GET /api/base/sites`.
    """
    ctx = _ctx(request)
    manager = ctx.basemode
    last = getattr(manager, "last_1005", None)
    return {
        "available": manager is not None,
        "mode": (manager.mode if manager else ctx.settings.base_mode).value,
        "site": (manager.applied_site.name if manager and manager.applied_site else None),
        "verified": bool(getattr(manager, "verified", False)),
        "last_1005": (
            {"station_id": last.station_id, "x": last.x, "y": last.y, "z": last.z}
            if last is not None
            else None
        ),
        "svin": {
            "min_duration_s": (
                manager.svin_min_duration_s if manager else ctx.settings.svin_min_duration_s
            ),
            "acc_limit_m": (manager.svin_acc_limit_m if manager else ctx.settings.svin_acc_limit_m),
        },
    }


def _survey_detail(survey: SurveyIn) -> str:
    """Why this survey-in cannot be frozen yet, in the operator's terms."""
    if not survey.active:
        return (
            "no survey-in is running: put the base in survey-in mode "
            "(PUT /api/base/mode) and wait for it to complete"
        )
    acc = f"{survey.mean_acc_m:.3f} m" if survey.mean_acc_m is not None else "unknown"
    return (
        f"the survey-in is not complete: {survey.dur_s}s and {survey.obs} observations so far, "
        f"mean accuracy {acc}"
    )


def _applied(manager: Any, site: Site) -> bool:
    """True when the receiver is sitting on *site* right now.

    Activating a row and applying it are two different things: there may be no manager at all
    (the row waits for a base to pick it up), or the receiver may have NAK'd the fixed position,
    in which case the row is active and the base is still where it was.
    """
    return (
        manager is not None
        and manager.mode is BaseMode.FIXED
        and manager.applied_site is not None
        and manager.applied_site.name == site.name
    )


def _site_result(ctx: AppContext, site: Site) -> dict[str, Any]:
    return {"site": site.model_dump(mode="json"), "applied": _applied(ctx.basemode, site)}


async def _activate(ctx: AppContext, name: str) -> Site:
    """Make *name* the active site and, when a base is running, sit the receiver on it.

    The database row is the durable part. `.env` is deliberately not written - this is the same
    contract as `mtrtk sites activate`, where the mode itself is a separate decision - so the
    running settings are moved to match instead: `GET /api/config` then reports what the daemon
    is really doing, and its `pending` block shows that a restart would go back to the configured
    mode. To make a fixed site outlive a restart, follow with
    `PUT /api/base/mode {"mode": "fixed", "site": name}`.
    """
    manager = ctx.basemode
    if manager is None:
        return await SitesRepo(ctx.db).activate(name)
    site: Site = await manager.activate_site(name)
    ctx.settings.active_site = name
    # `activate_site` moves the manager to fixed only if the receiver took the position; mirror
    # whatever it ended up in rather than assuming it worked.
    ctx.settings.base_mode = manager.mode
    return site


@router.get("/mode")
async def get_mode(request: Request) -> dict[str, Any]:
    return _mode_view(request)


@router.put("/mode", responses=MODE_ERRORS)
async def put_mode(body: ModeBody, request: Request) -> dict[str, Any]:
    """Change the base mode, persist it to `.env` and apply it to the receiver.

    One path: `apply_settings_change` validates, refuses `fixed` with no resolvable site (409,
    before anything is written), rewrites `.env` and calls `apply_mode()`. A body that changes
    nothing is a no-op - `POST /api/base/survey/restart` is how a running survey is started over.
    """
    ctx = _ctx(request)
    # A mode nobody can apply is not a mode change: refuse before `.env` records one.
    _manager(request)
    updates: dict[str, Any] = {"base_mode": body.mode}
    if body.svin_min_duration_s is not None:
        updates["svin_min_duration_s"] = body.svin_min_duration_s
    if body.svin_acc_limit_m is not None:
        updates["svin_acc_limit_m"] = body.svin_acc_limit_m
    if body.site is not None:
        updates["active_site"] = body.site
    await apply_settings_change(ctx, updates)
    return _mode_view(request)


@router.get("/survey")
async def get_survey(request: Request) -> dict[str, Any]:
    return _ctx(request).store.state.survey_in.model_dump(mode="json")


@router.post("/survey/restart", responses=RESTART_ERRORS)
async def restart_survey(request: Request) -> dict[str, Any]:
    """Start the survey-in over: TMODE off, then survey-in.

    Re-sending the same survey-in parameters does not restart a survey in progress on HPG 1.13,
    so this is not the same request as `PUT /api/base/mode {"mode": "survey-in"}` - which, when
    nothing changes, deliberately leaves the receiver alone.
    """
    manager = _manager(request)
    if manager.mode is not BaseMode.SURVEY_IN:
        raise HTTPException(
            409, f"base mode is {manager.mode.value}: switch to survey-in before restarting it"
        )
    result = await manager.restart_survey_in()
    if result is not RestartResult.OK:
        raise HTTPException(409, RESTART_DETAILS[result])
    return _mode_view(request)


@router.post("/survey/freeze", responses=FREEZE_ERRORS)
async def freeze_survey(body: FreezeBody, request: Request) -> dict[str, Any]:
    """Save the mean of a completed survey-in as a site, optionally activating it."""
    ctx = _ctx(request)
    manager = _manager(request)
    survey = ctx.store.state.survey_in
    if not survey.valid:
        raise HTTPException(409, _survey_detail(survey))
    try:
        site: Site = await manager.freeze_survey_in(body.name)
    except ValueError as exc:  # the name is taken, or the survey went invalid under us
        raise HTTPException(409, str(exc)) from exc
    if body.activate:
        site = await _activate(ctx, site.name)
    return _site_result(ctx, site)


@router.get("/sites")
async def list_sites(request: Request) -> list[dict[str, Any]]:
    return [s.model_dump(mode="json") for s in await SitesRepo(_ctx(request).db).list()]


@router.post("/sites", responses=ADD_SITE_ERRORS)
async def add_site(body: SiteBody, request: Request) -> dict[str, Any]:
    """Save a site. `{"site", "applied"}`, the same shape freeze and activate answer with.

    A site that has just been added is never the one the receiver is sitting on - adding and
    activating are separate decisions - so `applied` is false here. It is reported all the same,
    so a client reads one shape from all three routes.
    """
    ctx = _ctx(request)
    try:
        site = await SitesRepo(ctx.db).add(body.to_site())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _site_result(ctx, site)


@router.delete("/sites/{name}", responses=DELETE_SITE_ERRORS)
async def delete_site(name: str, request: Request) -> dict[str, bool]:
    repo = SitesRepo(_ctx(request).db)
    # `SitesRepo.delete` ignores an unknown name; reporting success for a typo would leave the
    # operator believing a site is gone when it is still there under its real name.
    if await repo.get(name) is None:
        raise HTTPException(404, f"no site named {name!r}")
    try:
        await repo.delete(name)
    except ValueError as exc:  # the active site: the base is broadcasting that position
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@router.post("/sites/{name}/activate", responses=ACTIVATE_SITE_ERRORS)
async def activate_site(name: str, request: Request) -> dict[str, Any]:
    ctx = _ctx(request)
    if await SitesRepo(ctx.db).get(name) is None:
        raise HTTPException(404, f"no site named {name!r}")
    try:
        site = await _activate(ctx, name)
    except KeyError as exc:
        # The row went away between the check and the activation - two requests, one database.
        raise HTTPException(404, f"no site named {name!r}") from exc
    return _site_result(ctx, site)
