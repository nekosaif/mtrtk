# mtrtk Phase 6: F9P Rover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `ROLE=rover` a complete field tool for a ZED-F9P: pull RTCM from the base over NTRIP and inject it into the receiver, expose RTK status (fixed/float, correction age, baseline), emit NMEA over TCP/UDP/serial and JSON over UDP for other software, always log raw UBX (plus camera time marks) for PPK, run field sessions and collect averaged survey points with exports — with matching RTK and Survey pages in the UI.

**Architecture:** The rover reuses the Phase 1 core (source → framer → bus → `StateStore`) with the rover receiver profile. `ReceiverState` gains `rtk`, `time_marks` and `attitude` sections (the normalized state every driver — F9P now, INS units in Phase 10 — fills). `rover/drivers/base.py` defines the `RoverDriver` protocol and `UbloxDriver` implements it on top of `ReceiverController`. `rover/ntrip_client.py` is an asyncio NTRIP v2/v1 client that frames incoming RTCM and calls `driver.inject_rtcm`. `rover/nmea_out.py` synthesizes GGA/RMC/GST/GSA/GSV/VTG/ZDA (+HDT/PASHR when attitude exists) from `ReceiverState` each epoch and fans out to TCP/UDP/serial sinks; `rover/json_out.py` sends the epoch as JSON over UDP. `rover/sessions.py` and `rover/points.py` persist field sessions and averaged points in SQLite. The daemon wires these as supervised consumers for the rover role; the API and UI add `/rtk` and `/survey`.

**Tech Stack:** Python asyncio (streams for NTRIP/TCP, `DatagramProtocol` for UDP, `pyserial-asyncio-fast`/`os.openpty` for serial), pynmeagps (`hpnmeamode=True` for 7-decimal minutes), pydantic, React (Phase 4 stack), MapLibre.

**Spec:** `docs/superpowers/specs/2026-09-18-mtrtk-design.md` — *rover*, *Rover: NTRIP client…*, *Phase 6*, spec constraints on drivers. Prerequisites: Phases 1–5 (the caster from Phase 2 is used as the test peer for the NTRIP client). Verified during planning: pynmeagps `NMEAMessage(..., hpnmeamode=True)` serializes 7-decimal minutes and round-trips exactly; GST's field is `stdLong`; RMC/ZDA need `datetime.date`/`datetime.time` objects (strings are mangled); pynmeagps pads unused GSA slots with `0`; ZDA's month is not zero-padded by pynmeagps, so ZDA is assembled by hand with `nmea_checksum`.

## Global Constraints

- One receiver profile for the rover (Phase 1 `rover_profile`): 5 Hz default (`ROVER_NAV_HZ`), USB input RTCM3 enabled, TMODE off, NAV-RELPOSNED, RXM-RTCM, TIM-TM2 enabled.
- Correction age: primary source is `time since the last RTCM frame was injected` (client-side, monotonic); NAV-PVT `lastCorrectionAge` code is decoded to seconds as the receiver's view (`CORR_AGE_CODE_S = {0: None, 1: 1, 2: 2, 3: 5, 4: 10, 5: 15, 6: 20, 7: 30, 8: 45, 9: 60, 10: 90, 11: 120, 12: 121}` = upper bound of each bucket). Alerts: warning when age > 10 s, serious when > 30 s, cleared under 5 s.
- NTRIP client: try v2 first (`Ntrip-Version: Ntrip/2.0`), accept `ICY 200 OK` (v1 raw), `HTTP/1.x 200` (dechunk when `Transfer-Encoding: chunked`), treat `SOURCETABLE 200 OK` as "mountpoint missing" (60 s backoff), `401` as auth failure (60 s backoff + alert), other failures with 1→60 s jittered exponential backoff; 10 s no-data timeout; GGA uploaded every `NTRIP_GGA_INTERVAL_S` when the receiver has a fix; only CRC-valid RTCM frames are injected.
- NMEA: talker `GN`, 7-decimal minutes, GGA quality 0/1/2/4/5 (none/GPS/DGPS/RTK fixed/RTK float), GST from hAcc/vAcc (σ_lat = σ_lon = hAcc/√2), one GSA per system in view with the system id, GSV per system in groups of four, VTG, ZDA, RMC; HDT + PASHR only when `attitude` is present. Sentence set and rate follow the receiver epoch (max 5 Hz); GSA/GSV at most 1 Hz.
- Raw logging on the rover is always on (Phase 2 writer, role `rover`, default message list already includes TIM-TM2); sessions are DB rows, not separate files.
- Point averaging: N epochs (default 30), optional "RTK fixed only" filter (skips non-fixed epochs, aborts after 5× N skipped), mean in ENU around the first accepted epoch, `sd_n/e/u` sample standard deviations, stores `h_acc/v_acc` of the last epoch, `fix_type`, `carr_soln`.
- Commit per task, Conventional Commits, trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File structure (this plan)

| Path | Responsibility |
|---|---|
| `src/mtrtk/core/state.py` (modify), `src/mtrtk/core/statestore.py` (modify) | `RtkStatus`, `RtcmRxStats`, `TimeMark`, `Attitude`; NAV-RELPOSNED / RXM-RTCM / TIM-TM2 handlers, correction-age code decode |
| `src/mtrtk/rover/__init__.py`, `drivers/__init__.py`, `drivers/base.py`, `drivers/ublox.py` | `RoverDriver` protocol, `DriverCapabilities`, `UbloxDriver` |
| `src/mtrtk/rover/ntrip_client.py` | `NtripClient` |
| `src/mtrtk/rover/nmea_out.py`, `src/mtrtk/rover/sinks.py`, `src/mtrtk/rover/json_out.py` | sentence synthesis, TCP/UDP/serial sinks, JSON UDP |
| `src/mtrtk/rover/sessions.py`, `src/mtrtk/rover/points.py`, `src/mtrtk/rover/exports.py` | sessions repo, point collector + repo, CSV/GeoJSON/KML/GPX |
| `src/mtrtk/web/api/rover.py`, `src/mtrtk/web/ws.py` (modify), `src/mtrtk/alerts.py` (modify), `src/mtrtk/store/sampler.py` (modify) | rover API, `rtk` in epoch bundle, rover alert rules, corr_age/baseline sampling |
| `src/mtrtk/daemon.py` (modify), `src/mtrtk/config.py` (modify) | rover consumers; `nmea_sentences`, `nmea_rate_hz`, `point_epochs`, `point_fixed_only` settings |
| `web/src/pages/Rtk.tsx`, `web/src/pages/Survey.tsx`, `web/src/app/Rail.tsx` (modify), `web/src/app/Tape.tsx` (modify), `web/src/pages/Dashboard.tsx` (modify) | rover UI |
| `docs/rover.md` | guide |
| `tests/unit/test_state_rover.py`, `test_ntrip_client.py`, `test_nmea_out.py`, `test_sinks.py`, `test_points.py`, `test_exports.py`, `test_web_rover.py`, `test_daemon_rover.py`; `tests/hardware/test_live_rover.py` | tests |

---

### Task 1: Rover state — RTK status, RTCM receive stats, time marks, attitude

**Files:**
- Modify: `src/mtrtk/core/state.py`, `src/mtrtk/core/statestore.py`
- Create: `tests/unit/test_state_rover.py`

**Interfaces:**
- Produces: `RtcmRxStats(count, used, crc_failed, last_seen_mono)`; `RtkStatus(carr_soln, carr_soln_name, diff_soln, rel_pos_n_m, rel_pos_e_m, rel_pos_d_m, baseline_m, heading_deg, heading_valid, acc_n_m, acc_e_m, acc_d_m, acc_length_m, acc_heading_deg, ref_station_id, rel_pos_valid, is_moving, ref_pos_missing, ref_obs_missing, normalized, corr_age_receiver_s, corr_age_s, rtcm_rx: dict[int, RtcmRxStats], rtcm_rx_total, rtcm_crc_failed, last_rtcm_mono)`; `TimeMark(channel, count, rising_week, rising_tow_s, falling_week, falling_tow_s, new_rising, new_falling, time_base, utc_based, acc_est_ns, rising_utc: datetime|None)`; `Attitude(roll_deg, pitch_deg, heading_deg, acc_roll_deg, acc_pitch_deg, acc_heading_deg, source)`; `ReceiverState.rtk`, `.time_marks: list[TimeMark]` (last 100, newest last), `.attitude: Attitude | None`; `StateStore` handlers `NAV-RELPOSNED` → `{"rtk"}`, `RXM-RTCM` → `{"rtk"}`, `TIM-TM2` → `{"time_marks"}` and publishes `state.time_mark` (the new `TimeMark`) on each new rising edge; NAV-PVT also fills `rtk.carr_soln*`, `rtk.diff_soln`, `rtk.corr_age_receiver_s`; `StateStore.note_rtcm_injected(now_mono)` sets `rtk.last_rtcm_mono` and `corr_age_s` is refreshed on every epoch as `now - last_rtcm_mono`; `gps_to_utc(week, tow_s, leap_s) -> datetime`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_state_rover.py`:
```python
from datetime import UTC, datetime

from pyubx2 import GET, UBXMessage

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.core.statestore import StateStore, gps_to_utc


def frame(msg: UBXMessage):
    return Framer().feed(msg.serialize())[0]


def test_relposned_maps_units_and_flags() -> None:
    store = StateStore()
    msg = UBXMessage(
        "NAV", "NAV-RELPOSNED", GET, version=1, refStationID=7, iTOW=1000, relPosN=123456, relPosE=-2345, relPosD=678,
        relPosLength=123478, relPosHeading=91.12345, accN=12.0, accE=15.0, accD=30.0, accLength=14.0, accHeading=0.5,
        gnssFixOK=1, diffSoln=1, relPosValid=1, carrSoln=2, isMoving=0, refPosMiss=0, refObsMiss=0, relPosHeadingValid=1, relPosNormalized=0,
    )
    assert store.apply(frame(msg)) == {"rtk"}
    r = store.state.rtk
    assert r.rel_pos_n_m == 1234.56 and r.rel_pos_e_m == -23.45 and r.rel_pos_d_m == 6.78
    assert r.baseline_m == 1234.78 and r.heading_deg == 91.12345 and r.heading_valid is True
    assert r.acc_n_m == 0.012 and r.acc_length_m == 0.014 and r.acc_heading_deg == 0.5
    assert r.ref_station_id == 7 and r.carr_soln == 2 and r.carr_soln_name == "RTK fixed" and r.rel_pos_valid is True


def test_rxm_rtcm_counts_used_and_crc_failures() -> None:
    store = StateStore()
    store.apply(frame(UBXMessage("RXM", "RXM-RTCM", GET, version=2, crcFailed=0, msgUsed=2, subType=0, refStation=7, msgType=1077)))
    store.apply(frame(UBXMessage("RXM", "RXM-RTCM", GET, version=2, crcFailed=0, msgUsed=1, subType=0, refStation=7, msgType=1077)))
    store.apply(frame(UBXMessage("RXM", "RXM-RTCM", GET, version=2, crcFailed=1, msgUsed=0, subType=0, refStation=7, msgType=1005)))
    r = store.state.rtk
    assert r.rtcm_rx[1077].count == 2 and r.rtcm_rx[1077].used == 1
    assert r.rtcm_rx[1005].crc_failed == 1 and r.rtcm_rx_total == 3 and r.rtcm_crc_failed == 1
    assert r.ref_station_id == 7


def test_pvt_correction_age_code_and_injection_age() -> None:
    store = StateStore()
    store.apply(frame(UBXMessage("NAV", "NAV-PVT", GET, iTOW=1, fixType=3, carrSoln=1, diffSoln=1, lastCorrectionAge=3)))
    assert store.state.rtk.corr_age_receiver_s == 5 and store.state.rtk.carr_soln_name == "RTK float" and store.state.rtk.diff_soln is True
    store.note_rtcm_injected(now_mono=100.0)
    store.apply(frame(UBXMessage("NAV", "NAV-EOE", GET, iTOW=1)), now_mono=103.5)
    assert store.state.rtk.corr_age_s == 3.5


def test_gps_to_utc() -> None:
    # GPS week 2436, TOW 492472 s, 18 leap seconds -> 2026-09-18 16:47:34 UTC (matches the base fixture)
    assert gps_to_utc(2436, 492472.0, 18) == datetime(2026, 9, 18, 16, 47, 34, tzinfo=UTC)


def test_tim_tm2_appends_time_mark_and_publishes() -> None:
    bus = Bus()
    sub = bus.subscribe("state.time_mark")
    store = StateStore(bus)
    store.state.time.leap_s = 18
    msg = UBXMessage("TIM", "TIM-TM2", GET, ch=0, mode=1, run=1, newFallingEdge=0, timeBase=1, utc=0, time=1, newRisingEdge=1, count=42, wnR=2436, wnF=2436, towMsR=492472123, towSubMsR=456000, towMsF=492472000, towSubMsF=0, accEst=25)
    assert store.apply(frame(msg)) == {"time_marks"}
    tm = store.state.time_marks[-1]
    assert tm.count == 42 and tm.new_rising is True and tm.rising_week == 2436
    assert abs(tm.rising_tow_s - 492472.123456) < 1e-9
    assert tm.rising_utc == datetime(2026, 9, 18, 16, 47, 34, 123456, tzinfo=UTC)
    assert sub.queue.qsize() == 1
    for i in range(120):
        store.apply(frame(UBXMessage("TIM", "TIM-TM2", GET, ch=0, newRisingEdge=1, count=43 + i, wnR=2436, towMsR=492473000 + i)))
    assert len(store.state.time_marks) == 100 and store.state.time_marks[-1].count == 162


def test_tm2_without_new_edge_does_not_append() -> None:
    store = StateStore()
    assert store.apply(frame(UBXMessage("TIM", "TIM-TM2", GET, ch=0, newRisingEdge=0, newFallingEdge=0, count=1))) == set()
    assert store.state.time_marks == []
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/unit/test_state_rover.py -q` → FAIL (no `rtk` attribute / `gps_to_utc`).

- [ ] **Step 3: Extend `src/mtrtk/core/state.py`**

Add after `Firmware`:
```python
class RtcmRxStats(BaseModel):
    count: int = 0
    used: int = 0
    crc_failed: int = 0
    last_seen_mono: float | None = None


class RtkStatus(BaseModel):
    carr_soln: int = 0
    carr_soln_name: str = "None"
    diff_soln: bool = False
    rel_pos_n_m: float | None = None
    rel_pos_e_m: float | None = None
    rel_pos_d_m: float | None = None
    baseline_m: float | None = None
    heading_deg: float | None = None
    heading_valid: bool = False
    acc_n_m: float | None = None
    acc_e_m: float | None = None
    acc_d_m: float | None = None
    acc_length_m: float | None = None
    acc_heading_deg: float | None = None
    ref_station_id: int | None = None
    rel_pos_valid: bool = False
    is_moving: bool = False
    ref_pos_missing: bool = False
    ref_obs_missing: bool = False
    normalized: bool = False
    corr_age_receiver_s: float | None = None  # decoded NAV-PVT lastCorrectionAge bucket (upper bound)
    corr_age_s: float | None = None  # seconds since we last injected RTCM
    rtcm_rx: dict[int, RtcmRxStats] = Field(default_factory=dict)
    rtcm_rx_total: int = 0
    rtcm_crc_failed: int = 0
    last_rtcm_mono: float | None = None


class TimeMark(BaseModel):
    channel: int
    count: int
    rising_week: int | None = None
    rising_tow_s: float | None = None
    falling_week: int | None = None
    falling_tow_s: float | None = None
    new_rising: bool = False
    new_falling: bool = False
    time_base: int = 0
    utc_based: bool = False
    acc_est_ns: int = 0
    rising_utc: datetime | None = None


class Attitude(BaseModel):
    roll_deg: float | None = None
    pitch_deg: float | None = None
    heading_deg: float | None = None
    acc_roll_deg: float | None = None
    acc_pitch_deg: float | None = None
    acc_heading_deg: float | None = None
    source: str = ""
```
Add to `ReceiverState`: `rtk: RtkStatus = Field(default_factory=RtkStatus)`, `time_marks: list[TimeMark] = Field(default_factory=list)`, `attitude: Attitude | None = None`. Add constant `CORR_AGE_CODE_S: dict[int, float | None] = {0: None, 1: 1, 2: 2, 3: 5, 4: 10, 5: 15, 6: 20, 7: 30, 8: 45, 9: 60, 10: 90, 11: 120, 12: 121}` and `MAX_TIME_MARKS = 100`.

- [ ] **Step 4: Extend `src/mtrtk/core/statestore.py`**

Imports: add `RtcmRxStats, TimeMark, CORR_AGE_CODE_S, MAX_TIME_MARKS` from `mtrtk.core.state`, `from datetime import timedelta`. Module-level helper:
```python
GPS_EPOCH = datetime(1980, 1, 6, tzinfo=UTC)


def gps_to_utc(week: int, tow_s: float, leap_s: int | None) -> datetime:
    return GPS_EPOCH + timedelta(weeks=week, seconds=tow_s - (leap_s if leap_s is not None else 18))
```
Register handlers `"NAV-RELPOSNED": self._nav_relposned`, `"RXM-RTCM": self._rxm_rtcm`, `"TIM-TM2": self._tim_tm2`. Change `apply` to accept `now_mono: float | None = None` and, in `_nav_eoe`, refresh the injection age:
```python
    def apply(self, frame: Frame, now_mono: float | None = None) -> set[str]:
        self._now_mono = now_mono if now_mono is not None else time.monotonic()
        ...
```
(`_nav_eoe` uses `self._now_mono` for `last_epoch_mono` and computes `rtk.corr_age_s = self._now_mono - rtk.last_rtcm_mono` when `last_rtcm_mono` is set, then publishes `state.rtk` as well as `state.epoch`.)

In `_nav_pvt`, add:
```python
        s.rtk.carr_soln = m.carrSoln
        s.rtk.carr_soln_name = CARR_SOLN_NAMES.get(m.carrSoln, f"carr{m.carrSoln}")
        s.rtk.diff_soln = bool(m.diffSoln)
        s.rtk.corr_age_receiver_s = CORR_AGE_CODE_S.get(m.lastCorrectionAge)
```
and return set including `"rtk"`.

New methods:
```python
    def note_rtcm_injected(self, now_mono: float | None = None) -> None:
        self.state.rtk.last_rtcm_mono = now_mono if now_mono is not None else time.monotonic()

    def _nav_relposned(self, m: Any) -> set[str]:
        r = self.state.rtk
        r.rel_pos_n_m, r.rel_pos_e_m, r.rel_pos_d_m = m.relPosN / 100, m.relPosE / 100, m.relPosD / 100
        r.baseline_m = m.relPosLength / 100
        r.heading_deg = m.relPosHeading
        r.heading_valid = bool(m.relPosHeadingValid)
        r.acc_n_m, r.acc_e_m, r.acc_d_m = m.accN / 1000, m.accE / 1000, m.accD / 1000
        r.acc_length_m = m.accLength / 1000
        r.acc_heading_deg = m.accHeading
        r.ref_station_id = m.refStationID
        r.rel_pos_valid = bool(m.relPosValid)
        r.is_moving = bool(m.isMoving)
        r.ref_pos_missing = bool(m.refPosMiss)
        r.ref_obs_missing = bool(m.refObsMiss)
        r.normalized = bool(m.relPosNormalized)
        r.carr_soln = m.carrSoln
        r.carr_soln_name = CARR_SOLN_NAMES.get(m.carrSoln, f"carr{m.carrSoln}")
        r.diff_soln = bool(m.diffSoln)
        return {"rtk"}

    def _rxm_rtcm(self, m: Any) -> set[str]:
        r = self.state.rtk
        st = r.rtcm_rx.setdefault(int(m.msgType), RtcmRxStats())
        st.count += 1
        st.last_seen_mono = self._now_mono
        if m.crcFailed:
            st.crc_failed += 1
            r.rtcm_crc_failed += 1
        elif m.msgUsed == 2:
            st.used += 1
        r.rtcm_rx_total += 1
        if m.refStation:
            r.ref_station_id = int(m.refStation)
        return {"rtk"}

    def _tim_tm2(self, m: Any) -> set[str]:
        if not (m.newRisingEdge or m.newFallingEdge):
            return set()
        leap = self.state.time.leap_s
        rising_tow = m.towMsR / 1000 + m.towSubMsR / 1e9 if m.newRisingEdge else None
        falling_tow = m.towMsF / 1000 + m.towSubMsF / 1e9 if m.newFallingEdge else None
        mark = TimeMark(
            channel=m.ch, count=m.count,
            rising_week=m.wnR if m.newRisingEdge else None, rising_tow_s=rising_tow,
            falling_week=m.wnF if m.newFallingEdge else None, falling_tow_s=falling_tow,
            new_rising=bool(m.newRisingEdge), new_falling=bool(m.newFallingEdge),
            time_base=m.timeBase, utc_based=bool(m.utc), acc_est_ns=m.accEst,
            rising_utc=gps_to_utc(m.wnR, rising_tow, leap) if rising_tow is not None and m.time else None,
        )
        marks = self.state.time_marks
        marks.append(mark)
        if len(marks) > MAX_TIME_MARKS:
            del marks[: len(marks) - MAX_TIME_MARKS]
        self._publish("state.time_mark", mark)
        return {"time_marks"}
