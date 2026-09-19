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


ACK_KEY_PREFIX = "ack:"


def _ack_key(cls: int, mid: int) -> str:
    return f"{ACK_KEY_PREFIX}{cls:02x}{mid:02x}"


class _Waiter:
    """One registered expectation of an answer under one key.

    `robbed` is set when a credit left by an earlier request is spent on an answer that arrived
    while this waiter stood at the head of its key's queue. As far as the link can tell, the
    frame it discarded was this waiter's own answer; so when the request times out in turn,
    nothing late is still owed to it, and it must not leave a credit of its own behind.
    """

    __slots__ = ("future", "robbed")

    def __init__(self, future: asyncio.Future[Frame]) -> None:
        self.future = future
        self.robbed = False


class UbxLink:
    """The one writer to the receiver: sends a request, waits for its correlated answer."""

    def __init__(self, source: ByteSource, bus: Bus) -> None:
        self._source = source
        self._bus = bus
        self._sub = bus.subscribe(*RESPONSE_TOPICS, maxsize=500)
        self._waiters: dict[str, deque[_Waiter]] = defaultdict(deque)
        # Deadlines, per ACK key, for answers still owed to requests that gave up waiting. An
        # ACK carries no tag beyond the class/id it acknowledges, so a late one cannot be told
        # apart from the next request's own answer; one is booked here when a request retires
        # unanswered and spent on the next answer for that key, instead of resolving somebody
        # else's waiter with a stale verdict. Two rules keep a credit from turning into a debt
        # the link can never pay off. It lapses after the timeout of the request that left it:
        # a credit never claimed means the receiver was simply silent. And when it is spent
        # while a request is waiting, that request - whose own answer it has most likely just
        # eaten - is marked robbed and books no credit when it times out in turn. So one lost
        # ACK costs at most one extra timeout, on the request that immediately follows (whose
        # retry is then answered normally), and the loss cannot chain from request to request.
        self._stale: dict[str, deque[float]] = {}
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
            for waiter in queue:
                if not waiter.future.done():
                    waiter.future.set_exception(LinkTimeout("link stopped"))
        self._waiters.clear()
        self._stale.clear()

    async def _dispatch(self) -> None:
        async for _, frame in self._sub:
            # A malformed (but checksum-valid) frame must never kill correlation: without
            # this guard the task would die and every later request would silently time out.
            try:
                self._deliver(frame)
            except Exception:
                log.exception("dropping frame the link could not dispatch: %r", frame.raw[:8])

    def _deliver(self, frame: Frame) -> None:
        now = asyncio.get_running_loop().time()
        for key in self._keys_for(frame):
            queue = self._waiters.get(key)
            if self._spend_stale(key, now):
                # The answer to a request that already gave up: it answers nobody now. The
                # request at the head of the queue, if there is one, has just lost the frame
                # that would have been its own answer - it owes no credit when it times out.
                if queue:
                    queue[0].robbed = True
                return
            if queue:
                waiter = queue.popleft()
                if not waiter.future.done():
                    waiter.future.set_result(frame)
                return

    def _spend_stale(self, key: str, now: float) -> bool:
        """True when this answer is owed to a request that timed out inside its own window."""
        deadlines = self._purge(key, now)
        if deadlines is None:
            return False
        deadlines.popleft()
        if not deadlines:
            del self._stale[key]
        return True

    def _purge(self, key: str, now: float) -> deque[float] | None:
        """Drop the credits for *key* that lapsed unclaimed - the receiver said nothing at all -
        and return the live ones, or None when none is left."""
        deadlines = self._stale.get(key)
        if deadlines is None:
            return None
        while deadlines and deadlines[0] <= now:
            deadlines.popleft()
        if not deadlines:
            del self._stale[key]
            return None
        return deadlines

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
        waiters = [_Waiter(loop.create_future()) for _ in keys]
        for key, waiter in zip(keys, waiters, strict=True):
            self._waiters[key].append(waiter)
        futures = [waiter.future for waiter in waiters]
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
            self._retire(keys, waiters, timeout)

    def _retire(
        self,
        keys: list[str],
        waiters: list[_Waiter],
        timeout: float,  # noqa: ASYNC109 - the wire deadline this request was given
    ) -> None:
        # A request that was answered simply drops the waiters it did not need (a poll answered
        # by its data frame never uses its ACK waiter). A request that got *nothing* - timed out
        # or cancelled - may still be answered later, and that answer has to be discarded rather
        # than handed to the next request for the same key.
        unanswered = not any(
            w.future.done() and not w.future.cancelled() and w.future.exception() is None
            for w in waiters
        )
        now = asyncio.get_running_loop().time()
        for key, waiter in zip(keys, waiters, strict=True):
            fut = waiter.future
            if fut.done():
                if not fut.cancelled():
                    fut.exception()  # the loser of a race: mark its error as retrieved
                continue
            fut.cancel()
            queue = self._waiters.get(key)
            if queue is not None:
                with contextlib.suppress(ValueError):
                    queue.remove(waiter)
            # ACK keys only. A data key is also the identity of the unsolicited periodic message
            # of the same name (MON-RF, NAV-SIG), which would spend the credit on the next
            # sample, and a CFG-VALGET is answered by its data frame, which must keep matching.
            # A robbed waiter books nothing: the credit that robbed it took the answer it was
            # owed, so there is nothing late left to discard on its behalf.
            if unanswered and not waiter.robbed and key.startswith(ACK_KEY_PREFIX):
                deadlines = self._purge(key, now)  # lapsed credits leave with the new booking
                if deadlines is None:
                    deadlines = self._stale[key] = deque()
                deadlines.append(now + timeout)

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
