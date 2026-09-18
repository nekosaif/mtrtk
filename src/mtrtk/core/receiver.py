"""Owns the receiver connection: open/reconnect, capability probe, profile apply and verify."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from mtrtk.core.bus import Bus
from mtrtk.core.link import LinkNak, LinkTimeout, UbxLink
from mtrtk.core.router import Router
from mtrtk.core.source import ByteSource
from mtrtk.core.statestore import StateStore
from mtrtk.core.ubx_config import (
    LAYERS_ALL,
    LAYERS_RAM,
    CfgItems,
    CfgValue,
    Profile,
    chunked,
)

log = logging.getLogger(__name__)

PROBE_POLLS: dict[str, tuple[str, str]] = {
    "MON-SPAN": ("MON", "MON-SPAN"),
    "MON-COMMS": ("MON", "MON-COMMS"),
    "NAV-TIMELS": ("NAV", "NAV-TIMELS"),
}
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 30.0


class ReceiverError(RuntimeError):
    pass


class ProfileError(ReceiverError):
    pass


class SourceEnded(Exception):
    """The byte source reached EOF (file replay finished)."""


@dataclass
class Capabilities:
    protver: str = ""
    fw_version: str = ""
    module: str = ""
    supported: set[str] = field(default_factory=set)
    unsupported: set[str] = field(default_factory=set)


class ReceiverController:
    def __init__(
        self,
        bus: Bus,
        source_factory: Callable[[], ByteSource],
        profile: Profile | None,
        strict: bool = True,
        passive: bool = False,
        rx_timeout_s: float = 5.0,
    ) -> None:
        self.bus = bus
        self._source_factory = source_factory
        self.profile = profile
        self.strict = strict
        self.passive = passive
        self.rx_timeout_s = rx_timeout_s
        self.capabilities = Capabilities()
        self.connected = False
        self.link: UbxLink | None = None
        self._first_apply = True
        self._last_rx = 0.0

    # ------------------------------------------------------------- lifecycle
    async def run(self, stop: asyncio.Event) -> None:
        backoff = BACKOFF_MIN_S
        while not stop.is_set():
            source = self._source_factory()
            try:
                await source.open()
            except (OSError, ValueError) as exc:  # serial errors derive from OSError/ValueError
                log.warning("cannot open %s: %s (retry in %.0fs)", source.name, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
                continue
            ended, failed = await self._session(source, stop)
            if ended or stop.is_set():
                return
            if failed:
                log.warning("reconnecting in %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
            else:
                backoff = BACKOFF_MIN_S

    async def _session(self, source: ByteSource, stop: asyncio.Event) -> tuple[bool, bool]:
        """Run one connection.

        Returns (ended_for_good, failed): EOF ends the run, failures reconnect with backoff.
        """
        router = Router(self.bus)
        link = UbxLink(source, self.bus)
        await link.start()
        self.link = link
        self.connected = True
        self._last_rx = time.monotonic()
        self.bus.publish("receiver.connected", source.name)
        reader = asyncio.create_task(self._read_loop(source, router), name="receiver-read")
        reason = "stopped"
        ended = failed = False
        try:
            if not self.passive and self.profile is not None:
                await self.configure(link, first=self._first_apply)
                self._first_apply = False
            await self._watchdog(reader, stop)
        except SourceEnded:
            reason, ended = "source ended", True
        except ReceiverError as exc:
            reason, failed = str(exc), True
            log.error("receiver error: %s", exc)
            self.bus.publish("receiver.error", reason)
        except (OSError, LinkTimeout) as exc:
            reason, failed = f"link failure: {exc}", True
            log.warning(reason)
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            await link.stop()
            await source.close()
            self.link = None
            self.connected = False
            self.bus.publish("receiver.disconnected", reason)
        return ended, failed

    async def _read_loop(self, source: ByteSource, router: Router) -> None:
        while True:
            data = await source.read()
            if not data:
                raise SourceEnded
            self._last_rx = time.monotonic()
            router.feed(data)

    async def _watchdog(self, reader: asyncio.Task[None], stop: asyncio.Event) -> None:
        stop_task = asyncio.create_task(stop.wait(), name="receiver-stop")
        try:
            while True:
                done, _ = await asyncio.wait(
                    {reader, stop_task}, timeout=1.0, return_when=asyncio.FIRST_COMPLETED
                )
                if stop_task in done:
                    return
                if reader in done:
                    exc = reader.exception()
                    if isinstance(exc, SourceEnded):
                        raise exc
                    raise ReceiverError(f"reader failed: {exc!r}")
                if time.monotonic() - self._last_rx > self.rx_timeout_s:
                    raise ReceiverError(f"no data from receiver for {self.rx_timeout_s:.0f}s")
        finally:
            stop_task.cancel()

    # ------------------------------------------------------------- configure
    async def probe(self, link: UbxLink) -> Capabilities:
        caps = Capabilities()
        frame = await link.poll("MON", "MON-VER")
        if frame.identity == "MON-VER":
            store = StateStore()
            store.apply(frame)
            fw = store.state.firmware
            caps.protver, caps.fw_version, caps.module = fw.protver, fw.fw_version, fw.module
        for feature, (cls, mid) in PROBE_POLLS.items():
            try:
                answer = await link.poll(cls, mid, timeout=1.0)
            except LinkTimeout:
                caps.unsupported.add(feature)
                continue
            (caps.supported if answer.identity == mid else caps.unsupported).add(feature)
        log.info(
            "receiver %s fw=%s protver=%s unsupported=%s",
            caps.module,
            caps.fw_version,
            caps.protver,
            sorted(caps.unsupported),
        )
        return caps

    async def configure(self, link: UbxLink, first: bool) -> Capabilities:
        if self.profile is None:
            raise ProfileError("no profile to apply")
        profile = self.profile
        layers = LAYERS_ALL if first else LAYERS_RAM
        caps = await self.probe(link)

        rejected = await self._apply_with_bisect(link, profile.core, layers)
        if rejected:
            message = f"receiver rejected core config keys: {rejected}"
            if self.strict:
                raise ProfileError(message)
            log.warning("%s (continuing: RECEIVER_STRICT=0)", message)

        current = await self._readback(link, [k for k, _ in profile.signals])
        if any(current.get(k) != v for k, v in profile.signals):
            log.info("signal configuration differs; rewriting (GNSS engine restarts)")
            if not await link.valset(profile.signals, layers):
                raise ProfileError("receiver rejected the signal configuration")

        for feature, items in profile.optional.items():
            if feature in caps.unsupported:
                continue
            try:
                ok = await link.valset(items, layers)
            except LinkTimeout:
                ok = False
            (caps.supported if ok else caps.unsupported).add(feature)
            if not ok:
                caps.supported.discard(feature)
                log.info("optional feature %s not accepted by this firmware", feature)

        mismatches = await self.verify(link, profile, skip=set(rejected))
        if mismatches:
            raise ProfileError(f"configuration verification failed: {mismatches}")
        self.capabilities = caps
        self.bus.publish("receiver.capabilities", caps)
        return caps

    async def apply_items(self, items: CfgItems, layers: int = LAYERS_ALL) -> bool:
        if self.link is None:
            raise ReceiverError("receiver not connected")
        return await self.link.valset(items, layers)

    async def verify(
        self, link: UbxLink, profile: Profile, skip: set[str] | None = None
    ) -> dict[str, tuple[CfgValue, CfgValue | None]]:
        wanted = {k: v for k, v in [*profile.core, *profile.signals] if not skip or k not in skip}
        got = await self._readback(link, list(wanted))
        return {k: (v, got.get(k)) for k, v in wanted.items() if got.get(k) != v}

    async def _readback(self, link: UbxLink, keys: list[str]) -> dict[str, CfgValue]:
        result: dict[str, CfgValue] = {}
        for chunk in chunked([(k, 0) for k in keys]):
            names = [k for k, _ in chunk]
            try:
                result.update(await link.valget(names))
            except LinkNak:
                for name in names:  # isolate unknown keys one by one
                    try:
                        result.update(await link.valget([name]))
                    except LinkNak:
                        log.debug("VALGET rejected for %s", name)
        return result

    async def _apply_with_bisect(self, link: UbxLink, items: CfgItems, layers: int) -> list[str]:
        rejected: list[str] = []
        for chunk in chunked(items):
            if not await link.valset(chunk, layers):
                rejected += await self._bisect(link, chunk, layers)
        return rejected

    async def _bisect(self, link: UbxLink, items: CfgItems, layers: int) -> list[str]:
        if len(items) == 1:
            return [items[0][0]]
        mid = len(items) // 2
        rejected: list[str] = []
        for half in (items[:mid], items[mid:]):
            if not await link.valset(half, layers):
                rejected += await self._bisect(link, half, layers)
        return rejected
