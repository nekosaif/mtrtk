"""GET/PUT /api/config, the one settings-apply path, and POST /api/restart."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from mtrtk.config import BaseMode, Settings
from mtrtk.store.models import Site
from mtrtk.store.repos import SitesRepo
from mtrtk.web.context import AppContext
from mtrtk.web.envfile import to_env_value, update_env

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["config"])

SECRET_KEYS = {"ntrip_password", "web_password", "alert_webhook_url", "tunnel_token"}
LIVE_KEYS = {"base_mode", "svin_min_duration_s", "svin_acc_limit_m", "active_site"}
MASK = "***"

# `base_mode=fixed` with no site is not an error the receiver reports: `BaseModeManager` falls
# back to survey-in and logs it. Recording that fallback in `.env` would make every later start
# claim "fixed" and survey instead, so the change is refused before anything is written.
NO_SITE_DETAIL = (
    "base_mode=fixed needs a site: add one and activate it, or set active_site to a saved site"
)
NTRIP_PASSWORD_NULL_DETAIL = 'ntrip_password: use "" for anonymous or set a password'


@dataclass(frozen=True, slots=True)
class ConfigChange:
    changed: list[str]
    restart_required: bool

    def as_dict(self) -> dict[str, Any]:
        return {"changed": self.changed, "restart_required": self.restart_required}


class ConfigBody(BaseModel):
    values: dict[str, Any]


def _masked(settings: Settings) -> dict[str, Any]:
    values = settings.model_dump(mode="json")
    for key in SECRET_KEYS:
        if values.get(key):
            values[key] = MASK
    return values


@router.get("/config")
async def get_config(request: Request) -> dict[str, Any]:
    ctx: AppContext = request.app.state.ctx
    return {
        "values": _masked(ctx.settings),
        "env_file": str(ctx.settings.mtrtk_env_file),
        "secret_keys": sorted(SECRET_KEYS),
        "live_keys": sorted(LIVE_KEYS),
    }


@router.put("/config")
async def put_config(body: ConfigBody, request: Request) -> dict[str, Any]:
    ctx: AppContext = request.app.state.ctx
    # A secret handed back masked means "leave it alone", not "set it to ***".
    updates = {k: v for k, v in body.values.items() if not (k in SECRET_KEYS and v == MASK)}
    change = await apply_settings_change(ctx, updates)
    return change.as_dict()


@router.post("/restart")
async def restart(request: Request) -> dict[str, bool]:
    ctx: AppContext = request.app.state.ctx
    stop = getattr(ctx.daemon, "stop", None)
    if stop is None:
        raise HTTPException(409, "daemon not running")
    stop.set()  # the process exits; Docker/systemd restarts it with the new .env
    return {"ok": True}


# --------------------------------------------------------------------------- the one write path


async def apply_settings_change(ctx: AppContext, updates: Mapping[str, Any]) -> ConfigChange:
    """Validate *updates*, persist them to `.env` and live-apply the base-mode keys among them.

    The single settings-write path, shared with the base-mode endpoints: every check runs before
    anything is written, so a refused change leaves both `.env` and the running daemon exactly as
    they were. Keys outside `LIVE_KEYS` are persisted only, and reported as `restart_required`.

    Raises `HTTPException` 422 (unknown key, invalid value, or `ntrip_password: null`, which would
    turn "undecided" into anonymous access) and 409 (`base_mode=fixed` with no resolvable site).
    """
    current = ctx.settings
    candidate = _validated(current, updates)
    changed = sorted(key for key in updates if getattr(candidate, key) != getattr(current, key))
    if not changed:
        return ConfigChange([], False)
    if candidate.base_mode is BaseMode.FIXED and not {"base_mode", "active_site"}.isdisjoint(
        changed
    ):
        await require_fixed_site(ctx, candidate.active_site)
    update_env(
        current.mtrtk_env_file,
        {key.upper(): to_env_value(getattr(candidate, key)) for key in changed},
    )
    # Keys only: several of them hold passwords, and this line goes to the daemon's log.
    log.info("configuration updated: %s", ", ".join(changed))
    manager = ctx.basemode
    if manager is None or not set(changed) <= LIVE_KEYS:
        return ConfigChange(changed, True)
    await _apply_live(current, candidate, changed, manager)
    return ConfigChange(changed, False)


async def resolve_fixed_site(ctx: AppContext, name: str | None) -> Site | None:
    """The site a fixed base would sit on - `BaseModeManager._resolve_site`, read-only.

    Same precedence as the manager: the configured name when it names a real row, otherwise
    whichever row is active. Nothing is activated here; `apply_mode()` does that when it applies.
    """
    sites = SitesRepo(ctx.db)
    if name:
        site = await sites.get(name)
        if site is not None:
            return site
    return await sites.active()


async def require_fixed_site(ctx: AppContext, name: str | None) -> Site:
    """`resolve_fixed_site`, but a missing site is a 409 instead of a `None`."""
    site = await resolve_fixed_site(ctx, name)
    if site is None:
        raise HTTPException(409, NO_SITE_DETAIL)
    return site


def _validated(current: Settings, updates: Mapping[str, Any]) -> Settings:
    """`current` with *updates* merged in, validated as a whole. 422 on anything it refuses."""
    unknown = sorted(set(updates) - set(Settings.model_fields))
    if unknown:
        raise HTTPException(422, f"unknown settings: {unknown}")
    if updates.get("ntrip_password", "") is None:
        # `to_env_value(None)` writes `NTRIP_PASSWORD=`, and an empty value *is* a decision:
        # anonymous access. `None` means nobody has decided yet, which the API may not choose for
        # the operator - a base station would come back up serving corrections to anyone.
        raise HTTPException(422, NTRIP_PASSWORD_NULL_DETAIL)
    try:
        # `_env_file=None`: the merged values are the whole truth, and re-reading `.env` here
        # would resurrect keys the caller is in the middle of changing. The ignore is pydantic's
        # `dataclass_transform`, which synthesises an `__init__` from the fields alone and so
        # hides `BaseSettings.__init__`'s own underscore-prefixed parameters from mypy.
        merged = current.model_dump() | dict(updates)
        return Settings(_env_file=None, **merged)  # type: ignore[call-arg]
    except ValidationError as exc:
        raise HTTPException(422, _errors(exc)) from exc


def _errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Pydantic's detail minus `input`: the value that was refused may itself be a secret."""
    return [
        {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]} for err in exc.errors()
    ]


async def _apply_live(
    current: Settings, candidate: Settings, changed: Sequence[str], manager: Any
) -> None:
    """Move the live keys into the running settings and reconfigure the receiver."""
    for key in changed:
        setattr(current, key, getattr(candidate, key))
    manager.mode = current.base_mode
    manager.svin_min_duration_s = current.svin_min_duration_s
    manager.svin_acc_limit_m = current.svin_acc_limit_m
    manager.active_site_name = current.active_site
    await manager.apply_mode()
