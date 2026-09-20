"""GET/PUT /api/config, the one settings-apply path, and POST /api/restart."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from mtrtk.config import BaseMode, Settings
from mtrtk.store.models import Site
from mtrtk.store.repos import SitesRepo
from mtrtk.web.context import AppContext
from mtrtk.web.envfile import read_env, to_env_value, update_env

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["config"])

# Every name here is a real `Settings` field, so the whole GET body can be posted straight
# back: `tunnel_token` was advertised here before it existed, and a form submitting what it
# was shown got a 422 for an unknown key. Phase 9 adds the field and puts it back with it.
SECRET_KEYS = {"ntrip_password", "web_password", "alert_webhook_url"}
# Not secrets themselves, but they carry one in their userinfo: `ntrip://user:pass@host/MP`.
URL_SECRET_KEYS = {"ntrip_url"}
LIVE_KEYS = {"base_mode", "svin_min_duration_s", "svin_acc_limit_m", "active_site"}
# `Settings.model_config` reads `.env` unconditionally, so moving this pointer would leave the
# daemon reading one file while every later PUT wrote another. It is a deployment decision.
READ_ONLY_KEYS = {"mtrtk_env_file"}
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


def _mask_value(key: str, value: Any) -> Any:
    if key in SECRET_KEYS:
        return MASK if value else value
    if key in URL_SECRET_KEYS:
        return mask_url_password(value)
    return value


def _masked(settings: Settings) -> dict[str, Any]:
    values = settings.model_dump(mode="json")
    for key in SECRET_KEYS | URL_SECRET_KEYS:
        values[key] = _mask_value(key, values[key])
    return values


async def _pending(settings: Settings) -> dict[str, Any]:
    """The keys where `.env` and the running process disagree: what a restart would pick up.

    Disagreement is judged on the *parsed* values, so a file that spells the running value
    differently - `DATA_DIR=/data/`, `WEB_ALLOW_INSECURE=1`, `RTCM_MSM=7` - reports nothing.

    Caveat for the compose deployment, which passes `env_file: .env`: those values reach the
    process as environment variables, which outrank the file, so a restarted *container* keeps
    them and only `docker compose up -d` (a recreate) applies the new file. Reporting the
    disagreement anyway is the useful answer - the alternative is showing the operator nothing.
    """
    disk = await asyncio.to_thread(read_env, settings.mtrtk_env_file)
    updates = {
        key.lower(): value
        for key, value in disk.items()
        # `READ_ONLY_KEYS` are left out on purpose: `pending` is a to-do list the UI offers to
        # apply, and a key `PUT /api/config` answers 422 for has no business on it.
        if key.lower() in Settings.model_fields and key.lower() not in READ_ONLY_KEYS
    }
    if not updates:
        return {}
    try:
        on_disk = _merged(settings, updates)
    except ValidationError:
        return {}  # a hand-edited file that will not load: the GET still has to answer
    dumped = on_disk.model_dump(mode="json")
    return {
        key: _mask_value(key, dumped[key])
        for key in updates
        if getattr(on_disk, key) != getattr(settings, key)
    }


@router.get("/config")
async def get_config(request: Request) -> dict[str, Any]:
    ctx: AppContext = request.app.state.ctx
    return {
        "values": _masked(ctx.settings),
        "pending": await _pending(ctx.settings),
        "env_file": str(ctx.settings.mtrtk_env_file),
        "secret_keys": sorted(SECRET_KEYS),
        "live_keys": sorted(LIVE_KEYS),
        "read_only_keys": sorted(READ_ONLY_KEYS),
        "url_secret_keys": sorted(URL_SECRET_KEYS),
    }


@router.put("/config")
async def put_config(body: ConfigBody, request: Request) -> dict[str, Any]:
    ctx: AppContext = request.app.state.ctx
    change = await apply_settings_change(ctx, strip_masks(ctx.settings, body.values))
    return change.as_dict()


def strip_masks(settings: Settings, values: Mapping[str, Any]) -> dict[str, Any]:
    """Undo what `GET` masked, so a form can post back what it was shown.

    A secret that came back as `***` was not edited, so it is dropped rather than written; a URL
    whose password came back as `***` keeps the stored one, so only the rest of it is changed.
    """
    stripped: dict[str, Any] = {}
    for key, value in values.items():
        if key in SECRET_KEYS and value == MASK:
            continue  # "leave it alone", not "set it to ***"
        if key in URL_SECRET_KEYS:
            value = unmask_url_password(value, getattr(settings, key, None))
        stripped[key] = value
    return stripped


def _userinfo(netloc: str) -> tuple[str, str, str] | None:
    """`user`, `password`, `host:port` of a `user:password@host` netloc, or None."""
    if "@" not in netloc:
        return None
    userinfo, _, hostport = netloc.rpartition("@")
    if ":" not in userinfo:
        return None
    user, _, password = userinfo.partition(":")
    return user, password, hostport


def mask_url_password(url: Any) -> Any:
    """`ntrip://user:pass@host/MP` -> `ntrip://user:***@host/MP`; anything else passes through."""
    if not isinstance(url, str) or not url:
        return url
    parts = urlsplit(url)
    split = _userinfo(parts.netloc)
    if split is None:
        return url
    user, _, hostport = split
    return urlunsplit(parts._replace(netloc=f"{user}:{MASK}@{hostport}"))


def unmask_url_password(url: Any, current: Any) -> Any:
    """Splice the stored password back into a URL whose password came back as `***`."""
    if not isinstance(url, str):
        return url
    parts = urlsplit(url)
    split = _userinfo(parts.netloc)
    if split is None or split[1] != MASK:
        return url
    user, _, hostport = split
    stored = _userinfo(urlsplit(current).netloc) if isinstance(current, str) else None
    userinfo = f"{user}:{stored[1]}@" if stored is not None else f"{user}@"
    return urlunsplit(parts._replace(netloc=f"{userinfo}{hostport}"))


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

    A key counts as changed when the running settings disagree with it **or** when `.env` holds a
    different value for it. The second half matters because the running settings are not the whole
    truth: `POST /api/base/sites/{name}/activate` moves them to match the manager it just
    reconfigured, so the follow-up that is meant to make the change durable would otherwise find
    nothing different in memory and write nothing at all. A key the file does not mention is not a
    difference in itself - the process gets it from the environment or the default, and a form
    posting its own values back must stay a no-op - but once something *is* being written, every
    value the request asked for is recorded with it, so the file ends up holding the whole change.

    Raises `HTTPException` 422 (unknown key, a read-only key the request would *change*, an
    invalid value, or `ntrip_password: null`, which would turn "undecided" into anonymous access)
    and 409 (`base_mode=fixed` with no resolvable site).
    """
    current = ctx.settings
    # One settings change at a time: reading `.env`, deciding what moved and writing it back is a
    # read-modify-write, and two of them interleaved would each write a file the other had not
    # been shown. The base-mode endpoints come through here too, so they queue behind a PUT.
    async with ctx.settings_lock:
        updates = _without_unchanged_read_only(current, updates)
        candidate = _validated(current, updates)
        # `read_env` and `update_env` stat, read, write, fsync and rename: on an SD card that is
        # tens of milliseconds with the receiver reader and the caster stopped behind it.
        disk = await asyncio.to_thread(read_env, current.mtrtk_env_file)
        targets = _env_values(candidate, updates)
        changed = sorted(
            key
            for key in updates
            if getattr(candidate, key) != getattr(current, key)
            or (key.upper() in disk and disk[key.upper()] != targets[key])
        )
        if not changed:
            return ConfigChange([], False)
        if candidate.base_mode is BaseMode.FIXED and not {"base_mode", "active_site"}.isdisjoint(
            changed
        ):
            await require_fixed_site(ctx, candidate.active_site)
        env_updates = {
            key.upper(): value for key, value in targets.items() if disk.get(key.upper()) != value
        }
        if env_updates:  # the file may already hold every value; only the process was behind
            await asyncio.to_thread(update_env, current.mtrtk_env_file, env_updates)
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


