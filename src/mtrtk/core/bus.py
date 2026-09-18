"""Single-process asyncio publish/subscribe bus with a bounded queue per subscriber."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from enum import Enum
from typing import Any

_CLOSED = object()


class Policy(Enum):
    DROP_OLDEST = "drop_oldest"  # live consumers: lose the oldest item under pressure
    UNBOUNDED = "unbounded"  # never drop (raw logger); count high-water crossings instead


class Subscription:
    """Receives `(topic, item)` tuples for topics matching its patterns."""

    def __init__(
        self, patterns: tuple[str, ...], policy: Policy, maxsize: int, high_water: int
    ) -> None:
        self.patterns = patterns
        self.policy = policy
        self.high_water = high_water
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self.high_water_hits = 0
        self._closed = False
        self._all = "*" in patterns
        self._prefixes = tuple(p[:-1] for p in patterns if p.endswith(".*"))
        self._exact = frozenset(p for p in patterns if not p.endswith("*"))

    def matches(self, topic: str) -> bool:
        return self._all or topic in self._exact or any(topic.startswith(p) for p in self._prefixes)

    def offer(self, topic: str, item: Any) -> None:
        if self._closed:
            return
        if self.policy is Policy.UNBOUNDED:
            self.queue.put_nowait((topic, item))
            if self.queue.qsize() >= self.high_water:
                self.high_water_hits += 1
            return
        try:
            self.queue.put_nowait((topic, item))
        except asyncio.QueueFull:
            self._evict_one()
            self.dropped += 1
            self.queue.put_nowait((topic, item))

    def _evict_one(self) -> None:
        with contextlib.suppress(asyncio.QueueEmpty):
            self.queue.get_nowait()

    async def get(self) -> tuple[str, Any]:
        return await self.queue.get()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.queue.put_nowait(("__closed__", _CLOSED))
        except asyncio.QueueFull:
            self._evict_one()
            self.queue.put_nowait(("__closed__", _CLOSED))

    def __aiter__(self) -> AsyncIterator[tuple[str, Any]]:
        return self

    async def __anext__(self) -> tuple[str, Any]:
        topic, item = await self.queue.get()
        if item is _CLOSED:
            raise StopAsyncIteration
        return topic, item


class Bus:
    def __init__(self) -> None:
        self._subs: list[Subscription] = []

    def subscribe(
        self,
        *patterns: str,
        maxsize: int = 1000,
        policy: Policy = Policy.DROP_OLDEST,
        high_water: int = 10_000,
    ) -> Subscription:
        size = 0 if policy is Policy.UNBOUNDED else maxsize
        sub = Subscription(tuple(patterns), policy, size, high_water)
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        sub.close()
        self._subs = [s for s in self._subs if s is not sub]

    def publish(self, topic: str, item: Any) -> None:
        for sub in self._subs:
            if sub.matches(topic):
                sub.offer(topic, item)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
