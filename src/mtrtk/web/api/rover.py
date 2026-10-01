"""Rover role: NTRIP client, RTK status, outputs, sessions and survey points.

Every route answers 409 on a daemon that is not running in the rover role (`AppContext.rover`
is None): there is no NTRIP client, collector or session store to talk to.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from mtrtk.rover.exports import to_csv, to_geojson, to_gpx, to_kml
from mtrtk.rover.ntrip_client import NtripClientConfig
from mtrtk.web.api.config import apply_settings_change, mask_url_password, unmask_url_password
from mtrtk.web.context import AppContext

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rover", tags=["rover"])

NOT_ROVER_DETAIL = "daemon is not running in the rover role"
NAME_MAX = 128
NOTE_MAX = 2000
POINTS_LIST_MAX = 10_000
# An export is the whole job, not a page of it: a season of points is a few thousand rows.
EXPORT_LIMIT = 100_000

ExportFormat = Literal["csv", "geojson", "kml", "gpx"]
_EXPORT_TYPES: dict[str, tuple[str, str]] = {
    "csv": ("text/csv", "csv"),
    "geojson": ("application/geo+json", "geojson"),
    "kml": ("application/vnd.google-earth.kml+xml", "kml"),
    "gpx": ("application/gpx+xml", "gpx"),
}


class NtripUrlBody(BaseModel):
    url: str = Field(min_length=1)


class SessionBody(BaseModel):
    name: str | None = Field(None, max_length=NAME_MAX)
    notes: str | None = Field(None, max_length=NOTE_MAX)


class CollectBody(BaseModel):
    name: str = Field(max_length=NAME_MAX)
    code: str | None = Field(None, max_length=NAME_MAX)
    note: str | None = Field(None, max_length=NOTE_MAX)
    epochs: int | None = None  # None: POINT_EPOCHS; the collector refuses outside 1-3600
    fixed_only: bool | None = None  # None: POINT_FIXED_ONLY


class PointPatch(BaseModel):
    """`None` (or a missing key) leaves a field as it is; `""` empties `code` or `note`."""

    name: str | None = Field(None, max_length=NAME_MAX)
    code: str | None = Field(None, max_length=NAME_MAX)
    note: str | None = Field(None, max_length=NOTE_MAX)


def _ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


def _rover(request: Request) -> Any:
    rover = _ctx(request).rover
    if rover is None:
        raise HTTPException(409, NOT_ROVER_DETAIL)
    return rover


def _dump(obj: Any) -> Any:
    """JSON-ready data from a pydantic model, a dataclass or a plain value."""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _dump(getattr(obj, k)) for k in obj.__dataclass_fields__}
    return obj


def _age(mono: float | None, now: float) -> float | None:
    return None if mono is None else max(0.0, now - mono)


def _ntrip(rover: Any) -> dict[str, Any] | None:
    client = getattr(rover, "ntrip_client", None)
    if client is None:
        return None
    status = client.status
    out: dict[str, Any] = _dump(status)
    # The `*_mono` fields are this process's monotonic clock, meaningless to a browser.
    now = time.monotonic()
    out["last_rtcm_age_s"] = _age(status.last_rtcm_mono, now)
    out["connected_for_s"] = _age(status.since_mono, now)
    return out


def _outputs(ctx: AppContext, rover: Any) -> dict[str, Any]:
    nmea = getattr(rover, "nmea", None)
    sinks = getattr(nmea, "sinks", None) or []
    # The TCP broadcast server is the only sink with clients to count.
    tcp = next((s for s in sinks if hasattr(s, "client_count")), None)
    settings = ctx.settings
    return {
        "nmea_tcp": {"port": tcp.port, "clients": tcp.client_count} if tcp else None,
        "nmea_udp": list(settings.nmea_udp_targets),
        "nmea_serial": settings.nmea_serial,
        "json_udp": settings.json_udp_port,
        "sentences": sorted(getattr(nmea, "sentences", None) or []),
    }


@router.get("")
async def overview(request: Request) -> dict[str, Any]:
    ctx = _ctx(request)
    rover = _rover(request)
    return {
        "role": ctx.settings.role.value,
        "driver": {"name": rover.driver.name, "capabilities": _dump(rover.driver.capabilities)},
        "ntrip": _ntrip(rover),
        # The configured caster, password masked; `ntrip` above is what the client is doing.
        "ntrip_url": mask_url_password(ctx.settings.ntrip_url),
        "rtk": ctx.store.state.rtk.model_dump(mode="json"),
        "outputs": _outputs(ctx, rover),
        "session": _dump(await rover.sessions_repo.current()),
        "collect": rover.collector.status.model_dump(mode="json"),
    }


@router.put("/ntrip")
async def set_ntrip(body: NtripUrlBody, request: Request) -> dict[str, Any]:
    """Validate the caster URL, write `NTRIP_URL` to `.env` and restart the client on it.

    A URL whose password comes back as `***` (the form posting what `GET /api/rover` showed it)
    keeps the stored password.
    """
    ctx = _ctx(request)
    rover = _rover(request)
    async with ctx.rover_ntrip_lock:
        url = unmask_url_password(body.url.strip(), ctx.settings.ntrip_url)
        try:
            NtripClientConfig.from_url(url)
        except ValueError as exc:
            # The parser's own message, never the URL: it may carry the password.
            raise HTTPException(422, f"url: {exc}") from exc
        # The one settings-write path: same validation, same lock, same `.env` writer.
        await apply_settings_change(ctx, {"ntrip_url": url})
        # Applied live below, so the running settings follow the file: nothing is pending.
        ctx.settings.ntrip_url = url
        await rover.set_ntrip_url(url)
    log.info("NTRIP client restarted on a new caster URL")
    return {"ok": True, "url": mask_url_password(url)}


@router.get("/sessions")
async def sessions(
    request: Request, limit: int = Query(100, ge=1, le=1000)
) -> list[dict[str, Any]]:
    """Newest first."""
    return [_dump(s) for s in await _rover(request).sessions_repo.list(limit)]


@router.post("/sessions")
async def start_session(body: SessionBody, request: Request) -> dict[str, Any]:
    """Open a session (closing the open one); points collected from now on belong to it."""
    rover = _rover(request)
    role = _ctx(request).settings.role.value
    session = await rover.sessions_repo.start(body.name, role, body.notes)
    result: dict[str, Any] = _dump(session)
    return result


@router.post("/sessions/stop")
async def stop_session(request: Request) -> dict[str, Any] | None:
    """Close the open session; `null` when none was open."""
    result: dict[str, Any] | None = _dump(await _rover(request).sessions_repo.stop())
    return result


@router.get("/collect")
async def collect_status(request: Request) -> dict[str, Any]:
    result: dict[str, Any] = _rover(request).collector.status.model_dump(mode="json")
    return result


@router.post("/collect")
async def collect_start(body: CollectBody, request: Request) -> dict[str, Any]:
    """Start averaging a point. 409 while one is being collected; 422 on a bad name or count."""
    collector = _rover(request).collector
    try:
        status = await collector.start(
            body.name, body.code, body.note, body.epochs, body.fixed_only
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    result: dict[str, Any] = status.model_dump(mode="json")
    return result


@router.delete("/collect")
async def collect_cancel(request: Request) -> dict[str, Any]:
    """Abandon the point being collected; a no-op when none is (the status says which)."""
    collector = _rover(request).collector
    collector.cancel()
    result: dict[str, Any] = collector.status.model_dump(mode="json")
    return result


@router.get("/points")
async def points(
    request: Request,
    session_id: int | None = None,
    limit: int = Query(1000, ge=1, le=POINTS_LIST_MAX),
) -> list[dict[str, Any]]:
    """Newest first; only `session_id`'s points when it is given."""
    return [_dump(p) for p in await _rover(request).points_repo.list(session_id, limit)]


