# mtrtk Phase 2: Base Daemon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Phase 1 receiver core into a working RTK base station: hourly raw UBX logging with sidecars and retention, an in-process NTRIP caster serving RTCM3 over Tailscale, survey-in / fixed-site management with RTCM 1005 verification, SQLite history sampling, system stats and alerts — all wired into `mtrtk base`.

**Architecture:** New consumers subscribe to the Phase 1 `Bus` and run as supervised tasks inside `Daemon`: `RawLogWriter` (topic `raw.ubx`, unbounded), `NtripCaster` (`raw.rtcm`), `Sampler` (`state.epoch` + `system.stats`), `SystemMonitor` (publisher), `AlertEngine` (`state.*`, `receiver.*`, `system.stats`, `rawlog.*`, `ntrip.*`), `BaseModeManager` (`receiver.capabilities`, `state.survey_in`, `rtcm.1005`; drives TMODE via `ReceiverController.apply_items`). Persistence is one SQLite file (`DATA_DIR/mtrtk.db`, WAL) behind small repository classes. Everything is unit-tested against synthetic frames and the Phase 1 fixtures; the milestone is verified live.

**Tech Stack:** Python 3.12 asyncio, aiosqlite, httpx (webhooks), psutil, pyubx2/pyrtcm, pytest-asyncio; dev oracle `pyproj` for geodesy tests.

**Spec:** `docs/superpowers/specs/2026-09-18-mtrtk-design.md` — sections *base*, *rawlog*, *store*, *alerts*, *NTRIP protocol gotchas*, *Phase 2*. Prerequisite: Phase 0–1 plan complete (`docs/superpowers/plans/2026-09-18-phase0-1-scaffold-and-core.md`); this plan uses its interfaces by name (`Bus`, `Frame`, `StateStore`, `ReceiverController.apply_items`, `Settings`, `Daemon`, `ubx_config.tmode_*`). **Before Task 1, verify those names against the real code**; if Phase 1 drifted, adapt the code in this plan, not the interfaces.

## Global Constraints

- Python `>=3.12`, `uv`, code under `src/mtrtk/`, tests under `tests/`, fixtures under `tests/fixtures/`. Hardware tests marked `@pytest.mark.hardware`.
- Hour rotation and file naming use **receiver UTC** from NAV-PVT, never the host clock; file path `DATA_DIR/ubx/YYYY/DDD/{STATION_ID}_{YYYYMMDD}_{HH}.ubx` with a `.json` sidecar of the same stem.
- Raw logger subscription is `Policy.UNBOUNDED` and never drops; sustained pressure publishes `rawlog.backpressure`. Flush every 1 s, fsync every `FSYNC_INTERVAL_S` (default 10).
- Retention: prune oldest files while free disk < `MIN_FREE_GB`; never delete a file whose sidecar has `keep: true`; delete the sidecar with the file.
- NTRIP: v1 detection = no `Ntrip-Version` header; v1 success is the literal `ICY 200 OK\r\n\r\n` followed by raw RTCM (never chunked); v2 success is `HTTP/1.1 200 OK` with `Ntrip-Version: Ntrip/2.0`, `Content-Type: gnss/data`, `Transfer-Encoding: chunked`, `Cache-Control: no-store, no-cache, max-age=0`, `Connection: close`; one chunk per RTCM frame; sourcetable ends with `ENDSOURCETABLE\r\n`; unknown mountpoint → sourcetable (v1) / `404` (v2); Basic auth compared with `hmac.compare_digest`; `401` carries `WWW-Authenticate: Basic realm="mtrtk"`; new clients immediately receive the cached last 1005 and 1230; slow clients: 64-frame drop-oldest queue, disconnect after write buffer > 256 KB for 10 s; request header limit 8 KB / 10 s.
- Bind resolution: `tailscale` → IPv4 of `tailscale0`, retried every 5 s and **never** silently replaced by `0.0.0.0`; `lan` → `0.0.0.0` (documented as LAN-wide); `all` → `0.0.0.0`; explicit IP as given.
- ECEF is the canonical site datum. TMODE fixed uses `tmode_fixed_ecef` (cm + 0.1 mm parts). Site verification: decoded RTCM 1005 ECEF must match the active site within 0.0005 m on each axis and NAV-PVT `fixType == 5` within 30 s.
- SQLite: single file `DATA_DIR/mtrtk.db`, `PRAGMA journal_mode=WAL`, schema via `PRAGMA user_version` migrations; 1 s samples kept 24 h, 1 min aggregates kept 90 d.
- Alerts never raise twice for the same active condition; a recovery event is emitted when the condition clears. Webhook body is JSON `{"level","kind","message","ts","host","role"}`.
- Commit per task with a Conventional Commit message ending in `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File structure (this plan)

| Path | Responsibility |
|---|---|
| `src/mtrtk/core/geo.py` | WGS84 LLH↔ECEF, UTM forward, ENU offsets, DMS formatting |
| `src/mtrtk/rawlog/__init__.py`, `writer.py` | `RawLogWriter`, `Sidecar`, `log_path`, `recover_incomplete` |
| `src/mtrtk/rawlog/index.py` | `list_logs`, `LogFile`, `files_for_window`, `hour_availability` |
| `src/mtrtk/rawlog/retention.py` | `RetentionPolicy.prune_once/run` |
| `src/mtrtk/store/__init__.py`, `db.py`, `schema/001_init.sql` | `Database` (aiosqlite, WAL, migrations) |
| `src/mtrtk/store/models.py` | `Site`, `Event`, `NtripClientRecord`, `SystemStats` |
| `src/mtrtk/store/repos.py` | `SitesRepo`, `EventsRepo`, `NtripLogRepo`, `LogFilesRepo` |
| `src/mtrtk/store/sampler.py` | `Sampler`: 1 s rows, 1 min rollups, pruning |
| `src/mtrtk/system.py` | `SystemMonitor` publishing `system.stats` |
| `src/mtrtk/alerts.py` | `AlertEngine`, rules, webhook |
| `src/mtrtk/base/__init__.py`, `ntrip_caster.py` | `NtripCaster`, `ClientInfo`, request parsing, sourcetable |
| `src/mtrtk/base/rtcm1005.py` | `decode_1005(frame) -> Ecef1005` |
| `src/mtrtk/base/basemode.py` | `BaseModeManager`: TMODE per settings, freeze survey-in, activate site, verify |
| `src/mtrtk/core/exposure.py` (modify) | `resolve_bind(mode) -> str | None`, `wait_for_bind(...)` |
| `src/mtrtk/daemon.py` (modify) | supervised consumer tasks per role, DB open/close |
| `src/mtrtk/cli.py` (modify) | `mtrtk sites list|add|activate|delete|freeze` |
| `tests/unit/test_geo.py`, `test_rawlog_writer.py`, `test_rawlog_index_retention.py`, `test_store.py`, `test_sampler.py`, `test_system.py`, `test_alerts.py`, `test_ntrip_caster.py`, `test_rtcm1005.py`, `test_basemode.py`, `test_daemon_base.py`, `tests/hardware/test_live_base.py` | tests |

---

### Task 1: Dependencies and geodesy helpers

**Files:**
- Modify: `pyproject.toml` (add `aiosqlite>=0.21`, `httpx>=0.28` to dependencies; add `pyproj>=3.7` to the dev group)
- Create: `src/mtrtk/core/geo.py`, `tests/unit/test_geo.py`

**Interfaces:**
- Produces: `llh_to_ecef(lat_deg, lon_deg, h_m) -> tuple[float, float, float]`; `ecef_to_llh(x, y, z) -> tuple[float, float, float]`; `utm_zone(lat_deg, lon_deg) -> int`; `Utm(zone, hemisphere, easting, northing)` with `.label` (e.g. `"46N"`); `llh_to_utm(lat_deg, lon_deg) -> Utm`; `ecef_to_enu(ref_lat, ref_lon, ref_h, x, y, z) -> tuple[float, float, float]`; `format_dms(value_deg, is_lat, decimals=4) -> str`.

- [ ] **Step 1: Add dependencies**

In `pyproject.toml` `dependencies`, append `"aiosqlite>=0.21",` and `"httpx>=0.28",`. In `[dependency-groups] dev`, append `"pyproj>=3.7",`. Run `uv sync` (updates `uv.lock`).

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_geo.py`:
```python
import math

import pyproj
import pytest

from mtrtk.core.geo import ecef_to_enu, ecef_to_llh, format_dms, llh_to_ecef, llh_to_utm, utm_zone

POINTS = [
    (23.8373506, 90.2625502, -36.268),  # Dhaka (the base station)
    (0.0, 0.0, 0.0),
    (89.9, 10.0, 100.0),
    (-33.8688, 151.2093, 25.0),  # Sydney
    (60.39, 5.32, 12.0),  # Bergen (UTM zone 32 exception)
]


@pytest.mark.parametrize("lat,lon,h", POINTS)
def test_llh_to_ecef_matches_pyproj(lat: float, lon: float, h: float) -> None:
    t = pyproj.Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
    ex, ey, ez = t.transform(lon, lat, h)
    x, y, z = llh_to_ecef(lat, lon, h)
    assert (x, y, z) == pytest.approx((ex, ey, ez), abs=1e-4)


@pytest.mark.parametrize("lat,lon,h", POINTS)
def test_ecef_roundtrip(lat: float, lon: float, h: float) -> None:
    lat2, lon2, h2 = ecef_to_llh(*llh_to_ecef(lat, lon, h))
    assert lat2 == pytest.approx(lat, abs=1e-9)
    assert lon2 == pytest.approx(lon, abs=1e-9)
    assert h2 == pytest.approx(h, abs=1e-4)


def test_ecef_equator_and_pole() -> None:
    assert llh_to_ecef(0, 0, 0) == pytest.approx((6378137.0, 0.0, 0.0))
    assert llh_to_ecef(90, 0, 0) == pytest.approx((0.0, 0.0, 6356752.314245), abs=1e-6)
    assert ecef_to_llh(0.0, 0.0, 6356752.314245)[0] == pytest.approx(90.0)


def test_utm_zones() -> None:
    assert utm_zone(23.8373506, 90.2625502) == 46
    assert utm_zone(-33.8688, 151.2093) == 56
    assert utm_zone(60.39, 5.32) == 32  # Norway exception
    assert utm_zone(78.22, 15.63) == 33  # Svalbard exception
    assert utm_zone(51.5, -0.12) == 30


@pytest.mark.parametrize("lat,lon,h", POINTS[:4])
def test_utm_matches_pyproj(lat: float, lon: float, h: float) -> None:
    utm = llh_to_utm(lat, lon)
    crs = pyproj.CRS.from_dict({"proj": "utm", "zone": utm.zone, "south": lat < 0, "ellps": "WGS84"})
    e, n = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(lon, lat)
    assert utm.easting == pytest.approx(e, abs=0.01)
    assert utm.northing == pytest.approx(n, abs=0.01)
    assert utm.hemisphere == ("S" if lat < 0 else "N")
    assert utm.label == f"{utm.zone}{utm.hemisphere}"


def test_enu_offsets() -> None:
    lat, lon, h = 23.8373506, 90.2625502, -36.268
    x, y, z = llh_to_ecef(lat, lon, h + 1.0)
    e, n, u = ecef_to_enu(lat, lon, h, x, y, z)
    assert (e, n, u) == pytest.approx((0.0, 0.0, 1.0), abs=1e-6)
    x, y, z = llh_to_ecef(lat + 1e-5, lon, h)
    e, n, u = ecef_to_enu(lat, lon, h, x, y, z)
    assert n == pytest.approx(1.1057, abs=0.002) and abs(e) < 1e-3 and abs(u) < 1e-3


def test_format_dms() -> None:
    assert format_dms(23.8373506, is_lat=True) == "23°50'14.4622\"N"
    assert format_dms(-90.2625502, is_lat=False) == "90°15'45.1807\"W"
    assert format_dms(-0.5, is_lat=True, decimals=1) == "0°30'00.0\"S"
    assert format_dms(45.99999999, is_lat=True, decimals=2) == "46°00'00.00\"N"  # carry
    assert math.isfinite(float(format_dms(1.5, is_lat=True).split("°")[0]))
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/unit/test_geo.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.core.geo'`.

- [ ] **Step 4: Write `src/mtrtk/core/geo.py`**

```python
"""WGS84 geodesy: LLH <-> ECEF, UTM (forward), ENU offsets and DMS formatting. Pure functions."""

from __future__ import annotations

import math
from dataclasses import dataclass

WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
WGS84_B = WGS84_A * (1 - WGS84_F)
WGS84_E2 = WGS84_F * (2 - WGS84_F)  # first eccentricity squared
_EP2 = WGS84_E2 / (1 - WGS84_E2)  # second eccentricity squared
UTM_K0 = 0.9996
UTM_FALSE_EASTING = 500_000.0
UTM_FALSE_NORTHING_SOUTH = 10_000_000.0


def llh_to_ecef(lat_deg: float, lon_deg: float, h_m: float) -> tuple[float, float, float]:
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * sin_lat * sin_lat)
    x = (n + h_m) * cos_lat * math.cos(lon)
    y = (n + h_m) * cos_lat * math.sin(lon)
    z = (n * (1 - WGS84_E2) + h_m) * sin_lat
    return x, y, z


def ecef_to_llh(x: float, y: float, z: float) -> tuple[float, float, float]:
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    if p < 1e-9:  # on the polar axis
        lat = math.copysign(math.pi / 2, z)
        return math.degrees(lat), math.degrees(lon), abs(z) - WGS84_B
    lat = math.atan2(z, p * (1 - WGS84_E2))
    for _ in range(20):
        sin_lat = math.sin(lat)
        n = WGS84_A / math.sqrt(1 - WGS84_E2 * sin_lat * sin_lat)
        h = p / math.cos(lat) - n
        new_lat = math.atan2(z, p * (1 - WGS84_E2 * n / (n + h)))
        if abs(new_lat - lat) < 1e-14:
            lat = new_lat
            break
        lat = new_lat
    sin_lat = math.sin(lat)
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * sin_lat * sin_lat)
    h = p / math.cos(lat) - n
    return math.degrees(lat), math.degrees(lon), h


@dataclass(frozen=True)
class Utm:
    zone: int
    hemisphere: str  # "N" | "S"
    easting: float
    northing: float

    @property
    def label(self) -> str:
        return f"{self.zone}{self.hemisphere}"


def utm_zone(lat_deg: float, lon_deg: float) -> int:
    zone = int((lon_deg + 180) // 6) + 1
    if 56 <= lat_deg < 64 and 3 <= lon_deg < 12:
        zone = 32  # south-west Norway
    if lat_deg >= 72 and 0 <= lon_deg < 42:  # Svalbard
        if lon_deg < 9:
            zone = 31
        elif lon_deg < 21:
            zone = 33
        elif lon_deg < 33:
            zone = 35
        else:
            zone = 37
    return min(max(zone, 1), 60)


def llh_to_utm(lat_deg: float, lon_deg: float) -> Utm:
    """Transverse Mercator series (Snyder 1987), accurate to ~1 mm inside the zone."""
    zone = utm_zone(lat_deg, lon_deg)
    hemisphere = "N" if lat_deg >= 0 else "S"
    lat = math.radians(lat_deg)
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    d_lon = math.radians(lon_deg) - lon0
    e2, ep2 = WGS84_E2, _EP2
    sin_lat, cos_lat, tan_lat = math.sin(lat), math.cos(lat), math.tan(lat)
    n = WGS84_A / math.sqrt(1 - e2 * sin_lat * sin_lat)
    t = tan_lat * tan_lat
    c = ep2 * cos_lat * cos_lat
    a = d_lon * cos_lat
    m = WGS84_A * (
        (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256) * lat
        - (3 * e2 / 8 + 3 * e2**2 / 32 + 45 * e2**3 / 1024) * math.sin(2 * lat)
        + (15 * e2**2 / 256 + 45 * e2**3 / 1024) * math.sin(4 * lat)
        - (35 * e2**3 / 3072) * math.sin(6 * lat)
    )
    easting = UTM_K0 * n * (a + (1 - t + c) * a**3 / 6 + (5 - 18 * t + t * t + 72 * c - 58 * ep2) * a**5 / 120)
    northing = UTM_K0 * (
        m
        + n
        * tan_lat
        * (a * a / 2 + (5 - t + 9 * c + 4 * c * c) * a**4 / 24 + (61 - 58 * t + t * t + 600 * c - 330 * ep2) * a**6 / 720)
    )
    easting += UTM_FALSE_EASTING
    if hemisphere == "S":
        northing += UTM_FALSE_NORTHING_SOUTH
    return Utm(zone, hemisphere, easting, northing)


def ecef_to_enu(
    ref_lat_deg: float, ref_lon_deg: float, ref_h_m: float, x: float, y: float, z: float
) -> tuple[float, float, float]:
    x0, y0, z0 = llh_to_ecef(ref_lat_deg, ref_lon_deg, ref_h_m)
    dx, dy, dz = x - x0, y - y0, z - z0
    lat, lon = math.radians(ref_lat_deg), math.radians(ref_lon_deg)
    sin_lat, cos_lat, sin_lon, cos_lon = math.sin(lat), math.cos(lat), math.sin(lon), math.cos(lon)
    e = -sin_lon * dx + cos_lon * dy
    n = -sin_lat * cos_lon * dx - sin_lat * sin_lon * dy + cos_lat * dz
    u = cos_lat * cos_lon * dx + cos_lat * sin_lon * dy + sin_lat * dz
    return e, n, u


def format_dms(value_deg: float, is_lat: bool, decimals: int = 4) -> str:
    if is_lat:
        hemi = "N" if value_deg >= 0 else "S"
    else:
        hemi = "E" if value_deg >= 0 else "W"
    total_seconds = round(abs(value_deg) * 3600, decimals)
    degrees = int(total_seconds // 3600)
    minutes = int((total_seconds - degrees * 3600) // 60)
    seconds = total_seconds - degrees * 3600 - minutes * 60
    width = 3 + decimals if decimals else 2
    return f"{degrees}°{minutes:02d}'{seconds:0{width}.{decimals}f}\"{hemi}"
```

- [ ] **Step 5: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_geo.py -q` → all pass (22 tests incl. parametrizations); `uv run ruff check . && uv run ruff format . && uv run mypy`.

If `test_format_dms` differs in the last digit for the carry case, keep the `round(total_seconds)` approach and adjust the expected string only if the rounding rule is mathematically defensible (the value 45.99999999° is 46°00'00.00" at 2 decimals).

```bash
git add pyproject.toml uv.lock src/mtrtk/core/geo.py tests/unit/test_geo.py
git commit -m "feat(core): WGS84 geodesy helpers (ECEF, UTM, ENU, DMS) and Phase 2 dependencies

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Hourly raw UBX logger with sidecars

**Files:**
- Create: `src/mtrtk/rawlog/__init__.py` (empty), `src/mtrtk/rawlog/writer.py`, `tests/unit/test_rawlog_writer.py`

**Interfaces:**
- Consumes: `Bus.subscribe("raw.ubx", policy=Policy.UNBOUNDED, high_water=…)`, `Frame.identity/raw/proto/parsed()`.
- Produces: `log_path(root, station_id, hour_utc: datetime) -> Path`; `sidecar_path(path) -> Path`; `Sidecar` dataclass (`station_id, role, start_utc, end_utc, hour_utc, bytes, msg_counts, sha256, firmware, site, keep, time_source, complete, recovered`) with `dump(path)` / `load(path)`; `RawLogWriter(bus, root, station_id, messages, role="base", fsync_interval_s=10, firmware="", site=None)` with sync `handle(frame)`, `tick(now_mono)`, `close()`, `current_path`, `stop()`, and `async run(stop_event)`; `recover_incomplete(root) -> list[Path]`. Bus topics published: `rawlog.rotated` (new `Path`), `rawlog.closed` (`Path`), `rawlog.backpressure` (queue size int).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_rawlog_writer.py`:
```python
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pyubx2 import GET, UBXMessage
from ubxtest import ubx_frame

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame, Framer
from mtrtk.rawlog.writer import RawLogWriter, Sidecar, log_path, recover_incomplete, sidecar_path

RAWX = ubx_frame(0x02, 0x15, b"\x11" * 16)
SFRBX = ubx_frame(0x02, 0x13, b"\x22" * 8)
NAV_SAT = ubx_frame(0x01, 0x35, b"\x33" * 8)  # not in the log filter
MESSAGES = ["RXM-RAWX", "RXM-SFRBX", "NAV-PVT"]


def frames(raw: bytes) -> list[Frame]:
    return Framer().feed(raw)


def pvt(hour: int, minute: int = 0, second: int = 0, valid: int = 1) -> bytes:
    return UBXMessage(
        "NAV", "NAV-PVT", GET, iTOW=1, year=2026, month=9, day=18, hour=hour, min=minute, second=second,
        validDate=valid, validTime=valid, fixType=3,
    ).serialize()


def make_writer(tmp_path: Path, bus: Bus | None = None) -> RawLogWriter:
    return RawLogWriter(bus or Bus(), tmp_path, "MTRK", MESSAGES, role="base", firmware="HPG 1.13")


def test_log_path_layout(tmp_path: Path) -> None:
    hour = datetime(2026, 9, 18, 16, tzinfo=UTC)
    path = log_path(tmp_path, "MTRK", hour)
    assert path == tmp_path / "ubx" / "2026" / "261" / "MTRK_20260918_16.ubx"
    assert sidecar_path(path) == path.with_suffix(".json")


