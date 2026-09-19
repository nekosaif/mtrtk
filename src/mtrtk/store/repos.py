"""Small repository classes over Database. One class per table family; no ORM."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from mtrtk.rawlog.writer import Sidecar, sidecar_path
from mtrtk.store.db import Database
from mtrtk.store.models import Event, Level, NtripClientRecord, Site

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SitesRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _row_to_site(row: aiosqlite.Row) -> Site:
        data = dict(row)
        data["active"] = bool(data["active"])
        return Site(**data)

    async def list(self) -> list[Site]:
        rows = await self.db.fetchall("SELECT * FROM sites ORDER BY name")
        return [self._row_to_site(r) for r in rows]

    async def get(self, name: str) -> Site | None:
        row = await self.db.fetchone("SELECT * FROM sites WHERE name = ?", (name,))
        return self._row_to_site(row) if row else None

    async def active(self) -> Site | None:
        row = await self.db.fetchone("SELECT * FROM sites WHERE active = 1 LIMIT 1")
        return self._row_to_site(row) if row else None

    async def add(self, site: Site) -> Site:
        try:
            async with self.db.transaction():
                if await self.db.fetchone("SELECT id FROM sites WHERE name = ?", (site.name,)):
                    raise ValueError(f"site {site.name!r} already exists")
                await self._insert(site)
        except aiosqlite.IntegrityError as exc:  # another process won the race for the name
            raise ValueError(f"site {site.name!r} already exists") from exc
        stored = await self.get(site.name)
        assert stored is not None
        return stored

    async def _insert(self, site: Site) -> None:
        await self.db.execute(
            """INSERT INTO sites (name, x, y, z, lat, lon, height_m, sigma_x, sigma_y, sigma_z,
                                  frame, epoch, source, notes, created_utc, active)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (
                site.name,
                site.x,
                site.y,
                site.z,
                site.lat,
                site.lon,
                site.height_m,
                site.sigma_x,
                site.sigma_y,
                site.sigma_z,
                site.frame,
                site.epoch,
                site.source,
                site.notes,
                _now(),
            ),
        )

    async def delete(self, name: str) -> None:
        """Delete a site. Unknown names are a no-op; the active one raises `ValueError`.

        A running base is broadcasting the active site's ARP, so deleting that row would leave
        the receiver on a position nothing can name or verify any more. The guard is here rather
        than in each caller so the CLI, the API and anything later are all held to it, and so the
        check and the DELETE are one transaction - an activation racing the delete cannot slip
        between them.
        """
        async with self.db.transaction():
            row = await self.db.fetchone("SELECT active FROM sites WHERE name = ?", (name,))
            if row is not None and row["active"]:
                raise ValueError(f"{name} is the active site; activate another site first")
            await self.db.execute("DELETE FROM sites WHERE name = ?", (name,))

    async def activate(self, name: str) -> Site:
        """Exactly one row ends up active: both updates land in a single transaction."""
        async with self.db.transaction():
            if await self.db.fetchone("SELECT id FROM sites WHERE name = ?", (name,)) is None:
                raise KeyError(name)
            await self.db.execute("UPDATE sites SET active = 0 WHERE active = 1")
            await self.db.execute("UPDATE sites SET active = 1 WHERE name = ?", (name,))
        site = await self.get(name)
        assert site is not None
        return site


class EventsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(
        self, level: Level, kind: str, message: str, meta: dict[str, Any] | None = None
    ) -> Event:
        ts = _now()
        cur = await self.db.execute(
            "INSERT INTO events (ts_utc, level, kind, message, meta) VALUES (?,?,?,?,?)",
            # `default=str`: an alert's meta carries whatever the event carried - a Path, a
            # datetime - and losing the event to a TypeError is worse than a stringified value.
            (ts, level, kind, message, json.dumps(meta or {}, default=str)),
        )
        await self.db.commit()
        return Event(
            id=cur.lastrowid,
            ts_utc=datetime.fromisoformat(ts),
            level=level,
            kind=kind,
            message=message,
            meta=meta or {},
        )

    async def list(self, limit: int = 200, level: Level | None = None) -> list[Event]:
        if level:
            rows = await self.db.fetchall(
                "SELECT * FROM events WHERE level = ? ORDER BY id DESC LIMIT ?", (level, limit)
            )
        else:
            rows = await self.db.fetchall("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [
            Event(
                id=r["id"],
                ts_utc=datetime.fromisoformat(r["ts_utc"]),
                level=r["level"],
                kind=r["kind"],
                message=r["message"],
                meta=json.loads(r["meta"] or "{}"),
                acked=bool(r["acked"]),
            )
            for r in rows
        ]

    async def ack(self, event_id: int) -> None:
        await self.db.execute("UPDATE events SET acked = 1 WHERE id = ?", (event_id,))
        await self.db.commit()


class NtripLogRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def connected(
        self, ip: str, mountpoint: str, user_agent: str, username: str | None
    ) -> int:
        cur = await self.db.execute(
            """INSERT INTO ntrip_clients_log (ip, mountpoint, user_agent, username, connected_utc)
               VALUES (?,?,?,?,?)""",
            (ip, mountpoint, user_agent, username, _now()),
        )
        await self.db.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    async def disconnected(
        self,
        row_id: int,
        bytes_sent: int,
        last_lat: float | None,
        last_lon: float | None,
        reason: str,
    ) -> None:
        await self.db.execute(
            """UPDATE ntrip_clients_log
                  SET disconnected_utc=?, bytes_sent=?, last_lat=?, last_lon=?, reason=?
                WHERE id=?""",
            (_now(), bytes_sent, last_lat, last_lon, reason, row_id),
        )
        await self.db.commit()

    async def recent(self, limit: int = 100) -> list[NtripClientRecord]:
        rows = await self.db.fetchall(
            "SELECT * FROM ntrip_clients_log ORDER BY id DESC LIMIT ?", (limit,)
        )
        out: list[NtripClientRecord] = []
        for r in rows:
            data = dict(r)
            data["connected_utc"] = datetime.fromisoformat(data["connected_utc"])
            if data["disconnected_utc"]:
                data["disconnected_utc"] = datetime.fromisoformat(data["disconnected_utc"])
            out.append(NtripClientRecord(**data))
        return out


class LogFilesRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert(self, path: Path, sidecar: Sidecar) -> None:
        await self.db.execute(
            """INSERT INTO log_files (path, hour_utc, start_utc, end_utc, bytes, keep, sha256,
                                      msg_counts, role, site, complete)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET hour_utc=excluded.hour_utc,
                 start_utc=excluded.start_utc, end_utc=excluded.end_utc, bytes=excluded.bytes,
                 sha256=excluded.sha256, msg_counts=excluded.msg_counts, role=excluded.role,
                 site=excluded.site, complete=excluded.complete""",
            (
                str(path),
                sidecar.hour_utc,
                sidecar.start_utc,
                sidecar.end_utc,
                sidecar.bytes,
                int(sidecar.keep),
                sidecar.sha256,
                json.dumps(sidecar.msg_counts),
                sidecar.role,
                sidecar.site,
                int(sidecar.complete),
            ),
        )
        await self.db.commit()

    async def set_keep(self, path: Path, keep: bool) -> None:
        """Write the flag to the sidecar first, then mirror it into the row.

        Retention reads `keep` off the sidecar on disk, so a flag that lived only in the database
        would not stop the sweeper from deleting the file.
        """
        sc_path = sidecar_path(path)
        try:
            sidecar = Sidecar.load(sc_path)
        except (OSError, TypeError, ValueError):  # json.JSONDecodeError is a ValueError
            log.warning("no usable sidecar at %s; keep recorded in the database only", sc_path)
        else:
            sidecar.keep = keep
            sidecar.dump(sc_path)
        await self.db.execute(
            "UPDATE log_files SET keep = ? WHERE path = ?", (int(keep), str(path))
        )
        await self.db.commit()

    async def delete(self, path: Path) -> None:
        await self.db.execute("DELETE FROM log_files WHERE path = ?", (str(path),))
        await self.db.commit()

    async def list(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM log_files ORDER BY hour_utc")
        return [dict(r) for r in rows]
