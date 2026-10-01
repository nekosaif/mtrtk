"""Receiver introspection and actions: GET /api/receiver, reapply, reset, poll, profile.

On an INS rover (`ROVER_DRIVER=sbg_ellipse|vectornav`) the same routes act on the INS unit:
`GET` adds its `ins` block (identity, configuration report, status) and reports no u-blox
capabilities; `reset` restarts the unit (a factory reset is refused: too dangerous remotely);
`poll` and `profile {"apply": false}` re-read its configuration; `profile` applies the vendor
profile, which needs `INS_APPLY_CONFIG=1` or an explicit `{"force": true}` (the UI's confirm).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from pyubx2 import UBXMessageError

from mtrtk.core.link import LinkTimeout
from mtrtk.core.receiver import ReceiverError, ResetKind
from mtrtk.web.api.status import driver_summary

router = APIRouter(prefix="/api/receiver", tags=["receiver"])

# Declared so the schema Phase 4 generates its client from carries them; FastAPI only infers
# the 2xx and the validation 422 on its own.
UNREACHABLE: dict[int | str, dict[str, Any]] = {
    409: {"description": "no controller, receiver not connected, passive mode, or link failure"},
    504: {"description": "the receiver did not answer in time"},
}
UNPOLLABLE: dict[int | str, dict[str, Any]] = {
    **UNREACHABLE,
    422: {"description": "no such UBX message, or a malformed body"},
}

NO_CONTROLLER_DETAIL = "no receiver: this daemon runs without a receiver controller"
NOT_CONNECTED_DETAIL = "receiver not connected"
# A replay source swallows every byte written to it, so a reset sent in passive mode would answer
# "ok" having done nothing at all. A poll is read-only on a real receiver, but a file cannot
# answer one either: it would only burn the link timeout and fail. All three are refused.
PASSIVE_DETAIL = "receiver is in passive mode: mtrtk only listens and writes no configuration"


class ResetBody(BaseModel):
    kind: ResetKind


class PollBody(BaseModel):
    msg_class: str
    msg_id: str


class ProfileBody(BaseModel):
    """`POST /api/receiver/profile` (INS rovers): `apply` false re-reads only; `force` applies
    even with `INS_APPLY_CONFIG=0` (written to the unit's RAM, not saved to flash)."""

    apply: bool = True
    force: bool = False


INS_FACTORY_DETAIL = (
    "a factory reset of an INS unit is not offered remotely: it clears the unit's whole "
    "configuration (outputs, lever arms, baud rate) and can strand the link; use the vendor's "
    "tool (sbgCenter / VectorNav Control Center) on site"
)
INS_REAPPLY_DETAIL = (
    "an INS unit has no u-blox profile to re-apply: use POST /api/receiver/profile to apply "
    '(or, with {"apply": false}, re-read) its configuration'
)
UBLOX_PROFILE_DETAIL = (
    "/api/receiver/profile is for INS rovers; the u-blox profile is re-applied with "
    "POST /api/receiver/reapply"
)
INS_APPLY_DETAIL = (
    'INS_APPLY_CONFIG=0: mtrtk only reads the unit\'s configuration. Send {"force": true} '
    "to apply the profile once (written to the unit, not saved to flash), or set "
    "INS_APPLY_CONFIG=1"
)
INS_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {"description": "not an INS rover, unit not connected, apply not allowed, link lost"},
    504: {"description": "the unit did not answer in time"},
}


def _controller(request: Request) -> Any:
    """The live controller, or a 409 saying why this request cannot reach the receiver."""
    controller = request.app.state.ctx.controller
    if controller is None:
        raise HTTPException(409, NO_CONTROLLER_DETAIL)
    if not getattr(controller, "connected", False):
        raise HTTPException(409, NOT_CONNECTED_DETAIL)
    if getattr(controller, "passive", False):
        raise HTTPException(409, PASSIVE_DETAIL)
    return controller


def _ins(request: Request) -> Any:
    """The INS bundle when this daemon runs one, else None."""
    return request.app.state.ctx.ins


def _ins_ready(ins: Any) -> Any:
    if not ins.connected:
        raise HTTPException(409, NOT_CONNECTED_DETAIL)
    return ins


def _ins_failed(exc: Exception) -> HTTPException:
    """A command that went unanswered is a gateway timeout; a link that dropped (or any
    vendor refusal that escaped the report) is a conflict with the unit's state."""
    if isinstance(exc, TimeoutError) or getattr(exc, "code", 0) is None:
        return HTTPException(504, f"INS unit did not answer: {exc}")
    return HTTPException(409, str(exc) or type(exc).__name__)