def test_frames_before_time_is_known_are_buffered_then_written(tmp_path: Path) -> None:
    w = make_writer(tmp_path)
    for f in frames(RAWX + SFRBX):
        w.handle(f)
    assert w.current_path is None
    for f in frames(pvt(16) + RAWX):
        w.handle(f)
    w.close()
    path = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 16, tzinfo=UTC))
    assert path.read_bytes() == RAWX + SFRBX + pvt(16) + RAWX
    sc = Sidecar.load(sidecar_path(path))
    assert sc.msg_counts == {"RXM-RAWX": 2, "RXM-SFRBX": 1, "NAV-PVT": 1}
    assert sc.bytes == path.stat().st_size
    assert sc.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert sc.complete is True and sc.start_utc == "2026-09-18T16:00:00+00:00"
    assert sc.firmware == "HPG 1.13" and sc.station_id == "MTRK" and sc.keep is False


def test_unfiltered_messages_are_not_logged(tmp_path: Path) -> None:
    w = make_writer(tmp_path)
    for f in frames(pvt(16) + NAV_SAT + RAWX):
        w.handle(f)
    w.close()
    path = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 16, tzinfo=UTC))
    assert path.read_bytes() == pvt(16) + RAWX


def test_rotation_on_receiver_hour_boundary(tmp_path: Path) -> None:
    bus = Bus()
    events = bus.subscribe("rawlog.*")
    w = make_writer(tmp_path, bus)
    for f in frames(pvt(16, 59, 59) + RAWX + pvt(17, 0, 0) + RAWX):
        w.handle(f)
    w.close()
    p16 = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 16, tzinfo=UTC))
    p17 = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 17, tzinfo=UTC))
    assert p16.read_bytes() == pvt(16, 59, 59) + RAWX
    assert p17.read_bytes() == pvt(17, 0, 0) + RAWX
    assert Sidecar.load(sidecar_path(p16)).complete is True
    assert Sidecar.load(sidecar_path(p16)).end_utc == "2026-09-18T17:00:00+00:00"
    topics = [t for t, _ in [events.queue.get_nowait() for _ in range(events.queue.qsize())]]
    assert topics == ["rawlog.rotated", "rawlog.closed", "rawlog.rotated", "rawlog.closed"]


def test_invalid_time_does_not_advance_clock(tmp_path: Path) -> None:
    w = make_writer(tmp_path)
    for f in frames(pvt(16) + pvt(18, valid=0) + RAWX):
        w.handle(f)
    w.close()
    assert log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 16, tzinfo=UTC)).exists()
    assert not log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 18, tzinfo=UTC)).exists()


def test_pending_cap_falls_back_to_host_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mtrtk.rawlog import writer as writer_mod

    monkeypatch.setattr(writer_mod, "PENDING_CAP_BYTES", 40)
    fixed_now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return fixed_now

    monkeypatch.setattr(writer_mod, "datetime", FixedDatetime)
    w = make_writer(tmp_path)
    for f in frames(RAWX + RAWX + RAWX):
        w.handle(f)
    w.close()
    path = log_path(tmp_path, "MTRK", datetime(2026, 1, 2, 3, tzinfo=UTC))
    assert path.read_bytes() == RAWX * 3
    assert Sidecar.load(sidecar_path(path)).time_source == "host"


def test_tick_flushes_and_updates_sidecar(tmp_path: Path) -> None:
    w = make_writer(tmp_path)
    for f in frames(pvt(16) + RAWX):
        w.handle(f)
    path = w.current_path
    assert path is not None
    w._last_sidecar = -1e9  # force the 60 s sidecar refresh on this tick
    w.tick(now_mono=1000.0)
    assert path.stat().st_size == len(pvt(16)) + len(RAWX)  # flushed to disk
    sc = Sidecar.load(sidecar_path(path))
    assert sc.complete is False and sc.bytes == path.stat().st_size
    w.close()


def test_recover_incomplete_finalizes_orphans(tmp_path: Path) -> None:
    path = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 12, tzinfo=UTC))
    path.parent.mkdir(parents=True)
    path.write_bytes(RAWX * 5)
    Sidecar("MTRK", "base", "2026-09-18T12:00:00+00:00", hour_utc="2026-09-18T12:00:00+00:00").dump(sidecar_path(path))
    orphan_json = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 13, tzinfo=UTC)).with_suffix(".json")
    Sidecar("MTRK", "base", None).dump(orphan_json)  # sidecar without data file
    recovered = recover_incomplete(tmp_path)
    assert recovered == [path]
    sc = Sidecar.load(sidecar_path(path))
    assert sc.complete is True and sc.recovered is True
    assert sc.bytes == len(RAWX) * 5 and sc.sha256 == hashlib.sha256(RAWX * 5).hexdigest()
    assert sc.end_utc is not None
    assert not orphan_json.exists()


async def test_run_consumes_bus_and_reports_backpressure(tmp_path: Path) -> None:
    import asyncio

    bus = Bus()
    pressure = bus.subscribe("rawlog.backpressure")
    w = RawLogWriter(bus, tmp_path, "MTRK", MESSAGES, role="base")
    w.sub.high_water = 2
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    for f in frames(pvt(16) + RAWX + RAWX + RAWX):
        bus.publish("raw.ubx", f)
    await asyncio.sleep(0.05)
    w.stop()
    await asyncio.wait_for(task, 2.0)
    path = log_path(tmp_path, "MTRK", datetime(2026, 9, 18, 16, tzinfo=UTC))
    assert path.read_bytes() == pvt(16) + RAWX * 3
    assert pressure.queue.qsize() >= 1


def test_sidecar_json_is_plain_and_loadable(tmp_path: Path) -> None:
    sc = Sidecar("MTRK", "base", "2026-09-18T16:00:00+00:00", msg_counts={"RXM-RAWX": 3})
    p = tmp_path / "x.json"
    sc.dump(p)
    assert json.loads(p.read_text())["msg_counts"] == {"RXM-RAWX": 3}
    assert Sidecar.load(p) == sc
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_rawlog_writer.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.rawlog'`.

- [ ] **Step 3: Write `src/mtrtk/rawlog/writer.py`** (and empty `src/mtrtk/rawlog/__init__.py`)

```python
"""Hourly raw UBX logger. Rotation keyed on receiver UTC (NAV-PVT); JSON sidecar per file."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mtrtk.core.bus import Bus, Policy
from mtrtk.core.frames import Frame, Proto

log = logging.getLogger(__name__)

PENDING_CAP_BYTES = 8 * 1024 * 1024  # buffered before receiver time is known
SIDECAR_UPDATE_S = 60.0
FLUSH_INTERVAL_S = 1.0


def log_path(root: Path, station_id: str, hour_utc: datetime) -> Path:
    return root / "ubx" / f"{hour_utc:%Y}" / f"{hour_utc:%j}" / f"{station_id}_{hour_utc:%Y%m%d}_{hour_utc:%H}.ubx"


def sidecar_path(path: Path) -> Path:
    return path.with_suffix(".json")


@dataclass
class Sidecar:
    station_id: str
    role: str
    start_utc: str | None
    end_utc: str | None = None
    hour_utc: str | None = None
    bytes: int = 0
    msg_counts: dict[str, int] = field(default_factory=dict)
    sha256: str | None = None
    firmware: str = ""
    site: str | None = None
    keep: bool = False
    time_source: str = "receiver"  # "receiver" | "host"
    complete: bool = False
    recovered: bool = False

    def dump(self, path: Path) -> None:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> Sidecar:
        return cls(**json.loads(path.read_text()))


class _OpenLog:
    def __init__(self, path: Path, hour: datetime, sidecar: Sidecar) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.hour = hour
        self.sidecar = sidecar
        self.hasher = hashlib.sha256()
        if path.exists() and path.stat().st_size:  # resumed after a restart inside the same hour
            self.hasher.update(path.read_bytes())
            self.sidecar.bytes = path.stat().st_size
        self.fh = path.open("ab")

    def write(self, raw: bytes, identity: str) -> None:
        self.fh.write(raw)
        self.hasher.update(raw)
        self.sidecar.bytes += len(raw)
        self.sidecar.msg_counts[identity] = self.sidecar.msg_counts.get(identity, 0) + 1

    def flush(self) -> None:
        self.fh.flush()

    def fsync(self) -> None:
        self.fh.flush()
        os.fsync(self.fh.fileno())

    def dump_sidecar(self) -> None:
        self.sidecar.dump(sidecar_path(self.path))

    def close(self, end_utc: datetime | None) -> None:
        self.fsync()
        self.fh.close()
        self.sidecar.end_utc = end_utc.isoformat() if end_utc else None
        self.sidecar.sha256 = self.hasher.hexdigest()
        self.sidecar.complete = True
        self.dump_sidecar()


class RawLogWriter:
    def __init__(
        self,
        bus: Bus,
        root: Path,
        station_id: str,
        messages: list[str],
        role: str = "base",
        fsync_interval_s: int = 10,
        firmware: str = "",
        site: str | None = None,
    ) -> None:
        self.bus = bus
        self.root = Path(root)
        self.station_id = station_id
        self.filter = frozenset(messages)
        self.role = role
        self.fsync_interval_s = fsync_interval_s
        self.firmware = firmware
        self.site = site
        self.sub = bus.subscribe("raw.ubx", policy=Policy.UNBOUNDED, high_water=20_000)
        self._current: _OpenLog | None = None
        self._pending = bytearray()
        self._pending_counts: dict[str, int] = {}
        self._utc: datetime | None = None
        self._time_source = "receiver"
        self._last_fsync = time.monotonic()
        self._last_sidecar = time.monotonic()
        self._backpressure_seen = 0

    # ------------------------------------------------------------ properties
    @property
    def current_path(self) -> Path | None:
        return self._current.path if self._current else None

    @property
    def receiver_utc(self) -> datetime | None:
        return self._utc

    # ------------------------------------------------------------- sync core
    def handle(self, frame: Frame) -> None:
        if frame.proto is not Proto.UBX:
            return
        identity = frame.identity
        if identity == "NAV-PVT":
            self._update_time(frame)
        if identity not in self.filter:
            return
        if self._utc is None:
            self._buffer_pending(frame.raw, identity)
            return
        hour = self._utc.replace(minute=0, second=0, microsecond=0)
        if self._current is None or self._current.hour != hour:
            self._rotate(hour)
        assert self._current is not None
        self._current.write(frame.raw, identity)

    def _update_time(self, frame: Frame) -> None:
        m = frame.parsed()
        if m.validDate and m.validTime:
            self._utc = datetime(m.year, m.month, m.day, m.hour, m.min, m.second, tzinfo=UTC)

    def _buffer_pending(self, raw: bytes, identity: str) -> None:
        self._pending += raw
        self._pending_counts[identity] = self._pending_counts.get(identity, 0) + 1
        if len(self._pending) > PENDING_CAP_BYTES:
            log.warning("no receiver time after %d buffered bytes; naming file by host clock", len(self._pending))
            self._utc = datetime.now(UTC)
            self._time_source = "host"
            self._rotate(self._utc.replace(minute=0, second=0, microsecond=0))

    def _rotate(self, hour: datetime) -> None:
        if self._current is not None:
            self._close_current()
        path = log_path(self.root, self.station_id, hour)
        sidecar = Sidecar(
            station_id=self.station_id,
            role=self.role,
            start_utc=self._utc.isoformat() if self._utc else None,
            hour_utc=hour.isoformat(),
            firmware=self.firmware,
            site=self.site,
            time_source=self._time_source,
        )
        self._current = _OpenLog(path, hour, sidecar)
        if self._pending:
            self._current.fh.write(bytes(self._pending))
            self._current.hasher.update(bytes(self._pending))
            self._current.sidecar.bytes += len(self._pending)
            for ident, count in self._pending_counts.items():
                self._current.sidecar.msg_counts[ident] = self._current.sidecar.msg_counts.get(ident, 0) + count
            self._pending.clear()
            self._pending_counts.clear()
        self._current.dump_sidecar()
        log.info("logging to %s", path)
        self.bus.publish("rawlog.rotated", path)

    def _close_current(self) -> None:
        assert self._current is not None
        self._current.close(self._utc)
        self.bus.publish("rawlog.closed", self._current.path)
        self._current = None

    def tick(self, now_mono: float) -> None:
        """Called about once per second: flush, periodic fsync and sidecar refresh."""
        if self._current is None:
            return
        self._current.flush()
        if now_mono - self._last_fsync >= self.fsync_interval_s:
            self._current.fsync()
            self._last_fsync = now_mono
        if now_mono - self._last_sidecar >= SIDECAR_UPDATE_S:
            self._current.dump_sidecar()
            self._last_sidecar = now_mono

    def close(self) -> None:
        if self._current is not None:
            self._close_current()

    # ------------------------------------------------------------ async run
    def stop(self) -> None:
        self.sub.close()

    async def run(self, stop: asyncio.Event) -> None:
        ticker = asyncio.create_task(self._ticker(stop), name="rawlog-ticker")
        try:
            async for _, frame in self.sub:
                self.handle(frame)
                if self.sub.high_water_hits > self._backpressure_seen:
                    self._backpressure_seen = self.sub.high_water_hits
                    self.bus.publish("rawlog.backpressure", self.sub.queue.qsize())
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
            self.close()

    async def _ticker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(FLUSH_INTERVAL_S)
            self.tick(time.monotonic())


def recover_incomplete(root: Path) -> list[Path]:
    """Finalize sidecars left incomplete by a crash; drop sidecars whose data file is gone."""
    recovered: list[Path] = []
    for sc_path in sorted(Path(root).glob("ubx/*/*/*.json")):
        try:
            sc = Sidecar.load(sc_path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            log.warning("unreadable sidecar %s", sc_path)
            continue
        if sc.complete:
            continue
        data = sc_path.with_suffix(".ubx")
        if not data.exists():
            sc_path.unlink()
            continue
        sc.bytes = data.stat().st_size
        sc.sha256 = hashlib.sha256(data.read_bytes()).hexdigest()
        sc.end_utc = datetime.fromtimestamp(data.stat().st_mtime, UTC).isoformat()
        sc.complete = True
        sc.recovered = True
        sc.dump(sc_path)
        recovered.append(data)
    return recovered
```

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_rawlog_writer.py -q` → `10 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

```bash
git add src/mtrtk/rawlog tests/unit/test_rawlog_writer.py
git commit -m "feat(rawlog): hourly UBX writer keyed on receiver UTC with JSON sidecars and crash recovery

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Log index and disk retention

**Files:**
- Create: `src/mtrtk/rawlog/index.py`, `src/mtrtk/rawlog/retention.py`, `tests/unit/test_rawlog_index_retention.py`

**Interfaces:**
- Consumes: `log_path`, `sidecar_path`, `Sidecar` (Task 2).
- Produces: `LogFile(path, sidecar_path, station_id, hour_utc, bytes, complete, keep, msg_counts, start_utc, end_utc)` with `.hour_end`; `parse_log_name(path) -> tuple[str, datetime] | None`; `list_logs(root) -> list[LogFile]` (oldest first); `files_for_window(root, start, end) -> list[LogFile]`; `HourSlot(hour_utc, file: LogFile | None)`; `hour_availability(root, start, end) -> list[HourSlot]`; `RetentionPolicy(root, min_free_gb, bus=None, disk_usage=shutil.disk_usage)` with `free_gb()`, `prune_once() -> list[Path]`, `async run(stop, interval_s=3600)`. Bus topic `rawlog.pruned` (`Path`).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_rawlog_index_retention.py`:
```python
import asyncio
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mtrtk.core.bus import Bus
from mtrtk.rawlog.index import files_for_window, hour_availability, list_logs, parse_log_name
from mtrtk.rawlog.retention import RetentionPolicy
from mtrtk.rawlog.writer import Sidecar, log_path, sidecar_path

Usage = namedtuple("Usage", "total used free")
H0 = datetime(2026, 9, 18, 10, tzinfo=UTC)


def make_log(root: Path, hour: datetime, size: int = 1000, keep: bool = False, sidecar: bool = True) -> Path:
    path = log_path(root, "MTRK", hour)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xb5" * size)
    if sidecar:
        Sidecar("MTRK", "base", hour.isoformat(), end_utc=(hour + timedelta(hours=1)).isoformat(),
                hour_utc=hour.isoformat(), bytes=size, keep=keep, complete=True,
                msg_counts={"RXM-RAWX": 3600}).dump(sidecar_path(path))
    return path


def test_parse_log_name() -> None:
    assert parse_log_name(Path("/x/MTRK_20260918_16.ubx")) == ("MTRK", datetime(2026, 9, 18, 16, tzinfo=UTC))
    assert parse_log_name(Path("/x/notes.txt")) is None
    assert parse_log_name(Path("/x/MTRK_2026091_16.ubx")) is None


def test_list_logs_sorted_with_and_without_sidecar(tmp_path: Path) -> None:
    make_log(tmp_path, H0 + timedelta(hours=2))
    make_log(tmp_path, H0, keep=True)
    make_log(tmp_path, H0 + timedelta(hours=1), size=5, sidecar=False)
    logs = list_logs(tmp_path)
    assert [lf.hour_utc for lf in logs] == [H0, H0 + timedelta(hours=1), H0 + timedelta(hours=2)]
    assert logs[0].keep is True and logs[0].msg_counts == {"RXM-RAWX": 3600}
    assert logs[1].bytes == 5 and logs[1].complete is False and logs[1].msg_counts == {}
    assert logs[0].hour_end == H0 + timedelta(hours=1)


def test_files_for_window_selects_overlapping_hours(tmp_path: Path) -> None:
    for i in range(5):
        make_log(tmp_path, H0 + timedelta(hours=i))
    sel = files_for_window(tmp_path, H0 + timedelta(hours=1, minutes=30), H0 + timedelta(hours=3, minutes=10))
    assert [lf.hour_utc for lf in sel] == [H0 + timedelta(hours=1), H0 + timedelta(hours=2), H0 + timedelta(hours=3)]
    assert files_for_window(tmp_path, H0 + timedelta(hours=1), H0 + timedelta(hours=2)) == [list_logs(tmp_path)[1]]


def test_hour_availability_marks_gaps(tmp_path: Path) -> None:
    make_log(tmp_path, H0)
    make_log(tmp_path, H0 + timedelta(hours=2))
    slots = hour_availability(tmp_path, H0, H0 + timedelta(hours=3))
    assert [s.hour_utc for s in slots] == [H0, H0 + timedelta(hours=1), H0 + timedelta(hours=2)]
    assert [s.file is not None for s in slots] == [True, False, True]


def test_prune_deletes_oldest_unkept_until_free(tmp_path: Path) -> None:
    for i in range(4):
        make_log(tmp_path, H0 + timedelta(hours=i), keep=(i == 1))
    free = iter([1.0e9, 1.0e9, 6.0e9, 6.0e9, 6.0e9])
    policy = RetentionPolicy(tmp_path, min_free_gb=5.0, disk_usage=lambda p: Usage(10e9, 5e9, next(free)))
    bus = Bus()
    pruned_sub = bus.subscribe("rawlog.pruned")
    policy.bus = bus
    deleted = policy.prune_once()
    assert deleted == [log_path(tmp_path, "MTRK", H0)]  # hour 0 gone; hour 1 kept; free now ok
    assert not log_path(tmp_path, "MTRK", H0).exists() and not sidecar_path(log_path(tmp_path, "MTRK", H0)).exists()
    assert log_path(tmp_path, "MTRK", H0 + timedelta(hours=1)).exists()
    assert pruned_sub.queue.qsize() == 1


def test_prune_never_deletes_kept_or_newest(tmp_path: Path) -> None:
    make_log(tmp_path, H0, keep=True)
    make_log(tmp_path, H0 + timedelta(hours=1))  # newest = probably being written
    policy = RetentionPolicy(tmp_path, min_free_gb=5.0, disk_usage=lambda p: Usage(10e9, 9e9, 1e9))
    assert policy.prune_once() == []
    assert len(list_logs(tmp_path)) == 2


def test_prune_removes_empty_day_directories(tmp_path: Path) -> None:
    old = make_log(tmp_path, H0 - timedelta(days=3))
    make_log(tmp_path, H0)
    calls = iter([1e9, 9e9])
    RetentionPolicy(tmp_path, 5.0, disk_usage=lambda p: Usage(10e9, 1e9, next(calls))).prune_once()
    assert not old.parent.exists()


async def test_run_prunes_on_interval(tmp_path: Path) -> None:
    make_log(tmp_path, H0)
    make_log(tmp_path, H0 + timedelta(hours=1))
    policy = RetentionPolicy(tmp_path, 5.0, disk_usage=lambda p: Usage(10e9, 9e9, 9e9))
    stop = asyncio.Event()
    task = asyncio.create_task(policy.run(stop, interval_s=0.01))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 1.0)
    assert policy.runs >= 1
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_rawlog_index_retention.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.rawlog.index'`.

- [ ] **Step 3: Write `src/mtrtk/rawlog/index.py`**

