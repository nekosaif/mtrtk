"""Objects the web layer needs from the running daemon."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.statestore import StateStore
from mtrtk.store.db import Database

if TYPE_CHECKING:  # imported for the annotation alone: `mtrtk.jobs` needs nothing from the web
    from mtrtk.jobs import JobRunner


@dataclass
class AppContext:
    settings: Settings
    bus: Bus
    store: StateStore
    db: Database
    daemon: Any  # Daemon (or a stand-in in tests) exposing controller / caster / basemode / stop
    jobs: JobRunner | None = None  # None on a daemon that runs no background jobs
    started_mono: float = field(default_factory=time.monotonic)
    # Held across the whole of `apply_settings_change`. Writing `.env` is a read-modify-write in a
    # worker thread, so two requests that overlap - a PUT and a base-mode change, say - would each
    # read the file before the other wrote it, and one of the two changes would be lost.
    settings_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def uptime_s(self) -> float:
        return time.monotonic() - self.started_mono

    @property
    def controller(self) -> Any:
        return getattr(self.daemon, "controller", None)

    @property
    def caster(self) -> Any:
        return getattr(self.daemon, "caster", None)

    @property
    def basemode(self) -> Any:
        return getattr(self.daemon, "basemode", None)
