# mtrtk Phase 10: INS Drivers (SBG Ellipse-D, VectorNav VN-200) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the rover role run on an SBG Systems Ellipse-D or a VectorNav VN-200 instead of a ZED-F9P: parse each vendor's native serial protocol, fill the same normalized `ReceiverState` (position, accuracy, fix, velocity, time, satellites, attitude, RTK status, time marks) so NMEA/JSON/WebSocket/ROS 2/points/logging work unchanged, inject RTCM where the unit accepts it, capture raw GNSS where the unit provides it, and apply the vendor configuration the user asked for (lever arms, alignment, output set and rates, motion profile) through the vendor protocol. **No hardware is available**: everything is built from the published protocol specifications with hand-built fixtures, and every assumption that only hardware can confirm is recorded in `docs/ins-drivers.md` under *Unverified*.

**Architecture:** `Proto` gains `SBG` and `VN`; two new framers (`rover/drivers/sbg/framer.py`, `rover/drivers/vectornav/framer.py`) turn bytes into `Frame`s that the existing `Router` publishes as `raw.sbg`/`sbg.<LOG>` and `raw.vn`/`vn.<KIND>`. A generic `InsController` (open/reconnect/watchdog, one writer, request↔response correlation, a vendor `configure()` hook) replaces `ReceiverController` when `ROVER_DRIVER != ublox`. A per-vendor `StateAdapter` consumes vendor frames and writes `ReceiverState` sections, publishing `state.<section>` and `state.epoch` exactly like `StateStore` does for UBX, so every downstream consumer is untouched. Drivers implement Phase 6's `RoverDriver` protocol with honest `DriverCapabilities`. Raw GNSS: the Ellipse-D forwards its internal receiver's stream in `GPS1_RAW` logs; when that stream is UBX it is re-framed and published on `raw.ubx` so Phase 2's `RawLogWriter` logs it (RINEX-convertible); the VN-200's `RawMeas` binary field is stored as hourly `.vnraw` files (no RINEX conversion in this phase). Vendor configuration is a declarative profile rendered into commands, applied with ACK checking and read back for verification, mirroring Phase 1's UBX profile flow.

**Tech Stack:** Python `struct`, asyncio, pyserial-asyncio-fast, pydantic; sbgECom binary protocol (frame `FF 5A … 33`, CRC-16/KERMIT); VectorNav binary (`FA` sync, group/field bitmasks, CRC-16/XMODEM) and ASCII (`$VNRRG/$VNWRG`, XOR or CRC16 checksum).

**Spec:** `docs/superpowers/specs/2026-09-18-mtrtk-design.md` — *rover/drivers*, *INS drivers*, *Phase 10*, risk *INS drivers unverified*. Prerequisites: Phases 1–7 (Phase 6 `RoverDriver`, `Attitude`, `TimeMark`, `RtkStatus`; Phase 7 ROS bridge consumes `attitude`).

## Global Constraints

