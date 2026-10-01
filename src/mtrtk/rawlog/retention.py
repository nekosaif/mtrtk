"""Deletes the oldest unkept raw logs when free disk space falls below the configured floor.

Derived RINEX goes with the raw hours it came from: before an hour is deleted, `reclaim` (the
job runner's `prune_exports`) removes the finished export jobs whose window ended by the end of
that hour. An export still being built is not counted against the floor - its staging is
temporary, and pruning a week of raw history to make room for it would be the wrong trade.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

from mtrtk.core.bus import Bus
from mtrtk.rawlog.index import LogFile, list_logs

log = logging.getLogger(__name__)
GB = 1e9
# Temporary export space under DATA_DIR: a job's staging directory, a download's working one.
STAGING_GLOBS = ("jobs/*/.export-*", "tmp/export-*")

Reclaim = Callable[[datetime], Awaitable[object]]


def staging_bytes(root: Path) -> int:
    """Bytes held by exports in progress (their staging and working directories)."""
    total = 0
    for pattern in STAGING_GLOBS:
        for directory in Path(root).glob(pattern):
            for path in directory.rglob("*"):
                with contextlib.suppress(OSError):
                    if path.is_file():
                        total += path.stat().st_size
    return total


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
        reclaim: Reclaim | None = None,
    ) -> None:
        self.root = Path(root)
        self.min_free_gb = min_free_gb
        self.bus = bus
        self._disk_usage = disk_usage
        self.reclaim = reclaim
        self.runs = 0
        self._staging = 0  # bytes of exports in progress, measured once per pass

    def free_gb(self) -> float:
        """Free space as the floor sees it: what an export in progress holds counts as free."""
        target = self.root if self.root.exists() else self.root.parent
        return (self._disk_usage(target).free + self._staging) / GB

    def prune_once(self) -> list[Path]:
        """One pass, scanning the tree on the calling thread, with no `reclaim`. `prune()` is
        the async form."""
        self._staging = staging_bytes(self.root)
        return self._prune(list_logs(self.root))

    async def prune(self) -> list[Path]:
        """One pass whose directory walk runs in a worker thread.

        The walk stats every hour of every day kept on the card; on a full SD card that is
        thousands of files, and it used to run once per deleted file, on the event loop. The
        deletions and `rawlog.pruned` stay on the loop: bus subscribers are asyncio queues.
        """
        self._staging = await asyncio.to_thread(staging_bytes, self.root)
        logs = await asyncio.to_thread(list_logs, self.root)
        if self.reclaim is None:
            return self._prune(logs)
        self.runs += 1
        deleted: list[Path] = []
        for victim in self._prunable(logs):
            if self.free_gb() >= self.min_free_gb:
                break
            # The exports made of hours up to this one go first: they are derived from raw
            # data that is about to go, and they may free enough on their own.
            await self.reclaim(victim.hour_end)
            if self.free_gb() >= self.min_free_gb:
                break
            self._delete(victim)
            deleted.append(victim.path)
        return deleted

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
