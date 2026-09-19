"""Writes one history row per receiver epoch, rolls minutes up, prunes old rows."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from mtrtk.core.bus import Bus
from mtrtk.core.state import ReceiverState
from mtrtk.store.db import Database
from mtrtk.store.models import SystemStats

log = logging.getLogger(__name__)

SAMPLE_COLUMNS = (
    "ts",
    "lat",
    "lon",
    "height_m",
    "hmsl_m",
    "h_acc_m",
    "v_acc_m",
    "fix_type",
    "carr_soln",
    "nsat_used",
    "nsat_tracked",
    "pdop",
    "hdop",
    "vdop",
    "cno_mean",
    "jam_ind",
    "agc_cnt",
    "noise_per_ms",
    "corr_age_s",
    "baseline_m",
    "rtcm_bytes_per_s",
    "ntrip_clients",
    "cpu_pct",
    "mem_pct",
    "disk_free_gb",
    "temp_c",
)
_INSERT_1S = (
    f"INSERT OR REPLACE INTO samples_1s ({', '.join(SAMPLE_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(SAMPLE_COLUMNS))})"
)
_ROLLUP_1M = """
INSERT OR REPLACE INTO samples_1m (
    ts, n, lat_avg, lon_avg, height_avg, h_acc_avg, h_acc_max, v_acc_avg, v_acc_max,
    fix_type_min, carr_soln_min, nsat_used_avg, nsat_used_min, nsat_tracked_avg,
    pdop_avg, pdop_max, cno_mean_avg, jam_ind_max, agc_cnt_avg, noise_per_ms_avg,
    corr_age_max, baseline_avg, rtcm_bytes_avg, ntrip_clients_max,
    cpu_pct_avg, mem_pct_avg, disk_free_gb_min, temp_c_max)
SELECT ?, COUNT(*), AVG(lat), AVG(lon), AVG(height_m), AVG(h_acc_m), MAX(h_acc_m), AVG(v_acc_m),
    MAX(v_acc_m),
    MIN(fix_type), MIN(carr_soln), AVG(nsat_used), MIN(nsat_used), AVG(nsat_tracked),
    AVG(pdop), MAX(pdop), AVG(cno_mean), MAX(jam_ind), AVG(agc_cnt), AVG(noise_per_ms),
    MAX(corr_age_s), AVG(baseline_m), AVG(rtcm_bytes_per_s), MAX(ntrip_clients),
    AVG(cpu_pct), AVG(mem_pct), MIN(disk_free_gb), MAX(temp_c)
