"""Owns the receiver connection: open/reconnect, capability probe, profile apply and verify."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from mtrtk.core.bus import Bus
from mtrtk.core.link import LinkNak, LinkTimeout, UbxLink
from mtrtk.core.router import Router
from mtrtk.core.source import ByteSource
from mtrtk.core.statestore import StateStore
from mtrtk.core.ubx_config import (
    LAYERS_ALL,
    LAYERS_RAM,
    OPTIONAL_FEATURES,
    CfgItems,
    CfgValue,
    Profile,
    chunked,
)

log = logging.getLogger(__name__)

BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 30.0
WATCHDOG_TICK_S = 1.0

Sleeper = Callable[[float], Awaitable[None]]


async def _quietly(what: str, closing: Awaitable[None]) -> None:
    """Await a teardown step: it must never mask the failure that caused the teardown."""
    try:
        await closing
    except Exception:
        log.warning("ignoring %s failure while disconnecting", what, exc_info=True)


class ReceiverError(RuntimeError):
    pass


class ProfileError(ReceiverError):
    pass


class LinkDropped(ReceiverError):
    """A source that does not end at EOF returned no bytes: the device went away."""


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
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self.bus = bus
        self._source_factory = source_factory
        self.profile = profile
        self.strict = strict
        self.passive = passive
        self.rx_timeout_s = rx_timeout_s
        # Injected so a test can collapse the backoff ladder without patching the shared
        # `asyncio.sleep` out from under every other coroutine in the process.
        self._sleep = sleep
        self.capabilities = Capabilities()
        self.connected = False
        self.link: UbxLink | None = None
        self._first_apply = True
        self._last_rx = 0.0
        self._backoff = BACKOFF_MIN_S

    # ------------------------------------------------------------- lifecycle
    async def run(self, stop: asyncio.Event) -> None:
        self._backoff = BACKOFF_MIN_S
        while not stop.is_set():
            source = self._source_factory()
            try:
                await source.open()
            except (OSError, ValueError) as exc:  # serial errors derive from OSError/ValueError
                log.warning("cannot open %s: %s (retry in %.0fs)", source.name, exc, self._backoff)
                await self._backoff_sleep(stop)
                continue
            ended, failed = await self._session(source, stop)
            if ended or stop.is_set():
                return
            if failed:
                log.warning("reconnecting in %.0fs", self._backoff)
                await self._backoff_sleep(stop)

    async def _backoff_sleep(self, stop: asyncio.Event) -> None:
        """Wait out the current backoff, then widen it for the next failure.

        The wait races `stop`: with BACKOFF_MAX_S at 30 s, a Ctrl-C while the receiver is
        unplugged would otherwise leave the daemon alive for half a minute with the loop-level
        signal handler already disarmed, so further Ctrl-C would do nothing.
        """
        sleeping = asyncio.ensure_future(self._sleep(self._backoff))
        stopping = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({sleeping, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeping.cancel()
            stopping.cancel()
            await asyncio.gather(sleeping, stopping, return_exceptions=True)
        self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)

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
        self._backoff = BACKOFF_MIN_S  # a session was established: earn a fresh backoff ladder
        self.bus.publish("receiver.connected", source.name)
        reader = asyncio.create_task(self._read_loop(source, router), name="receiver-read")
        reason = "stopped"
        ended = failed = False
        fatal: ProfileError | None = None
        try:
            if (
                not self.passive
                and self.profile is not None
                and not await self._configure_or_stop(link, stop)
            ):
                return ended, failed  # stop fired mid-configure; `finally` still tears down
            await self._watchdog(reader, stop)
        except SourceEnded:
            reason, ended = "source ended", True
        except ProfileError as exc:
            reason, failed = str(exc), True
            log.error("receiver error: %s", exc)
            self.bus.publish("receiver.error", reason)
            # RECEIVER_STRICT=1 means a profile the receiver will not take is a startup
            # failure: reconnecting would only re-apply the same rejected profile forever.
            fatal = exc if self.strict else None
        except ReceiverError as exc:
            reason, failed = str(exc), True
            log.error("receiver error: %s", exc)
            self.bus.publish("receiver.error", reason)
        except (OSError, LinkTimeout) as exc:
            reason, failed = f"link failure: {exc}", True
            log.warning(reason)
            self.bus.publish("receiver.error", reason)
        except Exception as exc:  # one bad frame must never end the supervisor
            reason, failed = f"unexpected failure: {exc!r}", True
            log.exception("unexpected receiver failure")
            self.bus.publish("receiver.error", reason)
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            # Closing a handle whose device was unplugged raises; the disconnect event and
            # the state reset below must happen anyway, or the supervisor loses the receiver.
            await _quietly("link.stop", link.stop())
            await _quietly("source.close", source.close())
            self.link = None
            self.connected = False
            self.bus.publish("receiver.disconnected", reason)
        if fatal is not None:
            raise fatal
        return ended, failed

    async def _configure_or_stop(self, link: UbxLink, stop: asyncio.Event) -> bool:
        """Configure the receiver, racing `stop`. False means stop won.

        A receiver that answers nothing keeps `configure()` busy for a poll timeout per key
        and a retry ladder per VALSET; without this race a Ctrl-C would sit through all of it.
        """
        configuring = asyncio.ensure_future(self.configure(link, first=self._first_apply))
        stopping = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({configuring, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)
        if not configuring.done():
            configuring.cancel()
            await asyncio.gather(configuring, return_exceptions=True)
            log.info("stop requested while configuring %s", self.profile and self.profile.name)
            return False
        configuring.result()  # re-raise whatever configure() raised
        self._first_apply = False
        return True

    async def _read_loop(self, source: ByteSource, router: Router) -> None:
        while True:
            data = await source.read()
            if not data:
                if not source.ends_at_eof:
                    # A live device does not "end": no bytes means the handle went away.
                    raise LinkDropped("eof")
                raise SourceEnded
            self._last_rx = time.monotonic()
            router.feed(data)

    async def _watchdog(self, reader: asyncio.Task[None], stop: asyncio.Event) -> None:
        stop_task = asyncio.create_task(stop.wait(), name="receiver-stop")
        try:
            while True:
                done, _ = await asyncio.wait(
                    {reader, stop_task},
                    # never tick slower than the deadline it is there to enforce
                    timeout=min(WATCHDOG_TICK_S, self.rx_timeout_s),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task in done:
                    return
                if reader in done:
                    exc = reader.exception()
                    if isinstance(exc, SourceEnded | ReceiverError):
                        raise exc
                    raise ReceiverError(f"reader failed: {exc!r}")
                if time.monotonic() - self._last_rx > self.rx_timeout_s:
                    raise ReceiverError(f"no data from receiver for {self.rx_timeout_s:.0f}s")
        finally:
            stop_task.cancel()

    # ------------------------------------------------------------- configure
    async def probe(self, link: UbxLink) -> Capabilities:
        """Identify the receiver and read back each optional feature's keys. No side effects.

        Feature support is decided by VALGET, not by polling the message: a poll waiter is
        satisfied by the next *periodic* frame of that identity, so a message that is merely
        not streaming yet looks identical to one the firmware does not have.
        """
        caps = Capabilities()
        frame = await link.poll("MON", "MON-VER")
        if frame.identity == "MON-VER":
            store = StateStore()
            store.apply(frame)
            fw = store.state.firmware
            caps.protver, caps.fw_version, caps.module = fw.protver, fw.fw_version, fw.module
        # The profile owns its optional set; the module default only covers a bare probe.
        features = OPTIONAL_FEATURES if self.profile is None else self.profile.optional
        for feature, items in features.items():
            try:
                await link.valget([key for key, _ in items])
            except (LinkNak, LinkTimeout):  # the firmware has no such configuration key
                caps.unsupported.add(feature)
                continue
            caps.supported.add(feature)
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

        rejected = await self._apply_core(link, profile.core, layers)
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
            await self._apply_optional(link, caps, feature, items, layers)

        mismatches = await self.verify(link, profile, skip=set(rejected))
        if mismatches:
            raise ProfileError(f"configuration verification failed: {mismatches}")
        self.capabilities = caps
        self.bus.publish("receiver.capabilities", caps)
        return caps

    async def _apply_core(self, link: UbxLink, core: CfgItems, layers: int) -> list[str]:
        """Write only the core keys that differ from what the receiver already holds.

        The first apply of a process targets FLASH (`LAYERS_ALL`), so rewriting the whole
        profile on every start would spend a flash erase cycle to change nothing.
        """
        current = await self._readback(link, [k for k, _ in core])
        pending = [(k, v) for k, v in core if current.get(k) != v]
        if not pending:
            log.info("core configuration already matches the profile; nothing written")
            return []
        log.info("applying %d of %d core keys that differ", len(pending), len(core))
        return await self._apply_with_bisect(link, pending, layers)

    async def _apply_optional(
        self, link: UbxLink, caps: Capabilities, feature: str, items: CfgItems, layers: int
    ) -> None:
        current = await self._readback(link, [k for k, _ in items])
        if all(current.get(k) == v for k, v in items):
            caps.supported.add(feature)
            return
        if await self._valset_optional(link, feature, items, layers):
            caps.supported.add(feature)
            return
        caps.supported.discard(feature)
        caps.unsupported.add(feature)
        log.info("optional feature %s not accepted by this firmware", feature)

    async def _valset_optional(
        self, link: UbxLink, feature: str, items: CfgItems, layers: int
    ) -> bool:
        """A NAK is the firmware refusing the feature and demotes it at once.

        A timeout is only silence - a dropped ACK must not cost the feature for the rest of
        the session - so the write is retried once before the feature is given up.
        """
        for attempt in (1, 2):
            try:
                return await link.valset(items, layers)
            except LinkTimeout:
                log.warning(
                    "optional feature %s: no answer to CFG-VALSET (attempt %d/2)", feature, attempt
                )
        return False

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
