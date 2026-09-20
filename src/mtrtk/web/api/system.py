"""GET /api/system - host information and the latest SystemStats."""

from __future__ import annotations

import asyncio
import contextlib
import platform
import socket
import time
from typing import Any

import pyubx2
from fastapi import APIRouter, Request

from mtrtk import __version__
from mtrtk.core.bus import Bus
from mtrtk.core.exposure import tailscale_ipv4
from mtrtk.store.models import SystemStats

router = APIRouter(prefix="/api", tags=["system"])

# `tailscale_ipv4()` walks every interface and every address on it through psutil. The answer
# changes when tailscaled comes up or goes away, which is minutes apart, and `/api/system` is a
# poll a dashboard makes every second or two - so it is read at most this often, in a thread.
TAILSCALE_TTL_S = 60.0


class SystemCache:
    """Remembers the newest SystemStats published on the bus.

    Owned by the app's lifespan (`app.state.system_cache`): one subscription for the life of the
    process, not one per request. `/api/system` is a poll, and a cache built per request would
    always answer `None` - the sampler publishes once a second, long after the response is gone.
    """

    def __init__(self, bus: Bus) -> None:
        self.latest: SystemStats | None = None
        self._bus = bus
        self._sub = bus.subscribe("system.stats", maxsize=5)
        self._task: asyncio.Task[None] | None = None
        self._tailscale_ip: str | None = None
        self._tailscale_at: float | None = None

    async def tailscale_ip(self) -> str | None:
        """The tailscale0 address, re-read at most once every `TAILSCALE_TTL_S`, off the loop."""
        now = time.monotonic()
        if self._tailscale_at is None or now - self._tailscale_at >= TAILSCALE_TTL_S:
            self._tailscale_ip = await asyncio.to_thread(tailscale_ipv4)
            self._tailscale_at = now
        return self._tailscale_ip

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="web-system-cache")

    async def _run(self) -> None:
        async for _, item in self._sub:
            self.latest = item

    def close(self) -> None:
        # `bus.unsubscribe`, never `self._sub.close()` on its own: closing only pushes the
        # sentinel that stops the reader, leaving the subscription registered on the bus so
        # every later publish keeps filling a queue nobody will ever drain.
        self._bus.unsubscribe(self._sub)

    async def aclose(self) -> None:
        """Close, then let the reader task finish so shutdown leaves nothing pending."""
        self.close()
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _cache(request: Request) -> SystemCache | None:
    cache: SystemCache | None = getattr(request.app.state, "system_cache", None)
    return cache


@router.get("/system")
async def system(request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    cache = _cache(request)
    latest = cache.latest if cache is not None else None
    # Without the cache (an app started with no lifespan) the scan is still answered, once.
    ip = (
        await cache.tailscale_ip() if cache is not None else await asyncio.to_thread(tailscale_ipv4)
    )
    return {
        "hostname": socket.gethostname(),
        "tailscale_ip": ip,
        "data_dir": str(ctx.settings.data_dir),
        "stats": latest.model_dump(mode="json") if latest else None,
        "versions": {
            "mtrtk": __version__,
            "python": platform.python_version(),
            "pyubx2": pyubx2.__version__,
        },
    }
