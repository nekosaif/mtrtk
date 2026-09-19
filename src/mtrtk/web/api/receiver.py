"""Receiver introspection and actions: GET /api/receiver, reapply, reset, poll."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from mtrtk.core.link import LinkTimeout
from mtrtk.core.receiver import ReceiverError, ResetKind

router = APIRouter(prefix="/api/receiver", tags=["receiver"])

NO_CONTROLLER_DETAIL = "no receiver: this daemon runs without a receiver controller"
NOT_CONNECTED_DETAIL = "receiver not connected"


class ResetBody(BaseModel):
    kind: ResetKind


class PollBody(BaseModel):
    msg_class: str
    msg_id: str


def _controller(request: Request) -> Any:
    controller = request.app.state.ctx.controller
    if controller is None:
        raise HTTPException(409, NO_CONTROLLER_DETAIL)
    if not getattr(controller, "connected", False):
        raise HTTPException(409, NOT_CONNECTED_DETAIL)
    return controller


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
    controller = ctx.controller
    caps = getattr(controller, "capabilities", None)
    return {
        "connected": bool(getattr(controller, "connected", False)),
        "passive": bool(getattr(controller, "passive", ctx.settings.source_is_file)),
        "source": ctx.settings.mtrtk_source,
        "capabilities": _caps_dict(caps) if caps is not None else None,
        "firmware": ctx.store.state.firmware.model_dump(mode="json"),
    }


@router.post("/reapply")
async def reapply(request: Request) -> dict[str, Any]:
    controller = _controller(request)
    try:
        caps = await controller.reapply()
    except (ReceiverError, OSError) as exc:
        raise _failed(exc) from exc
    return {"ok": True, "capabilities": _caps_dict(caps)}


@router.post("/reset")
async def reset(body: ResetBody, request: Request) -> dict[str, Any]:
    controller = _controller(request)
    try:
        await controller.reset(body.kind)
    except (ReceiverError, ValueError, OSError) as exc:
        raise _failed(exc) from exc
    return {"ok": True, "kind": body.kind}


@router.post("/poll")
async def poll(body: PollBody, request: Request) -> dict[str, Any]:
    controller = _controller(request)
    try:
        polled: dict[str, Any] = await controller.poll(body.msg_class, body.msg_id)
    except (ReceiverError, OSError) as exc:
        raise _failed(exc) from exc
    except Exception as exc:  # an unknown message name raises out of pyubx2
        raise HTTPException(422, f"cannot poll {body.msg_id}: {exc}") from exc
    return polled
