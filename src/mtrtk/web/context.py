"""Objects the web layer needs from the running daemon."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.statestore import StateStore
from mtrtk.store.db import Database


@dataclass
class AppContext:
    settings: Settings
    bus: Bus
    store: StateStore
    db: Database
    daemon: Any  # Daemon (or a stand-in in tests) exposing controller / caster / basemode / stop
    jobs: Any = None  # mtrtk.jobs.JobRunner | None once that module exists (Phase 3 Task 9)
    started_mono: float = field(default_factory=time.monotonic)

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