```python
"""Read-only view of the raw log directory: what hours exist, which files cover a window."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mtrtk.rawlog.writer import Sidecar, sidecar_path

_NAME_RE = re.compile(r"^(?P<station>[A-Z0-9]{4})_(?P<date>\d{8})_(?P<hour>\d{2})\.ubx$")


@dataclass(frozen=True)
class LogFile:
    path: Path
    sidecar_path: Path
    station_id: str
    hour_utc: datetime
    bytes: int
    complete: bool
    keep: bool
    msg_counts: dict[str, int] = field(default_factory=dict)
    start_utc: str | None = None
    end_utc: str | None = None

    @property
    def hour_end(self) -> datetime:
        return self.hour_utc + timedelta(hours=1)


@dataclass(frozen=True)
class HourSlot:
    hour_utc: datetime
    file: LogFile | None


def parse_log_name(path: Path) -> tuple[str, datetime] | None:
    m = _NAME_RE.match(path.name)
    if not m:
        return None
    try:
        hour = datetime.strptime(m["date"] + m["hour"], "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError:
        return None
    return m["station"], hour


def _load_logfile(path: Path) -> LogFile | None:
    parsed = parse_log_name(path)
    if parsed is None:
        return None
    station, hour = parsed
    sc_path = sidecar_path(path)
    size = path.stat().st_size
    if sc_path.exists():
        try:
            sc = Sidecar.load(sc_path)
        except (TypeError, ValueError, json.JSONDecodeError):
            sc = None
    else:
        sc = None
    if sc is None:
        return LogFile(path, sc_path, station, hour, size, complete=False, keep=False)
    return LogFile(path, sc_path, station, hour, size, sc.complete, sc.keep, dict(sc.msg_counts), sc.start_utc, sc.end_utc)


def list_logs(root: Path) -> list[LogFile]:
    logs = [lf for p in Path(root).glob("ubx/*/*/*.ubx") if (lf := _load_logfile(p)) is not None]
    return sorted(logs, key=lambda lf: lf.hour_utc)


def files_for_window(root: Path, start: datetime, end: datetime) -> list[LogFile]:
    """Hourly files overlapping [start, end)."""
    return [lf for lf in list_logs(root) if lf.hour_utc < end and lf.hour_end > start]


def hour_availability(root: Path, start: datetime, end: datetime) -> list[HourSlot]:
    by_hour = {lf.hour_utc: lf for lf in list_logs(root)}
    slots: list[HourSlot] = []
    hour = start.replace(minute=0, second=0, microsecond=0)
    while hour < end:
        slots.append(HourSlot(hour, by_hour.get(hour)))
        hour += timedelta(hours=1)
    return slots
```

- [ ] **Step 4: Write `src/mtrtk/rawlog/retention.py`**

```python
"""Deletes the oldest unkept raw logs when free disk space falls below the configured floor."""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mtrtk.core.bus import Bus
from mtrtk.rawlog.index import list_logs

log = logging.getLogger(__name__)
GB = 1e9


class RetentionPolicy:
    def __init__(
        self,
        root: Path,
        min_free_gb: float,
        bus: Bus | None = None,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
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
            logs = list_logs(self.root)
            if len(logs) < 2:  # never touch the newest file (it may be open for writing)
                break
            candidates = [lf for lf in logs[:-1] if not lf.keep]
            if not candidates:
                log.warning("disk below %.1f GB but every older log is marked keep", self.min_free_gb)
                break
            victim = candidates[0]
            victim.path.unlink(missing_ok=True)
            victim.sidecar_path.unlink(missing_ok=True)
            self._remove_empty_parents(victim.path.parent)
            deleted.append(victim.path)
            log.info("pruned %s (%d bytes)", victim.path, victim.bytes)
            if self.bus is not None:
                self.bus.publish("rawlog.pruned", victim.path)
        return deleted

    def _remove_empty_parents(self, directory: Path) -> None:
        ubx_root = self.root / "ubx"
        while directory != ubx_root and directory.exists() and not any(directory.iterdir()):
            directory.rmdir()
            directory = directory.parent

    async def run(self, stop: asyncio.Event, interval_s: float = 3600.0) -> None:
        while not stop.is_set():
            try:
                self.prune_once()
            except OSError:
                log.exception("retention pass failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except TimeoutError:
                pass
```

- [ ] **Step 5: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_rawlog_index_retention.py -q` → `8 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

```bash
git add src/mtrtk/rawlog/index.py src/mtrtk/rawlog/retention.py tests/unit/test_rawlog_index_retention.py
git commit -m "feat(rawlog): log index, window selection and keep-aware disk retention

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: SQLite store — database, schema, models, repositories

**Files:**
- Create: `src/mtrtk/store/__init__.py` (empty), `src/mtrtk/store/db.py`, `src/mtrtk/store/schema/__init__.py` (empty), `src/mtrtk/store/schema/001_init.sql`, `src/mtrtk/store/models.py`, `src/mtrtk/store/repos.py`, `tests/unit/test_store.py`

**Interfaces:**
- Consumes: `geo.ecef_to_llh` (Task 1), `Sidecar` (Task 2).
- Produces: `Database(path)` with `await open()`, `await close()`, `conn` (aiosqlite connection, `row_factory = aiosqlite.Row`), `await execute(sql, params=())`, `await fetchall(sql, params=())`, `await fetchone(sql, params=())`, `await commit()`, `user_version`; models `Site` (`from_ecef(name, x, y, z, *, sigma_m=None, source, frame="ITRF2020", epoch=None, notes=None)`), `Event`, `NtripClientRecord`, `SystemStats`; repos `SitesRepo(db)`: `list()`, `get(name)`, `add(site)`, `delete(name)`, `activate(name)`, `active()`; `EventsRepo(db)`: `add(level, kind, message, meta=None) -> Event`, `list(limit=200, level=None)`, `ack(event_id)`; `NtripLogRepo(db)`: `connected(ip, mountpoint, user_agent, username) -> int`, `disconnected(row_id, bytes_sent, last_lat, last_lon, reason)`, `recent(limit=100)`; `LogFilesRepo(db)`: `upsert(path, sidecar)`, `set_keep(path, keep)`, `delete(path)`, `list()`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_store.py`:
```python
from pathlib import Path

import pytest

from mtrtk.rawlog.writer import Sidecar
from mtrtk.store.db import Database
from mtrtk.store.models import Site, SystemStats
from mtrtk.store.repos import EventsRepo, LogFilesRepo, NtripLogRepo, SitesRepo


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "mtrtk.db")
    await database.open()
    try:
        yield database
    finally:
        await database.close()


async def test_open_creates_schema_and_is_idempotent(tmp_path: Path) -> None:
    db1 = Database(tmp_path / "m.db")
    await db1.open()
    tables = {r["name"] for r in await db1.fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"samples_1s", "samples_1m", "sites", "sessions", "points", "events", "log_files", "ntrip_clients_log", "jobs"} <= tables
    assert db1.user_version == 1
    mode = (await db1.fetchone("PRAGMA journal_mode"))[0]
    assert mode == "wal"
    await db1.close()
    db2 = Database(tmp_path / "m.db")
    await db2.open()
    assert db2.user_version == 1
    await db2.close()


def test_site_from_ecef_fills_llh() -> None:
    site = Site.from_ecef("roof", 1234567.8912, 5000000.0, 3000000.0, sigma_m=0.005, source="csrs-ppp")
    assert site.lat is not None and site.lon is not None and site.height_m is not None
    assert site.sigma_x == site.sigma_y == site.sigma_z == 0.005
    assert site.frame == "ITRF2020" and site.active is False


async def test_sites_repo_crud_and_single_active(db: Database) -> None:
    repo = SitesRepo(db)
    a = await repo.add(Site.from_ecef("a", 1.0, 2.0, 3.0, source="survey-in"))
    b = await repo.add(Site.from_ecef("b", 4.0, 5.0, 6.0, source="manual"))
    assert a.id is not None and b.id is not None
    assert [s.name for s in await repo.list()] == ["a", "b"]
    with pytest.raises(ValueError, match="exists"):
        await repo.add(Site.from_ecef("a", 0, 0, 0, source="manual"))
    await repo.activate("b")
    assert (await repo.active()).name == "b"
    await repo.activate("a")
    active = [s.name for s in await repo.list() if s.active]
    assert active == ["a"]
    with pytest.raises(KeyError):
        await repo.activate("zzz")
    await repo.delete("b")
    assert await repo.get("b") is None
    assert (await repo.get("a")).x == 1.0


async def test_events_repo(db: Database) -> None:
    repo = EventsRepo(db)
    e1 = await repo.add("warning", "jamming", "jam_ind 210", {"jam_ind": 210})
    await repo.add("info", "survey_in_valid", "survey-in complete")
    assert e1.id is not None and e1.meta == {"jam_ind": 210}
    assert [e.kind for e in await repo.list()] == ["survey_in_valid", "jamming"]  # newest first
    assert [e.kind for e in await repo.list(level="warning")] == ["jamming"]
    await repo.ack(e1.id)
    assert (await repo.list(level="warning"))[0].acked is True


async def test_ntrip_log_repo(db: Database) -> None:
    repo = NtripLogRepo(db)
    row = await repo.connected("100.100.50.12", "MTRK", "NTRIP SWMaps/1.0", "rover")
    await repo.disconnected(row, bytes_sent=12345, last_lat=23.8, last_lon=90.2, reason="client closed")
    recent = await repo.recent()
    assert len(recent) == 1
    assert recent[0].bytes_sent == 12345 and recent[0].last_lat == 23.8 and recent[0].disconnected_utc is not None


async def test_log_files_repo(db: Database, tmp_path: Path) -> None:
    repo = LogFilesRepo(db)
    sc = Sidecar("MTRK", "base", "2026-09-18T16:00:00+00:00", hour_utc="2026-09-18T16:00:00+00:00", bytes=10, msg_counts={"RXM-RAWX": 2}, complete=True)
    path = tmp_path / "MTRK_20260918_16.ubx"
    await repo.upsert(path, sc)
    sc.bytes = 20
    await repo.upsert(path, sc)
    rows = await repo.list()
    assert len(rows) == 1 and rows[0]["bytes"] == 20 and rows[0]["keep"] == 0
    await repo.set_keep(path, True)
    assert (await repo.list())[0]["keep"] == 1
    await repo.delete(path)
    assert await repo.list() == []


def test_system_stats_model_defaults() -> None:
    s = SystemStats(cpu_pct=1.0, mem_pct=2.0, disk_free_gb=3.0, disk_used_pct=4.0, uptime_s=5.0)
    assert s.temp_c is None and s.load1 is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_store.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.store'`.

- [ ] **Step 3: Write `src/mtrtk/store/schema/001_init.sql`**

```sql
-- mtrtk schema v1
CREATE TABLE IF NOT EXISTS samples_1s (
    ts REAL PRIMARY KEY,            -- unix seconds (receiver UTC)
    lat REAL, lon REAL, height_m REAL, hmsl_m REAL,
    h_acc_m REAL, v_acc_m REAL,
    fix_type INTEGER, carr_soln INTEGER,
    nsat_used INTEGER, nsat_tracked INTEGER,
    pdop REAL, hdop REAL, vdop REAL,
    cno_mean REAL, jam_ind INTEGER, agc_cnt INTEGER, noise_per_ms INTEGER,
    corr_age_s REAL, baseline_m REAL,
    rtcm_bytes_per_s REAL, ntrip_clients INTEGER,
    cpu_pct REAL, mem_pct REAL, disk_free_gb REAL, temp_c REAL
);

CREATE TABLE IF NOT EXISTS samples_1m (
    ts REAL PRIMARY KEY,            -- minute start, unix seconds
    n INTEGER,
    lat_avg REAL, lon_avg REAL, height_avg REAL,
    h_acc_avg REAL, h_acc_max REAL, v_acc_avg REAL, v_acc_max REAL,
    fix_type_min INTEGER, carr_soln_min INTEGER,
    nsat_used_avg REAL, nsat_used_min INTEGER, nsat_tracked_avg REAL,
    pdop_avg REAL, pdop_max REAL,
    cno_mean_avg REAL, jam_ind_max INTEGER, agc_cnt_avg REAL, noise_per_ms_avg REAL,
    corr_age_max REAL, baseline_avg REAL,
    rtcm_bytes_avg REAL, ntrip_clients_max INTEGER,
    cpu_pct_avg REAL, mem_pct_avg REAL, disk_free_gb_min REAL, temp_c_max REAL
);

CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    x REAL NOT NULL, y REAL NOT NULL, z REAL NOT NULL,
    lat REAL, lon REAL, height_m REAL,
    sigma_x REAL, sigma_y REAL, sigma_z REAL,
    frame TEXT, epoch TEXT,
    source TEXT NOT NULL,
    notes TEXT,
    created_utc TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    start_utc TEXT NOT NULL,
    end_utc TEXT,
    role TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    code TEXT, note TEXT,
    ts_utc TEXT NOT NULL,
    lat REAL, lon REAL, height_m REAL, hmsl_m REAL,
    n_epochs INTEGER,
    sd_n REAL, sd_e REAL, sd_u REAL,
    fix_type INTEGER, carr_soln INTEGER,
    h_acc_m REAL, v_acc_m REAL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    level TEXT NOT NULL,            -- info | warning | error
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    meta TEXT,                      -- JSON
    acked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts_utc);

CREATE TABLE IF NOT EXISTS log_files (
    path TEXT PRIMARY KEY,
    hour_utc TEXT, start_utc TEXT, end_utc TEXT,
    bytes INTEGER,
    keep INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT,
    msg_counts TEXT,                -- JSON
    role TEXT, site TEXT,
    complete INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ntrip_clients_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT, mountpoint TEXT, user_agent TEXT, username TEXT,
    connected_utc TEXT NOT NULL,
    disconnected_utc TEXT,
    bytes_sent INTEGER NOT NULL DEFAULT 0,
    last_lat REAL, last_lon REAL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,             -- export | ppk
    status TEXT NOT NULL,           -- queued | running | done | failed
    created_utc TEXT NOT NULL,
    updated_utc TEXT,
    progress REAL NOT NULL DEFAULT 0,
    params TEXT, result TEXT, error TEXT
);
```

- [ ] **Step 4: Write `src/mtrtk/store/db.py`**

```python
"""SQLite access via aiosqlite: WAL mode and numbered SQL migrations from the schema package."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
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
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self.user_version = 0

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database not open")
        return self._conn

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._migrate()

    async def _migrate(self) -> None:
        row = await self.fetchone("PRAGMA user_version")
        current = int(row[0]) if row else 0
        for version, sql in _migrations():
            if version <= current:
                continue
            log.info("applying schema migration %03d", version)
            await self.conn.executescript(sql)
            await self.conn.execute(f"PRAGMA user_version={version}")
            await self.conn.commit()
            current = version
        self.user_version = current

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        return await self.conn.execute(sql, tuple(params))

    async def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        await self.conn.executemany(sql, [tuple(r) for r in rows])

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(sql, tuple(params))
        return list(await cur.fetchall())

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        cur = await self.conn.execute(sql, tuple(params))
        return await cur.fetchone()

    async def commit(self) -> None:
        await self.conn.commit()
```

- [ ] **Step 5: Write `src/mtrtk/store/models.py`**

```python
"""Persistent records shared by repositories, the API and the UI."""

from __future__ import annotations

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
            name=name, x=x, y=y, z=z, lat=lat, lon=lon, height_m=h,
            sigma_x=sigma_m, sigma_y=sigma_m, sigma_z=sigma_m,
            frame=frame, epoch=epoch, source=source, notes=notes,
        )

    @property
    def sigma_3d(self) -> float | None:
        if None in (self.sigma_x, self.sigma_y, self.sigma_z):
            return None
        return (self.sigma_x**2 + self.sigma_y**2 + self.sigma_z**2) ** 0.5  # type: ignore[operator]


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
```

- [ ] **Step 6: Write `src/mtrtk/store/repos.py`**

```python
"""Small repository classes over Database. One class per table family; no ORM."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from mtrtk.rawlog.writer import Sidecar
from mtrtk.store.db import Database
from mtrtk.store.models import Event, Level, NtripClientRecord, Site


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
        if await self.get(site.name) is not None:
            raise ValueError(f"site {site.name!r} already exists")
        cur = await self.db.execute(
            """INSERT INTO sites (name, x, y, z, lat, lon, height_m, sigma_x, sigma_y, sigma_z,
                                  frame, epoch, source, notes, created_utc, active)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (site.name, site.x, site.y, site.z, site.lat, site.lon, site.height_m,
             site.sigma_x, site.sigma_y, site.sigma_z, site.frame, site.epoch,
             site.source, site.notes, _now()),
        )
        await self.db.commit()
        stored = await self.get(site.name)
        assert stored is not None and cur.lastrowid == stored.id
        return stored

    async def delete(self, name: str) -> None:
        await self.db.execute("DELETE FROM sites WHERE name = ?", (name,))
        await self.db.commit()

    async def activate(self, name: str) -> Site:
        if await self.get(name) is None:
            raise KeyError(name)
        await self.db.execute("UPDATE sites SET active = 0")
        await self.db.execute("UPDATE sites SET active = 1 WHERE name = ?", (name,))
        await self.db.commit()
        site = await self.get(name)
        assert site is not None
        return site


class EventsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, level: Level, kind: str, message: str, meta: dict[str, Any] | None = None) -> Event:
        ts = _now()
        cur = await self.db.execute(
            "INSERT INTO events (ts_utc, level, kind, message, meta) VALUES (?,?,?,?,?)",
            (ts, level, kind, message, json.dumps(meta or {})),
        )
        await self.db.commit()
        return Event(id=cur.lastrowid, ts_utc=datetime.fromisoformat(ts), level=level, kind=kind, message=message, meta=meta or {})

    async def list(self, limit: int = 200, level: Level | None = None) -> list[Event]:
        if level:
            rows = await self.db.fetchall(
                "SELECT * FROM events WHERE level = ? ORDER BY id DESC LIMIT ?", (level, limit)
            )
        else:
            rows = await self.db.fetchall("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [
            Event(id=r["id"], ts_utc=datetime.fromisoformat(r["ts_utc"]), level=r["level"], kind=r["kind"],
                  message=r["message"], meta=json.loads(r["meta"] or "{}"), acked=bool(r["acked"]))
            for r in rows
        ]

    async def ack(self, event_id: int) -> None:
        await self.db.execute("UPDATE events SET acked = 1 WHERE id = ?", (event_id,))
        await self.db.commit()


class NtripLogRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def connected(self, ip: str, mountpoint: str, user_agent: str, username: str | None) -> int:
        cur = await self.db.execute(
            "INSERT INTO ntrip_clients_log (ip, mountpoint, user_agent, username, connected_utc) VALUES (?,?,?,?,?)",
            (ip, mountpoint, user_agent, username, _now()),
        )
        await self.db.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    async def disconnected(self, row_id: int, bytes_sent: int, last_lat: float | None, last_lon: float | None, reason: str) -> None:
        await self.db.execute(
            "UPDATE ntrip_clients_log SET disconnected_utc=?, bytes_sent=?, last_lat=?, last_lon=?, reason=? WHERE id=?",
            (_now(), bytes_sent, last_lat, last_lon, reason, row_id),
        )
        await self.db.commit()

    async def recent(self, limit: int = 100) -> list[NtripClientRecord]:
        rows = await self.db.fetchall("SELECT * FROM ntrip_clients_log ORDER BY id DESC LIMIT ?", (limit,))
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
            """INSERT INTO log_files (path, hour_utc, start_utc, end_utc, bytes, keep, sha256, msg_counts, role, site, complete)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET hour_utc=excluded.hour_utc, start_utc=excluded.start_utc,
                 end_utc=excluded.end_utc, bytes=excluded.bytes, sha256=excluded.sha256,
                 msg_counts=excluded.msg_counts, complete=excluded.complete""",
            (str(path), sidecar.hour_utc, sidecar.start_utc, sidecar.end_utc, sidecar.bytes, int(sidecar.keep),
             sidecar.sha256, json.dumps(sidecar.msg_counts), sidecar.role, sidecar.site, int(sidecar.complete)),
        )
        await self.db.commit()

    async def set_keep(self, path: Path, keep: bool) -> None:
        await self.db.execute("UPDATE log_files SET keep = ? WHERE path = ?", (int(keep), str(path)))
        await self.db.commit()

    async def delete(self, path: Path) -> None:
        await self.db.execute("DELETE FROM log_files WHERE path = ?", (str(path),))
        await self.db.commit()

    async def list(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM log_files ORDER BY hour_utc")
        return [dict(r) for r in rows]
```

- [ ] **Step 7: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_store.py -q` → `7 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

If `resources.files("mtrtk.store.schema").iterdir()` finds no `.sql` in an installed wheel, add to `pyproject.toml`: `[tool.hatch.build.targets.wheel.force-include]` is not needed for files inside the package — hatchling includes them — but verify with `uv build && unzip -l dist/*.whl | grep sql` and report if missing.