async def _ins_configure(ins: Any, *, apply: bool) -> dict[str, Any]:
    try:
        await ins.configure(apply=apply)
    except Exception as exc:  # ConnectionError mostly; the report holds unit-side refusals
        raise _ins_failed(exc) from exc
    return {"ok": True, "applied": apply, "report": ins.report_dict()}


def _failed(exc: Exception) -> HTTPException:
    """Map a receiver failure onto a status code.

    `LinkTimeout` is a `TimeoutError`, hence an `OSError`, so it has to be recognised before
    the link-failure case: silence from the receiver is a gateway timeout, not a bad request.
    """
    if isinstance(exc, LinkTimeout):
        return HTTPException(504, f"receiver did not answer: {exc}")
    return HTTPException(409, str(exc))


def _caps_dict(caps: Any) -> dict[str, Any]:
    return {
        "protver": caps.protver,
        "fw_version": caps.fw_version,
        "module": caps.module,
        # Sets have no order of their own; sorted so the same capabilities always look the same.
        "supported": sorted(caps.supported),
        "unsupported": sorted(caps.unsupported),
    }


@router.get("")
async def get_receiver(request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    ins = ctx.ins
    if ins is not None:
        return {
            "connected": bool(ins.connected),
            "passive": False,
            "source": ctx.settings.ins_port,
            "capabilities": None,  # u-blox capabilities: an INS unit has none
            "firmware": ctx.store.state.firmware.model_dump(mode="json"),
            "driver": driver_summary(ctx),
            "ins": ins.as_dict(),
        }
    controller = ctx.controller
    caps = getattr(controller, "capabilities", None)
    return {
        "connected": bool(getattr(controller, "connected", False)),
        "passive": bool(getattr(controller, "passive", ctx.settings.source_is_file)),
        "source": ctx.settings.mtrtk_source,
        "capabilities": _caps_dict(caps) if caps is not None else None,
        "firmware": ctx.store.state.firmware.model_dump(mode="json"),
        "driver": driver_summary(ctx),
        "ins": None,
    }


@router.post("/reapply", responses=UNREACHABLE)
async def reapply(request: Request) -> dict[str, Any]:
    if _ins(request) is not None:
        raise HTTPException(409, INS_REAPPLY_DETAIL)
    controller = _controller(request)
    try:
        caps = await controller.reapply()
    except (ReceiverError, OSError) as exc:
        raise _failed(exc) from exc
    return {"ok": True, "capabilities": _caps_dict(caps)}


@router.post("/reset", responses=UNREACHABLE)
async def reset(body: ResetBody, request: Request) -> dict[str, Any]:
    ins = _ins(request)
    if ins is not None:
        if body.kind == "factory":
            raise HTTPException(409, INS_FACTORY_DETAIL)
        _ins_ready(ins)
        try:
            await ins.reset()  # hot / warm / cold alike: the unit restarts, settings kept
        except Exception as exc:
            raise _ins_failed(exc) from exc
        request.app.state.ctx.bus.publish("receiver.reset", {"kind": body.kind})
        return {"ok": True, "kind": body.kind}
    controller = _controller(request)
    try:
        await controller.reset(body.kind)
    except (ReceiverError, ValueError, OSError) as exc:
        raise _failed(exc) from exc
    return {"ok": True, "kind": body.kind}


@router.post("/poll", responses=UNPOLLABLE)
async def poll(body: PollBody, request: Request) -> dict[str, Any]:
    ins = _ins(request)
    if ins is not None:  # nothing to poll by name: re-read the unit's configuration instead
        return await _ins_configure(_ins_ready(ins), apply=False)
    controller = _controller(request)
    try:
        polled: dict[str, Any] = await controller.poll(body.msg_class, body.msg_id)
    except (ReceiverError, OSError) as exc:
        raise _failed(exc) from exc
    # Exactly what an unknown message name raises out of pyubx2, and nothing wider: a bug of
    # ours is not a bad request, and answering 422 for it would bury the traceback that names it.
    except (UBXMessageError, KeyError, ValueError) as exc:
        raise HTTPException(422, f"cannot poll {body.msg_id}: {exc}") from exc
    return polled


@router.post("/profile", responses=INS_RESPONSES)
async def profile(body: ProfileBody, request: Request) -> dict[str, Any]:
    """Apply (or with `apply: false`, re-read) the INS unit's profile; returns the report."""
    ins = _ins(request)
    if ins is None:
        raise HTTPException(409, UBLOX_PROFILE_DETAIL)
    _ins_ready(ins)
    if body.apply and not (request.app.state.ctx.settings.ins_apply_config or body.force):
        raise HTTPException(409, INS_APPLY_DETAIL)
    return await _ins_configure(ins, apply=body.apply)
