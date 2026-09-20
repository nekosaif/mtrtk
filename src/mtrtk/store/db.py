"""SQLite access via aiosqlite: WAL mode and numbered SQL migrations from the schema package."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)
_MIGRATION_RE = re.compile(r"^(\d{3})_.*\.sql$")


def _migrations() -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for entry in resources.files("mtrtk.store.schema").iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            found.append((int(m.group(1)), entry.read_text(encoding="utf-8")))
    return sorted(found)


class Database:
    """One aiosqlite connection shared by every repository.

    Because the connection is shared, a statement issued from another task while a multi-statement
    unit is in flight would join that unit: it would read half-applied rows, or its own `commit()`
    would persist them. So every statement is serialised against `transaction()`, which is the only
    way to group writes.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self.user_version = 0
        self._lock = asyncio.Lock()
        self._tx_task: asyncio.Task[Any] | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database not open")
        return self._conn

    async def open(self) -> None:
        """Idempotent, and all-or-nothing: a failure leaves no half-initialised connection.

        `conn` would otherwise hand out a connection whose migrations never ran, and a second
        `open()` would leak the first connection and its thread.
        """
        if self._conn is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: no implicit transactions, so BEGIN/COMMIT are ours alone.
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        self._conn = conn  # _migrate() goes through `self.conn`
        try:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            # WAL lets readers and one writer coexist, but not two writers - and `mtrtk sites add`
            # run against a running daemon is exactly a second writer. Without a busy timeout
            # SQLite fails such a write immediately with "database is locked". `sqlite3.connect`
            # happens to default to the same five seconds, but that is the driver's choice, not
            # ours: stated here so a change to it cannot quietly turn the CLI into a coin toss.
            await conn.execute("PRAGMA busy_timeout=5000")
            await self._migrate()
        except BaseException:
            self._conn = None
            with contextlib.suppress(Exception):
                await conn.close()
            raise

    async def _migrate(self) -> None:
        row = await self.fetchone("PRAGMA user_version")
        current = int(row[0]) if row else 0
        for version, sql in _migrations():
            if version <= current:
                continue
            log.info("applying schema migration %03d", version)
            # Each migration runs as one transaction with its own version bump: SQLite DDL is
            # transactional, so a crash or a bad statement rolls the file back whole and
            # user_version still names the last version that fully applied.
            try:
                await self.conn.executescript(
                    f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version={version};\nCOMMIT;"
                )
            except Exception:
                await self.conn.rollback()
                raise
            current = version
        self.user_version = current

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """`BEGIN IMMEDIATE` … `COMMIT` as one unit; rolls back if the body raises.

        Other tasks' statements wait until it finishes, so nothing outside can observe or commit
        an intermediate state. It does not nest: SQLite has no nested transactions and waiting on
        our own lock would hang, so re-entry is an error.
        """
        if self._owns_transaction():
            raise RuntimeError("transaction already open in this task")
        async with self._lock:
            # Claim the task only once BEGIN has succeeded: a failed BEGIN opened nothing, and a
            # task left pinned to a transaction that never started would bypass `_serialised()`.
            await self.conn.execute("BEGIN IMMEDIATE")
            self._tx_task = asyncio.current_task()
            try:
                yield
            except BaseException:
                await self.conn.rollback()
                raise
            else:
                await self.conn.commit()
            finally:
                self._tx_task = None

    def _owns_transaction(self) -> bool:
        """True when the calling task is the one inside `transaction()`."""
        return self._tx_task is not None and self._tx_task is asyncio.current_task()

    @asynccontextmanager
    async def _serialised(self) -> AsyncIterator[None]:
        """Pass straight through inside our own transaction; otherwise wait for one to finish."""
        if self._owns_transaction():
            yield
        else:
            async with self._lock:
                yield

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        async with self._serialised():
            return await self.conn.execute(sql, tuple(params))

    async def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        """One batch, one unit: a row that fails takes the whole batch with it, and WAL
        commits once instead of once per row."""
        params = [tuple(r) for r in rows]
        if self._owns_transaction():
            await self.conn.executemany(sql, params)
            return
        async with self.transaction():
            await self.conn.executemany(sql, params)

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self._serialised():
            cur = await self.conn.execute(sql, tuple(params))
            return list(await cur.fetchall())

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        async with self._serialised():
            cur = await self.conn.execute(sql, tuple(params))
            return await cur.fetchone()

    async def commit(self) -> None:
        """Only `transaction()` ends the unit it began: a repository's trailing `commit()` called
        from inside a caller's transaction would otherwise persist half of it."""
        if self._owns_transaction():
            return
        async with self._lock:
            await self.conn.commit()