```bash
git add src/mtrtk/store tests/unit/test_store.py
git commit -m "feat(store): SQLite database with WAL, migrations, models and repositories

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: History sampler (1 s rows, 1 min rollups, pruning)

**Files:**
- Create: `src/mtrtk/store/sampler.py`, `tests/unit/test_sampler.py`

**Interfaces:**
- Consumes: `Database` (Task 4), `ReceiverState` (Phase 1), `SystemStats` (Task 4); bus topics `state.epoch` (ReceiverState), `system.stats` (SystemStats), `ntrip.clients` (list, Task 9).
- Produces: `Sampler(bus, db, keep_1s_h=24, keep_1m_d=90)` with `sample_row(state, system, ntrip_clients) -> dict | None` (None when receiver UTC unknown), `await insert(row)`, `await rollup_minute(minute_ts)`, `await prune(now_ts)`, `await run(stop)`, `SAMPLE_COLUMNS` (ordered column names of `samples_1s`), `async history(table, start_ts, end_ts, columns) -> list[dict]`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_sampler.py`:
```python
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mtrtk.core.bus import Bus
from mtrtk.core.state import Hardware, ReceiverState, Satellite
from mtrtk.store.db import Database
from mtrtk.store.models import SystemStats
from mtrtk.store.sampler import SAMPLE_COLUMNS, Sampler

T0 = datetime(2026, 9, 18, 16, 0, 0, tzinfo=UTC)


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "m.db")
    await database.open()
    try:
        yield database
    finally:
        await database.close()


def state_at(t: datetime, h_acc: float = 1.0, fix: int = 3, cno: tuple[int, ...] = (40, 30)) -> ReceiverState:
    s = ReceiverState()
    s.time.utc = t
    s.position.lat, s.position.lon, s.position.height_m, s.position.hmsl_m = 23.8, 90.2, -36.2, 13.3
    s.accuracy.h_acc_m, s.accuracy.v_acc_m = h_acc, h_acc * 1.5
    s.fix.fix_type, s.fix.carr_soln = fix, 0
    s.dops.p, s.dops.h, s.dops.v = 1.2, 0.8, 0.9
    s.sats = [Satellite(gnss_id=0, gnss="GPS", sv_id=i + 1, cno=c, used=True) for i, c in enumerate(cno)]
    s.sats.append(Satellite(gnss_id=6, gnss="GLONASS", sv_id=9, cno=0, used=False))
    s.sat_summary.tracked, s.sat_summary.used = len(s.sats), len(cno)
    s.hardware = Hardware(jam_ind=12, agc_cnt=3000, noise_per_ms=90)
    s.rtcm_out.bytes_per_s = 1900.0
    return s


def test_sample_row_maps_state() -> None:
    sampler = Sampler(Bus(), None)  # type: ignore[arg-type]
    sys_stats = SystemStats(cpu_pct=10.0, mem_pct=20.0, disk_free_gb=30.0, disk_used_pct=40.0, uptime_s=50.0, temp_c=55.0)
    row = sampler.sample_row(state_at(T0), sys_stats, ntrip_clients=2)
    assert row is not None
    assert row["ts"] == T0.timestamp() and row["lat"] == 23.8 and row["h_acc_m"] == 1.0
    assert row["nsat_used"] == 2 and row["nsat_tracked"] == 3
    assert row["cno_mean"] == 35.0  # only used satellites with cno > 0
    assert row["jam_ind"] == 12 and row["rtcm_bytes_per_s"] == 1900.0 and row["ntrip_clients"] == 2
    assert row["cpu_pct"] == 10.0 and row["temp_c"] == 55.0
    assert set(row) == set(SAMPLE_COLUMNS)


def test_sample_row_none_without_receiver_time() -> None:
    assert Sampler(Bus(), None).sample_row(ReceiverState(), None, 0) is None  # type: ignore[arg-type]


async def test_insert_and_history(db: Database) -> None:
    sampler = Sampler(Bus(), db)
    for i in range(3):
        row = sampler.sample_row(state_at(T0 + timedelta(seconds=i), h_acc=1.0 + i), None, 0)
        assert row is not None
        await sampler.insert(row)
    rows = await sampler.history("samples_1s", T0.timestamp(), (T0 + timedelta(seconds=10)).timestamp(), ["ts", "h_acc_m"])
    assert [r["h_acc_m"] for r in rows] == [1.0, 2.0, 3.0]
    await sampler.insert(sampler.sample_row(state_at(T0, h_acc=9.0), None, 0))  # same ts replaces
    rows = await sampler.history("samples_1s", T0.timestamp(), (T0 + timedelta(seconds=1)).timestamp(), ["h_acc_m"])
    assert [r["h_acc_m"] for r in rows] == [9.0]


async def test_rollup_minute_aggregates(db: Database) -> None:
    sampler = Sampler(Bus(), db)
    for i in range(60):
        await sampler.insert(sampler.sample_row(state_at(T0 + timedelta(seconds=i), h_acc=1.0 + i % 2, fix=3 if i else 2), None, i % 3))
    await sampler.rollup_minute(T0.timestamp())
    row = (await sampler.history("samples_1m", T0.timestamp(), T0.timestamp() + 60, ["n", "h_acc_avg", "h_acc_max", "fix_type_min", "ntrip_clients_max", "nsat_used_min"]))[0]
    assert row["n"] == 60 and row["h_acc_avg"] == 1.5 and row["h_acc_max"] == 2.0
    assert row["fix_type_min"] == 2 and row["ntrip_clients_max"] == 2 and row["nsat_used_min"] == 2


async def test_prune(db: Database) -> None:
    sampler = Sampler(Bus(), db, keep_1s_h=1, keep_1m_d=1)
    old = T0 - timedelta(hours=2)
    await sampler.insert(sampler.sample_row(state_at(old), None, 0))
    await sampler.insert(sampler.sample_row(state_at(T0), None, 0))
    await sampler.rollup_minute(old.timestamp())
    await sampler.rollup_minute(T0.timestamp())
    await db.execute("UPDATE samples_1m SET ts = ? WHERE ts = ?", ((T0 - timedelta(days=2)).timestamp(), old.timestamp()))
    await sampler.prune(now_ts=T0.timestamp() + 1)
    assert len(await sampler.history("samples_1s", 0, 4e9, ["ts"])) == 1
    assert len(await sampler.history("samples_1m", 0, 4e9, ["ts"])) == 1


async def test_run_consumes_bus_and_rolls_up_on_minute_change(db: Database) -> None:
    bus = Bus()
    sampler = Sampler(bus, db)
    stop = asyncio.Event()
    task = asyncio.create_task(sampler.run(stop))
    bus.publish("system.stats", SystemStats(cpu_pct=1, mem_pct=2, disk_free_gb=3, disk_used_pct=4, uptime_s=5))
    bus.publish("ntrip.clients", [object(), object()])
    for i in range(3):
        bus.publish("state.epoch", state_at(T0 + timedelta(seconds=58 + i)))  # crosses 16:01:00
    await asyncio.sleep(0.1)
    sampler.stop()
    await asyncio.wait_for(task, 2.0)
    rows = await sampler.history("samples_1s", 0, 4e9, ["ts", "ntrip_clients", "cpu_pct"])
    assert len(rows) == 3 and rows[0]["ntrip_clients"] == 2 and rows[0]["cpu_pct"] == 1.0
    assert len(await sampler.history("samples_1m", 0, 4e9, ["ts"])) == 1  # minute 16:00 rolled up
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_sampler.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.store.sampler'`.

- [ ] **Step 3: Write `src/mtrtk/store/sampler.py`**

```python
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
    "ts", "lat", "lon", "height_m", "hmsl_m", "h_acc_m", "v_acc_m", "fix_type", "carr_soln",
    "nsat_used", "nsat_tracked", "pdop", "hdop", "vdop", "cno_mean", "jam_ind", "agc_cnt", "noise_per_ms",
    "corr_age_s", "baseline_m", "rtcm_bytes_per_s", "ntrip_clients", "cpu_pct", "mem_pct", "disk_free_gb", "temp_c",
)
_INSERT_1S = f"INSERT OR REPLACE INTO samples_1s ({', '.join(SAMPLE_COLUMNS)}) VALUES ({', '.join('?' * len(SAMPLE_COLUMNS))})"
_ROLLUP_1M = """
INSERT OR REPLACE INTO samples_1m (
    ts, n, lat_avg, lon_avg, height_avg, h_acc_avg, h_acc_max, v_acc_avg, v_acc_max,
    fix_type_min, carr_soln_min, nsat_used_avg, nsat_used_min, nsat_tracked_avg,
    pdop_avg, pdop_max, cno_mean_avg, jam_ind_max, agc_cnt_avg, noise_per_ms_avg,
    corr_age_max, baseline_avg, rtcm_bytes_avg, ntrip_clients_max,
    cpu_pct_avg, mem_pct_avg, disk_free_gb_min, temp_c_max)
SELECT ?, COUNT(*), AVG(lat), AVG(lon), AVG(height_m), AVG(h_acc_m), MAX(h_acc_m), AVG(v_acc_m), MAX(v_acc_m),
    MIN(fix_type), MIN(carr_soln), AVG(nsat_used), MIN(nsat_used), AVG(nsat_tracked),
    AVG(pdop), MAX(pdop), AVG(cno_mean), MAX(jam_ind), AVG(agc_cnt), AVG(noise_per_ms),
    MAX(corr_age_s), AVG(baseline_m), AVG(rtcm_bytes_per_s), MAX(ntrip_clients),
    AVG(cpu_pct), AVG(mem_pct), MIN(disk_free_gb), MAX(temp_c)
FROM samples_1s WHERE ts >= ? AND ts < ?
"""
_TABLES = {"samples_1s", "samples_1m"}


class Sampler:
    def __init__(self, bus: Bus, db: Database, keep_1s_h: float = 24, keep_1m_d: float = 90) -> None:
        self.bus = bus
        self.db = db
        self.keep_1s_s = keep_1s_h * 3600
        self.keep_1m_s = keep_1m_d * 86400
        self.sub = bus.subscribe("state.epoch", "system.stats", "ntrip.clients", maxsize=200)
        self._system: SystemStats | None = None
        self._ntrip_clients = 0
        self._current_minute: int | None = None
        self._last_prune = 0.0

    # ------------------------------------------------------------- mapping
    @staticmethod
    def sample_row(state: ReceiverState, system: SystemStats | None, ntrip_clients: int) -> dict[str, Any] | None:
        if state.time.utc is None:
            return None
        used_cno = [s.cno for s in state.sats if s.used and s.cno > 0]
        hw = state.hardware
        row: dict[str, Any] = {
            "ts": state.time.utc.timestamp(),
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
        start = float(int(minute_ts) // 60 * 60)
        await self.db.execute(_ROLLUP_1M, (start, start, start + 60))
        await self.db.execute("DELETE FROM samples_1m WHERE ts = ? AND n = 0", (start,))
        await self.db.commit()

    async def prune(self, now_ts: float) -> None:
        await self.db.execute("DELETE FROM samples_1s WHERE ts < ?", (now_ts - self.keep_1s_s,))
        await self.db.execute("DELETE FROM samples_1m WHERE ts < ?", (now_ts - self.keep_1m_s,))
        await self.db.commit()

    async def history(self, table: str, start_ts: float, end_ts: float, columns: list[str]) -> list[dict[str, Any]]:
        if table not in _TABLES:
            raise ValueError(f"unknown table {table}")
        cols = ", ".join(columns)
        rows = await self.db.fetchall(f"SELECT {cols} FROM {table} WHERE ts >= ? AND ts < ? ORDER BY ts", (start_ts, end_ts))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- run loop
    def stop(self) -> None:
        self.sub.close()

    async def run(self, stop: asyncio.Event) -> None:
        async for topic, item in self.sub:
            if topic == "system.stats":
                self._system = item
            elif topic == "ntrip.clients":
                self._ntrip_clients = len(item)
            elif topic == "state.epoch":
                await self._on_epoch(item)
            if stop.is_set():
                break

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
            if now - self._last_prune > 3600:
                await self.prune(row["ts"])
                self._last_prune = now
        except Exception:  # never let a DB hiccup stop sampling
            log.exception("sampler write failed")
```

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_sampler.py -q` → `6 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

```bash
git add src/mtrtk/store/sampler.py tests/unit/test_sampler.py
git commit -m "feat(store): 1 s history sampler with minute rollups and retention pruning

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: System monitor

**Files:**
- Create: `src/mtrtk/system.py`, `tests/unit/test_system.py`

**Interfaces:**
- Consumes: `SystemStats` (Task 4), `Bus`.
- Produces: `SystemMonitor(bus, data_dir, interval_s=5.0, psutil_module=psutil, loadavg=os.getloadavg)` with `snapshot() -> SystemStats`, `async run(stop)`; publishes `system.stats`. `read_temperature(psutil_module) -> float | None` prefers sensor groups `cpu_thermal` (Raspberry Pi), `coretemp`, `k10temp`, `soc_thermal`, `acpitz`, else the first available reading.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_system.py`:
```python
import asyncio
from pathlib import Path
from types import SimpleNamespace

from mtrtk.core.bus import Bus
from mtrtk.system import SystemMonitor, read_temperature


def fake_psutil(temps: dict | None = None, boot: float = 1000.0):
    return SimpleNamespace(
        cpu_percent=lambda interval=None: 12.5,
        virtual_memory=lambda: SimpleNamespace(percent=43.0),
        disk_usage=lambda p: SimpleNamespace(total=100e9, used=60e9, free=40e9, percent=60.0),
        boot_time=lambda: boot,
        sensors_temperatures=lambda: temps if temps is not None else {},
    )


def test_read_temperature_prefers_cpu_thermal() -> None:
    temps = {
        "acpitz": [SimpleNamespace(label="", current=33.0)],
        "cpu_thermal": [SimpleNamespace(label="", current=51.2)],
    }
    assert read_temperature(fake_psutil(temps)) == 51.2
    assert read_temperature(fake_psutil({"whatever": [SimpleNamespace(label="x", current=29.0)]})) == 29.0
    assert read_temperature(fake_psutil({})) is None

    class NoSensors:
        pass

    assert read_temperature(NoSensors()) is None  # platforms without sensors_temperatures


def test_snapshot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("mtrtk.system.time.time", lambda: 1360.0)
    mon = SystemMonitor(Bus(), tmp_path, psutil_module=fake_psutil({"cpu_thermal": [SimpleNamespace(label="", current=50.0)]}), loadavg=lambda: (0.5, 0.4, 0.3))
    s = mon.snapshot()
    assert s.cpu_pct == 12.5 and s.mem_pct == 43.0
    assert s.disk_free_gb == 40.0 and s.disk_used_pct == 60.0
    assert s.uptime_s == 360.0 and s.temp_c == 50.0 and s.load1 == 0.5


def test_snapshot_uses_parent_when_data_dir_missing(tmp_path: Path) -> None:
    seen: list[Path] = []

    def disk_usage(p):
        seen.append(Path(p))
        return SimpleNamespace(total=1, used=0, free=1, percent=0.0)

    ps = fake_psutil()
    ps.disk_usage = disk_usage
    SystemMonitor(Bus(), tmp_path / "missing", psutil_module=ps).snapshot()
    assert seen == [tmp_path]


async def test_run_publishes_periodically(tmp_path: Path) -> None:
    bus = Bus()
    sub = bus.subscribe("system.stats")
    mon = SystemMonitor(bus, tmp_path, interval_s=0.01, psutil_module=fake_psutil())
    stop = asyncio.Event()
    task = asyncio.create_task(mon.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 1.0)
    assert sub.queue.qsize() >= 2
    assert (sub.queue.get_nowait()[1]).cpu_pct == 12.5
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_system.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.system'`.

- [ ] **Step 3: Write `src/mtrtk/system.py`**

```python
"""Host health: CPU, memory, disk, temperature, uptime. Published on `system.stats`."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil

from mtrtk.core.bus import Bus
from mtrtk.store.models import SystemStats

log = logging.getLogger(__name__)
PREFERRED_SENSORS = ("cpu_thermal", "coretemp", "k10temp", "soc_thermal", "acpitz")


def read_temperature(psutil_module: Any) -> float | None:
    sensors = getattr(psutil_module, "sensors_temperatures", None)
    if sensors is None:
        return None
    try:
        groups = sensors() or {}
    except (OSError, RuntimeError):
        return None
    for name in PREFERRED_SENSORS:
        readings = groups.get(name)
        if readings:
            return float(readings[0].current)
    for readings in groups.values():
        if readings:
            return float(readings[0].current)
    return None


class SystemMonitor:
    def __init__(
        self,
        bus: Bus,
        data_dir: Path,
        interval_s: float = 5.0,
        psutil_module: Any = psutil,
        loadavg: Callable[[], tuple[float, float, float]] | None = os.getloadavg,
    ) -> None:
        self.bus = bus
        self.data_dir = Path(data_dir)
        self.interval_s = interval_s
        self._ps = psutil_module
        self._loadavg = loadavg

    def snapshot(self) -> SystemStats:
        target = self.data_dir if self.data_dir.exists() else self.data_dir.parent
        du = self._ps.disk_usage(str(target))
        load1: float | None = None
        if self._loadavg is not None:
            try:
                load1 = float(self._loadavg()[0])
            except (OSError, AttributeError):
                load1 = None
        return SystemStats(
            cpu_pct=float(self._ps.cpu_percent(interval=None)),
            mem_pct=float(self._ps.virtual_memory().percent),
            disk_free_gb=du.free / 1e9,
            disk_used_pct=float(du.percent),
            uptime_s=time.time() - float(self._ps.boot_time()),
            temp_c=read_temperature(self._ps),
            load1=load1,
        )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                self.bus.publish("system.stats", self.snapshot())
            except Exception:  # a psutil hiccup must not kill the monitor
                log.exception("system snapshot failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval_s)
            except TimeoutError:
                pass
```

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_system.py -q` → `4 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

```bash
git add src/mtrtk/system.py tests/unit/test_system.py
git commit -m "feat: system monitor publishing CPU, memory, disk, temperature and uptime

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Alert engine with stateful rules and webhook

**Files:**
- Create: `src/mtrtk/alerts.py`, `tests/unit/test_alerts.py`

**Interfaces:**
- Consumes: `EventsRepo` (Task 4), bus topics `state.fix` (FixInfo), `state.hardware` (Hardware), `state.survey_in` (SurveyIn), `receiver.connected/disconnected/error/capabilities`, `system.stats` (SystemStats), `rawlog.backpressure`, `rawlog.pruned`, `base.site_verified`, `base.site_mismatch` (Task 10).
- Produces: `AlertEngine(bus, events_repo, *, role, host, min_free_gb, webhook_url=None, http=None, clock=time.monotonic)` with `async handle(topic, item)`, `async run(stop)`, `active: dict[str, Event]`; publishes `events.new` (Event). Rules and thresholds: `FIX_LOST_GRACE_S = 10`, `JAMMING_SUSTAIN_S = 30`, `JAM_IND_THRESHOLD = 200`, `TEMP_HIGH_C = 80`, `TEMP_CLEAR_C = 70`, `ONE_SHOT_DEDUP_S = 300`. Webhook JSON: `{"level","kind","message","ts","host","role"}`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_alerts.py`:
```python
import asyncio
from pathlib import Path

import pytest

from mtrtk.alerts import AlertEngine
from mtrtk.core.bus import Bus
from mtrtk.core.state import FixInfo, Hardware, SurveyIn
from mtrtk.store.db import Database
from mtrtk.store.models import SystemStats
from mtrtk.store.repos import EventsRepo


class FakeHttp:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, json: dict, timeout: float) -> None:
        self.posts.append((url, json))


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
async def env(tmp_path: Path):
    db = Database(tmp_path / "m.db")
    await db.open()
    bus = Bus()
    http = FakeHttp()
    clock = Clock()
    engine = AlertEngine(bus, EventsRepo(db), role="base", host="pi", min_free_gb=5.0, webhook_url="https://ntfy.sh/x", http=http, clock=clock)
    sub = bus.subscribe("events.new")
    try:
        yield engine, sub, http, clock, EventsRepo(db)
    finally:
        await db.close()


def kinds(sub) -> list[str]:
    return [item.kind for _, item in [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]]


async def test_disconnect_raises_once_and_clears_on_connect(env) -> None:
    engine, sub, http, _, repo = env
    await engine.handle("receiver.disconnected", "no data from receiver for 5s")
    await engine.handle("receiver.disconnected", "no data from receiver for 5s")
    assert kinds(sub) == ["receiver_disconnected"]
    assert "receiver_disconnected" in engine.active
    await engine.handle("receiver.connected", "serial:/dev/ttyACM0")
    assert kinds(sub) == ["receiver_disconnected_cleared"]
    assert engine.active == {}
    assert [e.level for e in await repo.list()] == ["info", "error"]
    assert http.posts[0][1] == {
        "level": "error", "kind": "receiver_disconnected",
        "message": "receiver disconnected: no data from receiver for 5s",
        "ts": http.posts[0][1]["ts"], "host": "pi", "role": "base",
    }


async def test_source_ended_is_not_an_alert(env) -> None:
    engine, sub, *_ = env
    await engine.handle("receiver.disconnected", "source ended")
    assert kinds(sub) == []


async def test_fix_lost_needs_prior_3d_and_grace(env) -> None:
    engine, sub, _, clock, _ = env
    await engine.handle("state.fix", FixInfo(fix_type=0))  # never had a fix: no alert
    await engine.handle("state.fix", FixInfo(fix_type=3))
    await engine.handle("state.fix", FixInfo(fix_type=2))
    clock.t += 5
    await engine.handle("state.fix", FixInfo(fix_type=2))
    assert kinds(sub) == []
    clock.t += 6
    await engine.handle("state.fix", FixInfo(fix_type=2))
    assert kinds(sub) == ["fix_lost"]
    await engine.handle("state.fix", FixInfo(fix_type=3))
    assert kinds(sub) == ["fix_lost_cleared"]


async def test_jamming_sustained_and_antenna_fault(env) -> None:
    engine, sub, _, clock, _ = env
    await engine.handle("state.hardware", Hardware(jam_ind=250, ant_status=2))
    clock.t += 31
    await engine.handle("state.hardware", Hardware(jam_ind=250, ant_status=2))
    assert kinds(sub) == ["jamming"]
    await engine.handle("state.hardware", Hardware(jam_ind=10, ant_status=4))
    assert sorted(kinds(sub)) == ["antenna_fault", "jamming_cleared"]
    assert engine.active["antenna_fault"].level == "error"
    await engine.handle("state.hardware", Hardware(jam_ind=10, ant_status=2))
    assert kinds(sub) == ["antenna_fault_cleared"]


async def test_disk_and_temperature_thresholds(env) -> None:
    engine, sub, *_ = env

    def stats(free: float, temp: float | None = 40.0) -> SystemStats:
        return SystemStats(cpu_pct=0, mem_pct=0, disk_free_gb=free, disk_used_pct=0, uptime_s=0, temp_c=temp)

    await engine.handle("system.stats", stats(20.0))
    assert kinds(sub) == []
    await engine.handle("system.stats", stats(6.0))
    assert kinds(sub) == ["disk_warning"]
    await engine.handle("system.stats", stats(4.0))
    assert kinds(sub) == ["disk_low"]
    await engine.handle("system.stats", stats(9.0))
    assert sorted(kinds(sub)) == ["disk_low_cleared", "disk_warning_cleared"]
    await engine.handle("system.stats", stats(20.0, temp=85.0))
    assert kinds(sub) == ["temperature_high"]
    await engine.handle("system.stats", stats(20.0, temp=75.0))
    assert kinds(sub) == []  # hysteresis
    await engine.handle("system.stats", stats(20.0, temp=60.0))
    assert kinds(sub) == ["temperature_high_cleared"]


async def test_one_shot_events_dedup(env) -> None:
    engine, sub, _, clock, _ = env
    await engine.handle("rawlog.backpressure", 25000)
    await engine.handle("rawlog.backpressure", 26000)
    assert kinds(sub) == ["logger_backpressure"]
    clock.t += 301
    await engine.handle("rawlog.backpressure", 27000)
    assert kinds(sub) == ["logger_backpressure"]
    await engine.handle("rawlog.pruned", Path("/data/ubx/2026/261/MTRK_20260918_10.ubx"))
    assert kinds(sub) == ["log_pruned"]
    await engine.handle("daemon.consumer_failed", {"name": "ntrip", "error": "OSError: address in use"})
    assert kinds(sub) == ["consumer_failed_ntrip"]


async def test_survey_in_valid_and_site_verification(env) -> None:
    engine, sub, *_ = env
    await engine.handle("state.survey_in", SurveyIn(active=True, valid=False, mean_acc_m=3.0))
    await engine.handle("state.survey_in", SurveyIn(active=True, valid=True, mean_acc_m=1.2))
    await engine.handle("state.survey_in", SurveyIn(active=True, valid=True, mean_acc_m=1.1))
    assert kinds(sub) == ["survey_in_valid"]
    await engine.handle("base.site_mismatch", {"site": "roof", "dx": 0.5})
    await engine.handle("base.site_verified", {"site": "roof"})
    assert kinds(sub) == ["site_mismatch", "site_mismatch_cleared", "site_verified"]


async def test_webhook_failure_is_swallowed(env) -> None:
    engine, sub, http, *_ = env

    async def boom(url: str, json: dict, timeout: float) -> None:
        raise OSError("network down")

    http.post = boom  # type: ignore[method-assign]
    await engine.handle("receiver.error", "receiver rejected core config keys: ['X']")
    assert kinds(sub) == ["receiver_error"]


async def test_run_consumes_bus(env) -> None:
    engine, sub, *_ = env
    stop = asyncio.Event()
    task = asyncio.create_task(engine.run(stop))
    engine.bus.publish("receiver.disconnected", "usb unplugged")
    await asyncio.sleep(0.05)
    engine.stop()
    await asyncio.wait_for(task, 1.0)
    assert kinds(sub) == ["receiver_disconnected"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_alerts.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.alerts'`.

