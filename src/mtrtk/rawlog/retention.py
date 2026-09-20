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
        """One pass, scanning the tree on the calling thread. `prune()` is the async form."""
        return self._prune(list_logs(self.root))

    async def prune(self) -> list[Path]:
        """One pass whose directory walk runs in a worker thread.

        The walk stats every hour of every day kept on the card; on a full SD card that is
        thousands of files, and it used to run once per deleted file, on the event loop. The
        deletions and `rawlog.pruned` stay on the loop: bus subscribers are asyncio queues.
        """
        return self._prune(await asyncio.to_thread(list_logs, self.root))

    def _prune(self, logs: list[LogFile]) -> list[Path]:
        self.runs += 1
        deleted: list[Path] = []
        candidates = iter(self._prunable(logs))
        while self.free_gb() < self.min_free_gb:
            victim = next(candidates, None)
            if victim is None:
                break
            self._delete(victim)
            deleted.append(victim.path)
            if self.free_gb() >= self.min_free_gb:
                break  # back above the floor: leave the rest of the listing alone
        return deleted

    def _prunable(self, logs: list[LogFile]) -> list[LogFile]:
        """Oldest first, never the newest file (the writer probably still has it open) and
        never one an operator marked `keep`."""
        if len(logs) < 2:
            return []
        candidates = [lf for lf in logs[:-1] if not lf.keep]
        if not candidates:
            log.warning("disk below %.1f GB but every older log is marked keep", self.min_free_gb)
        return candidates

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
                await self.prune()
            except OSError:
                log.exception("retention pass failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
