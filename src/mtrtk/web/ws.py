"""WebSocket hub: snapshot on connect, one bundled message per receiver epoch, event-driven updates.

One hub per process owns one bus subscription and fans every payload out to the connected
sockets. A socket adds no subscription of its own - only a bounded send queue, its topic set and
its span throttle - so a hundred browsers cost the bus exactly what one does, and a client that
cannot keep up is dropped instead of being allowed to back the bus up behind it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from mtrtk.core.state import ReceiverState
from mtrtk.web.auth import websocket_authorized
from mtrtk.web.context import AppContext

log = logging.getLogger(__name__)

TOPICS = (
    "pvt",
    "sats",
    "rtcm",
    "svin",
    "rf",
    "span",
    "ntrip",
    "events",
    "system",
    "receiver",
    "base",
    "jobs",
    "rawlog",
)
# The four that ride the per-epoch bundle instead of arriving as their own `update`.
EPOCH_TOPICS = frozenset({"pvt", "sats", "rtcm", "svin"})
BUS_TO_TOPIC = {
    "state.hardware": "rf",
    "state.rf": "rf",
    "state.spectrum": "span",
    "ntrip.clients": "ntrip",
    "events.new": "events",
    "system.stats": "system",
    "jobs.update": "jobs",  # nothing publishes it until the job runner lands (Task 9)
}
PREFIX_TO_TOPIC = {"receiver.": "receiver", "base.": "base", "rawlog.": "rawlog"}
BUS_PATTERNS = ("state.epoch", *BUS_TO_TOPIC, *(f"{p}*" for p in PREFIX_TO_TOPIC))

SPAN_MIN_INTERVAL_S = 1.0  # a 256-bin spectrum per RF block is the fattest payload we send
SEND_QUEUE_LIMIT = 50  # messages buffered for one socket before it is dropped
BUS_QUEUE_SIZE = 256  # the hub's own backlog; the fan-out never blocks, so this is slack
SLOW_CLIENT_CODE = 1008  # policy violation - the closest standard code to "you are too slow"
UNAUTHORIZED_CODE = 1008
NO_HUB_CODE = 1011  # internal error: the app was started without its lifespan


class WsLike(Protocol):
    """What the hub needs from a socket: Starlette's `WebSocket` and the test fakes both fit."""

    async def send_json(self, obj: dict[str, Any], /) -> None: ...
    async def receive_text(self) -> str: ...
    async def close(self, code: int = 1000) -> None: ...