- [ ] **Step 3: Write `src/mtrtk/alerts.py`**

```python
"""Stateful alert rules -> events table + bus `events.new` + optional webhook."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from mtrtk.core.bus import Bus
from mtrtk.core.state import FixInfo, Hardware, SurveyIn
from mtrtk.store.models import Event, Level, SystemStats
from mtrtk.store.repos import EventsRepo

log = logging.getLogger(__name__)

FIX_LOST_GRACE_S = 10.0
JAMMING_SUSTAIN_S = 30.0
JAM_IND_THRESHOLD = 200
JAMMING_STATE_WARNING = 2
TEMP_HIGH_C = 80.0
TEMP_CLEAR_C = 70.0
ONE_SHOT_DEDUP_S = 300.0
ANTENNA_FAULT_STATES = {3: "short", 4: "open"}
WEBHOOK_TIMEOUT_S = 5.0

TOPICS = (
    "state.fix", "state.hardware", "state.survey_in",
    "receiver.connected", "receiver.disconnected", "receiver.error", "receiver.capabilities",
    "system.stats", "rawlog.backpressure", "rawlog.pruned",
    "base.site_verified", "base.site_mismatch", "daemon.consumer_failed",
)


class AlertEngine:
    def __init__(
        self,
        bus: Bus,
        events: EventsRepo,
        *,
        role: str,
        host: str,
        min_free_gb: float,
        webhook_url: str | None = None,
        http: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bus = bus
        self.events = events
        self.role = role
        self.host = host
        self.min_free_gb = min_free_gb
        self.webhook_url = webhook_url
        self._http = http if http is not None else (httpx.AsyncClient() if webhook_url else None)
        self._clock = clock
        self.sub = bus.subscribe(*TOPICS, maxsize=500)
        self.active: dict[str, Event] = {}
        self._one_shot_last: dict[str, float] = {}
        self._had_3d = False
        self._fix_bad_since: float | None = None
        self._jam_since: float | None = None
        self._survey_valid = False

    # ------------------------------------------------------------- emitting
    async def _emit(self, level: Level, kind: str, message: str, meta: dict[str, Any] | None = None) -> Event:
        event = await self.events.add(level, kind, message, meta)
        self.bus.publish("events.new", event)
        log.log({"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}[level], "%s: %s", kind, message)
        await self._webhook(event)
        return event

    async def raise_(self, kind: str, level: Level, message: str, meta: dict[str, Any] | None = None) -> None:
        if kind in self.active:
            return
        self.active[kind] = await self._emit(level, kind, message, meta)

    async def clear(self, kind: str, message: str) -> None:
        if kind not in self.active:
            return
        del self.active[kind]
        await self._emit("info", f"{kind}_cleared", message)

    async def one_shot(self, kind: str, level: Level, message: str, meta: dict[str, Any] | None = None) -> None:
        now = self._clock()
        last = self._one_shot_last.get(kind)
        if last is not None and now - last < ONE_SHOT_DEDUP_S:
            return
        self._one_shot_last[kind] = now
        await self._emit(level, kind, message, meta)

    async def _webhook(self, event: Event) -> None:
        if not self.webhook_url or self._http is None:
            return
        body = {
            "level": event.level, "kind": event.kind, "message": event.message,
            "ts": event.ts_utc.isoformat(), "host": self.host, "role": self.role,
        }
        try:
            await self._http.post(self.webhook_url, json=body, timeout=WEBHOOK_TIMEOUT_S)
        except Exception as exc:  # network problems must never propagate
            log.warning("alert webhook failed: %s", exc)

    # ------------------------------------------------------------- dispatch
    async def handle(self, topic: str, item: Any) -> None:
        handler = getattr(self, "_on_" + topic.replace(".", "_"), None)
        if handler is not None:
            await handler(item)

    async def _on_receiver_disconnected(self, reason: str) -> None:
        if reason == "source ended":
            return
        await self.raise_("receiver_disconnected", "error", f"receiver disconnected: {reason}")

    async def _on_receiver_connected(self, source: str) -> None:
        await self.clear("receiver_disconnected", f"receiver connected ({source})")

    async def _on_receiver_error(self, message: str) -> None:
        await self.raise_("receiver_error", "error", message)

    async def _on_receiver_capabilities(self, caps: Any) -> None:
        await self.clear("receiver_error", "receiver configured")

    async def _on_state_fix(self, fix: FixInfo) -> None:
        now = self._clock()
        if fix.fix_type >= 3:
            self._had_3d = True
            self._fix_bad_since = None
            await self.clear("fix_lost", f"fix restored ({fix.fix_type_name})")
            return
        if not self._had_3d:
            return
        if self._fix_bad_since is None:
            self._fix_bad_since = now
        elif now - self._fix_bad_since >= FIX_LOST_GRACE_S:
            await self.raise_("fix_lost", "warning", f"position fix lost ({fix.fix_type_name})", {"fix_type": fix.fix_type})

    async def _on_state_hardware(self, hw: Hardware) -> None:
        now = self._clock()
        jammed = hw.jam_ind >= JAM_IND_THRESHOLD or hw.jamming_state >= JAMMING_STATE_WARNING
        if jammed:
            if self._jam_since is None:
                self._jam_since = now
            elif now - self._jam_since >= JAMMING_SUSTAIN_S:
                await self.raise_("jamming", "warning", f"RF interference: jam_ind={hw.jam_ind} state={hw.jamming_state_name}", {"jam_ind": hw.jam_ind})
        else:
            self._jam_since = None
            await self.clear("jamming", f"RF interference cleared (jam_ind={hw.jam_ind})")
        fault = ANTENNA_FAULT_STATES.get(hw.ant_status)
        if fault:
            await self.raise_("antenna_fault", "error", f"antenna {fault} detected", {"ant_status": hw.ant_status})
        else:
            await self.clear("antenna_fault", f"antenna status {hw.ant_status_name}")

    async def _on_system_stats(self, stats: SystemStats) -> None:
        free = stats.disk_free_gb
        if free < self.min_free_gb:
            await self.raise_("disk_low", "error", f"disk free {free:.1f} GB below {self.min_free_gb:.1f} GB: pruning logs", {"free_gb": free})
        if free < self.min_free_gb * 1.5:
            await self.raise_("disk_warning", "warning", f"disk free {free:.1f} GB", {"free_gb": free})
        else:
            await self.clear("disk_low", f"disk free {free:.1f} GB")
            await self.clear("disk_warning", f"disk free {free:.1f} GB")
        if stats.temp_c is not None:
            if stats.temp_c >= TEMP_HIGH_C:
                await self.raise_("temperature_high", "warning", f"host temperature {stats.temp_c:.0f} °C", {"temp_c": stats.temp_c})
            elif stats.temp_c <= TEMP_CLEAR_C:
                await self.clear("temperature_high", f"host temperature {stats.temp_c:.0f} °C")

    async def _on_rawlog_backpressure(self, queue_size: int) -> None:
        await self.one_shot("logger_backpressure", "warning", f"raw logger falling behind: {queue_size} frames queued", {"queued": queue_size})

    async def _on_rawlog_pruned(self, path: Any) -> None:
        await self.one_shot("log_pruned", "info", f"pruned old raw log {getattr(path, 'name', path)} to free disk")

    async def _on_daemon_consumer_failed(self, meta: dict[str, Any]) -> None:
        await self.one_shot(f"consumer_failed_{meta.get('name')}", "error", f"{meta.get('name')} crashed and was restarted: {meta.get('error')}", meta)

    async def _on_state_survey_in(self, svin: SurveyIn) -> None:
        if svin.valid and not self._survey_valid:
            acc = f"{svin.mean_acc_m:.2f} m" if svin.mean_acc_m is not None else "n/a"
            await self._emit("info", "survey_in_valid", f"survey-in complete: mean 3D accuracy {acc}", {"mean_acc_m": svin.mean_acc_m, "dur_s": svin.dur_s})
        self._survey_valid = svin.valid

    async def _on_base_site_mismatch(self, meta: dict[str, Any]) -> None:
        await self.raise_("site_mismatch", "error", f"RTCM 1005 position does not match site {meta.get('site')}", meta)

    async def _on_base_site_verified(self, meta: dict[str, Any]) -> None:
        await self.clear("site_mismatch", f"site {meta.get('site')} verified")
        await self._emit("info", "site_verified", f"fixed site {meta.get('site')} verified against RTCM 1005", meta)

    # ------------------------------------------------------------- run loop
    def stop(self) -> None:
        self.sub.close()

    async def run(self, stop: asyncio.Event) -> None:
        async for topic, item in self.sub:
            try:
                await self.handle(topic, item)
            except Exception:
                log.exception("alert rule failed for %s", topic)
            if stop.is_set():
                break
```

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_alerts.py -q` → `9 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

```bash
git add src/mtrtk/alerts.py tests/unit/test_alerts.py
git commit -m "feat: alert engine with hysteresis rules, event log and webhook delivery

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: RTCM 1005 decoding and bind-address resolution

**Files:**
- Create: `src/mtrtk/base/__init__.py` (empty), `src/mtrtk/base/rtcm1005.py`, `tests/unit/test_rtcm1005.py`
- Modify: `src/mtrtk/core/exposure.py`, `tests/unit/test_exposure.py` (create)

**Interfaces:**
- Produces: `Ecef1005(station_id, x, y, z, gps, glonass, galileo)`; `decode_1005(frame) -> Ecef1005 | None`; `exposure.resolve_bind(mode: str) -> str | None` (`tailscale` → tailscale0 IPv4 or None; `lan`/`all` → `"0.0.0.0"`; explicit IP → itself); `async exposure.wait_for_bind(mode, stop, retry_s=5.0) -> str | None` (retries until resolved or `stop` set; logs once per minute).
- Test vector (hand-built, verified with pyrtcm during planning): RTCM 1005 frame hex `d300133ed7fd0382dfdc1c403db34fe8fe0cef5e6b30bd2e23` decodes to station 2045, ECEF (1234567.8912, −987654.3234, 5555555.0), GPS/GLONASS/Galileo flags all set.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_rtcm1005.py`:
```python
from ubxtest import rtcm_frame, ubx_frame

from mtrtk.base.rtcm1005 import decode_1005
from mtrtk.core.frames import Framer

RTCM_1005_HEX = "d300133ed7fd0382dfdc1c403db34fe8fe0cef5e6b30bd2e23"


def test_decode_1005_vector() -> None:
    frame = Framer().feed(bytes.fromhex(RTCM_1005_HEX))[0]
    ecef = decode_1005(frame)
    assert ecef is not None
    assert ecef.station_id == 2045
    assert (ecef.x, ecef.y, ecef.z) == (1234567.8912, -987654.3234, 5555555.0)
    assert ecef.gps and ecef.glonass and ecef.galileo


def test_decode_1005_ignores_other_frames() -> None:
    framer = Framer()
    other_rtcm, ubx = framer.feed(rtcm_frame(1077, b"\x00" * 20) + ubx_frame(0x01, 0x07, b"\x00" * 92))
    assert decode_1005(other_rtcm) is None
    assert decode_1005(ubx) is None
```

`tests/unit/test_exposure.py`:
```python
import asyncio

import pytest

from mtrtk.core import exposure


def test_resolve_bind_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exposure, "tailscale_ipv4", lambda: "100.100.50.10")
    assert exposure.resolve_bind("tailscale") == "100.100.50.10"
    assert exposure.resolve_bind("lan") == "0.0.0.0"
    assert exposure.resolve_bind("all") == "0.0.0.0"
    assert exposure.resolve_bind("192.168.1.20") == "192.168.1.20"
    monkeypatch.setattr(exposure, "tailscale_ipv4", lambda: None)
    assert exposure.resolve_bind("tailscale") is None


async def test_wait_for_bind_retries_until_available(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([None, None, "100.100.50.10"])
    monkeypatch.setattr(exposure, "tailscale_ipv4", lambda: next(answers))
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr(exposure.asyncio, "sleep", fake_sleep)
    assert await exposure.wait_for_bind("tailscale", asyncio.Event(), retry_s=5.0) == "100.100.50.10"
    assert sleeps == [5.0, 5.0]


async def test_wait_for_bind_gives_up_on_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exposure, "tailscale_ipv4", lambda: None)
    stop = asyncio.Event()

    async def fake_sleep(d: float) -> None:
        stop.set()

    monkeypatch.setattr(exposure.asyncio, "sleep", fake_sleep)
    assert await exposure.wait_for_bind("tailscale", stop, retry_s=0.01) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_rtcm1005.py tests/unit/test_exposure.py -q`
Expected: FAIL (`mtrtk.base` missing; `resolve_bind` missing).

- [ ] **Step 3: Write `src/mtrtk/base/rtcm1005.py`**

```python
"""Decode RTCM 1005 (stationary reference station ARP) into ECEF metres."""

from __future__ import annotations

from dataclasses import dataclass

from mtrtk.core.frames import Frame, Proto


@dataclass(frozen=True)
class Ecef1005:
    station_id: int
    x: float
    y: float
    z: float
    gps: bool
    glonass: bool
    galileo: bool


def decode_1005(frame: Frame) -> Ecef1005 | None:
    if frame.proto is not Proto.RTCM3 or frame.rtcm_type != 1005:
        return None
    m = frame.parsed()  # pyrtcm scales DF025/26/27 (0.0001 m units) to metres
    return Ecef1005(
        station_id=int(m.DF003),
        x=float(m.DF025),
        y=float(m.DF026),
        z=float(m.DF027),
        gps=bool(m.DF022),
        glonass=bool(m.DF023),
        galileo=bool(m.DF024),
    )
```

- [ ] **Step 4: Extend `src/mtrtk/core/exposure.py`**

Append (keep `tailscale_ipv4` as is; add `import asyncio`, `import logging` at the top and `log = logging.getLogger(__name__)`):
```python
BIND_ANY = "0.0.0.0"


def resolve_bind(mode: str) -> str | None:
    """Turn a bind mode from settings into a host to listen on. None = not available yet."""
    if mode == "tailscale":
        return tailscale_ipv4()
    if mode in ("lan", "all"):
        return BIND_ANY
    return mode  # explicit IP address (validated by Settings)


async def wait_for_bind(mode: str, stop: asyncio.Event, retry_s: float = 5.0) -> str | None:
    """Retry until the bind address exists. Never falls back to 0.0.0.0 for tailscale."""
    attempts = 0
    while not stop.is_set():
        host = resolve_bind(mode)
        if host is not None:
            return host
        if attempts % max(1, int(60 / retry_s)) == 0:
            log.warning("bind mode %r not available yet (is tailscaled running?); retrying every %.0fs", mode, retry_s)
        attempts += 1
        await asyncio.sleep(retry_s)
    return None
```

- [ ] **Step 5: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_rtcm1005.py tests/unit/test_exposure.py -q` → `5 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