FROM samples_1s WHERE ts >= ? AND ts < ?
"""
_TABLES = {"samples_1s", "samples_1m"}
_PRUNE_INTERVAL_S = 3600.0


class Sampler:
    """One `samples_1s` row per receiver second, a `samples_1m` rollup per minute.

    Receiver UTC alone dates a row: the host clock may be wrong or stepping, and a row dated by it
    would not line up with the raw log. An epoch without receiver time is therefore dropped, and
    two epochs inside the same second collapse onto one row (`ts` is the primary key).
    """

    def __init__(
        self, bus: Bus, db: Database, keep_1s_h: float = 24, keep_1m_d: float = 90
    ) -> None:
        self.bus = bus
        self.db = db
        self.keep_1s_s = keep_1s_h * 3600
        self.keep_1m_s = keep_1m_d * 86400
        self.sub = bus.subscribe("state.epoch", "system.stats", "ntrip.clients", maxsize=200)
        self._system: SystemStats | None = None
        self._ntrip_clients = 0
        self._current_minute: int | None = None
        self._last_prune = 0.0
        self._columns: dict[str, frozenset[str]] = {}

    # ------------------------------------------------------------- mapping
    @staticmethod
    def sample_row(
        state: ReceiverState, system: SystemStats | None, ntrip_clients: int
    ) -> dict[str, Any] | None:
        if state.time.utc is None:
            return None
        used_cno = [s.cno for s in state.sats if s.used and s.cno > 0]
        hw = state.hardware
        row: dict[str, Any] = {
            # NAV-PVT dates a fix to the nanosecond, and its `nano` correction is signed: 16:00:00
            # arrives as 15:59:59.999983 as readily as 16:00:00.000017. Rounding to the nominal
            # second is what makes `ts` a primary key one epoch per second can land on.
            "ts": float(round(state.time.utc.timestamp())),
            "lat": state.position.lat,
            "lon": state.position.lon,
            "height_m": state.position.height_m,
            "hmsl_m": state.position.hmsl_m,
            "h_acc_m": state.accuracy.h_acc_m,
            "v_acc_m": state.accuracy.v_acc_m,
            "fix_type": state.fix.fix_type,
            "carr_soln": state.fix.carr_soln,
            "nsat_used": state.sat_summary.used,
            "nsat_tracked": state.sat_summary.tracked,
            "pdop": state.dops.p,
            "hdop": state.dops.h,
            "vdop": state.dops.v,
            "cno_mean": sum(used_cno) / len(used_cno) if used_cno else None,
            "jam_ind": hw.jam_ind if hw else None,
            "agc_cnt": hw.agc_cnt if hw else None,
            "noise_per_ms": hw.noise_per_ms if hw else None,
            "corr_age_s": None,  # rover phase fills this
            "baseline_m": None,  # rover phase fills this
            "rtcm_bytes_per_s": state.rtcm_out.bytes_per_s,
            "ntrip_clients": ntrip_clients,
            "cpu_pct": system.cpu_pct if system else None,
            "mem_pct": system.mem_pct if system else None,
            "disk_free_gb": system.disk_free_gb if system else None,
            "temp_c": system.temp_c if system else None,
        }
        return row

    # ------------------------------------------------------------- storage
    async def insert(self, row: dict[str, Any] | None) -> None:
        if row is None:
            return
        await self.db.execute(_INSERT_1S, [row[c] for c in SAMPLE_COLUMNS])
        await self.db.commit()

    async def rollup_minute(self, minute_ts: float) -> None:
        """Aggregate the minute containing `minute_ts`; a minute with no rows leaves no row."""
        start = float(int(minute_ts) // 60 * 60)
        # One unit: nothing may read the COUNT(*) = 0 placeholder the aggregate always inserts.
        async with self.db.transaction():
            await self.db.execute(_ROLLUP_1M, (start, start, start + 60))
            await self.db.execute("DELETE FROM samples_1m WHERE ts = ? AND n = 0", (start,))

    async def prune(self, now_ts: float) -> None:
        async with self.db.transaction():
            await self.db.execute("DELETE FROM samples_1s WHERE ts < ?", (now_ts - self.keep_1s_s,))
            await self.db.execute("DELETE FROM samples_1m WHERE ts < ?", (now_ts - self.keep_1m_s,))

    async def history(
        self, table: str, start_ts: float, end_ts: float, columns: list[str]
    ) -> list[dict[str, Any]]:
        """Rows in `[start_ts, end_ts)`. Table and columns are checked against the schema because
        SQLite cannot parameterise either, and the API layer passes both through from a query."""
        if table not in _TABLES:
            raise ValueError(f"unknown table {table}")
        if not columns:
            raise ValueError("no columns requested")
        known = await self._table_columns(table)
        unknown = [c for c in columns if c not in known]
        if unknown:
            raise ValueError(f"unknown column {', '.join(unknown)}")
        cols = ", ".join(columns)
        rows = await self.db.fetchall(
            f"SELECT {cols} FROM {table} WHERE ts >= ? AND ts < ? ORDER BY ts", (start_ts, end_ts)
        )
        return [dict(r) for r in rows]

    async def _table_columns(self, table: str) -> frozenset[str]:
        """The table's real column names, read once (`table` is already known to be ours)."""
        known = self._columns.get(table)
        if known is None:
            rows = await self.db.fetchall(f"PRAGMA table_info({table})")
            known = frozenset(str(r["name"]) for r in rows)
            self._columns[table] = known
        return known

    # ------------------------------------------------------------- run loop
    def stop(self) -> None:
        """End `run()`: the queued epochs still drain before the loop exits."""
        self.bus.unsubscribe(self.sub)

    async def run(self, stop: asyncio.Event) -> None:
        waiter = asyncio.create_task(self._wait_stop(stop), name="sampler-stop")
        try:
            async for topic, item in self.sub:
                if topic == "system.stats":
                    self._system = item
                elif topic == "ntrip.clients":
                    self._ntrip_clients = len(item)
                elif topic == "state.epoch":
                    await self._on_epoch(item)
                if stop.is_set():
                    break
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            self.stop()

    async def _wait_stop(self, stop: asyncio.Event) -> None:
        """A silent receiver must not wedge `run()`: closing the subscription ends the loop."""
        await stop.wait()
        self.stop()

    async def _on_epoch(self, state: ReceiverState) -> None:
        row = self.sample_row(state, self._system, self._ntrip_clients)
        if row is None:
            return
        try:
            await self.insert(row)
            minute = int(row["ts"]) // 60
            if self._current_minute is not None and minute != self._current_minute:
                await self.rollup_minute(self._current_minute * 60)
            self._current_minute = minute
            now = time.monotonic()
            if now - self._last_prune > _PRUNE_INTERVAL_S:
                await self.prune(row["ts"])
                self._last_prune = now
        except Exception:  # never let a DB hiccup stop sampling
            log.exception("sampler write failed")
