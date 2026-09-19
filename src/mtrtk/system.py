"""Host health: CPU, memory, disk, temperature, uptime. Published on `system.stats`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil

from mtrtk.core.bus import Bus
from mtrtk.store.models import SystemStats

log = logging.getLogger(__name__)
PREFERRED_SENSORS = ("cpu_thermal", "coretemp", "k10temp", "soc_thermal", "acpitz")
_ERROR_LOG_INTERVAL_S = 60.0


def _first_reading(readings: Any) -> float | None:
    """The first channel of a group that actually has a value: drivers do report `current=None`."""
    for reading in readings or ():
        current = getattr(reading, "current", None)
        if current is not None:
            return float(current)
    return None


def read_temperature(psutil_module: Any) -> float | None:
    sensors = getattr(psutil_module, "sensors_temperatures", None)
    if sensors is None:
        return None
    try:
        groups = sensors() or {}
    except (OSError, RuntimeError):
        return None
    for name in PREFERRED_SENSORS:
        value = _first_reading(groups.get(name))
        if value is not None:
            return value
    for readings in groups.values():
        value = _first_reading(readings)
        if value is not None:
            return value
    return None


class SystemMonitor:
    def __init__(
        self,
        bus: Bus,
        data_dir: Path,
        interval_s: float = 5.0,
        psutil_module: Any = psutil,
        loadavg: Callable[[], tuple[float, float, float]] | None = os.getloadavg,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bus = bus
        self.data_dir = Path(data_dir)
        self.interval_s = interval_s
        self._ps = psutil_module
        self._loadavg = loadavg
        self._clock = clock
        self._failing = False
        self._suppressed = 0
        self._last_error_log = 0.0

    def snapshot(self) -> SystemStats:
        target = self.data_dir if self.data_dir.exists() else self.data_dir.parent
        du = self._ps.disk_usage(str(target))
        load1: float | None = None
        if self._loadavg is not None:
            try:
                load1 = float(self._loadavg()[0])
            except (OSError, AttributeError):
                load1 = None
        return SystemStats(
            cpu_pct=float(self._ps.cpu_percent(interval=None)),
            mem_pct=float(self._ps.virtual_memory().percent),
            disk_free_gb=du.free / 1e9,
            disk_used_pct=float(du.percent),
            uptime_s=time.time() - float(self._ps.boot_time()),
            temp_c=read_temperature(self._ps),
            load1=load1,
        )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                self.bus.publish("system.stats", self.snapshot())
            except Exception as exc:  # a psutil hiccup must not kill the monitor
                self._failed(exc)
            else:
                self._recovered()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.interval_s)

    # ------------------------------------------------------- failure signalling
    def _failed(self, exc: BaseException) -> None:
        """One traceback per outage, then one line a minute with the count.

        A missing sensor or an unreadable `/proc` fails every pass: at a 5 s interval that is
        ~17 000 tracebacks a day, which buries every other line in the log.
        """
        now = self._clock()
        if not self._failing:
            self._failing = True
            self._suppressed = 0
            self._last_error_log = now
            log.error("system snapshot failed", exc_info=exc)
            return
        self._suppressed += 1
        if now - self._last_error_log >= _ERROR_LOG_INTERVAL_S:
            log.warning("system snapshot still failing: %r (%d suppressed)", exc, self._suppressed)
            self._last_error_log = now
            self._suppressed = 0

    def _recovered(self) -> None:
        """A sample got through again: say so once, so the log shows the outage ending."""
        if not self._failing:
            return
        self._failing = False
        log.info("system snapshot working again")
