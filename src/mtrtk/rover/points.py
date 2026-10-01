"""Averaged survey points collected over N epochs, optionally from RTK-fixed epochs only.

Averaging works in a local tangent plane: every accepted epoch becomes an ENU offset from the
first accepted epoch, `sd_n`/`sd_e`/`sd_u` are the sample standard deviations (n-1) of those
offsets, and the stored position is the mean offset mapped back to geodetic coordinates. Mapping
the mean ENU offset back is the same as taking the mean ECEF position, which is how it is done
here (as offsets from the reference, so nothing is summed at 6e6 m magnitude).
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime
from typing import Literal

import aiosqlite
from pydantic import BaseModel

from mtrtk.core.bus import Bus
from mtrtk.core.geo import ecef_to_enu, ecef_to_llh, llh_to_ecef
from mtrtk.core.state import ReceiverState
from mtrtk.core.statestore import StateStore
from mtrtk.rover.sessions import SessionsRepo
from mtrtk.store.db import Database
from mtrtk.store.models import Point

log = logging.getLogger(__name__)

# A collection gives up once it has skipped this many times its target epoch count: at 5 Hz and
# the default 30 epochs that is 30 s without a usable solution.
ABORT_SKIP_FACTOR = 5
MAX_EPOCHS = 3600
CARR_FIXED = 2
FIX_3D_TYPES = (3, 4)  # 3D, GNSS + dead reckoning: the only fixes with a meaningful height
# A point's fix type is the worst one among its epochs: a plain GNSS 3D fix ranks above GNSS + DR.
_FIX_RANK = {3: 1, 4: 0}

CollectState = Literal["idle", "collecting", "done", "aborted"]


class CollectStatus(BaseModel):
    state: CollectState = "idle"
    name: str | None = None
    target: int = 0
    accepted: int = 0
    skipped: int = 0
    sd_n: float | None = None
    sd_e: float | None = None
    sd_u: float | None = None
    mean_lat: float | None = None
    mean_lon: float | None = None
    mean_h: float | None = None
    point_id: int | None = None
    reason: str | None = None


_POINT_COLUMNS = (
    "session_id",
    "name",
    "code",
    "note",
    "ts_utc",
    "lat",
    "lon",
    "height_m",
    "hmsl_m",
    "n_epochs",
    "sd_n",
    "sd_e",
    "sd_u",
    "fix_type",
    "carr_soln",
    "h_acc_m",
    "v_acc_m",
)


class PointsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _row(r: aiosqlite.Row) -> Point:
        d = dict(r)
        d["ts_utc"] = datetime.fromisoformat(d["ts_utc"])
        return Point(**d)

    async def add(self, p: Point) -> Point:
        values = p.model_dump(include=set(_POINT_COLUMNS))
        values["ts_utc"] = p.ts_utc.isoformat()
        placeholders = ",".join("?" for _ in _POINT_COLUMNS)
        cur = await self.db.execute(
            f"INSERT INTO points ({', '.join(_POINT_COLUMNS)}) VALUES ({placeholders})",
            [values[c] for c in _POINT_COLUMNS],
        )
        await self.db.commit()
        assert cur.lastrowid is not None
        stored = await self.get(cur.lastrowid)
        assert stored is not None
        return stored

    async def get(self, point_id: int) -> Point | None:
        row = await self.db.fetchone("SELECT * FROM points WHERE id = ?", (point_id,))
        return self._row(row) if row else None

    async def list(self, session_id: int | None = None, limit: int = 1000) -> list[Point]:
        """Newest first; only `session_id`'s points when it is given."""
        if session_id is None:
            rows = await self.db.fetchall("SELECT * FROM points ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM points WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            )
        return [self._row(r) for r in rows]

    async def update(
        self,
        point_id: int,
        name: str | None = None,
        code: str | None = None,
        note: str | None = None,
    ) -> Point:
        """Change the given descriptive fields (`None` leaves one as it is).

        Raises `KeyError` when there is no such point.
        """
        given = (("name", name), ("code", code), ("note", note))
        fields = {k: v for k, v in given if v is not None}
        if fields:
            assignments = ", ".join(f"{k} = ?" for k in fields)
            await self.db.execute(
                f"UPDATE points SET {assignments} WHERE id = ?", [*fields.values(), point_id]
            )
            await self.db.commit()
        point = await self.get(point_id)
        if point is None:
            raise KeyError(point_id)
        return point

    async def delete(self, point_id: int) -> bool:
        """True when a point was deleted."""
        cur = await self.db.execute("DELETE FROM points WHERE id = ?", (point_id,))
        await self.db.commit()
        return cur.rowcount > 0


def _worst(current: float | None, value: float | None) -> float | None:
    """The larger (worse) accuracy; an unknown one never hides a known one."""
    if current is None:
        return value
    return current if value is None else max(current, value)


def _sd(values: list[float]) -> float:
    """Sample standard deviation (n-1); 0.0 for fewer than two values."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


class PointCollector:
    """Collects one point at a time from `state.epoch`.

    Publishes `points.progress` (a `CollectStatus` snapshot) on every epoch it looks at while
    collecting and on every state change, and `points.saved` (the stored `Point`) last, once the
    point is in the database.
    """

    def __init__(
        self,
        bus: Bus,
        store: StateStore,
        points: PointsRepo,
        sessions: SessionsRepo,
        *,
        default_epochs: int = 30,
        default_fixed_only: bool = True,
    ) -> None:
        self.bus, self.store, self.points, self.sessions = bus, store, points, sessions
        self.default_epochs, self.default_fixed_only = default_epochs, default_fixed_only
        self.status = CollectStatus()
        self.sub = bus.subscribe("state.epoch", maxsize=20)
        self._saving = False
        self._reset()

    def _reset(self) -> None:
        self._session_id: int | None = None
        self._code: str | None = None
        self._note: str | None = None
        self._fixed_only = self.default_fixed_only
        self._ref_llh: tuple[float, float, float] | None = None
        self._ref_xyz: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._e: list[float] = []
        self._n: list[float] = []
        self._u: list[float] = []
        self._dxyz: list[tuple[float, float, float]] = []
        self._hmsl: list[float] = []
        self._last: ReceiverState | None = None
        # The quality stored with the point is the worst over all accepted epochs, never that of
        # the last one alone: 29 float epochs and one fixed one average to a float point.
        self._fix_type = 0
        self._carr_soln = 0
        self._h_acc: float | None = None
        self._v_acc: float | None = None

    def _publish(self) -> None:
        # A snapshot: queued progress must not change under a subscriber that reads it later.
        self.bus.publish("points.progress", self.status.model_copy())

    # ------------------------------------------------------------- control
    async def start(
        self,
        name: str,
        code: str | None = None,
        note: str | None = None,
        epochs: int | None = None,
        fixed_only: bool | None = None,
    ) -> CollectStatus:
        if self.status.state == "collecting":
            raise RuntimeError("already collecting a point; cancel it first")
        name = name.strip()
        if not name:
            raise ValueError("a point needs a name")
        target = self.default_epochs if epochs is None else epochs
        if not 1 <= target <= MAX_EPOCHS:
            raise ValueError(f"epochs must be 1-{MAX_EPOCHS}, not {target}")
        # The session open when collection starts owns the point, even if it changes meanwhile.
        session = await self.sessions.current()
        if self.status.state == "collecting":  # another start() won the race during the await
            raise RuntimeError("already collecting a point; cancel it first")
        self._reset()
        self._session_id = session.id if session else None
        self._code, self._note = code, note
        self._fixed_only = self.default_fixed_only if fixed_only is None else fixed_only
        self.status = CollectStatus(state="collecting", name=name, target=target)
        self._publish()
        return self.status.model_copy()

    def cancel(self) -> None:
        """Abandon the point being collected. Too late once its epochs are in and it is saving."""
        if self.status.state == "collecting" and not self._saving:
            self.status.state, self.status.reason = "aborted", "cancelled"
            self._publish()

    # ------------------------------------------------------------- epochs
    def _usable(self, state: ReceiverState) -> bool:
        p, fix = state.position, state.fix
        if p.lat is None or p.lon is None or p.height_m is None or p.invalid_llh:
            return False
        # u-blox: fixType is only meaningful together with gnssFixOK (within the DOP/acc masks).
        if fix.fix_type not in FIX_3D_TYPES or not fix.gnss_fix_ok:
            return False
        if self._fixed_only and fix.carr_soln != CARR_FIXED:
            return False
        return not self._repeats_last(state)

    def _repeats_last(self, state: ReceiverState) -> bool:
        """The same epoch again: NAV-EOE arrived but NAV-PVT for it did not (lost or garbled)."""
        last = self._last
        if last is None:
            return False
        if state.time.itow_ms is not None or last.time.itow_ms is not None:
            return state.time.itow_ms == last.time.itow_ms
        return state.time.utc is not None and state.time.utc == last.time.utc

    def _skip(self) -> None:
        self.status.skipped += 1
        limit = ABORT_SKIP_FACTOR * self.status.target
        if self.status.skipped >= limit:
            wanted = "an RTK fixed solution" if self._fixed_only else "a 3D fix"
            self.status.state = "aborted"
            self.status.reason = f"gave up after {self.status.skipped} epochs without {wanted}"
        self._publish()

    async def on_epoch(self, state: ReceiverState) -> None:
        if self.status.state != "collecting" or self._saving:
            return
        if not self._usable(state):
            self._skip()
            return
        p = state.position
        assert p.lat is not None and p.lon is not None and p.height_m is not None
        xyz = llh_to_ecef(p.lat, p.lon, p.height_m)
        if self._ref_llh is None:
            self._ref_llh, self._ref_xyz = (p.lat, p.lon, p.height_m), xyz
        e, n, u = ecef_to_enu(*self._ref_llh, *xyz)
        self._e.append(e)
        self._n.append(n)
        self._u.append(u)
        x0, y0, z0 = self._ref_xyz
        self._dxyz.append((xyz[0] - x0, xyz[1] - y0, xyz[2] - z0))
        if p.hmsl_m is not None:
            self._hmsl.append(p.hmsl_m)
        self._track_quality(state)
        self._last = state

        st = self.status
        st.accepted += 1
        st.sd_n, st.sd_e, st.sd_u = _sd(self._n), _sd(self._e), _sd(self._u)
        count = len(self._dxyz)
        mean = [self._ref_xyz[i] + sum(d[i] for d in self._dxyz) / count for i in range(3)]
        st.mean_lat, st.mean_lon, st.mean_h = ecef_to_llh(*mean)
        if st.accepted >= st.target:
            await self._finish()
        else:
            self._publish()

    def _track_quality(self, state: ReceiverState) -> None:
        fix, acc = state.fix, state.accuracy
        first = self._last is None
        if first or _FIX_RANK[fix.fix_type] < _FIX_RANK[self._fix_type]:
            self._fix_type = fix.fix_type
        self._carr_soln = fix.carr_soln if first else min(self._carr_soln, fix.carr_soln)
        self._h_acc = _worst(self._h_acc, acc.h_acc_m)
        self._v_acc = _worst(self._v_acc, acc.v_acc_m)

    async def _finish(self) -> None:
        st, last = self.status, self._last
        assert last is not None
        assert st.mean_lat is not None and st.mean_lon is not None and st.mean_h is not None
        self._saving = True
        try:
            point = Point(
                session_id=self._session_id,
                name=st.name or "point",
                code=self._code,
                note=self._note,
                ts_utc=last.time.utc or datetime.now(UTC),
                lat=st.mean_lat,
                lon=st.mean_lon,
                height_m=st.mean_h,
                hmsl_m=sum(self._hmsl) / len(self._hmsl) if self._hmsl else None,
                n_epochs=st.accepted,
                sd_n=st.sd_n or 0.0,
                sd_e=st.sd_e or 0.0,
                sd_u=st.sd_u or 0.0,
                fix_type=self._fix_type,
                carr_soln=self._carr_soln,
                h_acc_m=self._h_acc,
                v_acc_m=self._v_acc,
            )
            stored = await self.points.add(point)
        except Exception:
            # Aborted here, not only in run(): a direct caller must not retry the save with one
            # epoch more than the target.
            st.state, st.reason = "aborted", "save failed"
            self._publish()
            raise
        finally:
            self._saving = False
        st.state, st.point_id = "done", stored.id
        self._publish()
        self.bus.publish("points.saved", stored)

    # ------------------------------------------------------------- run loop
    def stop(self) -> None:
        """End `run()`: the queued epochs still drain before the loop exits."""
        self.bus.unsubscribe(self.sub)

    async def run(self, stop: asyncio.Event) -> None:
        waiter = asyncio.create_task(self._wait_stop(stop), name="points-stop")
        try:
            async for _, state in self.sub:
                try:
                    await self.on_epoch(state)
                except Exception:
                    # One failed point, never the collector: the next start() works again.
                    log.exception("point collection failed")
                    if self.status.state == "collecting":
                        self.status.state, self.status.reason = "aborted", "internal error"
                        self._publish()
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
