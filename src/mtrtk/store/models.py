"""Persistent records shared by repositories, the API and the UI."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from mtrtk.core.geo import ecef_to_llh


class Site(BaseModel):
    id: int | None = None
    name: str
    x: float
    y: float
    z: float
    lat: float | None = None
    lon: float | None = None
    height_m: float | None = None
    sigma_x: float | None = None
    sigma_y: float | None = None
    sigma_z: float | None = None
    frame: str = "ITRF2020"
    epoch: str | None = None
    source: str  # survey-in | csrs-ppp | auspos | opus | manual
    notes: str | None = None
    created_utc: datetime | None = None
    active: bool = False

    @classmethod
    def from_ecef(
        cls,
        name: str,
        x: float,
        y: float,
        z: float,
        *,
        source: str,
        sigma_m: float | None = None,
        frame: str = "ITRF2020",
        epoch: str | None = None,
        notes: str | None = None,
    ) -> Site:
        lat, lon, h = ecef_to_llh(x, y, z)
        return cls(
            name=name,
            x=x,
            y=y,
            z=z,
            lat=lat,
            lon=lon,
            height_m=h,
            sigma_x=sigma_m,
            sigma_y=sigma_m,
            sigma_z=sigma_m,
            frame=frame,
            epoch=epoch,
            source=source,
            notes=notes,
        )

    @property
    def sigma_3d(self) -> float | None:
        sx, sy, sz = self.sigma_x, self.sigma_y, self.sigma_z
        if sx is None or sy is None or sz is None:
            return None
        return math.hypot(sx, sy, sz)


Level = Literal["info", "warning", "error"]


class Event(BaseModel):
    id: int | None = None
    ts_utc: datetime
    level: Level
    kind: str
    message: str
    meta: dict[str, Any] = Field(default_factory=dict)
    acked: bool = False


class NtripClientRecord(BaseModel):
    id: int
    ip: str | None
    mountpoint: str | None
    user_agent: str | None
    username: str | None
    connected_utc: datetime
    disconnected_utc: datetime | None = None
    bytes_sent: int = 0
    last_lat: float | None = None
    last_lon: float | None = None
    reason: str | None = None


class SystemStats(BaseModel):
    cpu_pct: float
    mem_pct: float
    disk_free_gb: float
    disk_used_pct: float
    uptime_s: float
    temp_c: float | None = None
    load1: float | None = None
    ts_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))