def _env_values(candidate: Settings, updates: Mapping[str, Any]) -> dict[str, str]:
    """The `.env` string each updated key would hold. 422 on a value a file cannot carry."""
    values: dict[str, str] = {}
    for key in updates:
        try:
            values[key] = to_env_value(getattr(candidate, key))
        except ValueError as exc:  # a newline would smuggle a second assignment into the file
            raise HTTPException(422, f"{key}: {exc}") from exc
    return values


def _without_unchanged_read_only(current: Settings, updates: Mapping[str, Any]) -> dict[str, Any]:
    """Drop the read-only keys *updates* merely echoes back; 422 on one it would actually move.

    `GET /api/config` reports every field, read-only ones included, so a form that posts back what
    it was shown must not be refused for repeating a value nobody edited. Only a request that would
    move the pointer is a request to do something this API may not do.
    """
    posted = sorted(set(updates) & READ_ONLY_KEYS)
    changing = [key for key in posted if not _same_env_value(updates[key], getattr(current, key))]
    if changing:
        raise HTTPException(422, f"read-only settings: {changing}")
    return {key: value for key, value in updates.items() if key not in posted}


def _same_env_value(posted: Any, current: Any) -> bool:
    """True when the posted value would be written as exactly what is stored now."""
    try:
        return to_env_value(posted) == to_env_value(current)
    except ValueError:  # a newline or a `${`: not the stored value, whatever else it is
        return False


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
        return _merged(current, updates)
    except ValidationError as exc:
        raise HTTPException(422, _errors(exc)) from exc


def _merged(current: Settings, updates: Mapping[str, Any]) -> Settings:
    """`current` with *updates* applied, re-validated from scratch.

    `_env_file=None`: the merged values are the whole truth, and re-reading `.env` here would
    resurrect keys the caller is in the middle of changing. The ignore is pydantic's
    `dataclass_transform`, which synthesises an `__init__` from the model's fields alone and so
    hides `BaseSettings.__init__`'s own underscore-prefixed parameters from mypy.
    """
    return Settings(_env_file=None, **(current.model_dump() | dict(updates)))  # type: ignore[call-arg]


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