```
`m.time` is the TIM-TM2 "time is valid" flag; when unset, `rising_utc` stays `None` (the test sets `time=1`). Note `rising_utc` uses `timeBase`=1 (GNSS time); if `utc_based` is true the tow already is UTC and no leap correction applies: pass `leap_s=0` in that case.

- [ ] **Step 5: Run the whole suite (existing state tests must still pass), lint, commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/mtrtk/core/state.py src/mtrtk/core/statestore.py tests/unit/test_state_rover.py
git commit -m "feat(core): RTK status, RTCM receive stats, time marks and attitude in receiver state

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Driver protocol and NTRIP client

**Files:**
- Create: `src/mtrtk/rover/__init__.py` (empty), `src/mtrtk/rover/drivers/__init__.py` (empty), `src/mtrtk/rover/drivers/base.py`, `src/mtrtk/rover/drivers/ublox.py`, `src/mtrtk/rover/ntrip_client.py`, `tests/unit/test_ntrip_client.py`

**Interfaces:**
- Produces: `DriverCapabilities(accepts_rtcm, raw_gnss_log, attitude, imu, sats, spectrum)`; `RoverDriver` protocol: `name: str`, `capabilities: DriverCapabilities`, `async inject_rtcm(data: bytes) -> None`; `UbloxDriver(controller, store)` (`inject_rtcm` writes to `controller.link` when connected and calls `store.note_rtcm_injected()`; drops silently when disconnected and counts `dropped_bytes`); `NtripClientConfig.from_url("ntrip://user:pass@host:2101/MTRK")` (also accepts `http://`), `NtripClient(config, bus, driver, gga_provider: Callable[[], bytes | None], gga_interval_s=10)` with `await run(stop)`, `.status: NtripClientStatus(connected, host, port, mountpoint, version, bytes_received, frames_injected, crc_dropped, last_rtcm_mono, last_error, reconnects, next_retry_s)`; publishes `ntrip_client.status` on every change (connect, disconnect, every 5 s while streaming); `decode_chunked` async helper.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_ntrip_client.py`:
```python
import asyncio
from pathlib import Path

import pytest
from ubxtest import rtcm_frame

from mtrtk.base.ntrip_caster import CasterConfig, NtripCaster
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.rover.drivers.base import DriverCapabilities
from mtrtk.rover.ntrip_client import NtripClient, NtripClientConfig

RTCM_1005 = bytes.fromhex("d300133ed7fd0382dfdc1c403db34fe8fe0cef5e6b30bd2e23")
RTCM_1077 = rtcm_frame(1077, b"\x00" * 40)


class RecordingDriver:
    name = "recording"
    capabilities = DriverCapabilities(accepts_rtcm=True, raw_gnss_log=True, attitude=False, imu=False, sats=True, spectrum=False)

    def __init__(self) -> None:
        self.injected: list[bytes] = []

    async def inject_rtcm(self, data: bytes) -> None:
        self.injected.append(data)


@pytest.fixture
async def caster(tmp_path: Path):
    bus = Bus()
    c = NtripCaster(bus, CasterConfig(mountpoint="MTRK", username="rover", password="pw", station_id="MTRK", country="BGD"), host="127.0.0.1", port=0)
    await c.start()
    try:
        yield c, bus
    finally:
        await c.stop()


def publish(bus: Bus, raw: bytes) -> None:
    for f in Framer().feed(raw):
        bus.publish("raw.rtcm", f)


def test_config_from_url() -> None:
    cfg = NtripClientConfig.from_url("ntrip://rover:s3cret@100.100.50.10:2101/MTRK")
    assert (cfg.host, cfg.port, cfg.mountpoint, cfg.username, cfg.password) == ("100.100.50.10", 2101, "MTRK", "rover", "s3cret")
    anon = NtripClientConfig.from_url("http://base.tailnet/MTRK")
    assert anon.port == 2101 and anon.username is None
    with pytest.raises(ValueError):
        NtripClientConfig.from_url("ntrip://host:2101/")


async def test_client_streams_v2_and_injects_valid_frames(caster) -> None:
    c, bus = caster
    client_bus = Bus()
    statuses = client_bus.subscribe("ntrip_client.status")
    driver = RecordingDriver()
    client = NtripClient(NtripClientConfig.from_url(f"ntrip://rover:pw@127.0.0.1:{c.port}/MTRK"), client_bus, driver, gga_provider=lambda: b"$GNGGA,164734.00,2350.24104,N,09015.75301,E,1,12,0.9,13.3,M,-49.6,M,0.0,0*78\r\n", gga_interval_s=0.05)
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if client.status.connected:
            break
    assert client.status.connected and client.status.version == 2
    publish(bus, RTCM_1005 + RTCM_1077 + b"\xd3\x00\x05junkxx")  # last one has a bad CRC
    await asyncio.sleep(0.1)
    assert driver.injected == [RTCM_1005, RTCM_1077]
    assert client.status.frames_injected == 2 and client.status.bytes_received >= len(RTCM_1005) + len(RTCM_1077)
    assert any(ci.last_gga_lat is not None for ci in c.clients.values())  # GGA reached the caster
    assert statuses.queue.qsize() >= 1
    stop.set()
    await asyncio.wait_for(task, 2.0)
    assert client.status.connected is False


async def test_client_falls_back_to_v1_when_server_speaks_icy(caster, monkeypatch: pytest.MonkeyPatch) -> None:
    c, bus = caster
    driver = RecordingDriver()
    client = NtripClient(NtripClientConfig.from_url(f"ntrip://rover:pw@127.0.0.1:{c.port}/MTRK"), Bus(), driver, gga_provider=lambda: None)
    client.force_v1 = True
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if client.status.connected:
            break
    assert client.status.version == 1
    publish(bus, RTCM_1077)
    await asyncio.sleep(0.1)
    assert driver.injected == [RTCM_1077]
    stop.set()
    await asyncio.wait_for(task, 2.0)


async def test_wrong_password_backs_off_and_reports(caster, monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = caster
    from mtrtk.rover import ntrip_client as mod

    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)
        if len(sleeps) >= 2:
            stop.set()

    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    client = NtripClient(NtripClientConfig.from_url(f"ntrip://rover:wrong@127.0.0.1:{c.port}/MTRK"), Bus(), RecordingDriver(), gga_provider=lambda: None)
    stop = asyncio.Event()
    await asyncio.wait_for(client.run(stop), 5.0)
    assert client.status.connected is False and "401" in (client.status.last_error or "")
    assert sleeps[0] == 60.0


async def test_missing_mountpoint_is_reported(caster, monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = caster
    from mtrtk.rover import ntrip_client as mod

    async def fake_sleep(d: float) -> None:
        stop.set()

    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    client = NtripClient(NtripClientConfig.from_url(f"ntrip://rover:pw@127.0.0.1:{c.port}/NOPE"), Bus(), RecordingDriver(), gga_provider=lambda: None)
    stop = asyncio.Event()
    await asyncio.wait_for(client.run(stop), 5.0)
    assert "mountpoint" in (client.status.last_error or "").lower()


async def test_unreachable_host_backs_off_exponentially(monkeypatch: pytest.MonkeyPatch) -> None:
    from mtrtk.rover import ntrip_client as mod

    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)
        if len(sleeps) >= 4:
            stop.set()

    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(mod.random, "uniform", lambda a, b: 1.0)
    client = NtripClient(NtripClientConfig.from_url("ntrip://127.0.0.1:1/MTRK"), Bus(), RecordingDriver(), gga_provider=lambda: None)
    stop = asyncio.Event()
    await asyncio.wait_for(client.run(stop), 5.0)
    assert sleeps[:4] == [1.0, 2.0, 4.0, 8.0] and client.status.reconnects == 4
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Write the driver files**

`src/mtrtk/rover/drivers/base.py`:
```python
"""What every rover receiver driver provides. The F9P driver lives in ublox.py; INS drivers come later."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DriverCapabilities:
    accepts_rtcm: bool
    raw_gnss_log: bool
    attitude: bool
    imu: bool
    sats: bool
    spectrum: bool


class RoverDriver(Protocol):
    name: str
    capabilities: DriverCapabilities

    async def inject_rtcm(self, data: bytes) -> None:
        """Forward one CRC-valid RTCM3 frame to the receiver (no-op when it cannot accept corrections)."""
        ...
```

`src/mtrtk/rover/drivers/ublox.py`:
```python
"""ZED-F9P rover driver: corrections go straight into the receiver's USB port via the live UbxLink."""

from __future__ import annotations

import logging

from mtrtk.core.receiver import ReceiverController
from mtrtk.core.statestore import StateStore
from mtrtk.rover.drivers.base import DriverCapabilities

log = logging.getLogger(__name__)


class UbloxDriver:
    name = "ublox"
    capabilities = DriverCapabilities(accepts_rtcm=True, raw_gnss_log=True, attitude=False, imu=False, sats=True, spectrum=True)

    def __init__(self, controller: ReceiverController, store: StateStore) -> None:
        self.controller = controller
        self.store = store
        self.dropped_bytes = 0

    async def inject_rtcm(self, data: bytes) -> None:
        link = self.controller.link
        if link is None or not self.controller.connected:
            self.dropped_bytes += len(data)
            return
        await link.write(data)
        self.store.note_rtcm_injected()
```

- [ ] **Step 4: Write `src/mtrtk/rover/ntrip_client.py`**

```python
"""NTRIP v2/v1 client: pulls RTCM from a caster, frames it and hands valid frames to the driver."""

from __future__ import annotations

import asyncio
import base64
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

from mtrtk import __version__
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer, Proto
from mtrtk.rover.drivers.base import RoverDriver

log = logging.getLogger(__name__)

NO_DATA_TIMEOUT_S = 10.0
AUTH_BACKOFF_S = 60.0
MOUNT_BACKOFF_S = 60.0
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0
STATUS_INTERVAL_S = 5.0


@dataclass(frozen=True)
class NtripClientConfig:
    host: str
    port: int
    mountpoint: str
    username: str | None
    password: str | None

    @classmethod
    def from_url(cls, url: str) -> NtripClientConfig:
        parsed = urlparse(url if "://" in url else f"ntrip://{url}")
        mount = parsed.path.strip("/")
        if not parsed.hostname or not mount:
            raise ValueError("NTRIP URL must look like ntrip://user:pass@host:2101/MOUNTPOINT")
        return cls(parsed.hostname, parsed.port or 2101, mount, parsed.username, parsed.password)


@dataclass
class NtripClientStatus:
    connected: bool = False
    host: str = ""
    port: int = 0
    mountpoint: str = ""
    version: int | None = None
    bytes_received: int = 0
    frames_injected: int = 0
    crc_dropped: int = 0
    last_rtcm_mono: float | None = None
    last_error: str | None = None
    reconnects: int = 0
    next_retry_s: float | None = None
    since_mono: float | None = None


class _Response(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class NtripClient:
    def __init__(self, config: NtripClientConfig, bus: Bus, driver: RoverDriver, gga_provider: Callable[[], bytes | None], gga_interval_s: float = 10.0) -> None:
        self.config = config
        self.bus = bus
        self.driver = driver
        self.gga_provider = gga_provider
        self.gga_interval_s = gga_interval_s
        self.force_v1 = False
        self.status = NtripClientStatus(host=config.host, port=config.port, mountpoint=config.mountpoint)

    # ------------------------------------------------------------- lifecycle
    async def run(self, stop: asyncio.Event) -> None:
        backoff = BACKOFF_MIN_S
        while not stop.is_set():
            try:
                await self._session(stop)
                backoff = BACKOFF_MIN_S
                if stop.is_set():
                    break
                delay = BACKOFF_MIN_S
            except _Response as exc:
                self.status.last_error = str(exc)
                delay = AUTH_BACKOFF_S if exc.kind == "auth" else MOUNT_BACKOFF_S if exc.kind == "mount" else backoff
                if exc.kind not in ("auth", "mount"):
                    backoff = min(backoff * 2, BACKOFF_MAX_S)
                log.warning("NTRIP %s: %s (retry in %.0fs)", exc.kind, exc, delay)
            except (OSError, asyncio.TimeoutError, ConnectionError) as exc:
                self.status.last_error = f"{type(exc).__name__}: {exc}"
                delay = backoff * random.uniform(0.8, 1.2)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
                log.warning("NTRIP connection failed: %s (retry in %.1fs)", exc, delay)
            self._set_connected(False)
            self.status.reconnects += 1
            self.status.next_retry_s = delay
            self._publish()
            await asyncio.sleep(delay)
        self._set_connected(False)
        self._publish()

    def _set_connected(self, value: bool) -> None:
        self.status.connected = value
        self.status.since_mono = time.monotonic() if value else None

    def _publish(self) -> None:
        self.bus.publish("ntrip_client.status", self.status)

    # ------------------------------------------------------------- one session
    def _request(self, v2: bool) -> bytes:
        c = self.config
        lines = [f"GET /{c.mountpoint} HTTP/1.{1 if v2 else 0}", f"Host: {c.host}:{c.port}", f"User-Agent: NTRIP mtrtk/{__version__}", "Connection: close"]
        if v2:
            lines.append("Ntrip-Version: Ntrip/2.0")
        if c.username is not None:
            token = base64.b64encode(f"{c.username}:{c.password or ''}".encode()).decode()
            lines.append(f"Authorization: Basic {token}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode()

    async def _session(self, stop: asyncio.Event) -> None:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(self.config.host, self.config.port), 10.0)
        try:
            v2 = not self.force_v1
            writer.write(self._request(v2))
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10.0)
            status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
            headers = {k.strip().lower(): v.strip() for k, v in (ln.decode("latin-1").split(":", 1) for ln in head.split(b"\r\n")[1:] if b":" in ln)}
            if status_line.startswith("ICY 200"):
                chunked, version = False, 1
            elif status_line.startswith("HTTP/1.") and " 200" in status_line:
                chunked, version = headers.get("transfer-encoding", "").lower() == "chunked", 2 if "ntrip-version" in headers else 1
            elif status_line.startswith("SOURCETABLE 200") or " 404" in status_line:
                raise _Response("mount", f"mountpoint /{self.config.mountpoint} not found on {self.config.host}")
            elif " 401" in status_line:
                raise _Response("auth", f"401 unauthorized for user {self.config.username!r}")
            else:
                raise _Response("other", f"unexpected response: {status_line}")
            self.status.version = version
            self.status.last_error = None
            self._set_connected(True)
            self._publish()
            log.info("NTRIP connected to %s:%d/%s (v%d%s)", self.config.host, self.config.port, self.config.mountpoint, version, ", chunked" if chunked else "")
            gga_task = asyncio.create_task(self._gga_loop(writer, stop), name="ntrip-gga")
            try:
                await self._stream(reader, chunked, stop)
            finally:
                gga_task.cancel()
                await asyncio.gather(gga_task, return_exceptions=True)
        finally:
            writer.close()
            await asyncio.gather(writer.wait_closed(), return_exceptions=True)

    async def _stream(self, reader: asyncio.StreamReader, chunked: bool, stop: asyncio.Event) -> None:
        framer = Framer()
        last_status = time.monotonic()
        while not stop.is_set():
            try:
                data = await asyncio.wait_for(self._read_chunk(reader) if chunked else reader.read(4096), NO_DATA_TIMEOUT_S)
            except TimeoutError as exc:
                raise ConnectionError(f"no data for {NO_DATA_TIMEOUT_S:.0f}s") from exc
            if not data:
                raise ConnectionError("caster closed the connection")
            self.status.bytes_received += len(data)
            for frame in framer.feed(data):
                if frame.proto is not Proto.RTCM3:
                    continue
                await self.driver.inject_rtcm(frame.raw)
                self.status.frames_injected += 1
                self.status.last_rtcm_mono = time.monotonic()
            self.status.crc_dropped = framer.stats.checksum_errors
            if time.monotonic() - last_status > STATUS_INTERVAL_S:
                last_status = time.monotonic()
                self._publish()

    @staticmethod
    async def _read_chunk(reader: asyncio.StreamReader) -> bytes:
        size_line = await reader.readline()
        if not size_line:
            return b""
        size = int(size_line.split(b";")[0].strip() or b"0", 16)
        if size == 0:
            return b""
        data = await reader.readexactly(size)
        await reader.readexactly(2)  # CRLF
        return data

    async def _gga_loop(self, writer: asyncio.StreamWriter, stop: asyncio.Event) -> None:
        while not stop.is_set():
            gga = self.gga_provider()
            if gga:
                writer.write(gga if gga.endswith(b"\r\n") else gga + b"\r\n")
                await writer.drain()
            await asyncio.sleep(self.gga_interval_s)
```

- [ ] **Step 5: Run tests, lint, commit**

`uv run pytest tests/unit/test_ntrip_client.py -q` → `6 passed`. The backoff test patches `asyncio.sleep` globally for the module; if `asyncio.open_connection` itself needs the event loop's sleep internally and hangs, replace the patch target with a module-level `_sleep = asyncio.sleep` used by `run()` and patch `mod._sleep` instead (make that change in the code and the tests together).
```bash
git add src/mtrtk/rover tests/unit/test_ntrip_client.py
git commit -m "feat(rover): driver protocol, u-blox driver and NTRIP v2/v1 client with backoff and GGA upload

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: NMEA synthesis, output sinks, JSON UDP

**Files:**
- Create: `src/mtrtk/rover/nmea_out.py`, `src/mtrtk/rover/sinks.py`, `src/mtrtk/rover/json_out.py`, `tests/unit/test_nmea_out.py`, `tests/unit/test_sinks.py`
- Modify: `src/mtrtk/config.py` (add `nmea_sentences: list[str] = ["GGA","RMC","GST","GSA","GSV","VTG","ZDA"]` (CSV env), `nmea_slow_interval_s: float = 1.0`)

**Interfaces:**
- Produces: `gga_quality(fix) -> int`; builders returning `bytes` (or `None` without a position): `build_gga(state)`, `build_rmc(state)`, `build_gst(state)`, `build_vtg(state)`, `build_zda(state)`, `build_hdt(state)`, `build_pashr(state)`; list builders `build_gsa(state) -> list[bytes]`, `build_gsv(state) -> list[bytes]`; `build_sentences(state, wanted: set[str], include_slow: bool) -> list[bytes]`; `NmeaPublisher(bus, store, sinks, sentences, slow_interval_s=1.0)` with `await run(stop)`; sinks: `TcpBroadcastSink(host, port)` (`.port`, `.client_count`), `UdpSink(targets: list[tuple[str,int]])`, `SerialSink(path)` where `path == "pty"` creates a pseudo-terminal and exposes `.slave_path` (also symlinked to `DATA_DIR/ttyMTRTK` by the daemon) and a real path opens a serial port at `settings.baud`; `JsonUdpPublisher(bus, store, targets)` sending one JSON object per epoch: `{"t", "lat", "lon", "height_m", "hmsl_m", "h_acc_m", "v_acc_m", "fix_type", "carr_soln", "num_sv", "vel_n_mps", "vel_e_mps", "vel_d_mps", "heading_deg", "baseline_m", "corr_age_s", "attitude"}`.
- Talkers: GGA/RMC/GST/VTG/ZDA/HDT use `GN`; GSA/GSV per system: GPS `GP` (system id 1), GLONASS `GL` (2, sv id + 64), Galileo `GA` (3), BeiDou `GB` (4), QZSS `GQ` (5). GSV `signalID` 1.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_nmea_out.py`:
```python
from datetime import UTC, datetime