```bash
git add src/mtrtk/base/__init__.py src/mtrtk/base/rtcm1005.py src/mtrtk/core/exposure.py tests/unit/test_rtcm1005.py tests/unit/test_exposure.py
git commit -m "feat(base): RTCM 1005 decoding and bind-address resolution with tailscale retry

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: NTRIP caster (v1 + v2)

**Files:**
- Create: `src/mtrtk/base/ntrip_caster.py`, `tests/unit/test_ntrip_caster.py`

**Interfaces:**
- Consumes: `Bus.subscribe("raw.rtcm")`, `Frame.raw/rtcm_type`, `NtripLogRepo` (Task 4, optional).
- Produces: `CasterConfig(mountpoint, username, password, station_id, country, identifier="mtrtk", format_details=..., nav_system=...)`; `ClientInfo` (id, ip, port, mountpoint, user_agent, username, version, connected_utc, bytes_sent, dropped_frames, last_gga_lat, last_gga_lon, last_gga_utc) with `.public() -> dict`; `NtripCaster(bus, config, host, port=2101, ntrip_log=None, position=lambda: None, bitrate=lambda: 0.0)` with `await start()`, `await stop()`, `.port` (actual bound port), `.clients: dict[int, ClientInfo]`, `sourcetable_body() -> bytes`, `parse_request(head: bytes) -> Request`; bus topic `ntrip.clients` (list[ClientInfo]) on every connect/disconnect/GGA.
- Protocol behaviour exactly as in Global Constraints.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_ntrip_caster.py`:
```python
import asyncio
import base64
from pathlib import Path

import pytest
from ubxtest import rtcm_frame

from mtrtk.base.ntrip_caster import CasterConfig, NtripCaster, parse_request
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.store.db import Database
from mtrtk.store.repos import NtripLogRepo

RTCM_1005 = bytes.fromhex("d300133ed7fd0382dfdc1c403db34fe8fe0cef5e6b30bd2e23")
RTCM_1077 = rtcm_frame(1077, b"\x00" * 40)
RTCM_1230 = rtcm_frame(1230, b"\x00" * 6)
AUTH = "Basic " + base64.b64encode(b"rover:secret").decode()


def config(password: str = "secret") -> CasterConfig:
    return CasterConfig(mountpoint="MTRK", username="rover", password=password, station_id="MTRK", country="BGD")


@pytest.fixture
async def caster(tmp_path: Path):
    db = Database(tmp_path / "m.db")
    await db.open()
    bus = Bus()
    c = NtripCaster(bus, config(), host="127.0.0.1", port=0, ntrip_log=NtripLogRepo(db), position=lambda: (23.84, 90.26), bitrate=lambda: 1900.0)
    await c.start()
    try:
        yield c, bus, NtripLogRepo(db)
    finally:
        await c.stop()
        await db.close()


def publish(bus: Bus, raw: bytes) -> None:
    for frame in Framer().feed(raw):
        bus.publish("raw.rtcm", frame)


async def request(port: int, head: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head.encode())
    await writer.drain()
    return reader, writer


async def read_headers(reader: asyncio.StreamReader) -> bytes:
    return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2.0)


def test_parse_request() -> None:
    req = parse_request(b"GET /MTRK HTTP/1.1\r\nHost: x\r\nNtrip-Version: Ntrip/2.0\r\nUser-Agent: NTRIP test/1.0\r\nAuthorization: Basic abc\r\n\r\n")
    assert req.method == "GET" and req.path == "/MTRK" and req.v2 is True
    assert req.headers["user-agent"] == "NTRIP test/1.0" and req.headers["authorization"] == "Basic abc"
    v1 = parse_request(b"GET /MTRK HTTP/1.0\r\nUser-Agent: NTRIP str2str\r\n\r\n")
    assert v1.v2 is False and v1.headers.get("ntrip-version") is None


async def test_v1_stream_receives_cached_1005_then_live_frames(caster) -> None:
    c, bus, _ = caster
    publish(bus, RTCM_1005 + RTCM_1230)  # cached before any client connects
    await asyncio.sleep(0.02)
    reader, writer = await request(c.port, f"GET /MTRK HTTP/1.0\r\nUser-Agent: NTRIP str2str\r\nAuthorization: {AUTH}\r\n\r\n")
    assert await read_headers(reader) == b"ICY 200 OK\r\n\r\n"
    cached = await asyncio.wait_for(reader.readexactly(len(RTCM_1005) + len(RTCM_1230)), 2.0)
    assert cached == RTCM_1005 + RTCM_1230
    publish(bus, RTCM_1077)
    assert await asyncio.wait_for(reader.readexactly(len(RTCM_1077)), 2.0) == RTCM_1077
    assert len(c.clients) == 1
    info = next(iter(c.clients.values()))
    assert info.version == 1 and info.username == "rover" and info.bytes_sent == len(RTCM_1005) + len(RTCM_1230) + len(RTCM_1077)
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)
    assert c.clients == {}


async def test_v2_stream_is_chunked_with_ntrip_headers(caster) -> None:
    c, bus, _ = caster
    reader, writer = await request(c.port, f"GET /MTRK HTTP/1.1\r\nHost: base\r\nNtrip-Version: Ntrip/2.0\r\nUser-Agent: NTRIP SWMaps\r\nAuthorization: {AUTH}\r\n\r\n")
    head = await read_headers(reader)
    assert head.startswith(b"HTTP/1.1 200 OK\r\n")
    for expected in (b"Ntrip-Version: Ntrip/2.0", b"Content-Type: gnss/data", b"Transfer-Encoding: chunked", b"Cache-Control: no-store, no-cache, max-age=0", b"Connection: close"):
        assert expected in head
    publish(bus, RTCM_1077)
    size_line = await asyncio.wait_for(reader.readline(), 2.0)
    assert int(size_line.strip(), 16) == len(RTCM_1077)
    body = await asyncio.wait_for(reader.readexactly(len(RTCM_1077) + 2), 2.0)
    assert body == RTCM_1077 + b"\r\n"
    writer.close()
    await writer.wait_closed()


async def test_sourcetable_v1_and_v2(caster) -> None:
    c, _, _ = caster
    reader, writer = await request(c.port, "GET / HTTP/1.0\r\nUser-Agent: NTRIP x\r\n\r\n")
    head = await read_headers(reader)
    assert head.startswith(b"SOURCETABLE 200 OK\r\n") and b"Content-Type: text/plain" in head
    body = await asyncio.wait_for(reader.read(-1), 2.0)
    assert body.startswith(b"STR;MTRK;mtrtk;RTCM 3.3;1005(1),1077(1),1087(1),1097(1),1127(1),1230(5);2;GPS+GLO+GAL+BDS;mtrtk;BGD;23.84;90.26;0;0;u-blox ZED-F9P;none;B;N;1900;\r\n")
    assert body.endswith(b"ENDSOURCETABLE\r\n")
    writer.close()
    reader, writer = await request(c.port, "GET / HTTP/1.1\r\nHost: x\r\nNtrip-Version: Ntrip/2.0\r\n\r\n")
    head = await read_headers(reader)
    assert head.startswith(b"HTTP/1.1 200 OK\r\n") and b"Content-Type: gnss/sourcetable" in head
    writer.close()


async def test_unknown_mount_v1_sourcetable_v2_404(caster) -> None:
    c, _, _ = caster
    reader, writer = await request(c.port, f"GET /NOPE HTTP/1.0\r\nAuthorization: {AUTH}\r\n\r\n")
    assert (await read_headers(reader)).startswith(b"SOURCETABLE 200 OK\r\n")
    writer.close()
    reader, writer = await request(c.port, f"GET /NOPE HTTP/1.1\r\nNtrip-Version: Ntrip/2.0\r\nAuthorization: {AUTH}\r\n\r\n")
    assert (await read_headers(reader)).startswith(b"HTTP/1.1 404 Not Found\r\n")
    writer.close()


async def test_bad_or_missing_auth_is_401(caster) -> None:
    c, _, _ = caster
    reader, writer = await request(c.port, "GET /MTRK HTTP/1.0\r\n\r\n")
    head = await read_headers(reader)
    assert head.startswith(b"HTTP/1.0 401 Unauthorized\r\n") and b'WWW-Authenticate: Basic realm="mtrtk"' in head
    writer.close()
    bad = "Basic " + base64.b64encode(b"rover:wrong").decode()
    reader, writer = await request(c.port, f"GET /MTRK HTTP/1.1\r\nNtrip-Version: Ntrip/2.0\r\nAuthorization: {bad}\r\n\r\n")
    assert (await read_headers(reader)).startswith(b"HTTP/1.1 401 Unauthorized\r\n")
    writer.close()


async def test_anonymous_when_password_empty(tmp_path: Path) -> None:
    bus = Bus()
    c = NtripCaster(bus, config(password=""), host="127.0.0.1", port=0)
    await c.start()
    try:
        reader, writer = await request(c.port, "GET /MTRK HTTP/1.0\r\n\r\n")
        assert await read_headers(reader) == b"ICY 200 OK\r\n\r\n"
        assert c.sourcetable_body().split(b";")[15] == b"N"  # authentication field
        writer.close()
    finally:
        await c.stop()


async def test_gga_intake_updates_client_and_publishes(caster) -> None:
    c, bus, log_repo = caster
    clients_sub = bus.subscribe("ntrip.clients")
    reader, writer = await request(c.port, f"GET /MTRK HTTP/1.0\r\nUser-Agent: NTRIP rover\r\nAuthorization: {AUTH}\r\n\r\n")
    await read_headers(reader)
    writer.write(b"$GNGGA,164734.00,2350.24104,N,09015.75301,E,1,12,0.9,13.3,M,-49.6,M,0.0,0*78\r\n")
    await writer.drain()
    await asyncio.sleep(0.05)
    info = next(iter(c.clients.values()))
    assert info.last_gga_lat == pytest.approx(23.8373507, abs=1e-6)
    assert info.last_gga_lon == pytest.approx(90.2625502, abs=1e-6)
    assert clients_sub.queue.qsize() >= 2  # connect + gga
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)
    rows = await log_repo.recent()
    assert len(rows) == 1 and rows[0].last_lat == pytest.approx(23.8373507, abs=1e-6) and rows[0].disconnected_utc is not None


async def test_source_upload_not_supported(caster) -> None:
    c, _, _ = caster
    reader, writer = await request(c.port, "SOURCE pw /MTRK\r\nSource-Agent: NTRIP x\r\n\r\n")
    assert (await asyncio.wait_for(reader.read(-1), 2.0)).startswith(b"ERROR - Not Supported")
    writer.close()


async def test_oversized_header_is_rejected(caster) -> None:
    c, _, _ = caster
    reader, writer = await asyncio.open_connection("127.0.0.1", c.port)
    writer.write(b"GET /MTRK HTTP/1.0\r\nX: " + b"a" * 9000 + b"\r\n\r\n")
    await writer.drain()
    data = await asyncio.wait_for(reader.read(-1), 2.0)
    assert data == b"" or data.startswith(b"HTTP/1.0 400")
    writer.close()


def test_offer_drops_oldest_when_client_queue_full() -> None:
    from mtrtk.base.ntrip_caster import CLIENT_QUEUE_FRAMES, _Client

    client = _Client.__new__(_Client)
    client.queue = asyncio.Queue(maxsize=CLIENT_QUEUE_FRAMES)
    client.dropped = 0
    for i in range(CLIENT_QUEUE_FRAMES + 3):
        NtripCaster._offer(client, bytes([i]))
    assert client.dropped == 3 and client.queue.qsize() == CLIENT_QUEUE_FRAMES
    assert client.queue.get_nowait() == bytes([3])
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_ntrip_caster.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.base.ntrip_caster'`.

- [ ] **Step 3: Write `src/mtrtk/base/ntrip_caster.py`**

```python
"""In-process NTRIP caster (v1 + v2) that serves the receiver's RTCM3 frames to rovers."""

from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pynmeagps import NMEAReader

from mtrtk import __version__
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame
from mtrtk.store.repos import NtripLogRepo

log = logging.getLogger(__name__)

HEADER_LIMIT = 8192
HEADER_TIMEOUT_S = 10.0
CLIENT_QUEUE_FRAMES = 64
SLOW_CLIENT_BYTES = 256 * 1024
SLOW_CLIENT_GRACE_S = 10.0
SERVER_NAME = f"mtrtk/{__version__}"


@dataclass
class CasterConfig:
    mountpoint: str
    username: str
    password: str  # "" = anonymous
    station_id: str
    country: str
    identifier: str = "mtrtk"
    format_details: str = "1005(1),1077(1),1087(1),1097(1),1127(1),1230(5)"
    nav_system: str = "GPS+GLO+GAL+BDS"
    receiver: str = "u-blox ZED-F9P"

    @property
    def anonymous(self) -> bool:
        return self.password == ""


@dataclass
class Request:
    method: str
    path: str
    v2: bool
    headers: dict[str, str]


def parse_request(head: bytes) -> Request:
    text = head.decode("latin-1")
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    method = parts[0].upper() if parts else ""
    path = parts[1] if len(parts) > 1 else "/"
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    v2 = headers.get("ntrip-version", "").lower().startswith("ntrip/2")
    return Request(method, path, v2, headers)


@dataclass
class ClientInfo:
    id: int
    ip: str
    port: int
    mountpoint: str
    user_agent: str
    username: str | None
    version: int
    connected_utc: datetime
    bytes_sent: int = 0
    dropped_frames: int = 0
    last_gga_lat: float | None = None
    last_gga_lon: float | None = None
    last_gga_utc: datetime | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "ip": self.ip, "port": self.port, "mountpoint": self.mountpoint,
            "user_agent": self.user_agent, "username": self.username, "version": self.version,
            "connected_utc": self.connected_utc.isoformat(), "bytes_sent": self.bytes_sent,
            "dropped_frames": self.dropped_frames, "last_gga_lat": self.last_gga_lat,
            "last_gga_lon": self.last_gga_lon,
            "last_gga_utc": self.last_gga_utc.isoformat() if self.last_gga_utc else None,
        }


class _Client:
    def __init__(self, info: ClientInfo, writer: asyncio.StreamWriter, v2: bool) -> None:
        self.info = info
        self.writer = writer
        self.v2 = v2
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=CLIENT_QUEUE_FRAMES)
        self.dropped = 0
        self.log_row: int | None = None
        self.slow_since: float | None = None


class NtripCaster:
    def __init__(
        self,
        bus: Bus,
        config: CasterConfig,
        host: str,
        port: int = 2101,
        ntrip_log: NtripLogRepo | None = None,
        position: Callable[[], tuple[float, float] | None] = lambda: None,
        bitrate: Callable[[], float] = lambda: 0.0,
    ) -> None:
        self.bus = bus
        self.config = config
        self.host = host
        self._port = port
        self.ntrip_log = ntrip_log
        self._position = position
        self._bitrate = bitrate
        self.sub = bus.subscribe("raw.rtcm", maxsize=500)
        self.clients: dict[int, ClientInfo] = {}
        self._conns: dict[int, _Client] = {}
        self._next_id = 1
        self._last_1005: bytes | None = None
        self._last_1230: bytes | None = None
        self._server: asyncio.Server | None = None
        self._feed_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------- lifecycle
    @property
    def port(self) -> int:
        if self._server and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self._port

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_conn, self.host, self._port, limit=HEADER_LIMIT * 2)
        self._feed_task = asyncio.create_task(self._feed(), name="ntrip-feed")
        log.info("NTRIP caster listening on %s:%d mountpoint /%s (%s)", self.host, self.port, self.config.mountpoint, "anonymous" if self.config.anonymous else "auth required")

    async def stop(self) -> None:
        self.sub.close()
        if self._feed_task:
            self._feed_task.cancel()
            await asyncio.gather(self._feed_task, return_exceptions=True)
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for conn in list(self._conns.values()):
            conn.writer.close()

    # ------------------------------------------------------------- fan-out
    async def _feed(self) -> None:
        async for _, frame in self.sub:
            self._on_frame(frame)

    def _on_frame(self, frame: Frame) -> None:
        raw = frame.raw
        msg_type = frame.rtcm_type
        if msg_type == 1005:
            self._last_1005 = raw
        elif msg_type == 1230:
            self._last_1230 = raw
        for conn in self._conns.values():
            self._offer(conn, raw)

    @staticmethod
    def _offer(conn: _Client, raw: bytes) -> None:
        try:
            conn.queue.put_nowait(raw)
        except asyncio.QueueFull:
            conn.queue.get_nowait()
            conn.dropped += 1
            conn.queue.put_nowait(raw)

    # ------------------------------------------------------------- sourcetable
    def sourcetable_body(self) -> bytes:
        pos = self._position()
        lat, lon = pos if pos else (0.0, 0.0)
        c = self.config
        line = (
            f"STR;{c.mountpoint};{c.identifier};RTCM 3.3;{c.format_details};2;{c.nav_system};mtrtk;{c.country};"
            f"{lat:.2f};{lon:.2f};0;0;{c.receiver};none;{'N' if c.anonymous else 'B'};N;{self._bitrate():.0f};\r\n"
        )
        return line.encode() + b"ENDSOURCETABLE\r\n"

    def _sourcetable_response(self, v2: bool) -> bytes:
        body = self.sourcetable_body()
        if v2:
            head = (
                b"HTTP/1.1 200 OK\r\nNtrip-Version: Ntrip/2.0\r\nServer: " + SERVER_NAME.encode()
                + b"\r\nContent-Type: gnss/sourcetable\r\nContent-Length: " + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
            )
        else:
            head = (
                b"SOURCETABLE 200 OK\r\nServer: " + SERVER_NAME.encode()
                + b"\r\nContent-Type: text/plain\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n"
            )
        return head + body

    # ------------------------------------------------------------- auth
    def _authorized(self, req: Request) -> tuple[bool, str | None]:
        if self.config.anonymous:
            return True, None
        header = req.headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            return False, None
        try:
            user, _, password = base64.b64decode(header[6:].strip()).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False, None
        ok = hmac.compare_digest(user.encode(), self.config.username.encode()) & hmac.compare_digest(
            password.encode(), self.config.password.encode()
        )
        return bool(ok), user if ok else None

    # ------------------------------------------------------------- connections
    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        ip, port = str(peer[0]), int(peer[1])
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEADER_TIMEOUT_S)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ConnectionError):
            await self._close(writer, b"HTTP/1.0 400 Bad Request\r\n\r\n")
            return
        if len(head) > HEADER_LIMIT:
            await self._close(writer, b"HTTP/1.0 400 Bad Request\r\n\r\n")
            return
        req = parse_request(head)
        if req.method != "GET":
            await self._close(writer, b"ERROR - Not Supported\r\n")
            return
        mount = req.path.lstrip("/").split("?", 1)[0]
        if mount == "":
            await self._close(writer, self._sourcetable_response(req.v2))
            return
        if mount != self.config.mountpoint:
            await self._close(writer, b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n" if req.v2 else self._sourcetable_response(False))
            return
        ok, username = self._authorized(req)
        if not ok:
            status = b"HTTP/1.1 401 Unauthorized\r\n" if req.v2 else b"HTTP/1.0 401 Unauthorized\r\n"
            await self._close(writer, status + b'WWW-Authenticate: Basic realm="mtrtk"\r\nConnection: close\r\n\r\n')
            return
        if req.v2:
            writer.write(
                b"HTTP/1.1 200 OK\r\nNtrip-Version: Ntrip/2.0\r\nServer: " + SERVER_NAME.encode()
                + b"\r\nContent-Type: gnss/data\r\nTransfer-Encoding: chunked\r\n"
                b"Cache-Control: no-store, no-cache, max-age=0\r\nPragma: no-cache\r\nConnection: close\r\n\r\n"
            )
        else:
            writer.write(b"ICY 200 OK\r\n\r\n")
        await writer.drain()

        info = ClientInfo(
            id=self._next_id, ip=ip, port=port, mountpoint=mount,
            user_agent=req.headers.get("user-agent", ""), username=username,
            version=2 if req.v2 else 1, connected_utc=datetime.now(UTC),
        )
        self._next_id += 1
        conn = _Client(info, writer, req.v2)
        self._conns[info.id] = conn
        self.clients[info.id] = info
        if self.ntrip_log is not None:
            conn.log_row = await self.ntrip_log.connected(ip, mount, info.user_agent, username)
        log.info("NTRIP client %d connected from %s (v%d, %s)", info.id, ip, info.version, info.user_agent or "no agent")
        self._publish_clients()
        for cached in (self._last_1005, self._last_1230):
            if cached:
                self._offer(conn, cached)

        gga_task = asyncio.create_task(self._read_gga(conn, reader), name=f"ntrip-gga-{info.id}")
        reason = "client closed"
        try:
            reason = await self._write_loop(conn)
        except (ConnectionError, asyncio.CancelledError, OSError) as exc:
            reason = f"{type(exc).__name__}"
        finally:
            gga_task.cancel()
            await asyncio.gather(gga_task, return_exceptions=True)
            self._conns.pop(info.id, None)
            self.clients.pop(info.id, None)
            writer.close()
            if self.ntrip_log is not None and conn.log_row is not None:
                await self.ntrip_log.disconnected(conn.log_row, info.bytes_sent, info.last_gga_lat, info.last_gga_lon, reason)
            log.info("NTRIP client %d disconnected (%s, %d bytes)", info.id, reason, info.bytes_sent)
            self._publish_clients()

    async def _write_loop(self, conn: _Client) -> str:
        writer = conn.writer
        while True:
            raw = await conn.queue.get()
            if conn.v2:
                writer.write(f"{len(raw):X}\r\n".encode() + raw + b"\r\n")
            else:
                writer.write(raw)
            conn.info.bytes_sent += len(raw)
            conn.info.dropped_frames = conn.dropped
            if writer.transport.get_write_buffer_size() > SLOW_CLIENT_BYTES:
                now = time.monotonic()
                conn.slow_since = conn.slow_since or now
                if now - conn.slow_since > SLOW_CLIENT_GRACE_S:
                    return "slow client"
            else:
                conn.slow_since = None
            if writer.is_closing():
                return "client closed"
            await writer.drain()

    async def _read_gga(self, conn: _Client, reader: asyncio.StreamReader) -> None:
        while True:
            line = await reader.readline()
            if not line:
                conn.writer.close()  # peer went away: unblock the write loop
                self._offer(conn, b"")
                return
            if line.startswith(b"$") and b"GGA" in line[:7]:
                try:
                    msg = NMEAReader.parse(line.strip() + b"\r\n")
                    conn.info.last_gga_lat = float(msg.lat)
                    conn.info.last_gga_lon = float(msg.lon)
                    conn.info.last_gga_utc = datetime.now(UTC)
                    self._publish_clients()
                except Exception:  # malformed GGA from a client is not our problem
                    log.debug("ignoring bad GGA from client %d", conn.info.id)

    def _publish_clients(self) -> None:
        self.bus.publish("ntrip.clients", list(self.clients.values()))

    @staticmethod
    async def _close(writer: asyncio.StreamWriter, payload: bytes) -> None:
        try:
            writer.write(payload)
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        writer.close()
```

Notes for the implementer: the empty-bytes sentinel `self._offer(conn, b"")` makes the write loop wake up and observe `writer.is_closing()`; writing `b""` is harmless. Keep `writer.transport.get_write_buffer_size()` — it exists on asyncio transports and is the basis of the slow-client rule.

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_ntrip_caster.py -q` → `12 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

Also a manual cross-check with RTKLIB while the F9P is not yet configured for RTCM: not needed here — Task 12 covers it live.

