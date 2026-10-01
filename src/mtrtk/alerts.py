"""Stateful alert rules -> events table + bus `events.new` + optional webhook."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from mtrtk.core.bus import Bus
from mtrtk.core.state import FixInfo, Hardware, InsStatus, RtkStatus, SurveyIn
from mtrtk.store.models import Event, Level, SystemStats
from mtrtk.store.repos import EventsRepo

log = logging.getLogger(__name__)

FIX_LOST_GRACE_S = 10.0
JAMMING_SUSTAIN_S = 30.0
JAM_IND_THRESHOLD = 200
JAMMING_STATE_WARNING = 2
TEMP_HIGH_C = 80.0
TEMP_CLEAR_C = TEMP_HIGH_C - 5.0  # hysteresis: a host sitting on the threshold must not flap
TEMP_SUSTAIN_S = 60.0
ONE_SHOT_DEDUP_S = 300.0
DISK_WARNING_FACTOR = 1.5  # warn while there is still headroom above the pruning floor
DISK_LOW_CLEAR_FACTOR = 1.1  # retention holds free space just above the floor: clear near it
ANTENNA_FAULT_STATES = {3: "short", 4: "open"}
WEBHOOK_TIMEOUT_S = 5.0
WEBHOOK_LOG_INTERVAL_S = 60.0
HANDLER_LOG_INTERVAL_S = 60.0
# Rover: corrections older than this are stale; they must fall under CORR_OK_S to clear, so an
# age wandering around the threshold on a lossy link does not flap.
CORR_STALE_S = 10.0
CORR_OK_S = 5.0
RTK_LOST_GRACE_S = 10.0  # a fix that drops to float for a few epochs under a tree is not lost
CARR_FIXED = 2
# INS rovers. The filter counts as aligned (navigating) from this mode on, per vendor: SBG EKF
# NAV_POSITION, VectorNav InsStatus "tracking".
INS_ALIGNED_MODE = {"sbg": 4, "vectornav": 2}
INS_ALIGN_GRACE_S = 60.0  # a unit needs motion (or a heading aid) to align: give it a minute
INS_GNSS_LOST_GRACE_S = 10.0
GNSS_NO_FIX = 0  # SBG GPS1_POS NO_SOLUTION, VectorNav GPS Fix "No fix"

_URL_RE = re.compile(r"(https?)://([^\s'\"/]+)[^\s'\"]*")


def _redact_urls(text: str) -> str:
    """For ntfy, Discord and Slack the whole URL is the credential, and the secret is in the
    path: only the scheme and the host may reach the log."""
    return _URL_RE.sub(r"\1://\2/...", text)


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
    "rawlog.drained",
    "rawlog.pruned",
    "rawlog.error",
    "sampler.error",
    "sampler.recovered",
    "base.site_verified",
    "base.site_mismatch",
    "daemon.consumer_failed",
    "ntrip_client.status",  # rover only
    "state.rtk",  # rover only: published per epoch once corrections have been injected
    "state.ins",  # INS rovers: filter mode, GNSS fix and health
    "ins.config",  # INS rovers: a configuration report (read on connect, or applied)
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
        self._raising: set[str] = set()  # conditions whose first event is still being written
        self._one_shot_last: dict[str, float] = {}
        self._had_3d = False
        self._fix_bad_since: float | None = None
        self._jam_since: float | None = None
        self._temp_high_since: float | None = None
        self._survey_valid = False
        self._was_fixed = False
        self._rtk_bad_since: float | None = None
        self._ins_unaligned_since: float | None = None
        self._ins_gnss_had_fix = False
        self._ins_gnss_bad_since: float | None = None
        self._webhook_failing = False
        self._webhook_suppressed = 0
        self._last_webhook_log = 0.0
        self._handler_failing = False
        self._handler_suppressed = 0
        self._last_handler_log = 0.0

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
        """Start a condition. A condition already active raises nothing: it is the same fault.

        The slot is reserved before the first await, not after `_emit` returns: writing the row
        and posting the webhook take milliseconds during which a second caller would otherwise
        see an empty `active` and raise the same fault again. A failed `_emit` releases it, so
        the next sample retries.
        """
        if kind in self.active or kind in self._raising:
            return
        self._raising.add(kind)
        try:
            self.active[kind] = await self._emit(level, kind, message, meta)
        finally:
            self._raising.discard(kind)

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
            log.warning("alert webhook failed: %s", self._describe(exc))
            return
        self._webhook_suppressed += 1
        if now - self._last_webhook_log >= WEBHOOK_LOG_INTERVAL_S:
            log.warning(
                "alert webhook still failing: %s (%d suppressed)",
                self._describe(exc),
                self._webhook_suppressed,
            )
            self._last_webhook_log = now
            self._webhook_suppressed = 0

    def _describe(self, exc: BaseException) -> str:
        """`%r` of an httpx error can carry the request URL, and the URL is the secret."""
        target = _redact_urls(self.webhook_url or "")
        return f"{target}: {_redact_urls(f'{type(exc).__name__}: {exc}')}"

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
        self._ins_unaligned_since = None  # an INS gets its alignment minute from each connect
        await self.clear("receiver_disconnected", f"receiver connected ({source})")
        # `receiver.capabilities` is published by `configure()` alone, which a passive or replay
        # run never calls: without this edge a transient link error would stay active for the
        # life of the process. The controller publishes `connected` before any error of that
        # session, so this can only clear an error from the session that just ended.
        await self.clear("receiver_error", f"receiver connected ({source})")

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
        # `state.hardware` republishes the live object, so every field is read once, here: a
        # value read back after an await belongs to a later sample, not to this one.
        jam_ind, jamming_state = hw.jam_ind, hw.jamming_state
        jamming_state_name = hw.jamming_state_name
        ant_status, ant_status_name = hw.ant_status, hw.ant_status_name
        jammed = jam_ind >= JAM_IND_THRESHOLD or jamming_state >= JAMMING_STATE_WARNING
        if jammed:
            # A passing vehicle jams a base for seconds; only a sustained level is worth an event.
            if self._jam_since is None:
                self._jam_since = now
            elif now - self._jam_since >= JAMMING_SUSTAIN_S:
                await self.raise_(
                    "jamming",
                    "warning",
                    f"RF interference: jam_ind={jam_ind} state={jamming_state_name}",
                    {"jam_ind": jam_ind},
                )
        else:
            self._jam_since = None
            await self.clear("jamming", f"RF interference cleared (jam_ind={jam_ind})")
        fault = ANTENNA_FAULT_STATES.get(ant_status)
        if fault:
            await self.raise_(
                "antenna_fault", "error", f"antenna {fault} detected", {"ant_status": ant_status}
            )
        else:
            await self.clear("antenna_fault", f"antenna status {ant_status_name}")

    async def _on_system_stats(self, stats: SystemStats) -> None:
        now = self._clock()
        free = stats.disk_free_gb
        # Two independent bands. Sharing one `else` made `disk_low` unclearable in the very
        # state retention maintains: free space pinned just above the floor but under 1.5x it.
        if free < self.min_free_gb:
            await self.raise_(
                "disk_low",
                "error",
                f"disk free {free:.1f} GB below {self.min_free_gb:.1f} GB: pruning logs",
                {"free_gb": free},
            )
        elif free >= self.min_free_gb * DISK_LOW_CLEAR_FACTOR:
            await self.clear("disk_low", f"disk free {free:.1f} GB")
        if free < self.min_free_gb * DISK_WARNING_FACTOR:
            await self.raise_(
                "disk_warning", "warning", f"disk free {free:.1f} GB", {"free_gb": free}
            )
        else:
            await self.clear("disk_warning", f"disk free {free:.1f} GB")
        if stats.temp_c is not None:
            await self._temperature(stats.temp_c, now)

    async def _temperature(self, temp_c: float, now: float) -> None:
        """A single hot sample is a fan spinning up, not a station in trouble: the reading has
        to hold for a minute of consecutive samples, and clears 5 degrees below the threshold."""
        if temp_c >= TEMP_HIGH_C:
            if self._temp_high_since is None:
                self._temp_high_since = now
            elif now - self._temp_high_since >= TEMP_SUSTAIN_S:
                await self.raise_(
                    "temperature_high",
                    "warning",
                    f"host temperature {temp_c:.0f} °C",
                    {"temp_c": temp_c},
                )
            return
        self._temp_high_since = None
        if temp_c < TEMP_CLEAR_C:
            await self.clear("temperature_high", f"host temperature {temp_c:.0f} °C")

    async def _on_rawlog_backpressure(self, meta: dict[str, Any]) -> None:
        # Stateful: the queue staying over the mark is one condition, and the writer says when
        # it has drained (`rawlog.drained`), so `active` names it for as long as it lasts.
        queued = meta.get("queued")
        await self.raise_(
            "logger_backpressure",
            "warning",
            f"raw logger falling behind: {queued} frames queued",
            {"queued": queued},
        )

    async def _on_rawlog_drained(self, meta: dict[str, Any]) -> None:
        await self.clear(
            "logger_backpressure", f"raw logger caught up ({meta.get('queued')} frames queued)"
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

    async def _on_ntrip_client_status(self, status: Any) -> None:
        # A status published while still connecting has no error yet: nothing has gone wrong.
        if status.connected:
            await self.clear(
                "ntrip_disconnected",
                f"NTRIP connected to {status.host}:{status.port}/{status.mountpoint}",
            )
        elif status.last_error:
            await self.raise_(
                "ntrip_disconnected", "warning", f"NTRIP client disconnected: {status.last_error}"
            )

    async def _on_state_rtk(self, rtk: RtkStatus) -> None:
        now = self._clock()
        # `state.rtk` publishes the live object: read the sample once, before any await.
        corr_age, carr_soln, carr_name = rtk.corr_age_s, rtk.carr_soln, rtk.carr_soln_name
        if corr_age is not None and corr_age > CORR_STALE_S:
            await self.raise_(
                "corrections_stale",
                "warning",
                f"corrections are {corr_age:.0f} s old",
                {"corr_age_s": corr_age},
            )
        elif corr_age is not None and corr_age < CORR_OK_S:
            await self.clear("corrections_stale", f"corrections are fresh again ({corr_age:.0f} s)")
        if carr_soln == CARR_FIXED:
            self._was_fixed = True
            self._rtk_bad_since = None
            await self.clear("rtk_lost", "RTK fixed again")
            return
        # Only a fix that existed can be lost: a rover still converging has lost nothing.
        if not self._was_fixed:
            return
        if self._rtk_bad_since is None:
            self._rtk_bad_since = now
        elif now - self._rtk_bad_since >= RTK_LOST_GRACE_S:
            await self.raise_(
                "rtk_lost", "warning", f"RTK fixed lost ({carr_name})", {"carr_soln": carr_soln}
            )

    async def _on_state_ins(self, ins: InsStatus) -> None:
        now = self._clock()
        # `state.ins` publishes the live object: read the sample once, before any await.
        vendor, mode, mode_name = ins.vendor, ins.mode, ins.mode_name
        gnss_fix, gnss_name = ins.gnss_fix, ins.gnss_fix_name
        imu_error = ins.errors.get("imu") is True or ins.general_ok.get("imu_power") is False
        imu_known = "imu" in ins.errors or "imu_power" in ins.general_ok
        aligned_from = INS_ALIGNED_MODE.get(vendor)
        if aligned_from is not None and mode is not None:
            if mode >= aligned_from:
                self._ins_unaligned_since = None
                await self.clear("ins_not_aligned", f"INS aligned ({mode_name or mode})")
            elif self._ins_unaligned_since is None:
                self._ins_unaligned_since = now
            elif now - self._ins_unaligned_since >= INS_ALIGN_GRACE_S:
                await self.raise_(
                    "ins_not_aligned",
                    "warning",
                    f"INS not aligned after {INS_ALIGN_GRACE_S:.0f} s "
                    f"({mode_name or f'mode {mode}'}): it needs motion or a heading aid",
                    {"mode": mode},
                )
        if gnss_fix is not None:
            if gnss_fix != GNSS_NO_FIX:
                self._ins_gnss_had_fix = True
                self._ins_gnss_bad_since = None
                await self.clear("ins_gnss_lost", f"INS GNSS fix back ({gnss_name or gnss_fix})")
            elif self._ins_gnss_had_fix:
                # The INS still outputs (this sample) on inertial alone: it drifts from here.
                if self._ins_gnss_bad_since is None:
                    self._ins_gnss_bad_since = now
                elif now - self._ins_gnss_bad_since >= INS_GNSS_LOST_GRACE_S:
                    await self.raise_(
                        "ins_gnss_lost",
                        "error",
                        "INS GNSS solution lost: the position is dead reckoning",
                        {"gnss_fix": gnss_fix},
                    )
        if imu_error:
            await self.raise_("imu_error", "error", "INS reports an IMU error")
        elif imu_known:
            await self.clear("imu_error", "INS IMU OK")

    async def _on_ins_config(self, report: Any) -> None:
        mismatched = list(getattr(report, "mismatched", None) or [])
        if mismatched:
            await self.raise_(
                "ins_config_mismatch",
                "error",
                f"INS configuration read back different: {', '.join(map(str, mismatched))}",
                {"mismatched": mismatched},
            )
        else:
            await self.clear("ins_config_mismatch", "INS configuration matches")

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
                except Exception as exc:
                    self._handler_failed(topic, exc)
                else:
                    self._handler_ok()
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

    # ------------------------------------------------------- failure signalling
    def _handler_failed(self, topic: str, exc: BaseException) -> None:
        """One traceback per outage, then one line a minute with the count.

        An events table that cannot be written fails on every sample, and `state.fix` arrives at
        1 Hz: logging each would be ~86 000 tracebacks a day and bury every other line.
        """
        now = self._clock()
        if not self._handler_failing:
            self._handler_failing = True
            self._handler_suppressed = 0
            self._last_handler_log = now
            log.error("alert rule failed for %s", topic, exc_info=exc)
            return
        self._handler_suppressed += 1
        if now - self._last_handler_log >= HANDLER_LOG_INTERVAL_S:
            log.warning(
                "alert rules still failing (%s): %r (%d suppressed)",
                topic,
                exc,
                self._handler_suppressed,
            )
            self._last_handler_log = now
            self._handler_suppressed = 0

    def _handler_ok(self) -> None:
        """A message got through again: say so once, so the log shows the outage ending."""
        if not self._handler_failing:
            return
        self._handler_failing = False
        log.info("alert rules working again")

    async def aclose(self) -> None:
        """Close the webhook client, but only the one we made: an injected one is the caller's."""
        if not self._owns_http or self._http is None:
            return
        self._owns_http = False
        with contextlib.suppress(Exception):
            await self._http.aclose()
