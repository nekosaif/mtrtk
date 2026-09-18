"""Correlates UBX requests (polls, CFG-VALSET, CFG-VALGET) with their responses."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict, deque

from pyubx2 import POLL, POLL_LAYER_RAM, TXN_NONE, UBXMessage

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame
from mtrtk.core.source import ByteSource
from mtrtk.core.ubx_config import CfgItems, CfgValue

log = logging.getLogger(__name__)

CFG_VALSET = (0x06, 0x8A)
CFG_VALGET = (0x06, 0x8B)

# Only answers are routed to the link; live navigation traffic stays off its queue.
RESPONSE_TOPICS = (
    "ubx.ACK-ACK",
    "ubx.ACK-NAK",
    "ubx.CFG-VALGET",
    "ubx.MON-VER",
    "ubx.MON-SPAN",
    "ubx.MON-RF",
    "ubx.MON-COMMS",
    "ubx.MON-HW",
    "ubx.NAV-SIG",
    "ubx.NAV-TIMELS",
    "ubx.RXM-RTCM",
    "ubx.SEC-UNIQID",
)


class LinkTimeout(TimeoutError):
    """The receiver did not answer in time."""


class LinkNak(RuntimeError):
    """The receiver rejected the request."""


def _ack_key(cls: int, mid: int) -> str:
    return f"ack:{cls:02x}{mid:02x}"


class UbxLink:
    """The one writer to the receiver: sends a request, waits for its correlated answer."""

    def __init__(self, source: ByteSource, bus: Bus) -> None:
        self._source = source
        self._bus = bus
        self._sub = bus.subscribe(*RESPONSE_TOPICS, maxsize=500)
        self._waiters: dict[str, deque[asyncio.Future[Frame]]] = defaultdict(deque)
        self._write_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._dispatch(), name="ubxlink-dispatch")

    async def stop(self) -> None:
        self._bus.unsubscribe(self._sub)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for queue in self._waiters.values():
            for fut in queue:
                if not fut.done():
                    fut.set_exception(LinkTimeout("link stopped"))
        self._waiters.clear()

    async def _dispatch(self) -> None:
        async for _, frame in self._sub:
            # A malformed (but checksum-valid) frame must never kill correlation: without
            # this guard the task would die and every later request would silently time out.
            try:
                self._deliver(frame)
            except Exception:
                log.exception("dropping frame the link could not dispatch: %r", frame.raw[:8])

    def _deliver(self, frame: Frame) -> None:
        for key in self._keys_for(frame):
            queue = self._waiters.get(key)
            if queue:
                fut = queue.popleft()
                if not fut.done():
                    fut.set_result(frame)
                return

    @staticmethod
    def _keys_for(frame: Frame) -> tuple[str, ...]:
        ident = frame.identity
        payload = frame.payload
        if ident in ("ACK-ACK", "ACK-NAK") and len(payload) >= 2:
            return (_ack_key(payload[0], payload[1]),)
        return (ident,)

    async def write(self, data: bytes) -> None:
        async with self._write_lock:
            await self._source.write(data)

    async def _request(
        self,
        keys: list[str],
        data: bytes,
        timeout: float,  # noqa: ASYNC109 - a wire deadline, not a caller cancel scope
    ) -> Frame:
        """Register a waiter per acceptable answer, write, and return the first to arrive."""
        loop = asyncio.get_running_loop()
        futures: list[asyncio.Future[Frame]] = [loop.create_future() for _ in keys]
        for key, fut in zip(keys, futures, strict=True):
            self._waiters[key].append(fut)
        try:
            await self.write(data)
            done, _ = await asyncio.wait(
                futures, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                raise LinkTimeout(f"no response for {keys} within {timeout}s")
            # A receiver answers in one burst (CFG-VALGET *then* its ACK-ACK), so several
            # waiters can resolve in the same dispatch batch. `keys` is ordered by
            # preference - the payload-carrying answer first, its bare ACK last.
            return next(fut for fut in futures if fut in done).result()
        finally:
            self._retire(keys, futures)

    def _retire(self, keys: list[str], futures: list[asyncio.Future[Frame]]) -> None:
        for key, fut in zip(keys, futures, strict=True):
            if fut.done():
                if not fut.cancelled():
                    fut.exception()  # the loser of a race: mark its error as retrieved
                continue
            fut.cancel()
            queue = self._waiters.get(key)
            if queue is not None:
                with contextlib.suppress(ValueError):
                    queue.remove(fut)

    async def poll(
        self,
        msg_class: str,
        msg_id: str,
        timeout: float = 2.0,  # noqa: ASYNC109
    ) -> Frame:
        """Poll one message; returns it, or the ACK-NAK frame if the receiver refuses."""
        raw: bytes = UBXMessage(msg_class, msg_id, POLL).serialize()
        return await self._request([msg_id, _ack_key(raw[2], raw[3])], raw, timeout)

    async def valset(
        self,
        items: CfgItems,
        layers: int,
        timeout: float = 2.0,  # noqa: ASYNC109
        retries: int = 3,
    ) -> bool:
        """Apply one CFG-VALSET; True on ACK-ACK, False on ACK-NAK, raises on no answer."""
        raw: bytes = UBXMessage.config_set(layers, TXN_NONE, list(items)).serialize()
        for attempt in range(1, retries + 1):
            try:
                frame = await self._request([_ack_key(*CFG_VALSET)], raw, timeout)
            except LinkTimeout:
                log.warning("CFG-VALSET attempt %d/%d timed out", attempt, retries)
                continue
            return frame.identity == "ACK-ACK"
        raise LinkTimeout("no ACK for CFG-VALSET")

    async def valget(
        self,
        keys: list[str],
        layer: int = POLL_LAYER_RAM,
        timeout: float = 2.0,  # noqa: ASYNC109
    ) -> dict[str, CfgValue]:
        """Read configuration keys back; raises `LinkNak` if the receiver rejects the poll."""
        raw: bytes = UBXMessage.config_poll(layer, 0, list(keys)).serialize()
        frame = await self._request(["CFG-VALGET", _ack_key(*CFG_VALGET)], raw, timeout)
        if frame.identity != "CFG-VALGET":
            raise LinkNak(f"CFG-VALGET rejected for {keys}")
        parsed = frame.parsed()
        return {
            name: value if isinstance(value, bytes) else int(value)
            for name, value in parsed.__dict__.items()
            if name.startswith("CFG_")
        }
