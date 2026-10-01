"""GET /api/status (one-screen summary) and GET /api/state (the full ReceiverState)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request

from mtrtk import __version__
from mtrtk.rover.drivers.ublox import UbloxDriver

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    s = ctx.store.state
    ins = ctx.ins
    # An INS rover has no u-blox controller: its own controller says whether the unit is there.
    controller = ctx.controller if ins is None else ins.controller
    caps = getattr(controller, "capabilities", None)
    caster = ctx.caster
    return {
        "role": ctx.settings.role.value,
        "version": __version__,
        "uptime_s": round(ctx.uptime_s, 1),
        "connected": bool(getattr(controller, "connected", False)),
        "source": (
            ctx.settings.mtrtk_source
            if ins is None or ctx.settings.source_is_file  # an INS replay: the file
            else ctx.settings.ins_port
        ),
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
        "driver": driver_summary(ctx),
    }


def driver_summary(ctx: Any) -> dict[str, Any]:
    """The receiver driver's name and capabilities: the INS driver's, the running rover's, or
    the u-blox one (a base, or a rover not yet started)."""
    ins = ctx.ins
    rover = ctx.rover
    driver = ins.driver if ins is not None else getattr(rover, "driver", None)
    if driver is None:
        return {"name": UbloxDriver.name, "capabilities": asdict(UbloxDriver.capabilities)}
    return {"name": driver.name, "capabilities": asdict(driver.capabilities)}


@router.get("/state")
async def state(request: Request) -> dict[str, Any]:
    # Annotated on the way out: `app.state` is untyped, so the dump would otherwise be `Any`.
    dumped: dict[str, Any] = request.app.state.ctx.store.state.model_dump(mode="json")
    return dumped