def _json(value: Any) -> Any:
    """Convert a bus payload into JSON-ready data. Never returns a live object."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, datetime):  # ISO-8601, UTC, like every other datetime we emit
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return _json(value.value)
    if isinstance(value, Mapping):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_json(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    public = getattr(value, "public", None)  # e.g. ClientInfo.public()
    if callable(public):
        return _json(public())
    fields = getattr(value, "__dataclass_fields__", None)
    if fields is not None:
        return {k: _json(getattr(value, k)) for k in fields}
    return str(value)  # Path, and anything else we have no shape for


def topic_for(bus_topic: str) -> str | None:
    """The client-facing topic a bus topic belongs to, or None if clients never see it."""
    topic = BUS_TO_TOPIC.get(bus_topic)
    if topic is not None:
        return topic
    for prefix, name in PREFIX_TO_TOPIC.items():
        if bus_topic.startswith(prefix):
            return name
    return None


def parse_topics(raw: str) -> set[str]:
    """`?topics=pvt,rf` -> {"pvt", "rf"}. Unknown names are dropped; nothing asked means all."""
    asked = {t.strip() for t in raw.split(",") if t.strip()}
    return asked & set(TOPICS) or set(TOPICS)


def snapshot_message(ctx: AppContext, topics: Iterable[str]) -> dict[str, Any]:
    return {
        "type": "snapshot",
        "role": ctx.settings.role.value,
        "topics": sorted(topics),
        "state": ctx.store.state.model_dump(mode="json"),
    }


def epoch_message(state: ReceiverState, topics: Iterable[str]) -> dict[str, Any]:
    """One bundle per receiver epoch, carrying only the sections this client asked for."""
    wanted = set(topics)
    msg: dict[str, Any] = {
        "type": "epoch",
        "t": state.time.utc.timestamp() if state.time.utc else None,
    }
    if "pvt" in wanted:
        msg["pvt"] = {
            name: getattr(state, name).model_dump(mode="json")
            for name in ("position", "accuracy", "dops", "fix", "velocity", "time")
        }
    if "sats" in wanted:
        msg["sats"] = {
            "sats": [s.model_dump(mode="json") for s in state.sats],
            "sat_summary": state.sat_summary.model_dump(mode="json"),
        }
    if "rtcm" in wanted:
        msg["rtcm"] = state.rtcm_out.model_dump(mode="json")
    if "svin" in wanted:
        msg["svin"] = state.survey_in.model_dump(mode="json")
    return msg


class _Client:
    """One connected socket: a bounded outbox, the topics it wants, its span throttle."""

    def __init__(self, socket: WsLike, topics: set[str]) -> None:
        self.socket = socket
        self.topics = topics
        self.outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SEND_QUEUE_LIMIT)
        self.overflowed = asyncio.Event()
        self.finished = False  # set the moment `serve` starts tearing this socket down
        self._last_span_mono: float | None = None

    def enqueue(self, msg: dict[str, Any]) -> None:
        """Hand a message over without ever waiting: the caller is the shared bus handler."""
        if self.finished or self.overflowed.is_set():
            return
        try:
            self.outbox.put_nowait(msg)
        except asyncio.QueueFull:
            # Dropping messages would leave this client silently out of date, and waiting would
            # stall every other client. Cut it loose; the browser reconnects and re-snapshots.
            self.overflowed.set()

    def span_due(self, now: float) -> bool:
        if self._last_span_mono is not None and now - self._last_span_mono < SPAN_MIN_INTERVAL_S:
            return False
        self._last_span_mono = now
        return True


class WsHub:
    """Fans the bus out to every connected socket over a single subscription.

    Owned by the app's lifespan (`app.state.ws_hub`), exactly like `SystemCache`: built once at
    startup, closed at shutdown. `serve()` starts the reader on demand so a test - or any other
    caller holding a hub of its own - needs no separate `start()`.
    """

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._bus = ctx.bus
        self._sub = ctx.bus.subscribe(*BUS_PATTERNS, maxsize=BUS_QUEUE_SIZE)
        self._clients: set[_Client] = set()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def client_count(self) -> int:
        """Sockets the hub is still serving, teardown included."""
        return len(self._clients)

    def start(self) -> None:
        if self._task is None and not self._closed:
            self._task = asyncio.create_task(self._run(), name="web-ws-hub")

    def close(self) -> None:
        # `bus.unsubscribe`, never `self._sub.close()` on its own: closing only pushes the
        # sentinel that stops the reader, leaving the subscription registered on the bus so
        # every later publish keeps filling a queue nobody will ever drain.
        self._closed = True
        self._bus.unsubscribe(self._sub)

    async def aclose(self) -> None:
        """Close, then let the reader finish so shutdown leaves no pending task behind."""
        self.close()
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ------------------------------------------------------------------ fan-out
    async def _run(self) -> None:
        async for bus_topic, item in self._sub:
            try:
                self._dispatch(bus_topic, item)
            except Exception:  # one malformed payload must not end the fan-out
                log.exception("websocket fan-out failed on %s", bus_topic)
            # One yield per bus message, and never inside `_dispatch`: the senders get to drain
            # between items, so a burst that arrives in a single loop step cannot overflow - and
            # so disconnect - a client that is perfectly able to keep up.
            await asyncio.sleep(0)

    def _dispatch(self, bus_topic: str, item: Any) -> None:
        """Turn one bus payload into per-client messages. Synchronous, start to finish.

        No `await` anywhere in here, which is what makes it safe to read the live objects the
        caster and the state store publish: nothing can mutate them before they are JSON, and no
        slow socket can hold the bus - or another client - up behind it.
        """
        if not self._clients:
            return
        if bus_topic == "state.epoch":
            self._dispatch_epoch(item)
            return
        topic = topic_for(bus_topic)
        if topic is None:
            return
        interested = [c for c in self._clients if topic in c.topics and not c.finished]
        if topic == "span":
            now = time.monotonic()
            interested = [c for c in interested if c.span_due(now)]
        if not interested:
            return
        msg = {"type": "update", "topic": topic, "source": bus_topic, "data": _json(item)}
        for client in interested:
            client.enqueue(msg)

    def _dispatch_epoch(self, state: ReceiverState) -> None:
        # `state.epoch` is a deep copy, so it is safe to read lazily - but it is still dumped
        # once per distinct topic selection rather than once per client.
        built: dict[frozenset[str], dict[str, Any]] = {}
        for client in self._clients:
            wanted = client.topics & EPOCH_TOPICS
            if not wanted or client.finished:
                continue
            key = frozenset(wanted)
            msg = built.get(key)
            if msg is None:
                msg = built[key] = epoch_message(state, wanted)
            client.enqueue(msg)

    # -------------------------------------------------------------- one socket
    async def serve(self, socket: WsLike, topics: set[str]) -> None:
        """Send the snapshot, then stream until the client goes away or falls behind."""
        if self._closed:
            await socket.close(code=NO_HUB_CODE)
            return
        self.start()
        wanted = set(topics) & set(TOPICS) or set(TOPICS)
        client = _Client(socket, wanted)
        # The snapshot rides the same queue as everything else, so a socket that blocks on it is
        # dropped by the same rule instead of hanging this coroutine before the client exists.
        client.enqueue(snapshot_message(self.ctx, wanted))
        self._clients.add(client)
        tasks = (
            asyncio.create_task(self._send(client), name="web-ws-send"),
            asyncio.create_task(self._drain(socket), name="web-ws-recv"),
            asyncio.create_task(client.overflowed.wait(), name="web-ws-overflow"),
        )
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.cancelled():  # `.exception()` would re-raise it and skip the teardown
                    continue
                exc = task.exception()
                if exc is not None and not isinstance(exc, (ConnectionError, WebSocketDisconnect)):
                    log.debug("websocket task ended: %r", exc)
        finally:
            client.finished = True  # stop the fan-out first: nothing is queued from here on
            dropped = client.overflowed.is_set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if dropped:
                log.info("websocket client fell behind (%d queued); closing", SEND_QUEUE_LIMIT)
                with contextlib.suppress(Exception):  # it may already be gone
                    await socket.close(code=SLOW_CLIENT_CODE)
            # Last, so `client_count` means "sockets the hub is still busy with": a caller
            # waiting for it to reach zero knows every `serve` has finished, not merely stopped
            # being fed. `finished` above is what actually detaches it from the fan-out.
            self._clients.discard(client)

    @staticmethod
    async def _send(client: _Client) -> None:
        while True:
            await client.socket.send_json(await client.outbox.get())

    @staticmethod
    async def _drain(socket: WsLike) -> None:
        """Read and discard: clients send nothing, but this is how a disconnect reaches us."""
        while True:
            await socket.receive_text()


async def websocket_endpoint(ws: WebSocket) -> None:
    if not websocket_authorized(ws):
        await ws.close(code=UNAUTHORIZED_CODE)  # before accept: the handshake itself fails
        return
    hub: WsHub | None = getattr(ws.app.state, "ws_hub", None)
    if hub is None:
        log.error("no WebSocket hub on app.state - the app was started without its lifespan")
        await ws.close(code=NO_HUB_CODE)
        return
    await ws.accept()
    topics = parse_topics(ws.query_params.get("topics", ""))
    with contextlib.suppress(WebSocketDisconnect):  # the browser closed while we were sending
        await hub.serve(ws, topics)
