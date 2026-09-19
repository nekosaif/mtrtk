"""Deletes the oldest unkept raw logs when free disk space falls below the configured floor."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from mtrtk.core.bus import Bus
from mtrtk.rawlog.index import LogFile, list_logs

log = logging.getLogger(__name__)
GB = 1e9


class DiskUsage(Protocol):
    """The one field this policy reads off `shutil.disk_usage`."""

    @property
    def free(self) -> int: ...


class RetentionPolicy:
    """Keeps `min_free_gb` free by deleting whole hours, oldest first.

    Two files are never candidates: anything whose sidecar says `keep`, and the newest hour,
    which is most likely the file the writer still has open.
    """

    def __init__(
        self,
        root: Path,
        min_free_gb: float,
        bus: Bus | None = None,
        disk_usage: Callable[[Path], DiskUsage] = shutil.disk_usage,
    ) -> None:
        self.root = Path(root)
        self.min_free_gb = min_free_gb
        self.bus = bus
        self._disk_usage = disk_usage
        self.runs = 0

    def free_gb(self) -> float:
        target = self.root if self.root.exists() else self.root.parent
        return self._disk_usage(target).free / GB

    def prune_once(self) -> list[Path]:
        self.runs += 1
        deleted: list[Path] = []
        while self.free_gb() < self.min_free_gb:
            victim = self._oldest_prunable()
            if victim is None:
                break
            self._delete(victim)
            deleted.append(victim.path)
            if self.free_gb() >= self.min_free_gb:
                break  # back above the floor: stop before re-scanning the whole tree
        return deleted

    def _oldest_prunable(self) -> LogFile | None:
        logs = list_logs(self.root)
        if len(logs) < 2:  # never touch the newest file (it may be open for writing)
            return None
        candidates = [lf for lf in logs[:-1] if not lf.keep]
        if not candidates:
            log.warning("disk below %.1f GB but every older log is marked keep", self.min_free_gb)
            return None
        return candidates[0]

    def _delete(self, victim: LogFile) -> None:
        victim.path.unlink(missing_ok=True)
        victim.sidecar_path.unlink(missing_ok=True)
        self._remove_empty_parents(victim.path.parent)
        log.info("pruned %s (%d bytes)", victim.path, victim.bytes)
        if self.bus is not None:
            self.bus.publish("rawlog.pruned", victim.path)

    def _remove_empty_parents(self, directory: Path) -> None:
        ubx_root = self.root / "ubx"
        while directory != ubx_root and directory.exists() and not any(directory.iterdir()):
            directory.rmdir()
            directory = directory.parent

    async def run(self, stop: asyncio.Event, interval_s: float = 3600.0) -> None:
        while not stop.is_set():
            try:
                # On the loop thread: a pass publishes `rawlog.pruned`, and the bus queues are
                # asyncio queues, so waking a subscriber must not happen from a worker thread.
                self.prune_once()
            except OSError:
                log.exception("retention pass failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
