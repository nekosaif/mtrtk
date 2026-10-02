"""Owns the receiver connection: open/reconnect, capability probe, profile apply and verify."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pyubx2 import SET, UBXMessage

from mtrtk.core.bus import Bus
from mtrtk.core.link import DEFAULT_TIMEOUT_S, LinkNak, LinkNoData, LinkTimeout, UbxLink
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
# A relayed link (socat over a Tailscale DERP relay) drops the odd answer and stalls for
# seconds at a time. One missed CFG-VALGET must neither hide an optional feature for the rest
# of the session nor - when every key of a verification readback comes back empty - end the
# daemon: both are silence, not the receiver's verdict.
PROBE_ATTEMPTS = 3  # VALGETs per optional feature before a silent probe counts as "no"
# NAKs per optional feature before the probe takes "no" for an answer. An ACK carries only the
# class/id it answers, so a lone ACK-NAK may be the late verdict on an earlier request whose
# stale credit lapsed during a multi-second stall; the same NAK on a re-ask is the firmware's.
PROBE_NAKS_FINAL = 2
VERIFY_ATTEMPTS = 3  # unanswered verification readbacks before the very first start gives up
VERIFY_RETRY_PAUSE_S = 1.0  # between them: long enough for a stalled relay to catch up

Sleeper = Callable[[float], Awaitable[None]]

ResetKind = Literal["hot", "warm", "cold", "factory"]

# UBX-CFG-RST's navBbrMask is a *bitfield group* in pyubx2 ("navBbrMask_bit"), not a plain
# attribute: a `navBbrMask=<int>` kwarg is silently dropped and every kind would serialise to
# the same all-zero (hot start) frame. The bits have to be named one by one.
_COLD_BITS = ("eph", "alm", "health", "klob", "pos", "clkd", "osc", "utc", "rtc", "aop")
RESET_BITS: dict[str, dict[str, int]] = {
    "hot": {},  # keep everything in battery-backed RAM
    "warm": {"eph": 1},  # drop the ephemerides only
    "cold": dict.fromkeys(_COLD_BITS, 1),  # clear the whole BBR navigation store
    "factory": dict.fromkeys(_COLD_BITS, 1),  # same restart, after the configuration wipe
}
RESET_MODE_HW = 0x01  # hardware reset: the USB device re-enumerates and the daemon reconnects
# CFG-CFG's masks are X004 and pyubx2 rejects an int for them (UBXTypeError); 0xFFFF covers
# every configuration section u-blox defines. Save nothing, clear and reload the defaults.
CFG_MASK_ALL = b"\xff\xff\x00\x00"
CFG_MASK_NONE = b"\x00\x00\x00\x00"


async def _quietly(what: str, closing: Awaitable[None]) -> None:
    """Await a teardown step: it must never mask the failure that caused the teardown."""
    try:
        await closing
    except Exception:
        log.warning("ignoring %s failure while disconnecting", what, exc_info=True)


def _jsonable(value: Any) -> Any:
    """Make one parsed UBX field safe to hand to the JSON encoder."""
    if isinstance(value, bytes):
        return value.decode("ascii", "replace").rstrip("\x00")
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


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
        ack_timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.bus = bus
        self._source_factory = source_factory
        self.profile = profile
        self.strict = strict
        self.passive = passive
        self.rx_timeout_s = rx_timeout_s
        # RECEIVER_ACK_TIMEOUT_S: every poll/VALSET/VALGET deadline on the links built here.
        self.ack_timeout_s = ack_timeout_s
        # Injected so a test can collapse the backoff ladder without patching the shared
        # `asyncio.sleep` out from under every other coroutine in the process.
        self._sleep = sleep
        self.capabilities = Capabilities()
        self.connected = False
        self.link: UbxLink | None = None
        self._first_apply = True
        # Set by the first configure that succeeds. Until then a profile the receiver refuses
        # is a startup failure (RECEIVER_STRICT=1); afterwards the daemon is running and a
        # reconnect that goes wrong only reports and tries again - it never ends the process.
        self._configured_once = False
        # Optional features some probe in this process found: a later probe that hears nothing
        # back for one of them keeps it, since only a NAK says the firmware lacks it.
        self._seen_supported: set[str] = set()
        self._last_rx = 0.0
        self._backoff = BACKOFF_MIN_S
        # One configure at a time. A reapply asked for over the API drives the same link as the
        # session's own configure; two overlapping VALGET bursts would have the link hand each
        # run the other's answers, and both would then "verify" against the wrong readback.
        self._configuring = asyncio.Lock()

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
        link = UbxLink(source, self.bus, timeout_s=self.ack_timeout_s)
        await link.start()
        self.link = link
        self.connected = True
        self._last_rx = time.monotonic()
        configuring = not self.passive and self.profile is not None
        if not configuring:
            self._backoff = BACKOFF_MIN_S  # a session was established: earn a fresh ladder
        self.bus.publish("receiver.connected", source.name)
        reader = asyncio.create_task(self._read_loop(source, router), name="receiver-read")
        reason = "stopped"
        ended = failed = False
        fatal: ProfileError | None = None
        try:
            if configuring:
                if not await self._configure_or_stop(link, stop):
                    return ended, failed  # stop fired mid-configure; `finally` still tears down
                # Only a configured session earns a fresh backoff ladder. One refused or left
                # unanswered in configure keeps widening its backoff up to BACKOFF_MAX_S, rather
                # than re-probing and re-writing the receiver every second forever.
                self._backoff = BACKOFF_MIN_S
            await self._watchdog(reader, stop)
        except SourceEnded:
            reason, ended = "source ended", True
        except ProfileError as exc:
            reason, failed = str(exc), True
            log.error("receiver error: %s", exc)
            self.bus.publish("receiver.error", reason)
            # RECEIVER_STRICT=1 means a profile the receiver will not take is a startup
            # failure: reconnecting would only re-apply the same rejected profile forever.
            # Only a startup one, though: once a configure has succeeded the daemon is serving,
            # and a reconnect that is refused reports the error and keeps trying.
            fatal = exc if self.strict and not self._configured_once else None
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
        self._configured_once = True
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
                    raise ReceiverError(f"no data from receiver for {self.rx_timeout_s:g}s")
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
            found = await self._probe_feature(link, feature, [key for key, _ in items])
            (caps.supported if found else caps.unsupported).add(feature)
        self._seen_supported |= caps.supported
        log.info(
            "receiver %s fw=%s protver=%s unsupported=%s",
            caps.module,
            caps.fw_version,
            caps.protver,
            sorted(caps.unsupported),
        )
        return caps

    async def _probe_feature(self, link: UbxLink, feature: str, keys: list[str]) -> bool:
        """True when the firmware holds the feature's keys.

        A NAK is the firmware's answer once it repeats (PROBE_NAKS_FINAL): a single one may be
        an earlier request's late verdict. Silence is no answer at all: the VALGET is asked up
        to PROBE_ATTEMPTS times, and a feature an earlier probe found stays supported even
        when every attempt goes unanswered. An ACK-ACK without its data frame, and an answer
        that lacks the keys asked for (some other request's late reply), count as silence.
        """
        naks = 0
        for attempt in range(1, PROBE_ATTEMPTS + 1):
            try:
                got = await link.valget(keys)
            except LinkNoData:  # ACK'd, but the data frame was lost: a miss, not a refusal
                pass
            except LinkNak:
                naks += 1
                if naks >= PROBE_NAKS_FINAL:  # the firmware has no such configuration key
                    return False
                log.info("optional feature %s: NAK'd once; asking again", feature)
                continue
            except LinkTimeout:
                pass
            else:
                if all(key in got for key in keys):
                    return True
            log.warning(
                "optional feature %s: no answer to CFG-VALGET (attempt %d/%d)",
                feature,
                attempt,
                PROBE_ATTEMPTS,
            )
        if feature in self._seen_supported:
            log.warning("optional feature %s: keeping it, an earlier probe found it", feature)
            return True
        return False

    async def configure(self, link: UbxLink, first: bool) -> Capabilities:
        async with self._configuring:
            return await self._configure(link, first)

    async def _configure(self, link: UbxLink, first: bool) -> Capabilities:
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

        await self._verify_or_raise(link, profile, skip=set(rejected))
        self.capabilities = caps
        self.bus.publish("receiver.capabilities", caps)
        return caps

    async def _verify_or_raise(self, link: UbxLink, profile: Profile, skip: set[str]) -> None:
        """Read the profile back; raise unless the receiver holds every key of it.

        A readback with wrong or missing values is the receiver's verdict: `ProfileError`.
        One in which *no* key came back at all - every key asked for is None - is the link's
        silence, not a verdict. Once a configure has succeeded that is a `LinkTimeout`, the
        same reconnect any link failure gets; on the very first start it is read again,
        VERIFY_ATTEMPTS times in all, before the start fails.
        """
        wanted = len(self._wanted(profile, skip))
        attempts = 1 if self._configured_once else VERIFY_ATTEMPTS
        for attempt in range(1, attempts + 1):
            mismatches = await self.verify(link, profile, skip=skip)
            if not mismatches:
                return
            # A wanted value is never None, so every key that came back empty is a mismatch:
            # silence is exactly "as many empty keys as keys asked for".
            blank = sum(got is None for _, got in mismatches.values())
            if blank != wanted:
                raise ProfileError(f"configuration verification failed: {mismatches}")
            log.warning(
                "configuration verification got no answers (attempt %d/%d)", attempt, attempts
            )
            if attempt < attempts:
                await self._sleep(VERIFY_RETRY_PAUSE_S)
        if self._configured_once:
            raise LinkTimeout(
                f"configuration verification got no answers for {len(mismatches)} keys"
            )
        raise ProfileError(
            f"configuration verification got no answers after {attempts} attempts"
            f" ({len(mismatches)} keys)"
        )

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
        accepted = await self._valset_optional(link, feature, items, layers)
        if accepted is None:
            # Silence, not a refusal. The feature is only here because the probe found its keys
            # (or an earlier probe did), so dropping it now would hide it on a stalled relay.
            caps.supported.add(feature)
            log.warning("optional feature %s: CFG-VALSET went unanswered; keeping it", feature)
            return
        if accepted:
            caps.supported.add(feature)
            return
        caps.supported.discard(feature)
        caps.unsupported.add(feature)
        log.info("optional feature %s not accepted by this firmware", feature)

    async def _valset_optional(
        self, link: UbxLink, feature: str, items: CfgItems, layers: int
    ) -> bool | None:
        """True on ACK, False on NAK - the firmware refusing the feature, which demotes it.

        A timeout is only silence - a dropped ACK must not cost the feature for the rest of
        the session - so the write is retried once, and None says neither attempt was heard.
        """
        for attempt in (1, 2):
            try:
                return await link.valset(items, layers)
            except LinkTimeout:
                log.warning(
                    "optional feature %s: no answer to CFG-VALSET (attempt %d/2)", feature, attempt
                )
        return None

    async def apply_items(self, items: CfgItems, layers: int = LAYERS_ALL) -> bool:
        if self.link is None:
            raise ReceiverError("receiver not connected")
        return await self.link.valset(items, layers)

    # --------------------------------------------------------------- actions
    def _live_link(self) -> UbxLink:
        link = self.link
        if link is None or not self.connected:
            raise ReceiverError("receiver not connected")
        return link

    async def reapply(self) -> Capabilities:
        """Re-run the full first-apply against the live receiver, flash layer included."""
        async with self._configuring:
            # Under the lock, not before it: a reapply that waited out a long configure may have
            # been queued behind the session that owned the link, and the handle it would have
            # captured is closed by now. Re-read, and say "not connected" rather than fail on IO.
            caps = await self._configure(self._live_link(), first=True)
            # The profile is in flash again, so a later reconnect only needs the RAM layer.
            # Inside the lock too: a factory reset waiting behind us raises the flag, and
            # lowering it after releasing would undo the reset's only lasting effect.
            self._first_apply = False
        return caps

    async def reset(self, kind: ResetKind) -> None:
        """Restart the receiver. `factory` first wipes every stored configuration item.

        The reset itself is fire-and-forget: a receiver that is restarting does not ACK, and
        with `RESET_MODE_HW` the USB device re-enumerates - the supervisor's reconnect loop is
        what brings the profile back.

        It takes the configure lock, though it only writes. A configure running alongside it
        would put VALSETs between the wipe and the restart, and - worse - a reapply finishing
        afterwards would lower the `_first_apply` this reset just raised, so the profile would
        never be written back to flash.
        """
        if kind not in RESET_BITS:
            raise ValueError(f"unknown reset kind {kind!r}")
        async with self._configuring:
            link = self._live_link()  # re-read under the lock: the session may have ended
            if kind == "factory":
                clear = UBXMessage(
                    "CFG",
                    "CFG-CFG",
                    SET,
                    clearMask=CFG_MASK_ALL,
                    saveMask=CFG_MASK_NONE,
                    loadMask=CFG_MASK_ALL,
                    devBBR=1,
                    devFlash=1,
                )
                await link.write(clear.serialize())
                # Nothing of ours survives the wipe: the next apply has to write flash again.
                self._first_apply = True
            rst = UBXMessage("CFG", "CFG-RST", SET, resetMode=RESET_MODE_HW, **RESET_BITS[kind])
            await link.write(rst.serialize())
        log.warning("sent %s reset to the receiver; expect a reconnect", kind)
        self.bus.publish("receiver.reset", {"kind": kind})

    async def poll(self, msg_class: str, msg_id: str) -> dict[str, Any]:
        """Poll one UBX message and return its parsed fields, ready for JSON."""
        frame = await self._live_link().poll(msg_class, msg_id)
        parsed = frame.parsed()
        out = {k: _jsonable(v) for k, v in parsed.__dict__.items() if not k.startswith("_")}
        # Says which message actually answered: a receiver that refuses the poll sends ACK-NAK.
        out["identity"] = frame.identity
        return out

    async def verify(
        self, link: UbxLink, profile: Profile, skip: set[str] | None = None
    ) -> dict[str, tuple[CfgValue, CfgValue | None]]:
        wanted = self._wanted(profile, skip)
        got = await self._readback(link, list(wanted))
        return {k: (v, got.get(k)) for k, v in wanted.items() if got.get(k) != v}

    @staticmethod
    def _wanted(profile: Profile, skip: set[str] | None) -> dict[str, CfgValue]:
        """The keys a verification reads back, and the value each must hold."""
        return {k: v for k, v in [*profile.core, *profile.signals] if not skip or k not in skip}

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
