"""GET /api/status - one-screen summary."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/status")
async def status(request: Request) -> dict[str, object]:
    ctx = request.app.state.ctx
    return {"role": ctx.settings.role.value, "uptime_s": round(ctx.uptime_s, 1)}