import pytest
from pynmeagps import NMEAReader

from mtrtk.core.state import Attitude, FixInfo, ReceiverState, Satellite
from mtrtk.rover.nmea_out import build_gga, build_gsa, build_gst, build_gsv, build_hdt, build_pashr, build_rmc, build_sentences, build_vtg, build_zda, gga_quality


def rover_state() -> ReceiverState:
    s = ReceiverState()
    s.time.utc = datetime(2026, 9, 18, 16, 47, 34, 120000, tzinfo=UTC)
    s.position.lat, s.position.lon, s.position.height_m, s.position.hmsl_m = 23.83735067, 90.26255021, -36.268, 13.363
    s.accuracy.h_acc_m, s.accuracy.v_acc_m = 0.012, 0.018
    s.fix = FixInfo(fix_type=3, fix_type_name="3D", gnss_fix_ok=True, diff_soln=True, carr_soln=2, carr_soln_name="RTK fixed", num_sv=27)
    s.rtk.carr_soln, s.rtk.corr_age_s, s.rtk.ref_station_id = 2, 1.2, 7
    s.dops.p, s.dops.h, s.dops.v = 1.2, 0.7, 1.0
    s.velocity.ground_speed_mps, s.velocity.heading_motion_deg = 1.5, 123.4
    s.sats = [
        Satellite(gnss_id=0, gnss="GPS", sv_id=5, cno=45, elev=72, azim=120, used=True), Satellite(gnss_id=0, gnss="GPS", sv_id=12, cno=38, elev=35, azim=210, used=True),
        Satellite(gnss_id=0, gnss="GPS", sv_id=25, cno=22, elev=8, azim=300, used=False), Satellite(gnss_id=6, gnss="GLONASS", sv_id=3, cno=40, elev=50, azim=40, used=True),
        Satellite(gnss_id=2, gnss="Galileo", sv_id=4, cno=42, elev=60, azim=180, used=True), Satellite(gnss_id=3, gnss="BeiDou", sv_id=21, cno=36, elev=44, azim=260, used=True),
    ]
    return s


def test_quality_mapping() -> None:
    assert gga_quality(FixInfo(fix_type=0)) == 0
    assert gga_quality(FixInfo(fix_type=3)) == 1
    assert gga_quality(FixInfo(fix_type=3, diff_soln=True)) == 2
    assert gga_quality(FixInfo(fix_type=3, carr_soln=1)) == 5
    assert gga_quality(FixInfo(fix_type=3, carr_soln=2)) == 4


def test_gga_high_precision_and_fields() -> None:
    raw = build_gga(rover_state())
    assert raw is not None and raw.startswith(b"$GNGGA,164734.12,2350.2410402,N,09015.7530126,E,4,27,0.7,13.363,M,-49.631,M,1.2,0007")
    m = NMEAReader.parse(raw)
    assert m.lat == pytest.approx(23.83735067, abs=1e-9) and m.lon == pytest.approx(90.26255021, abs=1e-9)
    assert m.quality == 4 and m.numSV == 27 and m.alt == 13.363 and m.sep == -49.631


def test_gga_none_without_position() -> None:
    assert build_gga(ReceiverState()) is None


def test_rmc_gst_vtg_zda() -> None:
    s = rover_state()
    rmc = NMEAReader.parse(build_rmc(s))
    assert rmc.status == "A" and str(rmc.date) == "2026-09-18" and rmc.posMode == "R" and rmc.spd == pytest.approx(1.5 * 1.943844, abs=1e-3)
    gst = NMEAReader.parse(build_gst(s))
    assert gst.stdLat == pytest.approx(0.012 / 2**0.5, abs=1e-4) and gst.stdAlt == 0.018
    vtg = NMEAReader.parse(build_vtg(s))
    assert vtg.cogt == 123.4 and vtg.sogk == pytest.approx(5.4, abs=1e-3) and vtg.posMode == "R"
    zda = build_zda(s)
    assert zda == b"$GNZDA,164734.12,18,09,2026,00,00*4F\r\n"[:-6] + zda[-6:]  # same body; checksum computed
    assert NMEAReader.parse(zda).month == 9


def test_gsa_and_gsv_per_system() -> None:
    s = rover_state()
    gsa = [NMEAReader.parse(x) for x in build_gsa(s)]
    assert [m.talker for m in gsa] == ["GP", "GL", "GA", "GB"]
    gp = gsa[0]
    assert gp.systemId == 1 and gp.svid_01 == 5 and gp.svid_02 == 12 and gp.PDOP == 1.2
    gl = gsa[1]
    assert gl.systemId == 2 and gl.svid_01 == 67  # GLONASS slot 3 -> 67
    gsv = [NMEAReader.parse(x) for x in build_gsv(s)]
    assert [m.talker for m in gsv] == ["GP", "GL", "GA", "GB"]
    assert gsv[0].numSV == 3 and gsv[0].svid_01 == 5 and gsv[0].elv_01 == 72 and gsv[0].cno_03 == 22


def test_gsv_splits_in_groups_of_four() -> None:
    s = rover_state()
    s.sats = [Satellite(gnss_id=0, gnss="GPS", sv_id=i, cno=30, elev=10, azim=i * 10, used=True) for i in range(1, 10)]
    msgs = build_gsv(s)
    assert len(msgs) == 3
    parsed = [NMEAReader.parse(m) for m in msgs]
    assert [m.msgNum for m in parsed] == [1, 2, 3] and all(m.numMsg == 3 for m in parsed) and parsed[2].svid_01 == 9


def test_attitude_sentences_only_with_attitude() -> None:
    s = rover_state()
    assert build_hdt(s) is None and build_pashr(s) is None
    s.attitude = Attitude(roll_deg=1.5, pitch_deg=-2.25, heading_deg=91.2, acc_roll_deg=0.1, acc_pitch_deg=0.1, acc_heading_deg=0.2, source="test")
    assert NMEAReader.parse(build_hdt(s)).headingT == 91.2
    pashr = build_pashr(s)
    assert pashr.startswith(b"$PASHR,164734.12,91.20,T,1.50,-2.25,0.00,0.100,0.100,0.200,2,1*")


def test_build_sentences_respects_selection_and_slow_flag() -> None:
    s = rover_state()
    fast = build_sentences(s, {"GGA", "RMC", "GSA", "GSV"}, include_slow=False)
    assert [x[3:6] for x in fast] == [b"GGA", b"RMC"]
    slow = build_sentences(s, {"GGA", "GSA", "GSV", "ZDA"}, include_slow=True)
    assert [x[3:6] for x in slow] == [b"GGA", b"GSA", b"GSA", b"GSA", b"GSA", b"GSV", b"GSV", b"GSV", b"GSV", b"ZDA"]
```

`tests/unit/test_sinks.py`:
```python
import asyncio
import os
from pathlib import Path

from mtrtk.rover.sinks import SerialSink, TcpBroadcastSink, UdpSink


async def test_tcp_broadcast_to_all_clients() -> None:
    sink = TcpBroadcastSink("127.0.0.1", 0)
    await sink.start()
    r1, w1 = await asyncio.open_connection("127.0.0.1", sink.port)
    r2, w2 = await asyncio.open_connection("127.0.0.1", sink.port)
    await asyncio.sleep(0.02)
    assert sink.client_count == 2
    await sink.write(b"$GNGGA,x*00\r\n")
    assert await asyncio.wait_for(r1.readline(), 1.0) == b"$GNGGA,x*00\r\n"
    assert await asyncio.wait_for(r2.readline(), 1.0) == b"$GNGGA,x*00\r\n"
    w1.close()
    await asyncio.sleep(0.02)
    await sink.write(b"$GNRMC,y*00\r\n")  # closed client must not raise
    assert await asyncio.wait_for(r2.readline(), 1.0) == b"$GNRMC,y*00\r\n"
    w2.close()
    await sink.close()


async def test_udp_sink_sends_to_each_target() -> None:
    loop = asyncio.get_running_loop()
    received: list[bytes] = []

    class Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[no-untyped-def]
            received.append(data)

    transport, _ = await loop.create_datagram_endpoint(Proto, local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]
    sink = UdpSink([("127.0.0.1", port), ("127.0.0.1", port)])
    await sink.start()
    await sink.write(b"hello")
    await asyncio.sleep(0.05)
    assert received == [b"hello", b"hello"]
    await sink.close()
    transport.close()


