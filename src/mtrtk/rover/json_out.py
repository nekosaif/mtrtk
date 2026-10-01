"""One compact JSON datagram per epoch for ROS bridges and custom consumers."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mtrtk.core.bus import Bus, Subscription
from mtrtk.core.state import ReceiverState
from mtrtk.rover.sinks import UdpSink

log = logging.getLogger(__name__)


def epoch_json(state: ReceiverState) -> dict[str, Any]:
    p, a, f, v, r = state.position, state.accuracy, state.fix, state.velocity, state.rtk
    return {
        "t": state.time.utc.isoformat() if state.time.utc else None,
        "lat": p.lat,
        "lon": p.lon,
        "height_m": p.height_m,
        "hmsl_m": p.hmsl_m,
        "h_acc_m": a.h_acc_m,
        "v_acc_m": a.v_acc_m,
        "fix_type": f.fix_type,
        "carr_soln": f.carr_soln,
        "num_sv": f.num_sv,
        "vel_n_mps": v.vel_n_mps,
        "vel_e_mps": v.vel_e_mps,
        "vel_d_mps": v.vel_d_mps,
        "heading_deg": v.heading_motion_deg,
        "baseline_m": r.baseline_m,
        "corr_age_s": r.corr_age_s,
        "attitude": state.attitude.model_dump() if state.attitude else None,
    }


class JsonUdpPublisher:
    """Sends `epoch_json(state)` to every target once per `state.epoch`. Restartable."""

    def __init__(self, bus: Bus, targets: list[tuple[str, int]]) -> None:
        self.bus = bus
        self.sink = UdpSink(targets)
        self.sub: Subscription = bus.subscribe("state.epoch", maxsize=20)
        self._sub_closed = False
        self.sent = 0

    async def run(self, stop: asyncio.Event) -> None:
        if self._sub_closed:
            self.sub = self.bus.subscribe("state.epoch", maxsize=20)
            self._sub_closed = False
        await self.sink.start()
        waiter = asyncio.create_task(self._close_on(stop), name="json-udp-stop")
        try:
            async for _, state in self.sub:
                if stop.is_set():
                    break
                try:
                    doc = json.dumps(epoch_json(state), separators=(",", ":"), allow_nan=False)
                except ValueError:  # a NaN/inf somewhere: skip the epoch, never the feed
                    log.debug("JSON epoch skipped: non-finite value")
                    continue
                await self.sink.write(doc.encode())
                self.sent += 1
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            self.stop()
            await self.sink.close()

    async def _close_on(self, stop: asyncio.Event) -> None:
        await stop.wait()
        self.stop()

    def stop(self) -> None:
        if not self._sub_closed:
            self._sub_closed = True
            self.bus.unsubscribe(self.sub)