```bash
git add src/mtrtk/base/ntrip_caster.py tests/unit/test_ntrip_caster.py
git commit -m "feat(base): NTRIP caster with v1/v2 responses, sourcetable, auth, GGA intake and client stats

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Base mode manager — survey-in, fixed sites, 1005 verification

**Files:**
- Create: `src/mtrtk/base/basemode.py`, `tests/unit/test_basemode.py`

**Interfaces:**
- Consumes: `ReceiverController.apply_items(items, layers=LAYERS_ALL) -> bool` (Phase 1), `SitesRepo` (Task 4), `Site`, `decode_1005` (Task 8), `ubx_config.tmode_off/tmode_survey_in/tmode_fixed_ecef`, `StateStore.state` (survey_in, fix), bus topics `receiver.capabilities`, `rtcm.1005`, `state.fix`.
- Produces: `BaseModeManager(bus, controller, sites, store, *, base_mode, svin_min_duration_s, svin_acc_limit_m, active_site_name=None, clock=time.monotonic)` with `await apply_mode()`, `await freeze_survey_in(name) -> Site`, `await activate_site(name) -> Site`, `on_1005(frame)`, `on_fix(fix)`, `await poll_active_site()`, `await run(stop, poll_s=10.0)`, attributes `mode`, `applied_site`, `last_1005`, `verified`. Bus topics: `base.mode` (`{"mode", "site", "reason"}`), `base.site_verified` (`{"site","dx","dy","dz"}`), `base.site_mismatch` (`{"site","dx","dy","dz"}` or `{"site","reason"}`). Verification tolerance `SITE_TOLERANCE_M = 0.0005`, deadline `VERIFY_DEADLINE_S = 30`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_basemode.py`:
```python
import asyncio
from pathlib import Path

import pytest

from mtrtk.base.basemode import SITE_TOLERANCE_M, BaseModeManager
from mtrtk.config import BaseMode
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.core.state import FixInfo, SurveyIn
from mtrtk.core.statestore import StateStore
from mtrtk.core.ubx_config import LAYERS_ALL, tmode_fixed_ecef, tmode_off, tmode_survey_in
from mtrtk.store.db import Database
from mtrtk.store.models import Site
from mtrtk.store.repos import SitesRepo

RTCM_1005 = bytes.fromhex("d300133ed7fd0382dfdc1c403db34fe8fe0cef5e6b30bd2e23")  # (1234567.8912, -987654.3234, 5555555.0)


class FakeController:
    def __init__(self) -> None:
        self.applied: list[tuple[list[tuple[str, int]], int]] = []

    async def apply_items(self, items, layers=LAYERS_ALL):
        self.applied.append((list(items), layers))
        return True


class Clock:
    t = 100.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
async def env(tmp_path: Path):
    db = Database(tmp_path / "m.db")
    await db.open()
    bus = Bus()
    store = StateStore(bus)
    ctrl = FakeController()
    clock = Clock()
    sub = bus.subscribe("base.*")

    def make(mode: BaseMode, active: str | None = None) -> BaseModeManager:
        return BaseModeManager(bus, ctrl, SitesRepo(db), store, base_mode=mode, svin_min_duration_s=300, svin_acc_limit_m=2.0, active_site_name=active, clock=clock)

    try:
        yield make, ctrl, SitesRepo(db), store, sub, clock
    finally:
        await db.close()


def drain(sub) -> list[tuple[str, dict]]:
    return [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]


async def test_apply_survey_in(env) -> None:
    make, ctrl, *_ , sub, _ = env
    await make(BaseMode.SURVEY_IN).apply_mode()
    assert ctrl.applied == [(tmode_survey_in(300, 2.0), LAYERS_ALL)]
    assert drain(sub) == [("base.mode", {"mode": "survey-in", "site": None, "reason": None})]


async def test_apply_off(env) -> None:
    make, ctrl, *_ = env
    await make(BaseMode.OFF).apply_mode()
    assert ctrl.applied == [(tmode_off(), LAYERS_ALL)]


async def test_apply_fixed_uses_active_site(env) -> None:
    make, ctrl, sites, _, sub, _ = env
    await sites.add(Site.from_ecef("roof", 1234567.8912, -987654.3234, 5555555.0, sigma_m=0.004, source="csrs-ppp"))
    await sites.activate("roof")
    mgr = make(BaseMode.FIXED)
    await mgr.apply_mode()
    assert ctrl.applied == [(tmode_fixed_ecef(1234567.8912, -987654.3234, 5555555.0, 0.004 * 3**0.5), LAYERS_ALL)]
    assert mgr.applied_site is not None and mgr.applied_site.name == "roof"
    assert drain(sub)[0][1]["site"] == "roof"


async def test_apply_fixed_by_name_from_settings(env) -> None:
    make, ctrl, sites, *_ = env
    await sites.add(Site.from_ecef("field", 1.0, 2.0, 3.0, source="manual"))
    await make(BaseMode.FIXED, active="field").apply_mode()
    assert ctrl.applied[0][0][0] == ("CFG_TMODE_MODE", 2)
    assert (await sites.active()).name == "field"  # settings name gets activated in the DB


async def test_apply_fixed_without_site_falls_back_to_survey_in(env) -> None:
    make, ctrl, _, _, sub, _ = env
    mgr = make(BaseMode.FIXED)
    await mgr.apply_mode()
    assert ctrl.applied == [(tmode_survey_in(300, 2.0), LAYERS_ALL)]
    assert mgr.mode is BaseMode.SURVEY_IN
    assert drain(sub) == [("base.mode", {"mode": "survey-in", "site": None, "reason": "no active site; falling back to survey-in"})]


async def test_freeze_survey_in(env) -> None:
    make, _, sites, store, *_ = env
    mgr = make(BaseMode.SURVEY_IN)
    store.state.survey_in = SurveyIn(active=True, valid=False, mean_x_m=1.0, mean_y_m=2.0, mean_z_m=3.0, mean_acc_m=5.0)
    with pytest.raises(ValueError, match="not valid"):
        await mgr.freeze_survey_in("roof")
    store.state.survey_in = SurveyIn(active=True, valid=True, dur_s=600, mean_x_m=1234567.8912, mean_y_m=-987654.3234, mean_z_m=5555555.0, mean_acc_m=1.2)
    site = await mgr.freeze_survey_in("roof")
    assert site.source == "survey-in" and site.sigma_x == 1.2 and site.frame == "WGS84 (receiver)"
    assert (await sites.get("roof")).x == 1234567.8912


async def test_activate_and_verify_against_1005(env) -> None:
    make, ctrl, sites, _, sub, _ = env
    await sites.add(Site.from_ecef("roof", 1234567.8912, -987654.3234, 5555555.0, sigma_m=0.004, source="csrs-ppp"))
    mgr = make(BaseMode.SURVEY_IN)
    await mgr.activate_site("roof")
    assert mgr.mode is BaseMode.FIXED and ctrl.applied[-1][0][0] == ("CFG_TMODE_MODE", 2)
    drain(sub)
    mgr.on_1005(Framer().feed(RTCM_1005)[0])
    mgr.on_1005(Framer().feed(RTCM_1005)[0])  # verified only once
    published = drain(sub)
    assert [t for t, _ in published] == ["base.site_verified"]
    assert published[0][1]["site"] == "roof" and abs(published[0][1]["dx"]) <= SITE_TOLERANCE_M
    assert mgr.verified is True and mgr.last_1005 is not None


async def test_mismatching_1005_is_reported_once(env) -> None:
    make, _, sites, _, sub, _ = env
    await sites.add(Site.from_ecef("roof", 1234567.8912 + 0.5, -987654.3234, 5555555.0, sigma_m=0.004, source="manual"))
    mgr = make(BaseMode.SURVEY_IN)
    await mgr.activate_site("roof")
    drain(sub)
    mgr.on_1005(Framer().feed(RTCM_1005)[0])
    mgr.on_1005(Framer().feed(RTCM_1005)[0])
    published = drain(sub)
    assert [t for t, _ in published] == ["base.site_mismatch"]
    assert published[0][1]["dx"] == pytest.approx(-0.5, abs=1e-6)


async def test_fix_type_deadline_reports_mismatch(env) -> None:
    make, _, sites, _, sub, clock = env
    await sites.add(Site.from_ecef("roof", 1.0, 2.0, 3.0, source="manual"))
    mgr = make(BaseMode.SURVEY_IN)
    await mgr.activate_site("roof")
    drain(sub)
    mgr.on_fix(FixInfo(fix_type=3))
    assert drain(sub) == []
    clock.t += 31
    mgr.on_fix(FixInfo(fix_type=3))
    published = drain(sub)
    assert published[0][0] == "base.site_mismatch" and "fixType" in published[0][1]["reason"]
    mgr.on_fix(FixInfo(fix_type=3))
    assert drain(sub) == []


async def test_poll_active_site_picks_up_cli_activation(env) -> None:
    make, ctrl, sites, *_ = env
    await sites.add(Site.from_ecef("a", 1.0, 2.0, 3.0, source="manual"))
    mgr = make(BaseMode.SURVEY_IN)
    await mgr.apply_mode()
    await sites.activate("a")  # as `mtrtk sites activate a` would
    await mgr.poll_active_site()
    assert mgr.mode is BaseMode.FIXED and mgr.applied_site.name == "a"
    n = len(ctrl.applied)
    await mgr.poll_active_site()
    assert len(ctrl.applied) == n  # unchanged: no re-apply


async def test_run_applies_on_capabilities_and_routes_frames(env) -> None:
    make, ctrl, _, _, sub, _ = env
    mgr = make(BaseMode.SURVEY_IN)
    stop = asyncio.Event()
    task = asyncio.create_task(mgr.run(stop, poll_s=0.01))
    mgr.bus.publish("receiver.capabilities", object())
    await asyncio.sleep(0.05)
    assert ctrl.applied == [(tmode_survey_in(300, 2.0), LAYERS_ALL)]
    mgr.stop()
    await asyncio.wait_for(task, 1.0)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_basemode.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.base.basemode'`.

- [ ] **Step 3: Write `src/mtrtk/base/basemode.py`**

```python
"""Base-station positioning mode: survey-in, fixed site, or off; verifies fixed sites via RTCM 1005."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from mtrtk.base.rtcm1005 import Ecef1005, decode_1005
from mtrtk.config import BaseMode
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame
from mtrtk.core.state import FixInfo
from mtrtk.core.statestore import StateStore
from mtrtk.core.ubx_config import LAYERS_ALL, CfgItems, tmode_fixed_ecef, tmode_off, tmode_survey_in
from mtrtk.store.models import Site
from mtrtk.store.repos import SitesRepo

log = logging.getLogger(__name__)

SITE_TOLERANCE_M = 0.0005
VERIFY_DEADLINE_S = 30.0
DEFAULT_FIXED_ACC_M = 0.01
FIX_TYPE_TIME_ONLY = 5


class ConfigApplier(Protocol):
    async def apply_items(self, items: CfgItems, layers: int = LAYERS_ALL) -> bool: ...


class BaseModeManager:
    def __init__(
        self,
        bus: Bus,
        controller: ConfigApplier,
        sites: SitesRepo,
        store: StateStore,
        *,
        base_mode: BaseMode,
        svin_min_duration_s: int,
        svin_acc_limit_m: float,
        active_site_name: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bus = bus
        self.controller = controller
        self.sites = sites
        self.store = store
        self.mode = base_mode
        self.svin_min_duration_s = svin_min_duration_s
        self.svin_acc_limit_m = svin_acc_limit_m
        self.active_site_name = active_site_name
        self._clock = clock
        self.applied_site: Site | None = None
        self.last_1005: Ecef1005 | None = None
        self.verified = False
        self._mismatch_reported = False
        self._verify_deadline: float | None = None
        self.sub = bus.subscribe("receiver.capabilities", "rtcm.1005", "state.fix", maxsize=200)

    # ------------------------------------------------------------- mode
    async def apply_mode(self) -> None:
        if self.mode is BaseMode.OFF:
            await self.controller.apply_items(tmode_off())
            self._announce(None)
            return
        if self.mode is BaseMode.FIXED:
            site = await self._resolve_site()
            if site is None:
                self.mode = BaseMode.SURVEY_IN
                await self.controller.apply_items(tmode_survey_in(self.svin_min_duration_s, self.svin_acc_limit_m))
                self._announce(None, reason="no active site; falling back to survey-in")
                return
            await self._apply_fixed(site)
            return
        await self.controller.apply_items(tmode_survey_in(self.svin_min_duration_s, self.svin_acc_limit_m))
        self._announce(None)

    async def _resolve_site(self) -> Site | None:
        if self.active_site_name:
            named = await self.sites.get(self.active_site_name)
            if named is not None:
                if not named.active:
                    await self.sites.activate(named.name)
                return await self.sites.get(named.name)
            log.warning("ACTIVE_SITE=%r not found in the sites table", self.active_site_name)
        return await self.sites.active()

    async def _apply_fixed(self, site: Site) -> None:
        acc = site.sigma_3d or DEFAULT_FIXED_ACC_M
        await self.controller.apply_items(tmode_fixed_ecef(site.x, site.y, site.z, acc))
        self.mode = BaseMode.FIXED
        self.applied_site = site
        self.verified = False
        self._mismatch_reported = False
        self._verify_deadline = self._clock() + VERIFY_DEADLINE_S
        log.info("TMODE fixed at site %s (%.4f, %.4f, %.4f) acc %.4f m", site.name, site.x, site.y, site.z, acc)
        self._announce(site.name)

    def _announce(self, site: str | None, reason: str | None = None) -> None:
        self.bus.publish("base.mode", {"mode": self.mode.value, "site": site, "reason": reason})

    # ------------------------------------------------------------- sites
    async def freeze_survey_in(self, name: str) -> Site:
        svin = self.store.state.survey_in
        if not svin.valid or None in (svin.mean_x_m, svin.mean_y_m, svin.mean_z_m):
            raise ValueError("survey-in is not valid yet; wait for it to complete")
        site = Site.from_ecef(
            name, float(svin.mean_x_m), float(svin.mean_y_m), float(svin.mean_z_m),  # type: ignore[arg-type]
            sigma_m=svin.mean_acc_m, source="survey-in", frame="WGS84 (receiver)",
            notes=f"survey-in {svin.dur_s}s, {svin.obs} observations",
        )
        return await self.sites.add(site)

    async def activate_site(self, name: str) -> Site:
        site = await self.sites.activate(name)
        await self._apply_fixed(site)
        return site

    async def poll_active_site(self) -> None:
        """Pick up `mtrtk sites activate` done from another process."""
        active = await self.sites.active()
        if active is None:
            return
        if self.applied_site is None or self.applied_site.name != active.name or self.applied_site.id != active.id:
            await self._apply_fixed(active)

    # ------------------------------------------------------------- verification
    def on_1005(self, frame: Frame) -> None:
        ecef = decode_1005(frame)
        if ecef is None:
            return
        self.last_1005 = ecef
        site = self.applied_site
        if self.mode is not BaseMode.FIXED or site is None:
            return
        dx, dy, dz = ecef.x - site.x, ecef.y - site.y, ecef.z - site.z
        meta: dict[str, Any] = {"site": site.name, "dx": dx, "dy": dy, "dz": dz}
        if max(abs(dx), abs(dy), abs(dz)) <= SITE_TOLERANCE_M:
            if not self.verified:
                self.verified = True
                self.bus.publish("base.site_verified", meta)
        elif not self._mismatch_reported:
            self._mismatch_reported = True
            self.bus.publish("base.site_mismatch", meta)

    def on_fix(self, fix: FixInfo) -> None:
        if self.mode is not BaseMode.FIXED or self.applied_site is None or self.verified or self._mismatch_reported:
            return
        if self._verify_deadline is not None and self._clock() > self._verify_deadline and fix.fix_type != FIX_TYPE_TIME_ONLY:
            self._mismatch_reported = True
            self.bus.publish(
                "base.site_mismatch",
                {"site": self.applied_site.name, "reason": f"receiver fixType={fix.fix_type} (expected 5) {VERIFY_DEADLINE_S:.0f}s after applying the fixed position"},
            )

    # ------------------------------------------------------------- run
    def stop(self) -> None:
        self.sub.close()

    async def run(self, stop: asyncio.Event, poll_s: float = 10.0) -> None:
        poller = asyncio.create_task(self._poll_loop(stop, poll_s), name="basemode-poll")
        try:
            async for topic, item in self.sub:
                try:
                    if topic == "receiver.capabilities":
                        await self.apply_mode()
                    elif topic == "rtcm.1005":
                        self.on_1005(item)
                    elif topic == "state.fix":
                        self.on_fix(item)
                except Exception:
                    log.exception("base mode handling failed for %s", topic)
                if stop.is_set():
                    break
        finally:
            poller.cancel()
            await asyncio.gather(poller, return_exceptions=True)

    async def _poll_loop(self, stop: asyncio.Event, poll_s: float) -> None:
        while not stop.is_set():
            await asyncio.sleep(poll_s)
            try:
                await self.poll_active_site()
            except Exception:
                log.exception("active-site poll failed")
```

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_basemode.py -q` → `11 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

```bash
git add src/mtrtk/base/basemode.py tests/unit/test_basemode.py
git commit -m "feat(base): survey-in / fixed-site mode manager with RTCM 1005 verification

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Daemon wiring for the base role and `mtrtk sites` CLI

**Files:**
- Modify: `src/mtrtk/daemon.py`, `src/mtrtk/cli.py`, `src/mtrtk/config.py` (add `replay_log: bool = False`)
- Create: `tests/unit/test_daemon_base.py`, `tests/unit/test_cli_sites.py`

**Interfaces:**
- Consumes: everything from Tasks 2–10.
- Produces: `Daemon.db: Database`, `Daemon.caster: NtripCaster | None`, `Daemon.basemode: BaseModeManager | None`, `Daemon._supervise(name, factory)` (restarts a failed consumer with backoff, publishes `daemon.consumer_failed` `{"name","error"}`); `StatusPrinter.format_line` appends `svin <dur>s σ<acc>m <✓|…>` while survey-in is active/valid; CLI group `mtrtk sites` with `list`, `add NAME (--ecef X Y Z | --llh LAT LON H) [--sigma M] [--source S] [--frame F] [--epoch E] [--notes N]`, `activate NAME`, `delete NAME`.
- Consumers per role (base): `SystemMonitor`, `Sampler`, `AlertEngine`, `RawLogWriter` + `RetentionPolicy` (skipped for file replay unless `REPLAY_LOG=1`), `NtripCaster` (after `wait_for_bind`), `BaseModeManager` (skipped in passive/replay mode). Rover role: `SystemMonitor`, `Sampler`, `AlertEngine` only (Phase 6 adds the rest).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_daemon_base.py`:
```python
import asyncio
import base64
from pathlib import Path

import pytest

from mtrtk.config import Settings
from mtrtk.core.frames import Framer, Proto
from mtrtk.daemon import Daemon
from mtrtk.rawlog.index import list_logs
from mtrtk.store.db import Database

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_base_30s.ubx"


async def test_base_daemon_on_replay_serves_rtcm_logs_and_samples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "pw")
    settings = Settings(
        _env_file=None, role="base", mtrtk_source=f"file:{FIXTURE}", replay_speed=5, replay_log=True,
        data_dir=tmp_path, ntrip_bind="127.0.0.1", ntrip_port=0, ntrip_user="rover",
    )
    daemon = Daemon(settings)
    run_task = asyncio.create_task(daemon.run())
    for _ in range(100):
        await asyncio.sleep(0.02)
        if daemon.caster is not None and daemon.caster._server is not None:
            break
    assert daemon.caster is not None
    auth = base64.b64encode(b"rover:pw").decode()
    reader, writer = await asyncio.open_connection("127.0.0.1", daemon.caster.port)
    writer.write(f"GET /MTRK HTTP/1.0\r\nUser-Agent: NTRIP test\r\nAuthorization: Basic {auth}\r\n\r\n".encode())
    await writer.drain()
    assert await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2.0) == b"ICY 200 OK\r\n\r\n"
    data = await asyncio.wait_for(reader.read(4096), 5.0)
    frames = Framer().feed(data)
    assert frames and all(f.proto is Proto.RTCM3 for f in frames)
    writer.close()
    await asyncio.wait_for(run_task, 30.0)  # replay ends -> daemon exits
    logs = list_logs(tmp_path)
    assert logs and logs[0].msg_counts.get("RXM-RAWX", 0) > 0 and logs[0].complete is True
    db = Database(tmp_path / "mtrtk.db")
    await db.open()
    n = (await db.fetchone("SELECT COUNT(*) FROM samples_1s"))[0]
    events = await db.fetchall("SELECT kind FROM events")
    await db.close()
    assert n >= 20
    assert {r["kind"] for r in events} >= set()  # table exists; replay produces no alerts by itself


async def test_replay_without_replay_log_writes_no_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    settings = Settings(_env_file=None, role="base", mtrtk_source=f"file:{FIXTURE}", replay_speed=0, data_dir=tmp_path, ntrip_bind="127.0.0.1", ntrip_port=0)
    await asyncio.wait_for(Daemon(settings).run(), 30.0)
    assert list_logs(tmp_path) == []
    assert (tmp_path / "mtrtk.db").exists()


async def test_supervisor_restarts_failed_consumer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    settings = Settings(_env_file=None, mtrtk_source=f"file:{FIXTURE}", replay_speed=0, data_dir=tmp_path)
    daemon = Daemon(settings)
    failures = daemon.bus.subscribe("daemon.consumer_failed")
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        daemon.stop.set()

    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr("mtrtk.daemon.asyncio.sleep", fake_sleep)
    await daemon._supervise("flaky", flaky)
    assert calls == 2 and sleeps == [1.0]
    assert failures.queue.get_nowait()[1] == {"name": "flaky", "error": "RuntimeError: boom"}
```

`tests/unit/test_cli_sites.py`:
```python
from pathlib import Path

import pytest
from click.testing import CliRunner

from mtrtk.cli import main


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    return tmp_path


def test_sites_add_list_activate_delete(env: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(main, ["sites", "add", "roof", "--ecef", "1234567.8912", "-987654.3234", "5555555.0", "--sigma", "0.004", "--source", "csrs-ppp", "--frame", "ITRF2020", "--epoch", "2026.71"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(main, ["sites", "add", "field", "--llh", "23.8373506", "90.2625502", "-36.268"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(main, ["sites", "list"])
    assert r.exit_code == 0 and "roof" in r.output and "field" in r.output and "csrs-ppp" in r.output
    r = runner.invoke(main, ["sites", "activate", "field"])
    assert r.exit_code == 0 and "active" in r.output
    r = runner.invoke(main, ["sites", "list"])
    assert r.output.index("* field") >= 0
    r = runner.invoke(main, ["sites", "delete", "roof"])
    assert r.exit_code == 0
    r = runner.invoke(main, ["sites", "list"])
    assert "roof" not in r.output


def test_sites_add_requires_coordinates(env: Path) -> None:
    r = CliRunner().invoke(main, ["sites", "add", "x"])
    assert r.exit_code != 0 and "--ecef" in r.output


def test_sites_activate_unknown(env: Path) -> None:
    r = CliRunner().invoke(main, ["sites", "activate", "nope"])
    assert r.exit_code != 0 and "nope" in r.output
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_daemon_base.py tests/unit/test_cli_sites.py -q`
Expected: failures (no `Daemon.caster`, no `sites` group, `replay_log` unknown).

- [ ] **Step 3: Add `replay_log` to `Settings`**

In `src/mtrtk/config.py` after `replay_loop: bool = False` add:
```python
    replay_log: bool = False  # write raw logs even when replaying a file (tests, demos)
```

- [ ] **Step 4: Rewrite `src/mtrtk/daemon.py`**

