"""GET /api/status (one-screen summary) and GET /api/state (the full ReceiverState)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from mtrtk import __version__

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    s = ctx.store.state
    controller = ctx.controller
    caps = getattr(controller, "capabilities", None)
    caster = ctx.caster
    return {
        "role": ctx.settings.role.value,
        "version": __version__,
        "uptime_s": round(ctx.uptime_s, 1),
        "connected": bool(getattr(controller, "connected", False)),
        "source": ctx.settings.mtrtk_source,
        "firmware": {
            "fw_version": s.firmware.fw_version,
            "protver": s.firmware.protver,
            "module": s.firmware.module,
        },
        "fix": {
            "fix_type_name": s.fix.fix_type_name,
            "carr_soln_name": s.fix.carr_soln_name,
            "num_sv": s.fix.num_sv,
        },
        "position": {
            "lat": s.position.lat,
            "lon": s.position.lon,
            "height_m": s.position.height_m,
        },
        "accuracy": {"h_acc_m": s.accuracy.h_acc_m, "v_acc_m": s.accuracy.v_acc_m},
        "survey_in": {
            "active": s.survey_in.active,
            "valid": s.survey_in.valid,
            "dur_s": s.survey_in.dur_s,
            "mean_acc_m": s.survey_in.mean_acc_m,
        },
        "ntrip_clients": len(getattr(caster, "clients", None) or {}),
        # Callers turned away at `max_clients` are invisible in the live count, and a base whose
        # cap is full looks identical to an idle one without this number.
        "ntrip_rejected": int(getattr(caster, "rejected", 0) or 0),
        "rtcm_bytes_per_s": s.rtcm_out.bytes_per_s,
        "epoch_count": s.epoch_count,
        "capabilities": (
            {"supported": sorted(caps.supported), "unsupported": sorted(caps.unsupported)}
            if caps
            else None
        ),
    }


@router.get("/state")
async def state(request: Request) -> dict[str, Any]:
    # Annotated on the way out: `app.state` is untyped, so the dump would otherwise be `Any`.
    dumped: dict[str, Any] = request.app.state.ctx.store.state.model_dump(mode="json")
    return dumped