async def test_pty_sink(tmp_path: Path) -> None:
    sink = SerialSink("pty")
    await sink.start()
    assert sink.slave_path and Path(sink.slave_path).exists()
    fd = os.open(sink.slave_path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        await sink.write(b"$GNGGA,test*00\r\n")
        await asyncio.sleep(0.02)
        assert os.read(fd, 100) == b"$GNGGA,test*00\r\n"
    finally:
        os.close(fd)
        await sink.close()
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Write `src/mtrtk/rover/nmea_out.py`**

```python
"""Synthesize NMEA 0183 sentences from ReceiverState (the receiver itself emits UBX only)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable

from pynmeagps import GET, NMEAMessage

from mtrtk.core.bus import Bus
from mtrtk.core.crc import nmea_checksum
from mtrtk.core.state import FixInfo, ReceiverState, Satellite
from mtrtk.core.statestore import StateStore
from mtrtk.rover.sinks import NmeaSink

log = logging.getLogger(__name__)

MPS_TO_KNOTS = 1.943844
MPS_TO_KMH = 3.6
SYSTEM_TALKER: dict[str, tuple[str, int]] = {"GPS": ("GP", 1), "GLONASS": ("GL", 2), "Galileo": ("GA", 3), "BeiDou": ("GB", 4), "QZSS": ("GQ", 5)}
SLOW_SENTENCES = {"GSA", "GSV", "ZDA"}
ALL_SENTENCES = ["GGA", "RMC", "GST", "VTG", "ZDA", "GSA", "GSV", "HDT", "PASHR"]


def gga_quality(fix: FixInfo) -> int:
    if fix.carr_soln == 2:
        return 4
    if fix.carr_soln == 1:
        return 5
    if fix.fix_type == 0:
        return 0
    return 2 if fix.diff_soln else 1


def _nmea_sv(sat: Satellite) -> int:
    return sat.sv_id + 64 if sat.gnss == "GLONASS" else sat.sv_id


def _manual(body: str) -> bytes:
    return f"${body}*{nmea_checksum(body.encode()):02X}\r\n".encode()


def _hms(state: ReceiverState) -> str | None:
    return state.time.utc.strftime("%H%M%S.%f")[:-4] if state.time.utc else None


def build_gga(state: ReceiverState) -> bytes | None:
    p, a, f = state.position, state.accuracy, state.fix
    if p.lat is None or p.lon is None or state.time.utc is None:
        return None
    hmsl = p.hmsl_m if p.hmsl_m is not None else 0.0
    sep = (p.height_m - hmsl) if p.height_m is not None else 0.0
    age = state.rtk.corr_age_s
    msg = NMEAMessage("GN", "GGA", GET, hpnmeamode=True, time=state.time.utc.time(), lat=p.lat, NS="N" if p.lat >= 0 else "S", lon=abs(p.lon), EW="E" if p.lon >= 0 else "W",
                      quality=gga_quality(f), numSV=min(f.num_sv, 99), HDOP=round(state.dops.h or 0.0, 2), alt=round(hmsl, 3), altUnit="M", sep=round(sep, 3), sepUnit="M",
                      diffAge=round(age, 1) if age is not None else "", diffStation=f"{state.rtk.ref_station_id:04d}" if state.rtk.ref_station_id is not None else "")
    return bytes(msg.serialize())


def build_rmc(state: ReceiverState) -> bytes | None:
    p, v = state.position, state.velocity
    if p.lat is None or p.lon is None or state.time.utc is None:
        return None
    mode = {4: "R", 5: "F", 2: "D", 1: "A", 0: "N"}[gga_quality(state.fix)]
    msg = NMEAMessage("GN", "RMC", GET, hpnmeamode=True, time=state.time.utc.time(), status="A" if state.fix.fix_type else "V", lat=abs(p.lat), NS="N" if p.lat >= 0 else "S", lon=abs(p.lon), EW="E" if p.lon >= 0 else "W",
                      spd=round((v.ground_speed_mps or 0.0) * MPS_TO_KNOTS, 3), cog=round(v.heading_motion_deg or 0.0, 2), date=state.time.utc.date(), mv="", mvEW="", posMode=mode, navStatus="V")
    return bytes(msg.serialize())


def build_gst(state: ReceiverState) -> bytes | None:
    a = state.accuracy
    if a.h_acc_m is None or state.time.utc is None:
        return None
    std_h = a.h_acc_m / 2**0.5
    msg = NMEAMessage("GN", "GST", GET, time=state.time.utc.time(), rangeRms=round(a.h_acc_m, 3), stdMajor=round(std_h, 3), stdMinor=round(std_h, 3), orient=0.0, stdLat=round(std_h, 4), stdLong=round(std_h, 4), stdAlt=round(a.v_acc_m or 0.0, 4))
    return bytes(msg.serialize())


def build_vtg(state: ReceiverState) -> bytes | None:
    v = state.velocity
    if v.ground_speed_mps is None:
        return None
    mode = {4: "R", 5: "F", 2: "D", 1: "A", 0: "N"}[gga_quality(state.fix)]
    msg = NMEAMessage("GN", "VTG", GET, cogt=round(v.heading_motion_deg or 0.0, 2), cogtUnit="T", cogm="", cogmUnit="M", sogn=round(v.ground_speed_mps * MPS_TO_KNOTS, 3), sognUnit="N", sogk=round(v.ground_speed_mps * MPS_TO_KMH, 3), sogkUnit="K", posMode=mode)
    return bytes(msg.serialize())


def build_zda(state: ReceiverState) -> bytes | None:
    t = state.time.utc
    if t is None:
        return None
    return _manual(f"GNZDA,{t:%H%M%S}.{t.microsecond // 10000:02d},{t:%d},{t:%m},{t:%Y},00,00")


def build_hdt(state: ReceiverState) -> bytes | None:
    att = state.attitude
    if att is None or att.heading_deg is None:
        return None
    return bytes(NMEAMessage("GN", "HDT", GET, headingT=round(att.heading_deg, 2), headingTu="T").serialize())


def build_pashr(state: ReceiverState) -> bytes | None:
    att, t = state.attitude, state.time.utc
    if att is None or t is None or att.heading_deg is None:
        return None
    flag = 2 if state.fix.carr_soln == 2 else 1 if state.fix.fix_type else 0
    return _manual(f"PASHR,{t:%H%M%S}.{t.microsecond // 10000:02d},{att.heading_deg:.2f},T,{att.roll_deg or 0.0:.2f},{att.pitch_deg or 0.0:.2f},0.00,{att.acc_roll_deg or 0.0:.3f},{att.acc_pitch_deg or 0.0:.3f},{att.acc_heading_deg or 0.0:.3f},{flag},1")


def _by_system(sats: Iterable[Satellite]) -> dict[str, list[Satellite]]:
    groups: dict[str, list[Satellite]] = {}
    for s in sats:
        if s.gnss in SYSTEM_TALKER:
            groups.setdefault(s.gnss, []).append(s)
    return {k: groups[k] for k in SYSTEM_TALKER if k in groups}


def build_gsa(state: ReceiverState) -> list[bytes]:
    out: list[bytes] = []
    nav_mode = 3 if state.fix.fix_type >= 3 else 2 if state.fix.fix_type == 2 else 1
    for system, sats in _by_system(state.sats).items():
        talker, sys_id = SYSTEM_TALKER[system]
        used = [_nmea_sv(s) for s in sats if s.used][:12]
        kwargs = {f"svid_{i + 1:02d}": sv for i, sv in enumerate(used)}
        msg = NMEAMessage(talker, "GSA", GET, opMode="A", navMode=nav_mode, PDOP=round(state.dops.p or 0.0, 2), HDOP=round(state.dops.h or 0.0, 2), VDOP=round(state.dops.v or 0.0, 2), systemId=sys_id, **kwargs)
        out.append(bytes(msg.serialize()))
    return out


def build_gsv(state: ReceiverState) -> list[bytes]:
    out: list[bytes] = []
    for system, sats in _by_system(state.sats).items():
        talker, _ = SYSTEM_TALKER[system]
        chunks = [sats[i : i + 4] for i in range(0, len(sats), 4)]
        for n, chunk in enumerate(chunks, start=1):
            kwargs: dict[str, object] = {}
            for i, s in enumerate(chunk, start=1):
                kwargs.update({f"svid_{i:02d}": _nmea_sv(s), f"elv_{i:02d}": s.elev if s.elev is not None else "", f"az_{i:02d}": s.azim if s.azim is not None else "", f"cno_{i:02d}": s.cno or ""})
            msg = NMEAMessage(talker, "GSV", GET, numMsg=len(chunks), msgNum=n, numSV=len(sats), signalID=1, **kwargs)
            out.append(bytes(msg.serialize()))
    return out


_BUILDERS = {"GGA": build_gga, "RMC": build_rmc, "GST": build_gst, "VTG": build_vtg, "ZDA": build_zda, "HDT": build_hdt, "PASHR": build_pashr}


def build_sentences(state: ReceiverState, wanted: set[str], include_slow: bool) -> list[bytes]:
    out: list[bytes] = []
    for name in ALL_SENTENCES:
        if name not in wanted or (name in SLOW_SENTENCES and not include_slow):
            continue
        if name == "GSA":
            out += build_gsa(state)
        elif name == "GSV":
            out += build_gsv(state)
        else:
            sentence = _BUILDERS[name](state)
            if sentence:
                out.append(sentence)
    return out


class NmeaPublisher:
    def __init__(self, bus: Bus, store: StateStore, sinks: list[NmeaSink], sentences: Iterable[str], slow_interval_s: float = 1.0) -> None:
        self.bus = bus
        self.store = store
        self.sinks = sinks
        self.sentences = {s.upper() for s in sentences}
        self.slow_interval_s = slow_interval_s
        self.sub = bus.subscribe("state.epoch", maxsize=20)
        self._last_slow = 0.0
        self.sent = 0

    async def run(self, stop: asyncio.Event) -> None:
        for sink in self.sinks:
            await sink.start()
        try:
            async for _, state in self.sub:
                now = time.monotonic()
                slow = now - self._last_slow >= self.slow_interval_s
                if slow:
                    self._last_slow = now
                payload = b"".join(build_sentences(state, self.sentences, include_slow=slow))
                if payload:
                    for sink in self.sinks:
                        await sink.write(payload)
                    self.sent += 1
                if stop.is_set():
                    break
        finally:
            for sink in self.sinks:
                await sink.close()

    def stop(self) -> None:
        self.sub.close()
```

- [ ] **Step 4: Write `src/mtrtk/rover/sinks.py` and `src/mtrtk/rover/json_out.py`**

`sinks.py`:
```python
"""Where NMEA/JSON bytes go: TCP server, UDP targets, serial port or pseudo-terminal."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Protocol

from serial_asyncio_fast import open_serial_connection

log = logging.getLogger(__name__)
TCP_BACKLOG_LIMIT = 64 * 1024


class NmeaSink(Protocol):
    async def start(self) -> None: ...
    async def write(self, data: bytes) -> None: ...
    async def close(self) -> None: ...


class TcpBroadcastSink:
    def __init__(self, host: str, port: int) -> None:
        self.host, self._port = host, port
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def port(self) -> int:
        return int(self._server.sockets[0].getsockname()[1]) if self._server and self._server.sockets else self._port

    @property
    def client_count(self) -> int:
        return len(self._writers)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, self.host, self._port)
        log.info("NMEA TCP server on %s:%d", self.host, self.port)

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            while await reader.read(1024):  # ignore input; detect close
                pass
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    async def write(self, data: bytes) -> None:
        for w in list(self._writers):
            if w.is_closing() or w.transport.get_write_buffer_size() > TCP_BACKLOG_LIMIT:
                self._writers.discard(w)
                w.close()
                continue
            try:
                w.write(data)
            except (ConnectionError, RuntimeError):
                self._writers.discard(w)

    async def close(self) -> None:
        for w in list(self._writers):
            w.close()
        self._writers.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()


class UdpSink:
    def __init__(self, targets: list[tuple[str, int]]) -> None:
        self.targets = targets
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, local_addr=("0.0.0.0", 0))

    async def write(self, data: bytes) -> None:
        if self._transport is None:
            return
        for target in self.targets:
            self._transport.sendto(data, target)

    async def close(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None


class SerialSink:
    """`path == "pty"` creates a pseudo-terminal (read the slave with any NMEA consumer); otherwise a real serial port."""

    def __init__(self, path: str, baud: int = 115200) -> None:
        self.path, self.baud = path, baud
        self.slave_path: str | None = None
        self._master_fd: int | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def start(self) -> None:
        if self.path == "pty":
            master, slave = os.openpty()
            os.set_blocking(master, False)
            self._master_fd = master
            self.slave_path = os.ttyname(slave)
            log.info("NMEA pseudo-terminal at %s", self.slave_path)
        else:
            _, self._writer = await open_serial_connection(url=self.path, baudrate=self.baud)
            log.info("NMEA serial output on %s @ %d", self.path, self.baud)

    async def write(self, data: bytes) -> None:
        if self._master_fd is not None:
            try:
                os.write(self._master_fd, data)
            except BlockingIOError:
                pass  # nobody is reading the pty; drop
        elif self._writer is not None:
            self._writer.write(data)
            await self._writer.drain()

    async def close(self) -> None:
        if self._master_fd is not None:
            os.close(self._master_fd)
            self._master_fd = None
        if self._writer is not None:
            self._writer.close()
            self._writer = None
```

`json_out.py`:
```python
"""One compact JSON datagram per epoch for ROS bridges and custom consumers."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mtrtk.core.bus import Bus
from mtrtk.core.state import ReceiverState
from mtrtk.rover.sinks import UdpSink


def epoch_json(state: ReceiverState) -> dict[str, Any]:
    p, a, f, v, r = state.position, state.accuracy, state.fix, state.velocity, state.rtk
    return {
        "t": state.time.utc.isoformat() if state.time.utc else None,
        "lat": p.lat, "lon": p.lon, "height_m": p.height_m, "hmsl_m": p.hmsl_m,
        "h_acc_m": a.h_acc_m, "v_acc_m": a.v_acc_m,
        "fix_type": f.fix_type, "carr_soln": f.carr_soln, "num_sv": f.num_sv,
        "vel_n_mps": v.vel_n_mps, "vel_e_mps": v.vel_e_mps, "vel_d_mps": v.vel_d_mps, "heading_deg": v.heading_motion_deg,
        "baseline_m": r.baseline_m, "corr_age_s": r.corr_age_s,
        "attitude": state.attitude.model_dump() if state.attitude else None,
    }


class JsonUdpPublisher:
    def __init__(self, bus: Bus, targets: list[tuple[str, int]]) -> None:
        self.sink = UdpSink(targets)
        self.sub = bus.subscribe("state.epoch", maxsize=20)

    async def run(self, stop: asyncio.Event) -> None:
        await self.sink.start()
        try:
            async for _, state in self.sub:
                await self.sink.write(json.dumps(epoch_json(state), separators=(",", ":")).encode())
                if stop.is_set():
                    break
        finally:
            await self.sink.close()

    def stop(self) -> None:
        self.sub.close()
```

- [ ] **Step 5: Add the settings, run tests, lint, commit**

In `config.py`: `nmea_sentences: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["GGA", "RMC", "GST", "GSA", "GSV", "VTG", "ZDA"])` (add to the CSV validator list) and `nmea_slow_interval_s: float = 1.0`.
`uv run pytest tests/unit/test_nmea_out.py tests/unit/test_sinks.py -q` → all pass. If `NMEAMessage` rejects `diffAge=""` for an empty field, pass `None`/omit the kwarg instead and adjust the GGA prefix assertion (the trailing `,,` fields).
```bash
git add src/mtrtk/rover src/mtrtk/config.py tests/unit/test_nmea_out.py tests/unit/test_sinks.py
git commit -m "feat(rover): NMEA synthesis with high-precision fields, TCP/UDP/serial sinks and JSON UDP output

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Sessions, point averaging and exports

**Files:**
- Create: `src/mtrtk/rover/sessions.py`, `src/mtrtk/rover/points.py`, `src/mtrtk/rover/exports.py`, `tests/unit/test_points.py`, `tests/unit/test_exports.py`
- Modify: `src/mtrtk/store/models.py` (`Session`, `Point`), `src/mtrtk/config.py` (`point_epochs: int = 30`, `point_fixed_only: bool = True`)

**Interfaces:**
- Produces: `Session(id, name, start_utc, end_utc, role, notes)`; `Point(id, session_id, name, code, note, ts_utc, lat, lon, height_m, hmsl_m, n_epochs, sd_n, sd_e, sd_u, fix_type, carr_soln, h_acc_m, v_acc_m)`; `SessionsRepo(db)`: `start(name, role, notes=None) -> Session` (closes an open session first), `stop() -> Session | None`, `current() -> Session | None`, `list(limit=100)`; `PointsRepo(db)`: `add(point) -> Point`, `list(session_id=None, limit=1000)`, `get(id)`, `update(id, name=None, code=None, note=None)`, `delete(id)`; `CollectStatus(state: "idle"|"collecting"|"done"|"aborted", name, target, accepted, skipped, sd_n, sd_e, sd_u, mean_lat, mean_lon, mean_h, point_id, reason)`; `PointCollector(bus, store, points, sessions, *, default_epochs=30, default_fixed_only=True)` with `start(name, code=None, note=None, epochs=None, fixed_only=None)`, `cancel()`, `on_epoch(state)`, `status`, `await run(stop)`; publishes `points.progress` (`CollectStatus`) on every accepted epoch and on completion, `points.saved` (`Point`) when stored; exports `to_csv(points) -> str`, `to_geojson(points) -> dict`, `to_kml(points) -> str`, `to_gpx(points) -> str`.
- Averaging: ENU offsets relative to the first accepted epoch's LLH (`geo.ecef_to_enu` + `llh_to_ecef`); `sd_*` are sample standard deviations (`n-1`, `0.0` for one epoch); the mean LLH is the ENU mean mapped back (`ecef_to_llh` of mean ECEF).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_points.py`:
```python
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mtrtk.core.bus import Bus
from mtrtk.core.state import ReceiverState
from mtrtk.core.statestore import StateStore
from mtrtk.rover.points import PointCollector, PointsRepo
from mtrtk.rover.sessions import SessionsRepo
from mtrtk.store.db import Database

T0 = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)


def epoch(i: int, lat_off: float = 0.0, carr: int = 2) -> ReceiverState:
    s = ReceiverState()
    s.time.utc = T0 + timedelta(seconds=i)
    s.position.lat, s.position.lon, s.position.height_m, s.position.hmsl_m = 23.8373506 + lat_off, 90.2625502, -36.268, 13.363
    s.accuracy.h_acc_m, s.accuracy.v_acc_m = 0.012, 0.018
    s.fix.fix_type, s.fix.carr_soln = 3, carr
    return s


@pytest.fixture
async def env(tmp_path: Path):
    db = Database(tmp_path / "m.db")
    await db.open()
    bus = Bus()
    store = StateStore(bus)
    collector = PointCollector(bus, store, PointsRepo(db), SessionsRepo(db), default_epochs=5, default_fixed_only=True)
    try:
        yield collector, bus, db
    finally:
        await db.close()


async def test_sessions_start_stop_current(env) -> None:
    _, _, db = env
    repo = SessionsRepo(db)
    a = await repo.start("field-1", "rover")
    assert (await repo.current()).id == a.id and a.end_utc is None
    b = await repo.start("field-2", "rover", notes="second")
    assert (await repo.current()).id == b.id
    assert (await repo.list())[1].end_utc is not None  # field-1 was closed automatically
    stopped = await repo.stop()
    assert stopped is not None and stopped.id == b.id and await repo.current() is None


async def test_collect_averages_and_stores_point(env) -> None:
    collector, bus, db = env
    progress = bus.subscribe("points.*")
    session = await SessionsRepo(db).start("s", "rover")
    await collector.start("BM-1", code="BM", note="brass disk", epochs=5)
    offsets = [0.0, 1e-7, -1e-7, 2e-7, -2e-7]  # ~ ±1-2 cm north
    for i, off in enumerate(offsets):
        await collector.on_epoch(epoch(i, lat_off=off))
    st = collector.status
    assert st.state == "done" and st.accepted == 5 and st.point_id is not None
    points = await PointsRepo(db).list()
    assert len(points) == 1
    p = points[0]
    assert p.name == "BM-1" and p.code == "BM" and p.session_id == session.id and p.n_epochs == 5
    assert p.lat == pytest.approx(23.8373506, abs=1e-8) and p.height_m == pytest.approx(-36.268, abs=1e-4)
    assert 0.01 < p.sd_n < 0.03 and p.sd_e == pytest.approx(0.0, abs=1e-6) and p.carr_soln == 2 and p.h_acc_m == 0.012
    kinds = [t for t, _ in [progress.queue.get_nowait() for _ in range(progress.queue.qsize())]]
    assert kinds.count("points.progress") >= 5 and kinds[-1] == "points.saved"


async def test_fixed_only_skips_float_epochs_and_aborts_after_too_many(env) -> None:
    collector, bus, db = env
    await collector.start("x", epochs=2, fixed_only=True)
    await collector.on_epoch(epoch(0, carr=1))
    await collector.on_epoch(epoch(1, carr=2))
    assert collector.status.accepted == 1 and collector.status.skipped == 1
    for i in range(2, 13):
        await collector.on_epoch(epoch(i, carr=1))
    assert collector.status.state == "aborted" and "fixed" in collector.status.reason.lower()
    assert await PointsRepo(db).list() == []


async def test_cancel_and_restart(env) -> None:
    collector, *_ = env
    await collector.start("a", epochs=3)
    await collector.on_epoch(epoch(0))
    collector.cancel()
    assert collector.status.state == "aborted" and collector.status.reason == "cancelled"
    await collector.start("b", epochs=1, fixed_only=False)
    await collector.on_epoch(epoch(1, carr=0))
    assert collector.status.state == "done"


async def test_start_while_collecting_is_an_error(env) -> None:
    collector, *_ = env
    await collector.start("a", epochs=3)
    with pytest.raises(RuntimeError, match="already"):
        await collector.start("b")


async def test_points_repo_update_delete(env) -> None:
    collector, _, db = env
    await collector.start("p", epochs=1, fixed_only=False)
    await collector.on_epoch(epoch(0))
    repo = PointsRepo(db)
    p = (await repo.list())[0]
    updated = await repo.update(p.id, name="P1", note="renamed")
    assert updated.name == "P1" and updated.note == "renamed" and updated.code == p.code
    await repo.delete(p.id)
    assert await repo.get(p.id) is None


async def test_run_consumes_epochs_from_bus(env) -> None:
    collector, bus, _ = env
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(stop))
    await collector.start("bus", epochs=2, fixed_only=False)
    bus.publish("state.epoch", epoch(0))
    bus.publish("state.epoch", epoch(1))
    await asyncio.sleep(0.05)
    assert collector.status.state == "done"
    collector.stop()
    await asyncio.wait_for(task, 1.0)
```

`tests/unit/test_exports.py`:
```python
import csv
import io
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from mtrtk.rover.exports import to_csv, to_geojson, to_gpx, to_kml
from mtrtk.store.models import Point

P = [
    Point(id=1, session_id=1, name="BM-1", code="BM", note="brass disk", ts_utc=datetime(2026, 9, 18, 16, 0, tzinfo=UTC), lat=23.8373506, lon=90.2625502, height_m=-36.268, hmsl_m=13.363, n_epochs=30, sd_n=0.004, sd_e=0.003, sd_u=0.009, fix_type=3, carr_soln=2, h_acc_m=0.012, v_acc_m=0.018),
    Point(id=2, session_id=1, name="Fence,corner", code=None, note=None, ts_utc=datetime(2026, 9, 18, 16, 5, tzinfo=UTC), lat=23.8374, lon=90.2626, height_m=-36.1, hmsl_m=13.5, n_epochs=10, sd_n=0.02, sd_e=0.02, sd_u=0.05, fix_type=3, carr_soln=1, h_acc_m=0.3, v_acc_m=0.5),
]


def test_csv() -> None:
    rows = list(csv.DictReader(io.StringIO(to_csv(P))))
    assert rows[0]["name"] == "BM-1" and rows[0]["lat"] == "23.837350600" and rows[0]["carr_soln"] == "RTK fixed"
    assert rows[1]["name"] == "Fence,corner" and rows[1]["code"] == ""
    assert set(rows[0]) >= {"id", "name", "code", "note", "time_utc", "lat", "lon", "height_m", "hmsl_m", "n_epochs", "sd_n_m", "sd_e_m", "sd_u_m", "fix", "carr_soln", "h_acc_m", "v_acc_m"}


def test_geojson() -> None:
    gj = to_geojson(P)
    assert gj["type"] == "FeatureCollection" and len(gj["features"]) == 2
    f = gj["features"][0]
    assert f["geometry"] == {"type": "Point", "coordinates": [90.2625502, 23.8373506, -36.268]}
    assert f["properties"]["name"] == "BM-1" and f["properties"]["sd_u_m"] == 0.009


def test_kml_and_gpx_are_valid_xml() -> None:
    kml = ET.fromstring(to_kml(P))
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    placemarks = kml.findall(".//k:Placemark", ns)
    assert len(placemarks) == 2 and placemarks[0].find("k:name", ns).text == "BM-1"
    assert placemarks[0].find(".//k:coordinates", ns).text.strip() == "90.2625502,23.8373506,13.363"
    gpx = ET.fromstring(to_gpx(P))
    wpts = gpx.findall(".//{http://www.topografix.com/GPX/1/1}wpt")
    assert len(wpts) == 2 and wpts[0].get("lat") == "23.8373506" and wpts[0].find("{http://www.topografix.com/GPX/1/1}ele").text == "13.363"
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Models and settings**

Append to `src/mtrtk/store/models.py`:
```python
class Session(BaseModel):
    id: int | None = None
    name: str | None = None
    start_utc: datetime
    end_utc: datetime | None = None
    role: str | None = None
    notes: str | None = None


class Point(BaseModel):
    id: int | None = None
    session_id: int | None = None
    name: str
    code: str | None = None
    note: str | None = None
    ts_utc: datetime
    lat: float
    lon: float
    height_m: float
    hmsl_m: float | None = None
    n_epochs: int
    sd_n: float
    sd_e: float
    sd_u: float
    fix_type: int
    carr_soln: int
    h_acc_m: float | None = None
    v_acc_m: float | None = None
```
`config.py`: `point_epochs: int = Field(30, ge=1, le=3600)`, `point_fixed_only: bool = True`.

- [ ] **Step 4: Write `src/mtrtk/rover/sessions.py`**

```python
"""Field sessions: named time ranges that group points and raw logs."""

from __future__ import annotations

from datetime import UTC, datetime

from mtrtk.store.db import Database
from mtrtk.store.models import Session


def _row(r) -> Session:  # type: ignore[no-untyped-def]
    d = dict(r)
    d["start_utc"] = datetime.fromisoformat(d["start_utc"])
    if d.get("end_utc"):
        d["end_utc"] = datetime.fromisoformat(d["end_utc"])
    return Session(**d)


class SessionsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def current(self) -> Session | None:
        row = await self.db.fetchone("SELECT * FROM sessions WHERE end_utc IS NULL ORDER BY id DESC LIMIT 1")
        return _row(row) if row else None

    async def start(self, name: str | None, role: str, notes: str | None = None) -> Session:
        await self.stop()
        now = datetime.now(UTC).isoformat()
        cur = await self.db.execute("INSERT INTO sessions (name, start_utc, role, notes) VALUES (?,?,?,?)", (name, now, role, notes))
        await self.db.commit()
        row = await self.db.fetchone("SELECT * FROM sessions WHERE id = ?", (cur.lastrowid,))
        assert row is not None
        return _row(row)

    async def stop(self) -> Session | None:
        current = await self.current()
        if current is None:
            return None
        await self.db.execute("UPDATE sessions SET end_utc = ? WHERE id = ?", (datetime.now(UTC).isoformat(), current.id))
        await self.db.commit()
        row = await self.db.fetchone("SELECT * FROM sessions WHERE id = ?", (current.id,))
        return _row(row) if row else None

    async def list(self, limit: int = 100) -> list[Session]:
        rows = await self.db.fetchall("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,))
        return [_row(r) for r in rows]
```

- [ ] **Step 5: Write `src/mtrtk/rover/points.py`**

```python
"""Averaged survey points collected over N epochs, optionally RTK-fixed only."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from mtrtk.core.bus import Bus
from mtrtk.core.geo import ecef_to_enu, ecef_to_llh, llh_to_ecef
from mtrtk.core.state import ReceiverState
from mtrtk.core.statestore import StateStore
from mtrtk.rover.sessions import SessionsRepo
from mtrtk.store.db import Database
from mtrtk.store.models import Point

log = logging.getLogger(__name__)
ABORT_SKIP_FACTOR = 5


class CollectStatus(BaseModel):
    state: Literal["idle", "collecting", "done", "aborted"] = "idle"
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


class PointsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _row(r) -> Point:  # type: ignore[no-untyped-def]
        d = dict(r)
        d["ts_utc"] = datetime.fromisoformat(d["ts_utc"])
        return Point(**d)

    async def add(self, p: Point) -> Point:
        cur = await self.db.execute(
            """INSERT INTO points (session_id, name, code, note, ts_utc, lat, lon, height_m, hmsl_m, n_epochs, sd_n, sd_e, sd_u, fix_type, carr_soln, h_acc_m, v_acc_m)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (p.session_id, p.name, p.code, p.note, p.ts_utc.isoformat(), p.lat, p.lon, p.height_m, p.hmsl_m, p.n_epochs, p.sd_n, p.sd_e, p.sd_u, p.fix_type, p.carr_soln, p.h_acc_m, p.v_acc_m),
        )
        await self.db.commit()
        stored = await self.get(int(cur.lastrowid or 0))
        assert stored is not None
        return stored

    async def get(self, point_id: int) -> Point | None:
        row = await self.db.fetchone("SELECT * FROM points WHERE id = ?", (point_id,))
        return self._row(row) if row else None

    async def list(self, session_id: int | None = None, limit: int = 1000) -> list[Point]:
        if session_id is None:
            rows = await self.db.fetchall("SELECT * FROM points ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = await self.db.fetchall("SELECT * FROM points WHERE session_id = ? ORDER BY id DESC LIMIT ?", (session_id, limit))
        return [self._row(r) for r in rows]

    async def update(self, point_id: int, name: str | None = None, code: str | None = None, note: str | None = None) -> Point:
        fields = {k: v for k, v in (("name", name), ("code", code), ("note", note)) if v is not None}
        if fields:
            assignments = ", ".join(f"{k} = ?" for k in fields)
            await self.db.execute(f"UPDATE points SET {assignments} WHERE id = ?", [*fields.values(), point_id])
            await self.db.commit()
        point = await self.get(point_id)
        if point is None:
            raise KeyError(point_id)
        return point

    async def delete(self, point_id: int) -> None:
        await self.db.execute("DELETE FROM points WHERE id = ?", (point_id,))
        await self.db.commit()


def _sd(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


class PointCollector:
    def __init__(self, bus: Bus, store: StateStore, points: PointsRepo, sessions: SessionsRepo, *, default_epochs: int = 30, default_fixed_only: bool = True) -> None:
        self.bus, self.store, self.points, self.sessions = bus, store, points, sessions
        self.default_epochs, self.default_fixed_only = default_epochs, default_fixed_only
        self.status = CollectStatus()
        self.sub = bus.subscribe("state.epoch", maxsize=20)
        self._reset()

    def _reset(self) -> None:
        self._code: str | None = None
        self._note: str | None = None
        self._fixed_only = self.default_fixed_only
        self._ref: tuple[float, float, float] | None = None
        self._e: list[float] = []
        self._n: list[float] = []
        self._u: list[float] = []
        self._xyz: list[tuple[float, float, float]] = []
        self._hmsl: list[float] = []
        self._last: ReceiverState | None = None

    async def start(self, name: str, code: str | None = None, note: str | None = None, epochs: int | None = None, fixed_only: bool | None = None) -> CollectStatus:
        if self.status.state == "collecting":
            raise RuntimeError("already collecting a point; cancel it first")
        self._reset()
        self._code, self._note = code, note
        self._fixed_only = self.default_fixed_only if fixed_only is None else fixed_only
        self.status = CollectStatus(state="collecting", name=name, target=epochs or self.default_epochs)
        self.bus.publish("points.progress", self.status)
        return self.status

    def cancel(self) -> None:
        if self.status.state == "collecting":
            self.status.state, self.status.reason = "aborted", "cancelled"
            self.bus.publish("points.progress", self.status)

    async def on_epoch(self, state: ReceiverState) -> None:
        if self.status.state != "collecting":
            return
        p = state.position
        if p.lat is None or p.lon is None or p.height_m is None or state.fix.fix_type < 2:
            return
        if self._fixed_only and state.fix.carr_soln != 2:
            self.status.skipped += 1
            if self.status.skipped >= ABORT_SKIP_FACTOR * self.status.target:
                self.status.state, self.status.reason = "aborted", f"no RTK fixed epochs in {self.status.skipped} tries"
            self.bus.publish("points.progress", self.status)
            return
        xyz = llh_to_ecef(p.lat, p.lon, p.height_m)
        if self._ref is None:
            self._ref = (p.lat, p.lon, p.height_m)
        e, n, u = ecef_to_enu(*self._ref, *xyz)
        self._e.append(e)
        self._n.append(n)
        self._u.append(u)
        self._xyz.append(xyz)
        if p.hmsl_m is not None:
            self._hmsl.append(p.hmsl_m)
        self._last = state
        self.status.accepted += 1
        self.status.sd_n, self.status.sd_e, self.status.sd_u = _sd(self._n), _sd(self._e), _sd(self._u)
        mx = sum(v[0] for v in self._xyz) / len(self._xyz)
        my = sum(v[1] for v in self._xyz) / len(self._xyz)
        mz = sum(v[2] for v in self._xyz) / len(self._xyz)
        self.status.mean_lat, self.status.mean_lon, self.status.mean_h = ecef_to_llh(mx, my, mz)
        if self.status.accepted >= self.status.target:
            await self._finish()
        self.bus.publish("points.progress", self.status)

    async def _finish(self) -> None:
        assert self._last is not None and self.status.mean_lat is not None
        session = await self.sessions.current()
        point = Point(
            session_id=session.id if session else None, name=self.status.name or "point", code=self._code, note=self._note,
            ts_utc=self._last.time.utc or datetime.now(UTC), lat=self.status.mean_lat, lon=self.status.mean_lon or 0.0, height_m=self.status.mean_h or 0.0,
            hmsl_m=(sum(self._hmsl) / len(self._hmsl)) if self._hmsl else None, n_epochs=self.status.accepted,
            sd_n=self.status.sd_n or 0.0, sd_e=self.status.sd_e or 0.0, sd_u=self.status.sd_u or 0.0,
            fix_type=self._last.fix.fix_type, carr_soln=self._last.fix.carr_soln, h_acc_m=self._last.accuracy.h_acc_m, v_acc_m=self._last.accuracy.v_acc_m,
        )
        stored = await self.points.add(point)
        self.status.state, self.status.point_id = "done", stored.id
        self.bus.publish("points.saved", stored)

    def stop(self) -> None:
        self.sub.close()

    async def run(self, stop: asyncio.Event) -> None:
        async for _, state in self.sub:
            try:
                await self.on_epoch(state)
            except Exception:
                log.exception("point collection failed")
                self.status.state, self.status.reason = "aborted", "internal error"
            if stop.is_set():
                break
```

- [ ] **Step 6: Write `src/mtrtk/rover/exports.py`**

```python
"""Point exports: CSV, GeoJSON, KML, GPX."""

from __future__ import annotations

import csv
import io
from typing import Any
from xml.sax.saxutils import escape

from mtrtk.core.state import CARR_SOLN_NAMES, FIX_TYPE_NAMES
from mtrtk.store.models import Point

CSV_FIELDS = ["id", "name", "code", "note", "time_utc", "lat", "lon", "height_m", "hmsl_m", "n_epochs", "sd_n_m", "sd_e_m", "sd_u_m", "fix", "carr_soln", "h_acc_m", "v_acc_m", "session_id"]


def _row(p: Point) -> dict[str, Any]:
    return {
        "id": p.id, "name": p.name, "code": p.code or "", "note": p.note or "", "time_utc": p.ts_utc.isoformat(),
        "lat": f"{p.lat:.9f}", "lon": f"{p.lon:.9f}", "height_m": f"{p.height_m:.4f}", "hmsl_m": "" if p.hmsl_m is None else f"{p.hmsl_m:.4f}",
        "n_epochs": p.n_epochs, "sd_n_m": f"{p.sd_n:.4f}", "sd_e_m": f"{p.sd_e:.4f}", "sd_u_m": f"{p.sd_u:.4f}",
        "fix": FIX_TYPE_NAMES.get(p.fix_type, str(p.fix_type)), "carr_soln": CARR_SOLN_NAMES.get(p.carr_soln, str(p.carr_soln)),
        "h_acc_m": "" if p.h_acc_m is None else f"{p.h_acc_m:.4f}", "v_acc_m": "" if p.v_acc_m is None else f"{p.v_acc_m:.4f}", "session_id": p.session_id or "",
    }


def to_csv(points: list[Point]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for p in points:
        writer.writerow(_row(p))
    return buf.getvalue()


def to_geojson(points: list[Point]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [p.lon, p.lat, p.height_m]}, "properties": {**{k: v for k, v in p.model_dump(mode="json").items() if k not in ("lat", "lon", "height_m")}, "sd_n_m": p.sd_n, "sd_e_m": p.sd_e, "sd_u_m": p.sd_u}}
            for p in points
        ],
    }


def to_kml(points: list[Point]) -> str:
    marks = []
    for p in points:
        alt = p.hmsl_m if p.hmsl_m is not None else p.height_m
        desc = escape(f"{p.code or ''} {p.note or ''}".strip() + f" · {p.n_epochs} epochs · σ {p.sd_n:.3f}/{p.sd_e:.3f}/{p.sd_u:.3f} m · {CARR_SOLN_NAMES.get(p.carr_soln, p.carr_soln)}")
        marks.append(f"<Placemark><name>{escape(p.name)}</name><description>{desc}</description><TimeStamp><when>{p.ts_utc.isoformat()}</when></TimeStamp><Point><altitudeMode>absolute</altitudeMode><coordinates>{p.lon},{p.lat},{alt}</coordinates></Point></Placemark>")
    return '<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>mtrtk points</name>' + "".join(marks) + "</Document></kml>\n"


def to_gpx(points: list[Point]) -> str:
    wpts = []
    for p in points:
        ele = p.hmsl_m if p.hmsl_m is not None else p.height_m
        wpts.append(f'<wpt lat="{p.lat}" lon="{p.lon}"><ele>{ele}</ele><time>{p.ts_utc.isoformat()}</time><name>{escape(p.name)}</name>' + (f"<desc>{escape(p.note)}</desc>" if p.note else "") + (f"<type>{escape(p.code)}</type>" if p.code else "") + "</wpt>")
    return '<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="mtrtk" xmlns="http://www.topografix.com/GPX/1/1">' + "".join(wpts) + "</gpx>\n"
```

- [ ] **Step 7: Run tests, lint, commit**

`uv run pytest tests/unit/test_points.py tests/unit/test_exports.py -q` → all pass.
```bash
git add src/mtrtk/rover src/mtrtk/store/models.py src/mtrtk/config.py tests/unit/test_points.py tests/unit/test_exports.py
git commit -m "feat(rover): field sessions, RTK-fixed point averaging and CSV/GeoJSON/KML/GPX exports

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Rover API, WebSocket topics, alert rules, sampling

**Files:**
- Create: `src/mtrtk/web/api/rover.py`, `tests/unit/test_web_rover.py`
- Modify: `src/mtrtk/web/ws.py` (`rtk` in the epoch bundle; `survey` topic), `src/mtrtk/alerts.py` (rover rules), `src/mtrtk/store/sampler.py` (`corr_age_s`, `baseline_m`), `src/mtrtk/web/app.py` (router list), `src/mtrtk/web/context.py` (`rover` accessor), `tests/unit/test_alerts.py`, `tests/unit/test_sampler.py`, `tests/unit/test_web_ws.py` (additions)

**Interfaces:**
- Produces: `AppContext.rover` → `daemon.rover` (a `RoverServices` namespace built in Task 6: `driver, ntrip_client, collector, sessions_repo, points_repo, nmea, json_udp, set_ntrip_url(url)`) or `None`; endpoints: `GET /api/rover` → `{role, driver:{name, capabilities}, ntrip: NtripClientStatus|null, rtk: RtkStatus, outputs:{nmea_tcp:{port, clients}|null, nmea_udp:[...], nmea_serial: path|null, json_udp: port|null, sentences:[...]}, session: Session|null, collect: CollectStatus}`; `PUT /api/rover/ntrip {"url"}` (validates, writes `NTRIP_URL` to `.env`, restarts the client live); `GET /api/rover/sessions`, `POST /api/rover/sessions {"name","notes"}`, `POST /api/rover/sessions/stop`; `GET /api/rover/points?session_id=`, `PATCH /api/rover/points/{id} {"name","code","note"}`, `DELETE /api/rover/points/{id}`, `GET /api/rover/points/export?fmt=csv|geojson|kml|gpx&session_id=`; `POST /api/rover/collect {"name","code","note","epochs","fixed_only"}` → `CollectStatus`, `DELETE /api/rover/collect` (cancel), `GET /api/rover/collect`. 409 when the daemon is not in the rover role.
- WebSocket: `EPOCH_TOPICS` gains `rtk` (epoch message key `rtk` = `state.rtk` JSON); `TOPICS` gains `survey`; `BUS_TO_TOPIC` gains `ntrip_client.status → rtk`, `state.time_mark → rtk`, `points.progress → survey`, `points.saved → survey`.
- Alerts (rover): `ntrip_client.status` not connected with `last_error` → `ntrip_disconnected` (warning), cleared when connected; `state.rtk.corr_age_s > 10` → `corrections_stale` (warning; message includes the age), cleared under 5 s; RTK fixed lost (carr_soln was 2, now < 2 for ≥ 10 s) → `rtk_lost` (warning), cleared when fixed again.
- Sampler: `corr_age_s = state.rtk.corr_age_s`, `baseline_m = state.rtk.baseline_m`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_web_rover.py`:
```python
import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from webtest import client, make_ctx

from mtrtk.core.state import ReceiverState
from mtrtk.rover.drivers.base import DriverCapabilities
from mtrtk.rover.ntrip_client import NtripClientStatus
from mtrtk.rover.points import PointCollector, PointsRepo
from mtrtk.rover.sessions import SessionsRepo
from mtrtk.web.app import create_app


def epoch(i: int) -> ReceiverState:
    s = ReceiverState()
    s.time.utc = datetime(2026, 9, 18, 16, 0, i, tzinfo=UTC)
    s.position.lat, s.position.lon, s.position.height_m = 23.8373506, 90.2625502, -36.268
    s.fix.fix_type, s.fix.carr_soln = 3, 2
    return s


@pytest.fixture
async def ctx(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("ROLE=rover\n")
    c = await make_ctx(tmp_path, role="rover", mtrtk_env_file=env)
    sessions, points = SessionsRepo(c.db), PointsRepo(c.db)
    collector = PointCollector(c.bus, c.store, points, sessions, default_epochs=2, default_fixed_only=True)
    urls: list[str] = []

    async def set_ntrip_url(url: str) -> None:
        urls.append(url)

    c.daemon.rover = SimpleNamespace(
        driver=SimpleNamespace(name="ublox", capabilities=DriverCapabilities(True, True, False, False, True, True)),
        ntrip_client=SimpleNamespace(status=NtripClientStatus(connected=True, host="base", port=2101, mountpoint="MTRK", version=2, bytes_received=5000, frames_injected=40)),
        collector=collector, sessions_repo=sessions, points_repo=points,
        nmea=SimpleNamespace(sinks=[SimpleNamespace(port=10110, client_count=1)], sentences={"GGA", "RMC"}), json_udp=None, set_ntrip_url=set_ntrip_url, urls=urls,
    )
    try:
        yield c
    finally:
        await c.db.close()


async def test_get_rover_overview(ctx) -> None:
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/rover")).json()
    assert body["role"] == "rover" and body["driver"]["name"] == "ublox" and body["driver"]["capabilities"]["accepts_rtcm"] is True
    assert body["ntrip"]["connected"] is True and body["ntrip"]["frames_injected"] == 40
    assert body["outputs"]["nmea_tcp"] == {"port": 10110, "clients": 1} and sorted(body["outputs"]["sentences"]) == ["GGA", "RMC"]
    assert body["collect"]["state"] == "idle" and body["session"] is None


async def test_put_ntrip_url_validates_persists_and_applies(ctx) -> None:
    async with client(create_app(ctx)) as c:
        bad = await c.put("/api/rover/ntrip", json={"url": "nonsense"})
        assert bad.status_code == 422
        ok = await c.put("/api/rover/ntrip", json={"url": "ntrip://rover:pw@100.100.50.10:2101/MTRK"})
    assert ok.status_code == 200 and ctx.daemon.rover.urls == ["ntrip://rover:pw@100.100.50.10:2101/MTRK"]
    assert "NTRIP_URL=ntrip://rover:pw@100.100.50.10:2101/MTRK" in ctx.settings.mtrtk_env_file.read_text()


async def test_sessions_and_collect_flow(ctx) -> None:
    async with client(create_app(ctx)) as c:
        s = await c.post("/api/rover/sessions", json={"name": "field-1", "notes": "test"})
        assert s.status_code == 200 and s.json()["name"] == "field-1"
        r = await c.post("/api/rover/collect", json={"name": "BM-1", "code": "BM", "epochs": 2, "fixed_only": True})
        assert r.status_code == 200 and r.json()["state"] == "collecting" and r.json()["target"] == 2
        assert (await c.post("/api/rover/collect", json={"name": "again"})).status_code == 409
        await ctx.daemon.rover.collector.on_epoch(epoch(0))
        await ctx.daemon.rover.collector.on_epoch(epoch(1))
        assert (await c.get("/api/rover/collect")).json()["state"] == "done"
        points = (await c.get("/api/rover/points")).json()
        assert len(points) == 1 and points[0]["name"] == "BM-1" and points[0]["session_id"] == s.json()["id"]
        pid = points[0]["id"]
        upd = await c.patch(f"/api/rover/points/{pid}", json={"note": "brass disk"})
        assert upd.json()["note"] == "brass disk"
        csv_r = await c.get("/api/rover/points/export", params={"fmt": "csv"})
        assert csv_r.status_code == 200 and csv_r.text.startswith("id,name,code") and "BM-1" in csv_r.text and "attachment" in csv_r.headers["content-disposition"]
        gj = await c.get("/api/rover/points/export", params={"fmt": "geojson", "session_id": s.json()["id"]})
        assert gj.json()["features"][0]["properties"]["name"] == "BM-1"
        assert (await c.get("/api/rover/points/export", params={"fmt": "xlsx"})).status_code == 422
        stop = await c.post("/api/rover/sessions/stop")
        assert stop.json()["end_utc"] is not None
        assert (await c.delete(f"/api/rover/points/{pid}")).json() == {"ok": True}
        assert (await c.get("/api/rover/points")).json() == []


async def test_cancel_collect(ctx) -> None:
    async with client(create_app(ctx)) as c:
        await c.post("/api/rover/collect", json={"name": "x", "epochs": 5})
        r = await c.delete("/api/rover/collect")
    assert r.json()["state"] == "aborted" and r.json()["reason"] == "cancelled"


async def test_rover_endpoints_409_for_base_role(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path)
    try:
        async with client(create_app(ctx)) as c:
            assert (await c.get("/api/rover")).status_code == 409
            assert (await c.post("/api/rover/collect", json={"name": "x"})).status_code == 409
    finally:
        await ctx.db.close()
```

Additions to `tests/unit/test_alerts.py`:
```python
async def test_rover_rules(env) -> None:
    from mtrtk.core.state import RtkStatus
    from mtrtk.rover.ntrip_client import NtripClientStatus

    engine, sub, _, clock, _ = env
    await engine.handle("ntrip_client.status", NtripClientStatus(connected=False, last_error="401 unauthorized"))
    assert kinds(sub) == ["ntrip_disconnected"]
    await engine.handle("ntrip_client.status", NtripClientStatus(connected=True))
    assert kinds(sub) == ["ntrip_disconnected_cleared"]
    await engine.handle("state.rtk", RtkStatus(carr_soln=2, corr_age_s=1.0))
    await engine.handle("state.rtk", RtkStatus(carr_soln=2, corr_age_s=12.0))
    assert kinds(sub) == ["corrections_stale"]
    await engine.handle("state.rtk", RtkStatus(carr_soln=1, corr_age_s=2.0))
    assert kinds(sub) == ["corrections_stale_cleared"]
    clock.t += 11
    await engine.handle("state.rtk", RtkStatus(carr_soln=1, corr_age_s=2.0))
    assert kinds(sub) == ["rtk_lost"]
    await engine.handle("state.rtk", RtkStatus(carr_soln=2, corr_age_s=1.0))
    assert kinds(sub) == ["rtk_lost_cleared"]
```
Addition to `tests/unit/test_sampler.py` (inside `test_sample_row_maps_state`, after building the state): set `s.rtk.corr_age_s, s.rtk.baseline_m = 1.5, 1234.5` in `state_at` and assert `row["corr_age_s"] == 1.5 and row["baseline_m"] == 1234.5`.
Addition to `tests/unit/test_web_ws.py`: in `test_snapshot_and_epoch_shapes`, `epoch_message(ctx.store.state, {"rtk"})` has key `rtk` with `carr_soln`; and a translate check: `WsHub._translate("points.progress", {"state": "collecting"}, {"survey"})["topic"] == "survey"`.

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: WebSocket, alerts, sampler, context changes**

`ws.py`: `TOPICS = (..., "survey")`; `EPOCH_TOPICS = {"pvt", "sats", "rtcm", "svin", "rtk"}`; in `epoch_message` add `if "rtk" in topics: msg["rtk"] = state.rtk.model_dump(mode="json")`; `BUS_TO_TOPIC` add `"ntrip_client.status": "rtk", "state.time_mark": "rtk", "points.progress": "survey", "points.saved": "survey"`; subscribe to those bus topics in `serve`.
`alerts.py`: add `"ntrip_client.status", "state.rtk"` to `TOPICS`; constants `CORR_STALE_S = 10.0`, `CORR_OK_S = 5.0`, `RTK_LOST_GRACE_S = 10.0`; state `self._was_fixed = False`, `self._rtk_bad_since: float | None = None`; handlers:
```python
    async def _on_ntrip_client_status(self, status: Any) -> None:
        if status.connected:
            await self.clear("ntrip_disconnected", f"NTRIP connected to {status.host}:{status.port}/{status.mountpoint}")
        elif status.last_error:
            await self.raise_("ntrip_disconnected", "warning", f"NTRIP client disconnected: {status.last_error}")

    async def _on_state_rtk(self, rtk: Any) -> None:
        now = self._clock()
        if rtk.corr_age_s is not None and rtk.corr_age_s > CORR_STALE_S:
            await self.raise_("corrections_stale", "warning", f"corrections are {rtk.corr_age_s:.0f}s old", {"corr_age_s": rtk.corr_age_s})
        elif rtk.corr_age_s is not None and rtk.corr_age_s < CORR_OK_S:
            await self.clear("corrections_stale", "corrections are fresh again")
        if rtk.carr_soln == 2:
            self._was_fixed = True
            self._rtk_bad_since = None
            await self.clear("rtk_lost", "RTK fixed again")
        elif self._was_fixed:
            self._rtk_bad_since = self._rtk_bad_since or now
            if now - self._rtk_bad_since >= RTK_LOST_GRACE_S:
                await self.raise_("rtk_lost", "warning", f"RTK fixed lost ({rtk.carr_soln_name})", {"carr_soln": rtk.carr_soln})
```
`sampler.py`: `"corr_age_s": state.rtk.corr_age_s, "baseline_m": state.rtk.baseline_m`.
`context.py`: add `@property def rover(self) -> Any: return getattr(self.daemon, "rover", None)`.

- [ ] **Step 4: Write `src/mtrtk/web/api/rover.py`**

```python
"""Rover role: NTRIP client, RTK status, outputs, sessions and survey points."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel

from mtrtk.rover.exports import to_csv, to_geojson, to_gpx, to_kml
from mtrtk.rover.ntrip_client import NtripClientConfig
from mtrtk.web.envfile import update_env

router = APIRouter(prefix="/api/rover", tags=["rover"])


class NtripUrlBody(BaseModel):
    url: str


class SessionBody(BaseModel):
    name: str | None = None
    notes: str | None = None


class CollectBody(BaseModel):
    name: str
    code: str | None = None
    note: str | None = None
    epochs: int | None = None
    fixed_only: bool | None = None


class PointPatch(BaseModel):
    name: str | None = None
    code: str | None = None
    note: str | None = None


def _rover(request: Request) -> Any:
    rover = request.app.state.ctx.rover
    if rover is None:
        raise HTTPException(409, "daemon is not running in the rover role")
    return rover


def _dump(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _dump(getattr(obj, k)) for k in obj.__dataclass_fields__}
    return obj


@router.get("")
async def overview(request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    rover = _rover(request)
    nmea = getattr(rover, "nmea", None)
    tcp = next((s for s in (getattr(nmea, "sinks", []) or []) if hasattr(s, "client_count")), None)
    return {
        "role": ctx.settings.role.value,
        "driver": {"name": rover.driver.name, "capabilities": _dump(rover.driver.capabilities)},
        "ntrip": _dump(rover.ntrip_client.status) if rover.ntrip_client else None,
        "rtk": ctx.store.state.rtk.model_dump(mode="json"),
        "outputs": {
            "nmea_tcp": {"port": tcp.port, "clients": tcp.client_count} if tcp else None,
            "nmea_udp": ctx.settings.nmea_udp_targets,
            "nmea_serial": ctx.settings.nmea_serial,
            "json_udp": ctx.settings.json_udp_port,
            "sentences": sorted(getattr(nmea, "sentences", [])),
        },
        "session": _dump(await rover.sessions_repo.current()),
        "collect": rover.collector.status.model_dump(mode="json"),
    }


@router.put("/ntrip")
async def set_ntrip(body: NtripUrlBody, request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    rover = _rover(request)
    try:
        NtripClientConfig.from_url(body.url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    update_env(ctx.settings.mtrtk_env_file, {"NTRIP_URL": body.url})
    ctx.settings.ntrip_url = body.url
    await rover.set_ntrip_url(body.url)
    return {"ok": True, "url": body.url}


@router.get("/sessions")
async def sessions(request: Request) -> list[dict[str, Any]]:
    return [_dump(s) for s in await _rover(request).sessions_repo.list()]


@router.post("/sessions")
async def start_session(body: SessionBody, request: Request) -> dict[str, Any]:
    return _dump(await _rover(request).sessions_repo.start(body.name, request.app.state.ctx.settings.role.value, body.notes))


@router.post("/sessions/stop")
async def stop_session(request: Request) -> dict[str, Any] | None:
    return _dump(await _rover(request).sessions_repo.stop())


@router.get("/collect")
async def collect_status(request: Request) -> dict[str, Any]:
    return _rover(request).collector.status.model_dump(mode="json")


@router.post("/collect")
async def collect_start(body: CollectBody, request: Request) -> dict[str, Any]:
    try:
        status = await _rover(request).collector.start(body.name, body.code, body.note, body.epochs, body.fixed_only)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return status.model_dump(mode="json")


@router.delete("/collect")
async def collect_cancel(request: Request) -> dict[str, Any]:
    rover = _rover(request)
    rover.collector.cancel()
    return rover.collector.status.model_dump(mode="json")


@router.get("/points")
async def points(request: Request, session_id: int | None = None, limit: int = Query(1000, ge=1, le=10000)) -> list[dict[str, Any]]:
    return [_dump(p) for p in await _rover(request).points_repo.list(session_id, limit)]


@router.get("/points/export")
async def export_points(request: Request, fmt: Literal["csv", "geojson", "kml", "gpx"] = "csv", session_id: int | None = None) -> Response:
    pts = list(reversed(await _rover(request).points_repo.list(session_id, 10000)))
    station = request.app.state.ctx.settings.station_id
    if fmt == "csv":
        return Response(to_csv(pts), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{station}-points.csv"'})
    if fmt == "geojson":
        import json

        return Response(json.dumps(to_geojson(pts)), media_type="application/geo+json", headers={"Content-Disposition": f'attachment; filename="{station}-points.geojson"'})
    if fmt == "kml":
        return Response(to_kml(pts), media_type="application/vnd.google-earth.kml+xml", headers={"Content-Disposition": f'attachment; filename="{station}-points.kml"'})
    return Response(to_gpx(pts), media_type="application/gpx+xml", headers={"Content-Disposition": f'attachment; filename="{station}-points.gpx"'})


@router.patch("/points/{point_id}")
async def patch_point(point_id: int, body: PointPatch, request: Request) -> dict[str, Any]:
    try:
        return _dump(await _rover(request).points_repo.update(point_id, body.name, body.code, body.note))
    except KeyError as exc:
        raise HTTPException(404, "point not found") from exc


@router.delete("/points/{point_id}")
async def delete_point(point_id: int, request: Request) -> dict[str, bool]:
    await _rover(request).points_repo.delete(point_id)
    return {"ok": True}
```
Add `"rover"` to the router list in `web/app.py`. FastAPI resolves `/points/export` before `/points/{point_id}` only if declared first — keep the order above (`export` is declared before the `{point_id}` routes; a GET to `/points/export` with the `{point_id}` route would 422 on `int` parsing otherwise).

- [ ] **Step 5: Run tests, lint, commit**

`uv run pytest tests/unit/test_web_rover.py tests/unit/test_alerts.py tests/unit/test_sampler.py tests/unit/test_web_ws.py -q` → all pass.
```bash
git add src/mtrtk/web src/mtrtk/alerts.py src/mtrtk/store/sampler.py tests/unit
git commit -m "feat(web): rover API (NTRIP, sessions, points, collect, exports), rover alert rules and WS topics

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Daemon wiring for the rover role

**Files:**
- Modify: `src/mtrtk/daemon.py`, `src/mtrtk/config.py` (`nmea_udp_targets` parsing helper), `tests/unit/test_daemon_rover.py` (create)

**Interfaces:**
- Produces: `RoverServices` dataclass on `Daemon.rover` (`driver: UbloxDriver`, `ntrip_client: NtripClient | None`, `collector: PointCollector`, `sessions_repo`, `points_repo`, `nmea: NmeaPublisher | None`, `json_udp: JsonUdpPublisher | None`, `set_ntrip_url(url)`); rover consumers: raw log + retention (now for both roles), NTRIP client (when `settings.ntrip_url`), NMEA publisher (TCP on `nmea_tcp_port` when > 0, UDP targets from `nmea_udp_targets` as `host:port`, serial from `nmea_serial`), JSON UDP (to `127.0.0.1:JSON_UDP_PORT` when set), point collector; `Daemon.set_ntrip_url(url)` cancels the running NTRIP task and starts a new client; `StatusPrinter.format_line` adds `rtk <carr> age <s> base <m>` for the rover; `Settings.udp_targets() -> list[tuple[str, int]]`.
- The pty serial sink's slave path is symlinked to `DATA_DIR/ttyMTRTK` (documented for Docker: mount `./data` and point consumers at `data/ttyMTRTK`).

- [ ] **Step 1: Write the failing test**

`tests/unit/test_daemon_rover.py`:
```python
import asyncio
from pathlib import Path

import httpx
import pytest

from mtrtk.base.ntrip_caster import CasterConfig, NtripCaster
from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.daemon import Daemon

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_base_30s.ubx"
RTCM_FRAME = bytes.fromhex("d300133ed7fd0382dfdc1c403db34fe8fe0cef5e6b30bd2e23")  # a valid RTCM 1005; any valid frame works for the plumbing test


async def test_rover_daemon_connects_ntrip_serves_nmea_and_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    caster_bus = Bus()
    caster = NtripCaster(caster_bus, CasterConfig("MTRK", "rover", "pw", "MTRK", "BGD"), host="127.0.0.1", port=0)
    await caster.start()
    settings = Settings(
        _env_file=None, role="rover", mtrtk_source=f"file:{FIXTURE}", replay_speed=5, replay_log=True, data_dir=tmp_path,
        ntrip_url=f"ntrip://rover:pw@127.0.0.1:{caster.port}/MTRK", nmea_tcp_port=0, web_bind="127.0.0.1", web_port=0, web_allow_insecure=True,
    )
    daemon = Daemon(settings)
    run_task = asyncio.create_task(daemon.run())
    try:
        for _ in range(200):
            await asyncio.sleep(0.02)
            if daemon.rover and daemon.rover.ntrip_client and daemon.rover.ntrip_client.status.connected and daemon.web and daemon.web.started.is_set():
                break
        assert daemon.rover is not None and daemon.rover.ntrip_client.status.connected
        for f in Framer().feed(RTCM_FRAME):
            caster_bus.publish("raw.rtcm", f)
        await asyncio.sleep(0.2)
        assert daemon.rover.ntrip_client.status.frames_injected == 1
        assert daemon.rover.driver.dropped_bytes == len(RTCM_FRAME)  # replay: no live link to inject into
        tcp = next(s for s in daemon.rover.nmea.sinks if hasattr(s, "client_count"))
        reader, writer = await asyncio.open_connection("127.0.0.1", tcp.port)
        line = await asyncio.wait_for(reader.readline(), 5.0)
        assert line.startswith(b"$GNGGA,") or line.startswith(b"$GNRMC,")
        writer.close()
        async with httpx.AsyncClient() as c:
            body = (await c.get(f"http://127.0.0.1:{daemon.web.port}/api/rover")).json()
        assert body["ntrip"]["connected"] is True and body["outputs"]["nmea_tcp"]["port"] == tcp.port
    finally:
        daemon.stop.set()
        await asyncio.wait_for(run_task, 30.0)
        await caster.stop()
    from mtrtk.rawlog.index import list_logs

    assert list_logs(tmp_path) and list_logs(tmp_path)[0].msg_counts.get("RXM-RAWX", 0) > 0


def test_udp_targets_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROLE", "rover")
    monkeypatch.setenv("NMEA_UDP_TARGETS", "192.168.1.5:10110, 10.0.0.2:5000,bad")
    s = Settings(_env_file=None)
    assert s.udp_targets() == [("192.168.1.5", 10110), ("10.0.0.2", 5000)]
```

- [ ] **Step 2: Run to verify failure** — FAIL (`Daemon.rover` missing).

- [ ] **Step 3: Settings helper**

In `config.py` add to `Settings`:
```python
    def udp_targets(self) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        for item in self.nmea_udp_targets:
            host, _, port = item.rpartition(":")
            if host and port.isdigit():
                out.append((host, int(port)))
        return out
```

- [ ] **Step 4: Extend `src/mtrtk/daemon.py`**

Imports:
```python
import os
from dataclasses import dataclass

from mtrtk.rover.drivers.ublox import UbloxDriver
from mtrtk.rover.json_out import JsonUdpPublisher
from mtrtk.rover.nmea_out import NmeaPublisher
from mtrtk.rover.ntrip_client import NtripClient, NtripClientConfig
from mtrtk.rover.points import PointCollector, PointsRepo
from mtrtk.rover.sessions import SessionsRepo
from mtrtk.rover.sinks import NmeaSink, SerialSink, TcpBroadcastSink, UdpSink
```
Add:
```python
@dataclass
class RoverServices:
    driver: UbloxDriver
    collector: PointCollector
    sessions_repo: SessionsRepo
    points_repo: PointsRepo
    ntrip_client: NtripClient | None = None
    nmea: NmeaPublisher | None = None
    json_udp: JsonUdpPublisher | None = None
    _set_url: Callable[[str], Awaitable[None]] | None = None

    async def set_ntrip_url(self, url: str) -> None:
        if self._set_url:
            await self._set_url(url)
```
In `Daemon.__init__`: `self.rover: RoverServices | None = None`, `self._ntrip_task: asyncio.Task[None] | None = None`.
In `_consumers()`: make raw logging + retention apply to both roles (`if not s.source_is_file or s.replay_log:` outside the base-only block), keep caster/basemode base-only, and add for `Role.ROVER`: `("rover", self._run_rover)`.
Add:
```python
    async def _run_rover(self) -> None:
        s = self.settings
        sessions, points = SessionsRepo(self.db), PointsRepo(self.db)
        driver = UbloxDriver(self.controller, self.store)
        collector = PointCollector(self.bus, self.store, points, sessions, default_epochs=s.point_epochs, default_fixed_only=s.point_fixed_only)
        self.rover = RoverServices(driver, collector, sessions, points, _set_url=self._restart_ntrip)
        tasks = [asyncio.create_task(collector.run(self.stop), name="points")]
        sinks: list[NmeaSink] = []
        if s.nmea_tcp_port is not None and s.nmea_tcp_port >= 0:
            sinks.append(TcpBroadcastSink("0.0.0.0", s.nmea_tcp_port))
        if s.udp_targets():
            sinks.append(UdpSink(s.udp_targets()))
        if s.nmea_serial:
            sinks.append(SerialSink(s.nmea_serial, s.baud))
        if sinks:
            self.rover.nmea = NmeaPublisher(self.bus, self.store, sinks, s.nmea_sentences, s.nmea_slow_interval_s)
            tasks.append(asyncio.create_task(self.rover.nmea.run(self.stop), name="nmea"))
        if s.json_udp_port:
            self.rover.json_udp = JsonUdpPublisher(self.bus, [("127.0.0.1", s.json_udp_port)])
            tasks.append(asyncio.create_task(self.rover.json_udp.run(self.stop), name="json-udp"))
        if s.nmea_serial == "pty" and self.rover.nmea:
            tasks.append(asyncio.create_task(self._link_pty_when_ready(), name="pty-link"))
        if s.ntrip_url:
            await self._restart_ntrip(s.ntrip_url)
        try:
            await self.stop.wait()
        finally:
            if self._ntrip_task:
                self._ntrip_task.cancel()
                await asyncio.gather(self._ntrip_task, return_exceptions=True)
            collector.stop()
            if self.rover.nmea:
                self.rover.nmea.stop()
            if self.rover.json_udp:
                self.rover.json_udp.stop()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.rover = None

    async def _link_pty_when_ready(self) -> None:
        """Symlink DATA_DIR/ttyMTRTK to the pty slave once the serial sink has started."""
        assert self.rover and self.rover.nmea
        for _ in range(50):
            slave = next((getattr(sink, "slave_path", None) for sink in self.rover.nmea.sinks if getattr(sink, "slave_path", None)), None)
            if slave:
                link = self.settings.data_dir / "ttyMTRTK"
                try:
                    if link.is_symlink() or link.exists():
                        link.unlink()
                    os.symlink(slave, link)
                    log.info("NMEA pseudo-terminal linked at %s", link)
                except OSError as exc:
                    log.warning("could not link %s: %s", link, exc)
                return
            await asyncio.sleep(0.1)

    async def _restart_ntrip(self, url: str) -> None:
        assert self.rover is not None
        if self._ntrip_task:
            self._ntrip_task.cancel()
            await asyncio.gather(self._ntrip_task, return_exceptions=True)
        config = NtripClientConfig.from_url(url)
        client = NtripClient(config, self.bus, self.rover.driver, gga_provider=self._gga_for_caster, gga_interval_s=self.settings.ntrip_gga_interval_s)
        self.rover.ntrip_client = client
        self._ntrip_task = asyncio.create_task(self._supervise("ntrip-client", lambda: client.run(self.stop)), name="ntrip-client")

    def _gga_for_caster(self) -> bytes | None:
        from mtrtk.rover.nmea_out import build_gga

        return build_gga(self.store.state)
```
`StatusPrinter.format_line`: append `f" rtk {s.rtk.carr_soln_name} age {s.rtk.corr_age_s:.1f}s base {s.rtk.baseline_m:.1f}m"` when `s.rtk.corr_age_s is not None`, guarding `baseline_m` for `None`.

- [ ] **Step 5: Run tests, lint, commit**

`uv run pytest -q` → all pass (the rover daemon test needs the base fixture from Phase 1).
```bash
git add src/mtrtk/daemon.py src/mtrtk/config.py tests/unit/test_daemon_rover.py
git commit -m "feat: rover role wiring — NTRIP client, NMEA/JSON outputs, point collector, raw logging for both roles

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Rover UI — role-adaptive shell, RTK page, Survey page

**Files:**
- Create: `web/src/pages/Rtk.tsx`, `web/src/pages/Survey.tsx`, `web/src/components/FixTimeline.tsx`, `web/src/pages/Rtk.test.tsx`, `web/src/pages/Survey.test.tsx`
- Modify: `web/src/lib/types.ts`, `web/src/lib/live.ts`, `web/src/lib/queries.ts`, `web/src/app/Rail.tsx`, `web/src/app/Tape.tsx`, `web/src/app/router.tsx`, `web/src/pages/Dashboard.tsx`, `web/src/components/MapPanel.tsx` (optional `points` markers)

**Interfaces:**
- Types: `RtcmRxStats`, `RtkStatus`, `TimeMark`, `Attitude` (added to `ReceiverState`), `NtripClientStatus`, `CollectStatus`, `Session`, `Point`, `RoverOverview`.
- Live store: epoch `rtk` merges into `state.rtk`; `update` topic `rtk` with source `ntrip_client.status` → `ntripClient: NtripClientStatus | null`; source `state.time_mark` → `timeMarks` ring (last 50); topic `survey` source `points.progress` → `collect: CollectStatus | null`, source `points.saved` → `lastSavedPointId` (pages invalidate the points query on change).
- Rail: `NAV_BASE` (Phase 4 list) and `NAV_ROVER` = Dashboard, Satellites, Receiver, RTK (`/rtk`, icon `Crosshair`), Survey (`/survey`, icon `Flag`), Logs, History, Events, Settings; the list follows `useLive().role` (base list until the snapshot arrives); the wordmark subtitle shows the role.
- Tape (rover): after the fix badge show `age <corr_age_s> s` coloured by level (good < 5, warning < 10, critical ≥ 10) and `base <baseline_m>`.
- Dashboard (rover): the "Position mode" and "Corrections" panels are replaced by "RTK" (carrier solution badge, correction age gauge 0–30 s, baseline, ref station) and "NTRIP client" (host/mount, connected badge, bytes, frames, last error).
- `FixTimeline({from, to})` — a strip of 1 s cells from `/api/history?metrics=carr_soln,fix_type&res=1s` coloured by state (none/2D/3D/float/fixed) with a legend and hover; used on `/rtk` for the last 10 minutes.
- `MapPanel` accepts `points?: {lat, lon, label}[]` and draws them as small brass squares with a title.

- [ ] **Step 1: Write the failing tests**

`web/src/pages/Rtk.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { useLive } from "@/lib/live";
import { sampleState } from "@/test/fixtures";
import Rtk from "./Rtk";

const overview = { role: "rover", driver: { name: "ublox", capabilities: { accepts_rtcm: true, raw_gnss_log: true, attitude: false, imu: false, sats: true, spectrum: true } }, ntrip: { connected: true, host: "100.100.50.10", port: 2101, mountpoint: "MTRK", version: 2, bytes_received: 123456, frames_injected: 400, crc_dropped: 0, last_rtcm_mono: 1, last_error: null, reconnects: 0, next_retry_s: null, since_mono: 1 }, rtk: {}, outputs: { nmea_tcp: { port: 10110, clients: 0 }, nmea_udp: [], nmea_serial: null, json_udp: null, sentences: ["GGA"] }, session: null, collect: { state: "idle" } };
let calls: [string, RequestInit | undefined][] = [];

describe("RTK page", () => {
  beforeEach(() => {
    calls = [];
    const state = sampleState();
    state.rtk = { ...state.rtk, carr_soln: 2, carr_soln_name: "RTK fixed", baseline_m: 1234.56, heading_deg: 91.2, heading_valid: true, corr_age_s: 1.2, ref_station_id: 7, rtcm_rx: { "1077": { count: 120, used: 118, crc_failed: 0, last_seen_mono: 1 }, "1005": { count: 120, used: 120, crc_failed: 1, last_seen_mono: 1 } }, rtcm_rx_total: 240, rtcm_crc_failed: 1 } as typeof state.rtk;
    useLive.setState({ state, role: "rover", status: "open", lastEpochAt: Date.now(), ntripClient: overview.ntrip as never });
    globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      calls.push([String(url), init]);
      if (String(url).endsWith("/api/rover")) return new Response(JSON.stringify(overview), { status: 200 });
      if (String(url).includes("/api/history")) return new Response(JSON.stringify({ res: "1s", columns: ["ts", "carr_soln", "fix_type"], rows: [[1, 2, 3], [2, 1, 3], [3, 0, 3]] }), { status: 200 });
      if (init?.method === "PUT") return new Response(JSON.stringify({ ok: true }), { status: 200 });
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
  });

  it("shows RTK solution, correction age, RTCM table and NTRIP client", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><MemoryRouter><Rtk /></MemoryRouter></QueryClientProvider>);
    expect(await screen.findByText(/RTK fixed/)).toBeInTheDocument();
    expect(screen.getByText(/1234\.56 m/)).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: /correction age/i })).toHaveAttribute("aria-valuenow", "1.2");
    expect(screen.getByText("1077")).toBeInTheDocument();
    expect(await screen.findByText(/100\.100\.50\.10:2101\/MTRK/)).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: /fix state/i })).toBeInTheDocument();
  });

  it("edits the NTRIP url", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><MemoryRouter><Rtk /></MemoryRouter></QueryClientProvider>);
    await userEvent.click(await screen.findByRole("button", { name: /change caster/i }));
    const input = screen.getByLabelText(/ntrip url/i);
    await userEvent.clear(input);
    await userEvent.type(input, "ntrip://rover:pw@base:2101/MTRK");
    await userEvent.click(screen.getByRole("button", { name: /^connect$/i }));
    const put = calls.find(([, i]) => i?.method === "PUT")!;
    expect(JSON.parse(put[1]!.body as string)).toEqual({ url: "ntrip://rover:pw@base:2101/MTRK" });
  });
});
```

`web/src/pages/Survey.test.tsx`:
```tsx
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { useLive } from "@/lib/live";
import { sampleState } from "@/test/fixtures";
import Survey from "./Survey";

vi.mock("maplibre-gl", () => {
  class Map { on() { return this; } once() { return this; } addControl() { return this; } remove() {} getSource() { return undefined; } addSource() {} addLayer() {} setStyle() {} easeTo() {} isStyleLoaded() { return true; } }
  class Marker { setLngLat() { return this; } addTo() { return this; } remove() {} }
  class NavigationControl {}
  return { default: { Map, Marker, NavigationControl }, Map, Marker, NavigationControl };
});

const points = [{ id: 1, session_id: 1, name: "BM-1", code: "BM", note: "brass", ts_utc: "2026-09-18T16:00:00+00:00", lat: 23.8373506, lon: 90.2625502, height_m: -36.268, hmsl_m: 13.363, n_epochs: 30, sd_n: 0.004, sd_e: 0.003, sd_u: 0.009, fix_type: 3, carr_soln: 2, h_acc_m: 0.012, v_acc_m: 0.018 }];
let calls: [string, RequestInit | undefined][] = [];

describe("Survey page", () => {
  beforeEach(() => {
    calls = [];
    useLive.setState({ state: sampleState(), role: "rover", status: "open", lastEpochAt: Date.now(), collect: null });
    globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      calls.push([String(url), init]);
      const u = String(url);
      if (u.endsWith("/api/rover/sessions") && !init?.method) return new Response(JSON.stringify([{ id: 1, name: "field-1", start_utc: "2026-09-18T15:00:00+00:00", end_utc: null, role: "rover", notes: null }]), { status: 200 });
      if (u.includes("/api/rover/points") && !init?.method) return new Response(JSON.stringify(points), { status: 200 });
      if (u.endsWith("/api/rover/collect") && init?.method === "POST") return new Response(JSON.stringify({ state: "collecting", name: "BM-2", target: 30, accepted: 0, skipped: 0 }), { status: 200 });
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
  });

  it("lists points, shows the open session and starts a collection", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><MemoryRouter><Survey /></MemoryRouter></QueryClientProvider>);
    expect(await screen.findByText("BM-1")).toBeInTheDocument();
    expect(screen.getByText(/field-1/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/point name/i), "BM-2");
    await userEvent.click(screen.getByRole("button", { name: /collect point/i }));
    const post = calls.find(([u, i]) => u.endsWith("/api/rover/collect") && i?.method === "POST")!;
    expect(JSON.parse(post[1]!.body as string)).toMatchObject({ name: "BM-2", fixed_only: true });
    useLive.setState({ collect: { state: "collecting", name: "BM-2", target: 30, accepted: 12, skipped: 1, sd_n: 0.004, sd_e: 0.003, sd_u: 0.01, mean_lat: 23.8, mean_lon: 90.2, mean_h: -36, point_id: null, reason: null } });
    expect(await screen.findByText(/12 of 30/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /csv/i })).toHaveAttribute("href", "/api/rover/points/export?fmt=csv");
  });
});
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Types, live store, queries**

`types.ts` additions:
```ts
export interface RtcmRxStats { count: number; used: number; crc_failed: number; last_seen_mono: number | null }
export interface RtkStatus { carr_soln: number; carr_soln_name: string; diff_soln: boolean; rel_pos_n_m: number | null; rel_pos_e_m: number | null; rel_pos_d_m: number | null; baseline_m: number | null; heading_deg: number | null; heading_valid: boolean; acc_n_m: number | null; acc_e_m: number | null; acc_d_m: number | null; acc_length_m: number | null; acc_heading_deg: number | null; ref_station_id: number | null; rel_pos_valid: boolean; is_moving: boolean; ref_pos_missing: boolean; ref_obs_missing: boolean; normalized: boolean; corr_age_receiver_s: number | null; corr_age_s: number | null; rtcm_rx: Record<string, RtcmRxStats>; rtcm_rx_total: number; rtcm_crc_failed: number; last_rtcm_mono: number | null }
export interface TimeMark { channel: number; count: number; rising_week: number | null; rising_tow_s: number | null; falling_week: number | null; falling_tow_s: number | null; new_rising: boolean; new_falling: boolean; time_base: number; utc_based: boolean; acc_est_ns: number; rising_utc: string | null }
export interface Attitude { roll_deg: number | null; pitch_deg: number | null; heading_deg: number | null; acc_roll_deg: number | null; acc_pitch_deg: number | null; acc_heading_deg: number | null; source: string }
export interface NtripClientStatus { connected: boolean; host: string; port: number; mountpoint: string; version: number | null; bytes_received: number; frames_injected: number; crc_dropped: number; last_rtcm_mono: number | null; last_error: string | null; reconnects: number; next_retry_s: number | null; since_mono: number | null }
export interface CollectStatus { state: "idle" | "collecting" | "done" | "aborted"; name: string | null; target: number; accepted: number; skipped: number; sd_n: number | null; sd_e: number | null; sd_u: number | null; mean_lat: number | null; mean_lon: number | null; mean_h: number | null; point_id: number | null; reason: string | null }
export interface Session { id: number; name: string | null; start_utc: string; end_utc: string | null; role: string | null; notes: string | null }
export interface Point { id: number; session_id: number | null; name: string; code: string | null; note: string | null; ts_utc: string; lat: number; lon: number; height_m: number; hmsl_m: number | null; n_epochs: number; sd_n: number; sd_e: number; sd_u: number; fix_type: number; carr_soln: number; h_acc_m: number | null; v_acc_m: number | null }
export interface RoverOverview { role: string; driver: { name: string; capabilities: Record<string, boolean> }; ntrip: NtripClientStatus | null; rtk: RtkStatus; outputs: { nmea_tcp: { port: number; clients: number } | null; nmea_udp: string[]; nmea_serial: string | null; json_udp: number | null; sentences: string[] }; session: Session | null; collect: CollectStatus }
```
Add `rtk: RtkStatus; time_marks: TimeMark[]; attitude: Attitude | null;` to `ReceiverState` and `rtk?: RtkStatus` to `EpochSections`. Update `test/fixtures.ts` `sampleState()` with a default `rtk` object (all zeros/nulls, `rtcm_rx: {}`), `time_marks: []`, `attitude: null`.

`live.ts`: add `ntripClient: NtripClientStatus | null`, `timeMarks: TimeMark[]`, `collect: CollectStatus | null`, `lastSavedPointId: number | null` to the store; in the epoch branch `if (msg.rtk) next.rtk = msg.rtk;`; in `update` handling: `case "rtk"`: if `msg.source === "ntrip_client.status"` set `ntripClient`; if `msg.source === "state.time_mark"` prepend to `timeMarks` (cap 50); `case "survey"`: `points.progress` → `collect`, `points.saved` → `lastSavedPointId = (data as Point).id`.

`queries.ts`: `useRover = () => useQuery({ queryKey: ["rover"], queryFn: () => get<RoverOverview>("/api/rover"), refetchInterval: 5000 })`, `useSessions`, `usePoints(sessionId?)`.

- [ ] **Step 4: Shell changes**

`Rail.tsx`: export `NAV_BASE` (current list) and `NAV_ROVER` (see Interfaces); `const role = useLive((s) => s.role); const nav = role === "rover" ? NAV_ROVER : NAV_BASE;` and subtitle `{role === "rover" ? "rover" : "base station"}`.
`Tape.tsx`: after the fix badge, when `role === "rover" && state?.rtk`: `<span className="num" style={{ color: ageColor(age) }}>age {age?.toFixed(1) ?? "—"} s</span>` and `<span className="num">base {fmtMeters(state.rtk.baseline_m, 1)}</span>`; `ageColor`: `< 5 → var(--status-good)`, `< 10 → var(--status-warning)`, else `var(--status-critical)`.
`Dashboard.tsx`: `const role = useLive((s) => s.role)`; when `role === "rover"` render the RTK panel (`StatusBadge` for carr soln, `Gauge` "Correction age" 0–30 s, `Stat` baseline/heading/ref station) and the NTRIP panel (from `useLive().ntripClient`) in place of the two base panels.
`router.tsx`: add `{ path: "rtk", element: <Rtk /> }`, `{ path: "survey", element: <Survey /> }`.
`MapPanel.tsx`: new prop `points?: { lat: number; lon: number; label: string }[]`; effect creates/updates a `Map<label, Marker>` of small squares (`size-2 bg-brass rounded-[2px]`) with `el.title = label`.

- [ ] **Step 5: Write `web/src/components/FixTimeline.tsx`**

```tsx
import { useHistory } from "@/lib/queries";
import { STATUS } from "@/lib/palette";
import { Legend } from "@/components/charts/Legend";

const COLOR = (carr: number | null, fix: number | null): string => (carr === 2 ? STATUS.good : carr === 1 ? STATUS.warning : (fix ?? 0) >= 3 ? "var(--ink-3)" : STATUS.critical);
const LEGEND = [{ label: "RTK fixed", color: STATUS.good }, { label: "RTK float", color: STATUS.warning }, { label: "3D", color: "var(--ink-3)" }, { label: "no fix", color: STATUS.critical }];

export function FixTimeline({ from, to }: { from: string; to: string }) {
  const q = useHistory(["carr_soln", "fix_type"], from, to, "1s");
  if (!q.data) return <p className="text-ink-2">Loading…</p>;
  const rows = q.data.rows;
  if (rows.length === 0) return <p className="text-ink-2">No samples in this range.</p>;
  const fixed = rows.filter((r) => r[1] === 2).length;
  return (
    <div className="flex flex-col gap-2">
      <svg role="img" aria-label={`Fix state over time: ${Math.round((fixed / rows.length) * 100)}% RTK fixed`} viewBox={`0 0 ${rows.length} 10`} preserveAspectRatio="none" className="h-6 w-full">
        {rows.map((r, i) => <rect key={r[0] as number} x={i} y={0} width={1} height={10} fill={COLOR(r[1] as number | null, r[2] as number | null)}><title>{`${new Date((r[0] as number) * 1000).toISOString().slice(11, 19)} UTC`}</title></rect>)}
      </svg>
      <div className="flex items-center justify-between"><Legend items={LEGEND} /><span className="num text-[12px] text-ink-2">{Math.round((fixed / rows.length) * 100)}% fixed over {rows.length} s</span></div>
    </div>
  );
}
```

- [ ] **Step 6: Write `web/src/pages/Rtk.tsx`**

```tsx
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { PageHeader } from "@/app/PageHeader";
import { Panel } from "@/components/Panel";
import { Stat } from "@/components/Stat";
import { StatusBadge } from "@/components/StatusBadge";
import { EmptyState } from "@/components/EmptyState";
import { FixTimeline } from "@/components/FixTimeline";
import { Gauge } from "@/components/charts/Gauge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { put } from "@/lib/api";
import { fmtBytes, fmtMeters, fmtUtc } from "@/lib/format";
import { useLive } from "@/lib/live";
import { useRover } from "@/lib/queries";
import { fixLevel } from "@/lib/status";

const RTCM_NAMES: Record<string, string> = { "1005": "Base position", "1074": "GPS MSM4", "1077": "GPS MSM7", "1084": "GLONASS MSM4", "1087": "GLONASS MSM7", "1094": "Galileo MSM4", "1097": "Galileo MSM7", "1124": "BeiDou MSM4", "1127": "BeiDou MSM7", "1230": "GLONASS biases" };
const ageLevel = (age: number | null) => (age == null ? "warning" : age < 5 ? "good" : age < 10 ? "warning" : "critical");

export default function Rtk() {
  const { state, ntripClient, receiverConnected, lastEpochAt, timeMarks } = useLive();
  const rover = useRover();
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [url, setUrl] = useState("");
  const setNtrip = useMutation({ mutationFn: (u: string) => put("/api/rover/ntrip", { url: u }), onSuccess: () => { toast.success("Connecting to the new caster"); setEditing(false); qc.invalidateQueries({ queryKey: ["rover"] }); }, onError: (e) => toast.error(String(e)) });
  if (!state) return <><PageHeader title="RTK" /><EmptyState title="Waiting for the receiver" /></>;
  const rtk = state.rtk;
  const ntrip = ntripClient ?? rover.data?.ntrip ?? null;
  const fix = fixLevel(state.fix, receiverConnected, lastEpochAt == null);
  const [from, to] = [new Date(Date.now() - 600_000).toISOString(), new Date().toISOString()];
  const types = Object.keys(rtk.rtcm_rx).sort((a, b) => Number(a) - Number(b));
  return (
    <>
      <PageHeader title="RTK"><StatusBadge level={fix.level} label={fix.label} /></PageHeader>
      <div className="grid grid-cols-12 gap-4">
        <Panel className="col-span-12 lg:col-span-4" title="Solution">
          <Stat label="Carrier solution" value={rtk.carr_soln_name} level={rtk.carr_soln === 2 ? "good" : rtk.carr_soln === 1 ? "warning" : "serious"} />
          <Stat label="Baseline" value={fmtMeters(rtk.baseline_m, 2)} />
          <Stat label="Baseline N / E / D" value={`${fmtMeters(rtk.rel_pos_n_m, 3)} / ${fmtMeters(rtk.rel_pos_e_m, 3)} / ${fmtMeters(rtk.rel_pos_d_m, 3)}`} />
          <Stat label="Heading to base" value={rtk.heading_valid && rtk.heading_deg != null ? `${rtk.heading_deg.toFixed(2)}°` : "—"} />
          <Stat label="Baseline accuracy" value={fmtMeters(rtk.acc_length_m, 3)} />
          <Stat label="Reference station" value={rtk.ref_station_id == null ? "—" : String(rtk.ref_station_id)} />
          <div className="mt-3"><Gauge label="Correction age" value={rtk.corr_age_s ?? 30} max={30} level={ageLevel(rtk.corr_age_s)} format={(v) => (rtk.corr_age_s == null ? "no corrections" : `${v.toFixed(1)} s`)} /></div>
          {rtk.corr_age_receiver_s != null ? <p className="mt-1 text-[12px] text-ink-2">Receiver reports corrections ≤ {rtk.corr_age_receiver_s} s old</p> : null}
        </Panel>
        <Panel className="col-span-12 lg:col-span-4" title="NTRIP client" actions={<Button size="sm" variant="outline" onClick={() => { setEditing((e) => !e); setUrl(rover.data?.ntrip ? `ntrip://${rover.data.ntrip.host}:${rover.data.ntrip.port}/${rover.data.ntrip.mountpoint}` : ""); }}>Change caster</Button>}>
          {editing ? (
            <form className="mb-3 flex flex-col gap-2" onSubmit={(e) => { e.preventDefault(); setNtrip.mutate(url); }}>
              <Label htmlFor="ntrip-url">NTRIP URL</Label>
              <Input id="ntrip-url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="ntrip://user:password@base:2101/MTRK" />
              <div className="flex gap-2"><Button type="submit" disabled={!url}>Connect</Button><Button type="button" variant="outline" onClick={() => setEditing(false)}>Cancel</Button></div>
            </form>
          ) : null}
          {ntrip ? (
            <>
              <StatusBadge level={ntrip.connected ? "good" : "critical"} label={ntrip.connected ? "Connected" : "Disconnected"} className="mb-2" />
              <Stat label="Caster" value={`${ntrip.host}:${ntrip.port}/${ntrip.mountpoint}`} />
              <Stat label="Protocol" value={ntrip.version ? `NTRIP v${ntrip.version}` : "—"} />
              <Stat label="Received" value={`${fmtBytes(ntrip.bytes_received)} · ${ntrip.frames_injected} frames`} />
              <Stat label="Dropped (bad CRC)" value={String(ntrip.crc_dropped)} level={ntrip.crc_dropped ? "warning" : undefined} />
              <Stat label="Reconnects" value={String(ntrip.reconnects)} />
              {ntrip.last_error ? <p className="mt-2 text-status-critical">{ntrip.last_error}{ntrip.next_retry_s ? ` · retry in ${ntrip.next_retry_s.toFixed(0)} s` : ""}</p> : null}
            </>
          ) : <p className="text-ink-2">No caster configured. Set NTRIP_URL or use "Change caster".</p>}
        </Panel>
        <Panel className="col-span-12 lg:col-span-4" title="Corrections received" bodyClassName="p-2">
          {types.length === 0 ? <p className="p-2 text-ink-2">No RTCM messages seen by the receiver yet.</p> : (
            <table className="w-full text-[14px]">
              <thead><tr className="border-b border-line text-left text-ink-2"><th className="py-1.5 pr-3 font-medium">Type</th><th className="py-1.5 pr-3 font-medium">Content</th><th className="py-1.5 pr-3 text-right font-medium">Count</th><th className="py-1.5 pr-3 text-right font-medium">Used</th><th className="py-1.5 pr-3 text-right font-medium">Bad CRC</th></tr></thead>
              <tbody>{types.map((t) => { const s = rtk.rtcm_rx[t]; return <tr key={t} className="border-b border-line/60 last:border-0"><td className="num py-1.5 pr-3">{t}</td><td className="py-1.5 pr-3 text-ink-2">{RTCM_NAMES[t] ?? ""}</td><td className="num py-1.5 pr-3 text-right">{s.count}</td><td className="num py-1.5 pr-3 text-right">{s.used}</td><td className="num py-1.5 pr-3 text-right">{s.crc_failed}</td></tr>; })}</tbody>
            </table>
          )}
        </Panel>
        <Panel className="col-span-12 lg:col-span-8" title="Fix state, last 10 minutes"><FixTimeline from={from} to={to} /></Panel>
        <Panel className="col-span-12 lg:col-span-4" title="Camera time marks" bodyClassName="p-2">
          {timeMarks.length === 0 ? <p className="p-2 text-ink-2">No pulses on EXTINT yet.</p> : (
            <table className="w-full text-[14px]"><thead><tr className="border-b border-line text-left text-ink-2"><th className="py-1.5 pr-3 font-medium">#</th><th className="py-1.5 pr-3 font-medium">UTC</th><th className="py-1.5 pr-3 text-right font-medium">TOW (s)</th></tr></thead>
              <tbody>{timeMarks.slice(0, 20).map((m) => <tr key={`${m.channel}-${m.count}`} className="border-b border-line/60 last:border-0"><td className="num py-1.5 pr-3">{m.count}</td><td className="num py-1.5 pr-3">{m.rising_utc ? `${fmtUtc(m.rising_utc)}.${m.rising_utc.slice(20, 26)}` : "—"}</td><td className="num py-1.5 pr-3 text-right">{m.rising_tow_s?.toFixed(6) ?? "—"}</td></tr>)}</tbody></table>
          )}
        </Panel>
        {state.attitude ? (
          <Panel className="col-span-12 lg:col-span-4" title={`Attitude (${state.attitude.source})`}>
            <Stat label="Heading" value={state.attitude.heading_deg == null ? "—" : `${state.attitude.heading_deg.toFixed(2)}°`} />
            <Stat label="Roll / pitch" value={`${state.attitude.roll_deg?.toFixed(2) ?? "—"}° / ${state.attitude.pitch_deg?.toFixed(2) ?? "—"}°`} />
          </Panel>
        ) : null}
        {rover.data ? <Panel className="col-span-12" title="Outputs"><p className="text-ink-2">NMEA over TCP {rover.data.outputs.nmea_tcp ? `on port ${rover.data.outputs.nmea_tcp.port} (${rover.data.outputs.nmea_tcp.clients} client${rover.data.outputs.nmea_tcp.clients === 1 ? "" : "s"})` : "off"} · UDP {rover.data.outputs.nmea_udp.length ? rover.data.outputs.nmea_udp.join(", ") : "off"} · serial {rover.data.outputs.nmea_serial ?? "off"} · JSON UDP {rover.data.outputs.json_udp ?? "off"} · sentences {rover.data.outputs.sentences.join(", ")}.</p></Panel> : null}
      </div>
    </>
  );
}
```
- [ ] **Step 7: Write `web/src/pages/Survey.tsx`**

```tsx
import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { PageHeader } from "@/app/PageHeader";
import { Panel } from "@/components/Panel";
import { Stat } from "@/components/Stat";
import { StatusBadge } from "@/components/StatusBadge";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { MapPanel } from "@/components/MapPanel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Switch } from "@/components/ui/switch";
import { del, patch, post } from "@/lib/api";
import { fmtAcc, fmtDms, fmtUtcDate } from "@/lib/format";
import { useLive } from "@/lib/live";
import { usePoints, useSessions } from "@/lib/queries";
import type { Point } from "@/lib/types";

const FORMATS = ["csv", "geojson", "kml", "gpx"] as const;

export default function Survey() {
  const { state, collect, lastSavedPointId } = useLive();
  const qc = useQueryClient();
  const sessions = useSessions();
  const [sessionFilter, setSessionFilter] = useState<number | undefined>(undefined);
  const points = usePoints(sessionFilter);
  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const [note, setNote] = useState("");
  const [epochs, setEpochs] = useState("30");
  const [fixedOnly, setFixedOnly] = useState(true);
  const [sessionName, setSessionName] = useState("");
  useEffect(() => { if (lastSavedPointId != null) qc.invalidateQueries({ queryKey: ["rover", "points"] }); }, [lastSavedPointId, qc]);
  const start = useMutation({ mutationFn: () => post("/api/rover/collect", { name, code: code || null, note: note || null, epochs: Number(epochs), fixed_only: fixedOnly }), onSuccess: () => toast.success(`Collecting ${name}`), onError: (e) => toast.error(String(e)) });
  const cancel = useMutation({ mutationFn: () => del("/api/rover/collect") });
  const startSession = useMutation({ mutationFn: () => post("/api/rover/sessions", { name: sessionName || null }), onSuccess: () => { setSessionName(""); qc.invalidateQueries({ queryKey: ["rover", "sessions"] }); } });
  const stopSession = useMutation({ mutationFn: () => post("/api/rover/sessions/stop"), onSuccess: () => qc.invalidateQueries({ queryKey: ["rover", "sessions"] }) });
  const update = useMutation({ mutationFn: ({ id, body }: { id: number; body: Partial<Pick<Point, "name" | "code" | "note">> }) => patch(`/api/rover/points/${id}`, body), onSuccess: () => qc.invalidateQueries({ queryKey: ["rover", "points"] }) });
  const remove = useMutation({ mutationFn: (id: number) => del(`/api/rover/points/${id}`), onSuccess: () => qc.invalidateQueries({ queryKey: ["rover", "points"] }) });
  const current = sessions.data?.find((s) => s.end_utc === null) ?? null;
  const collecting = collect?.state === "collecting";
  const exportQuery = (fmt: string) => `/api/rover/points/export?fmt=${fmt}${sessionFilter ? `&session_id=${sessionFilter}` : ""}`;
  return (
    <>
      <PageHeader title="Survey">{current ? <StatusBadge level="good" label={`Session ${current.name ?? current.id} open`} /> : <StatusBadge level="warning" label="No session open" />}</PageHeader>
      <div className="grid grid-cols-12 gap-4">
        <Panel className="col-span-12 lg:col-span-4" title="Session">
          {current ? (
            <>
              <p>{current.name ?? `#${current.id}`} started {fmtUtcDate(current.start_utc)}</p>
              <Button className="mt-2" variant="outline" onClick={() => stopSession.mutate()}>Stop session</Button>
            </>
          ) : (
            <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); startSession.mutate(); }}>
              <div className="flex-1"><Label htmlFor="session-name">Session name</Label><Input id="session-name" value={sessionName} onChange={(e) => setSessionName(e.target.value)} placeholder="site-2026-09-19" /></div>
              <Button type="submit" className="self-end">Start</Button>
            </form>
          )}
          <p className="mt-3 text-[12px] leading-4 text-ink-2">Sessions group points and mark the raw-log window used for PPK.</p>
        </Panel>
        <Panel className="col-span-12 lg:col-span-8" title="Collect a point">
          <form className="grid grid-cols-2 gap-3 md:grid-cols-4" onSubmit={(e) => { e.preventDefault(); start.mutate(); }}>
            <div><Label htmlFor="pt-name">Point name</Label><Input id="pt-name" value={name} onChange={(e) => setName(e.target.value)} disabled={collecting} /></div>
            <div><Label htmlFor="pt-code">Code</Label><Input id="pt-code" value={code} onChange={(e) => setCode(e.target.value)} placeholder="BM, FENCE…" disabled={collecting} /></div>
            <div><Label htmlFor="pt-note">Note</Label><Input id="pt-note" value={note} onChange={(e) => setNote(e.target.value)} disabled={collecting} /></div>
            <div><Label htmlFor="pt-epochs">Epochs</Label><Input id="pt-epochs" inputMode="numeric" value={epochs} onChange={(e) => setEpochs(e.target.value)} disabled={collecting} /></div>
            <label className="col-span-2 flex items-center gap-2 text-[14px]"><Switch checked={fixedOnly} onCheckedChange={setFixedOnly} aria-label="RTK fixed epochs only" disabled={collecting} />RTK fixed epochs only</label>
            <div className="col-span-2 flex justify-end gap-2">
              {collecting ? <Button type="button" variant="outline" onClick={() => cancel.mutate()}>Cancel</Button> : null}
              <Button type="submit" disabled={collecting || !name.trim() || !state}>Collect point</Button>
            </div>
          </form>
          {collect && collect.state !== "idle" ? (
            <div className="mt-4 rounded-md border border-line p-3">
              <div className="flex items-center justify-between"><span>{collect.name}</span><span className="num text-ink-2">{collect.accepted} of {collect.target} epochs{collect.skipped ? ` · ${collect.skipped} skipped` : ""}</span></div>
              <Progress value={(collect.accepted / Math.max(1, collect.target)) * 100} className="mt-2" />
              <div className="mt-2 grid grid-cols-3 gap-2"><Stat label="σ north" value={fmtAcc(collect.sd_n)} /><Stat label="σ east" value={fmtAcc(collect.sd_e)} /><Stat label="σ up" value={fmtAcc(collect.sd_u)} /></div>
              {collect.state === "aborted" ? <p className="mt-2 text-status-critical">Stopped: {collect.reason}</p> : collect.state === "done" ? <p className="mt-2 text-status-good">Saved.</p> : null}
            </div>
          ) : null}
        </Panel>
        <Panel className="col-span-12 lg:col-span-7" title={`Points (${points.data?.length ?? 0})`} bodyClassName="p-2" actions={
          <div className="flex items-center gap-2">
            <select aria-label="Session filter" value={sessionFilter ?? ""} onChange={(e) => setSessionFilter(e.target.value ? Number(e.target.value) : undefined)} className="rounded-md border border-line bg-panel-2 px-2 py-1 text-[12px]">
              <option value="">All sessions</option>{(sessions.data ?? []).map((s) => <option key={s.id} value={s.id}>{s.name ?? `#${s.id}`}</option>)}
            </select>
            {FORMATS.map((f) => <Button key={f} size="sm" variant="outline" asChild><a href={exportQuery(f)} download>{f.toUpperCase()}</a></Button>)}
          </div>
        }>
          <table className="w-full text-[14px]">
            <thead><tr className="border-b border-line text-left text-ink-2"><th className="py-1.5 pr-3 font-medium">Name</th><th className="py-1.5 pr-3 font-medium">Code</th><th className="py-1.5 pr-3 font-medium">Position</th><th className="py-1.5 pr-3 text-right font-medium">σ N/E/U</th><th className="py-1.5 pr-3 font-medium">Fix</th><th className="py-1.5 pr-3 font-medium">Time</th><th /></tr></thead>
            <tbody>
              {(points.data ?? []).map((p) => (
                <tr key={p.id} className="border-b border-line/60 last:border-0">
                  <td className="py-1.5 pr-3"><input aria-label={`Name of point ${p.id}`} defaultValue={p.name} onBlur={(e) => e.target.value !== p.name && update.mutate({ id: p.id, body: { name: e.target.value } })} className="w-full bg-transparent" /></td>
                  <td className="py-1.5 pr-3"><input aria-label={`Code of point ${p.id}`} defaultValue={p.code ?? ""} onBlur={(e) => e.target.value !== (p.code ?? "") && update.mutate({ id: p.id, body: { code: e.target.value } })} className="w-full bg-transparent" /></td>
                  <td className="num py-1.5 pr-3">{fmtDms(p.lat, true)} {fmtDms(p.lon, false)} · {p.height_m.toFixed(3)} m</td>
                  <td className="num py-1.5 pr-3 text-right">{(p.sd_n * 1000).toFixed(0)}/{(p.sd_e * 1000).toFixed(0)}/{(p.sd_u * 1000).toFixed(0)} mm</td>
                  <td className="py-1.5 pr-3">{p.carr_soln === 2 ? "RTK fixed" : p.carr_soln === 1 ? "RTK float" : "3D"} · {p.n_epochs} ep</td>
                  <td className="num py-1.5 pr-3">{fmtUtcDate(p.ts_utc).slice(0, 19)}</td>
                  <td className="py-1.5 text-right"><ConfirmDialog trigger={<Button size="sm" variant="ghost">Delete</Button>} title={`Delete ${p.name}?`} confirmLabel="Delete" destructive onConfirm={() => remove.mutateAsync(p.id).then(() => undefined)} /></td>
                </tr>
              ))}
              {points.data?.length === 0 ? <tr><td colSpan={7} className="py-6 text-center text-ink-3">No points yet.</td></tr> : null}
            </tbody>
          </table>
        </Panel>
        <Panel className="col-span-12 lg:col-span-5" title="Map" bodyClassName="p-0">
          <MapPanel lat={state?.position.lat ?? null} lon={state?.position.lon ?? null} hAcc={state?.accuracy.h_acc_m ?? null} points={(points.data ?? []).map((p) => ({ lat: p.lat, lon: p.lon, label: p.name }))} height={360} />
        </Panel>
      </div>
    </>
  );
}
```

- [ ] **Step 8: Run, build, commit**

`cd web && pnpm test && pnpm lint && pnpm build`; then
```bash
cd /home/nekosaif/github/mtrtk && git add web && git commit -m "feat(web): rover role UI — RTK page, survey page with point collection, role-adaptive shell

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Live rover check, docs, close-out

**Files:**
- Create: `tests/hardware/test_live_rover.py`, `docs/rover.md`
- Modify: `README.md`, `.env.example` (`NMEA_SENTENCES`, `NMEA_SLOW_INTERVAL_S`, `POINT_EPOCHS`, `POINT_FIXED_ONLY`)

- [ ] **Step 1: Write the live test (one F9P: plumbing only; RTK fixed needs a second receiver or a live caster)**

`tests/hardware/test_live_rover.py`:
```python
"""Rover plumbing on the real F9P: profile applies at 5 Hz, injected RTCM is seen by the receiver (RXM-RTCM),
NMEA is served over TCP. A real RTK fixed needs live corrections from a second receiver — see docs/rover.md."""

import asyncio
import os
from pathlib import Path

import pytest

from mtrtk.base.ntrip_caster import CasterConfig, NtripCaster
from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer, Proto
from mtrtk.daemon import Daemon

pytestmark = pytest.mark.hardware
BASE_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_base_30s.ubx"


async def test_live_rover_plumbing(tmp_path: Path) -> None:
    caster_bus = Bus()
    caster = NtripCaster(caster_bus, CasterConfig("MTRK", "rover", "pw", "MTRK", "BGD"), host="127.0.0.1", port=0)
    await caster.start()
    rtcm_frames = [f.raw for f in Framer().feed(BASE_FIXTURE.read_bytes()) if f.proto is Proto.RTCM3]

    async def feed() -> None:  # replay the recorded base corrections at ~1 Hz (stale: the receiver will count but not use them)
        while True:
            for raw in rtcm_frames[:20]:
                for f in Framer().feed(raw):
                    caster_bus.publish("raw.rtcm", f)
            await asyncio.sleep(1)

    feeder = asyncio.create_task(feed())
    settings = Settings(_env_file=None, role="rover", mtrtk_source=os.environ.get("MTRTK_TEST_PORT", "auto"), data_dir=tmp_path,
                        ntrip_url=f"ntrip://rover:pw@127.0.0.1:{caster.port}/MTRK", nmea_tcp_port=0, web_bind="127.0.0.1", web_port=0, web_allow_insecure=True, rover_nav_hz=5)
    daemon = Daemon(settings)
    run = asyncio.create_task(daemon.run())
    try:
        for _ in range(600):
            await asyncio.sleep(0.1)
            if daemon.rover and daemon.rover.ntrip_client and daemon.rover.ntrip_client.status.connected and daemon.controller.connected and daemon.store.state.epoch_count > 20:
                break
        assert daemon.controller.connected and daemon.rover is not None
        await asyncio.sleep(15)
        st = daemon.store.state
        print("epochs:", st.epoch_count, "rtcm_rx:", {k: v.model_dump() for k, v in st.rtk.rtcm_rx.items()}, "corr_age:", st.rtk.corr_age_s, "receiver age code:", st.rtk.corr_age_receiver_s)
        assert st.epoch_count > 50, "expected ~5 Hz epochs"
        assert st.rtk.rtcm_rx_total > 0, "receiver did not report any RXM-RTCM — injection path broken"
        assert st.rtk.corr_age_s is not None and st.rtk.corr_age_s < 5
        tcp = next(s for s in daemon.rover.nmea.sinks if hasattr(s, "client_count"))
        reader, writer = await asyncio.open_connection("127.0.0.1", tcp.port)
        lines = [await asyncio.wait_for(reader.readline(), 5.0) for _ in range(10)]
        writer.close()
        assert any(line.startswith(b"$GNGGA") for line in lines)
    finally:
        feeder.cancel()
        daemon.stop.set()
        await asyncio.wait_for(run, 30.0)
        await caster.stop()
```

- [ ] **Step 2: Run it (hardware step)** — `uv run pytest -m hardware tests/hardware/test_live_rover.py -s`. Expected `1 passed`; the printed `rtcm_rx` shows counts with `used == 0` (stale corrections) — that is the expected outcome with one receiver. Also run `uv run mtrtk rover` with a real `NTRIP_URL` if a public caster is reachable, and open `/rtk` in the UI.

- [ ] **Step 3: Write `docs/rover.md`**

```markdown
# Rover

`ROLE=rover` turns a ZED-F9P on a Pi/laptop/Jetson into an RTK rover fed by your base over Tailscale.

## Minimal `.env`
```
ROLE=rover
NTRIP_URL=ntrip://rover:<password>@<base-tailscale-ip>:2101/MTRK
ROVER_NAV_HZ=5
ROVER_DYNMODEL=automotive        # portable | pedestrian | automotive | airborne1g | airborne2g | airborne4g
NMEA_TCP_PORT=10110
```

## Outputs
- **NMEA over TCP** (`NMEA_TCP_PORT`, default 10110): QGIS → GPS Information panel → "Serial device / TCP" to `<rover-ip>:10110`; gpsd: `gpsd tcp://<rover-ip>:10110`; SW Maps / Mission Planner: TCP client.
- **NMEA over UDP** (`NMEA_UDP_TARGETS=host:port,host:port`).
- **NMEA on a serial device** (`NMEA_SERIAL=/dev/ttyUSB1`) or a **pseudo-terminal** (`NMEA_SERIAL=pty` → `DATA_DIR/ttyMTRTK`) for software that insists on a serial port.
- **JSON over UDP** (`JSON_UDP_PORT=5555`): one JSON object per epoch with position, accuracy, fix, RTK baseline, correction age — what the ROS 2 bridge consumes.
- Sentence set: `NMEA_SENTENCES=GGA,RMC,GST,GSA,GSV,VTG,ZDA` (HDT/PASHR are added automatically when an INS driver provides attitude).

## Reading the RTK page
Correction age under 5 s and "RTK fixed" is the goal. Float for minutes = weak signals or a long baseline; "no corrections" = check NTRIP_URL, the base, and Tailscale. "Received / Used" in the RTCM table tells you whether the receiver accepts the corrections (stale or distant base → received but not used).

## Survey points
Start a session, name a point, collect N epochs (default 30, RTK fixed only). Points are averaged in ENU with per-axis standard deviations and exported as CSV, GeoJSON, KML or GPX from the Survey page or `GET /api/rover/points/export?fmt=csv`.

## PPK
Raw UBX (RAWX/SFRBX + TIM-TM2 camera pulses on EXTINT) is logged hourly under `DATA_DIR/ubx/`, exactly like the base. Phase 8 adds the post-processing pipeline.

## Testing without a second receiver
`tests/hardware/test_live_rover.py` proves the plumbing with recorded base corrections; the receiver counts them but cannot use them (stale). A real fixed solution needs a live base within ~20 km.
```

- [ ] **Step 4: `.env.example`, README, commit, tag**

Add under the rover block of `.env.example`:
```
NMEA_SENTENCES=GGA,RMC,GST,GSA,GSV,VTG,ZDA
NMEA_SLOW_INTERVAL_S=1.0          # GSA/GSV/ZDA cadence
POINT_EPOCHS=30                   # default averaging length for survey points
POINT_FIXED_ONLY=1                # only accept RTK fixed epochs when collecting
```
README status: "Phase 6 (F9P rover) complete: NTRIP client, RTK status, NMEA/JSON outputs, sessions and survey points, rover UI. Next: ROS 2 bridge (Phase 7)."
```bash
git add tests/hardware/test_live_rover.py docs/rover.md README.md .env.example
git commit -m "docs: rover guide, live rover plumbing test, Phase 6 status

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git tag -a v0.6.0-phase6 -m "Phase 6: F9P rover"
```

Phase 7 (ROS 2 bridge) is the next plan: `docs/superpowers/plans/2026-09-19-phase7-ros2.md`.
