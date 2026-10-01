"""Field sessions: named time ranges that group survey points and raw logs.

At most one session is open (has no `end_utc`) at a time: starting a new one closes the open one
in the same transaction, so no reader ever sees two open sessions or none in between.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite

from mtrtk.store.db import Database
from mtrtk.store.models import Session


def _row(r: aiosqlite.Row) -> Session:
    d = dict(r)
    d["start_utc"] = datetime.fromisoformat(d["start_utc"])
    d["end_utc"] = datetime.fromisoformat(d["end_utc"]) if d.get("end_utc") else None
    return Session(**d)


def _now() -> str:
    return datetime.now(UTC).isoformat()


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

    async def start(self, name: str | None, role: str, notes: str | None = None) -> Session:
        """Open a new session, closing whichever one is open first."""
        async with self.db.transaction():
            now = _now()
            await self.db.execute("UPDATE sessions SET end_utc = ? WHERE end_utc IS NULL", (now,))
            cur = await self.db.execute(
                "INSERT INTO sessions (name, start_utc, role, notes) VALUES (?,?,?,?)",
                (name, now, role, notes),
            )
            new_id = cur.lastrowid
        assert new_id is not None
        session = await self.get(new_id)
        assert session is not None
        return session

    async def stop(self) -> Session | None:
        """Close the open session; `None` when there was none."""
        async with self.db.transaction():
            current = await self.current()
            if current is None:
                return None
            await self.db.execute(
                "UPDATE sessions SET end_utc = ? WHERE end_utc IS NULL", (_now(),)
            )
        assert current.id is not None
        return await self.get(current.id)

    async def list(self, limit: int = 100) -> list[Session]:
        """Newest first."""
        rows = await self.db.fetchall("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,))
        return [_row(r) for r in rows]
