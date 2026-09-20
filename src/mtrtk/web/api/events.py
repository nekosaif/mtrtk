"""The event log: what the daemon thought the operator should know, and the ack that clears it."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request

from mtrtk.store.models import Level
from mtrtk.store.repos import EventsRepo

router = APIRouter(prefix="/api/events", tags=["events"])

# A page for a UI, not an export: the log keeps a year, and a caller that wants all of it should
# ask for it a page at a time rather than in one response a Pi has to build in memory.
MAX_LIMIT = 1000

ACK_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "no event with that id (retention may already have deleted it)"},
}


@router.get("")
async def list_events(
    request: Request,
    limit: int = Query(200, ge=1, le=MAX_LIMIT, description="newest first"),
    # `Annotated` rather than a `Query(...)` default: ruff's B008 exemption for FastAPI
    # parameters does not see through the `Level` alias, and re-spelling the literal here would
    # let the API's idea of a level drift from `store.models.Level` the next time one is added.
    level: Annotated[Level | None, Query(description="info, warning or error")] = None,
) -> list[dict[str, Any]]:
    """Newest first, by insertion order rather than timestamp.

    `ts_utc` comes from the host clock, which steps when NTP or the receiver corrects it, so two
    events written a second apart can carry timestamps a year apart on a base without an RTC.
    The id never goes backwards, so it is what "newest" means here.

    An unrecognised `level` is a 422, not an empty list: a typo in a filter must not look like a
    clean log.
    """
    events = await EventsRepo(request.app.state.ctx.db).list(limit=limit, level=level)
    return [e.model_dump(mode="json") for e in events]


@router.post("/{event_id}/ack", responses=ACK_ERRORS)
async def ack(event_id: int, request: Request) -> dict[str, bool]:
    """Mark one event acknowledged. Idempotent, and it publishes nothing: an ack is a UI gesture
    with no effect on the receiver, and the tab that sent it reads the list back anyway.
    """
    if not await EventsRepo(request.app.state.ctx.db).ack(event_id):
        raise HTTPException(404, f"no event with id {event_id}")
    return {"ok": True}
