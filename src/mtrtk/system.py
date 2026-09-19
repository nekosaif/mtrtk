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


def read_temperature(psutil_module: Any) -> float | None:
    sensors = getattr(psutil_module, "sensors_temperatures", None)
    if sensors is None:
        return None
    try:
        groups = sensors() or {}
    except (OSError, RuntimeError):
        return None
    for name in PREFERRED_SENSORS:
        readings = groups.get(name)
        if readings:
            return float(readings[0].current)
    for readings in groups.values():
        if readings:
            return float(readings[0].current)
    return None


class SystemMonitor:
    def __init__(
        self,
        bus: Bus,
        data_dir: Path,
        interval_s: float = 5.0,
        psutil_module: Any = psutil,
        loadavg: Callable[[], tuple[float, float, float]] | None = os.getloadavg,
    ) -> None:
        self.bus = bus
        self.data_dir = Path(data_dir)
        self.interval_s = interval_s
        self._ps = psutil_module
        self._loadavg = loadavg

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
            except Exception:  # a psutil hiccup must not kill the monitor
                log.exception("system snapshot failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.interval_s)