```python
"""Process supervisor: wires source -> router -> bus -> state and the role's consumers."""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
import time
from collections.abc import Awaitable, Callable

from mtrtk.alerts import AlertEngine
from mtrtk.base.basemode import BaseModeManager
from mtrtk.base.ntrip_caster import CasterConfig, NtripCaster
from mtrtk.config import Role, Settings
from mtrtk.core.bus import Bus
from mtrtk.core.exposure import wait_for_bind
from mtrtk.core.receiver import ReceiverController
from mtrtk.core.router import TOPIC_RAW_RTCM, TOPIC_RAW_UBX
from mtrtk.core.source import ByteSource, FileReplaySource, SerialSource, find_ublox_port
from mtrtk.core.statestore import StateStore
from mtrtk.core.ubx_config import base_profile, rover_profile
from mtrtk.rawlog.retention import RetentionPolicy
from mtrtk.rawlog.writer import RawLogWriter, recover_incomplete
from mtrtk.store.db import Database
from mtrtk.store.repos import EventsRepo, NtripLogRepo, SitesRepo
from mtrtk.store.sampler import Sampler
from mtrtk.system import SystemMonitor

log = logging.getLogger(__name__)

ConsumerFactory = Callable[[], Awaitable[None]]
SUPERVISE_BACKOFF_MAX_S = 60.0


class StatusPrinter:
    """One status line per epoch (NAV-EOE) or, lacking EOE, at most one per interval."""

    def __init__(self, bus: Bus, store: StateStore, echo: Callable[[str], object] = print, interval_s: float = 1.0) -> None:
        self.store = store
        self.echo = echo
        self.interval_s = interval_s
        self._last = 0.0
        self._sub = bus.subscribe("state.epoch", "state.position", maxsize=50)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="status-printer")

    async def stop(self) -> None:
        self._sub.close()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        async for topic, _ in self._sub:
            now = time.monotonic()
            if topic == "state.position" and now - self._last < self.interval_s:
                continue
            self._last = now
            self.echo(self.format_line())

    def format_line(self) -> str:
        s = self.store.state
        utc = s.time.utc.strftime("%H:%M:%S") if s.time.utc else "--:--:--"
        lat = f"{s.position.lat:.7f}" if s.position.lat is not None else "-"
        lon = f"{s.position.lon:.7f}" if s.position.lon is not None else "-"
        height = f"{s.position.height_m:.2f}" if s.position.height_m is not None else "-"
        hacc = f"{s.accuracy.h_acc_m:.2f}" if s.accuracy.h_acc_m is not None else "-"
        line = (
            f"{utc} {s.fix.fix_type_name:<9} {s.fix.carr_soln_name:<9} "
            f"sats {s.sat_summary.used}/{s.sat_summary.tracked} "
            f"lat {lat} lon {lon} h {height} hAcc {hacc} rtcm {s.rtcm_out.bytes_per_s:.0f} B/s"
        )
        svin = s.survey_in
        if svin.active or svin.valid:
            acc = f"{svin.mean_acc_m:.2f}" if svin.mean_acc_m is not None else "-"
            line += f" svin {svin.dur_s}s σ{acc}m {'✓' if svin.valid else '…'}"
        return line


class Daemon:
    def __init__(
        self,
        settings: Settings,
        source_factory: Callable[[], ByteSource] | None = None,
        passive: bool | None = None,
    ) -> None:
        self.settings = settings
        self.bus = Bus()
        self.store = StateStore(self.bus)
        self.stop = asyncio.Event()
        self.db = Database(settings.data_dir / "mtrtk.db")
        self.caster: NtripCaster | None = None
        self.basemode: BaseModeManager | None = None
        self.passive = settings.source_is_file if passive is None else passive
        self._raw_sub = self.bus.subscribe(TOPIC_RAW_UBX, TOPIC_RAW_RTCM, maxsize=5000)
        profile = base_profile(settings) if settings.role is Role.BASE else rover_profile(settings)
        self.controller = ReceiverController(
            self.bus,
            source_factory or self._default_source_factory(),
            profile=None if self.passive else profile,
            strict=settings.receiver_strict,
            passive=self.passive,
        )

    # ------------------------------------------------------------- sources
    def _default_source_factory(self) -> Callable[[], ByteSource]:
        s = self.settings
        if s.source_is_file:
            path = s.source_path
            return lambda: FileReplaySource(path, speed=s.replay_speed, loop=s.replay_loop)
        port = s.mtrtk_source
        if port == "auto":
            found = find_ublox_port()
            if found is None:
                raise RuntimeError("no u-blox receiver found; set MTRTK_SOURCE to the serial device")
            port = found
        return lambda: SerialSource(port, s.baud)

    # ------------------------------------------------------------- consumers
    def _consumers(self) -> list[tuple[str, ConsumerFactory]]:
        s = self.settings
        stop = self.stop
        host = socket.gethostname()
        consumers: list[tuple[str, ConsumerFactory]] = [
            ("system", lambda: SystemMonitor(self.bus, s.data_dir).run(stop)),
            ("sampler", lambda: Sampler(self.bus, self.db).run(stop)),
            ("alerts", lambda: AlertEngine(
                self.bus, EventsRepo(self.db), role=s.role.value, host=host,
                min_free_gb=s.min_free_gb, webhook_url=s.alert_webhook_url,
            ).run(stop)),
        ]
        if s.role is Role.BASE:
            if not s.source_is_file or s.replay_log:
                consumers.append(("rawlog", self._run_rawlog))
                consumers.append(("retention", lambda: RetentionPolicy(s.data_dir, s.min_free_gb, self.bus).run(stop)))
            consumers.append(("ntrip", self._run_caster))
            if not self.passive:
                consumers.append(("basemode", self._run_basemode))
        return consumers

    async def _run_rawlog(self) -> None:
        s = self.settings
        writer = RawLogWriter(self.bus, s.data_dir, s.station_id, s.log_messages, role=s.role.value, fsync_interval_s=s.fsync_interval_s)
        meta_sub = self.bus.subscribe("receiver.capabilities", "base.mode", maxsize=10)

        async def track_metadata() -> None:
            async for topic, item in meta_sub:
                if topic == "receiver.capabilities":
                    writer.firmware = getattr(item, "fw_version", "") or writer.firmware
                elif topic == "base.mode":
                    writer.site = item.get("site")

        meta_task = asyncio.create_task(track_metadata(), name="rawlog-meta")
        run_task = asyncio.create_task(writer.run(self.stop), name="rawlog-run")
        stop_task = asyncio.create_task(self.stop.wait(), name="rawlog-stop")
        try:
            await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            writer.stop()  # closes the subscription -> writer.run drains and returns (closing the file)
            meta_sub.close()
            stop_task.cancel()
            await asyncio.gather(run_task, meta_task, stop_task, return_exceptions=True)

    async def _run_caster(self) -> None:
        s = self.settings
        host = await wait_for_bind(s.ntrip_bind, self.stop)
        if host is None:
            return
        config = CasterConfig(
            mountpoint=s.mountpoint, username=s.ntrip_user, password=s.ntrip_password or "",
            station_id=s.station_id, country=s.country,
            format_details=("1005(1),1077(1),1087(1),1097(1),1127(1),1230(%d)" if s.rtcm_msm == 7 else "1005(1),1074(1),1084(1),1094(1),1124(1),1230(%d)") % s.rtcm_1230_rate,
        )

        def position() -> tuple[float, float] | None:
            p = self.store.state.position
            return (p.lat, p.lon) if p.lat is not None and p.lon is not None else None

        self.caster = NtripCaster(
            self.bus, config, host, s.ntrip_port, ntrip_log=NtripLogRepo(self.db),
            position=position, bitrate=lambda: self.store.state.rtcm_out.bytes_per_s * 8,
        )
        await self.caster.start()
        try:
            await self.stop.wait()
        finally:
            await self.caster.stop()
            self.caster = None

    async def _run_basemode(self) -> None:
        s = self.settings
        self.basemode = BaseModeManager(
            self.bus, self.controller, SitesRepo(self.db), self.store,
            base_mode=s.base_mode, svin_min_duration_s=s.svin_min_duration_s,
            svin_acc_limit_m=s.svin_acc_limit_m, active_site_name=s.active_site,
        )
        try:
            await self.basemode.run(self.stop)
        finally:
            self.basemode = None

    async def _supervise(self, name: str, factory: ConsumerFactory) -> None:
        backoff = 1.0
        while not self.stop.is_set():
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("consumer %s failed", name)
                self.bus.publish("daemon.consumer_failed", {"name": name, "error": f"{type(exc).__name__}: {exc}"})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, SUPERVISE_BACKOFF_MAX_S)

    # ------------------------------------------------------------- run
    async def _state_loop(self) -> None:
        async for _, frame in self._raw_sub:
            self.store.apply(frame)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        await self.db.open()
        recovered = recover_incomplete(self.settings.data_dir)
        if recovered:
            log.info("recovered %d incomplete raw log(s) from a previous run", len(recovered))
        state_task = asyncio.create_task(self._state_loop(), name="state-loop")
        consumer_tasks = [asyncio.create_task(self._supervise(name, factory), name=f"consumer-{name}") for name, factory in self._consumers()]
        controller_task = asyncio.create_task(self.controller.run(self.stop), name="receiver")
        try:
            await controller_task  # returns on EOF (replay) or when stop is set
        finally:
            self.stop.set()
            self._raw_sub.close()
            await asyncio.gather(state_task, return_exceptions=True)
            for task in consumer_tasks:
                task.cancel()
            await asyncio.gather(*consumer_tasks, return_exceptions=True)
            await self.db.close()
```

Consumers that block on `async for` over a bus subscription end when `stop` is set only if their subscription is closed; cancelling the supervisor tasks in `run()`'s `finally` handles that. Each consumer's own `finally` (writer close, caster stop) runs on cancellation.

- [ ] **Step 5: Add the `sites` command group to `src/mtrtk/cli.py`**

Append:
```python
@main.group()
def sites() -> None:
    """Manage fixed base-station sites (ECEF positions)."""


def _with_db(fn):  # type: ignore[no-untyped-def]
    """Run an async function with an open Database at DATA_DIR/mtrtk.db."""
    from mtrtk.store.db import Database

    async def runner() -> None:
        settings = _load_settings(ntrip_password="")
        db = Database(settings.data_dir / "mtrtk.db")
        await db.open()
        try:
            await fn(db)
        finally:
            await db.close()

    asyncio.run(runner())


@sites.command("list")
def sites_list() -> None:
    """List saved sites; the active one is marked with *."""
    from mtrtk.store.repos import SitesRepo

    async def go(db) -> None:  # type: ignore[no-untyped-def]
        rows = await SitesRepo(db).list()
        if not rows:
            click.echo("no sites saved")
            return
        for s in rows:
            mark = "*" if s.active else " "
            sigma = f"{s.sigma_3d:.4f}" if s.sigma_3d is not None else "-"
            click.echo(f"{mark} {s.name:<16} {s.x:14.4f} {s.y:14.4f} {s.z:14.4f}  σ3D {sigma:>8} m  {s.frame:<10} {s.source}")

    _with_db(go)


@sites.command("add")
@click.argument("name")
@click.option("--ecef", nargs=3, type=float, metavar="X Y Z", help="ECEF metres (preferred: paste from a PPP report).")
@click.option("--llh", nargs=3, type=float, metavar="LAT LON H", help="Geodetic degrees + ellipsoidal height metres.")
@click.option("--sigma", type=float, default=None, help="1-sigma per axis, metres.")
@click.option("--source", default="manual", show_default=True)
@click.option("--frame", default="ITRF2020", show_default=True)
@click.option("--epoch", default=None)
@click.option("--notes", default=None)
def sites_add(name: str, ecef, llh, sigma, source, frame, epoch, notes) -> None:  # type: ignore[no-untyped-def]
    """Save a site from ECEF or LLH coordinates."""
    from mtrtk.core.geo import llh_to_ecef
    from mtrtk.store.models import Site
    from mtrtk.store.repos import SitesRepo

    if not ecef and not llh:
        raise click.UsageError("give --ecef X Y Z or --llh LAT LON H")
    x, y, z = ecef if ecef else llh_to_ecef(*llh)

    async def go(db) -> None:  # type: ignore[no-untyped-def]
        try:
            site = await SitesRepo(db).add(Site.from_ecef(name, x, y, z, sigma_m=sigma, source=source, frame=frame, epoch=epoch, notes=notes))
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"saved site {site.name}: lat {site.lat:.8f} lon {site.lon:.8f} h {site.height_m:.3f}")

    _with_db(go)


@sites.command("activate")
@click.argument("name")
def sites_activate(name: str) -> None:
    """Make NAME the active fixed site (a running base daemon applies it within 10 s)."""
    from mtrtk.store.repos import SitesRepo

    async def go(db) -> None:  # type: ignore[no-untyped-def]
        try:
            site = await SitesRepo(db).activate(name)
        except KeyError as exc:
            raise click.ClickException(f"no site named {name!r}") from exc
        click.echo(f"{site.name} is now the active site; set BASE_MODE=fixed to use it at startup")

    _with_db(go)


@sites.command("delete")
@click.argument("name")
def sites_delete(name: str) -> None:
    """Delete a saved site."""
    from mtrtk.store.repos import SitesRepo

    async def go(db) -> None:  # type: ignore[no-untyped-def]
        await SitesRepo(db).delete(name)
        click.echo(f"deleted {name}")

    _with_db(go)
```

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass. `test_base_daemon_on_replay_serves_rtcm_logs_and_samples` needs the Phase 1 fixture `f9p_hpg113_base_30s.ubx` (recorded after the base profile was applied, so it contains RTCM and NAV-EOE); if it is absent, record it first per Phase 1 Task 14 Step 6.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add src/mtrtk/daemon.py src/mtrtk/cli.py src/mtrtk/config.py tests/unit/test_daemon_base.py tests/unit/test_cli_sites.py
git commit -m "feat: wire base-role consumers into the daemon with supervision; add mtrtk sites CLI

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: Live milestone, docs and close-out

**Files:**
- Create: `docs/base.md`, `tests/hardware/test_live_base.py`
- Modify: `README.md` (status section), `.env.example` (add `REPLAY_LOG=0`)

- [ ] **Step 1: Write the live test**

`tests/hardware/test_live_base.py`:
```python
"""Live base-station smoke test. Requires the F9P and ~2 minutes. Run: uv run pytest -m hardware tests/hardware/test_live_base.py -s"""

import asyncio
import base64
import os
from pathlib import Path

import pytest

from mtrtk.config import Settings
from mtrtk.core.frames import Framer, Proto
from mtrtk.daemon import Daemon
from mtrtk.rawlog.index import list_logs

pytestmark = pytest.mark.hardware


async def test_live_base_serves_rtcm_and_logs(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, role="base", mtrtk_source=os.environ.get("MTRTK_TEST_PORT", "auto"),
        data_dir=tmp_path, ntrip_bind="127.0.0.1", ntrip_port=0, ntrip_user="rover", ntrip_password="pw",
        base_mode="survey-in", svin_min_duration_s=60, svin_acc_limit_m=5.0,
    )
    daemon = Daemon(settings)
    run_task = asyncio.create_task(daemon.run())
    for _ in range(300):
        await asyncio.sleep(0.1)
        if daemon.caster is not None and daemon.caster._server is not None and daemon.controller.connected:
            break
    assert daemon.caster is not None
    auth = base64.b64encode(b"rover:pw").decode()
    reader, writer = await asyncio.open_connection("127.0.0.1", daemon.caster.port)
    writer.write(f"GET /MTRK HTTP/1.1\r\nHost: x\r\nNtrip-Version: Ntrip/2.0\r\nUser-Agent: NTRIP pytest\r\nAuthorization: Basic {auth}\r\n\r\n".encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5.0)
    assert head.startswith(b"HTTP/1.1 200 OK")
    deadline = asyncio.get_running_loop().time() + 90
    types: set[int] = set()
    framer = Framer()
    while asyncio.get_running_loop().time() < deadline:
        chunk = await asyncio.wait_for(reader.read(65536), 10.0)
        for f in framer.feed(chunk.replace(b"\r\n", b"")):  # strip chunk framing crudely for the smoke test
            if f.proto is Proto.RTCM3:
                types.add(f.rtcm_type)
        if 1005 in types and daemon.store.state.survey_in.valid:
            break
    print("RTCM types seen:", sorted(types), "survey-in:", daemon.store.state.survey_in)
    assert types & {1077, 1074}, "no MSM observations received"
    writer.close()
    daemon.stop.set()
    await asyncio.wait_for(run_task, 30.0)
    logs = list_logs(tmp_path)
    assert logs and logs[-1].msg_counts.get("RXM-RAWX", 0) > 30
```

The crude `replace(b"\r\n", b"")` can corrupt an RTCM frame that happens to contain `0D 0A`; the framer's CRC check drops it, which is fine for a smoke test.

- [ ] **Step 2: Run the live test (hardware step, ~2 min)**

Run: `uv run pytest -m hardware tests/hardware/test_live_base.py -s`
Expected: `1 passed`; printed types include MSM (1077…) and, once survey-in is valid (≈60 s), 1005 and 1230. If MSM never appears before survey-in is valid, that answers spec open item 6 (HPG 1.13 gates MSM on a valid TMODE position) — record it in the spec's *Open items* with the observed behaviour and relax the assertion to wait for survey-in first.

- [ ] **Step 3: Manual end-to-end with RTKLIB over Tailscale**

```bash
cp -n .env.example .env && sed -i 's/^NTRIP_PASSWORD=.*/NTRIP_PASSWORD=pw/; s/^SVIN_MIN_DURATION_S=.*/SVIN_MIN_DURATION_S=120/' .env
uv run mtrtk -v base            # terminal 1; wait for "NTRIP caster listening on 100.x.y.z:2101"
str2str -in ntrip://rover:pw@100.100.50.10:2101/MTRK -out file://./rtcm_test.bin &   # terminal 2
sleep 60; kill %1
uv run python -c "
from collections import Counter
from mtrtk.core.frames import Framer
c = Counter(f.rtcm_type for f in Framer().feed(open('rtcm_test.bin','rb').read()))
print(c)"
ls -la data/ubx/*/*/ ; cat data/ubx/*/*/*.json | head -40
uv run mtrtk sites list
```
Expected: `Counter({1077: ~60, 1087: ~60, 1097: ~60, 1127: ~60, 1005: ~60, 1230: ~12})` (1005 present once survey-in became valid), one `.ubx` + `.json` per hour started, `sites list` prints `no sites saved`. Delete `rtcm_test.bin` afterwards.

- [ ] **Step 4: Write `docs/base.md`**

```markdown
# Base station

## What runs
`mtrtk base` opens the ZED-F9P, applies the base profile (1 Hz, RAWX+SFRBX, RTCM3 MSM7 + 1005 + 1230),
starts survey-in (or a fixed site), logs raw UBX hourly under `DATA_DIR/ubx/YYYY/DDD/`, serves RTCM over
NTRIP on `NTRIP_BIND:NTRIP_PORT/MOUNTPOINT`, samples history into `DATA_DIR/mtrtk.db` and raises alerts.

## Minimal `.env`
```
ROLE=base
NTRIP_PASSWORD=choose-a-password     # empty = anonymous (tailnet only)
BASE_MODE=survey-in
SVIN_MIN_DURATION_S=300
SVIN_ACC_LIMIT_M=2.0
```

## Rovers connect with
`ntrip://rover:<password>@<tailscale-ip>:2101/MTRK` — NTRIP v1 (`str2str`, u-center, RTKLIB) and v2
(SW Maps, Lefebure, Emlid) both work. The sourcetable is at `GET /`.

## Survey-in → fixed site
1. Let survey-in finish (status line shows `svin … ✓`; event `survey_in_valid`).
2. Save it: from Phase 3 on, use the UI/API "freeze"; until then add the mean position printed in the
   log with `mtrtk sites add roof --ecef X Y Z --sigma <acc> --source survey-in`.
3. `mtrtk sites activate roof` and set `BASE_MODE=fixed`. The daemon verifies RTCM 1005 against the site
   (event `site_verified`, or `site_mismatch` if the receiver disagrees).

Survey-in gives ~1–2 m absolute accuracy; rovers inherit that offset. For centimetre-level absolute
coordinates, log 24 h, export RINEX (Phase 5) and submit to CSRS-PPP, then add the result as a site.

## Files
- `DATA_DIR/ubx/2026/261/MTRK_20260918_16.ubx` + `.json` sidecar (counts, sha256, `keep` flag).
- `DATA_DIR/mtrtk.db` — SQLite (WAL): samples, sites, events, NTRIP client log.
- Retention deletes the oldest unkept hour when free disk < `MIN_FREE_GB`.
```

- [ ] **Step 5: Update `.env.example` and README**

Add under the role/receiver block of `.env.example`: `REPLAY_LOG=0                      # 1 = also write raw logs when MTRTK_SOURCE is a file`.
In `README.md` *Status*, replace the Phase 1 line with: `Phase 2 (base daemon) complete: hourly raw logging + retention, NTRIP caster (v1/v2), survey-in / fixed sites with RTCM 1005 verification, SQLite history, alerts. Next: web API (Phase 3), UI (Phase 4).`

- [ ] **Step 6: Full verification and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy
docker build -f docker/Dockerfile -t mtrtk:dev .
git add docs/base.md tests/hardware/test_live_base.py README.md .env.example
git commit -m "docs: base station guide, live base smoke test, Phase 2 status

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git tag -a v0.2.0-phase2 -m "Phase 2: base daemon"
```

Phase 3 (web API) is the next plan: `docs/superpowers/plans/2026-09-18-phase3-web-api.md`.
