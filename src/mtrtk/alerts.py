"""Stateful alert rules -> events table + bus `events.new` + optional webhook."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from mtrtk.core.bus import Bus
from mtrtk.core.state import FixInfo, Hardware, SurveyIn
from mtrtk.store.models import Event, Level, SystemStats
from mtrtk.store.repos import EventsRepo

log = logging.getLogger(__name__)

FIX_LOST_GRACE_S = 10.0
JAMMING_SUSTAIN_S = 30.0
JAM_IND_THRESHOLD = 200
JAMMING_STATE_WARNING = 2
TEMP_HIGH_C = 80.0
TEMP_CLEAR_C = 70.0
ONE_SHOT_DEDUP_S = 300.0
DISK_WARNING_FACTOR = 1.5  # warn while there is still headroom above the pruning floor
ANTENNA_FAULT_STATES = {3: "short", 4: "open"}
WEBHOOK_TIMEOUT_S = 5.0
WEBHOOK_LOG_INTERVAL_S = 60.0

_LOG_LEVELS: dict[str, int] = {
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

TOPICS = (
    "state.fix",
    "state.hardware",
    "state.survey_in",
    "receiver.connected",
    "receiver.disconnected",
    "receiver.error",
    "receiver.capabilities",
    "system.stats",
    "rawlog.backpressure",
    "rawlog.pruned",
    "rawlog.error",
    "sampler.error",
    "sampler.recovered",
    "base.site_verified",
    "base.site_mismatch",
    "daemon.consumer_failed",
)


class AlertEngine:
    """Turns bus traffic into the event log the UI and the webhook show.

    Two kinds of rule. A *condition* is stateful: `raise_()` writes one event the first time it
    holds and `clear()` one `<kind>_cleared` event when it stops, so a receiver that is down for
    an hour costs two rows, not one per sample, and `active` always names what is wrong now. A
    *one-shot* is a moment with nothing to recover from (a pruned file, a backpressure burst);
    repeats inside `ONE_SHOT_DEDUP_S` are dropped so one sustained fault cannot flood the log.
    """

    def __init__(
        self,
        bus: Bus,
        events: EventsRepo,
        *,
        role: str,
        host: str,
        min_free_gb: float,
        webhook_url: str | None = None,
        http: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bus = bus
        self.events = events
        self.role = role
        self.host = host
        self.min_free_gb = min_free_gb
        self.webhook_url = webhook_url
        self._owns_http = http is None and webhook_url is not None
        self._http = http if http is not None else (httpx.AsyncClient() if webhook_url else None)
        self._clock = clock
        self.sub = bus.subscribe(*TOPICS, maxsize=500)
        self.active: dict[str, Event] = {}
        self._one_shot_last: dict[str, float] = {}
        self._had_3d = False
        self._fix_bad_since: float | None = None
        self._jam_since: float | None = None
        self._survey_valid = False
        self._webhook_failing = False
        self._webhook_suppressed = 0
        self._last_webhook_log = 0.0

    # ------------------------------------------------------------- emitting
    async def _emit(
        self, level: Level, kind: str, message: str, meta: dict[str, Any] | None = None
    ) -> Event:
        event = await self.events.add(level, kind, message, meta)
        self.bus.publish("events.new", event)
        log.log(_LOG_LEVELS[level], "%s: %s", kind, message)
        await self._webhook(event)
        return event

    async def raise_(
        self, kind: str, level: Level, message: str, meta: dict[str, Any] | None = None
    ) -> None:
        """Start a condition. A condition already active raises nothing: it is the same fault."""
        if kind in self.active:
            return
        self.active[kind] = await self._emit(level, kind, message, meta)

    async def clear(self, kind: str, message: str) -> None:
        """End a condition. Nothing active means nothing to recover from, so no event."""
        if kind not in self.active:
            return
        del self.active[kind]
        await self._emit("info", f"{kind}_cleared", message)

    async def one_shot(
        self, kind: str, level: Level, message: str, meta: dict[str, Any] | None = None
    ) -> None:
        now = self._clock()
        last = self._one_shot_last.get(kind)
        if last is not None and now - last < ONE_SHOT_DEDUP_S:
            return
        self._one_shot_last[kind] = now
        await self._emit(level, kind, message, meta)

    async def _webhook(self, event: Event) -> None:
        if not self.webhook_url or self._http is None:
            return
        body = {
            "level": event.level,
            "kind": event.kind,
            "message": event.message,
            "ts": event.ts_utc.isoformat(),
            "host": self.host,
            "role": self.role,
        }
        try:
            await self._http.post(self.webhook_url, json=body, timeout=WEBHOOK_TIMEOUT_S)
        except Exception as exc:  # network problems must never propagate
            self._webhook_failed(exc)
        else:
            self._webhook_ok()

    def _webhook_failed(self, exc: BaseException) -> None:
        """One line per outage, then one a minute: an unreachable webhook fails on every event."""
        now = self._clock()
        if not self._webhook_failing:
            self._webhook_failing = True
            self._webhook_suppressed = 0
            self._last_webhook_log = now
            log.warning("alert webhook failed: %r", exc)
            return
        self._webhook_suppressed += 1
        if now - self._last_webhook_log >= WEBHOOK_LOG_INTERVAL_S:
            log.warning(
                "alert webhook still failing: %r (%d suppressed)", exc, self._webhook_suppressed
            )
            self._last_webhook_log = now
            self._webhook_suppressed = 0

    def _webhook_ok(self) -> None:
        if not self._webhook_failing:
            return
        self._webhook_failing = False
        log.info("alert webhook delivering again")

    # ------------------------------------------------------------- dispatch
    async def handle(self, topic: str, item: Any) -> None:
        handler = getattr(self, "_on_" + topic.replace(".", "_"), None)
        if handler is not None:
            await handler(item)

    async def _on_receiver_disconnected(self, reason: str) -> None:
        # A replay file that ran out is the expected end of a run, not a fault worth waking for.
        if reason == "source ended":
            return
        await self.raise_("receiver_disconnected", "error", f"receiver disconnected: {reason}")

    async def _on_receiver_connected(self, source: str) -> None:
        await self.clear("receiver_disconnected", f"receiver connected ({source})")

    async def _on_receiver_error(self, message: str) -> None:
        await self.raise_("receiver_error", "error", message)

    async def _on_receiver_capabilities(self, caps: Any) -> None:
        await self.clear("receiver_error", "receiver configured")

    async def _on_state_fix(self, fix: FixInfo) -> None:
        now = self._clock()
        if fix.fix_type >= 3:
            self._had_3d = True
            self._fix_bad_since = None
            await self.clear("fix_lost", f"fix restored ({fix.fix_type_name})")
            return
        # A receiver still acquiring has not lost anything: only a fix that existed can be lost.
        if not self._had_3d:
            return
        if self._fix_bad_since is None:
            self._fix_bad_since = now
        elif now - self._fix_bad_since >= FIX_LOST_GRACE_S:
            await self.raise_(
                "fix_lost",
                "warning",
                f"position fix lost ({fix.fix_type_name})",
                {"fix_type": fix.fix_type},
            )

    async def _on_state_hardware(self, hw: Hardware) -> None:
        now = self._clock()
        jammed = hw.jam_ind >= JAM_IND_THRESHOLD or hw.jamming_state >= JAMMING_STATE_WARNING
        if jammed:
            # A passing vehicle jams a base for seconds; only a sustained level is worth an event.
            if self._jam_since is None:
                self._jam_since = now
            elif now - self._jam_since >= JAMMING_SUSTAIN_S:
                await self.raise_(
                    "jamming",
                    "warning",
                    f"RF interference: jam_ind={hw.jam_ind} state={hw.jamming_state_name}",
                    {"jam_ind": hw.jam_ind},
                )
        else:
            self._jam_since = None
            await self.clear("jamming", f"RF interference cleared (jam_ind={hw.jam_ind})")
        fault = ANTENNA_FAULT_STATES.get(hw.ant_status)
        if fault:
            await self.raise_(
                "antenna_fault", "error", f"antenna {fault} detected", {"ant_status": hw.ant_status}
            )
        else:
            await self.clear("antenna_fault", f"antenna status {hw.ant_status_name}")

    async def _on_system_stats(self, stats: SystemStats) -> None:
        free = stats.disk_free_gb
        if free < self.min_free_gb:
            await self.raise_(
                "disk_low",
                "error",
                f"disk free {free:.1f} GB below {self.min_free_gb:.1f} GB: pruning logs",
                {"free_gb": free},
            )
        if free < self.min_free_gb * DISK_WARNING_FACTOR:
            await self.raise_(
                "disk_warning", "warning", f"disk free {free:.1f} GB", {"free_gb": free}
            )
        else:
            await self.clear("disk_low", f"disk free {free:.1f} GB")
            await self.clear("disk_warning", f"disk free {free:.1f} GB")
        if stats.temp_c is not None:
            # Hysteresis: a host sitting on the threshold must not alternate alert and recovery.
            if stats.temp_c >= TEMP_HIGH_C:
                await self.raise_(
                    "temperature_high",
                    "warning",
                    f"host temperature {stats.temp_c:.0f} °C",
                    {"temp_c": stats.temp_c},
                )
            elif stats.temp_c <= TEMP_CLEAR_C:
                await self.clear("temperature_high", f"host temperature {stats.temp_c:.0f} °C")

    async def _on_rawlog_backpressure(self, queue_size: int) -> None:
        # The writer republishes this on every high-water crossing, which under sustained
        # overload is once per drained frame: the dedup window makes that one event.
        await self.one_shot(
            "logger_backpressure",
            "warning",
            f"raw logger falling behind: {queue_size} frames queued",
            {"queued": queue_size},
        )

    async def _on_rawlog_pruned(self, path: Any) -> None:
        await self.one_shot(
            "log_pruned", "info", f"pruned old raw log {getattr(path, 'name', path)} to free disk"
        )

    async def _on_rawlog_error(self, message: str) -> None:
        # Every frame of a failing write fails: dedup, or a full disk writes a row per frame.
        await self.one_shot("logger_error", "error", f"raw log write failure: {message}")

    async def _on_sampler_error(self, message: str) -> None:
        await self.raise_("sampler_failing", "warning", f"history sampler failing: {message}")

    async def _on_sampler_recovered(self, message: str) -> None:
        await self.clear("sampler_failing", message)

    async def _on_daemon_consumer_failed(self, meta: dict[str, Any]) -> None:
        await self.one_shot(
            f"consumer_failed_{meta.get('name')}",
            "error",
            f"{meta.get('name')} crashed and was restarted: {meta.get('error')}",
            meta,
        )

    async def _on_state_survey_in(self, svin: SurveyIn) -> None:
        if svin.valid and not self._survey_valid:
            acc = f"{svin.mean_acc_m:.2f} m" if svin.mean_acc_m is not None else "n/a"
            await self._emit(
                "info",
                "survey_in_valid",
                f"survey-in complete: mean 3D accuracy {acc}",
                {"mean_acc_m": svin.mean_acc_m, "dur_s": svin.dur_s},
            )
        self._survey_valid = svin.valid

    async def _on_base_site_mismatch(self, meta: dict[str, Any]) -> None:
        await self.raise_(
            "site_mismatch",
            "error",
            f"RTCM 1005 position does not match site {meta.get('site')}",
            meta,
        )

    async def _on_base_site_verified(self, meta: dict[str, Any]) -> None:
        await self.clear("site_mismatch", f"site {meta.get('site')} verified")
        await self._emit(
            "info",
            "site_verified",
            f"fixed site {meta.get('site')} verified against RTCM 1005",
            meta,
        )

    # ------------------------------------------------------------- run loop
    def stop(self) -> None:
        """End `run()`: the queued messages still drain before the loop exits."""
        self.bus.unsubscribe(self.sub)

    async def run(self, stop: asyncio.Event) -> None:
        waiter = asyncio.create_task(self._wait_stop(stop), name="alerts-stop")
        try:
            async for topic, item in self.sub:
                # Nothing a message can do may end the engine: a malformed payload or a failing
                # rule costs that one message, never every alert after it.
                try:
                    await self.handle(topic, item)
                except Exception:
                    log.exception("alert rule failed for %s", topic)
                if stop.is_set():
                    break
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            self.stop()
            await self.aclose()

    async def _wait_stop(self, stop: asyncio.Event) -> None:
        """A silent bus must not wedge `run()`: closing the subscription ends the loop."""
        await stop.wait()
        self.stop()

    async def aclose(self) -> None:
        """Close the webhook client, but only the one we made: an injected one is the caller's."""
        if not self._owns_http or self._http is None:
            return
        self._owns_http = False
        with contextlib.suppress(Exception):
            await self._http.aclose()
