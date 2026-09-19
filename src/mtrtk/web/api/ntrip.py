"""NTRIP caster status, live clients and connection history."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from mtrtk.config import BIND_MODES, Settings
from mtrtk.store.repos import NtripLogRepo

router = APIRouter(prefix="/api/ntrip", tags=["ntrip"])

# `tailscale`, `lan` and `all` name an interface, not an address: which one the caster ends up
# on is only known once it has bound. Printing a placeholder is better than printing the word
# "tailscale" where a rover expects a host.
BIND_PLACEHOLDER = "<bind-address>"
MASK = "***"  # the password never leaves the process; `.env` is where the operator reads it back


def _configured_host(settings: Settings) -> str:
    """The address the caster would bind, where the configuration already names one."""
    return BIND_PLACEHOLDER if settings.ntrip_bind in BIND_MODES else settings.ntrip_bind


@router.get("")
async def info(request: Request) -> dict[str, Any]:
    """What a rover needs in order to connect, and what the caster is doing right now.

    A read, not a command: with no caster (the rover role, a replay, or a base whose interface
    has not come up yet) this answers `running: false` with the configured mountpoint, port and
    host and `null` for everything only a running caster can know - rather than a 409, which
    would leave the UI unable to show the connection details at all.
    """
    ctx = request.app.state.ctx
    s: Settings = ctx.settings
    caster = ctx.caster
    host = getattr(caster, "host", None) or _configured_host(s)
    port = getattr(caster, "port", None) or s.ntrip_port
    anonymous = s.ntrip_anonymous
    credentials = "" if anonymous else f"{s.ntrip_user}:{MASK}@"
    return {
        "running": caster is not None,
        "host": host,
        "port": port,
        "bind_mode": s.ntrip_bind,
        "mountpoint": s.mountpoint,
        "anonymous": anonymous,
        "username": None if anonymous else s.ntrip_user,
        "connection_url": f"ntrip://{credentials}{host}:{port}/{s.mountpoint}",
        "clients": len(caster.clients) if caster is not None else None,
        # The cap is configuration, so it is knowable either way; the rejections are not.
        "max_clients": getattr(caster, "max_clients", None) or s.ntrip_max_clients,
        "rejected": int(getattr(caster, "rejected", 0) or 0) if caster is not None else None,
        "sourcetable": caster.sourcetable_body().decode("latin-1") if caster is not None else None,
    }


@router.get("/clients")
async def clients(request: Request) -> list[dict[str, Any]]:
    """The rovers connected now. No caster means no clients, not an error."""
    caster = request.app.state.ctx.caster
    if caster is None:
        return []
    return [c.public() for c in caster.clients.values()]


@router.get("/history")
async def history(request: Request, limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
    """Past connections, newest first - including those made before this daemon started."""
    rows = await NtripLogRepo(request.app.state.ctx.db).recent(limit)
    return [r.model_dump(mode="json") for r in rows]