- **Spec-derived, hardware-unverified.** Each protocol constant that the implementer cannot confirm from a primary source (the sbgECom C headers or the VectorNav user manual) is marked `# VERIFY` in code and listed in `docs/ins-drivers.md`. Primary sources: `https://github.com/SBG-Systems/sbgECom` (`src/sbgEComIds.h`, `src/binaryLogs/*.h`, `src/commands/*.h`, `common/crc/sbgCrc.c`), VectorNav *VN-200 User Manual* (UM004) sections *Binary Output* and *Command Reference*. Task 2 generates the SBG id tables from the headers rather than hand-typing them.
- Frame CRC/checksum verification is mandatory before any byte is trusted; corrupted frames are counted (`FramerStats.crc_failed`) and resync proceeds from the next preamble.
- Normalized state fills the **same fields** with the **same units** as the UBX path: degrees for lat/lon and angles, metres, m/s, `TimeInfo.utc` timezone-aware, accuracies as 1σ metres (vendor values that are 1σ already are passed through; none of the fields here are 95 %). `FixInfo.fix_type` uses the UBX scale (0 none, 2 2D, 3 3D, 5 time-only) and `carr_soln` 0/1/2 (none/float/fixed) so GGA quality and ROS status mapping stay valid.
- `Attitude.heading_deg` is **true heading** (0–360, clockwise from north); roll/pitch in degrees; `source` is `"sbg-ekf"`, `"sbg-gnss-hdt"`, or `"vn-ins"`.
- `DriverCapabilities` are honest: SBG `accepts_rtcm=True` (via configured RTCM port), `raw_gnss_log=True` (UBX re-framing, only when the internal receiver streams UBX), `attitude=True`, `imu=True`, `sats=True` when `GPS1_SAT` is present, `spectrum=False`. VN-200 `accepts_rtcm=False` (no RTCM input on the VN-200), `raw_gnss_log=True` only after the `RawMeas` probe succeeds, `attitude=True`, `imu=True`, `sats=True` when `SatInfo` is present, `spectrum=False`.
- The daemon does not start the NTRIP client when `driver.capabilities.accepts_rtcm` is false; the RTK page says so.
- Vendor configuration is applied only when `INS_APPLY_CONFIG=1` (default `0` — a wrong lever arm silently degrades a customer's INS, so the first connection is read-only) and every write is followed by a read-back that must match; flash save (`SETTINGS_ACTION SAVE` / `$VNWNV`) happens once after a successful verify, never on reconnect.
- Rates: `INS_OUTPUT_HZ` (default 10, allowed 1–200 for SBG EKF/IMU logs; VN `rateDivisor = 800 // hz` for binary output 1) drives nav/attitude; `GPS1_*` and `SatInfo` at their native 1–5 Hz; the `state.epoch` publish rate follows the nav log rate, capped by `ROVER_NAV_HZ` for downstream (`StateAdapter` decimates).
- Commit per task, Conventional Commits, trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File structure (this plan)

| Path | Responsibility |
|---|---|
| `src/mtrtk/core/framer.py` (modify), `src/mtrtk/core/router.py` (modify) | `Proto.SBG`, `Proto.VN`; `Frame.identity/payload/parsed()` for vendor protos; topics `raw.sbg`/`sbg.<LOG>`, `raw.vn`/`vn.<KIND>` |
| `src/mtrtk/rover/drivers/ins_common.py` | `InsController` (source lifecycle, writer, request/response correlation), `StateAdapter` base (epoch publish, decimation), `RawCapture` (hourly opaque files) |
| `src/mtrtk/rover/drivers/sbg/{__init__,ids,crc,framer,logs,commands,adapter,driver,config}.py`, `scripts/gen-sbg-ids.py` | sbgECom protocol, Ellipse-D driver |
| `src/mtrtk/rover/drivers/vectornav/{__init__,checksum,framer,fields,parse,registers,adapter,driver,config}.py` | VectorNav protocol, VN-200 driver |
| `src/mtrtk/config.py` (modify), `src/mtrtk/daemon.py` (modify), `src/mtrtk/cli.py` (modify), `src/mtrtk/rawlog/writer.py` (modify: `note_utc`) | INS settings, driver selection, `mtrtk ins info|config` |
| `src/mtrtk/web/api/receiver.py` (modify), `src/mtrtk/web/api/status.py` (modify), `web/src/pages/Receiver.tsx` (modify), `web/src/pages/Rtk.tsx` (modify), `web/src/components/InsPanel.tsx` | INS in API and UI |
| `docs/ins-drivers.md` | wiring, config, verified/unverified matrix, hardware validation checklist |
| `tests/unit/test_framer_ins.py`, `tests/unit/sbg/test_{crc,framer,logs,commands,adapter,driver,config}.py`, `tests/unit/vectornav/test_{checksum,framer,parse,registers,adapter,config}.py`, `tests/unit/test_daemon_ins.py`, `tests/unit/test_web_ins.py`, `tests/fixtures/ins/*.hex` | tests and golden fixtures |

---

### Task 1: Protocol plumbing — `Proto.SBG`/`Proto.VN`, generic `InsController`, `StateAdapter` base, `RawCapture`, settings

**Files:**
- Modify: `src/mtrtk/core/framer.py`, `src/mtrtk/core/router.py`, `src/mtrtk/config.py`, `src/mtrtk/rawlog/writer.py`
- Create: `src/mtrtk/rover/drivers/ins_common.py`, `tests/unit/test_framer_ins.py`, `tests/unit/test_ins_common.py`

**Interfaces:**
- `Proto.SBG = "sbg"`, `Proto.VN = "vn"`. `Frame.identity`: SBG → `f"SBG-{cls:02X}-{id:02X}"` resolved to a name through a registry `FRAME_NAMERS: dict[Proto, Callable[[bytes], str]]` that vendor packages populate at import (`register_namer(Proto.SBG, fn)`); VN → `"VN-BIN"` for `0xFA` frames, `"VN-ASCII"` for `$VN…` lines. `Frame.payload`: SBG → `raw[6:-3]`; VN → `raw` (the vendor parser handles layout). `Frame.parsed()` dispatches to `FRAME_PARSERS[proto]` registered the same way (UBX/RTCM/NMEA parsers stay in place).
- `topics_for`: SBG → `("raw.sbg", f"sbg.{identity}")`; VN → `("raw.vn", f"vn.{identity}")`.
- `Framer` protocol (`core/framer.py` already exposes `feed(data) -> list[Frame]` and `.stats`); the vendor framers implement the same two members so `Router(bus, framer=SbgFramer())` works.
- `ins_common.InsController(bus, source_factory, framer_factory, configure: Callable[[InsController], Awaitable[None]] | None, rx_timeout_s=5.0)`: `await run(stop)` loop = open source → `bus.publish("receiver.connected", name)` → read/route until `b""` or `rx_timeout_s` silence → `receiver.disconnected` → backoff 1→30 s; `await write(data)` (single writer lock); `await request(match: Callable[[Frame], bool], send: bytes, timeout_s=2.0) -> Frame` (registers a waiter, writes, resolves on the first routed frame matching; `asyncio.TimeoutError` otherwise); `.connected`, `.router`, `.stats`; `configure` is awaited once per connection right after open (errors → `receiver.error` event, connection kept, so a read-only session still works).
- `ins_common.StateAdapter(bus, state: ReceiverState | None = None, nav_hz_cap: float = 5.0)`: owns `.state`, `publish_sections(changed: set[str])`, `end_epoch()` (publishes `state.epoch` with decimation to `nav_hz_cap`; increments `epoch_count`, sets `last_epoch_mono`), `note_rtcm_injected()` (same semantics as Phase 6 `StateStore.note_rtcm_injected`), `push_time_mark(mark)` (append with `MAX_TIME_MARKS`, publish `state.time_mark`), abstract `apply(frame) -> set[str]`.
- `ins_common.RawCapture(root, station_id, suffix, bus)`: hourly opaque files `DATA_DIR/ins/YYYY/DDD/{STATION}_{YYYYMMDD}_{HH}.{suffix}` with a `.json` sidecar (`start_utc`, `bytes`, `frames`, `vendor`); `note_utc(dt)` sets the rotation clock; `write(b)`; `close()`; publishes `rawcapture.rotated` (path).
- `RawLogWriter.note_utc(dt: datetime)`: public setter for the receiver clock (the adapter calls it from the vendor UTC log because no NAV-PVT flows when the raw stream is re-framed UBX without NAV-PVT).
- `Settings`: `ins_port: str | None = None` (`INS_PORT`, required when `rover_driver != ublox`; no auto-detect, FTDI ids are generic), `ins_baud: int = 115200`, `ins_rtcm_port: str | None = None` (SBG: separate serial device carrying RTCM to Port B when the main port cannot; `None` = inject on the main port), `ins_output_hz: int = 10` (1–200), `ins_apply_config: bool = False`, `ins_raw_gnss: bool = True`, `ins_lever_arm_gnss1: tuple[float,float,float] | None` (`"x,y,z"` metres, IMU→antenna, body frame), `ins_lever_arm_gnss2`, `ins_imu_lever_arm` (SBG only), `ins_imu_axis: str = "xyz"` (SBG axis mapping, see Task 4), `ins_motion_profile: str = "general"` (`general|automotive|marine|airplane|helicopter|uav|pedestrian` — validated per vendor in Tasks 4/5), `ins_init_position: tuple[float,float,float] | None` (lat, lon, alt for `INIT_PARAMETERS`). Validator: `rover_driver != "ublox"` and `ins_port is None` → `ValueError("INS_PORT is required for ROVER_DRIVER=…")`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_framer_ins.py`:
```python
import time

from mtrtk.core.framer import FRAME_NAMERS, FRAME_PARSERS, Frame, Proto, register_namer, register_parser
from mtrtk.core.router import topics_for


def test_proto_members() -> None:
    assert Proto.SBG.value == "sbg" and Proto.VN.value == "vn"


def test_sbg_identity_via_registry() -> None:
    raw = bytes([0xFF, 0x5A, 0x08, 0x00, 0x02, 0x00, 0xAA, 0xBB, 0x00, 0x00, 0x33])
    f = Frame(Proto.SBG, raw, time.monotonic(), time.time())
    FRAME_NAMERS.pop(Proto.SBG, None)
    assert f.identity == "SBG-00-08"
    register_namer(Proto.SBG, lambda r: {0x08: "EKF_NAV"}.get(r[2], f"SBG-{r[3]:02X}-{r[2]:02X}"))
    assert f.identity == "EKF_NAV"
    assert f.payload == b"\xaa\xbb"
    assert topics_for(f) == ("raw.sbg", "sbg.EKF_NAV")


def test_vn_identity_and_topics() -> None:
    binary = Frame(Proto.VN, b"\xfa\x01\x00\x00\x00\x00", time.monotonic(), time.time())
    ascii_ = Frame(Proto.VN, b"$VNRRG,01,VN-200*4B\r\n", time.monotonic(), time.time())
    assert binary.identity == "VN-BIN" and ascii_.identity == "VN-ASCII"
    assert topics_for(binary) == ("raw.vn", "vn.VN-BIN") and topics_for(ascii_) == ("raw.vn", "vn.VN-ASCII")


def test_parsed_dispatch_and_cache() -> None:
    calls = []
    register_parser(Proto.VN, lambda f: calls.append(1) or {"ok": True})
    f = Frame(Proto.VN, b"\xfa\x01\x00\x00\x00\x00", 0.0, 0.0)
    assert f.parsed() == {"ok": True} and f.parsed() == {"ok": True}
    assert calls == [1]
    FRAME_PARSERS.pop(Proto.VN, None)
```

`tests/unit/test_ins_common.py`:
```python
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mtrtk.core.bus import Bus
from mtrtk.core.framer import Frame, Proto
from mtrtk.core.state import ReceiverState
from mtrtk.rover.drivers.ins_common import InsController, RawCapture, StateAdapter


class ScriptedSource:
    """Yields queued chunks, then blocks until closed (simulating a quiet port)."""

    name = "scripted"

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.written: list[bytes] = []
        self.opened = 0
        self._closed = asyncio.Event()

    async def open(self) -> None:
        self.opened += 1

    async def read(self) -> bytes:
        if self.chunks:
            await asyncio.sleep(0)
            return self.chunks.pop(0)
        await self._closed.wait()
        return b""

    async def write(self, data: bytes) -> None:
        self.written.append(data)
        if data == b"PING":
            self.chunks.append(b"\xfa\x01\x00\x00\x00\x00")  # a fake reply frame

    async def close(self) -> None:
        self._closed.set()


class OneByteFramer:
    """Test framer: every 6 bytes starting with 0xFA is a VN frame."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        self.buf += data
        out = []
        while len(self.buf) >= 6:
            if self.buf[0] != 0xFA:
                del self.buf[0]
                continue
            out.append(Frame(Proto.VN, bytes(self.buf[:6]), 0.0, 0.0))
            del self.buf[:6]
        return out


async def test_controller_routes_frames_and_correlates_requests() -> None:
    bus = Bus()
    frames = bus.subscribe("raw.vn")
    events = bus.subscribe("receiver.*")
    src = ScriptedSource([b"\xfa\x01\x00\x00\x00\x00", b"garbage"])
    configured = asyncio.Event()

    async def configure(ctrl: InsController) -> None:
        reply = await ctrl.request(lambda f: f.proto is Proto.VN, b"PING", timeout_s=1.0)
        assert reply.raw[0] == 0xFA
        configured.set()

    ctrl = InsController(bus, lambda: src, OneByteFramer, configure, rx_timeout_s=0.5)
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    await asyncio.wait_for(configured.wait(), 2)
    assert src.written == [b"PING"] and ctrl.connected
    topic, first = frames.queue.get_nowait()
    assert topic == "raw.vn" and first.identity == "VN-BIN"
    assert events.queue.get_nowait()[0] == "receiver.connected"
    stop.set()
    await src.close()
    await asyncio.wait_for(task, 2)


async def test_controller_reconnects_after_silence() -> None:
    bus = Bus()
    events = bus.subscribe("receiver.*")
    src = ScriptedSource([])
    ctrl = InsController(bus, lambda: src, OneByteFramer, None, rx_timeout_s=0.05)
    ctrl.backoff_s = (0.01, 0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    await asyncio.sleep(0.3)
    stop.set()
    await src.close()
    await asyncio.wait_for(task, 2)
    topics = [t for t, _ in [events.queue.get_nowait() for _ in range(events.queue.qsize())]]
    assert topics.count("receiver.disconnected") >= 2 and src.opened >= 2


class DummyAdapter(StateAdapter):
    def apply(self, frame: Frame) -> set[str]:
        self.state.fix.num_sv += 1
        return {"fix"}


async def test_adapter_publishes_sections_and_decimated_epochs() -> None:
    bus = Bus()
    sections = bus.subscribe("state.fix")
    epochs = bus.subscribe("state.epoch")
    a = DummyAdapter(bus, nav_hz_cap=2.0)
    f = Frame(Proto.VN, b"\xfa\x01\x00\x00\x00\x00", 0.0, 0.0)
    for i in range(10):
        a.publish_sections(a.apply(f))
        a.end_epoch(now_mono=i * 0.1)  # 10 Hz input
    assert sections.queue.qsize() == 10
    assert 2 <= epochs.queue.qsize() <= 3  # capped near 2 Hz over 0.9 s
    assert a.state.epoch_count == 10


async def test_adapter_time_marks_and_rtcm_note() -> None:
    from mtrtk.core.state import MAX_TIME_MARKS, TimeMark

    bus = Bus()
    marks = bus.subscribe("state.time_mark")
    a = DummyAdapter(bus)
    for i in range(MAX_TIME_MARKS + 5):
        a.push_time_mark(TimeMark(channel=0, count=i, rising_week=2400, rising_tow_s=float(i), new_rising=True))
    assert len(a.state.time_marks) == MAX_TIME_MARKS and marks.queue.qsize() == MAX_TIME_MARKS + 5
    a.note_rtcm_injected(now_mono=100.0)
    assert a.state.rtk.last_rtcm_mono == 100.0
    a.end_epoch(now_mono=101.5)
    assert a.state.rtk.corr_age_s == pytest.approx(1.5)


async def test_raw_capture_rotates_hourly(tmp_path: Path) -> None:
    bus = Bus()
    rotated = bus.subscribe("rawcapture.rotated")
    cap = RawCapture(tmp_path, "MTRK", "vnraw", bus, vendor="vectornav")
    cap.write(b"early")  # buffered until a clock exists
    cap.note_utc(datetime(2026, 9, 19, 10, 59, 59, tzinfo=UTC))
    cap.write(b"abc")
    cap.note_utc(datetime(2026, 9, 19, 11, 0, 1, tzinfo=UTC))
    cap.write(b"def")
    cap.close()
    p10 = tmp_path / "ins" / "2026" / "262" / "MTRK_20260919_10.vnraw"
    p11 = p10.with_name("MTRK_20260919_11.vnraw")
    assert p10.read_bytes() == b"earlyabc" and p11.read_bytes() == b"def"
    side = json.loads(p10.with_suffix(".json").read_text())
    assert side["vendor"] == "vectornav" and side["bytes"] == 8 and side["start_utc"].startswith("2026-09-19T10:59:59")
    assert rotated.queue.qsize() == 2
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/unit/test_framer_ins.py tests/unit/test_ins_common.py -q` → ImportError.

- [ ] **Step 3: Extend `core/framer.py` and `core/router.py`**

In `framer.py`:
```python
class Proto(str, Enum):
    UBX = "ubx"
    RTCM3 = "rtcm3"
    NMEA = "nmea"
    SBG = "sbg"
    VN = "vn"


FRAME_NAMERS: dict[Proto, Callable[[bytes], str]] = {}
FRAME_PARSERS: dict[Proto, Callable[["Frame"], Any]] = {}


def register_namer(proto: Proto, fn: Callable[[bytes], str]) -> None:
    FRAME_NAMERS[proto] = fn


def register_parser(proto: Proto, fn: Callable[["Frame"], Any]) -> None:
    FRAME_PARSERS[proto] = fn
```
In `Frame.identity`, before the NMEA fallback:
```python
        if self.proto is Proto.SBG:
            namer = FRAME_NAMERS.get(Proto.SBG)
            return namer(self.raw) if namer else f"SBG-{self.raw[3]:02X}-{self.raw[2]:02X}"
        if self.proto is Proto.VN:
            return "VN-BIN" if self.raw[:1] == b"\xfa" else "VN-ASCII"
```
`Frame.payload`: `if self.proto is Proto.SBG: return self.raw[6:-3]`; VN returns `self.raw`. `Frame.parsed()`: after the cached check, `if (fn := FRAME_PARSERS.get(self.proto)) is not None: self._parsed = fn(self); return self._parsed` before the UBX/RTCM/NMEA branches. The existing UBX `Framer` is unchanged (it never sees `FF 5A` or `FA` preambles as valid frames; SBG/VN bytes are only ever fed to their own framers).

`router.py`:
```python
TOPIC_RAW_SBG = "raw.sbg"
TOPIC_RAW_VN = "raw.vn"
...
    if frame.proto is Proto.SBG:
        return (TOPIC_RAW_SBG, f"sbg.{frame.identity}")
    if frame.proto is Proto.VN:
        return (TOPIC_RAW_VN, f"vn.{frame.identity}")
```

- [ ] **Step 4: Write `rover/drivers/ins_common.py`**

```python
"""Vendor-neutral INS plumbing: serial lifecycle + request correlation, state adapter base, opaque raw capture."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mtrtk.core.bus import Bus
from mtrtk.core.framer import Frame
from mtrtk.core.router import Router
from mtrtk.core.source import ByteSource
from mtrtk.core.state import MAX_TIME_MARKS, ReceiverState, TimeMark

log = logging.getLogger(__name__)

Configure = Callable[["InsController"], Awaitable[None]]


class InsController:
    def __init__(
        self,
        bus: Bus,
        source_factory: Callable[[], ByteSource],
        framer_factory: Callable[[], Any],
        configure: Configure | None,
        rx_timeout_s: float = 5.0,
    ) -> None:
        self.bus = bus
        self.source_factory = source_factory
        self.framer_factory = framer_factory
        self.configure = configure
        self.rx_timeout_s = rx_timeout_s
        self.backoff_s: tuple[float, float] = (1.0, 30.0)
        self.connected = False
        self.router: Router | None = None
        self.source: ByteSource | None = None
        self.stats: dict[str, int] = {"bytes_in": 0, "frames": 0, "reconnects": 0}
        self._write_lock = asyncio.Lock()
        self._waiters: list[tuple[Callable[[Frame], bool], asyncio.Future[Frame]]] = []

    async def run(self, stop: asyncio.Event) -> None:
        delay = self.backoff_s[0]
        while not stop.is_set():
            source = self.source_factory()
            try:
                await source.open()
            except Exception as exc:
                self.bus.publish("receiver.error", f"open failed: {exc}")
                await self._sleep(delay, stop)
                delay = min(delay * 2, self.backoff_s[1])
                continue
            self.source, self.router = source, Router(self.bus, self.framer_factory())
            self.connected = True
            self.bus.publish("receiver.connected", source.name)
            delay = self.backoff_s[0]
            cfg_task = asyncio.create_task(self._configure_safely()) if self.configure else None
            reason = await self._read_loop(source, stop)
            if cfg_task:
                cfg_task.cancel()
                await asyncio.gather(cfg_task, return_exceptions=True)
            self.connected = False
            self._fail_waiters(ConnectionError(reason))
            await source.close()
            self.bus.publish("receiver.disconnected", reason)
            if stop.is_set():
                break
            self.stats["reconnects"] += 1
            await self._sleep(delay, stop)
            delay = min(delay * 2, self.backoff_s[1])

    async def _configure_safely(self) -> None:
        try:
            assert self.configure is not None
            await self.configure(self)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("INS configuration failed")
            self.bus.publish("receiver.error", f"configuration failed: {exc}")

    async def _read_loop(self, source: ByteSource, stop: asyncio.Event) -> str:
        stop_task = asyncio.create_task(stop.wait())
        try:
            while True:
                read_task = asyncio.create_task(source.read())
                done, _ = await asyncio.wait({read_task, stop_task}, timeout=self.rx_timeout_s, return_when=asyncio.FIRST_COMPLETED)
                if stop_task in done:
                    read_task.cancel()
                    return "stopped"
                if read_task not in done:
                    read_task.cancel()
                    return f"no data for {self.rx_timeout_s:g}s"
                data = read_task.result()
                if not data:
                    return "eof"
                self.stats["bytes_in"] += len(data)
                assert self.router is not None
                for frame in self.router.feed(data):
                    self.stats["frames"] += 1
                    self._resolve_waiters(frame)
        finally:
            stop_task.cancel()

    async def write(self, data: bytes) -> None:
        if not self.connected or self.source is None:
            raise ConnectionError("INS not connected")
        async with self._write_lock:
            await self.source.write(data)

    async def request(self, match: Callable[[Frame], bool], send: bytes, timeout_s: float = 2.0) -> Frame:
        fut: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
        self._waiters.append((match, fut))
        try:
            await self.write(send)
            return await asyncio.wait_for(fut, timeout_s)
        finally:
            self._waiters = [w for w in self._waiters if w[1] is not fut]

    def _resolve_waiters(self, frame: Frame) -> None:
        for match, fut in list(self._waiters):
            if not fut.done() and match(frame):
                fut.set_result(frame)

    def _fail_waiters(self, exc: Exception) -> None:
        for _, fut in self._waiters:
            if not fut.done():
                fut.set_exception(exc)
        self._waiters.clear()

    async def _sleep(self, delay: float, stop: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(stop.wait(), delay * random.uniform(0.8, 1.2))
        except TimeoutError:
            pass


class StateAdapter:
    """Vendor adapters subclass this and implement apply(frame) -> changed sections."""

    def __init__(self, bus: Bus, state: ReceiverState | None = None, nav_hz_cap: float = 5.0) -> None:
        self.bus = bus
        self.state = state or ReceiverState()
        self.nav_hz_cap = nav_hz_cap
        self._last_epoch_pub: float | None = None

    def apply(self, frame: Frame) -> set[str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def handle(self, frame: Frame) -> None:
        try:
            changed = self.apply(frame)
        except Exception:
            log.exception("failed to apply %s", frame.identity)
            return
        self.publish_sections(changed)

    def publish_sections(self, changed: set[str]) -> None:
        for section in changed:
            self.bus.publish(f"state.{section}", getattr(self.state, section))

    def end_epoch(self, now_mono: float | None = None) -> None:
        now = time.monotonic() if now_mono is None else now_mono
        self.state.epoch_count += 1
        self.state.last_epoch_mono = now
        if self.state.rtk.last_rtcm_mono is not None:
            self.state.rtk.corr_age_s = now - self.state.rtk.last_rtcm_mono
        if self._last_epoch_pub is None or now - self._last_epoch_pub >= 1.0 / self.nav_hz_cap - 1e-6:
            self._last_epoch_pub = now
            self.bus.publish("state.epoch", self.state)

    def note_rtcm_injected(self, now_mono: float | None = None) -> None:
        self.state.rtk.last_rtcm_mono = time.monotonic() if now_mono is None else now_mono

    def push_time_mark(self, mark: TimeMark) -> None:
        self.state.time_marks.append(mark)
        del self.state.time_marks[:-MAX_TIME_MARKS]
        self.bus.publish("state.time_mark", mark)
        self.bus.publish("state.time_marks", self.state.time_marks)


class RawCapture:
    """Hourly opaque capture (e.g. VectorNav RawMeas) with a JSON sidecar; rotation clock is set by the adapter."""

    def __init__(self, root: Path, station_id: str, suffix: str, bus: Bus, *, vendor: str) -> None:
        self.root, self.station_id, self.suffix, self.bus, self.vendor = Path(root), station_id, suffix, bus, vendor
        self._utc: datetime | None = None
        self._pending = bytearray()
        self._fh: Any = None
        self._path: Path | None = None
        self._side: dict[str, Any] = {}

    def note_utc(self, dt: datetime) -> None:
        self._utc = dt
        hour = dt.replace(minute=0, second=0, microsecond=0)
        if self._path is None or self._side.get("hour_utc") != hour.isoformat():
            self._rotate(hour)

    def write(self, data: bytes) -> None:
        if self._fh is None:
            self._pending += data
            return
        self._fh.write(data)
        self._side["bytes"] += len(data)
        self._side["frames"] += 1

    def _rotate(self, hour: datetime) -> None:
        self.close()
        path = self.root / "ins" / f"{hour:%Y}" / f"{hour:%j}" / f"{self.station_id}_{hour:%Y%m%d}_{hour:%H}.{self.suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("ab")
        self._path = path
        self._side = {"vendor": self.vendor, "station_id": self.station_id, "hour_utc": hour.isoformat(), "start_utc": self._utc.isoformat() if self._utc else None, "bytes": 0, "frames": 0}
        if self._pending:
            self.write(bytes(self._pending))
            self._pending.clear()
        self.bus.publish("rawcapture.rotated", path)

    def close(self) -> None:
        if self._fh is None:
            return
        self._fh.close()
        self._fh = None
        assert self._path is not None
        self._side["end_utc"] = self._utc.isoformat() if self._utc else None
        self._path.with_suffix(".json").write_text(json.dumps(self._side, indent=2))
```
Add `RawLogWriter.note_utc(self, dt: datetime) -> None: self._utc = dt` in `rawlog/writer.py` (used by the SBG adapter when it re-frames UBX without NAV-PVT).

- [ ] **Step 5: Settings**

Add the INS fields listed in *Interfaces* to `Settings` with a `field_validator` that parses `"x,y,z"` strings into 3-tuples of floats, and a `model_validator(mode="after")` enforcing `ins_port` for non-ublox drivers and `1 <= ins_output_hz <= 200`. Append an `# ---- INS drivers (ROVER_DRIVER=sbg_ellipse|vectornav)` block to `.env.example` with every key commented.

- [ ] **Step 6: Run tests, lint, commit**

`uv run pytest tests/unit/test_framer_ins.py tests/unit/test_ins_common.py tests/unit/test_framer.py tests/unit/test_router.py tests/unit/test_config.py -q` → all pass (existing framer/router tests unaffected).
```bash
git add src/mtrtk/core/framer.py src/mtrtk/core/router.py src/mtrtk/rover/drivers/ins_common.py src/mtrtk/config.py src/mtrtk/rawlog/writer.py .env.example tests/unit/test_framer_ins.py tests/unit/test_ins_common.py
git commit -m "feat(ins): vendor protocol slots in Frame/Router, generic INS controller, state adapter base, raw capture, INS settings

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: sbgECom protocol — ids (generated), CRC, framer, log parsers

**Files:**
- Create: `scripts/gen-sbg-ids.py`, `src/mtrtk/rover/drivers/sbg/__init__.py`, `src/mtrtk/rover/drivers/sbg/ids.py` (generated), `src/mtrtk/rover/drivers/sbg/crc.py`, `src/mtrtk/rover/drivers/sbg/framer.py`, `src/mtrtk/rover/drivers/sbg/logs.py`, `tests/unit/sbg/__init__.py`, `tests/unit/sbg/test_crc.py`, `tests/unit/sbg/test_framer.py`, `tests/unit/sbg/test_logs.py`, `tests/fixtures/ins/sbg_frames.hex`

**Primary source (verified 2026-09-19 against `SBG-Systems/sbgECom` tag `5.8.935-stable`):**
- Frame: `FF 5A | MSG_ID u8 | MSG_CLASS u8 | LEN u16le | PAYLOAD[LEN] | CRC u16le | 33`. CRC covers `MSG_ID … end of PAYLOAD` (LEN + 4 bytes). `SBG_ECOM_MAX_PAYLOAD_SIZE = 4086`. A LEN > 4086 marks an **extended frame** (payload = `transferId u8, pageIndex u16le, nrPages u16le, data…`, `payloadSize = LEN − 5`, max data 4081/page): used for settings import/export only → this phase reassembles nothing; extended frames are counted (`stats.extended_dropped`) and skipped.
- CRC: `sbgCrc16Compute` is table-driven CRC-16 with reflected polynomial `0x8408`, init `0`, no final XOR = CRC-16/KERMIT (`"123456789"` → `0x2189`).
- Classes: `LOG_ECOM_0 = 0x00`, `LOG_ECOM_1 = 0x01`, `LOG_NMEA_0 = 0x02`, `LOG_NMEA_1 = 0x03`, `LOG_THIRD_PARTY_0 = 0x04`, `LOG_NMEA_GNSS = 0x05`, `CMD_0 = 0x10`.
- Log ids (class 0): `STATUS 1, UTC_TIME 2, IMU_DATA 3 (deprecated), MAG 4, MAG_CALIB 5, EKF_EULER 6, EKF_QUAT 7, EKF_NAV 8, SHIP_MOTION 9, GPS1_VEL 13, GPS1_POS 14, GPS1_HDT 15, GPS2_VEL 16, GPS2_POS 17, GPS2_HDT 18, ODO_VEL 19, EVENT_A..E 24..28, GPS1_RAW 31, GPS2_RAW 38, IMU_SHORT 44, EVENT_OUT_A 45, EVENT_OUT_B 46, DIAG 48, RTCM_RAW 49, GPS1_SAT 50, GPS2_SAT 51, EKF_ROT_ACCEL_BODY 52, EKF_ROT_ACCEL_NED 53, EKF_VEL_BODY 54, SESSION_INFO 55`.
- Command ids (class 0x10): `ACK 0, SETTINGS_ACTION 1, IMPORT_SETTINGS 2, EXPORT_SETTINGS 3, INFO 4, INIT_PARAMETERS 5, MOTION_PROFILE_ID 7, IMU_ALIGNMENT_LEVER_ARM 8, AIDING_ASSIGNMENT 9, GNSS_1_MODEL_ID 17, GNSS_1_LEVER_ARM_ALIGNMENT 18 (deprecated), GNSS_1_REJECT_MODES 19, UART_CONF 23, SYNC_IN_CONF 26, SYNC_OUT_CONF 27, NMEA_TALKER_ID 29, OUTPUT_CONF 30, ADVANCED_CONF 32, FEATURES 33, OUTPUT_CLASS_ENABLE 35, VALIDITY_THRESHOLDS 38, GNSS_1_INSTALLATION 46, API_POST 47, API_GET 48`.
- Wire layouts (all little-endian; trailing fields are optional and version-dependent — parse while bytes remain):
  - `STATUS`: `timeStamp u32, generalStatus u16, comStatus2 u16, comStatus u32, aidingStatus u32, reserved2 u32, reserved3 u16, [uptime u32], [cpuUsage u8]`. General bits: `MAIN_POWER_OK 0, IMU_POWER_OK 1, GPS_POWER_OK 2, SETTINGS_OK 3, TEMPERATURE_OK 4, DATALOGGER_OK 5, CPU_OK 6`. Aiding bits: `GPS1_POS_RECV 0, GPS1_VEL_RECV 1, GPS1_HDT_RECV 2, GPS1_UTC_RECV 3, … MAG_RECV 8, ODO_RECV 9`.
  - `UTC_TIME`: `timeStamp u32, status u16, year u16, month i8, day i8, hour i8, minute i8, second i8, nanoSecond i32, gpsTimeOfWeek u32 (ms), [clkBiasStd f32, clkSfErrorStd f32, clkResidualError f32]`. `status`: bit 0 `CLOCK_STABLE_INPUT`, bits 1–4 clock state (`0 ERROR, 1 FREE_RUNNING, 2 STEERING, 3 VALID`), bit 5 `CLOCK_UTC_SYNC`, bits 6–9 UTC status (`0 INVALID, 1 NO_LEAP_SEC, 2 INITIALIZED`).
  - `IMU_SHORT`: `timeStamp u32, status u16, deltaVelocity i32[3] (1/1048576 m/s²), deltaAngle i32[3] (1/67108864 rad/s standard, 1/12304174 high-range — the unit picks; standard assumed, `# VERIFY`), temperature i16 (1/256 °C)`. `IMU_DATA` (legacy): `timeStamp u32, status u16, accel f32[3], gyro f32[3], temp f32, deltaVel f32[3], deltaAngle f32[3]`.
  - `EKF_EULER`: `timeStamp u32, euler f32[3] (roll, pitch, yaw rad), eulerStdDev f32[3] (rad, 1σ), status u32, [magDeclination f32, magInclination f32]`. `EKF_QUAT`: `timeStamp u32, quat f32[4] (w,x,y,z), eulerStdDev f32[3], status u32, [decl f32, incl f32]`. `EKF_NAV`: `timeStamp u32, velocity f32[3] (NED m/s), velocityStdDev f32[3], position f64[3] (lat °, lon °, altitude MSL m), undulation f32 (HAE = altitude + undulation), positionStdDev f32[3] (m, 1σ), status u32`. EKF status: bits 0–3 solution mode (`0 UNINITIALIZED, 1 VERTICAL_GYRO, 2 AHRS, 3 NAV_VELOCITY, 4 NAV_POSITION`), flags `ATTITUDE_VALID 1<<4, HEADING_VALID 1<<5, VELOCITY_VALID 1<<6, POSITION_VALID 1<<7, GPS1_POS_USED 1<<11, GPS1_HDT_USED 1<<13, ALIGN_VALID 1<<27`.
  - `GPS1_POS`: `timeStamp u32, status u32, timeOfWeek u32 (ms), latitude f64, longitude f64, altitude f64 (MSL), undulation f32, latitudeAccuracy f32, longitudeAccuracy f32, altitudeAccuracy f32 (1σ m), [numSvUsed u8, baseStationId u16, differentialAge u16 (0.01 s; 0xFFFF n/a)], [numSvTracked u8, statusExt u32], [nrDiagReboots u8, upTime u32]`. `status`: bits 0–5 status (`0 SOL_COMPUTED, 1 INSUFFICIENT_OBS, 2 INTERNAL_ERROR, 3 HEIGHT_LIMIT`), bits 6–11 type (`0 NO_SOLUTION, 1 UNKNOWN, 2 SINGLE, 3 PSRDIFF, 4 SBAS, 5 OMNISTAR, 6 RTK_FLOAT, 7 RTK_INT, 8 PPP_FLOAT, 9 PPP_INT, 10 FIXED`), bits 12–29 signals used (`GPS_L1 12, GPS_L2 13, GPS_L5 14, GLO_L1 15, GLO_L2 16, GLO_L3 17, GAL_E1 18, GAL_E5A 19, GAL_E5B 20, GAL_E5ALT 21, GAL_E6 22, BDS_B1 23, BDS_B2 24, BDS_B3 25, QZSS_L1 26, QZSS_L2 27, QZSS_L5 28, QZSS_L6 29`).
  - `GPS1_VEL`: `timeStamp u32, status u32, timeOfWeek u32, velocity f32[3] (NED), velocityAcc f32[3], course f32 (°), courseAcc f32`. Status bits 0–5 status, 6–11 type (`2 DOPPLER, 3 DIFFERENTIAL`).
  - `GPS1_HDT`: `timeStamp u32, status u16, timeOfWeek u32, heading f32 (° true), headingAccuracy f32, pitch f32, pitchAccuracy f32, [baseline f32], [numSvTracked u8], [numSvUsed u8]`. Status bits 0–5 (`0 SOL_COMPUTED …`), bit 6 `BASELINE_VALID`.
  - `EVENT_A..E`: `timeStamp u32, status u16, timeOffset0..3 u16 (µs offsets of 2nd–5th events within the 1 kHz window)`. Status: bit 0 `OVERFLOW`, bits 1–4 `OFFSET_n_VALID`.
  - `GPS1_SAT`: `timeStamp u32, reserved u32, nrSatellites u8, then per satellite: id u8, elevation i8, azimuth u16, flags u16, nrSignals u8, then per signal: id u8, flags u8, snr u8`. Satellite `flags`: bits 0–2 tracking status (`0 UNKNOWN, 1 SEARCHING, 2 TRACKING_UNKNOWN, 3 TRACKING_NOT_USED, 4 TRACKING_REJECTED, 5 TRACKING_USED`), bits 3–4 health (`0 UNKNOWN, 1 HEALTHY, 2 UNHEALTHY`), bits 5–6 elevation status, bits 7–10 constellation (`1 GPS, 2 GLONASS, 3 GALILEO, 4 BEIDOU, 5 QZSS, 6 SBAS, 7 IRNSS, 8 LBAND`). Signal `flags`: bits 0–2 tracking, bits 3–4 health, bit 5 `SNR_VALID`. Signal ids: GPS L1CA 14, L2C(M/L) 18/19/23, L5 27–29; GLONASS G1 CA 41, G2 CA 43; Galileo E1 60–64, E5b 65–67, E5a 68–70; BeiDou B1I 101, B2I 110, B1C 103–105, B2a 111–112; QZSS L1CA 153, L2C 156–158; SBAS L1 180.
  - `GPS1_RAW`: opaque `rawBuffer[LEN]` — the internal receiver's native stream (Ellipse-N/D: u-blox → UBX; `# VERIFY` on hardware, the driver sniffs `B5 62`).
  - `ACK`: `ackMsgId u8, ackMsgClass u8, errorCode u16le` (`0 = SBG_NO_ERROR`).
  - `INFO` response: `productCode char[32], serialNumber u32, calibrationRev u32, calibrationYear u16, calibrationMonth u8, calibrationDay u8, hardwareRev u32, firmwareRev u32` (revisions packed `major.minor.rev.build` as `u8.u8.u8.u8` from MSB — `# VERIFY` packing via `sbgVersion` macros in `common/version/sbgVersion.h`).

**Interfaces:**
- `scripts/gen-sbg-ids.py [--tag 5.8.935-stable]`: downloads `src/sbgEComIds.h` (raw GitHub URL) into the scratchpad, parses the `SBG_ECOM_CLASS_*`, `SBG_ECOM_LOG_*` (ECOM_0 enum only) and `SBG_ECOM_CMD_*` enum members with a regex over `NAME = value,` lines, and writes `src/mtrtk/rover/drivers/sbg/ids.py` with `CLASS: dict[str,int]`, `LOG: dict[str,int]`, `CMD: dict[str,int]`, the reverse maps `LOG_NAME: dict[int,str]`, `CMD_NAME`, and a header comment with the source tag and SHA-256 of the header. The generated file is committed; the script is re-run only to bump the tag.
- `crc.py`: `crc16_kermit(data: bytes) -> int` (table-driven).
- `framer.py`: `SbgFramer()` with `.feed(data) -> list[Frame]` and `.stats: FramerStats(frames, crc_failed, resyncs, extended_dropped, bytes_skipped)`; `encode(msg_class: int, msg_id: int, payload: bytes) -> bytes`; registers `Frame` namer (`LOG_NAME[msg_id]` for class 0, `"CMD-" + CMD_NAME[msg_id]` for class 0x10, `f"SBG-{cls:02X}-{id:02X}"` otherwise) and parser (`logs.parse`) at import. Resync policy: on CRC or ETX mismatch, drop the first byte and rescan (never trust LEN from a corrupt header).
- `logs.py`: dataclasses `SbgStatus, SbgUtcTime, SbgImuShort, SbgImuLegacy, SbgEkfEuler, SbgEkfQuat, SbgEkfNav, SbgGnssPos, SbgGnssVel, SbgGnssHdt, SbgEvent, SbgSatList (SbgSat(id, constellation, elevation, azimuth, tracking, health, signals: list[SbgSignal(id, tracking, health, snr, snr_valid)]))`, `SbgAck(msg_id, msg_class, error_code)`, `SbgInfo`; `parse(frame) -> object | None` (returns `None` for unhandled ids, never raises on short payloads — missing optional fields become `None`); helper enums/`IntFlag`s for the status bitfields; `gnss_pos_type(status) -> int`, `ekf_mode(status) -> int`.

- [ ] **Step 1: Generate ids**

Write `scripts/gen-sbg-ids.py` (≈60 lines: `urllib.request` download, regex `^\s*(SBG_ECOM_(CLASS|LOG|CMD)_[A-Z0-9_]+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)`, restrict `LOG_` members to the block between `SbgEComLog` enum start and `SBG_ECOM_LOG_ECOM_NUM_MESSAGES`, strip prefixes, emit sorted dicts). Run it; `ids.py` must contain `LOG["EKF_NAV"] == 8`, `LOG["GPS1_SAT"] == 50`, `CMD["OUTPUT_CONF"] == 30`, `CMD["GNSS_1_INSTALLATION"] == 46`, `CLASS["CMD_0"] == 0x10`. Add `tests/unit/sbg/test_ids.py` asserting those five values and that `LOG_NAME[31] == "GPS1_RAW"`.

- [ ] **Step 2: Write the failing tests (CRC, framer, logs)**

`tests/unit/sbg/test_crc.py`:
```python
from mtrtk.rover.drivers.sbg.crc import crc16_kermit


def test_kermit_check_value() -> None:
    assert crc16_kermit(b"123456789") == 0x2189
    assert crc16_kermit(b"") == 0
```

`tests/unit/sbg/test_framer.py`:
```python
import struct

from mtrtk.core.framer import Proto
from mtrtk.rover.drivers.sbg.framer import SbgFramer, encode
from mtrtk.rover.drivers.sbg.ids import CLASS, LOG


def ekf_nav_payload() -> bytes:
    return struct.pack("<I3f3f3df3fI", 123456, 0.5, -0.25, 0.01, 0.02, 0.02, 0.05, 23.7275, 90.3925, 12.5, -55.2, 0.3, 0.3, 0.6, (1 << 7) | (1 << 6) | 4)


def test_encode_roundtrip_and_identity() -> None:
    raw = encode(CLASS["LOG_ECOM_0"], LOG["EKF_NAV"], ekf_nav_payload())
    assert raw[:2] == b"\xff\x5a" and raw[-1] == 0x33 and raw[2] == 8 and raw[3] == 0
    frames = SbgFramer().feed(raw)
    assert len(frames) == 1 and frames[0].proto is Proto.SBG and frames[0].identity == "EKF_NAV"
    assert frames[0].payload == ekf_nav_payload()


def test_split_stream_garbage_and_crc_failure() -> None:
    good = encode(0, LOG["UTC_TIME"], bytes(21))
    bad = bytearray(good)
    bad[10] ^= 0xFF  # corrupt payload -> CRC mismatch
    f = SbgFramer()
    out = f.feed(b"\x00\x11" + bytes(bad) + good[:7])
    out += f.feed(good[7:] + b"\xff")  # a lone sync byte stays buffered
    assert [x.identity for x in out] == ["UTC_TIME"]
    assert f.stats.crc_failed == 1 and f.stats.frames == 1 and f.stats.bytes_skipped >= 2


def test_extended_frame_is_skipped() -> None:
    payload = b"\x01" + struct.pack("<HH", 0, 2) + bytes(100)
    body = bytes([3, 0x10]) + struct.pack("<H", 4086 + 1) + payload  # LEN > 4086 flags extended
    from mtrtk.rover.drivers.sbg.crc import crc16_kermit

    raw = b"\xff\x5a" + body + struct.pack("<H", crc16_kermit(body)) + b"\x33"
    f = SbgFramer()
    assert f.feed(raw) == [] and f.stats.extended_dropped == 1


def test_command_identity() -> None:
    raw = encode(CLASS["CMD_0"], 0, b"\x1e\x10\x00\x00")
    assert SbgFramer().feed(raw)[0].identity == "CMD-ACK"
```
(In `test_extended_frame_is_skipped` the encoder is bypassed because `encode` refuses payloads over 4086 bytes; the LEN field is what signals an extended frame, and the actual byte count that follows must equal LEN − 0: the framer treats LEN literally for slicing — write the test so the payload length equals LEN: `payload = b"\x01" + struct.pack("<HH", 0, 2) + bytes(4087 - 5)`.)

`tests/unit/sbg/test_logs.py`:
```python
import math
import struct

from mtrtk.rover.drivers.sbg import logs
from mtrtk.rover.drivers.sbg.framer import SbgFramer, encode
from mtrtk.rover.drivers.sbg.ids import LOG


def parse(msg_id: int, payload: bytes):
    frame = SbgFramer().feed(encode(0, msg_id, payload))[0]
    return frame.parsed()


def test_ekf_nav() -> None:
    p = struct.pack("<I3f3f3df3fI", 1000, 1.0, 2.0, -0.5, 0.1, 0.1, 0.2, 23.7275, 90.3925, 12.5, -55.2, 0.3, 0.4, 0.6, 4 | (1 << 7))
    nav = parse(LOG["EKF_NAV"], p)
    assert isinstance(nav, logs.SbgEkfNav)
    assert nav.lat == 23.7275 and nav.height_hae == 12.5 + -55.2 and nav.vel_ned == (1.0, 2.0, -0.5)
    assert logs.ekf_mode(nav.status) == 4 and nav.position_valid and not nav.heading_valid


def test_ekf_euler_radians_to_degrees_and_optional_tail() -> None:
    p = struct.pack("<I3f3fI", 1, 0.1, -0.2, math.pi / 2, 0.01, 0.01, 0.02, (1 << 4) | (1 << 5) | 4)
    e = parse(LOG["EKF_EULER"], p)
    assert e.roll_deg == math.degrees(0.1) and e.heading_deg == 90.0 and e.mag_declination is None
    assert e.attitude_valid and e.heading_valid


def test_gnss_pos_versions() -> None:
    base = struct.pack("<IIIdddffff", 1, (7 << 6) | 0 | (1 << 12), 1000, 23.0, 90.0, 10.0, -55.0, 0.02, 0.02, 0.05)
    short = parse(LOG["GPS1_POS"], base)
    assert short.pos_type == 7 and short.num_sv_used is None and short.rtk_fixed
    full = parse(LOG["GPS1_POS"], base + struct.pack("<BHH", 18, 7, 120) + struct.pack("<BI", 24, 0))
    assert full.num_sv_used == 18 and full.base_station_id == 7 and full.diff_age_s == 1.2 and full.num_sv_tracked == 24
    na = parse(LOG["GPS1_POS"], base + struct.pack("<BHH", 0xFF, 0xFFFF, 0xFFFF))
    assert na.num_sv_used is None and na.diff_age_s is None


def test_utc_time_status_decode() -> None:
    p = struct.pack("<IHHbbbbbiI", 5, (3 << 1) | (1 << 5) | (2 << 6), 2026, 9, 19, 10, 30, 15, 250_000_000, 37815250)
    t = parse(LOG["UTC_TIME"], p)
    assert t.utc.isoformat() == "2026-09-19T10:30:15.250000+00:00" and t.clock_state == 3 and t.utc_status == 2 and t.utc_sync


def test_utc_leap_second_60_clamps() -> None:
    p = struct.pack("<IHHbbbbbiI", 5, 0, 2026, 12, 31, 23, 59, 60, 0, 0)
    assert parse(LOG["UTC_TIME"], p).utc.second == 59


def test_gnss_hdt_and_vel() -> None:
    h = parse(LOG["GPS1_HDT"], struct.pack("<IHIffff", 1, 0 | (1 << 6), 1000, 91.25, 0.2, -1.0, 0.3) + struct.pack("<f", 1.02))
    assert h.heading_deg == 91.25 and h.baseline_m == 1.02 and h.solution_computed and h.baseline_valid
    v = parse(LOG["GPS1_VEL"], struct.pack("<IIIffffffff", 1, 2 << 6, 1000, 1.0, 0.0, 0.0, 0.1, 0.1, 0.1, 0.0, 1.0))
    assert v.vel_ned == (1.0, 0.0, 0.0) and v.course_deg == 0.0


def test_event_offsets() -> None:
    ev = parse(LOG["EVENT_B"], struct.pack("<IHHHHH", 5_000_000, 0b00110, 100, 250, 0, 0))
    assert ev.channel == "B" and ev.timestamp_us == 5_000_000 and ev.offsets_us == [100, 250]


def test_sat_list_parsing() -> None:
    sat1 = struct.pack("<BbHHB", 12, 45, 180, 5 | (1 << 3) | (1 << 7), 2) + struct.pack("<BBB", 14, 5 | (1 << 3) | (1 << 5), 44) + struct.pack("<BBB", 18, 3 | (1 << 3) | (1 << 5), 38)
    sat2 = struct.pack("<BbHHB", 3, 10, 90, 3 | (1 << 3) | (3 << 7), 1) + struct.pack("<BBB", 60, 3 | (1 << 3), 0)
    p = struct.pack("<IIB", 1, 0, 2) + sat1 + sat2
    lst = parse(LOG["GPS1_SAT"], p)
    assert len(lst.sats) == 2
    g = lst.sats[0]
    assert g.constellation == 1 and g.id == 12 and g.elevation == 45 and g.azimuth == 180 and g.used
    assert [s.snr for s in g.signals] == [44, 38] and g.signals[0].used and not g.signals[1].used
    assert lst.sats[1].constellation == 3 and lst.sats[1].signals[0].snr is None  # SNR_VALID clear


def test_imu_short_scaling() -> None:
    p = struct.pack("<IH3i3ih", 1, 0, 1048576, 0, -2097152, 67108864, 0, 0, 256 * 25)
    imu = parse(LOG["IMU_SHORT"], p)
    assert imu.accel_mps2 == (1.0, 0.0, -2.0) and imu.gyro_radps == (1.0, 0.0, 0.0) and imu.temperature_c == 25.0


def test_ack_and_unknown() -> None:
    ack = SbgFramer().feed(encode(0x10, 0, struct.pack("<BBH", 30, 0x10, 0)))[0].parsed()
    assert isinstance(ack, logs.SbgAck) and ack.msg_id == 30 and ack.ok
    assert parse(LOG["MAG"], bytes(10)) is None
```

- [ ] **Step 3: Run to verify failure** — ImportError.

- [ ] **Step 4: Write `crc.py`, `framer.py`, `logs.py`**

`crc.py`:
```python
"""CRC-16/KERMIT as used by sbgECom (sbgCrc16Compute): reflected poly 0x8408, init 0, no final xor."""

def _table() -> list[int]:
    out = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
        out.append(crc)
    return out


_TABLE = _table()


def crc16_kermit(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = _TABLE[(b ^ crc) & 0xFF] ^ (crc >> 8)
    return crc & 0xFFFF
```

`framer.py`:
```python
"""sbgECom binary framing: FF 5A | id | class | len(le16) | payload | crc(le16) | 33."""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from mtrtk.core.framer import Frame, Proto, register_namer, register_parser
from mtrtk.rover.drivers.sbg.crc import crc16_kermit
from mtrtk.rover.drivers.sbg.ids import CLASS, CMD_NAME, LOG_NAME

SYNC = b"\xff\x5a"
ETX = 0x33
MAX_PAYLOAD = 4086
HEADER = 6  # sync(2) id class len(2)
TRAILER = 3  # crc(2) etx


@dataclass
class FramerStats:
    frames: int = 0
    crc_failed: int = 0
    resyncs: int = 0
    extended_dropped: int = 0
    bytes_skipped: int = 0


def encode(msg_class: int, msg_id: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload exceeds 4086 bytes (extended frames are not supported)")
    body = bytes([msg_id, msg_class]) + struct.pack("<H", len(payload)) + payload
    return SYNC + body + struct.pack("<H", crc16_kermit(body)) + bytes([ETX])


def name_for(raw: bytes) -> str:
    msg_id, msg_class = raw[2], raw[3]
    if msg_class == CLASS["LOG_ECOM_0"]:
        return LOG_NAME.get(msg_id, f"SBG-00-{msg_id:02X}")
    if msg_class == CLASS["CMD_0"]:
        return "CMD-" + CMD_NAME.get(msg_id, f"{msg_id:02X}")
    return f"SBG-{msg_class:02X}-{msg_id:02X}"


class SbgFramer:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.stats = FramerStats()

    def feed(self, data: bytes) -> list[Frame]:
        self.buf += data
        out: list[Frame] = []
        while True:
            start = self.buf.find(SYNC)
            if start < 0:
                self.stats.bytes_skipped += max(len(self.buf) - 1, 0)
                del self.buf[:-1] if self.buf[-1:] == b"\xff" else self.buf[:]
                break
            if start:
                self.stats.bytes_skipped += start
                del self.buf[:start]
            if len(self.buf) < HEADER:
                break
            length = struct.unpack_from("<H", self.buf, 4)[0]
            total = HEADER + length + TRAILER
            if len(self.buf) < total:
                if length > MAX_PAYLOAD + 5:  # implausible length: resync now instead of waiting forever
                    self._skip_one()
                    continue
                break
            body = bytes(self.buf[2 : HEADER + length])
            crc = struct.unpack_from("<H", self.buf, HEADER + length)[0]
            if self.buf[total - 1] != ETX or crc != crc16_kermit(body):
                self.stats.crc_failed += 1
                self._skip_one()
                continue
            raw = bytes(self.buf[:total])
            del self.buf[:total]
            if length > MAX_PAYLOAD:
                self.stats.extended_dropped += 1
                continue
            self.stats.frames += 1
            now = time.monotonic()
            out.append(Frame(Proto.SBG, raw, now, time.time()))
        return out

    def _skip_one(self) -> None:
        self.stats.resyncs += 1
        self.stats.bytes_skipped += 1
        del self.buf[:1]


register_namer(Proto.SBG, name_for)
from mtrtk.rover.drivers.sbg import logs as _logs  # noqa: E402  (import cycle: logs imports ids only)

register_parser(Proto.SBG, _logs.parse)
```
(`Frame.payload` for SBG is `raw[6:-3]`, defined in Task 1.)

`logs.py` — write dataclasses with a `struct`-based reader helper:
```python
class _Reader:
    def __init__(self, data: bytes) -> None: self.d, self.o = data, 0
    def left(self) -> int: return len(self.d) - self.o
    def take(self, fmt: str): v = struct.unpack_from("<" + fmt, self.d, self.o); self.o += struct.calcsize("<" + fmt); return v
    def opt(self, fmt: str): return self.take(fmt) if self.left() >= struct.calcsize("<" + fmt) else None
```
and one `_parse_<log>` per layout above, `PARSERS: dict[int, Callable[[bytes], object]]` keyed by log id (EVENT_A..E share `_parse_event` with the channel letter bound), plus `_parse_ack` for class 0x10 id 0 and `_parse_info` for id 4. `parse(frame)` picks by `(raw[3], raw[2])`. Conversions: angles rad→deg (`math.degrees`), `height_hae = altitude + undulation`, `diff_age_s = differentialAge / 100` unless `0xFFFF`, `num_sv_* = None` when `0xFF`, `utc` built with `datetime(year, month, day, hour, minute, min(second, 59), nanoSecond // 1000, tzinfo=UTC)` (`second == 60` clamps and sets `leap_second_event=True`), `rtk_fixed = pos_type == 7`, `rtk_float = pos_type == 6`. Sat: `used = tracking == 5`, signal `snr = None` when `SNR_VALID` clear. IMU_SHORT: `accel = dv / 1048576`, `gyro = da / 67108864`, `temperature_c = t / 256`.

- [ ] **Step 5: Golden fixture**

`tests/fixtures/ins/sbg_frames.hex`: one line per frame (hex), produced once by a small script from the encoders used in tests (EKF_NAV, EKF_EULER, UTC_TIME, GPS1_POS full, GPS1_HDT, GPS1_SAT, EVENT_B, IMU_SHORT, STATUS, ACK). `test_framer.py::test_golden_fixture_parses` feeds the concatenated bytes and asserts the identity sequence and that every `parsed()` is not `None`. This pins the encoders so later refactors cannot silently shift a field.

- [ ] **Step 6: Run tests, lint, commit**

`uv run pytest tests/unit/sbg -q` → all pass.
```bash
git add scripts/gen-sbg-ids.py src/mtrtk/rover/drivers/sbg tests/unit/sbg tests/fixtures/ins/sbg_frames.hex
git commit -m "feat(sbg): sbgECom ids (generated from 5.8.935-stable), CRC-16/KERMIT, framer and log parsers

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: SBG state adapter and Ellipse-D driver (RTCM inject, raw GNSS re-framing, time marks)

**Files:**
- Create: `src/mtrtk/rover/drivers/sbg/adapter.py`, `src/mtrtk/rover/drivers/sbg/driver.py`, `tests/unit/sbg/test_adapter.py`, `tests/unit/sbg/test_driver.py`

**Interfaces:**
- `SbgStateAdapter(bus, *, nav_hz_cap, raw_writer: RawLogWriter | None, ubx_framer_factory=Framer)` (`StateAdapter` subclass). Mapping:
  - `EKF_NAV` → `position.lat/lon`, `position.height_m = height_hae`, `position.hmsl_m = altitude`, `accuracy.h_acc_m = hypot(σlat, σlon)`, `accuracy.v_acc_m = σalt`, `velocity.vel_n/e/d_mps`, `ground_speed_mps`, `heading_motion_deg`, `accuracy.s_acc_mps = hypot(σvel)`, `fix.gnss_fix_ok = position_valid`, `fix.fix_type = 3 if position_valid else (2 if ekf_mode >= 3 else 0)` plus names; then `end_epoch()` (EKF_NAV is the epoch driver).
  - `GPS1_POS` → `fix.carr_soln` (`7 → 2`, `6 → 1`, else `0`) + `carr_soln_name`, `fix.diff_soln = pos_type in (3,4,6,7,8,9)`, `fix.num_sv = num_sv_used`, `sat_summary.tracked/used` when `GPS1_SAT` absent, `rtk.ref_station_id`, `rtk.corr_age_receiver_s = diff_age_s`, `rtk.carr_soln*`; `raw_gnss` counters in `state.hardware` are not touched. (The EKF position is what NMEA/JSON publish; the GNSS-only fix lives in `rtk` and `fix.carr_soln` so the UI can show "INS 3D / GNSS RTK fixed".)
  - `GPS1_VEL` → nothing in `velocity` (EKF wins) but stored for the INS panel (`state.ins.gnss_vel`).
  - `GPS1_HDT` → `attitude` fallback when the EKF heading is invalid: `Attitude(heading=hdt.heading, source="sbg-gnss-hdt")`; also `rtk.baseline_m = baseline_m`, `rtk.heading_deg`, `rtk.heading_valid = solution_computed`.
  - `EKF_EULER` → `attitude = Attitude(roll, pitch, heading=yaw mod 360, acc_* from stdDev, source="sbg-ekf")` when `attitude_valid`; heading kept only when `heading_valid` else `None`.
  - `UTC_TIME` → `time.utc`, `time.gps_tow_s = gpsTimeOfWeek/1000`, `time.valid_time/valid_date/fully_resolved = utc_status == 2`, `time.valid_utc = utc_sync`; calls `raw_writer.note_utc(utc)` and `self._utc_anchor = (timestamp_us, utc)` for event conversion.
  - `EVENT_A..E` → `TimeMark(channel=ord(ch)-65, count=running, rising_utc=anchor_utc + (timestamp_us − anchor_us) µs, rising_week/tow from that UTC via gps_from_utc, new_rising=True)` for the frame's own timestamp and each valid offset; pushed via `push_time_mark`.
  - `GPS1_SAT` → `sats` (one `Satellite` per entry with `gnss_id` mapped `GPS 0, SBAS 1, GAL 2, BDS 3, QZSS 5, GLO 6` (UBX gnssId scale used by the UI), `signals` from signal entries with `cno = snr`, `used`), `sat_summary` recomputed → `{"sats", "sat_summary"}`.
  - `STATUS` → `state.ins.status` (new `InsStatus` model in `core/state.py`: `general_ok: dict[str,bool]`, `aiding: dict[str,bool]`, `uptime_s`, `cpu_pct`, `com_status`) → `{"ins"}`. Add `ins: InsStatus | None = None` and `imu: ImuSample | None = None` (`accel_mps2`, `gyro_radps`, `temperature_c`, `timestamp_us`) to `ReceiverState` (both `None` for the UBX path).
  - `IMU_SHORT`/`IMU_DATA` → `state.imu` at most 10 Hz publish (decimate; IMU can run at 200 Hz).
  - `GPS1_RAW` → if `raw_gnss` enabled: bytes go through an internal UBX `Framer`; each UBX `Frame` is published on `raw.ubx` + `ubx.<IDENT>` (via `topics_for`) so `RawLogWriter` logs it; if the first 64 bytes of the first payload contain no `B5 62`, publish `receiver.error` once ("GPS1_RAW is not UBX; raw capture disabled") and stop feeding (opaque capture via `RawCapture(..., suffix="sbgraw")` when `INS_RAW_GNSS=1`).
- `SbgDriver(controller: InsController, adapter, *, rtcm_source: ByteSource | None)`: `name="sbg_ellipse"`, `capabilities` (see constraints; `sats` becomes `True` after the first `GPS1_SAT`), `async inject_rtcm(data)`: writes to `rtcm_source` (separate Port B cable) when configured, else to the main port through `controller.write` (`# VERIFY` same-port RTCM multiplexing on Port A), then `adapter.note_rtcm_injected()`; counts `dropped_bytes` when disconnected. `.info: SbgInfo | None` set by Task 4's configure.

- [ ] **Step 1: Write the failing tests**

`tests/unit/sbg/test_adapter.py` (uses the encoders from Task 2's tests via a shared `tests/unit/sbg/helpers.py`: `ekf_nav(lat, lon, alt, undulation, status)`, `gps_pos(pos_type, num_sv, diff_age)`, `utc(dt)`, `event(ch, ts_us, offsets)`, `ekf_euler(r, p, y, status)`, `hdt(heading, baseline)`, `sat_list(...)`, `gps_raw(payload)` each returning a `Frame` through `SbgFramer`):
- `test_ekf_nav_drives_epoch_and_position`: feed UTC then EKF_NAV → `state.position.lat`, `height_m == alt + undulation`, `fix.fix_type == 3`, one `state.epoch` published, `time.utc` set.
- `test_gps_pos_sets_carr_soln_and_rtk`: pos_type 7 → `fix.carr_soln == 2`, `carr_soln_name == "Fixed"`, `rtk.corr_age_receiver_s == 1.2`, `rtk.ref_station_id == 7`; pos_type 2 → `carr_soln 0`, `diff_soln False`.
- `test_attitude_from_ekf_and_fallback_hdt`: EKF_EULER with heading invalid → `attitude.heading_deg is None`, roll/pitch set, `source == "sbg-ekf"`; then HDT → heading filled from HDT with `source == "sbg-gnss-hdt"`; EKF_EULER with heading valid → source back to `sbg-ekf`, `heading_deg == yaw mod 360` (yaw −90° → 270°).
- `test_events_become_time_marks`: UTC anchor at ts 1_000_000 µs = 10:00:00; EVENT_B at ts 1_500_000 with offsets [100, 250] → three `TimeMark`s with `rising_utc` = 10:00:00.5, .5001, .50025 and `channel == 1`; `state.time_mark` published 3×.
- `test_sat_list_maps_to_satellites`: two sats → `state.sats` len 2, GPS `gnss_id 0`, Galileo `gnss_id 2`, `sat_summary.used == 1`, `signals[0].cno == 44`.
- `test_gps_raw_reframed_to_ubx_and_time_forwarded`: a `RawLogWriter`-like fake with `note_utc` + a `raw.ubx` subscriber; feed GPS1_RAW containing one valid UBX NAV-PVT frame split across two GPS1_RAW payloads → one `raw.ubx` frame published; UTC frame → fake `note_utc` called.
- `test_gps_raw_not_ubx_disables_capture`: GPS1_RAW with `b"$GNGGA..."` → `receiver.error` published once, later GPS1_RAW ignored, `adapter.raw_gnss_format == "unknown"`.
- `test_status_and_imu_sections`: STATUS → `state.ins.general_ok["main_power"] is True`; IMU_SHORT ×50 in 0.1 s (fake clock) → `state.imu` updated but `state.imu` published ≤ 2 times.

`tests/unit/sbg/test_driver.py`:
- `test_inject_rtcm_prefers_separate_port`: fake `rtcm_source` with `.written`; `inject_rtcm(b"\xd3...")` → written there, not to controller; `adapter.state.rtk.last_rtcm_mono` set.
- `test_inject_rtcm_same_port_when_no_rtcm_source`: controller fake with `connected=True`, `write` records → data written; when `connected=False` → `dropped_bytes` grows, no exception.
- `test_capabilities`: `accepts_rtcm True, attitude True, imu True, spectrum False`; `raw_gnss_log` True initially, flips to False after the adapter marks raw format unknown (driver reads `adapter.raw_gnss_format`).

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement `adapter.py` and `driver.py`**

Skeleton of the adapter (full mapping per *Interfaces*):
```python
class SbgStateAdapter(StateAdapter):
    def __init__(self, bus, *, nav_hz_cap=5.0, raw_writer=None, raw_capture=None, raw_gnss=True):
        super().__init__(bus, nav_hz_cap=nav_hz_cap)
        self.raw_writer, self.raw_capture, self.raw_gnss = raw_writer, raw_capture, raw_gnss
        self.raw_gnss_format: str = "unknown-yet"  # "ubx" | "unknown"
        self._ubx = Framer()
        self._utc_anchor: tuple[int, datetime] | None = None
        self._event_count = 0
        self._last_imu_pub = 0.0
        self._sats_seen = False
        self._handlers = {"EKF_NAV": self._ekf_nav, "EKF_EULER": self._ekf_euler, "GPS1_POS": self._gps_pos, "GPS1_VEL": self._gps_vel, "GPS1_HDT": self._gps_hdt, "UTC_TIME": self._utc, "STATUS": self._status, "IMU_SHORT": self._imu, "IMU_DATA": self._imu, "GPS1_SAT": self._sats, "GPS1_RAW": self._gps_raw, **{f"EVENT_{c}": self._event for c in "ABCDE"}}

    def apply(self, frame: Frame) -> set[str]:
        handler = self._handlers.get(frame.identity)
        if handler is None:
            return set()
        msg = frame.parsed()
        return handler(msg, frame) if msg is not None else set()
```
`_gps_raw`: 
```python
    def _gps_raw(self, msg, frame) -> set[str]:
        payload = frame.payload
        if not self.raw_gnss or self.raw_gnss_format == "unknown":
            return set()
        if self.raw_gnss_format == "unknown-yet":
            if b"\xb5\x62" in payload[:64]:
                self.raw_gnss_format = "ubx"
            elif len(payload) >= 64:
                self.raw_gnss_format = "unknown"
                self.bus.publish("receiver.error", "GPS1_RAW does not look like UBX; raw GNSS capture disabled (see docs/ins-drivers.md)")
                if self.raw_capture:
                    self.raw_capture.write(payload)
                return set()
        for ubx in self._ubx.feed(payload):
            for topic in topics_for(ubx):
                self.bus.publish(topic, ubx)
        if self.raw_capture and self.raw_gnss_format != "ubx":
            self.raw_capture.write(payload)
        return set()
```
Time marks: `rising_utc = anchor_utc + timedelta(microseconds=ts_us - anchor_us)`; `rising_week, rising_tow_s = gps_from_utc(rising_utc, leap_s=state.time.leap_s or 18)` (add `gps_from_utc` next to Phase 6's `gps_to_utc` in `core/state.py`). Without an anchor, marks are buffered (max 100) and flushed on the first UTC.

Driver:
```python
class SbgDriver:
    name = "sbg_ellipse"

    def __init__(self, controller: InsController, adapter: SbgStateAdapter, rtcm_source: ByteSource | None = None) -> None:
        self.controller, self.adapter, self.rtcm_source = controller, adapter, rtcm_source
        self.dropped_bytes = 0
        self.info: SbgInfo | None = None

    @property
    def capabilities(self) -> DriverCapabilities:
        return DriverCapabilities(accepts_rtcm=True, raw_gnss_log=self.adapter.raw_gnss and self.adapter.raw_gnss_format != "unknown", attitude=True, imu=True, sats=self.adapter.sats_seen, spectrum=False)

    async def inject_rtcm(self, data: bytes) -> None:
        try:
            if self.rtcm_source is not None:
                await self.rtcm_source.write(data)
            else:
                await self.controller.write(data)  # VERIFY: RTCM multiplexed with sbgECom on Port A
        except ConnectionError:
            self.dropped_bytes += len(data)
            return
        self.adapter.note_rtcm_injected()
```
(`DriverCapabilities` is a frozen dataclass in Phase 6; a property returning a fresh instance keeps the `RoverDriver` protocol satisfied while letting `raw_gnss_log`/`sats` reflect what the unit actually sends.)

- [ ] **Step 4: Run tests, lint, commit**

```bash
uv run pytest tests/unit/sbg -q
git add src/mtrtk/rover/drivers/sbg/adapter.py src/mtrtk/rover/drivers/sbg/driver.py src/mtrtk/core/state.py tests/unit/sbg
git commit -m "feat(sbg): Ellipse-D state adapter (EKF/GNSS/UTC/events/sats/IMU) and driver with RTCM inject and UBX raw re-framing

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: SBG vendor configuration — commands, ACK correlation, profile apply with read-back

**Files:**
- Create: `src/mtrtk/rover/drivers/sbg/commands.py`, `src/mtrtk/rover/drivers/sbg/config.py`, `tests/unit/sbg/test_commands.py`, `tests/unit/sbg/test_config.py`

**Protocol facts (sbgECom 5.8, `src/commands/*.c`):**
- **Get** = send the command id (class `0x10`) with the *selector* payload (often empty); the device answers with **the same command id** carrying the full payload. **Set** = send the command id with the full payload; the device answers with `ACK` (id 0): `ackMsgId u8, ackMsgClass u8, errorCode u16le` (`0` = OK). The C library retries each command up to 3× with a 500 ms timeout.
- Payloads (little-endian):
  - `INFO (4)`: get, empty → `productCode[32] (NUL-padded ASCII), serialNumber u32, calibrationRev u32, calibrationYear u16, calibrationMonth u8, calibrationDay u8, hardwareRev u32, firmwareRev u32`. Revisions: `sbgVersion` packing — major `(v >> 24) & 0xFF`, minor `(v >> 16) & 0xFF`, rev `(v >> 8) & 0xFF`, build `v & 0xFF` for "basic" versions; software versions use a different layout (`# VERIFY` against `common/version/sbgVersion.h`; display raw hex too).
  - `OUTPUT_CONF (30)`: set `outputPort u8, msgId u8, classId u8, mode u16` (**note the id-before-class order**); get selector `outputPort u8, msgId u8, classId u8` → reply `outputPort, msgId, classId, mode u16`. Ports: `A 0, C 2, D 3, E 4`. Modes: `DISABLED 0, MAIN_LOOP 1 (200 Hz), DIV_2 2, DIV_4 4, DIV_5 5, DIV_8 8, DIV_10 10, DIV_20 20 (10 Hz), DIV_40 40 (5 Hz), DIV_100 100 (2 Hz, fw v3+), DIV_200 200 (1 Hz), PPS 10000, NEW_DATA 10001, EVENT_IN_A..E 10003..10007`.
  - `OUTPUT_CLASS_ENABLE (35)`: set `outputPort u8, classId u8, enable u8`.
  - `GNSS_1_INSTALLATION (46)`: set/reply `leverArmPrimary f32[3], leverArmPrimaryPrecise u8 (bool), leverArmSecondary f32[3], leverArmSecondaryMode u8` (`SINGLE 1, DUAL_AUTO 2, DUAL_ROUGH 3, DUAL_PRECISE 4`); get selector empty. Lever arms in IMU X/Y/Z axes, metres, IMU → antenna.
  - `IMU_ALIGNMENT_LEVER_ARM (8)`: set/reply `axisDirectionX u8, axisDirectionY u8, misRoll f32, misPitch f32, misYaw f32 (rad), leverArm f32[3] (IMU → vehicle reference point, m)`. Axis directions: `FORWARD 0, BACKWARD 1, LEFT 2, RIGHT 3, UP 4, DOWN 5`.
  - `MOTION_PROFILE_ID (7)`: generic model id, set `modelId u32`, get empty → `modelId u32`. Ids: `GENERAL_PURPOSE 1, AUTOMOTIVE 2, MARINE 3, AIRPLANE 4, HELICOPTER 5, PEDESTRIAN 6, UAV_ROTARY_WING 7, HEAVY_MACHINERY 8, STATIC 9, TRUCK 10, RAILWAY 11, OFF_ROAD_VEHICLE 12, UNDERWATER 13`.
  - `AIDING_ASSIGNMENT (9)`: set `gps1Port u8, gps1Sync u8, reserved u32 = 0, dvlPort u8, dvlSync u8, rtcmPort u8, airDataPort u8, odometerPinsConf u8`; get empty → same layout. Port assignment: `PORT_A 0, PORT_B 1, PORT_C 2, PORT_D 3, PORT_E 4, INTERNAL 5, DISABLED 0xFF`. Ellipse-D: `gps1Port = INTERNAL`. RTCM: Port B is the documented "auxiliary input interface for RTCM"; `rtcmPort = PORT_A` (same cable as sbgECom) is `# VERIFY`.
  - `INIT_PARAMETERS (5)`: set `latitude f64, longitude f64, altitude f64 (HAE), year u16, month u8, day u8`.
  - `SETTINGS_ACTION (1)`: set `action u8` (`REBOOT_ONLY 0, SAVE_SETTINGS 1 (saves **and reboots**), RESTORE_DEFAULT 2`) → ACK, then the device reboots (link drops for ~2–3 s).
  - `UART_CONF (23)`: get selector `interfaceId u8` → `interfaceId u8, baudRate u32, mode u8` (mode enum `# VERIFY`; read-only use here for display). Interface ids `COM_A 0, COM_B 1, COM_C 2, COM_D 3, COM_E 4`.

**Interfaces:**
- `commands.py`: `SbgCommands(controller: InsController)` with `await get(cmd: int, selector: bytes = b"", timeout_s=0.5, retries=3) -> bytes` (matches a reply `class 0x10, id == cmd`, or raises `SbgCommandError` when an ACK for that cmd with error arrives), `await set(cmd: int, payload: bytes) -> None` (waits for `ACK` with `ackMsgId == cmd`; raises `SbgCommandError(cmd, code)` on non-zero code / timeout); typed wrappers: `get_info() -> SbgInfo`, `get_output_conf(port, cls, msg_id) -> int`, `set_output_conf(port, cls, msg_id, mode)`, `set_output_class_enable(port, cls, enable)`, `get_gnss_installation() -> GnssInstallation`, `set_gnss_installation(GnssInstallation)`, `get_imu_alignment() -> ImuAlignment`, `set_imu_alignment(ImuAlignment)`, `get_motion_profile() -> int`, `set_motion_profile(int)`, `get_aiding_assignment() -> AidingAssignment`, `set_aiding_assignment(AidingAssignment)`, `set_init_parameters(lat, lon, alt_hae, date)`, `settings_action(action)`, `get_uart_conf(interface) -> UartConf`. Pure encoders/decoders `encode_*`/`decode_*` are module-level so tests need no I/O.
- `config.py`: `SbgProfile` (dataclass built from `Settings` by `sbg_profile(settings) -> SbgProfile`: `output_hz`, `outputs: list[tuple[int, int, int]]` (class, id, mode), `disable_classes: list[int]`, `gnss_installation: GnssInstallation | None`, `imu_alignment: ImuAlignment | None`, `motion_profile: int | None`, `aiding: {"rtcm_port": int} | None`, `init_position`); `hz_to_mode(hz) -> int` (`200→1, 100→2, 50→4, 40→5, 25→8, 20→10, 10→20, 5→40, 2→100, 1→200`; other → `ValueError`); `async configure(controller, driver, settings, *, apply: bool) -> SbgConfigReport` = read `INFO` (always), read current values of every profile item (always, for the UI), and when `apply`: for each item whose current value differs, `set` then `get` again and compare (`mismatch` → report + `receiver.error`); when at least one set succeeded and `settings.ins_apply_config`: `settings_action(SAVE_SETTINGS)` **once per process** (`driver.saved_this_run = True`) and expect the reconnect. Report: `SbgConfigReport(info, applied: list[str], unchanged: list[str], mismatched: list[str], errors: list[str], current: dict)` stored on `driver.config_report` and published as `receiver.capabilities`-style event `ins.config` for the UI.
- Default output profile for Port A, class 0: `STATUS DIV_200`, `UTC_TIME DIV_200`, `IMU_SHORT hz_to_mode(output_hz)`, `EKF_EULER hz_to_mode(output_hz)`, `EKF_NAV hz_to_mode(output_hz)`, `EKF_QUAT DISABLED`, `IMU_DATA DISABLED` (deprecated), `SHIP_MOTION DISABLED`, `GPS1_POS/GPS1_VEL/GPS1_HDT NEW_DATA`, `GPS1_SAT NEW_DATA`, `GPS1_RAW NEW_DATA if ins_raw_gnss else DISABLED`, `RTCM_RAW NEW_DATA` (echo of accepted corrections — the adapter counts these into `rtk.rtcm_rx_total`), `EVENT_A..E NEW_DATA`, `MAG DISABLED`; `OUTPUT_CLASS_ENABLE(port A, NMEA_0=False, NMEA_1=False, NMEA_GNSS=False)`. Baud is **never** changed by mtrtk (a wrong write strands the link); `doctor`/docs say to set Port A to 460800 in sbgCenter when `output_hz > 50` or raw GNSS is enabled.

- [ ] **Step 1: Write the failing tests**

`tests/unit/sbg/test_commands.py`:
```python
import asyncio
import struct

import pytest

from mtrtk.core.bus import Bus
from mtrtk.rover.drivers.sbg import commands as C
from mtrtk.rover.drivers.sbg.framer import SbgFramer, encode
from mtrtk.rover.drivers.sbg.ids import CLASS, CMD, LOG
from mtrtk.rover.drivers.ins_common import InsController

from tests.unit.test_ins_common import ScriptedSource  # reuse


def test_encoders_match_sbgecom_layouts() -> None:
    assert C.encode_output_conf(0, CLASS["LOG_ECOM_0"], LOG["EKF_NAV"], 20) == bytes([0, 8, 0]) + struct.pack("<H", 20)
    inst = C.GnssInstallation((0.5, 0.0, -1.2), True, (0.0, 0.0, 0.0), 1)
    assert C.encode_gnss_installation(inst) == struct.pack("<3f?3fB", 0.5, 0.0, -1.2, True, 0, 0, 0, 1)
    assert C.decode_gnss_installation(C.encode_gnss_installation(inst)) == inst
    al = C.ImuAlignment(0, 3, 0.0, 0.0, 0.01, (0.1, 0.2, 0.3))
    assert C.decode_imu_alignment(C.encode_imu_alignment(al)) == al
    aid = C.AidingAssignment(gps1_port=5, gps1_sync=5, dvl_port=0xFF, dvl_sync=0, rtcm_port=1, air_data_port=0xFF, odometer_pins=0)
    raw = C.encode_aiding_assignment(aid)
    assert len(raw) == 11 and raw[6] == 1 and C.decode_aiding_assignment(raw) == aid
    assert C.encode_init_parameters(23.7, 90.4, 12.0, 2026, 9, 19) == struct.pack("<dddHBB", 23.7, 90.4, 12.0, 2026, 9, 19)


def test_decode_info() -> None:
    raw = b"ELLIPSE-D-G4A3-B1".ljust(32, b"\0") + struct.pack("<IIHBBII", 12345, 3, 2025, 6, 1, 0x02000000, 0x03010000)
    info = C.decode_info(raw)
    assert info.product_code == "ELLIPSE-D-G4A3-B1" and info.serial_number == 12345 and info.firmware.startswith("3.1")


class Device:
    """Scripted sbgECom device: answers GET with a payload table and SET with ACK (configurable error)."""

    def __init__(self) -> None:
        self.src = ScriptedSource([])
        self.values: dict[int, bytes] = {}
        self.ack_error: dict[int, int] = {}
        self.sets: list[tuple[int, bytes]] = []
        self.src.write = self._write  # type: ignore[method-assign]

    async def _write(self, data: bytes) -> None:
        frame = SbgFramer().feed(data)[0]
        cmd, payload = frame.raw[2], frame.payload
        if cmd in self.ack_error or (payload and cmd not in (CMD["OUTPUT_CONF"],) or (cmd == CMD["OUTPUT_CONF"] and len(payload) == 5)):
            self.sets.append((cmd, payload))
            self.src.chunks.append(encode(0x10, 0, struct.pack("<BBH", cmd, 0x10, self.ack_error.get(cmd, 0))))
        else:
            self.src.chunks.append(encode(0x10, cmd, self.values.get(cmd, b"")))


async def run_with_device(dev: Device, body):
    bus = Bus()
    ctrl = InsController(bus, lambda: dev.src, SbgFramer, None, rx_timeout_s=5)
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    await asyncio.sleep(0.01)
    try:
        return await body(C.SbgCommands(ctrl))
    finally:
        stop.set()
        await dev.src.close()
        await task


async def test_get_and_set_roundtrip() -> None:
    dev = Device()
    dev.values[CMD["MOTION_PROFILE_ID"]] = struct.pack("<I", 2)

    async def body(cmds: C.SbgCommands):
        assert await cmds.get_motion_profile() == 2
        await cmds.set_motion_profile(7)
        assert dev.sets[-1] == (CMD["MOTION_PROFILE_ID"], struct.pack("<I", 7))

    await run_with_device(dev, body)


async def test_set_error_raises() -> None:
    dev = Device()
    dev.ack_error[CMD["MOTION_PROFILE_ID"]] = 5

    async def body(cmds: C.SbgCommands):
        with pytest.raises(C.SbgCommandError) as ei:
            await cmds.set_motion_profile(99)
        assert ei.value.code == 5

    await run_with_device(dev, body)


async def test_get_timeout_retries_then_raises() -> None:
    dev = Device()
    dev.src.write = ScriptedSource.write.__get__(dev.src)  # plain recorder: never answers

    async def body(cmds: C.SbgCommands):
        with pytest.raises(C.SbgCommandError):
            await cmds.get(CMD["INFO"], timeout_s=0.02, retries=2)
        assert len(dev.src.written) == 2

    await run_with_device(dev, body)
```
(The `Device` GET/SET discrimination follows the real protocol: a GET carries only a selector — empty for most, 3 bytes for `OUTPUT_CONF`, 1 byte for `UART_CONF` — so the fake decides by payload length per command; the implementer keeps that table in the fake, not in production code.)

`tests/unit/sbg/test_config.py`:
- `test_hz_to_mode`: `hz_to_mode(10) == 20`, `hz_to_mode(200) == 1`, `hz_to_mode(1) == 200`, `pytest.raises(ValueError): hz_to_mode(7)`.
- `test_profile_from_settings`: `Settings(rover_driver="sbg_ellipse", ins_port="/dev/x", ins_output_hz=10, ins_lever_arm_gnss1="0.5,0,-1.2", ins_motion_profile="uav", ins_raw_gnss=False)` → outputs contain `(0, LOG["EKF_NAV"], 20)`, `(0, LOG["GPS1_RAW"], 0)`, `(0, LOG["RTCM_RAW"], 10001)`; `gnss_installation.lever_arm_primary == (0.5, 0.0, -1.2)` with mode `SINGLE`; `motion_profile == 7`; `aiding.rtcm_port == 0` (same port) and `== 1` when `ins_rtcm_port` is set.
- `test_configure_read_only_reports_current`: `Device` pre-loaded with INFO + current values; `configure(apply=False)` → report `applied == []`, `current["motion_profile"] == 2`, no sets issued, `driver.info.product_code` set.
- `test_configure_applies_only_differences_and_saves_once`: current motion profile 2, profile wants 7; output conf for EKF_NAV currently 0 wants 20; everything else equal → exactly two sets + one `SETTINGS_ACTION(1)`; `report.applied == ["output:EKF_NAV", "motion_profile"]`; calling `configure` again with the same driver → no `SETTINGS_ACTION` (already saved this run).
- `test_configure_readback_mismatch_is_reported`: device accepts the set but keeps returning the old value → `report.mismatched == ["motion_profile"]`, `receiver.error` published, no save.

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement `commands.py`**

```python
class SbgCommandError(RuntimeError):
    def __init__(self, cmd: int, code: int | None, detail: str = "") -> None:
        super().__init__(f"sbgECom cmd {CMD_NAME.get(cmd, cmd)}: {'timeout' if code is None else f'error {code}'} {detail}".strip())
        self.cmd, self.code = cmd, code


class SbgCommands:
    def __init__(self, controller: InsController) -> None:
        self.ctrl = controller

    async def get(self, cmd: int, selector: bytes = b"", *, timeout_s: float = 0.5, retries: int = 3) -> bytes:
        def match(f: Frame) -> bool:
            return f.proto is Proto.SBG and f.raw[3] == CLASS["CMD_0"] and (f.raw[2] == cmd or (f.raw[2] == 0 and f.payload[:1] == bytes([cmd])))

        for attempt in range(retries):
            try:
                reply = await self.ctrl.request(match, encode(CLASS["CMD_0"], cmd, selector), timeout_s)
            except TimeoutError:
                continue
            if reply.raw[2] == 0:  # ACK with error instead of data
                raise SbgCommandError(cmd, struct.unpack_from("<H", reply.payload, 2)[0])
            return reply.payload
        raise SbgCommandError(cmd, None, f"after {retries} attempts")

    async def set(self, cmd: int, payload: bytes, *, timeout_s: float = 0.5, retries: int = 3) -> None:
        def match(f: Frame) -> bool:
            return f.proto is Proto.SBG and f.raw[3] == CLASS["CMD_0"] and f.raw[2] == 0 and f.payload[:1] == bytes([cmd])

        for _ in range(retries):
            try:
                ack = await self.ctrl.request(match, encode(CLASS["CMD_0"], cmd, payload), timeout_s)
            except TimeoutError:
                continue
            code = struct.unpack_from("<H", ack.payload, 2)[0]
            if code:
                raise SbgCommandError(cmd, code)
            return
        raise SbgCommandError(cmd, None, f"no ACK after {retries} attempts")
```
Dataclasses `GnssInstallation(lever_arm_primary, primary_precise, lever_arm_secondary, secondary_mode)`, `ImuAlignment(axis_x, axis_y, mis_roll, mis_pitch, mis_yaw, lever_arm)`, `AidingAssignment(...)`, `UartConf(interface, baud, mode)`, `SbgInfo(product_code, serial_number, calibration_rev, calibration_date, hardware_rev, firmware, firmware_raw)`; encoders/decoders with the exact `struct` formats above (`"<3f?3fB"`, `"<BBfff3f"`, `"<BBIBBBBB"`, `"<dddHBB"`, `"<32sIIHBBII"`), and the typed wrappers.

- [ ] **Step 4: Implement `config.py`**

`configure()` outline:
```python
async def configure(controller: InsController, driver: SbgDriver, settings: Settings, *, apply: bool) -> SbgConfigReport:
    cmds = SbgCommands(controller)
    profile = sbg_profile(settings)
    report = SbgConfigReport()
    report.info = driver.info = await cmds.get_info()
    items: list[_Item] = [
        *[_Item(f"output:{LOG_NAME[mid]}", lambda c=cls, m=mid: cmds.get_output_conf(0, c, m), lambda v, c=cls, m=mid: cmds.set_output_conf(0, c, m, v), mode) for cls, mid, mode in profile.outputs],
        _Item("motion_profile", cmds.get_motion_profile, cmds.set_motion_profile, profile.motion_profile),
        _Item("gnss_installation", cmds.get_gnss_installation, cmds.set_gnss_installation, profile.gnss_installation),
        _Item("imu_alignment", cmds.get_imu_alignment, cmds.set_imu_alignment, profile.imu_alignment),
        _Item("aiding_assignment", cmds.get_aiding_assignment, cmds.set_aiding_assignment, profile.aiding_target),  # target built from the current value with rtcm_port replaced
    ]
    for item in items:
        try:
            current = await item.get()
        except SbgCommandError as exc:
            report.errors.append(f"{item.name}: {exc}")
            continue
        report.current[item.name] = current
        want = item.want(current) if callable(item.want) else item.want
        if want is None or want == current:
            report.unchanged.append(item.name)
            continue
        if not apply:
            report.pending.append(item.name)
            continue
        try:
            await item.set(want)
            after = await item.get()
        except SbgCommandError as exc:
            report.errors.append(f"{item.name}: {exc}")
            continue
        (report.applied if after == want else report.mismatched).append(item.name)
        report.current[item.name] = after
    for cls in profile.disable_classes:  # NMEA classes off on port A; no read-back available, ACK is the evidence
        if apply:
            await cmds.set_output_class_enable(0, cls, False)
    if apply and report.applied and not report.mismatched and not driver.saved_this_run:
        driver.saved_this_run = True
        await cmds.settings_action(SAVE_SETTINGS)  # device reboots; InsController reconnects and calls configure again -> everything 'unchanged'
    if report.mismatched or report.errors:
        controller.bus.publish("receiver.error", "INS configuration: " + "; ".join(report.mismatched + report.errors))
    controller.bus.publish("ins.config", report)
    driver.config_report = report
    return report
```
`aiding_target(current)`: copy of `current` with `rtcm_port = 1 if settings.ins_rtcm_port else 0` and `gps1_port` left untouched. `_Item.want` may be a callable of the current value (aiding) or a constant. Floats compare with `math.isclose(abs_tol=1e-6)` per component (implement `__eq__`-free helper `_same(a, b)`).

- [ ] **Step 5: Run tests, lint, commit**

```bash
uv run pytest tests/unit/sbg -q
git add src/mtrtk/rover/drivers/sbg/commands.py src/mtrtk/rover/drivers/sbg/config.py tests/unit/sbg
git commit -m "feat(sbg): sbgECom commands with ACK correlation and declarative Ellipse profile apply/verify/save

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: VectorNav VN-200 — framer (binary + ASCII), parsers, registers, adapter, driver, configuration

**Files:**
- Create: `src/mtrtk/rover/drivers/vectornav/{__init__,checksum,fields,framer,parse,registers,adapter,driver,config}.py`, `tests/unit/vectornav/__init__.py`, `tests/unit/vectornav/test_{checksum,framer,parse,registers,adapter,config}.py`, `tests/fixtures/ins/vn_frames.hex`

**Protocol facts (vnproglib 1.2 `packet.cpp`/`types.h`, VN-200 User Manual):**
- **Binary**: `FA | groups u8 | for each set group bit (0..6, in order): field u16le [+ extension u16le when bit 15 set (GPS groups only)] | payload (groups in order, fields in bit order) | CRC16 u16 big-endian`. CRC-16/CCITT (XMODEM: poly `0x1021`, init `0`) over everything after the sync byte; running the CRC over the frame including the CRC bytes yields `0` (`"123456789"` → `0x31C3`). Payload values are little-endian. Groups: `COMMON 0x01, TIME 0x02, IMU 0x04, GPS 0x08, ATTITUDE 0x10, INS 0x20, GPS2 0x40`.
- Field sizes per group (bit index 0..14; `0` = unknown/invalid, `ext` = extension bit 0):

| group | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | ext0 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Common | 8 | 8 | 8 | 12 | 16 | 12 | 24 | 12 | 12 | 24 | 20 | 28 | 2 | 4 | 8 | – |
| Time | 8 | 8 | 8 | 2 | 8 | 8 | 8 | 4 | 4 | 1 | 0 | 0 | 0 | 0 | 0 | – |
| IMU | 2 | 12 | 12 | 12 | 4 | 4 | 16 | 12 | 12 | 12 | 12 | 2 | 40 | 0 | 0 | – |
| GPS / GPS2 | 8 | 8 | 2 | 1 | 1 | 24 | 24 | 12 | 12 | 12 | 4 | 4 | 2 | 28 | **2+8·n** | **12+28·m** |
| Attitude | 2 | 12 | 16 | 36 | 12 | 12 | 12 | 12 | 12 | 12 | 28 | 24 | 12 | 0 | 0 | – |
| INS | 2 | 24 | 24 | 12 | 12 | 12 | 12 | 12 | 12 | 4 | 4 | 68 | 64 | 0 | 0 | – |

  Field names — Common: `TimeStartup u64 ns, TimeGps u64 ns, TimeSyncIn u64 ns, YawPitchRoll 3f °, Quaternion 4f, AngularRate 3f rad/s, Position 3d (lat °, lon °, alt m), Velocity 3f NED m/s, Accel 3f, Imu 6f, MagPres 5f, DeltaTheta 7f (dt, dθ3, dv3), InsStatus u16, SyncInCnt u32, TimeGpsPps u64`. Time: `TimeStartup, TimeGps, GpsTow u64 ns, GpsWeek u16, TimeSyncIn, TimeGpsPps, TimeUTC (year i8 since 2000, month u8, day u8, hour u8, min u8, sec u8, ms u16), SyncInCnt u32, SyncOutCnt u32, TimeStatus u8 (bit0 timeOk, bit1 dateOk, bit2 utcTimeValid)`. IMU: `ImuStatus u16, UncompMag 3f, UncompAccel 3f, UncompGyro 3f, Temp f °C, Pres f kPa, DeltaTheta 4f, DeltaVel 3f, Mag 3f, Accel 3f m/s², AngularRate 3f rad/s, SensSat u16`. GPS: `UTC 8, Tow u64 ns, Week u16, NumSats u8, Fix u8, PosLla 3d, PosEcef 3d, VelNed 3f, VelEcef 3f, PosU 3f (N,E,D 1σ m), VelU f, TimeU f s, TimeInfo (status u8, leapSecs i8), DOP 7f (g,p,t,v,h,n,e), SatInfo (numSats u8, resv u8, then per sat: sys i8, svId u8, flags u8, cno u8, qi u8, el i8, az i16) # VERIFY, RawMeas (ext bit 0: tow f64, week u16, numMeas u8, resv u8, then per meas: sys u8, svId u8, freq u8, chan u8, slot i8, cno u8, flags u16, pr f64, cp f64, dp f32) # VERIFY`. Attitude: `VpeStatus u16, YawPitchRoll 3f °, Quaternion 4f, DCM 9f, MagNed 3f, AccelNed 3f, LinearAccelBody 3f, LinearAccelNed 3f, YprU 3f ° (1σ)`. INS: `InsStatus u16, PosLla 3d, PosEcef 3d, VelBody 3f, VelNed 3f, VelEcef 3f, MagEcef 3f, AccelEcef 3f, LinearAccelEcef 3f, PosU f (m, 1σ), VelU f`.
  GPS `Fix`: `0 no fix, 1 time only, 2 2D, 3 3D` (vnproglib enum), `4 SBAS`, `7 RTK float, 8 RTK fixed` on RTK-capable VectorNav units (`# VERIFY` — the VN-200 is not documented as RTK-capable). `InsStatus`: bits 0–1 mode (`0 not tracking, 1 aligning / insufficient dynamic motion, 2 tracking`), bit 2 GpsFix, bit 3 time error, bit 4 IMU error, bit 5 mag/pres error, bit 6 GPS error, bit 8 GpsHeadingIns, bit 9 GpsCompass. SatInfo `sys` uses the u-blox gnssId order (`0 GPS, 1 SBAS, 2 Galileo, 3 BeiDou, 5 QZSS, 6 GLONASS`, `# VERIFY`); flags `bit0 healthy, bit1 almanac, bit2 ephemeris, bit3 diff corr, bit4 used for nav, bit5 az/el valid, bit6 used for RTK` (`# VERIFY`).
- **ASCII**: `$VN<CMD>[,fields]*<CS>\r\n`; checksum = 8-bit XOR of bytes between `$` and `*` (2 hex digits) or CRC16-CCITT of the same span (4 hex digits) when register 30 selects CRC; either is accepted on receive. Commands: `$VNRRG,<reg>` (read; reply `$VNRRG,<reg>,<fields>`), `$VNWRG,<reg>,<fields>` (write; reply echoes the written register), `$VNWNV` (write settings to flash), `$VNRST` (reset), `$VNRFS` (restore factory), errors `$VNERR,<code hex>`: `1 HardFault, 2 SerialBufferOverflow, 3 InvalidChecksum, 4 InvalidCommand, 5 NotEnoughParameters, 6 TooManyParameters, 7 InvalidParameter, 8 InvalidRegister, 9 UnauthorizedAccess, 10 WatchdogReset, 11 OutputBufferOverflow, 12 InsufficientBaudRate, 255 ErrorBufferOverflow`.
- Registers used: `1 Model (string), 2 HW revision, 3 Serial number, 4 Firmware version (e.g. 2.0.0.0)`, `5 Serial baud (baud[,port])`, `6 Async data output type (0 = off ASCII)`, `7 Async output frequency`, `26 Reference frame rotation (9 floats, row-major C)`, `30 Communication protocol control (serialCount, serialStatus, spiCount, spiStatus, serialChecksum, spiChecksum, errorMode)`, `35 VPE basic control (enable, headingMode, filteringMode, tuningMode)`, `55 GNSS configuration (mode, ppsSource, rate, 0, antPow)`, `57 GNSS antenna offset (x,y,z m, body frame)`, `58 GNSS solution LLA`, `63 INS solution LLA`, `67 INS basic configuration (VN-200: scenario, ahrsAiding, 0, 0)` `# VERIFY scenario values`, `75/76/77 Binary output 1/2/3`: `$VNWRG,75,<asyncMode>,<rateDivisor>,<groups hex>,<field hex for each present group in order>` (extension: the GPS field is written with bit 15 set and is followed by the extension u16 hex — `# VERIFY` against the manual's register 75 description, vnproglib 1.2 does not encode it). Async mode `0 none, 1 serial 1, 2 serial 2, 3 both`; `rateDivisor` divides the 800 Hz IMU rate (`800 // hz`, hz ∈ {1,2,4,5,8,10,16,20,25,32,40,50,80,100,160,200,400,800}; others → nearest lower and reported).
- RTCM: vnproglib's `getting_started_with_rtcm` example forwards RTCM bytes with `send(buffer, waitForReply=false)` — the unit does not acknowledge them. Whether the **VN-200** uses them is undocumented → `INS_VN_RTCM` (default `0`) opts in; `accepts_rtcm` mirrors it; the RTK page shows "unverified on this unit" until `Fix ∈ {7, 8}` is observed.

**Interfaces:**
- `checksum.py`: `xor8(data) -> int`, `crc16_xmodem(data) -> int`, `verify_ascii(line: bytes) -> bool` (auto 2- or 4-hex), `finalize_ascii(body: str, *, crc: bool = False) -> bytes` (appends `*XX\r\n`).
- `fields.py`: `GROUP_BITS`, `FIELD_SIZES` (the table), `FIELD_NAMES` per group, `binary_length(buf: bytes) -> int | None` (returns the total frame length once determinable from the bytes present — reads `numSats`/`numMeas` at their computed offsets; `None` = need more bytes; raises `ValueError` for an invalid header: unknown group bit 7 set, field bit with size 0, count > 64/200), `field_layout(buf) -> list[tuple[int group, int bit, int offset, int size]]`.
- `framer.py`: `VnFramer()` with `.feed(data) -> list[Frame]`, `.stats: FramerStats(frames, ascii_frames, crc_failed, resyncs, bytes_skipped, invalid_headers)`; emits `Frame(Proto.VN, raw)` for both kinds; registers namer/parser. Resync: on invalid header or CRC failure drop one byte; ASCII lines longer than 512 bytes without `\r\n` are discarded.
- `parse.py`: `parse(frame) -> VnBinary | VnAscii | None`; `VnBinary(groups: dict[str, dict[str, Any]])` e.g. `parsed.groups["gps"]["fix"]`, `["ins"]["pos_lla"]`, `["attitude"]["ypr"]`, `["time"]["utc"] -> datetime`, `["gps"]["sat_info"] -> list[VnSat]`, `["gps"]["raw_meas"] -> bytes` (kept raw for capture) ; `VnAscii(cmd: str, register: int | None, fields: list[str], error: int | None)`.
- `registers.py`: `VnRegisters(controller)`: `await read(reg: int, timeout_s=1.0, retries=3) -> list[str]`, `await write(reg: int, *fields, timeout_s=1.0, retries=3) -> None` (matches echo `$VNWRG,<reg>` or `$VNERR` → `VnError(code)`), `await command(name: str)` for `WNV`/`RST`/`RFS` (reply `$VN<name>` or error), typed helpers `model()`, `firmware()`, `serial()`, `set_async_output_type(0)`, `write_binary_output(n, async_mode, divisor, fields: dict[str, int], gps_ext: int | None = None)`, `read_binary_output(n) -> BinaryOutputConf`, `set_antenna_offset(x,y,z)`, `read_antenna_offset()`, `set_reference_frame_rotation(matrix9)`, `set_vpe_basic_control(...)`, `set_ins_basic_config(scenario, ahrs_aiding)`, `write_settings()`.
- `adapter.py`: `VnStateAdapter(bus, *, nav_hz_cap, raw_capture: RawCapture | None)`: INS group → `position` (`pos_lla`, `height_m = alt` (VN INS altitude is HAE; `hmsl_m = None`), `accuracy.h_acc_m = pos_u`, `velocity` from `vel_ned`, `accuracy.s_acc_mps = vel_u`), `fix.fix_type = 3 when ins mode == 2 and GpsFix else (2 if GpsFix else 0)`, epoch on every INS-bearing frame (`end_epoch`); GPS group → `fix.num_sv`, `fix.carr_soln` from `Fix ∈ {7,8}`, `dops`, `time.gps_week/tow`, `time.leap_s`, `state.ins.gnss_fix = fix`, `sats` from SatInfo, `raw_capture.write(b"VNRM" + len u16 + raw_meas)`; Attitude group → `attitude = Attitude(roll, pitch, heading = yaw mod 360, acc from YprU, source="vn-ins")`; Time group → `time.utc` from TimeUTC when `TimeStatus.utcTimeValid`, `raw_capture.note_utc`, SyncInCnt increment + TimeSyncIn → `TimeMark(channel=0, rising_utc = frame_utc − TimeSyncIn ns)`; IMU group → `state.imu` (≤10 Hz publish); `state.ins.status` from InsStatus bits.
- `driver.py`: `VnDriver(controller, adapter, *, rtcm_enabled: bool)`: `name="vectornav"`, capabilities `accepts_rtcm=rtcm_enabled`, `raw_gnss_log=adapter.raw_meas_seen`, `attitude=True`, `imu=True`, `sats=adapter.sats_seen`, `spectrum=False`; `inject_rtcm` → `controller.write(data)` when enabled else counts `dropped_bytes`.
- `config.py`: `vn_profile(settings) -> VnProfile(divisor, fields: dict[str,int], gps_ext: int | None, antenna_offset, vpe, ins_basic, ref_rotation)`; `async configure(controller, driver, settings, *, apply)`: read model/firmware/serial (always, → `driver.info`), read binary output 1 and antenna offset (always); when `apply`: `set_async_output_type(0)` (ASCII off), `write_binary_output(1, async_mode=1, divisor, fields, gps_ext)` — first **with** `SatInfo` and `RawMeas`; on `VnError(7)` retry without `RawMeas`; on a second `VnError(7)` retry without `SatInfo` (capabilities follow what stuck); antenna offset when configured; INS basic config / VPE / rotation only when the corresponding `INS_VN_*` settings are set; `write_settings()` once per run when anything changed. Report `VnConfigReport` published as `ins.config`.
- Default binary output 1 fields: Time `0x02DE` (TimeGps, GpsTow, GpsWeek, TimeSyncIn, TimeUTC, SyncInCnt, TimeStatus), IMU `0x0611` (ImuStatus, Temp, Accel, AngularRate), GPS `0x7ABA` (Tow, NumSats, Fix, PosLla, VelNed, PosU, VelU, TimeU, TimeInfo, DOP, SatInfo) with `gps_ext = 0x0001` (RawMeas) when `INS_RAW_GNSS=1`, Attitude `0x0103` (VpeStatus, YawPitchRoll, YprU), INS `0x0613` (InsStatus, PosLla, VelNed, PosU, VelU).
- Settings additions: `ins_vn_rtcm: bool = False`, `ins_vn_scenario: int | None`, `ins_vn_ahrs_aiding: bool | None`, `ins_vn_ref_rotation: str | None` (9 comma floats), `ins_vn_vpe: str | None` (`enable,headingMode,filteringMode,tuningMode`).

- [ ] **Step 1: Write the failing tests**

`tests/unit/vectornav/test_checksum.py`:
```python
from mtrtk.rover.drivers.vectornav.checksum import crc16_xmodem, finalize_ascii, verify_ascii, xor8


def test_check_values() -> None:
    assert crc16_xmodem(b"123456789") == 0x31C3
    assert xor8(b"VNRRG,01,VN-200") == 0x59


def test_ascii_finalize_and_verify_both_modes() -> None:
    line = finalize_ascii("VNRRG,01")
    assert line == b"$VNRRG,01*" + f"{xor8(b'VNRRG,01'):02X}".encode() + b"\r\n" and verify_ascii(line)
    crc_line = finalize_ascii("VNRRG,01", crc=True)
    assert len(crc_line.split(b"*")[1].strip()) == 4 and verify_ascii(crc_line)
    assert not verify_ascii(b"$VNRRG,01*00\r\n")


def test_binary_crc_residue() -> None:
    body = b"\x01\x02\x03"
    frame = body + crc16_xmodem(body).to_bytes(2, "big")
    assert crc16_xmodem(frame) == 0
```

`tests/unit/vectornav/helpers.py` — `build_binary(groups: dict[int, tuple[int, bytes, int | None]]) -> bytes` (group bit → (field mask, payload, ext mask)) that assembles header + payload + CRC; `time_group_payload(...)`, `gps_payload(...)`, `ins_payload(...)`, `att_payload(...)`, `sat_info(sats)`, `raw_meas(n)` helpers producing exact byte layouts per the table.

`tests/unit/vectornav/test_framer.py`:
- `test_binary_frame_roundtrip`: Time(0x02DE)+INS(0x0613) frame → one `Frame`, `identity == "VN-BIN"`, `fields.binary_length(raw) == len(raw)`.
- `test_variable_satinfo_and_rawmeas_length`: GPS `0x7ABA | 0x8000` + ext `0x0001` with 3 sats and 2 meas → `binary_length` == fixed + 2+24 + 12+56; feeding the frame in 3 chunks yields one frame only after the last chunk.
- `test_invalid_header_resyncs`: `FA 80 ...` (bit 7 group) and `FA 02 00 04 ...` (Time bit 10, size 0) → `stats.invalid_headers == 2`, no frames, bytes skipped.
- `test_crc_failure_counted`: flip a payload byte → `crc_failed == 1`; the following good frame still parses.
- `test_ascii_frames_and_mixed_stream`: `b"$VNRRG,01,VN-200T-CR*5F\r\n"` (compute the real checksum in the test with `finalize_ascii`) interleaved with binary → identities `["VN-ASCII", "VN-BIN"]`, bad checksum ASCII counted in `crc_failed`, a `$VNERR,07*XX` line parses to `error == 7`.

`tests/unit/vectornav/test_parse.py`:
- `test_time_group`: TimeUTC `(26, 9, 19, 10, 30, 15, 250)` → `groups["time"]["utc"] == datetime(2026,9,19,10,30,15,250000, UTC)`; `gps_week`, `gps_tow_s == tow_ns/1e9`; `time_status` flags.
- `test_gps_group_with_satinfo_and_dop`: fix 3, numSats 12, PosLla, PosU, DOP → dict values; `sat_info` 2 entries with `sys`, `sv_id`, `cno`, `used`, `el`, `az`.
- `test_ins_and_attitude_groups`: `ins_status` mode 2 + GpsFix; `pos_lla`, `pos_u`, `vel_ned`; attitude `ypr == (91.0, -1.0, 0.5)` and `ypr_u`.
- `test_raw_meas_kept_as_bytes`: `groups["gps"]["raw_meas"]` equals the encoded bytes and `["raw_meas_count"] == 2`.
- `test_ascii_parse`: `$VNWRG,75,1,80,3A,...` echo → `cmd == "WRG"`, `register == 75`; `$VNERR,08` → `error == 8`.

`tests/unit/vectornav/test_registers.py` (scripted device like Task 4's, answering ASCII):
- `read(1)` → `["VN-200T-CR"]`; `write(6, 0)` echoes → returns; `write(75, ...)` with device scripted to answer `$VNERR,07` → `VnError(7)`; timeout retry count; `write_binary_output(1, 1, 80, {"time": 0x02DE, "gps": 0x7ABA}, gps_ext=1)` sends exactly `$VNWRG,75,1,80,A,2DE,FABA,1*XX` (`groups = 0x0A`, GPS field with bit 15 set = `0xFABA`, then the extension) — assert the body string.

`tests/unit/vectornav/test_adapter.py`:
- `test_ins_frame_drives_state_and_epoch`: INS+Attitude+Time frame → position, `fix.fix_type == 3`, attitude heading 91.0 with `source == "vn-ins"`, one `state.epoch`, `time.utc` set.
- `test_gps_group_rtk_fix_maps_to_carr_soln`: Fix 8 → `fix.carr_soln == 2`; Fix 3 → 0; `fix.num_sv == 12`; `dops.h == hDop`.
- `test_sync_in_event_becomes_time_mark`: two frames, SyncInCnt 4 → 5, TimeSyncIn 2_000_000 ns, frame UTC 10:00:01.000 → `TimeMark.rising_utc == 10:00:00.998`, channel 0.
- `test_raw_meas_captured_with_record_header`: `RawCapture` fake with `.written` → `b"VNRM" + len + bytes`, `adapter.raw_meas_seen is True`; `note_utc` called from the Time group.
- `test_imu_publish_decimated`: 80 IMU-only frames in 0.1 s → `state.imu` published ≤ 2.

`tests/unit/vectornav/test_config.py`:
- `test_divisor`: `divisor_for_hz(10) == 80`, `(200) == 4`, `(7) == 100` with `report.note`.
- `test_profile_fields_default_and_raw_off`: with `ins_raw_gnss=False` → `gps_ext is None` and GPS field has no bit 15.
- `test_configure_probe_fallbacks`: device rejects the first `WRG,75` (contains ext) with `VNERR,07`, accepts the second → capabilities `raw_gnss_log False`, `sats True`; rejects two → `sats False`; `write_settings` called once; `driver.info.model == "VN-200T-CR"`.
- `test_configure_read_only`: `apply=False` → no `WRG`, report `pending` lists `binary_output_1`.

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement**

`fields.py` core:
```python
FIELD_SIZES: dict[int, list[int]] = {
    0: [8, 8, 8, 12, 16, 12, 24, 12, 12, 24, 20, 28, 2, 4, 8],
    1: [8, 8, 8, 2, 8, 8, 8, 4, 4, 1, 0, 0, 0, 0, 0],
    2: [2, 12, 12, 12, 4, 4, 16, 12, 12, 12, 12, 2, 40, 0, 0],
    3: [8, 8, 2, 1, 1, 24, 24, 12, 12, 12, 4, 4, 2, 28, 2],   # bit 14 SatInfo header; + 8 * numSats
    4: [2, 12, 16, 36, 12, 12, 12, 12, 12, 12, 28, 24, 12, 0, 0],
    5: [2, 24, 24, 12, 12, 12, 12, 12, 12, 4, 4, 68, 64, 0, 0],
    6: [8, 8, 2, 1, 1, 24, 24, 12, 12, 12, 4, 4, 2, 28, 2],
}
RAWMEAS_HEADER, RAWMEAS_RECORD, SATINFO_RECORD = 12, 28, 8
MAX_SATS, MAX_MEAS = 64, 200


def binary_length(buf: bytes) -> int | None:
    """Total frame length (incl. sync + CRC) or None if more bytes are needed. ValueError on an invalid header."""
    if len(buf) < 2:
        return None
    groups = buf[1]
    if groups == 0 or groups & 0x80:
        raise ValueError("invalid groups byte")
    pos = 2
    layout: list[tuple[int, int, int | None]] = []  # (group, field mask, ext mask)
    for g in range(7):
        if not groups & (1 << g):
            continue
        if len(buf) < pos + 2:
            return None
        mask = int.from_bytes(buf[pos : pos + 2], "little"); pos += 2
        ext = None
        if mask & 0x8000:
            if g not in (3, 6):
                raise ValueError("extension bit outside GPS groups")
            if len(buf) < pos + 2:
                return None
            ext = int.from_bytes(buf[pos : pos + 2], "little"); pos += 2
            if ext & ~0x0001:
                raise ValueError("unknown GPS extension field")
        layout.append((g, mask & 0x7FFF, ext))
    payload_start = pos
    for g, mask, ext in layout:
        sizes = FIELD_SIZES[g]
        for bit in range(15):
            if not mask & (1 << bit):
                continue
            size = sizes[bit]
            if size == 0:
                raise ValueError(f"unknown field bit {bit} in group {g}")
            if g in (3, 6) and bit == 14:  # SatInfo: numSats at pos
                if len(buf) < pos + 1:
                    return None
                n = buf[pos]
                if n > MAX_SATS:
                    raise ValueError("implausible numSats")
                size += SATINFO_RECORD * n
            pos += size
        if ext:
            if len(buf) < pos + 11:
                return None
            m = buf[pos + 10]
            if m > MAX_MEAS:
                raise ValueError("implausible numMeas")
            pos += RAWMEAS_HEADER + RAWMEAS_RECORD * m
    return pos + 2  # CRC
```
`framer.py` uses it: at `FA`, call `binary_length`; `None` → wait (unless buffer > 8 KB → skip byte); `ValueError` → `invalid_headers += 1`, skip byte; when enough bytes, verify `crc16_xmodem(buf[1:total]) == 0` else `crc_failed`, skip byte. At `$`, look for `\r\n`; verify checksum; emit. Anything else → skip byte (`bytes_skipped`).

`parse.py`: walk the same layout, decode with `struct` per field name table (`FIELD_FORMATS[g][bit] = (name, fmt)`; `TimeUTC` custom; `SatInfo`/`RawMeas` custom). Angles stay degrees (VN already outputs degrees). `groups["time"]["utc"] = datetime(2000 + year, month, day, hour, minute, sec, ms * 1000, tzinfo=UTC)`.

`registers.py` request matcher: `f.identity == "VN-ASCII" and (parsed.cmd == cmd_name and parsed.register == reg or parsed.error is not None)`; error → `VnError(code)`. `write_binary_output` builds `groups` from the present keys in group order and writes each field as uppercase hex without padding (vnproglib `%X`).

`adapter.py`/`driver.py`/`config.py` per *Interfaces* (mirror the SBG structure; `configure` uses `VnRegisters`).

- [ ] **Step 4: Golden fixture**

`tests/fixtures/ins/vn_frames.hex`: binary frames (Time+IMU+GPS(+SatInfo)+Attitude+INS, and one with RawMeas) plus ASCII lines (`$VNRRG,01,…`, `$VNWRG,75,…`, `$VNERR,07`); `test_framer.py::test_golden_fixture` feeds all and asserts identities and non-`None` parses.

- [ ] **Step 5: Run tests, lint, commit**

```bash
uv run pytest tests/unit/vectornav -q
git add src/mtrtk/rover/drivers/vectornav tests/unit/vectornav tests/fixtures/ins/vn_frames.hex src/mtrtk/config.py .env.example
git commit -m "feat(vectornav): VN binary/ASCII framer with variable-length fields, parsers, register I/O, VN-200 adapter/driver and configuration

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Integration — daemon driver selection, API, UI, CLI, doctor

**Files:**
- Modify: `src/mtrtk/daemon.py`, `src/mtrtk/cli.py`, `src/mtrtk/doctor.py`, `src/mtrtk/web/api/receiver.py`, `src/mtrtk/web/api/status.py`, `src/mtrtk/web/api/rover.py`, `src/mtrtk/web/ws.py`, `src/mtrtk/alerts.py`, `web/src/types.ts`, `web/src/pages/Receiver.tsx`, `web/src/pages/Rtk.tsx`, `web/src/pages/Dashboard.tsx`
- Create: `src/mtrtk/rover/drivers/factory.py`, `web/src/components/InsPanel.tsx`, `tests/unit/test_daemon_ins.py`, `tests/unit/test_web_ins.py`, `web/src/components/InsPanel.test.tsx`

**Interfaces:**
- `drivers/factory.py`: `build_ins(settings, bus, db, raw_writer) -> InsBundle(controller: InsController, adapter: StateAdapter, driver: RoverDriver, extra_tasks: list[Callable[[], Awaitable[None]]])` for `sbg_ellipse` (`SerialSource(ins_port, ins_baud)`, `SbgFramer`, `SbgStateAdapter(raw_writer=raw_writer, raw_capture=RawCapture(..., "sbgraw") if ins_raw_gnss)`, optional `rtcm_source = SerialSource(ins_rtcm_port, ins_baud)` opened by an extra task, `configure = partial(sbg_config.configure, driver=…, settings=…, apply=settings.ins_apply_config)`) and `vectornav` (`VnFramer`, `VnStateAdapter(raw_capture=RawCapture(..., "vnraw"))`, `VnDriver(rtcm_enabled=settings.ins_vn_rtcm)`, `configure = vn_config.configure`).
- `Daemon`: when `settings.role is Role.ROVER and settings.rover_driver != "ublox"`: skip `ReceiverController`/UBX profile; `self.ins = build_ins(...)`; the state loop subscribes to `raw.sbg`/`raw.vn` (instead of `raw.ubx`/`raw.rtcm`) and calls `adapter.handle(frame)`; `self.store` is replaced by a thin `StoreFacade` exposing `.state` (the adapter's) so `AppContext`, WS, sampler, NMEA, JSON, points, alerts keep reading `daemon.store.state`; `_run_rover` uses `self.ins.driver` and starts the NTRIP client only when `driver.capabilities.accepts_rtcm`; raw logging stays on (`raw.ubx` still flows for SBG UBX re-framing; for VN nothing arrives and the writer simply idles); `StatusPrinter.format_line` adds `ins <mode> att <heading>` when `state.attitude`.
- API: `GET /api/status` gains `driver: {name, capabilities}`; `GET /api/receiver` gains `ins: {vendor, info, config_report, status}` and returns `capabilities.spectrum=false` so the UI hides RF/spectrum; `POST /api/receiver/reset` → `SbgCommands.settings_action(REBOOT_ONLY)` / `VnRegisters.command("RST")` (409 for `factory` on INS: too dangerous remotely); `POST /api/receiver/profile` (apply) → `configure(apply=True)` and returns the report (requires `ins_apply_config` or explicit `{"force": true}` body — the UI confirm dialog sends it); `POST /api/receiver/poll` → re-read info/config (`apply=False`). WS: `state.ins`, `state.imu` topics added to the section list; `ins.config` event forwarded as `events`.
- Alerts: `ins_not_aligned` (warning when EKF/INS mode < tracking for > 60 s after connect), `ins_gnss_lost` (serious when `GPS1_POS`/GPS `Fix` = no solution for > 10 s while INS still outputs), `ins_config_mismatch` (serious, from `ins.config` with `mismatched`), `imu_error` (critical from VN InsStatus IMU error / SBG general status IMU power not OK).
- UI: `InsPanel` (vendor, model/firmware/serial, INS mode, aiding flags, heading/roll/pitch with 1σ, IMU rates/temperature, lever arms as configured vs. read back, config report with per-item state and an "Apply INS configuration" button behind `ConfirmDialog`); `Receiver.tsx` shows `InsPanel` and hides Hardware/RF/Spectrum/Ports cards when `capabilities.spectrum` is false and `ins` present; `Rtk.tsx` shows a notice card "This receiver does not accept RTCM corrections (VN-200)" when `!capabilities.accepts_rtcm`, or "RTCM path unverified on this unit" when the driver reports `rtcm_unverified`; `Dashboard.tsx` attitude card already exists (Phase 6) — add IMU mini-card when `state.imu`.
- CLI: `mtrtk ins info` (opens the port, runs `configure(apply=False)`, prints info + current config as a table, exits), `mtrtk ins config [--apply] [--dry-run]` (`--dry-run` prints the commands/registers that would be written), `mtrtk ins monitor` (prints one line per epoch: mode, lat/lon, heading, fix). `mtrtk doctor`: check `ins_port` exists/permissions when `rover_driver != ublox`; WARN when `ins_output_hz > 50 or ins_raw_gnss` and `ins_baud < 460800`; WARN "RTCM on same port unverified" for SBG without `ins_rtcm_port` and `ntrip_url` set.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_daemon_ins.py`:
- `test_daemon_builds_ins_bundle_for_sbg`: `Settings(role=rover, rover_driver="sbg_ellipse", ins_port="/dev/null", ntrip_url=None, ...)` with a `source_factory` override returning a `ScriptedSource` streaming `tests/fixtures/ins/sbg_frames.hex` frames → after `run()` completes (source EOF), `daemon.store.state.position.lat` set, `daemon.rover.driver.name == "sbg_ellipse"`, `daemon.controller is None`, `daemon.ins is not None`.
- `test_ntrip_client_skipped_when_driver_rejects_rtcm`: `rover_driver="vectornav"`, `ntrip_url` set, `ins_vn_rtcm=False` → `daemon.rover.ntrip_client is None` and an `events.new` info event "corrections not supported by driver".
- `test_ubx_reframed_frames_reach_raw_logger`: SBG bundle with `raw_writer` fake; GPS1_RAW frames carrying UBX → writer `handle` called with a `Proto.UBX` frame.

`tests/unit/test_web_ins.py` (FastAPI `TestClient` with a fake `daemon.ins`):
- `GET /api/status` → `driver.name == "vectornav"`, `capabilities.accepts_rtcm is False`.
- `GET /api/receiver` → `ins.vendor == "vectornav"`, `ins.info.model`, `capabilities.spectrum is False`.
- `POST /api/receiver/reset {"kind":"factory"}` → 409; `{"kind":"warm"}` → calls fake `reset` once.
- `POST /api/receiver/profile` without force and `ins_apply_config=False` → 409 with hint; with `{"force": true}` → fake `configure(apply=True)` called, report returned.

`web/src/components/InsPanel.test.tsx`: renders vendor/model, shows mismatched items in the serious color and the apply button disabled when no `pending`/`mismatched` items; hides IMU block when `imu` null.

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement**

`daemon.py` changes (sketch):
```python
        self.ins: InsBundle | None = None
        if settings.role is Role.ROVER and settings.rover_driver != "ublox":
            self.controller = None
            self.ins = build_ins(settings, self.bus, source_factory)
            self.store = StoreFacade(self.ins.adapter)  # .state proxies adapter.state; .note_rtcm_injected forwards
            topic = TOPIC_RAW_SBG if settings.rover_driver == "sbg_ellipse" else TOPIC_RAW_VN
            self._raw_sub = self.bus.subscribe(topic, maxsize=5000)
        else:
            ... existing ReceiverController wiring ...

    async def _state_loop(self) -> None:
        async for _, frame in self._raw_sub:
            if self.ins is not None:
                self.ins.adapter.handle(frame)
            else:
                self.store.apply(frame)
```
`run()` awaits `self.ins.controller.run(self.stop)` in place of the receiver controller task; `_run_rover` picks `driver = self.ins.driver if self.ins else UbloxDriver(...)`; `_restart_ntrip` refuses (info event) when `not driver.capabilities.accepts_rtcm`. `build_ins` wires `raw_writer` after the raw log writer exists (`_consumers()` order: raw log first).

API/UI/CLI per *Interfaces*. `InsPanel.tsx` reuses Phase 4 `Panel`, `Stat`, `DataTable`, `ConfirmDialog`, status colors from the design tokens.

- [ ] **Step 4: Run everything, lint, commit**

```bash
uv run pytest -q && uv run ruff check . && uv run mypy src && (cd web && pnpm test -- --run && pnpm build)
git add src/mtrtk web/src tests/unit/test_daemon_ins.py tests/unit/test_web_ins.py
git commit -m "feat(ins): daemon driver selection, INS API/WS/alerts, Receiver/RTK UI panels, mtrtk ins CLI, doctor checks

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Documentation, verification matrix and hardware validation checklist

**Files:**
- Create: `docs/ins-drivers.md`
- Modify: `README.md` (supported hardware table: Ellipse-D / VN-200 → "spec-based, awaiting hardware validation"), `docs/rover.md` (link), `.env.example` (final INS block review), `CHANGELOG.md` (Unreleased → INS drivers)

- [ ] **Step 1: Write `docs/ins-drivers.md`**

Sections:
1. **Which unit for what** — F9P (RTK, cheapest), Ellipse-D (RTK + dual-antenna heading + IMU, accepts RTCM, raw GNSS for PPK), VN-200 (INS, single antenna, no documented RTK input; heading from motion).
2. **Wiring** — Ellipse-D: Port A (RS-232/TTL per variant) ↔ USB-serial (FTDI) → `INS_PORT=/dev/serial/by-id/usb-FTDI_…`; Port B ↔ second USB-serial for RTCM (`INS_RTCM_PORT`) or same cable (unverified); antennas, lever arm convention (IMU frame X forward, Y right, Z down; measure to the antenna ARP; `INS_LEVER_ARM_GNSS1="x,y,z"`; dual antenna: secondary lever arm + `DUAL_PRECISE` when measured to ±1 cm); power. VN-200: rugged connector pinout (TX/RX/GND, 3.3 V TTL vs RS-232 variants), GNSS antenna offset `INS_LEVER_ARM_GNSS1` → register 57; SyncIn for camera events.
3. **Configuration** — every `INS_*` key with default and vendor mapping; the read-only-first rule (`INS_APPLY_CONFIG=0` default); `mtrtk ins info` / `mtrtk ins config --dry-run` / `--apply`; what gets saved to flash and that SBG reboots on save; baud guidance (460800 for raw GNSS / >50 Hz; set once in sbgCenter / VectorNav Control Center — mtrtk never changes baud).
4. **What you get** — table of `ReceiverState` sections filled per driver (position/accuracy/fix/velocity/time/sats/attitude/imu/rtk/time_marks) and which outputs light up (NMEA HDT/PASHR, ROS `/mtrtk/imu`, `/mtrtk/heading`).
5. **Raw GNSS / PPK** — SBG: UBX re-framed into hourly `.ubx` → normal RINEX export and PPK; VN-200: `.vnraw` capture only, "RINEX conversion not implemented" (with the record format so someone can write it).
6. **Verified / Unverified matrix** — every `# VERIFY` in code, grouped: *Verified from primary source* (sbgECom 5.8 headers: frame, CRC, ids, log layouts, command payloads, ACK; vnproglib 1.2: field sizes, group bits, error codes, register command formats, CRC) vs. *Unverified until hardware* (Ellipse `GPS1_RAW` is UBX; RTCM multiplexed on Port A; `IMU_SHORT` gyro scale selection; firmware version packing; VN-200 RTCM acceptance and Fix values 7/8; VN SatInfo/RawMeas record layouts and `sys` numbering; register 75 extension encoding; register 67 scenario values; VN TimeUTC year base 2000). Each row: assumption, where it is used, how to verify, fallback behaviour if wrong.
7. **Hardware validation checklist** (to run when a unit arrives; results go into `docs/acceptance.md`): connect → `mtrtk ins info` shows model/firmware; `mtrtk ins monitor` shows EKF/INS mode progressing to tracking outdoors; `mtrtk ins config --dry-run` review → `--apply` → power-cycle → `ins info` shows values persisted; NTRIP → SBG `GPS1_POS` type reaches 7 (RTK_INT) and `RTCM_RAW` counter increments; SBG `GPS1_RAW` UBX detected → hourly `.ubx` present → `convbin` produces RINEX; camera pulse on Sync In A → `time_marks` in the UI and `events.csv` from PPK; VN-200: RawMeas probe outcome recorded, RTCM forward outcome recorded (`Fix` value seen), SyncIn events; NMEA HDT visible in a client; ROS `/mtrtk/imu` publishes.
8. **Troubleshooting** — no frames (baud/port/protocol mode: Port A must be in sbgECom mode; VN in binary output mode 1), `crc_failed` climbing (wrong baud / cable), `invalid_headers` (VN ASCII still on → register 6 = 0), configuration `mismatched` (settings locked / value clamped by firmware), SBG reboot loop after save (power).

- [ ] **Step 2: Cross-links and changelog**

Add the INS row to the README hardware table, link `docs/ins-drivers.md` from `docs/rover.md` and the README docs index; add to `CHANGELOG.md` *Unreleased → Added*: "INS drivers for SBG Ellipse-D and VectorNav VN-200 (spec-based; hardware validation pending)". Run the Phase 9 relative-link check snippet.

- [ ] **Step 3: Commit**

```bash
git add docs/ins-drivers.md README.md docs/rover.md CHANGELOG.md .env.example
git commit -m "docs: INS drivers guide with wiring, configuration, verified/unverified matrix and hardware validation checklist

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Phase 10 exit criteria

- `uv run pytest -q` green including `tests/unit/sbg`, `tests/unit/vectornav`, `test_ins_common`, `test_daemon_ins`, `test_web_ins`; golden fixtures committed.
- `ROVER_DRIVER=sbg_ellipse` / `vectornav` with a replayed fixture stream (`MTRTK_SOURCE=file:tests/fixtures/ins/sbg_frames.bin` — add a `.bin` alongside the `.hex` for `FileReplaySource`, which must learn to pace on host time when no UBX iTOW exists: `replay_pace="host"` fallback) shows position, attitude and IMU in the UI, NMEA `HDT`/`PASHR` on the TCP sink, and `/mtrtk/imu` in ROS.
- `mtrtk ins info|config --dry-run|monitor` work against the scripted fake device in tests and print sensible output.
- Every `# VERIFY` in `src/mtrtk/rover/drivers/` appears in the matrix in `docs/ins-drivers.md` (add `tests/unit/test_verify_markers.py` that greps the source tree for `# VERIFY` and asserts each marker's short tag is present in the doc).
- No behaviour change for `ROVER_DRIVER=ublox` (full Phase 6 suite still passes).

This is the last implementation plan. `docs/superpowers/plans/README.md` lists all plans in execution order.