# Declared before `/points/{point_id}`: FastAPI matches in declaration order, and `export` would
# otherwise be taken for a point id.
@router.get("/points/export")
async def export_points(
    request: Request, fmt: ExportFormat = "csv", session_id: int | None = None
) -> Response:
    """Every point (or `session_id`'s), oldest first, as a download."""
    rover = _rover(request)
    pts = list(reversed(await rover.points_repo.list(session_id, EXPORT_LIMIT)))
    if fmt == "csv":
        text = to_csv(pts)
    elif fmt == "geojson":
        text = json.dumps(to_geojson(pts))
    elif fmt == "kml":
        text = to_kml(pts)
    else:
        text = to_gpx(pts)
    media_type, ext = _EXPORT_TYPES[fmt]
    stem = f"{_ctx(request).settings.station_id}-points"
    if session_id is not None:
        stem += f"-session-{session_id}"
    return Response(
        text,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{stem}.{ext}"'},
    )


@router.patch("/points/{point_id}")
async def patch_point(point_id: int, body: PointPatch, request: Request) -> dict[str, Any]:
    rover = _rover(request)
    name = body.name.strip() if body.name is not None else None
    if name == "":
        raise HTTPException(422, "a point needs a name")
    try:
        point = await rover.points_repo.update(point_id, name, body.code, body.note)
    except KeyError as exc:
        raise HTTPException(404, "point not found") from exc
    result: dict[str, Any] = _dump(point)
    return result


@router.delete("/points/{point_id}")
async def delete_point(point_id: int, request: Request) -> dict[str, bool]:
    if not await _rover(request).points_repo.delete(point_id):
        raise HTTPException(404, "point not found")
    return {"ok": True}
