"""Field sessions: named time ranges that group survey points and raw logs.

At most one session is open (has no `end_utc`) at a time: starting a new one closes the open one
in the same transaction, so no reader ever sees two open sessions or none in between.

Sessions are stamped on the receiver's UTC, the clock the raw-log hours, the points and the
history are already on (PPK takes a session's window straight from `start_utc..end_utc`). The
host clock is only the fallback while the receiver has no time it vouches for: a field Pi with
no RTC and no network boots on its fake-hwclock time, which can be days off.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

import aiosqlite

from mtrtk.core.state import ReceiverState
from mtrtk.store.db import Database
from mtrtk.store.models import Session

log = logging.getLogger(__name__)

# An epoch older than this no longer says what time it is now (receiver unplugged, stream stuck).
RECEIVER_TIME_MAX_AGE_S = 5.0
# Host and receiver further apart than this are worth a warning: the host clock is wrong.
CLOCK_DISAGREE_S = 5.0


def session_clock(state: ReceiverState | None) -> datetime:
    """Now on the receiver's UTC when it vouches for it and is current, else the host clock."""
    host = datetime.now(UTC)
    if state is None or not state.connected:
        return host
    t = state.time
    if t.utc is None or not (t.valid_date and t.valid_time) or state.last_epoch_mono is None:
        return host
    age = time.monotonic() - state.last_epoch_mono
    if not 0 <= age <= RECEIVER_TIME_MAX_AGE_S:
        return host
    utc = t.utc if t.utc.tzinfo is not None else t.utc.replace(tzinfo=UTC)
    now = utc + timedelta(seconds=age)
    if abs((now - host).total_seconds()) > CLOCK_DISAGREE_S:
        log.warning(
            "host clock is %.0f s off the receiver's UTC; the session uses the receiver's",
            (host - now).total_seconds(),
        )
    return now


def _row(r: aiosqlite.Row) -> Session:
    d = dict(r)
    d["start_utc"] = datetime.fromisoformat(d["start_utc"])
    d["end_utc"] = datetime.fromisoformat(d["end_utc"]) if d.get("end_utc") else None
    return Session(**d)


def _stamp(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    return now if now.tzinfo is not None else now.replace(tzinfo=UTC)


def _end(start: datetime, now: datetime) -> str:
    """The end stamp, never before the start: the clock may step back between the two (host
    time at start, the receiver's once it has a fix)."""
    return max(start, now).isoformat()


class SessionsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def current(self) -> Session | None:
        row = await self.db.fetchone(
            "SELECT * FROM sessions WHERE end_utc IS NULL ORDER BY id DESC LIMIT 1"
        )
        return _row(row) if row else None

    async def get(self, session_id: int) -> Session | None:
        row = await self.db.fetchone("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return _row(row) if row else None

    async def start(
        self,
        name: str | None,
        role: str,
        notes: str | None = None,
        now: datetime | None = None,
    ) -> Session:
        """Open a new session at *now* (see `session_clock`), closing the open one first."""
        stamp = _stamp(now)
        async with self.db.transaction():
            current = await self.current()
            if current is not None:
                await self.db.execute(
                    "UPDATE sessions SET end_utc = ? WHERE id = ?",
                    (_end(current.start_utc, stamp), current.id),
                )
            cur = await self.db.execute(
                "INSERT INTO sessions (name, start_utc, role, notes) VALUES (?,?,?,?)",
                (name, stamp.isoformat(), role, notes),
            )
            new_id = cur.lastrowid
        assert new_id is not None
        session = await self.get(new_id)
        assert session is not None
        return session

    async def stop(self, now: datetime | None = None) -> Session | None:
        """Close the open session at *now*; `None` when there was none."""
        stamp = _stamp(now)
        async with self.db.transaction():
            current = await self.current()
            if current is None:
                return None
            await self.db.execute(
                "UPDATE sessions SET end_utc = ? WHERE id = ?",
                (_end(current.start_utc, stamp), current.id),
            )
        assert current.id is not None
        return await self.get(current.id)

    async def list(self, limit: int = 100) -> list[Session]:
        """Newest first."""
        rows = await self.db.fetchall("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,))
        return [_row(r) for r in rows]
