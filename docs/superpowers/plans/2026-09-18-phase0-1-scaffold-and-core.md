# mtrtk Phase 0–1: Scaffold + Receiver Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runnable `mtrtk` daemon that opens a ZED-F9P (or replays a recorded file), frames UBX/RTCM3/NMEA bytes, keeps a typed live `ReceiverState`, applies and verifies the receiver configuration profile, and ships in a multi-arch Docker image — the foundation every later phase builds on.

**Architecture:** One asyncio process. `Source` (serial or file replay) → `Framer` (incremental UBX/RTCM3/NMEA splitter) → `Router` publishes frames on an in-process `Bus` → `StateStore` maps parsed UBX into `ReceiverState`; `UbxLink` correlates CFG-VALSET/VALGET/poll requests with ACK/response frames; `ReceiverController` owns connect/reconnect, capability probe, profile apply and verify. `Daemon` wires it all per role. No web, logging or caster yet (Phases 2–3).

**Tech Stack:** Python 3.12, uv, click, pydantic-settings, pyubx2 1.3.6, pyrtcm 1.2, pynmeagps 1.1, pyserial-asyncio-fast 0.16, pytest + pytest-asyncio (auto mode), ruff, mypy. Frontend skeleton: React 19 + Vite + TypeScript + Tailwind v4 (pnpm). Docker multi-stage with RTKLIB demo5 `v2.5.1`.

**Spec:** `docs/superpowers/specs/2026-09-18-mtrtk-design.md` (sections *Architecture*, *core*, *Configuration*, *docker / deploy*, *Implementation reference*, *Phase 0*, *Phase 1*).

## Global Constraints

- Python `>=3.12`; package manager is `uv`; all Python code under `src/mtrtk/`; tests under `tests/`; fixtures under `tests/fixtures/`.
- Every CFG key name must exist in `pyubx2.ubxtypes_configdb.UBX_CONFIG_DATABASE` (test enforces). Max 64 key/value pairs per CFG-VALSET.
- One unknown key NAKs a whole VALSET → core keys and optional feature keys are applied in separate VALSETs.
- Layers: RAM|BBR|FLASH on the first explicit profile apply; RAM only on automatic reconnects.
- Live consumers use `Policy.DROP_OLDEST`; nothing on the hot path parses RXM-RAWX / RXM-SFRBX (lazy parse only).
- Hour boundaries and epochs come from receiver time (NAV-PVT / NAV-EOE), never the host clock.
- Firmware under test is **HPG 1.13 / PROTVER 27.12**; code must also run on HPG 1.51. Unsupported optional keys are skipped, never fatal.
- Hardware tests are marked `@pytest.mark.hardware` and excluded by default (`addopts = "-m 'not hardware'"`). They talk to the F9P on `MTRTK_TEST_PORT` (default `/dev/ttyACM0`) and **will reconfigure it** (user approved).
- Commit after every task with a Conventional Commit message ending in `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Docker image must build for `linux/amd64` and `linux/arm64`; container runs with `network_mode: host`, `/dev` bind and `device_cgroup_rules`.

---

## File structure (this plan)

| Path | Responsibility |
|---|---|
| `pyproject.toml`, `uv.lock`, `.python-version` | Project metadata, deps, tool config |
| `src/mtrtk/__init__.py` | `__version__` |
| `src/mtrtk/cli.py` | `mtrtk` click group: `run`, `base`, `rover`, `replay`, `record`, `doctor` |
| `src/mtrtk/config.py` | `Settings` (pydantic-settings, `.env`), enums, validators |
| `src/mtrtk/core/crc.py` | UBX Fletcher, CRC-24Q, NMEA XOR |
| `src/mtrtk/core/frames.py` | `Proto`, `Frame` (lazy parse), `Framer` (incremental splitter) |
| `src/mtrtk/core/bus.py` | `Bus`, `Subscription`, `Policy` |
| `src/mtrtk/core/router.py` | `topics_for(frame)`, `Router` (framer → bus) |
| `src/mtrtk/core/source.py` | `ByteSource` protocol, `SerialSource`, `FileReplaySource`, `find_ublox_port` |
| `src/mtrtk/core/recorder.py` | synchronous raw stream recorder (fixtures) |
| `src/mtrtk/core/state.py` | pydantic models: `ReceiverState` and sections, name tables |
| `src/mtrtk/core/statestore.py` | `StateStore.apply(frame)` → sections changed, publishes `state.<section>` |
| `src/mtrtk/core/ubx_config.py` | `Profile`, `base_profile`, `rover_profile`, TMODE helpers, `chunked` |
| `src/mtrtk/core/link.py` | `UbxLink`: poll / valset / valget with ACK correlation |
| `src/mtrtk/core/receiver.py` | `Capabilities`, `ReceiverController` (connect, probe, configure, verify, watchdog) |
| `src/mtrtk/core/exposure.py` | `tailscale_ipv4()` (used by `doctor` now, caster later) |
| `src/mtrtk/daemon.py` | `Daemon` supervisor, `StatusPrinter` |
| `src/mtrtk/doctor.py` | environment checks |
| `tests/ubxtest.py` | frame builders for tests (`ubx_frame`, `rtcm_frame`, `nmea_frame`, `FakeReceiver`) |
| `tests/unit/*.py`, `tests/hardware/*.py` | tests |
| `tests/fixtures/f9p_hpg113_raw_10s.ubx`, `..._60s.ubx` | recorded streams from the user's F9P |
| `web/` | Vite + React + TS + Tailwind skeleton (builds to `web/dist`) |
| `docker/Dockerfile`, `docker-compose.yml`, `.env.example`, `.github/workflows/ci.yml`, `README.md` | Packaging, deploy, CI, docs |

---

### Task 1: Project scaffold and CLI entry point

**Files:**
- Create: `pyproject.toml`, `.python-version`, `src/mtrtk/__init__.py`, `src/mtrtk/py.typed`, `src/mtrtk/cli.py`, `tests/unit/test_cli.py`, `README.md`

**Interfaces:**
- Produces: `mtrtk.__version__ == "0.1.0"`; click group `mtrtk.cli.main`; console script `mtrtk`.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "mtrtk"
version = "0.1.0"
description = "Multi-role GNSS RTK/PPK/PPP toolkit for u-blox ZED-F9P base stations and multi-receiver rovers"
readme = "README.md"
license = "MIT"
requires-python = ">=3.12"
dependencies = [
  "pyubx2>=1.3.6",
  "pyrtcm>=1.2.0",
  "pynmeagps>=1.1.7",
  "pyserial>=3.5",
  "pyserial-asyncio-fast>=0.16",
  "pydantic>=2.11",
  "pydantic-settings>=2.10",
  "click>=8.1",
  "psutil>=7",
]

[project.scripts]
mtrtk = "mtrtk.cli:main"

[dependency-groups]
dev = [
  "pytest>=8.3",
  "pytest-asyncio>=1.0",
  "ruff>=0.11",
  "mypy>=1.15",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/mtrtk"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["tests"]
markers = ["hardware: talks to a live receiver on MTRTK_TEST_PORT and reconfigures it"]
addopts = "-m 'not hardware'"

[tool.ruff]
line-length = 100
target-version = "py312"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "ASYNC", "SIM"]

[tool.mypy]
python_version = "3.12"
packages = ["mtrtk"]
mypy_path = "src"
warn_unused_ignores = true
check_untyped_defs = true

[[tool.mypy.overrides]]
module = ["pyubx2.*", "pyrtcm.*", "pynmeagps.*", "serial.*", "serial_asyncio_fast.*", "psutil.*"]
ignore_missing_imports = true
```

- [ ] **Step 2: Write package files**

`.python-version`:
```
3.12
```

`src/mtrtk/__init__.py`:
```python
"""mtrtk - GNSS RTK/PPK/PPP toolkit."""

__version__ = "0.1.0"
```

`src/mtrtk/py.typed`: empty file.

`src/mtrtk/cli.py`:
```python
"""mtrtk command line interface."""

from __future__ import annotations

import click

from mtrtk import __version__


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="mtrtk")
def main() -> None:
    """mtrtk - GNSS RTK/PPK/PPP toolkit for ZED-F9P base stations and rovers."""
```

`README.md`:
```markdown
# mtrtk

Multi-role GNSS toolkit for u-blox ZED-F9P: RTK base station (NTRIP caster, raw UBX logging,
RINEX export for PPP), rovers (NTRIP client, NMEA/ROS2 outputs, survey points) and PPK
post-processing. Design: `docs/superpowers/specs/2026-09-18-mtrtk-design.md`.

## Development

```bash
uv sync
uv run pytest
uv run mtrtk --help
```
```

- [ ] **Step 3: Write the failing CLI test**

`tests/unit/test_cli.py`:
```python
from click.testing import CliRunner

from mtrtk.cli import main


def test_version_flag() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "mtrtk, version 0.1.0" in result.output
```

- [ ] **Step 4: Install and run the test**

Run: `cd /home/nekosaif/github/mtrtk && uv sync && uv run pytest -q`
Expected: `1 passed`. (`uv sync` creates `.venv` and `uv.lock`.)

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: no errors (run `uv run ruff format .` if formatting complains).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock .python-version src/mtrtk README.md tests/unit/test_cli.py
git commit -m "feat: project scaffold with uv, click CLI and test tooling

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Settings from `.env`

**Files:**
- Create: `src/mtrtk/config.py`, `tests/unit/test_config.py`

**Interfaces:**
- Produces: `Settings` (pydantic-settings `BaseSettings`), enums `Role`, `BaseMode`, `DynModel`, constants `BIND_MODES`, properties `Settings.source_is_file`, `Settings.source_path`, `Settings.dynmodel_code`, `Settings.ntrip_anonymous`. Later tasks read `settings.role`, `settings.mtrtk_source`, `settings.baud`, `settings.rtcm_msm`, `settings.rtcm_1230_rate`, `settings.rtcm_station_id`, `settings.rover_nav_hz`, `settings.replay_speed`, `settings.replay_loop`, `settings.data_dir`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_config.py`:
```python
import pytest
from pydantic import ValidationError

from mtrtk.config import DynModel, Role, Settings


def make(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setenv("NTRIP_PASSWORD", "secret")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make(monkeypatch)
    assert s.role is Role.BASE
    assert s.station_id == "MTRK"
    assert s.ntrip_bind == "tailscale"
    assert s.rtcm_msm == 7
    assert s.source_is_file is False
    assert s.ntrip_anonymous is False


def test_env_override_and_role(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make(monkeypatch, ROLE="rover", ROVER_NAV_HZ="8", ROVER_DYNMODEL="automotive")
    assert s.role is Role.ROVER
    assert s.rover_nav_hz == 8
    assert s.rover_dynmodel is DynModel.AUTOMOTIVE
    assert s.dynmodel_code == 4


def test_file_source(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make(monkeypatch, MTRTK_SOURCE="file:tests/fixtures/x.ubx")
    assert s.source_is_file is True
    assert str(s.source_path) == "tests/fixtures/x.ubx"


def test_public_web_bind_requires_password(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="WEB_PASSWORD"):
        make(monkeypatch, WEB_BIND="all")


def test_public_web_bind_allowed_when_insecure_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make(monkeypatch, WEB_BIND="all", WEB_ALLOW_INSECURE="1")
    assert s.web_bind == "all"


def test_bind_accepts_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make(monkeypatch, NTRIP_BIND="100.100.50.10")
    assert s.ntrip_bind == "100.100.50.10"


def test_bind_rejects_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="NTRIP_BIND"):
        make(monkeypatch, NTRIP_BIND="everywhere")


def test_csv_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make(monkeypatch, NMEA_UDP_TARGETS="192.168.1.5:10110, 10.0.0.2:5000", LOG_MESSAGES="RXM-RAWX,RXM-SFRBX")
    assert s.nmea_udp_targets == ["192.168.1.5:10110", "10.0.0.2:5000"]
    assert s.log_messages == ["RXM-RAWX", "RXM-SFRBX"]


def test_base_requires_ntrip_password_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NTRIP_PASSWORD", raising=False)
    with pytest.raises(ValidationError, match="NTRIP_PASSWORD"):
        Settings(_env_file=None)


def test_empty_ntrip_password_means_anonymous(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make(monkeypatch, NTRIP_PASSWORD="")
    assert s.ntrip_anonymous is True


def test_rover_does_not_need_ntrip_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NTRIP_PASSWORD", raising=False)
    monkeypatch.setenv("ROLE", "rover")
    assert Settings(_env_file=None).role is Role.ROVER


def test_station_id_must_be_four_uppercase(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        make(monkeypatch, STATION_ID="abc")


def test_rtcm_msm_only_4_or_7(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        make(monkeypatch, RTCM_MSM="5")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_config.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.config'`.

- [ ] **Step 3: Write `src/mtrtk/config.py`**

```python
"""Runtime settings, loaded from environment variables and an optional .env file."""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Role(StrEnum):
    BASE = "base"
    ROVER = "rover"


class BaseMode(StrEnum):
    SURVEY_IN = "survey-in"
    FIXED = "fixed"
    OFF = "off"


class DynModel(StrEnum):
    PORTABLE = "portable"
    STATIONARY = "stationary"
    PEDESTRIAN = "pedestrian"
    AUTOMOTIVE = "automotive"
    AIRBORNE1G = "airborne1g"
    AIRBORNE2G = "airborne2g"
    AIRBORNE4G = "airborne4g"


# u-blox CFG-NAVSPG-DYNMODEL enumeration values
DYNMODEL_CODES: dict[DynModel, int] = {
    DynModel.PORTABLE: 0,
    DynModel.STATIONARY: 2,
    DynModel.PEDESTRIAN: 3,
    DynModel.AUTOMOTIVE: 4,
    DynModel.AIRBORNE1G: 6,
    DynModel.AIRBORNE2G: 7,
    DynModel.AIRBORNE4G: 8,
}

BIND_MODES = ("tailscale", "lan", "all")

DEFAULT_LOG_MESSAGES = [
    "RXM-RAWX",
    "RXM-SFRBX",
    "NAV-PVT",
    "NAV-HPPOSLLH",
    "NAV-SVIN",
    "TIM-TM2",
    "MON-VER",
]


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def _validate_bind(name: str, value: str) -> str:
    if value in BIND_MODES:
        return value
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be one of {BIND_MODES} or an IP address, got {value!r}") from exc
    return value


class Settings(BaseSettings):
    """All mtrtk configuration. Field names map to upper-case environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- identity / receiver -------------------------------------------------
    role: Role = Role.BASE
    mtrtk_source: str = "auto"  # "auto" | serial device path | "file:<path>"
    baud: int = 115200
    data_dir: Path = Path("/data")
    station_id: str = Field("MTRK", pattern=r"^[A-Z0-9]{4}$")
    country: str = "BGD"
    marker_name: str = "MTRK"
    antenna_type: str = "NONE"
    antenna_height_m: float = 0.0
    observer: str = "mtrtk"
    agency: str = "mtrtk"
    receiver_strict: bool = True  # fail startup if a core CFG key is rejected
    replay_speed: float = 1.0  # file source pacing multiplier; 0 = as fast as possible
    replay_loop: bool = False

    # --- base ----------------------------------------------------------------
    base_mode: BaseMode = BaseMode.SURVEY_IN
    svin_min_duration_s: int = 300
    svin_acc_limit_m: float = 2.0
    active_site: str | None = None
    rtcm_msm: Literal[4, 7] = 7
    rtcm_1230_rate: int = 5
    rtcm_station_id: int = Field(0, ge=0, le=4095)

    # --- NTRIP caster --------------------------------------------------------
    ntrip_bind: str = "tailscale"
    ntrip_port: int = 2101
    mountpoint: str = "MTRK"
    ntrip_user: str = "rover"
    ntrip_password: str | None = None  # None = not decided (error for base); "" = anonymous

    # --- web -----------------------------------------------------------------
    web_bind: str = "tailscale"
    web_port: int = 8080
    web_password: str | None = None
    web_allow_insecure: bool = False

    # --- logging / retention -------------------------------------------------
    log_messages: Annotated[list[str], NoDecode] = Field(default_factory=lambda: list(DEFAULT_LOG_MESSAGES))
    min_free_gb: float = 5.0
    fsync_interval_s: int = 10

    # --- rover ---------------------------------------------------------------
    rover_driver: Literal["ublox", "sbg_ellipse", "vectornav"] = "ublox"
    rover_nav_hz: int = Field(5, ge=1, le=8)
    rover_dynmodel: DynModel = DynModel.PORTABLE
    ntrip_url: str | None = None
    ntrip_gga_interval_s: int = 10
    nmea_tcp_port: int = 10110
    nmea_udp_targets: Annotated[list[str], NoDecode] = Field(default_factory=list)
    nmea_serial: str | None = None
    json_udp_port: int | None = None

    # --- alerts / exposure ---------------------------------------------------
    alert_webhook_url: str | None = None
    public_domain: str | None = None

    # --- validators ----------------------------------------------------------
    @field_validator("log_messages", "nmea_udp_targets", mode="before")
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("ntrip_bind")
    @classmethod
    def _ntrip_bind(cls, value: str) -> str:
        return _validate_bind("NTRIP_BIND", value)

    @field_validator("web_bind")
    @classmethod
    def _web_bind(cls, value: str) -> str:
        return _validate_bind("WEB_BIND", value)

    @model_validator(mode="after")
    def _cross_checks(self) -> Settings:
        if self.web_bind not in ("tailscale",) and not self.web_password and not self.web_allow_insecure:
            raise ValueError(
                "WEB_PASSWORD must be set when WEB_BIND is not 'tailscale' "
                "(or set WEB_ALLOW_INSECURE=1 to accept an unauthenticated UI)"
            )
        if self.role is Role.BASE and self.ntrip_password is None:
            raise ValueError(
                "NTRIP_PASSWORD must be set for the base role "
                "(use NTRIP_PASSWORD= with an empty value to allow anonymous rovers)"
            )
        return self

    # --- derived -------------------------------------------------------------
    @property
    def source_is_file(self) -> bool:
        return self.mtrtk_source.startswith("file:")

    @property
    def source_path(self) -> Path:
        if not self.source_is_file:
            raise ValueError("source is not a file")
        return Path(self.mtrtk_source[len("file:") :])

    @property
    def dynmodel_code(self) -> int:
        return DYNMODEL_CODES[self.rover_dynmodel]

    @property
    def ntrip_anonymous(self) -> bool:
        return self.ntrip_password == ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_config.py -q`
Expected: `13 passed`.

- [ ] **Step 5: Lint, then commit**

Run: `uv run ruff check . && uv run ruff format . && uv run mypy`

```bash
git add src/mtrtk/config.py tests/unit/test_config.py
git commit -m "feat(config): pydantic-settings Settings with .env loading and cross-field validation

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Checksums and the incremental framer

**Files:**
- Create: `src/mtrtk/core/__init__.py` (empty), `src/mtrtk/core/crc.py`, `src/mtrtk/core/frames.py`, `tests/ubxtest.py`, `tests/unit/test_crc.py`, `tests/unit/test_frames.py`

**Interfaces:**
- Produces: `crc24q(data: bytes) -> int`, `ubx_checksum(data: bytes) -> bytes`, `nmea_checksum(body: bytes) -> int`; `Proto` enum (`UBX`, `RTCM3`, `NMEA`); `Frame(proto, raw, t_mono, t_host)` with `.identity: str`, `.ubx_class_id: tuple[int,int]`, `.rtcm_type: int`, `.payload: bytes`, `.parsed() -> Any` (lazy, cached); `Framer.feed(data: bytes) -> list[Frame]` with `.stats: FramerStats(frames: dict[str,int], garbage_bytes, checksum_errors)`.
- Test helpers: `ubx_frame(cls, msg_id, payload) -> bytes`, `rtcm_frame(msg_type, payload_rest=b"\x00"*10) -> bytes`, `nmea_frame(body: str) -> bytes`.

- [ ] **Step 1: Write the test helpers**

`tests/ubxtest.py`:
```python
"""Builders for synthetic wire frames used across the test-suite."""

from __future__ import annotations

from mtrtk.core.crc import crc24q, nmea_checksum, ubx_checksum


def ubx_frame(cls: int, msg_id: int, payload: bytes) -> bytes:
    body = bytes((cls, msg_id)) + len(payload).to_bytes(2, "little") + payload
    return b"\xb5\x62" + body + ubx_checksum(body)


def rtcm_frame(msg_type: int, payload_rest: bytes = b"\x00" * 10) -> bytes:
    """RTCM3 frame whose first 12 payload bits carry *msg_type*."""
    first = bytes(((msg_type >> 4) & 0xFF, ((msg_type & 0x0F) << 4) | (payload_rest[0] & 0x0F)))
    payload = first + payload_rest[1:]
    head = bytes((0xD3, (len(payload) >> 8) & 0x03, len(payload) & 0xFF))
    return head + payload + crc24q(head + payload).to_bytes(3, "big")


def nmea_frame(body: str) -> bytes:
    raw = body.encode("ascii")
    return b"$" + raw + b"*%02X\r\n" % nmea_checksum(raw)
```

- [ ] **Step 2: Write the failing checksum tests**

`tests/unit/test_crc.py`:
```python
from pyrtcm.rtcmhelpers import calc_crc24q
from pyubx2.ubxhelpers import calc_checksum

from mtrtk.core.crc import crc24q, nmea_checksum, ubx_checksum


def test_crc24q_known_vector() -> None:
    # CRC-24/LTE-A == RTCM CRC-24Q: poly 0x864CFB, init 0, check value for "123456789"
    assert crc24q(b"123456789") == 0xCDE703


def test_crc24q_matches_pyrtcm() -> None:
    data = bytes(range(256)) * 3
    assert crc24q(data) == calc_crc24q(data)


def test_ubx_checksum_matches_pyubx2() -> None:
    body = b"\x01\x07\x04\x00\xde\xad\xbe\xef"
    assert ubx_checksum(body) == calc_checksum(body)


def test_nmea_checksum_textbook_gga() -> None:
    assert nmea_checksum(b"GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,") == 0x47
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/unit/test_crc.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.core'`.

- [ ] **Step 4: Write `src/mtrtk/core/crc.py`**

```python
"""Checksums used by the GNSS wire protocols we frame."""

from __future__ import annotations


def ubx_checksum(data: bytes) -> bytes:
    """8-bit Fletcher checksum over UBX class, id, length and payload -> bytes((CK_A, CK_B))."""
    ck_a = ck_b = 0
    for byte in data:
        ck_a = (ck_a + byte) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return bytes((ck_a, ck_b))


def _build_crc24q_table() -> list[int]:
    table: list[int] = []
    for i in range(256):
        crc = i << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
        table.append(crc & 0xFFFFFF)
    return table


_CRC24Q_TABLE = _build_crc24q_table()


def crc24q(data: bytes) -> int:
    """CRC-24Q as used by RTCM 3 (poly 0x1864CFB, init 0), returned as a 24-bit int."""
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFF) ^ _CRC24Q_TABLE[((crc >> 16) ^ byte) & 0xFF]
    return crc


def nmea_checksum(body: bytes) -> int:
    """XOR of every byte between '$' and '*' (both exclusive)."""
    result = 0
    for byte in body:
        result ^= byte
    return result
```

Also create empty `src/mtrtk/core/__init__.py`.

- [ ] **Step 5: Run checksum tests**

Run: `uv run pytest tests/unit/test_crc.py -q`
Expected: `4 passed`.

- [ ] **Step 6: Write the failing framer tests**

`tests/unit/test_frames.py`:
```python
from pyubx2 import GET, UBXMessage
from ubxtest import nmea_frame, rtcm_frame, ubx_frame

from mtrtk.core.frames import Frame, Framer, Proto

PVT = ubx_frame(0x01, 0x07, b"\x00" * 92)
SAT_EMPTY = ubx_frame(0x01, 0x35, b"\x00" * 8)
RAWX_EMPTY = ubx_frame(0x02, 0x15, b"\x00" * 16)


def test_ubx_single_feed() -> None:
    frames = Framer().feed(PVT)
    assert len(frames) == 1
    f = frames[0]
    assert f.proto is Proto.UBX
    assert f.identity == "NAV-PVT"
    assert f.ubx_class_id == (0x01, 0x07)
    assert f.payload == b"\x00" * 92


def test_frame_split_across_feeds() -> None:
    framer = Framer()
    assert framer.feed(PVT[:10]) == []
    out = framer.feed(PVT[10:])
    assert len(out) == 1 and out[0].raw == PVT


def test_garbage_is_skipped_and_counted() -> None:
    framer = Framer()
    out = framer.feed(b"\x00\xffjunk" + SAT_EMPTY)
    assert [f.identity for f in out] == ["NAV-SAT"]
    assert framer.stats.garbage_bytes == 6


def test_bad_ubx_checksum_is_dropped() -> None:
    corrupt = bytearray(PVT)
    corrupt[-1] ^= 0xFF
    framer = Framer()
    assert framer.feed(bytes(corrupt)) == []
    assert framer.stats.checksum_errors == 1


def test_stray_sync_byte_before_frame() -> None:
    out = Framer().feed(b"\xb5" + PVT)
    assert len(out) == 1 and out[0].identity == "NAV-PVT"


def test_rtcm_frame_type_and_identity() -> None:
    out = Framer().feed(rtcm_frame(1077, b"\x00" * 20))
    assert out[0].proto is Proto.RTCM3
    assert out[0].rtcm_type == 1077
    assert out[0].identity == "1077"
    assert len(out[0].payload) == 20


def test_bad_rtcm_crc_is_dropped() -> None:
    corrupt = bytearray(rtcm_frame(1005, b"\x00" * 16))
    corrupt[-1] ^= 0x01
    framer = Framer()
    assert framer.feed(bytes(corrupt)) == []
    assert framer.stats.checksum_errors == 1


def test_nmea_frame() -> None:
    out = Framer().feed(nmea_frame("GNGGA,123519.00,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,"))
    assert out[0].proto is Proto.NMEA
    assert out[0].identity == "GNGGA"


def test_mixed_stream_in_order() -> None:
    stream = PVT + rtcm_frame(1005, b"\x00" * 16) + nmea_frame("GNGGA,,,,,,0,00,99.99,,,,,,") + RAWX_EMPTY
    out = Framer().feed(stream)
    assert [f.proto for f in out] == [Proto.UBX, Proto.RTCM3, Proto.NMEA, Proto.UBX]
    assert out[3].identity == "RXM-RAWX"


def test_stats_count_frames() -> None:
    framer = Framer()
    framer.feed(PVT + PVT + rtcm_frame(1230))
    assert framer.stats.frames == {"ubx": 2, "rtcm3": 1, "nmea": 0}


def test_lazy_parse_is_cached() -> None:
    raw = UBXMessage("NAV", "NAV-EOE", GET, iTOW=1234).serialize()
    frame: Frame = Framer().feed(raw)[0]
    assert frame.parsed().iTOW == 1234
    assert frame.parsed() is frame.parsed()


def test_unknown_ubx_identity_falls_back_to_hex() -> None:
    out = Framer().feed(ubx_frame(0x7E, 0x7F, b"\x00"))
    assert out[0].identity == "UBX-7E-7F"
```

- [ ] **Step 7: Run to verify failure**

Run: `uv run pytest tests/unit/test_frames.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.core.frames'`.

- [ ] **Step 8: Write `src/mtrtk/core/frames.py`**

```python
"""Incremental framer: splits a mixed UBX / RTCM3 / NMEA byte stream into frames."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import pyubx2
from pynmeagps import NMEAReader
from pyrtcm import RTCMReader
from pyubx2 import UBXReader

from mtrtk.core.crc import crc24q, nmea_checksum, ubx_checksum

UBX_SYNC1 = 0xB5
UBX_SYNC2 = 0x62
RTCM_PREAMBLE = 0xD3
NMEA_START = 0x24  # '$'
UBX_MAX_PAYLOAD = 8192
NMEA_MAX_LEN = 128
_SYNC_BYTES = frozenset((UBX_SYNC1, RTCM_PREAMBLE, NMEA_START))


class Proto(str, Enum):
    UBX = "ubx"
    RTCM3 = "rtcm3"
    NMEA = "nmea"


@dataclass(eq=False)
class Frame:
    """One complete protocol frame with its raw bytes. Parsing is lazy and cached."""

    proto: Proto
    raw: bytes
    t_mono: float
    t_host: float
    _parsed: Any = field(default=None, repr=False)

    @property
    def ubx_class_id(self) -> tuple[int, int]:
        if self.proto is not Proto.UBX:
            raise ValueError("not a UBX frame")
        return self.raw[2], self.raw[3]

    @property
    def rtcm_type(self) -> int:
        if self.proto is not Proto.RTCM3:
            raise ValueError("not an RTCM3 frame")
        return (self.raw[3] << 4) | (self.raw[4] >> 4)

    @property
    def identity(self) -> str:
        if self.proto is Proto.UBX:
            name = pyubx2.UBX_MSGIDS.get(self.raw[2:4])
            return name if name else f"UBX-{self.raw[2]:02X}-{self.raw[3]:02X}"
        if self.proto is Proto.RTCM3:
            return str(self.rtcm_type)
        end = self.raw.find(b",")
        return self.raw[1 : end if end > 0 else 6].decode("ascii", "replace")

    @property
    def payload(self) -> bytes:
        if self.proto is Proto.UBX:
            return self.raw[6:-2]
        if self.proto is Proto.RTCM3:
            return self.raw[3:-3]
        return self.raw

    def parsed(self) -> Any:
        """Parse with pyubx2 / pyrtcm / pynmeagps on first call and cache the result."""
        if self._parsed is None:
            if self.proto is Proto.UBX:
                self._parsed = UBXReader.parse(self.raw)
            elif self.proto is Proto.RTCM3:
                self._parsed = RTCMReader.parse(self.raw)
            else:
                self._parsed = NMEAReader.parse(self.raw)
        return self._parsed


@dataclass
class FramerStats:
    frames: dict[str, int] = field(default_factory=lambda: {"ubx": 0, "rtcm3": 0, "nmea": 0})
    garbage_bytes: int = 0
    checksum_errors: int = 0


class Framer:
    """Feed bytes in any chunking; get back complete, checksum-verified frames."""

    def __init__(self, max_buffer: int = 1 << 20) -> None:
        self._buf = bytearray()
        self._max_buffer = max_buffer
        self.stats = FramerStats()

    def feed(self, data: bytes) -> list[Frame]:
        self._buf += data
        if len(self._buf) > self._max_buffer:
            dropped = len(self._buf) - self._max_buffer
            del self._buf[:dropped]
            self.stats.garbage_bytes += dropped

        out: list[Frame] = []
        while self._buf:
            first = self._buf[0]
            if first not in _SYNC_BYTES:
                skip = self._next_sync()
                del self._buf[:skip]
                self.stats.garbage_bytes += skip
                continue
            if first == UBX_SYNC1:
                result = self._try_ubx()
            elif first == RTCM_PREAMBLE:
                result = self._try_rtcm()
            else:
                result = self._try_nmea()
            if result is None:  # incomplete: wait for more bytes
                break
            if result is False:  # invalid at this offset: resync one byte later
                del self._buf[:1]
                self.stats.garbage_bytes += 1
                continue
            out.append(result)
        return out

    def _next_sync(self) -> int:
        for i in range(1, len(self._buf)):
            if self._buf[i] in _SYNC_BYTES:
                return i
        return len(self._buf)

    def _emit(self, proto: Proto, length: int) -> Frame:
        raw = bytes(self._buf[:length])
        del self._buf[:length]
        self.stats.frames[proto.value] += 1
        return Frame(proto, raw, time.monotonic(), time.time())

    def _try_ubx(self) -> Frame | bool | None:
        buf = self._buf
        if len(buf) < 2:
            return None
        if buf[1] != UBX_SYNC2:
            return False
        if len(buf) < 6:
            return None
        length = buf[4] | (buf[5] << 8)
        if length > UBX_MAX_PAYLOAD:
            return False
        total = 6 + length + 2
        if len(buf) < total:
            return None
        if bytes(buf[total - 2 : total]) != ubx_checksum(bytes(buf[2 : total - 2])):
            self.stats.checksum_errors += 1
            return False
        return self._emit(Proto.UBX, total)

    def _try_rtcm(self) -> Frame | bool | None:
        buf = self._buf
        if len(buf) < 3:
            return None
        if buf[1] & 0xFC:
            return False
        length = ((buf[1] & 0x03) << 8) | buf[2]
        total = 3 + length + 3
        if len(buf) < total:
            return None
        expected = crc24q(bytes(buf[: 3 + length]))
        got = (buf[total - 3] << 16) | (buf[total - 2] << 8) | buf[total - 1]
        if expected != got:
            self.stats.checksum_errors += 1
            return False
        return self._emit(Proto.RTCM3, total)

    def _try_nmea(self) -> Frame | bool | None:
        buf = self._buf
        end = buf.find(b"\r\n", 0, NMEA_MAX_LEN + 2)
        if end < 0:
            return None if len(buf) < NMEA_MAX_LEN else False
        star = buf.rfind(b"*", 0, end)
        if star < 0 or end - star != 3:
            return False
        try:
            given = int(bytes(buf[star + 1 : end]), 16)
        except ValueError:
            return False
        if given != nmea_checksum(bytes(buf[1:star])):
            self.stats.checksum_errors += 1
            return False
        return self._emit(Proto.NMEA, end + 2)
```

- [ ] **Step 9: Run framer tests**

Run: `uv run pytest tests/unit/test_frames.py tests/unit/test_crc.py -q`
Expected: `16 passed`.

- [ ] **Step 10: Lint and commit**

Run: `uv run ruff check . && uv run ruff format . && uv run mypy`

```bash
git add src/mtrtk/core tests/ubxtest.py tests/unit/test_crc.py tests/unit/test_frames.py
git commit -m "feat(core): checksums and incremental UBX/RTCM3/NMEA framer with lazy parsing

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: In-process pub/sub bus

**Files:**
- Create: `src/mtrtk/core/bus.py`, `tests/unit/test_bus.py`

**Interfaces:**
- Produces: `Policy` (`DROP_OLDEST`, `UNBOUNDED`); `Bus.subscribe(*patterns, maxsize=1000, policy=Policy.DROP_OLDEST, high_water=10_000) -> Subscription`; `Bus.publish(topic: str, item: Any) -> None` (sync, never blocks); `Bus.unsubscribe(sub)`; `Bus.subscriber_count`; `Subscription` is an async iterator of `(topic, item)` with `.get()`, `.close()`, `.dropped`, `.high_water_hits`, `.queue`. Patterns: exact `"ubx.NAV-PVT"`, prefix `"ubx.*"`, or `"*"`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_bus.py`:
```python
import asyncio

from mtrtk.core.bus import Bus, Policy


async def test_exact_prefix_and_all_patterns() -> None:
    bus = Bus()
    exact = bus.subscribe("ubx.NAV-PVT")
    prefix = bus.subscribe("ubx.*")
    everything = bus.subscribe("*")
    bus.publish("ubx.NAV-PVT", 1)
    bus.publish("ubx.NAV-SAT", 2)
    bus.publish("raw.rtcm", 3)
    assert exact.queue.qsize() == 1
    assert prefix.queue.qsize() == 2
    assert everything.queue.qsize() == 3


async def test_drop_oldest_keeps_newest() -> None:
    bus = Bus()
    sub = bus.subscribe("t", maxsize=2)
    for i in range(5):
        bus.publish("t", i)
    assert sub.dropped == 3
    assert [(await sub.get())[1] for _ in range(2)] == [3, 4]


async def test_unbounded_never_drops_but_counts_high_water() -> None:
    bus = Bus()
    sub = bus.subscribe("t", policy=Policy.UNBOUNDED, high_water=3)
    for i in range(5):
        bus.publish("t", i)
    assert sub.dropped == 0
    assert sub.queue.qsize() == 5
    assert sub.high_water_hits == 3


async def test_async_iteration_ends_on_close() -> None:
    bus = Bus()
    sub = bus.subscribe("t")
    bus.publish("t", "x")
    bus.publish("t", "y")
    sub.close()
    assert [item async for _, item in sub] == ["x", "y"]


async def test_close_wakes_waiting_consumer() -> None:
    bus = Bus()
    sub = bus.subscribe("t")

    async def consume() -> list[object]:
        return [item async for _, item in sub]

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    sub.close()
    assert await asyncio.wait_for(task, 1.0) == []


async def test_unsubscribe_stops_delivery() -> None:
    bus = Bus()
    sub = bus.subscribe("t")
    bus.unsubscribe(sub)
    bus.publish("t", 1)
    assert bus.subscriber_count == 0


async def test_publish_after_close_is_ignored() -> None:
    bus = Bus()
    sub = bus.subscribe("t", maxsize=1)
    sub.close()
    bus.publish("t", 1)
    assert [item async for _, item in sub] == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_bus.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.core.bus'`.

- [ ] **Step 3: Write `src/mtrtk/core/bus.py`**

```python
"""Single-process asyncio publish/subscribe bus with a bounded queue per subscriber."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from enum import Enum
from typing import Any

_CLOSED = object()


class Policy(Enum):
    DROP_OLDEST = "drop_oldest"  # live consumers: lose the oldest item under pressure
    UNBOUNDED = "unbounded"  # never drop (raw logger); count high-water crossings instead


class Subscription:
    """Receives `(topic, item)` tuples for topics matching its patterns."""

    def __init__(self, patterns: tuple[str, ...], policy: Policy, maxsize: int, high_water: int) -> None:
        self.patterns = patterns
        self.policy = policy
        self.high_water = high_water
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self.high_water_hits = 0
        self._closed = False
        self._all = "*" in patterns
        self._prefixes = tuple(p[:-1] for p in patterns if p.endswith(".*"))
        self._exact = frozenset(p for p in patterns if not p.endswith("*"))

    def matches(self, topic: str) -> bool:
        return self._all or topic in self._exact or any(topic.startswith(p) for p in self._prefixes)

    def offer(self, topic: str, item: Any) -> None:
        if self._closed:
            return
        if self.policy is Policy.UNBOUNDED:
            self.queue.put_nowait((topic, item))
            if self.queue.qsize() >= self.high_water:
                self.high_water_hits += 1
            return
        try:
            self.queue.put_nowait((topic, item))
        except asyncio.QueueFull:
            self._evict_one()
            self.dropped += 1
            self.queue.put_nowait((topic, item))

    def _evict_one(self) -> None:
        try:
            self.queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

    async def get(self) -> tuple[str, Any]:
        return await self.queue.get()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.queue.put_nowait(("__closed__", _CLOSED))
        except asyncio.QueueFull:
            self._evict_one()
            self.queue.put_nowait(("__closed__", _CLOSED))

    def __aiter__(self) -> AsyncIterator[tuple[str, Any]]:
        return self

    async def __anext__(self) -> tuple[str, Any]:
        topic, item = await self.queue.get()
        if item is _CLOSED:
            raise StopAsyncIteration
        return topic, item


class Bus:
    def __init__(self) -> None:
        self._subs: list[Subscription] = []

    def subscribe(
        self,
        *patterns: str,
        maxsize: int = 1000,
        policy: Policy = Policy.DROP_OLDEST,
        high_water: int = 10_000,
    ) -> Subscription:
        size = 0 if policy is Policy.UNBOUNDED else maxsize
        sub = Subscription(tuple(patterns), policy, size, high_water)
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        sub.close()
        self._subs = [s for s in self._subs if s is not sub]

    def publish(self, topic: str, item: Any) -> None:
        for sub in self._subs:
            if sub.matches(topic):
                sub.offer(topic, item)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_bus.py -q`
Expected: `7 passed`.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add src/mtrtk/core/bus.py tests/unit/test_bus.py
git commit -m "feat(core): asyncio pub/sub bus with drop-oldest and unbounded policies

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Router — frames onto bus topics

**Files:**
- Create: `src/mtrtk/core/router.py`, `tests/unit/test_router.py`

**Interfaces:**
- Consumes: `Bus.publish`, `Framer.feed`, `Frame.identity`, `Frame.rtcm_type`.
- Produces: constants `TOPIC_RAW_UBX = "raw.ubx"`, `TOPIC_RAW_RTCM = "raw.rtcm"`, `TOPIC_RAW_NMEA = "raw.nmea"`; `topics_for(frame) -> tuple[str, ...]` (`raw.ubx` + `ubx.<IDENT>`, `raw.rtcm` + `rtcm.<TYPE>`, `raw.nmea` + `nmea.<TALKER+ID>`); `Router(bus, framer=None).feed(data) -> list[Frame]` with `.bytes_in`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_router.py`:
```python
from ubxtest import nmea_frame, rtcm_frame, ubx_frame

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.core.router import TOPIC_RAW_RTCM, TOPIC_RAW_UBX, Router, topics_for


def test_topics_for_each_protocol() -> None:
    framer = Framer()
    ubx, rtcm, nmea = framer.feed(
        ubx_frame(0x01, 0x07, b"\x00" * 92) + rtcm_frame(1005, b"\x00" * 16) + nmea_frame("GNGGA,,,,,,0,00,99.99,,,,,,")
    )
    assert topics_for(ubx) == ("raw.ubx", "ubx.NAV-PVT")
    assert topics_for(rtcm) == ("raw.rtcm", "rtcm.1005")
    assert topics_for(nmea) == ("raw.nmea", "nmea.GNGGA")


async def test_router_publishes_to_matching_subscribers() -> None:
    bus = Bus()
    pvt_sub = bus.subscribe("ubx.NAV-PVT")
    raw_ubx = bus.subscribe(TOPIC_RAW_UBX)
    raw_rtcm = bus.subscribe(TOPIC_RAW_RTCM)
    router = Router(bus)
    data = ubx_frame(0x01, 0x07, b"\x00" * 92) + ubx_frame(0x02, 0x15, b"\x00" * 16) + rtcm_frame(1077)
    frames = router.feed(data)
    assert len(frames) == 3
    assert pvt_sub.queue.qsize() == 1
    assert raw_ubx.queue.qsize() == 2
    assert raw_rtcm.queue.qsize() == 1
    assert router.bytes_in == len(data)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_router.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.core.router'`.

- [ ] **Step 3: Write `src/mtrtk/core/router.py`**

```python
"""Turns incoming bytes into frames and publishes them on bus topics."""

from __future__ import annotations

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame, Framer, Proto

TOPIC_RAW_UBX = "raw.ubx"
TOPIC_RAW_RTCM = "raw.rtcm"
TOPIC_RAW_NMEA = "raw.nmea"


def topics_for(frame: Frame) -> tuple[str, ...]:
    if frame.proto is Proto.UBX:
        return (TOPIC_RAW_UBX, f"ubx.{frame.identity}")
    if frame.proto is Proto.RTCM3:
        return (TOPIC_RAW_RTCM, f"rtcm.{frame.rtcm_type}")
    return (TOPIC_RAW_NMEA, f"nmea.{frame.identity}")


class Router:
    def __init__(self, bus: Bus, framer: Framer | None = None) -> None:
        self.bus = bus
        self.framer = framer or Framer()
        self.bytes_in = 0

    def feed(self, data: bytes) -> list[Frame]:
        self.bytes_in += len(data)
        frames = self.framer.feed(data)
        for frame in frames:
            for topic in topics_for(frame):
                self.bus.publish(topic, frame)
        return frames
```

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_router.py -q` → `2 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

```bash
git add src/mtrtk/core/router.py tests/unit/test_router.py
git commit -m "feat(core): router publishing frames to raw.* and per-message topics

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Byte sources — serial receiver and file replay

**Files:**
- Create: `src/mtrtk/core/source.py`, `tests/unit/test_source.py`

**Interfaces:**
- Consumes: `Framer`, `Proto`.
- Produces: `ByteSource` protocol (`name: str`, `async open()`, `async read() -> bytes` (empty = EOF), `async write(data)`, `async close()`); `find_ublox_port() -> str | None`; `SerialSource(port, baud=115200, read_size=4096)`; `FileReplaySource(path, speed=1.0, loop=False)` which yields one chunk per epoch and sleeps `iTOW delta / speed` (no sleep when `speed == 0`). Epoch marker is NAV-EOE when the file contains one, else NAV-PVT.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_source.py`:
```python
from pathlib import Path

import pytest
from pyubx2 import GET, UBXMessage
from ubxtest import ubx_frame

from mtrtk.core import source as source_mod
from mtrtk.core.source import FileReplaySource, find_ublox_port


def pvt(itow: int) -> bytes:
    return UBXMessage("NAV", "NAV-PVT", GET, iTOW=itow).serialize()


def eoe(itow: int) -> bytes:
    return UBXMessage("NAV", "NAV-EOE", GET, iTOW=itow).serialize()


RAWX = ubx_frame(0x02, 0x15, b"\x00" * 16)


async def read_all(src: FileReplaySource) -> list[bytes]:
    chunks: list[bytes] = []
    while chunk := await src.read():
        chunks.append(chunk)
    return chunks


async def test_replay_paces_on_nav_pvt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "r.ubx"
    path.write_bytes(pvt(1000) + RAWX + pvt(2000) + pvt(3000))
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(source_mod.asyncio, "sleep", fake_sleep)
    src = FileReplaySource(path, speed=2.0)
    await src.open()
    chunks = await read_all(src)
    assert len(chunks) == 3
    assert sleeps == [0.5, 0.5]
    assert chunks[1] == RAWX + pvt(2000)


async def test_replay_prefers_nav_eoe_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "r.ubx"
    path.write_bytes(pvt(1000) + RAWX + eoe(1000) + pvt(2000) + eoe(2000))
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(source_mod.asyncio, "sleep", fake_sleep)
    src = FileReplaySource(path, speed=1.0)
    await src.open()
    chunks = await read_all(src)
    assert len(chunks) == 2
    assert chunks[0] == pvt(1000) + RAWX + eoe(1000)
    assert sleeps == [1.0]


async def test_replay_speed_zero_never_sleeps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "r.ubx"
    path.write_bytes(pvt(1000) + pvt(2000))

    async def boom(delay: float) -> None:
        raise AssertionError("must not sleep")

    monkeypatch.setattr(source_mod.asyncio, "sleep", boom)
    src = FileReplaySource(path, speed=0)
    await src.open()
    assert len(await read_all(src)) == 2


async def test_replay_loop_restarts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "r.ubx"
    path.write_bytes(pvt(1000) + pvt(2000))

    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(source_mod.asyncio, "sleep", fake_sleep)
    src = FileReplaySource(path, speed=0, loop=True)
    await src.open()
    chunks = [await src.read() for _ in range(5)]
    assert all(chunks)
    assert chunks[2] == pvt(1000)


async def test_replay_write_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "r.ubx"
    path.write_bytes(pvt(1))
    src = FileReplaySource(path)
    await src.open()
    await src.write(b"\xb5\x62")
    await src.close()


def test_find_ublox_port_prefers_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source_mod.glob, "glob", lambda pattern: ["/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_GNSS_receiver-if00"])
    assert find_ublox_port() == "/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_GNSS_receiver-if00"


def test_find_ublox_port_falls_back_to_vid(monkeypatch: pytest.MonkeyPatch) -> None:
    class Port:
        def __init__(self, device: str, vid: int | None) -> None:
            self.device = device
            self.vid = vid

    monkeypatch.setattr(source_mod.glob, "glob", lambda pattern: [])
    monkeypatch.setattr(source_mod.list_ports, "comports", lambda: [Port("/dev/ttyUSB0", 0x0403), Port("/dev/ttyACM0", 0x1546)])
    assert find_ublox_port() == "/dev/ttyACM0"


def test_find_ublox_port_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source_mod.glob, "glob", lambda pattern: [])
    monkeypatch.setattr(source_mod.list_ports, "comports", lambda: [])
    assert find_ublox_port() is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_source.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.core.source'`.

- [ ] **Step 3: Write `src/mtrtk/core/source.py`**

```python
"""Byte sources: a live serial receiver, or a recorded file replayed at receiver pace."""

from __future__ import annotations

import asyncio
import glob
import logging
from pathlib import Path
from typing import Protocol

from serial.tools import list_ports
from serial_asyncio_fast import open_serial_connection

from mtrtk.core.frames import Frame, Framer, Proto

log = logging.getLogger(__name__)

UBLOX_VID = 0x1546
NAV_PVT = (0x01, 0x07)
NAV_EOE = (0x01, 0x61)


class ByteSource(Protocol):
    name: str

    async def open(self) -> None: ...

    async def read(self) -> bytes:
        """Return the next chunk; b"" means the source ended / disconnected."""
        ...

    async def write(self, data: bytes) -> None: ...

    async def close(self) -> None: ...


def find_ublox_port() -> str | None:
    """Stable by-id symlink first, then any port whose USB VID is u-blox."""
    by_id = sorted(glob.glob("/dev/serial/by-id/*u-blox*"))
    if by_id:
        return by_id[0]
    for port in list_ports.comports():
        if port.vid == UBLOX_VID:
            return str(port.device)
    return None


class SerialSource:
    def __init__(self, port: str, baud: int = 115200, read_size: int = 4096) -> None:
        self.port = port
        self.baud = baud
        self.read_size = read_size
        self.name = f"serial:{port}"
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def open(self) -> None:
        self._reader, self._writer = await open_serial_connection(url=self.port, baudrate=self.baud, limit=1 << 16)
        log.info("opened %s @ %d", self.port, self.baud)

    async def read(self) -> bytes:
        if self._reader is None:
            return b""
        return await self._reader.read(self.read_size)

    async def write(self, data: bytes) -> None:
        if self._writer is None:
            raise ConnectionError("serial port not open")
        self._writer.write(data)
        await self._writer.drain()

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
        self._reader = None
        self._writer = None


class FileReplaySource:
    """Replays a recorded stream one epoch per read(), pacing on receiver time (iTOW)."""

    def __init__(self, path: str | Path, speed: float = 1.0, loop: bool = False) -> None:
        self.path = Path(path)
        self.speed = speed
        self.loop = loop
        self.name = f"file:{self.path.name}"
        self._frames: list[Frame] = []
        self._marker = NAV_PVT
        self._idx = 0
        self._last_itow: int | None = None

    async def open(self) -> None:
        self._frames = Framer().feed(self.path.read_bytes())
        has_eoe = any(f.proto is Proto.UBX and f.ubx_class_id == NAV_EOE for f in self._frames)
        self._marker = NAV_EOE if has_eoe else NAV_PVT
        self._idx = 0
        self._last_itow = None
        log.info("replaying %s: %d frames, marker %s", self.path, len(self._frames), "NAV-EOE" if has_eoe else "NAV-PVT")

    async def read(self) -> bytes:
        if self._idx >= len(self._frames):
            if not self.loop:
                return b""
            self._idx = 0
            self._last_itow = None
        chunk = bytearray()
        while self._idx < len(self._frames):
            frame = self._frames[self._idx]
            self._idx += 1
            chunk += frame.raw
            if frame.proto is Proto.UBX and frame.ubx_class_id == self._marker:
                await self._pace(int.from_bytes(frame.raw[6:10], "little"))
                break
        return bytes(chunk)

    async def _pace(self, itow: int) -> None:
        if self._last_itow is not None and self.speed > 0:
            delta_s = (itow - self._last_itow) / 1000.0
            if 0 < delta_s < 60:
                await asyncio.sleep(delta_s / self.speed)
        self._last_itow = itow

    async def write(self, data: bytes) -> None:
        log.debug("replay source ignores %d bytes written", len(data))

    async def close(self) -> None:
        self._frames = []
```

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_source.py -q` → `8 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

```bash
git add src/mtrtk/core/source.py tests/unit/test_source.py
git commit -m "feat(core): serial and file-replay byte sources with u-blox port discovery

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: `mtrtk record` and real fixtures from the F9P

**Files:**
- Create: `src/mtrtk/core/recorder.py`, `tests/unit/test_recorder.py`, `tests/unit/test_fixtures.py`, `tests/fixtures/f9p_hpg113_raw_10s.ubx`, `tests/fixtures/f9p_hpg113_raw_60s.ubx`
- Modify: `src/mtrtk/cli.py` (add `record` command)

**Interfaces:**
- Consumes: `Framer`, `find_ublox_port`.
- Produces: `record_stream(port: str, baud: int, seconds: float, out_path: Path) -> RecordStats` (`bytes`, `frames: dict[str,int]`, `garbage_bytes`, `checksum_errors`), raises `RuntimeError` when no receiver found; CLI `mtrtk record --port auto --baud 115200 --seconds 60 --out FILE`. Fixture files used by Tasks 11 and 14.

- [ ] **Step 1: Write the failing recorder test**

`tests/unit/test_recorder.py`:
```python
from pathlib import Path

import pytest
from ubxtest import ubx_frame

from mtrtk.core import recorder


class FakeSerial:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def __enter__(self) -> "FakeSerial":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


def test_record_stream_writes_bytes_and_counts_frames(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pvt = ubx_frame(0x01, 0x07, b"\x00" * 92)
    chunks = [pvt[:40], pvt[40:] + b"\x00\x00", pvt]
    monkeypatch.setattr(recorder.serial, "Serial", lambda port, baud, timeout: FakeSerial(chunks))
    clock = iter([0.0, 0.0, 0.1, 0.2, 10.0])
    monkeypatch.setattr(recorder.time, "monotonic", lambda: next(clock))
    out = tmp_path / "sub" / "x.ubx"
    stats = recorder.record_stream("/dev/fake", 115200, 5.0, out)
    assert out.read_bytes() == pvt + b"\x00\x00" + pvt
    assert stats.bytes == len(pvt) * 2 + 2
    assert stats.frames["ubx"] == 2
    assert stats.garbage_bytes == 2


def test_record_stream_requires_a_receiver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recorder, "find_ublox_port", lambda: None)
    with pytest.raises(RuntimeError, match="no u-blox receiver"):
        recorder.record_stream("auto", 115200, 1.0, tmp_path / "x.ubx")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_recorder.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.core.recorder'`.

- [ ] **Step 3: Write `src/mtrtk/core/recorder.py`**

```python
"""Blocking recorder that dumps the raw receiver byte stream to a file (fixtures, replay)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import serial

from mtrtk.core.frames import Framer
from mtrtk.core.source import find_ublox_port


@dataclass
class RecordStats:
    bytes: int
    frames: dict[str, int]
    garbage_bytes: int
    checksum_errors: int


def record_stream(port: str, baud: int, seconds: float, out_path: Path) -> RecordStats:
    if port == "auto":
        found = find_ublox_port()
        if found is None:
            raise RuntimeError("no u-blox receiver found; pass --port explicitly")
        port = found
    framer = Framer()
    total = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + seconds
    with serial.Serial(port, baud, timeout=0.5) as ser, out_path.open("wb") as fh:
        while time.monotonic() < deadline:
            data = ser.read(4096)
            if not data:
                continue
            fh.write(data)
            total += len(data)
            framer.feed(data)
    return RecordStats(total, dict(framer.stats.frames), framer.stats.garbage_bytes, framer.stats.checksum_errors)
```

- [ ] **Step 4: Add the CLI command to `src/mtrtk/cli.py`**

Append after `main`:
```python
@main.command()
@click.option("--port", default="auto", show_default=True, help="Serial device, or 'auto' to find a u-blox receiver.")
@click.option("--baud", default=115200, show_default=True, type=int)
@click.option("--seconds", default=60.0, show_default=True, type=float)
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False, path_type=Path))
def record(port: str, baud: int, seconds: float, out_path: Path) -> None:
    """Record the raw receiver byte stream to a file (for fixtures and replay)."""
    from mtrtk.core.recorder import record_stream

    try:
        stats = record_stream(port, baud, seconds, out_path)
    except (RuntimeError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"wrote {stats.bytes} bytes to {out_path}: frames={stats.frames} "
        f"garbage={stats.garbage_bytes} checksum_errors={stats.checksum_errors}"
    )
```
Add `from pathlib import Path` to the imports at the top of `cli.py`.

- [ ] **Step 5: Run recorder tests**

Run: `uv run pytest tests/unit/test_recorder.py tests/unit/test_cli.py -q`
Expected: `3 passed`.

- [ ] **Step 6: Record fixtures from the attached receiver (hardware step)**

Run (receiver must be plugged in; nothing else may hold `/dev/ttyACM0`):
```bash
uv run mtrtk record --seconds 10 --out tests/fixtures/f9p_hpg113_raw_10s.ubx
uv run mtrtk record --seconds 60 --out tests/fixtures/f9p_hpg113_raw_60s.ubx
ls -la tests/fixtures/
```
Expected: two files (~40 KB and ~250 KB), output showing `frames={'ubx': N, ...}` with `garbage` under 100 bytes (a partial frame at the start is normal) and `checksum_errors=0`.

- [ ] **Step 7: Write the fixture sanity test**

`tests/unit/test_fixtures.py`:
```python
from collections import Counter
from pathlib import Path

from mtrtk.core.frames import Framer, Proto

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_10s_fixture_frames_cleanly() -> None:
    framer = Framer()
    frames = framer.feed((FIXTURES / "f9p_hpg113_raw_10s.ubx").read_bytes())
    idents = Counter(f.identity for f in frames if f.proto is Proto.UBX)
    assert idents["NAV-PVT"] >= 8
    assert idents["RXM-RAWX"] >= 8
    assert idents["RXM-SFRBX"] >= 8
    assert idents["NAV-SAT"] >= 8
    assert framer.stats.checksum_errors == 0
    assert framer.stats.garbage_bytes < 2000  # at most one partial frame at the start
```

Run: `uv run pytest tests/unit/test_fixtures.py -q` → `1 passed`.

- [ ] **Step 8: Lint and commit (fixtures included — `.gitignore` re-includes `tests/fixtures/*.ubx`)**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add src/mtrtk/core/recorder.py src/mtrtk/cli.py tests/unit/test_recorder.py tests/unit/test_fixtures.py tests/fixtures/
git commit -m "feat(cli): record command and HPG 1.13 raw stream fixtures

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Typed receiver state models

**Files:**
- Create: `src/mtrtk/core/state.py`, `tests/unit/test_state_models.py`

**Interfaces:**
- Produces (pydantic models, all fields optional-with-defaults): `Position`, `Accuracy`, `Dops`, `FixInfo`, `Velocity`, `TimeInfo`, `Signal`, `Satellite`, `SatSummary`, `Hardware`, `RfBlock`, `Spectrum`, `PortStats`, `SurveyIn`, `RtcmMsgStats`, `RtcmStats`, `Firmware`, `ReceiverState`; name tables `GNSS_NAMES`, `SIGNAL_NAMES`, `FIX_TYPE_NAMES`, `CARR_SOLN_NAMES`, `ANT_STATUS_NAMES`, `ANT_POWER_NAMES`, `JAMMING_STATE_NAMES`; helper `signal_name(gnss_id, sig_id) -> str`. Later tasks mutate `ReceiverState` sections in place and publish them.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_state_models.py`:
```python
from mtrtk.core.state import (
    FIX_TYPE_NAMES,
    GNSS_NAMES,
    ReceiverState,
    Satellite,
    Signal,
    signal_name,
)


def test_default_state_is_empty_but_valid() -> None:
    s = ReceiverState()
    assert s.connected is False
    assert s.position.lat is None
    assert s.fix.fix_type_name == "No fix"
    assert s.sats == []
    assert s.rtcm_out.total_bytes == 0
    assert s.model_dump()["dops"]["p"] is None


def test_name_tables() -> None:
    assert GNSS_NAMES[0] == "GPS" and GNSS_NAMES[6] == "GLONASS"
    assert FIX_TYPE_NAMES[5] == "Time only"
    assert signal_name(0, 3) == "L2CL"
    assert signal_name(2, 5) == "E5bI"
    assert signal_name(9, 9) == "sig9"


def test_satellite_key_and_json_roundtrip() -> None:
    sat = Satellite(gnss_id=0, gnss="GPS", sv_id=5, cno=40, used=True, signals=[Signal(sig_id=0, name="L1C/A", cno=40)])
    assert sat.key == (0, 5)
    assert Satellite.model_validate_json(sat.model_dump_json()) == sat
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_state_models.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.core.state'`.

- [ ] **Step 3: Write `src/mtrtk/core/state.py`**

```python
"""Typed live receiver state. The UI, sampler and alert rules consume these models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

GNSS_NAMES: dict[int, str] = {
    0: "GPS", 1: "SBAS", 2: "Galileo", 3: "BeiDou", 4: "IMES", 5: "QZSS", 6: "GLONASS", 7: "NavIC",
}
# (gnssId, sigId) -> signal name, per u-blox ZED-F9P interface description
SIGNAL_NAMES: dict[tuple[int, int], str] = {
    (0, 0): "L1C/A", (0, 3): "L2CL", (0, 4): "L2CM", (0, 6): "L5I", (0, 7): "L5Q",
    (1, 0): "L1C/A",
    (2, 0): "E1C", (2, 1): "E1B", (2, 3): "E5aI", (2, 4): "E5aQ", (2, 5): "E5bI", (2, 6): "E5bQ",
    (3, 0): "B1I D1", (3, 1): "B1I D2", (3, 2): "B2I D1", (3, 3): "B2I D2", (3, 5): "B1C", (3, 7): "B2a",
    (5, 0): "L1C/A", (5, 1): "L1S", (5, 4): "L2CM", (5, 5): "L2CL", (5, 8): "L5I", (5, 9): "L5Q",
    (6, 0): "L1OF", (6, 2): "L2OF",
    (7, 0): "L5A",
}
FIX_TYPE_NAMES: dict[int, str] = {0: "No fix", 1: "Dead reckoning", 2: "2D", 3: "3D", 4: "GNSS+DR", 5: "Time only"}
CARR_SOLN_NAMES: dict[int, str] = {0: "None", 1: "RTK float", 2: "RTK fixed"}
ANT_STATUS_NAMES: dict[int, str] = {0: "Init", 1: "Unknown", 2: "OK", 3: "Short", 4: "Open"}
ANT_POWER_NAMES: dict[int, str] = {0: "Off", 1: "On", 2: "Unknown"}
JAMMING_STATE_NAMES: dict[int, str] = {0: "Unknown", 1: "OK", 2: "Warning", 3: "Critical"}


def signal_name(gnss_id: int, sig_id: int) -> str:
    return SIGNAL_NAMES.get((gnss_id, sig_id), f"sig{sig_id}")


class Position(BaseModel):
    lat: float | None = None
    lon: float | None = None
    height_m: float | None = None  # above ellipsoid
    hmsl_m: float | None = None  # above mean sea level (receiver geoid model)
    ecef_x_m: float | None = None
    ecef_y_m: float | None = None
    ecef_z_m: float | None = None
    invalid_llh: bool = False


class Accuracy(BaseModel):
    h_acc_m: float | None = None
    v_acc_m: float | None = None
    p_acc_m: float | None = None  # 3D position accuracy (NAV-HPPOSECEF)
    t_acc_ns: int | None = None
    s_acc_mps: float | None = None
    head_acc_deg: float | None = None


class Dops(BaseModel):
    g: float | None = None
    p: float | None = None
    t: float | None = None
    v: float | None = None
    h: float | None = None
    n: float | None = None
    e: float | None = None


class FixInfo(BaseModel):
    fix_type: int = 0
    fix_type_name: str = "No fix"
    gnss_fix_ok: bool = False
    diff_soln: bool = False
    carr_soln: int = 0
    carr_soln_name: str = "None"
    num_sv: int = 0
    last_correction_age: int = 0  # NAV-PVT flags3 code (0 = n/a)
    psm_state: int = 0
    spoof_det_state: int = 0
    ttff_ms: int | None = None
    uptime_ms: int | None = None


class Velocity(BaseModel):
    vel_n_mps: float | None = None
    vel_e_mps: float | None = None
    vel_d_mps: float | None = None
    ground_speed_mps: float | None = None
    heading_motion_deg: float | None = None


class TimeInfo(BaseModel):
    utc: datetime | None = None
    itow_ms: int | None = None
    gps_week: int | None = None
    gps_tow_s: float | None = None
    leap_s: int | None = None
    valid_date: bool = False
    valid_time: bool = False
    fully_resolved: bool = False
    valid_utc: bool = False
    utc_standard: int | None = None
    t_acc_ns: int | None = None
    clk_bias_ns: int | None = None
    clk_drift_nsps: int | None = None
    f_acc_psps: int | None = None
    leap_source: int | None = None
    time_to_leap_event_s: int | None = None
    leap_change: int | None = None


class Signal(BaseModel):
    sig_id: int
    name: str
    freq_id: int = 0
    cno: int = 0
    pr_res_m: float = 0.0
    quality_ind: int = 0
    corr_source: int = 0
    iono_model: int = 0
    health: int = 0
    pr_used: bool = False
    cr_used: bool = False
    do_used: bool = False


class Satellite(BaseModel):
    gnss_id: int
    gnss: str
    sv_id: int
    cno: int = 0
    elev: int | None = None
    azim: int | None = None
    pr_res_m: float = 0.0
    quality_ind: int = 0
    used: bool = False
    health: int = 0
    diff_corr: bool = False
    smoothed: bool = False
    orbit_source: int = 0
    eph_avail: bool = False
    alm_avail: bool = False
    signals: list[Signal] = Field(default_factory=list)

    @property
    def key(self) -> tuple[int, int]:
        return (self.gnss_id, self.sv_id)


class SatSummary(BaseModel):
    tracked: int = 0
    used: int = 0
    per_gnss: dict[str, dict[str, int]] = Field(default_factory=dict)  # {"GPS": {"tracked": 12, "used": 9}}


class Hardware(BaseModel):
    ant_status: int = 0
    ant_status_name: str = "Init"
    ant_power: int = 2
    ant_power_name: str = "Unknown"
    noise_per_ms: int = 0
    agc_cnt: int = 0
    jam_ind: int = 0
    jamming_state: int = 0
    jamming_state_name: str = "Unknown"
    rtc_calib: bool = False
    safe_boot: bool = False
    xtal_absent: bool = False


class RfBlock(BaseModel):
    block_id: int
    jamming_state: int = 0
    jamming_state_name: str = "Unknown"
    ant_status: int = 0
    ant_status_name: str = "Init"
    ant_power: int = 2
    ant_power_name: str = "Unknown"
    post_status: int = 0
    noise_per_ms: int = 0
    agc_cnt: int = 0
    jam_ind: int = 0
    ofs_i: int = 0
    mag_i: int = 0
    ofs_q: int = 0
    mag_q: int = 0


class Spectrum(BaseModel):
    block_id: int
    span_hz: int
    res_hz: int
    center_hz: int
    pga_db: int
    bins: list[int]


class PortStats(BaseModel):
    port_id: int
    tx_pending: int = 0
    tx_bytes: int = 0
    tx_usage: int = 0
    tx_peak_usage: int = 0
    rx_pending: int = 0
    rx_bytes: int = 0
    rx_usage: int = 0
    rx_peak_usage: int = 0
    overrun_errs: int = 0
    skipped: int = 0


class SurveyIn(BaseModel):
    active: bool = False
    valid: bool = False
    dur_s: int = 0
    obs: int = 0
    mean_x_m: float | None = None
    mean_y_m: float | None = None
    mean_z_m: float | None = None
    mean_acc_m: float | None = None


class RtcmMsgStats(BaseModel):
    count: int = 0
    bytes: int = 0
    last_seen_mono: float | None = None


class RtcmStats(BaseModel):
    messages: dict[int, RtcmMsgStats] = Field(default_factory=dict)
    total_count: int = 0
    total_bytes: int = 0
    bytes_per_s: float = 0.0


class Firmware(BaseModel):
    sw_version: str = ""
    hw_version: str = ""
    fw_version: str = ""  # e.g. "HPG 1.13"
    protver: str = ""  # e.g. "27.12"
    module: str = ""  # e.g. "ZED-F9P"
    extensions: list[str] = Field(default_factory=list)


class ReceiverState(BaseModel):
    connected: bool = False
    source: str = ""
    position: Position = Field(default_factory=Position)
    accuracy: Accuracy = Field(default_factory=Accuracy)
    dops: Dops = Field(default_factory=Dops)
    fix: FixInfo = Field(default_factory=FixInfo)
    velocity: Velocity = Field(default_factory=Velocity)
    time: TimeInfo = Field(default_factory=TimeInfo)
    sats: list[Satellite] = Field(default_factory=list)
    sat_summary: SatSummary = Field(default_factory=SatSummary)
    hardware: Hardware | None = None
    rf: list[RfBlock] = Field(default_factory=list)
    spectrum: list[Spectrum] = Field(default_factory=list)
    ports: list[PortStats] = Field(default_factory=list)
    survey_in: SurveyIn = Field(default_factory=SurveyIn)
    rtcm_out: RtcmStats = Field(default_factory=RtcmStats)
    firmware: Firmware = Field(default_factory=Firmware)
    epoch_count: int = 0
    raw_epochs: int = 0  # RXM-RAWX frames seen (never parsed)
    last_epoch_mono: float | None = None
```

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_state_models.py -q` → `3 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

```bash
git add src/mtrtk/core/state.py tests/unit/test_state_models.py
git commit -m "feat(core): pydantic ReceiverState models and u-blox name tables

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: StateStore — navigation, accuracy, DOP and time handlers

**Files:**
- Create: `src/mtrtk/core/statestore.py`, `tests/unit/test_statestore_nav.py`

**Interfaces:**
- Consumes: `Frame`, `Proto`, `Bus.publish`, state models.
- Produces: `StateStore(bus: Bus | None = None)` with `.state: ReceiverState`, `.apply(frame: Frame) -> set[str]` returning the names of `ReceiverState` sections changed and publishing each as `state.<section>` (item = the section model). Handler registry `StateStore._handlers: dict[str, Callable]` keyed by UBX identity; Tasks 10–11 add handlers to this class.
- Unit conventions: pyubx2 pre-scales lat/lon to degrees and DOPs to floats; heights/accuracies arrive in **mm**, ECEF in **cm** (HP parts already merged by pyubx2), velocities in mm/s. StateStore converts everything to metres / m/s.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_statestore_nav.py`:
```python
from datetime import UTC, datetime

from pyubx2 import GET, UBXMessage

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.core.statestore import StateStore


def frame(msg: UBXMessage):
    return Framer().feed(msg.serialize())[0]


def test_nav_pvt_populates_position_fix_time_velocity() -> None:
    store = StateStore()
    msg = UBXMessage(
        "NAV", "NAV-PVT", GET,
        iTOW=492472000, year=2026, month=9, day=18, hour=16, min=47, second=34, nano=-250_000_000,
        validDate=1, validTime=1, fullyResolved=1, fixType=3, gnssFixOk=1, diffSoln=0, carrSoln=2,
        numSV=30, lon=90.2625502, lat=23.8373506, height=-36268, hMSL=13363, hAcc=1071, vAcc=1219,
        velN=100, velE=-200, velD=50, gSpeed=224, headMot=123.45, sAcc=90, headAcc=1.5, pDOP=1.09,
    )
    changed = store.apply(frame(msg))
    s = store.state
    assert changed == {"position", "accuracy", "dops", "fix", "velocity", "time"}
    assert s.position.lat == 23.8373506 and s.position.lon == 90.2625502
    assert s.position.height_m == -36.268 and s.position.hmsl_m == 13.363
    assert s.accuracy.h_acc_m == 1.071 and s.accuracy.v_acc_m == 1.219
    assert s.accuracy.s_acc_mps == 0.09 and s.accuracy.head_acc_deg == 1.5
    assert s.dops.p == 1.09
    assert s.fix.fix_type == 3 and s.fix.fix_type_name == "3D"
    assert s.fix.carr_soln == 2 and s.fix.carr_soln_name == "RTK fixed"
    assert s.fix.num_sv == 30 and s.fix.gnss_fix_ok is True
    assert s.velocity.vel_n_mps == 0.1 and s.velocity.vel_e_mps == -0.2 and s.velocity.ground_speed_mps == 0.224
    assert s.velocity.heading_motion_deg == 123.45
    assert s.time.utc == datetime(2026, 9, 18, 16, 47, 33, 750000, tzinfo=UTC)
    assert s.time.itow_ms == 492472000 and s.time.fully_resolved is True


def test_nav_pvt_without_valid_time_leaves_utc_none() -> None:
    store = StateStore()
    store.apply(frame(UBXMessage("NAV", "NAV-PVT", GET, iTOW=1, validDate=0, validTime=0, fixType=0)))
    assert store.state.time.utc is None
    assert store.state.fix.fix_type_name == "No fix"


def test_hpposllh_overrides_with_high_precision() -> None:
    store = StateStore()
    msg = UBXMessage("NAV", "NAV-HPPOSLLH", GET, iTOW=1, lon=90.26255021, lat=23.83735067, height=-36268.4, hMSL=13363.1, hAcc=12.3, vAcc=45.6)
    assert store.apply(frame(msg)) == {"position", "accuracy"}
    assert abs(store.state.position.lat - 23.83735067) < 1e-7  # pyubx2 merges the 1e-9 HP part
    assert abs(store.state.position.height_m - (-36.2684)) < 1e-3
    assert abs(store.state.accuracy.h_acc_m - 0.0123) < 1e-6


def test_hpposllh_invalid_flag_is_ignored() -> None:
    store = StateStore()
    store.apply(frame(UBXMessage("NAV", "NAV-HPPOSLLH", GET, iTOW=1, invalidLlh=1, lat=1.0, lon=2.0)))
    assert store.state.position.lat is None


def test_hpposecef_sets_ecef_metres_and_pacc() -> None:
    store = StateStore()
    msg = UBXMessage("NAV", "NAV-HPPOSECEF", GET, iTOW=1, ecefX=123456789, ecefY=-98765432, ecefZ=55555555, pAcc=250.0)
    assert store.apply(frame(msg)) == {"position", "accuracy"}
    assert store.state.position.ecef_x_m == 1234567.89
    assert store.state.position.ecef_y_m == -987654.32
    assert store.state.accuracy.p_acc_m == 0.25


def test_nav_dop() -> None:
    store = StateStore()
    msg = UBXMessage("NAV", "NAV-DOP", GET, iTOW=1, gDOP=1.5, pDOP=1.2, tDOP=0.8, vDOP=1.0, hDOP=0.7, nDOP=0.5, eDOP=0.4)
    assert store.apply(frame(msg)) == {"dops"}
    assert store.state.dops.model_dump() == {"g": 1.5, "p": 1.2, "t": 0.8, "v": 1.0, "h": 0.7, "n": 0.5, "e": 0.4}


def test_nav_status_clock_timegps_timels_timeutc() -> None:
    store = StateStore()
    store.apply(frame(UBXMessage("NAV", "NAV-STATUS", GET, iTOW=1, gpsFix=3, ttff=2500, msss=123456, spoofDetState=1)))
    store.apply(frame(UBXMessage("NAV", "NAV-CLOCK", GET, iTOW=1, clkB=1500, clkD=-7, tAcc=20, fAcc=300)))
    store.apply(frame(UBXMessage("NAV", "NAV-TIMEGPS", GET, iTOW=492472000, fTOW=-123456, week=2436, leapS=18, towValid=1, weekValid=1, leapSValid=1, tAcc=25)))
    store.apply(frame(UBXMessage("NAV", "NAV-TIMELS", GET, iTOW=1, srcOfCurrLs=2, currLs=18, srcOfLsChange=2, lsChange=0, timeToLsEvent=100000, validCurrLs=1, validTimeToLsEvent=1)))
    store.apply(frame(UBXMessage("NAV", "NAV-TIMEUTC", GET, iTOW=1, validUTC=1, utcStandard=3)))
    t = store.state.time
    assert store.state.fix.ttff_ms == 2500 and store.state.fix.uptime_ms == 123456 and store.state.fix.spoof_det_state == 1
    assert t.clk_bias_ns == 1500 and t.clk_drift_nsps == -7 and t.t_acc_ns == 25 and t.f_acc_psps == 300
    assert t.gps_week == 2436 and abs(t.gps_tow_s - 492471.999876544) < 1e-6 and t.leap_s == 18
    assert t.leap_source == 2 and t.time_to_leap_event_s == 100000 and t.leap_change == 0
    assert t.valid_utc is True and t.utc_standard == 3


def test_unknown_or_rawx_frames_change_nothing_but_count_epochs() -> None:
    store = StateStore()
    rawx = UBXMessage("RXM", "RXM-RAWX", GET, rcvTow=1.0, week=2436, leapS=18, numMeas=0)
    assert store.apply(frame(rawx)) == set()
    assert store.state.raw_epochs == 1
    assert store.apply(frame(UBXMessage("NAV", "NAV-VELECEF", GET, iTOW=1))) == set()


def test_sections_are_published_on_bus() -> None:
    bus = Bus()
    sub = bus.subscribe("state.*")
    store = StateStore(bus)
    store.apply(frame(UBXMessage("NAV", "NAV-DOP", GET, iTOW=1, pDOP=2.0)))
    topic, item = sub.queue.get_nowait()
    assert topic == "state.dops" and item.p == 2.0
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_statestore_nav.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.core.statestore'`.

- [ ] **Step 3: Write `src/mtrtk/core/statestore.py`**

```python
"""Applies parsed frames to a ReceiverState and publishes changed sections on the bus."""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame, Proto
from mtrtk.core.state import (
    CARR_SOLN_NAMES,
    FIX_TYPE_NAMES,
    Dops,
    ReceiverState,
    RtcmMsgStats,
)

log = logging.getLogger(__name__)

Handler = Callable[[Any], set[str]]

RTCM_RATE_WINDOW_S = 5.0


class StateStore:
    def __init__(self, bus: Bus | None = None) -> None:
        self.bus = bus
        self.state = ReceiverState()
        self._rtcm_window: deque[tuple[float, int]] = deque()
        self._handlers: dict[str, Handler] = {
            "NAV-PVT": self._nav_pvt,
            "NAV-HPPOSLLH": self._nav_hpposllh,
            "NAV-HPPOSECEF": self._nav_hpposecef,
            "NAV-DOP": self._nav_dop,
            "NAV-STATUS": self._nav_status,
            "NAV-CLOCK": self._nav_clock,
            "NAV-TIMEGPS": self._nav_timegps,
            "NAV-TIMELS": self._nav_timels,
            "NAV-TIMEUTC": self._nav_timeutc,
        }

    # ------------------------------------------------------------------ public
    def apply(self, frame: Frame) -> set[str]:
        if frame.proto is Proto.RTCM3:
            return self._rtcm(frame)
        if frame.proto is not Proto.UBX:
            return set()
        identity = frame.identity
        if identity == "RXM-RAWX":  # never parsed on the hot path
            self.state.raw_epochs += 1
            return set()
        handler = self._handlers.get(identity)
        if handler is None:
            return set()
        try:
            changed = handler(frame.parsed())
        except Exception:  # a malformed message must never kill the daemon
            log.exception("failed to apply %s", identity)
            return set()
        for section in changed:
            self._publish(f"state.{section}", getattr(self.state, section))
        return changed

    def _publish(self, topic: str, item: Any) -> None:
        if self.bus is not None:
            self.bus.publish(topic, item)

    # -------------------------------------------------------- nav / time handlers
    def _nav_pvt(self, m: Any) -> set[str]:
        s = self.state
        s.position.invalid_llh = bool(m.invalidLlh)
        if not m.invalidLlh:
            s.position.lat = m.lat
            s.position.lon = m.lon
            s.position.height_m = m.height / 1000
            s.position.hmsl_m = m.hMSL / 1000
        s.accuracy.h_acc_m = m.hAcc / 1000
        s.accuracy.v_acc_m = m.vAcc / 1000
        s.accuracy.t_acc_ns = m.tAcc
        s.accuracy.s_acc_mps = m.sAcc / 1000
        s.accuracy.head_acc_deg = m.headAcc
        s.dops.p = m.pDOP
        s.fix.fix_type = m.fixType
        s.fix.fix_type_name = FIX_TYPE_NAMES.get(m.fixType, f"fix{m.fixType}")
        s.fix.gnss_fix_ok = bool(m.gnssFixOk)
        s.fix.diff_soln = bool(m.diffSoln)
        s.fix.carr_soln = m.carrSoln
        s.fix.carr_soln_name = CARR_SOLN_NAMES.get(m.carrSoln, f"carr{m.carrSoln}")
        s.fix.num_sv = m.numSV
        s.fix.last_correction_age = m.lastCorrectionAge
        s.fix.psm_state = m.psmState
        s.velocity.vel_n_mps = m.velN / 1000
        s.velocity.vel_e_mps = m.velE / 1000
        s.velocity.vel_d_mps = m.velD / 1000
        s.velocity.ground_speed_mps = m.gSpeed / 1000
        s.velocity.heading_motion_deg = m.headMot
        s.time.itow_ms = m.iTOW
        s.time.valid_date = bool(m.validDate)
        s.time.valid_time = bool(m.validTime)
        s.time.fully_resolved = bool(m.fullyResolved)
        if m.validDate and m.validTime:
            base = datetime(m.year, m.month, m.day, m.hour, m.min, m.second, tzinfo=UTC)
            s.time.utc = base + timedelta(microseconds=round(m.nano / 1000))
        return {"position", "accuracy", "dops", "fix", "velocity", "time"}

    def _nav_hpposllh(self, m: Any) -> set[str]:
        if m.invalidLlh:
            return set()
        s = self.state
        s.position.lat = m.lat
        s.position.lon = m.lon
        s.position.height_m = m.height / 1000
        s.position.hmsl_m = m.hMSL / 1000
        s.accuracy.h_acc_m = m.hAcc / 1000
        s.accuracy.v_acc_m = m.vAcc / 1000
        return {"position", "accuracy"}

    def _nav_hpposecef(self, m: Any) -> set[str]:
        if m.invalidEcef:
            return set()
        s = self.state
        s.position.ecef_x_m = m.ecefX / 100
        s.position.ecef_y_m = m.ecefY / 100
        s.position.ecef_z_m = m.ecefZ / 100
        s.accuracy.p_acc_m = m.pAcc / 1000
        return {"position", "accuracy"}

    def _nav_dop(self, m: Any) -> set[str]:
        self.state.dops = Dops(g=m.gDOP, p=m.pDOP, t=m.tDOP, v=m.vDOP, h=m.hDOP, n=m.nDOP, e=m.eDOP)
        return {"dops"}

    def _nav_status(self, m: Any) -> set[str]:
        s = self.state.fix
        s.ttff_ms = m.ttff
        s.uptime_ms = m.msss
        s.spoof_det_state = m.spoofDetState
        return {"fix"}

    def _nav_clock(self, m: Any) -> set[str]:
        t = self.state.time
        t.clk_bias_ns = m.clkB
        t.clk_drift_nsps = m.clkD
        t.t_acc_ns = m.tAcc
        t.f_acc_psps = m.fAcc
        return {"time"}

    def _nav_timegps(self, m: Any) -> set[str]:
        t = self.state.time
        if m.weekValid:
            t.gps_week = m.week
        if m.towValid:
            t.gps_tow_s = m.iTOW / 1000 + m.fTOW * 1e-9
        if m.leapSValid:
            t.leap_s = m.leapS
        t.t_acc_ns = m.tAcc
        return {"time"}

    def _nav_timels(self, m: Any) -> set[str]:
        t = self.state.time
        if m.validCurrLs:
            t.leap_s = m.currLs
            t.leap_source = m.srcOfCurrLs
        if m.validTimeToLsEvent:
            t.time_to_leap_event_s = m.timeToLsEvent
            t.leap_change = m.lsChange
        return {"time"}

    def _nav_timeutc(self, m: Any) -> set[str]:
        t = self.state.time
        t.valid_utc = bool(m.validUTC)
        t.utc_standard = m.utcStandard
        return {"time"}

    # --------------------------------------------------------------- rtcm out
    def _rtcm(self, frame: Frame) -> set[str]:
        st = self.state.rtcm_out
        per = st.messages.setdefault(frame.rtcm_type, RtcmMsgStats())
        per.count += 1
        per.bytes += len(frame.raw)
        per.last_seen_mono = frame.t_mono
        st.total_count += 1
        st.total_bytes += len(frame.raw)
        now = time.monotonic()
        self._rtcm_window.append((now, len(frame.raw)))
        while self._rtcm_window and now - self._rtcm_window[0][0] > RTCM_RATE_WINDOW_S:
            self._rtcm_window.popleft()
        st.bytes_per_s = sum(n for _, n in self._rtcm_window) / RTCM_RATE_WINDOW_S
        self._publish("state.rtcm_out", st)
        return {"rtcm_out"}
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_statestore_nav.py -q`
Expected: `9 passed`. If `test_nav_pvt_populates_position_fix_time_velocity` fails on a scaled field (e.g. `headMot`), print `frame(msg).parsed()` and adjust the expected value to pyubx2's scaling — the store must expose degrees, metres and m/s.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add src/mtrtk/core/statestore.py tests/unit/test_statestore_nav.py
git commit -m "feat(core): StateStore with navigation, accuracy, DOP and time handlers

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: StateStore — satellites and signals (NAV-SAT + NAV-SIG merge)

**Files:**
- Modify: `src/mtrtk/core/statestore.py`
- Create: `tests/unit/test_statestore_sats.py`

**Interfaces:**
- Produces: handlers `"NAV-SAT"`, `"NAV-SIG"` registered in `StateStore._handlers`; `ReceiverState.sats` sorted by `(gnss_id, sv_id)` with per-satellite `signals` sorted by `sig_id`; `ReceiverState.sat_summary` (`tracked`, `used`, `per_gnss`). Satellites from both messages are merged per epoch (`iTOW`); a new `iTOW` from either message starts a fresh set.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_statestore_sats.py`:
```python
from pyubx2 import GET, UBXMessage

from mtrtk.core.frames import Framer
from mtrtk.core.statestore import StateStore


def frame(msg: UBXMessage):
    return Framer().feed(msg.serialize())[0]


def nav_sat(itow: int) -> UBXMessage:
    return UBXMessage(
        "NAV", "NAV-SAT", GET, iTOW=itow, version=1, numSvs=3,
        gnssId_01=0, svId_01=5, cno_01=40, elev_01=45, azim_01=120, prRes_01=0.5, qualityInd_01=7, svUsed_01=1, health_01=1, ephAvail_01=1, almAvail_01=1,
        gnssId_02=6, svId_02=3, cno_02=30, elev_02=20, azim_02=300, prRes_02=-1.2, qualityInd_02=4, svUsed_02=0, health_02=1,
        gnssId_03=0, svId_03=12, cno_03=0, elev_03=-91, azim_03=0, qualityInd_03=1, svUsed_03=0, health_03=0,
    )


def nav_sig(itow: int) -> UBXMessage:
    return UBXMessage(
        "NAV", "NAV-SIG", GET, iTOW=itow, version=0, numSigs=3,
        gnssId_01=0, svId_01=5, sigId_01=0, freqId_01=0, prRes_01=0.4, cno_01=40, qualityInd_01=7, corrSource_01=0, ionoModel_01=0, health_01=1, prUsed_01=1, crUsed_01=1, doUsed_01=1,
        gnssId_02=0, svId_02=5, sigId_02=3, freqId_02=0, prRes_02=0.2, cno_02=36, qualityInd_02=7, health_02=1, prUsed_02=1, crUsed_02=1, doUsed_02=0,
        gnssId_03=6, svId_03=3, sigId_03=0, freqId_03=5, cno_03=30, qualityInd_03=4, health_03=1,
    )


def test_nav_sat_builds_satellite_list_and_summary() -> None:
    store = StateStore()
    assert store.apply(frame(nav_sat(1000))) == {"sats", "sat_summary"}
    sats = store.state.sats
    assert [(s.gnss, s.sv_id) for s in sats] == [("GPS", 5), ("GPS", 12), ("GLONASS", 3)]
    g5 = sats[0]
    assert g5.cno == 40 and g5.elev == 45 and g5.azim == 120 and g5.used is True and g5.pr_res_m == 0.5
    assert g5.eph_avail is True and g5.alm_avail is True and g5.quality_ind == 7
    assert sats[1].elev is None  # -91 = unknown elevation
    summary = store.state.sat_summary
    assert summary.tracked == 3 and summary.used == 1
    assert summary.per_gnss == {"GPS": {"tracked": 2, "used": 1}, "GLONASS": {"tracked": 1, "used": 0}}


def test_nav_sig_merges_signals_into_satellites() -> None:
    store = StateStore()
    store.apply(frame(nav_sat(1000)))
    assert store.apply(frame(nav_sig(1000))) == {"sats", "sat_summary"}
    g5 = next(s for s in store.state.sats if s.key == (0, 5))
    assert [sig.name for sig in g5.signals] == ["L1C/A", "L2CL"]
    assert g5.signals[1].cno == 36 and g5.signals[1].pr_used is True and g5.signals[1].do_used is False
    r3 = next(s for s in store.state.sats if s.key == (6, 3))
    assert r3.signals[0].name == "L1OF" and r3.signals[0].freq_id == 5
    assert store.state.sat_summary.tracked == 3  # SIG did not add satellites


def test_nav_sig_before_nav_sat_still_merges() -> None:
    store = StateStore()
    store.apply(frame(nav_sig(1000)))
    assert len(store.state.sats) == 2  # created from SIG with unknown elevation
    store.apply(frame(nav_sat(1000)))
    g5 = next(s for s in store.state.sats if s.key == (0, 5))
    assert g5.elev == 45 and len(g5.signals) == 2


def test_new_itow_resets_satellite_set() -> None:
    store = StateStore()
    store.apply(frame(nav_sat(1000)))
    store.apply(frame(nav_sig(1000)))
    store.apply(frame(nav_sat(2000)))
    assert all(s.signals == [] for s in store.state.sats)
    store.apply(frame(nav_sig(2000)))
    assert any(s.signals for s in store.state.sats)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_statestore_sats.py -q`
Expected: 4 failures (`apply` returns `set()` because no handler is registered).

- [ ] **Step 3: Add satellite handling to `src/mtrtk/core/statestore.py`**

Extend the imports:
```python
from mtrtk.core.state import (
    CARR_SOLN_NAMES,
    FIX_TYPE_NAMES,
    GNSS_NAMES,
    Dops,
    ReceiverState,
    RtcmMsgStats,
    Satellite,
    SatSummary,
    Signal,
    signal_name,
)
```

In `__init__`, add before `self._handlers = {`:
```python
        self._sat_epoch: dict[tuple[int, int], Satellite] = {}
        self._sat_itow: int | None = None
```
and add to the handler dict:
```python
            "NAV-SAT": self._nav_sat,
            "NAV-SIG": self._nav_sig,
```

Add the handlers (after `_nav_timeutc`):
```python
    # ------------------------------------------------------------- satellites
    def _epoch_sats(self, itow: int) -> dict[tuple[int, int], Satellite]:
        if itow != self._sat_itow:
            self._sat_epoch = {}
            self._sat_itow = itow
        return self._sat_epoch

    def _sat_for(self, sats: dict[tuple[int, int], Satellite], gnss_id: int, sv_id: int) -> Satellite:
        key = (gnss_id, sv_id)
        sat = sats.get(key)
        if sat is None:
            sat = Satellite(gnss_id=gnss_id, gnss=GNSS_NAMES.get(gnss_id, f"gnss{gnss_id}"), sv_id=sv_id)
            sats[key] = sat
        return sat

    def _nav_sat(self, m: Any) -> set[str]:
        sats = self._epoch_sats(m.iTOW)
        for i in range(1, m.numSvs + 1):
            sfx = f"_{i:02d}"
            sat = self._sat_for(sats, getattr(m, "gnssId" + sfx), getattr(m, "svId" + sfx))
            sat.cno = getattr(m, "cno" + sfx)
            elev = getattr(m, "elev" + sfx)
            azim = getattr(m, "azim" + sfx)
            sat.elev = elev if -90 <= elev <= 90 else None
            sat.azim = azim if 0 <= azim <= 360 else None
            sat.pr_res_m = getattr(m, "prRes" + sfx)
            sat.quality_ind = getattr(m, "qualityInd" + sfx)
            sat.used = bool(getattr(m, "svUsed" + sfx))
            sat.health = getattr(m, "health" + sfx)
            sat.diff_corr = bool(getattr(m, "diffCorr" + sfx))
            sat.smoothed = bool(getattr(m, "smoothed" + sfx))
            sat.orbit_source = getattr(m, "orbitSource" + sfx)
            sat.eph_avail = bool(getattr(m, "ephAvail" + sfx))
            sat.alm_avail = bool(getattr(m, "almAvail" + sfx))
        self._finalize_sats()
        return {"sats", "sat_summary"}

    def _nav_sig(self, m: Any) -> set[str]:
        sats = self._epoch_sats(m.iTOW)
        for i in range(1, m.numSigs + 1):
            sfx = f"_{i:02d}"
            gnss_id = getattr(m, "gnssId" + sfx)
            sig_id = getattr(m, "sigId" + sfx)
            sat = self._sat_for(sats, gnss_id, getattr(m, "svId" + sfx))
            sig = Signal(
                sig_id=sig_id,
                name=signal_name(gnss_id, sig_id),
                freq_id=getattr(m, "freqId" + sfx),
                cno=getattr(m, "cno" + sfx),
                pr_res_m=getattr(m, "prRes" + sfx),
                quality_ind=getattr(m, "qualityInd" + sfx),
                corr_source=getattr(m, "corrSource" + sfx),
                iono_model=getattr(m, "ionoModel" + sfx),
                health=getattr(m, "health" + sfx),
                pr_used=bool(getattr(m, "prUsed" + sfx)),
                cr_used=bool(getattr(m, "crUsed" + sfx)),
                do_used=bool(getattr(m, "doUsed" + sfx)),
            )
            sat.signals = sorted([s for s in sat.signals if s.sig_id != sig_id] + [sig], key=lambda s: s.sig_id)
        self._finalize_sats()
        return {"sats", "sat_summary"}

    def _finalize_sats(self) -> None:
        sats = sorted(self._sat_epoch.values(), key=lambda s: (s.gnss_id, s.sv_id))
        per: dict[str, dict[str, int]] = {}
        for sat in sats:
            bucket = per.setdefault(sat.gnss, {"tracked": 0, "used": 0})
            bucket["tracked"] += 1
            bucket["used"] += int(sat.used)
        self.state.sats = sats
        self.state.sat_summary = SatSummary(tracked=len(sats), used=sum(int(s.used) for s in sats), per_gnss=per)
```

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_statestore_sats.py tests/unit/test_statestore_nav.py -q` → `13 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

```bash
git add src/mtrtk/core/statestore.py tests/unit/test_statestore_sats.py
git commit -m "feat(core): merge NAV-SAT and NAV-SIG into per-epoch satellite state

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: StateStore — hardware, RF, spectrum, comms, firmware, survey-in, epochs; fixture replay test

**Files:**
- Modify: `src/mtrtk/core/statestore.py`
- Create: `tests/unit/test_statestore_mon.py`, `tests/unit/test_statestore_fixture.py`

**Interfaces:**
- Produces: handlers `"MON-HW"`, `"MON-RF"`, `"MON-SPAN"`, `"MON-COMMS"`, `"MON-VER"`, `"NAV-SVIN"`, `"NAV-EOE"`. `NAV-EOE` increments `epoch_count`, sets `last_epoch_mono` and publishes the whole `ReceiverState` on topic `state.epoch`. `MON-VER` fills `Firmware` (`fw_version` from `FWVER=`, `protver` from `PROTVER=`, `module` from `MOD=`). NAV-SVIN converts cm + 0.1 mm HP parts into metres.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_statestore_mon.py`:
```python
import struct

from pyubx2 import GET, UBXMessage
from ubxtest import ubx_frame

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.core.statestore import StateStore


def frame(msg: UBXMessage):
    return Framer().feed(msg.serialize())[0]


def test_mon_hw() -> None:
    store = StateStore()
    msg = UBXMessage("MON", "MON-HW", GET, noisePerMS=90, agcCnt=3000, aStatus=2, aPower=1, jamInd=12, jammingState=1, rtcCalib=1, safeBoot=0, xtalAbsent=0)
    assert store.apply(frame(msg)) == {"hardware"}
    hw = store.state.hardware
    assert hw is not None
    assert hw.ant_status_name == "OK" and hw.ant_power_name == "On"
    assert hw.noise_per_ms == 90 and hw.agc_cnt == 3000 and hw.jam_ind == 12
    assert hw.jamming_state_name == "OK" and hw.rtc_calib is True


def test_mon_rf_two_blocks() -> None:
    store = StateStore()
    msg = UBXMessage(
        "MON", "MON-RF", GET, version=0, nBlocks=2,
        blockId_01=0, jammingState_01=1, antStatus_01=2, antPower_01=1, postStatus_01=0, noisePerMS_01=80, agcCnt_01=4000, jamInd_01=10, ofsI_01=1, magI_01=100, ofsQ_01=-1, magQ_01=99,
        blockId_02=1, jammingState_02=2, antStatus_02=2, antPower_02=1, postStatus_02=0, noisePerMS_02=70, agcCnt_02=5000, jamInd_02=5,
    )
    assert store.apply(frame(msg)) == {"rf"}
    rf = store.state.rf
    assert [b.block_id for b in rf] == [0, 1]
    assert rf[0].jamming_state_name == "OK" and rf[1].jamming_state_name == "Warning"
    assert rf[0].mag_i == 100 and rf[1].agc_cnt == 5000


def test_mon_span() -> None:
    store = StateStore()
    msg = UBXMessage("MON", "MON-SPAN", GET, version=0, numRfBlocks=1, span_01=100_000_000, res_01=390_625, center_01=1_580_000_000, pga_01=20)
    assert store.apply(frame(msg)) == {"spectrum"}
    sp = store.state.spectrum[0]
    assert sp.block_id == 0 and sp.span_hz == 100_000_000 and sp.center_hz == 1_580_000_000 and sp.pga_db == 20
    assert len(sp.bins) == 256


def test_mon_comms() -> None:
    store = StateStore()
    msg = UBXMessage(
        "MON", "MON-COMMS", GET, version=0, nPorts=1,
        portId_01=0x0300, txPending_01=10, txBytes_01=123456, txUsage_01=5, txPeakUsage_01=40,
        rxPending_01=0, rxBytes_01=999, rxUsage_01=1, rxPeakUsage_01=3, overrunErrs_01=2, skipped_01=7,
    )
    assert store.apply(frame(msg)) == {"ports"}
    p = store.state.ports[0]
    assert p.port_id == 0x0300 and p.tx_bytes == 123456 and p.tx_peak_usage == 40 and p.overrun_errs == 2 and p.skipped == 7


def mon_ver_raw() -> bytes:
    def cstr(text: str, size: int) -> bytes:
        return text.encode().ljust(size, b"\x00")

    payload = cstr("EXT CORE 1.00 (f10c36)", 30) + cstr("00190000", 10)
    for ext in ("ROM BASE 0x118B2060", "FWVER=HPG 1.13", "PROTVER=27.12", "MOD=ZED-F9P", "GPS;GLO;GAL;BDS", "SBAS;QZSS"):
        payload += cstr(ext, 30)
    return ubx_frame(0x0A, 0x04, payload)


def test_mon_ver_extracts_firmware_fields() -> None:
    store = StateStore()
    assert store.apply(Framer().feed(mon_ver_raw())[0]) == {"firmware"}
    fw = store.state.firmware
    assert fw.sw_version == "EXT CORE 1.00 (f10c36)" and fw.hw_version == "00190000"
    assert fw.fw_version == "HPG 1.13" and fw.protver == "27.12" and fw.module == "ZED-F9P"
    assert "GPS;GLO;GAL;BDS" in fw.extensions


def test_nav_svin_units() -> None:
    store = StateStore()
    msg = UBXMessage("NAV", "NAV-SVIN", GET, iTOW=1, dur=120, meanX=123456789, meanY=-98765432, meanZ=55555555, meanXHP=12, meanYHP=-34, meanZHP=0, meanAcc=15000, obs=118, valid=0, active=1)
    assert store.apply(frame(msg)) == {"survey_in"}
    sv = store.state.survey_in
    assert sv.active is True and sv.valid is False and sv.dur_s == 120 and sv.obs == 118
    assert abs(sv.mean_x_m - 1234567.8912) < 1e-9
    assert abs(sv.mean_y_m - (-987654.3234)) < 1e-9
    assert sv.mean_acc_m == 1.5


def test_nav_eoe_counts_epoch_and_publishes_snapshot() -> None:
    bus = Bus()
    sub = bus.subscribe("state.epoch")
    store = StateStore(bus)
    store.apply(frame(UBXMessage("NAV", "NAV-EOE", GET, iTOW=5000)))
    store.apply(frame(UBXMessage("NAV", "NAV-EOE", GET, iTOW=6000)))
    assert store.state.epoch_count == 2 and store.state.last_epoch_mono is not None
    assert sub.queue.qsize() == 2
    topic, snapshot = sub.queue.get_nowait()
    assert topic == "state.epoch" and snapshot is store.state


def test_rtcm_counters() -> None:
    from ubxtest import rtcm_frame

    store = StateStore()
    framer = Framer()
    for raw in (rtcm_frame(1005, b"\x00" * 16), rtcm_frame(1077, b"\x00" * 200), rtcm_frame(1077, b"\x00" * 200)):
        for f in framer.feed(raw):
            assert store.apply(f) == {"rtcm_out"}
    st = store.state.rtcm_out
    assert st.messages[1077].count == 2 and st.messages[1005].count == 1
    assert st.total_count == 3 and st.total_bytes == 22 + 2 * 206
    assert st.bytes_per_s > 0
```

`tests/unit/test_statestore_fixture.py`:
```python
from pathlib import Path

from mtrtk.core.frames import Framer
from mtrtk.core.statestore import StateStore

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_raw_10s.ubx"


def test_fixture_drives_state_to_a_3d_fix_with_satellites() -> None:
    store = StateStore()
    for frame in Framer().feed(FIXTURE.read_bytes()):
        store.apply(frame)
    s = store.state
    assert s.fix.fix_type == 3
    assert s.position.lat is not None and 23.0 < s.position.lat < 24.5  # recorded in Dhaka
    assert s.position.lon is not None and 90.0 < s.position.lon < 91.0
    assert s.time.utc is not None and s.time.utc.year == 2026
    assert s.sat_summary.tracked > 20 and s.sat_summary.used > 10
    assert any(sat.signals for sat in s.sats)
    assert s.raw_epochs >= 8
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_statestore_mon.py tests/unit/test_statestore_fixture.py -q`
Expected: the MON/SVIN/EOE tests fail (no handlers); `test_rtcm_counters` and the fixture test may already pass.

- [ ] **Step 3: Add the handlers to `src/mtrtk/core/statestore.py`**

Extend the `mtrtk.core.state` import with `ANT_POWER_NAMES, ANT_STATUS_NAMES, JAMMING_STATE_NAMES, Firmware, Hardware, PortStats, RfBlock, Spectrum, SurveyIn`. Register:
```python
            "NAV-SVIN": self._nav_svin,
            "NAV-EOE": self._nav_eoe,
            "MON-HW": self._mon_hw,
            "MON-RF": self._mon_rf,
            "MON-SPAN": self._mon_span,
            "MON-COMMS": self._mon_comms,
            "MON-VER": self._mon_ver,
```
Add module-level helper and handlers:
```python
def _cstr(value: object) -> str:
    if isinstance(value, bytes):
        return value.split(b"\x00", 1)[0].decode("ascii", "replace")
    return str(value).split("\x00", 1)[0]


    # ------------------------------------------------------ survey-in / epochs
    def _nav_svin(self, m: Any) -> set[str]:
        self.state.survey_in = SurveyIn(
            active=bool(m.active),
            valid=bool(m.valid),
            dur_s=m.dur,
            obs=m.obs,
            mean_x_m=m.meanX / 100 + m.meanXHP / 10000,
            mean_y_m=m.meanY / 100 + m.meanYHP / 10000,
            mean_z_m=m.meanZ / 100 + m.meanZHP / 10000,
            mean_acc_m=m.meanAcc / 10000,
        )
        return {"survey_in"}

    def _nav_eoe(self, m: Any) -> set[str]:
        self.state.epoch_count += 1
        self.state.last_epoch_mono = time.monotonic()
        self._publish("state.epoch", self.state)
        return set()

    # ---------------------------------------------------------------- monitor
    def _mon_hw(self, m: Any) -> set[str]:
        self.state.hardware = Hardware(
            ant_status=m.aStatus,
            ant_status_name=ANT_STATUS_NAMES.get(m.aStatus, str(m.aStatus)),
            ant_power=m.aPower,
            ant_power_name=ANT_POWER_NAMES.get(m.aPower, str(m.aPower)),
            noise_per_ms=m.noisePerMS,
            agc_cnt=m.agcCnt,
            jam_ind=m.jamInd,
            jamming_state=m.jammingState,
            jamming_state_name=JAMMING_STATE_NAMES.get(m.jammingState, str(m.jammingState)),
            rtc_calib=bool(m.rtcCalib),
            safe_boot=bool(m.safeBoot),
            xtal_absent=bool(m.xtalAbsent),
        )
        return {"hardware"}

    def _mon_rf(self, m: Any) -> set[str]:
        blocks: list[RfBlock] = []
        for i in range(1, m.nBlocks + 1):
            g = lambda name, i=i: getattr(m, f"{name}_{i:02d}")  # noqa: E731
            blocks.append(
                RfBlock(
                    block_id=g("blockId"),
                    jamming_state=g("jammingState"),
                    jamming_state_name=JAMMING_STATE_NAMES.get(g("jammingState"), str(g("jammingState"))),
                    ant_status=g("antStatus"),
                    ant_status_name=ANT_STATUS_NAMES.get(g("antStatus"), str(g("antStatus"))),
                    ant_power=g("antPower"),
                    ant_power_name=ANT_POWER_NAMES.get(g("antPower"), str(g("antPower"))),
                    post_status=g("postStatus"),
                    noise_per_ms=g("noisePerMS"),
                    agc_cnt=g("agcCnt"),
                    jam_ind=g("jamInd"),
                    ofs_i=g("ofsI"),
                    mag_i=g("magI"),
                    ofs_q=g("ofsQ"),
                    mag_q=g("magQ"),
                )
            )
        self.state.rf = blocks
        return {"rf"}

    def _mon_span(self, m: Any) -> set[str]:
        spectra: list[Spectrum] = []
        for i in range(1, m.numRfBlocks + 1):
            sfx = f"_{i:02d}"
            spectra.append(
                Spectrum(
                    block_id=i - 1,
                    span_hz=getattr(m, "span" + sfx),
                    res_hz=getattr(m, "res" + sfx),
                    center_hz=getattr(m, "center" + sfx),
                    pga_db=getattr(m, "pga" + sfx),
                    bins=list(getattr(m, "spectrum" + sfx)),
                )
            )
        self.state.spectrum = spectra
        return {"spectrum"}

    def _mon_comms(self, m: Any) -> set[str]:
        ports: list[PortStats] = []
        for i in range(1, m.nPorts + 1):
            sfx = f"_{i:02d}"
            ports.append(
                PortStats(
                    port_id=getattr(m, "portId" + sfx),
                    tx_pending=getattr(m, "txPending" + sfx),
                    tx_bytes=getattr(m, "txBytes" + sfx),
                    tx_usage=getattr(m, "txUsage" + sfx),
                    tx_peak_usage=getattr(m, "txPeakUsage" + sfx),
                    rx_pending=getattr(m, "rxPending" + sfx),
                    rx_bytes=getattr(m, "rxBytes" + sfx),
                    rx_usage=getattr(m, "rxUsage" + sfx),
                    rx_peak_usage=getattr(m, "rxPeakUsage" + sfx),
                    overrun_errs=getattr(m, "overrunErrs" + sfx),
                    skipped=getattr(m, "skipped" + sfx),
                )
            )
        self.state.ports = ports
        return {"ports"}

    def _mon_ver(self, m: Any) -> set[str]:
        extensions = [_cstr(v) for k, v in sorted(m.__dict__.items()) if k.startswith("extension_")]

        def tagged(prefix: str) -> str:
            return next((e[len(prefix) :] for e in extensions if e.startswith(prefix)), "")

        self.state.firmware = Firmware(
            sw_version=_cstr(m.swVersion),
            hw_version=_cstr(m.hwVersion),
            fw_version=tagged("FWVER="),
            protver=tagged("PROTVER="),
            module=tagged("MOD="),
            extensions=extensions,
        )
        return {"firmware"}
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass (≈ 60 tests). If `test_mon_ver_extracts_firmware_fields` fails because pyubx2 names extension attributes differently, print `frame.parsed().__dict__.keys()` and adapt the `startswith("extension_")` filter — the live poll in the planning session showed `extension_01 …`.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add src/mtrtk/core/statestore.py tests/unit/test_statestore_mon.py tests/unit/test_statestore_fixture.py
git commit -m "feat(core): hardware, RF, spectrum, comms, firmware, survey-in and epoch state

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: Receiver configuration profiles (CFG-VALSET key lists)

**Files:**
- Create: `src/mtrtk/core/ubx_config.py`, `tests/unit/test_ubx_config.py`

**Interfaces:**
- Consumes: `Settings` (`rtcm_msm`, `rtcm_1230_rate`, `rtcm_station_id`, `rover_nav_hz`, `dynmodel_code`).
- Produces: `CfgItems = list[tuple[str, int]]`; `Profile(name, core: CfgItems, signals: CfgItems, optional: dict[str, CfgItems], nav_hz: int)`; `base_profile(settings) -> Profile`; `rover_profile(settings) -> Profile`; `tmode_off()`, `tmode_survey_in(min_dur_s, acc_limit_m)`, `tmode_fixed_ecef(x_m, y_m, z_m, acc_m) -> CfgItems`; `chunked(items, n=64) -> list[CfgItems]`; `all_keys(profile) -> list[str]`; constants `LAYERS_RAM`, `LAYERS_ALL`, `MAX_KEYS_PER_VALSET`, `OPTIONAL_FEATURES`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_ubx_config.py`:
```python
import pytest
from pyubx2 import TXN_NONE, UBXMessage
from pyubx2.ubxtypes_configdb import UBX_CONFIG_DATABASE

from mtrtk.config import Settings
from mtrtk.core.ubx_config import (
    LAYERS_ALL,
    LAYERS_RAM,
    MAX_KEYS_PER_VALSET,
    all_keys,
    base_profile,
    chunked,
    rover_profile,
    tmode_fixed_ecef,
    tmode_off,
    tmode_survey_in,
)


@pytest.fixture
def base_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    return Settings(_env_file=None)


@pytest.fixture
def rover_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ROLE", "rover")
    monkeypatch.setenv("ROVER_NAV_HZ", "5")
    monkeypatch.setenv("ROVER_DYNMODEL", "airborne1g")
    return Settings(_env_file=None)


def test_every_key_exists_in_pyubx2_database(base_settings: Settings, rover_settings: Settings) -> None:
    for profile in (base_profile(base_settings), rover_profile(rover_settings)):
        missing = [k for k in all_keys(profile) if k not in UBX_CONFIG_DATABASE]
        assert missing == [], f"{profile.name}: unknown keys {missing}"
    for items in (tmode_off(), tmode_survey_in(300, 2.0), tmode_fixed_ecef(1.0, 2.0, 3.0, 0.01)):
        assert all(k in UBX_CONFIG_DATABASE for k, _ in items)


def test_every_chunk_serialises_to_a_valset(base_settings: Settings) -> None:
    profile = base_profile(base_settings)
    for group in (profile.core, profile.signals, *profile.optional.values()):
        for chunk in chunked(group):
            assert len(chunk) <= MAX_KEYS_PER_VALSET
            msg = UBXMessage.config_set(LAYERS_ALL, TXN_NONE, chunk)
            assert msg.identity == "CFG-VALSET"


def test_base_profile_contents(base_settings: Settings) -> None:
    core = dict(base_profile(base_settings).core)
    assert core["CFG_RATE_MEAS"] == 1000 and core["CFG_RATE_NAV"] == 1
    assert core["CFG_NAVSPG_DYNMODEL"] == 2
    assert core["CFG_USBOUTPROT_RTCM3X"] == 1 and core["CFG_USBINPROT_RTCM3X"] == 0
    assert core["CFG_USBOUTPROT_NMEA"] == 0
    assert core["CFG_MSGOUT_RTCM_3X_TYPE1005_USB"] == 1
    assert core["CFG_MSGOUT_RTCM_3X_TYPE1077_USB"] == 1 and core["CFG_MSGOUT_RTCM_3X_TYPE1074_USB"] == 0
    assert core["CFG_MSGOUT_RTCM_3X_TYPE1230_USB"] == 5
    assert core["CFG_MSGOUT_UBX_NAV_SVIN_USB"] == 1 and core["CFG_MSGOUT_UBX_RXM_RAWX_USB"] == 1
    assert core["CFG_MSGOUT_UBX_NAV_EOE_USB"] == 1
    assert "CFG_TMODE_MODE" not in core  # TMODE is applied separately from the site logic


def test_base_profile_msm4_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    monkeypatch.setenv("RTCM_MSM", "4")
    core = dict(base_profile(Settings(_env_file=None)).core)
    assert core["CFG_MSGOUT_RTCM_3X_TYPE1074_USB"] == 1 and core["CFG_MSGOUT_RTCM_3X_TYPE1077_USB"] == 0


def test_rover_profile_contents(rover_settings: Settings) -> None:
    profile = rover_profile(rover_settings)
    core = dict(profile.core)
    assert core["CFG_RATE_MEAS"] == 200 and profile.nav_hz == 5
    assert core["CFG_NAVSPG_DYNMODEL"] == 6
    assert core["CFG_USBINPROT_RTCM3X"] == 1 and core["CFG_USBOUTPROT_RTCM3X"] == 0
    assert core["CFG_MSGOUT_UBX_NAV_RELPOSNED_USB"] == 1 and core["CFG_MSGOUT_UBX_RXM_RTCM_USB"] == 1
    assert core["CFG_MSGOUT_UBX_TIM_TM2_USB"] == 1 and core["CFG_NAVHPG_DGNSSMODE"] == 3
    assert core["CFG_TMODE_MODE"] == 0


def test_signals_are_l1_l2_only_with_sbas_off(base_settings: Settings) -> None:
    signals = dict(base_profile(base_settings).signals)
    assert signals["CFG_SIGNAL_SBAS_ENA"] == 0
    assert signals["CFG_SIGNAL_GPS_L2C_ENA"] == 1 and signals["CFG_SIGNAL_GAL_E5B_ENA"] == 1
    assert not any("L5" in k or "E5A" in k for k in signals)


def test_optional_features(base_settings: Settings) -> None:
    optional = base_profile(base_settings).optional
    assert set(optional) == {"MON-SPAN", "MON-COMMS", "NAV-TIMELS"}


def test_tmode_helpers() -> None:
    assert tmode_off() == [("CFG_TMODE_MODE", 0)]
    assert tmode_survey_in(300, 2.0) == [
        ("CFG_TMODE_MODE", 1),
        ("CFG_TMODE_SVIN_MIN_DUR", 300),
        ("CFG_TMODE_SVIN_ACC_LIMIT", 20000),
    ]
    fixed = dict(tmode_fixed_ecef(1234.56789, -2.0, 0.00015, 0.0123))
    assert fixed["CFG_TMODE_MODE"] == 2 and fixed["CFG_TMODE_POS_TYPE"] == 0
    assert fixed["CFG_TMODE_ECEF_X"] == 123456 and fixed["CFG_TMODE_ECEF_X_HP"] == 79
    assert fixed["CFG_TMODE_ECEF_Y"] == -200 and fixed["CFG_TMODE_ECEF_Y_HP"] == 0
    assert fixed["CFG_TMODE_ECEF_Z"] == 0 and fixed["CFG_TMODE_ECEF_Z_HP"] == 2
    assert fixed["CFG_TMODE_FIXED_POS_ACC"] == 123


def test_chunked_splits_at_64() -> None:
    items = [(f"K{i}", i) for i in range(130)]
    parts = chunked(items)
    assert [len(p) for p in parts] == [64, 64, 2]
    assert LAYERS_RAM == 1 and LAYERS_ALL == 7
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_ubx_config.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.core.ubx_config'`.

- [ ] **Step 3: Write `src/mtrtk/core/ubx_config.py`**

```python
"""Receiver configuration profiles as CFG-VALSET key/value lists (pyubx2 key names)."""

from __future__ import annotations

from dataclasses import dataclass, field

from pyubx2 import SET_LAYER_BBR, SET_LAYER_FLASH, SET_LAYER_RAM
from pyubx2.ubxhelpers import val2sphp

from mtrtk.config import Settings

CfgItems = list[tuple[str, int]]

LAYERS_RAM = SET_LAYER_RAM
LAYERS_ALL = SET_LAYER_RAM | SET_LAYER_BBR | SET_LAYER_FLASH
MAX_KEYS_PER_VALSET = 64

RTCM_MSM7 = ("1077", "1087", "1097", "1127")
RTCM_MSM4 = ("1074", "1084", "1094", "1124")

# UBX messages both roles emit on USB (value = output every N navigation epochs)
COMMON_MSGOUT: dict[str, int] = {
    "NAV_PVT": 1,
    "NAV_SAT": 1,
    "NAV_SIG": 1,
    "NAV_DOP": 1,
    "NAV_STATUS": 1,
    "NAV_CLOCK": 1,
    "NAV_TIMEGPS": 1,
    "NAV_TIMEUTC": 1,
    "NAV_HPPOSLLH": 1,
    "NAV_HPPOSECEF": 1,
    "NAV_EOE": 1,
    "RXM_RAWX": 1,
    "RXM_SFRBX": 1,
    "MON_HW": 1,
    "MON_RF": 1,
}

# L1/L2 signal plan for HPG 1.13 (no L5 / E5a / B2a keys — those need newer firmware)
SIGNALS_L1_L2: CfgItems = [
    (f"CFG_SIGNAL_{name}_ENA", 1)
    for name in (
        "GPS", "GPS_L1CA", "GPS_L2C",
        "GLO", "GLO_L1", "GLO_L2",
        "GAL", "GAL_E1", "GAL_E5B",
        "BDS", "BDS_B1", "BDS_B2",
        "QZSS", "QZSS_L1CA", "QZSS_L2C",
    )
] + [("CFG_SIGNAL_SBAS_ENA", 0)]

# Features whose keys may be NAK'd on old firmware. Each is its own VALSET.
OPTIONAL_FEATURES: dict[str, CfgItems] = {
    "MON-SPAN": [("CFG_MSGOUT_UBX_MON_SPAN_USB", 5)],
    "MON-COMMS": [("CFG_MSGOUT_UBX_MON_COMMS_USB", 5)],
    "NAV-TIMELS": [("CFG_MSGOUT_UBX_NAV_TIMELS_USB", 10)],
}


@dataclass
class Profile:
    name: str
    core: CfgItems  # must be accepted (receiver_strict) — applied in ≤64-key chunks
    signals: CfgItems  # applied only when the readback differs (changing signals restarts the engine)
    optional: dict[str, CfgItems] = field(default_factory=dict)  # feature -> keys; NAK disables the feature
    nav_hz: int = 1


def _msgout(names: dict[str, int]) -> CfgItems:
    return [(f"CFG_MSGOUT_UBX_{name}_USB", rate) for name, rate in names.items()]


def _common_core(meas_ms: int, dynmodel: int, rtcm_out: bool) -> CfgItems:
    return [
        ("CFG_RATE_MEAS", meas_ms),
        ("CFG_RATE_NAV", 1),
        ("CFG_RATE_TIMEREF", 1),
        ("CFG_USBOUTPROT_UBX", 1),
        ("CFG_USBOUTPROT_NMEA", 0),
        ("CFG_USBOUTPROT_RTCM3X", 1 if rtcm_out else 0),
        ("CFG_USBINPROT_UBX", 1),
        ("CFG_USBINPROT_NMEA", 0),
        ("CFG_USBINPROT_RTCM3X", 0 if rtcm_out else 1),
        ("CFG_NAVSPG_DYNMODEL", dynmodel),
        ("CFG_NAVSPG_INFIL_MINELEV", 10),
        ("CFG_INFMSG_UBX_USB", 0),
        ("CFG_ITFM_ENABLE", 1),
        ("CFG_ITFM_ANTSETTING", 2),
    ] + _msgout(COMMON_MSGOUT)


def base_profile(settings: Settings) -> Profile:
    enabled = RTCM_MSM7 if settings.rtcm_msm == 7 else RTCM_MSM4
    disabled = RTCM_MSM4 if settings.rtcm_msm == 7 else RTCM_MSM7
    core = _common_core(1000, 2, rtcm_out=True) + _msgout({"NAV_SVIN": 1})
    core += [("CFG_MSGOUT_RTCM_3X_TYPE1005_USB", 1)]
    core += [(f"CFG_MSGOUT_RTCM_3X_TYPE{t}_USB", 1) for t in enabled]
    core += [(f"CFG_MSGOUT_RTCM_3X_TYPE{t}_USB", 0) for t in disabled]
    core += [
        ("CFG_MSGOUT_RTCM_3X_TYPE1230_USB", settings.rtcm_1230_rate),
        ("CFG_MSGOUT_RTCM_3X_TYPE4072_0_USB", 0),
        ("CFG_MSGOUT_RTCM_3X_TYPE4072_1_USB", 0),
        ("CFG_RTCM_DF003_OUT", settings.rtcm_station_id),
    ]
    return Profile("base", core, list(SIGNALS_L1_L2), dict(OPTIONAL_FEATURES), nav_hz=1)


def rover_profile(settings: Settings) -> Profile:
    meas_ms = round(1000 / settings.rover_nav_hz)
    core = _common_core(meas_ms, settings.dynmodel_code, rtcm_out=False)
    core += _msgout({"NAV_RELPOSNED": 1, "RXM_RTCM": 1, "TIM_TM2": 1, "NAV_VELNED": 1})
    core += [("CFG_NAVHPG_DGNSSMODE", 3), ("CFG_TMODE_MODE", 0)]
    return Profile("rover", core, list(SIGNALS_L1_L2), dict(OPTIONAL_FEATURES), nav_hz=settings.rover_nav_hz)


def tmode_off() -> CfgItems:
    return [("CFG_TMODE_MODE", 0)]


def tmode_survey_in(min_dur_s: int, acc_limit_m: float) -> CfgItems:
    return [
        ("CFG_TMODE_MODE", 1),
        ("CFG_TMODE_SVIN_MIN_DUR", int(min_dur_s)),
        ("CFG_TMODE_SVIN_ACC_LIMIT", int(round(acc_limit_m * 10_000))),  # 0.1 mm units
    ]


def tmode_fixed_ecef(x_m: float, y_m: float, z_m: float, acc_m: float) -> CfgItems:
    items: CfgItems = [("CFG_TMODE_MODE", 2), ("CFG_TMODE_POS_TYPE", 0)]
    for axis, value in (("X", x_m), ("Y", y_m), ("Z", z_m)):
        std_cm, hp_01mm = val2sphp(value, 0.01)
        items += [(f"CFG_TMODE_ECEF_{axis}", int(std_cm)), (f"CFG_TMODE_ECEF_{axis}_HP", int(hp_01mm))]
    items.append(("CFG_TMODE_FIXED_POS_ACC", int(round(acc_m * 10_000))))
    return items


def chunked(items: CfgItems, n: int = MAX_KEYS_PER_VALSET) -> list[CfgItems]:
    return [items[i : i + n] for i in range(0, len(items), n)]


def all_keys(profile: Profile) -> list[str]:
    keys = [k for k, _ in profile.core] + [k for k, _ in profile.signals]
    for items in profile.optional.values():
        keys += [k for k, _ in items]
    return keys
```

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_ubx_config.py -q` → `10 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

If `val2sphp(0.00015, 0.01)` yields `(0, 1)` instead of `(0, 2)` (rounding), change the test's `z_m` to `0.0002` and expectation to `2` — the receiver rounds to 0.1 mm either way.

```bash
git add src/mtrtk/core/ubx_config.py tests/unit/test_ubx_config.py
git commit -m "feat(core): base and rover CFG-VALSET profiles with TMODE helpers

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 13: UbxLink — request/response correlation over the bus

**Files:**
- Create: `src/mtrtk/core/link.py`
- Modify: `tests/ubxtest.py` (add `FakeReceiver`)
- Create: `tests/unit/test_link.py`

**Interfaces:**
- Consumes: `ByteSource.write`, `Bus.subscribe`, `Frame`, `CfgItems`.
- Produces: `class LinkTimeout(TimeoutError)`, `class LinkNak(RuntimeError)`; `UbxLink(source, bus)` with `await start()`, `await stop()`, `await write(data)`, `await poll(msg_class: str, msg_id: str, timeout=2.0) -> Frame` (returns the polled message, or an `ACK-NAK` frame when the receiver rejects the poll), `await valset(items, layers, timeout=2.0, retries=3) -> bool`, `await valget(keys, layer=POLL_LAYER_RAM, timeout=2.0) -> dict[str, int]` (raises `LinkNak` if rejected).
- Test helper: `FakeReceiver(bus)` — a `ByteSource` whose `write()` synchronously feeds canned responses back through a `Router`; configurable `valset_nak_keys`, `unsupported_polls`, `config` store, `mon_ver` bytes.

- [ ] **Step 1: Add `FakeReceiver` to `tests/ubxtest.py`**

Append:
```python
import struct

from pyubx2 import UBXReader
from pyubx2.ubxtypes_configdb import UBX_CONFIG_DATABASE

from mtrtk.core.bus import Bus
from mtrtk.core.router import Router

ACK_ACK = (0x05, 0x01)
ACK_NAK = (0x05, 0x00)
CFG_VALSET = (0x06, 0x8A)
CFG_VALGET = (0x06, 0x8B)

_SIZE = {"L001": 1, "U001": 1, "I001": 1, "E001": 1, "X001": 1, "U002": 2, "I002": 2, "X002": 2, "U004": 4, "I004": 4, "X004": 4, "R004": 4, "U008": 8, "I008": 8, "R008": 8}
_KEY_BY_ID = {kid: name for name, (kid, _) in UBX_CONFIG_DATABASE.items()}


def mon_ver_bytes(fw: str = "HPG 1.13", protver: str = "27.12") -> bytes:
    def cstr(text: str, size: int) -> bytes:
        return text.encode().ljust(size, b"\x00")

    payload = cstr("EXT CORE 1.00 (f10c36)", 30) + cstr("00190000", 10)
    for ext in ("ROM BASE 0x118B2060", f"FWVER={fw}", f"PROTVER={protver}", "MOD=ZED-F9P", "GPS;GLO;GAL;BDS", "SBAS;QZSS"):
        payload += cstr(ext, 30)
    return ubx_frame(0x0A, 0x04, payload)


class FakeReceiver:
    """ByteSource stand-in: answers VALSET/VALGET/polls immediately via the bus."""

    name = "fake"

    def __init__(self, bus: Bus) -> None:
        self.router = Router(bus)
        self.config: dict[str, int] = {}
        self.valset_nak_keys: set[str] = set()
        self.unsupported_polls: set[tuple[int, int]] = set()
        self.silent = False  # never answer (timeout tests)
        self.mon_ver = mon_ver_bytes()
        self.writes: list[bytes] = []
        self.valsets: list[tuple[int, dict[str, int]]] = []

    async def open(self) -> None:
        return None

    async def read(self) -> bytes:
        return b""

    async def close(self) -> None:
        return None

    def inject(self, data: bytes) -> None:
        self.router.feed(data)

    async def write(self, data: bytes) -> None:
        self.writes.append(data)
        if self.silent:
            return
        cls, mid = data[2], data[3]
        payload = data[6:-2]
        if (cls, mid) == CFG_VALSET:
            layers = payload[1]
            items = self._decode_items(payload[4:])
            self.valsets.append((layers, items))
            if set(items) & self.valset_nak_keys:
                self.inject(ubx_frame(*ACK_NAK, bytes(CFG_VALSET)))
            else:
                self.config.update(items)
                self.inject(ubx_frame(*ACK_ACK, bytes(CFG_VALSET)))
        elif (cls, mid) == CFG_VALGET:
            keys = [struct.unpack_from("<I", payload, i)[0] for i in range(4, len(payload), 4)]
            names = [_KEY_BY_ID[k] for k in keys]
            if any(n not in self.config for n in names):
                self.inject(ubx_frame(*ACK_NAK, bytes(CFG_VALGET)))
                return
            body = bytes([1, 0, 0, 0])
            for n in names:
                kid, typ = UBX_CONFIG_DATABASE[n]
                body += struct.pack("<I", kid) + int(self.config[n]).to_bytes(_SIZE[typ], "little", signed=typ.startswith("I"))
            self.inject(ubx_frame(*CFG_VALGET, body))
        elif len(payload) == 0:  # poll
            if (cls, mid) in self.unsupported_polls:
                self.inject(ubx_frame(*ACK_NAK, bytes((cls, mid))))
            elif (cls, mid) == (0x0A, 0x04):
                self.inject(self.mon_ver)
            else:
                self.inject(ubx_frame(cls, mid, b"\x00" * 8))  # generic empty-ish answer

    @staticmethod
    def _decode_items(data: bytes) -> dict[str, int]:
        items: dict[str, int] = {}
        i = 0
        while i + 4 <= len(data):
            kid = struct.unpack_from("<I", data, i)[0]
            name = _KEY_BY_ID[kid]
            size = _SIZE[UBX_CONFIG_DATABASE[name][1]]
            items[name] = int.from_bytes(data[i + 4 : i + 4 + size], "little", signed=UBX_CONFIG_DATABASE[name][1].startswith("I"))
            i += 4 + size
        return items
```

- [ ] **Step 2: Write the failing link tests**

`tests/unit/test_link.py`:
```python
import asyncio

import pytest
from ubxtest import FakeReceiver

from mtrtk.core.bus import Bus
from mtrtk.core.link import LinkNak, LinkTimeout, UbxLink
from mtrtk.core.ubx_config import LAYERS_ALL, LAYERS_RAM


@pytest.fixture
async def link_and_rx():
    bus = Bus()
    rx = FakeReceiver(bus)
    link = UbxLink(rx, bus)
    await link.start()
    try:
        yield link, rx
    finally:
        await link.stop()


async def test_valset_ack(link_and_rx) -> None:
    link, rx = link_and_rx
    assert await link.valset([("CFG_RATE_MEAS", 1000), ("CFG_RATE_NAV", 1)], LAYERS_ALL) is True
    assert rx.config == {"CFG_RATE_MEAS": 1000, "CFG_RATE_NAV": 1}
    assert rx.valsets[0][0] == LAYERS_ALL


async def test_valset_nak(link_and_rx) -> None:
    link, rx = link_and_rx
    rx.valset_nak_keys = {"CFG_RATE_NAV"}
    assert await link.valset([("CFG_RATE_MEAS", 1000), ("CFG_RATE_NAV", 1)], LAYERS_RAM) is False


async def test_valset_timeout_retries_then_raises(link_and_rx) -> None:
    link, rx = link_and_rx
    rx.silent = True
    with pytest.raises(LinkTimeout):
        await link.valset([("CFG_RATE_MEAS", 1000)], LAYERS_RAM, timeout=0.02, retries=2)
    assert len(rx.writes) == 2


async def test_valget_roundtrip(link_and_rx) -> None:
    link, rx = link_and_rx
    rx.config = {"CFG_RATE_MEAS": 200, "CFG_USBOUTPROT_RTCM3X": 1, "CFG_TMODE_ECEF_X_HP": -5}
    got = await link.valget(["CFG_RATE_MEAS", "CFG_USBOUTPROT_RTCM3X", "CFG_TMODE_ECEF_X_HP"])
    assert got == {"CFG_RATE_MEAS": 200, "CFG_USBOUTPROT_RTCM3X": 1, "CFG_TMODE_ECEF_X_HP": -5}


async def test_valget_unknown_key_raises_nak(link_and_rx) -> None:
    link, rx = link_and_rx
    with pytest.raises(LinkNak):
        await link.valget(["CFG_MSGOUT_UBX_MON_SPAN_USB"])


async def test_poll_returns_message(link_and_rx) -> None:
    link, rx = link_and_rx
    frame = await link.poll("MON", "MON-VER")
    assert frame.identity == "MON-VER"


async def test_poll_unsupported_returns_nak(link_and_rx) -> None:
    link, rx = link_and_rx
    rx.unsupported_polls = {(0x0A, 0x31)}
    frame = await link.poll("MON", "MON-SPAN", timeout=0.2)
    assert frame.identity == "ACK-NAK"


async def test_concurrent_requests_are_serialised(link_and_rx) -> None:
    link, rx = link_and_rx
    rx.config = {"CFG_RATE_MEAS": 1}
    results = await asyncio.gather(
        link.valset([("CFG_RATE_NAV", 1)], LAYERS_RAM),
        link.valget(["CFG_RATE_MEAS"]),
        link.poll("MON", "MON-VER"),
    )
    assert results[0] is True and results[1] == {"CFG_RATE_MEAS": 1} and results[2].identity == "MON-VER"
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/unit/test_link.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.core.link'`.

- [ ] **Step 4: Write `src/mtrtk/core/link.py`**

```python
"""Correlates UBX requests (polls, CFG-VALSET, CFG-VALGET) with their responses."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque

from pyubx2 import POLL, POLL_LAYER_RAM, TXN_NONE, UBXMessage

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame
from mtrtk.core.source import ByteSource
from mtrtk.core.ubx_config import CfgItems

log = logging.getLogger(__name__)

RESPONSE_TOPICS = (
    "ubx.ACK-ACK",
    "ubx.ACK-NAK",
    "ubx.CFG-VALGET",
    "ubx.MON-VER",
    "ubx.MON-SPAN",
    "ubx.MON-RF",
    "ubx.MON-COMMS",
    "ubx.MON-HW",
    "ubx.NAV-SIG",
    "ubx.NAV-TIMELS",
    "ubx.RXM-RTCM",
    "ubx.SEC-UNIQID",
)


class LinkTimeout(TimeoutError):
    """The receiver did not answer in time."""


class LinkNak(RuntimeError):
    """The receiver rejected the request."""


def _ack_key(cls: int, mid: int) -> str:
    return f"ack:{cls:02x}{mid:02x}"


class UbxLink:
    def __init__(self, source: ByteSource, bus: Bus) -> None:
        self._source = source
        self._sub = bus.subscribe(*RESPONSE_TOPICS, maxsize=500)
        self._waiters: dict[str, deque[asyncio.Future[Frame]]] = defaultdict(deque)
        self._write_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._dispatch(), name="ubxlink-dispatch")

    async def stop(self) -> None:
        self._sub.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for queue in self._waiters.values():
            for fut in queue:
                if not fut.done():
                    fut.set_exception(LinkTimeout("link stopped"))
        self._waiters.clear()

    async def _dispatch(self) -> None:
        async for _, frame in self._sub:
            for key in self._keys_for(frame):
                queue = self._waiters.get(key)
                if queue:
                    fut = queue.popleft()
                    if not fut.done():
                        fut.set_result(frame)
                    break

    @staticmethod
    def _keys_for(frame: Frame) -> tuple[str, ...]:
        ident = frame.identity
        if ident in ("ACK-ACK", "ACK-NAK"):
            payload = frame.payload
            return (_ack_key(payload[0], payload[1]),)
        return (ident,)

    async def write(self, data: bytes) -> None:
        async with self._write_lock:
            await self._source.write(data)

    async def _request(self, keys: list[str], data: bytes, timeout: float) -> Frame:
        loop = asyncio.get_running_loop()
        futures = [loop.create_future() for _ in keys]
        for key, fut in zip(keys, futures, strict=True):
            self._waiters[key].append(fut)
        try:
            await self.write(data)
            done, _ = await asyncio.wait(futures, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise LinkTimeout(f"no response for {keys} within {timeout}s")
            return next(iter(done)).result()
        finally:
            for key, fut in zip(keys, futures, strict=True):
                if not fut.done():
                    fut.cancel()
                    try:
                        self._waiters[key].remove(fut)
                    except ValueError:
                        pass

    async def poll(self, msg_class: str, msg_id: str, timeout: float = 2.0) -> Frame:
        raw = UBXMessage(msg_class, msg_id, POLL).serialize()
        return await self._request([msg_id, _ack_key(raw[2], raw[3])], raw, timeout)

    async def valset(self, items: CfgItems, layers: int, timeout: float = 2.0, retries: int = 3) -> bool:
        raw = UBXMessage.config_set(layers, TXN_NONE, items).serialize()
        for attempt in range(1, retries + 1):
            try:
                frame = await self._request([_ack_key(0x06, 0x8A)], raw, timeout)
            except LinkTimeout:
                log.warning("CFG-VALSET attempt %d/%d timed out", attempt, retries)
                continue
            return frame.identity == "ACK-ACK"
        raise LinkTimeout("no ACK for CFG-VALSET")

    async def valget(self, keys: list[str], layer: int = POLL_LAYER_RAM, timeout: float = 2.0) -> dict[str, int]:
        raw = UBXMessage.config_poll(layer, 0, keys).serialize()
        frame = await self._request(["CFG-VALGET", _ack_key(0x06, 0x8B)], raw, timeout)
        if frame.identity != "CFG-VALGET":
            raise LinkNak(f"CFG-VALGET rejected for {keys}")
        parsed = frame.parsed()
        return {k: int(v) for k, v in parsed.__dict__.items() if k.startswith("CFG_")}
```

- [ ] **Step 5: Run tests, lint, commit**

Run: `uv run pytest tests/unit/test_link.py -q` → `8 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.

If `test_poll_unsupported_returns_nak` fails because `MON-SPAN`'s id is not `0x31`, look it up: `python -c "import pyubx2; print({v:k for k,v in pyubx2.UBX_MSGIDS.items()}['MON-SPAN'].hex())"` and fix the test constant.

```bash
git add src/mtrtk/core/link.py tests/ubxtest.py tests/unit/test_link.py
git commit -m "feat(core): UbxLink request/response correlation with ACK matching and retries

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 14: ReceiverController — connect, probe, configure, verify, watchdog

**Files:**
- Create: `src/mtrtk/core/receiver.py`, `tests/unit/test_receiver.py`, `tests/hardware/__init__.py` (empty), `tests/hardware/test_live_f9p.py`

**Interfaces:**
- Consumes: `UbxLink`, `Profile`, `chunked`, `LAYERS_ALL`, `LAYERS_RAM`, `Router`, `Bus`, `ByteSource`, `Settings.receiver_strict`.
- Produces: `Capabilities(protver, fw_version, module, supported: set[str], unsupported: set[str])`; `class ReceiverError(RuntimeError)`, `class ProfileError(ReceiverError)`; `ReceiverController(bus, source_factory: Callable[[], ByteSource], profile: Profile | None, strict: bool = True, passive: bool = False, rx_timeout_s: float = 5.0)` with `await run(stop: asyncio.Event)`, `await probe(link) -> Capabilities`, `await configure(link, first: bool) -> Capabilities`, `await verify(link, profile) -> dict[str, tuple[int, int | None]]`, `await apply_items(items, layers=LAYERS_ALL) -> bool` (used by Phase 2 for TMODE), attributes `.capabilities`, `.connected`, `.link`. Bus topics: `receiver.connected` (source name), `receiver.disconnected` (reason str), `receiver.capabilities` (Capabilities), `receiver.error` (str).
- `run()` returns when the source ends (file replay EOF) or `stop` is set; serial errors trigger reconnect with exponential backoff (1 s → 30 s).

- [ ] **Step 1: Write the failing controller tests**

`tests/unit/test_receiver.py`:
```python
import asyncio

import pytest
from ubxtest import FakeReceiver, mon_ver_bytes

from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.link import UbxLink
from mtrtk.core.receiver import Capabilities, ProfileError, ReceiverController
from mtrtk.core.ubx_config import LAYERS_ALL, LAYERS_RAM, base_profile

MON_SPAN = (0x0A, 0x31)
MON_COMMS = (0x0A, 0x36)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    return Settings(_env_file=None)


@pytest.fixture
async def env(settings: Settings):
    bus = Bus()
    rx = FakeReceiver(bus)
    link = UbxLink(rx, bus)
    await link.start()
    ctrl = ReceiverController(bus, lambda: rx, base_profile(settings))
    try:
        yield ctrl, link, rx, bus
    finally:
        await link.stop()


async def test_probe_reads_firmware_and_optional_support(env) -> None:
    ctrl, link, rx, _ = env
    rx.unsupported_polls = {MON_SPAN}
    caps = await ctrl.probe(link)
    assert caps.protver == "27.12" and caps.fw_version == "HPG 1.13" and caps.module == "ZED-F9P"
    assert "MON-SPAN" in caps.unsupported
    assert "MON-COMMS" in caps.supported


async def test_configure_applies_core_signals_optional_and_verifies(env) -> None:
    ctrl, link, rx, bus = env
    rx.unsupported_polls = {MON_SPAN}
    caps_sub = bus.subscribe("receiver.capabilities")
    caps = await ctrl.configure(link, first=True)
    assert rx.config["CFG_RATE_MEAS"] == 1000 and rx.config["CFG_MSGOUT_RTCM_3X_TYPE1005_USB"] == 1
    assert rx.config["CFG_SIGNAL_SBAS_ENA"] == 0
    assert "CFG_MSGOUT_UBX_MON_SPAN_USB" not in rx.config  # skipped: probe said unsupported
    assert rx.config["CFG_MSGOUT_UBX_MON_COMMS_USB"] == 5
    assert all(layers == LAYERS_ALL for layers, _ in rx.valsets)
    assert caps_sub.queue.qsize() == 1
    assert isinstance(caps, Capabilities)


async def test_reconnect_uses_ram_layer_only(env) -> None:
    ctrl, link, rx, _ = env
    await ctrl.configure(link, first=False)
    assert all(layers == LAYERS_RAM for layers, _ in rx.valsets)


async def test_signals_not_rewritten_when_already_correct(env) -> None:
    ctrl, link, rx, _ = env
    await ctrl.configure(link, first=True)
    n_before = len(rx.valsets)
    await ctrl.configure(link, first=False)
    signal_writes = [items for _, items in rx.valsets[n_before:] if "CFG_SIGNAL_GPS_ENA" in items]
    assert signal_writes == []


async def test_optional_nak_disables_feature_not_startup(env) -> None:
    ctrl, link, rx, _ = env
    rx.valset_nak_keys = {"CFG_MSGOUT_UBX_NAV_TIMELS_USB"}
    caps = await ctrl.configure(link, first=True)
    assert "NAV-TIMELS" in caps.unsupported and "MON-SPAN" in caps.supported


async def test_core_nak_raises_profile_error_naming_the_key(env) -> None:
    ctrl, link, rx, _ = env
    rx.valset_nak_keys = {"CFG_ITFM_ANTSETTING"}
    with pytest.raises(ProfileError, match="CFG_ITFM_ANTSETTING"):
        await ctrl.configure(link, first=True)


async def test_core_nak_is_a_warning_when_not_strict(settings: Settings) -> None:
    bus = Bus()
    rx = FakeReceiver(bus)
    rx.valset_nak_keys = {"CFG_ITFM_ANTSETTING"}
    link = UbxLink(rx, bus)
    await link.start()
    ctrl = ReceiverController(bus, lambda: rx, base_profile(settings), strict=False)
    await ctrl.configure(link, first=True)
    assert rx.config["CFG_RATE_MEAS"] == 1000
    await link.stop()


async def test_verify_reports_mismatch(env) -> None:
    ctrl, link, rx, _ = env
    profile = base_profile(Settings(_env_file=None, ntrip_password="x"))
    await ctrl.configure(link, first=True)
    rx.config["CFG_RATE_MEAS"] = 250
    assert await ctrl.verify(link, profile) == {"CFG_RATE_MEAS": (1000, 250)}


async def test_run_ends_when_replay_source_ends(settings: Settings, tmp_path) -> None:
    from pyubx2 import GET, UBXMessage

    from mtrtk.core.source import FileReplaySource

    path = tmp_path / "r.ubx"
    path.write_bytes(UBXMessage("NAV", "NAV-PVT", GET, iTOW=1).serialize() + mon_ver_bytes())
    bus = Bus()
    pvt_sub = bus.subscribe("ubx.NAV-PVT")
    events = bus.subscribe("receiver.*")
    ctrl = ReceiverController(bus, lambda: FileReplaySource(path, speed=0), profile=None, passive=True)
    await asyncio.wait_for(ctrl.run(asyncio.Event()), 2.0)
    assert pvt_sub.queue.qsize() == 1
    topics = [t for t, _ in [events.queue.get_nowait() for _ in range(events.queue.qsize())]]
    assert topics[0] == "receiver.connected" and topics[-1] == "receiver.disconnected"


async def test_run_reconnects_after_open_failure(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from mtrtk.core import receiver as receiver_mod

    attempts = 0

    class Flaky:
        name = "flaky"

        async def open(self) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("device busy")

        async def read(self) -> bytes:
            return b""

        async def write(self, data: bytes) -> None:
            return None

        async def close(self) -> None:
            return None

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(receiver_mod.asyncio, "sleep", fake_sleep)
    ctrl = ReceiverController(Bus(), Flaky, profile=None, passive=True)
    await asyncio.wait_for(ctrl.run(asyncio.Event()), 2.0)
    assert attempts == 2 and sleeps[0] == 1.0
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_receiver.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mtrtk.core.receiver'`.

- [ ] **Step 3: Write `src/mtrtk/core/receiver.py`**

```python
"""Owns the receiver connection: open/reconnect, capability probe, profile apply and verify."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from mtrtk.core.bus import Bus
from mtrtk.core.link import LinkNak, LinkTimeout, UbxLink
from mtrtk.core.router import Router
from mtrtk.core.source import ByteSource
from mtrtk.core.statestore import StateStore
from mtrtk.core.ubx_config import LAYERS_ALL, LAYERS_RAM, CfgItems, Profile, chunked

log = logging.getLogger(__name__)

PROBE_POLLS: dict[str, tuple[str, str]] = {
    "MON-SPAN": ("MON", "MON-SPAN"),
    "MON-COMMS": ("MON", "MON-COMMS"),
    "NAV-TIMELS": ("NAV", "NAV-TIMELS"),
}
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 30.0


class ReceiverError(RuntimeError):
    pass


class ProfileError(ReceiverError):
    pass


class SourceEnded(Exception):
    """The byte source reached EOF (file replay finished)."""


@dataclass
class Capabilities:
    protver: str = ""
    fw_version: str = ""
    module: str = ""
    supported: set[str] = field(default_factory=set)
    unsupported: set[str] = field(default_factory=set)


class ReceiverController:
    def __init__(
        self,
        bus: Bus,
        source_factory: Callable[[], ByteSource],
        profile: Profile | None,
        strict: bool = True,
        passive: bool = False,
        rx_timeout_s: float = 5.0,
    ) -> None:
        self.bus = bus
        self._source_factory = source_factory
        self.profile = profile
        self.strict = strict
        self.passive = passive
        self.rx_timeout_s = rx_timeout_s
        self.capabilities = Capabilities()
        self.connected = False
        self.link: UbxLink | None = None
        self._first_apply = True
        self._last_rx = 0.0

    # ------------------------------------------------------------- lifecycle
    async def run(self, stop: asyncio.Event) -> None:
        backoff = BACKOFF_MIN_S
        while not stop.is_set():
            source = self._source_factory()
            try:
                await source.open()
            except (OSError, ValueError) as exc:  # serial errors derive from OSError/ValueError
                log.warning("cannot open %s: %s (retry in %.0fs)", source.name, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
                continue
            ended, failed = await self._session(source, stop)
            if ended or stop.is_set():
                return
            if failed:
                log.warning("reconnecting in %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
            else:
                backoff = BACKOFF_MIN_S

    async def _session(self, source: ByteSource, stop: asyncio.Event) -> tuple[bool, bool]:
        """Run one connection. Returns (ended_for_good, failed): EOF ends the run; failures reconnect with backoff."""
        router = Router(self.bus)
        link = UbxLink(source, self.bus)
        await link.start()
        self.link = link
        self.connected = True
        self._last_rx = time.monotonic()
        self.bus.publish("receiver.connected", source.name)
        reader = asyncio.create_task(self._read_loop(source, router), name="receiver-read")
        reason = "stopped"
        ended = failed = False
        try:
            if not self.passive and self.profile is not None:
                await self.configure(link, first=self._first_apply)
                self._first_apply = False
            await self._watchdog(reader, stop)
        except SourceEnded:
            reason, ended = "source ended", True
        except ReceiverError as exc:
            reason, failed = str(exc), True
            log.error("receiver error: %s", exc)
            self.bus.publish("receiver.error", reason)
        except (OSError, LinkTimeout) as exc:
            reason, failed = f"link failure: {exc}", True
            log.warning(reason)
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            await link.stop()
            await source.close()
            self.link = None
            self.connected = False
            self.bus.publish("receiver.disconnected", reason)
        return ended, failed

    async def _read_loop(self, source: ByteSource, router: Router) -> None:
        while True:
            data = await source.read()
            if not data:
                raise SourceEnded
            self._last_rx = time.monotonic()
            router.feed(data)

    async def _watchdog(self, reader: asyncio.Task[None], stop: asyncio.Event) -> None:
        stop_task = asyncio.create_task(stop.wait(), name="receiver-stop")
        try:
            while True:
                done, _ = await asyncio.wait({reader, stop_task}, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
                if stop_task in done:
                    return
                if reader in done:
                    exc = reader.exception()
                    if isinstance(exc, SourceEnded):
                        raise exc
                    raise ReceiverError(f"reader failed: {exc!r}")
                if time.monotonic() - self._last_rx > self.rx_timeout_s:
                    raise ReceiverError(f"no data from receiver for {self.rx_timeout_s:.0f}s")
        finally:
            stop_task.cancel()

    # ------------------------------------------------------------- configure
    async def probe(self, link: UbxLink) -> Capabilities:
        caps = Capabilities()
        frame = await link.poll("MON", "MON-VER")
        if frame.identity == "MON-VER":
            store = StateStore()
            store.apply(frame)
            fw = store.state.firmware
            caps.protver, caps.fw_version, caps.module = fw.protver, fw.fw_version, fw.module
        for feature, (cls, mid) in PROBE_POLLS.items():
            try:
                answer = await link.poll(cls, mid, timeout=1.0)
            except LinkTimeout:
                caps.unsupported.add(feature)
                continue
            (caps.supported if answer.identity == mid else caps.unsupported).add(feature)
        log.info("receiver %s fw=%s protver=%s unsupported=%s", caps.module, caps.fw_version, caps.protver, sorted(caps.unsupported))
        return caps

    async def configure(self, link: UbxLink, first: bool) -> Capabilities:
        if self.profile is None:
            raise ProfileError("no profile to apply")
        profile = self.profile
        layers = LAYERS_ALL if first else LAYERS_RAM
        caps = await self.probe(link)

        rejected = await self._apply_with_bisect(link, profile.core, layers)
        if rejected:
            message = f"receiver rejected core config keys: {rejected}"
            if self.strict:
                raise ProfileError(message)
            log.warning("%s (continuing: RECEIVER_STRICT=0)", message)

        current = await self._readback(link, [k for k, _ in profile.signals])
        if any(current.get(k) != v for k, v in profile.signals):
            log.info("signal configuration differs; rewriting (GNSS engine restarts)")
            if not await link.valset(profile.signals, layers):
                raise ProfileError("receiver rejected the signal configuration")

        for feature, items in profile.optional.items():
            if feature in caps.unsupported:
                continue
            try:
                ok = await link.valset(items, layers)
            except LinkTimeout:
                ok = False
            (caps.supported if ok else caps.unsupported).add(feature)
            if not ok:
                caps.supported.discard(feature)
                log.info("optional feature %s not accepted by this firmware", feature)

        mismatches = await self.verify(link, profile, skip=set(rejected))
        if mismatches:
            raise ProfileError(f"configuration verification failed: {mismatches}")
        self.capabilities = caps
        self.bus.publish("receiver.capabilities", caps)
        return caps

    async def apply_items(self, items: CfgItems, layers: int = LAYERS_ALL) -> bool:
        if self.link is None:
            raise ReceiverError("receiver not connected")
        return await self.link.valset(items, layers)

    async def verify(self, link: UbxLink, profile: Profile, skip: set[str] | None = None) -> dict[str, tuple[int, int | None]]:
        wanted = {k: v for k, v in [*profile.core, *profile.signals] if not skip or k not in skip}
        got = await self._readback(link, list(wanted))
        return {k: (v, got.get(k)) for k, v in wanted.items() if got.get(k) != v}

    async def _readback(self, link: UbxLink, keys: list[str]) -> dict[str, int]:
        result: dict[str, int] = {}
        for chunk in chunked([(k, 0) for k in keys]):
            names = [k for k, _ in chunk]
            try:
                result.update(await link.valget(names))
            except LinkNak:
                for name in names:  # isolate unknown keys one by one
                    try:
                        result.update(await link.valget([name]))
                    except LinkNak:
                        log.debug("VALGET rejected for %s", name)
        return result

    async def _apply_with_bisect(self, link: UbxLink, items: CfgItems, layers: int) -> list[str]:
        rejected: list[str] = []
        for chunk in chunked(items):
            if not await link.valset(chunk, layers):
                rejected += await self._bisect(link, chunk, layers)
        return rejected

    async def _bisect(self, link: UbxLink, items: CfgItems, layers: int) -> list[str]:
        if len(items) == 1:
            return [items[0][0]]
        mid = len(items) // 2
        rejected: list[str] = []
        for half in (items[:mid], items[mid:]):
            if not await link.valset(half, layers):
                rejected += await self._bisect(link, half, layers)
        return rejected
```

- [ ] **Step 4: Run controller tests**

Run: `uv run pytest tests/unit/test_receiver.py -q`
Expected: `10 passed`.

- [ ] **Step 5: Write the live hardware test**

`tests/hardware/test_live_f9p.py`:
```python
"""Live tests against the attached ZED-F9P. Run with: uv run pytest -m hardware -q
They reconfigure the receiver (RAM+BBR+Flash) with the base profile — approved by the owner."""

import asyncio
import os

import pytest

from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.link import UbxLink
from mtrtk.core.receiver import ReceiverController
from mtrtk.core.router import Router
from mtrtk.core.source import SerialSource, find_ublox_port
from mtrtk.core.ubx_config import base_profile

pytestmark = pytest.mark.hardware

PORT = os.environ.get("MTRTK_TEST_PORT") or find_ublox_port() or "/dev/ttyACM0"


@pytest.fixture
async def live():
    bus = Bus()
    source = SerialSource(PORT, 115200)
    await source.open()
    router = Router(bus)

    async def pump() -> None:
        while True:
            router.feed(await source.read())

    pump_task = asyncio.create_task(pump())
    link = UbxLink(source, bus)
    await link.start()
    try:
        yield bus, source, link
    finally:
        pump_task.cancel()
        await link.stop()
        await source.close()


async def test_probe_reports_f9p_firmware(live) -> None:
    bus, source, link = live
    ctrl = ReceiverController(bus, lambda: source, None)
    caps = await ctrl.probe(link)
    assert caps.module == "ZED-F9P"
    assert caps.protver.startswith("27.") or caps.protver.startswith("32.")
    print("capabilities:", caps)


async def test_base_profile_applies_and_verifies(live) -> None:
    bus, source, link = live
    settings = Settings(_env_file=None, ntrip_password="x")
    ctrl = ReceiverController(bus, lambda: source, base_profile(settings))
    caps = await ctrl.configure(link, first=True)
    print("unsupported optional features:", sorted(caps.unsupported))
    assert await ctrl.verify(link, base_profile(settings)) == {}
```

- [ ] **Step 6: Run the hardware test against the F9P (hardware step)**

Run: `uv run pytest -m hardware -q -s`
Expected: `2 passed`, printed capabilities show `fw_version='HPG 1.13'`. If `test_base_profile_applies_and_verifies` raises `ProfileError: receiver rejected core config keys: [...]`, move each listed key from `_common_core` / `base_profile` into `OPTIONAL_FEATURES` in `ubx_config.py` (one feature entry per key, named after the key), update `test_optional_features` accordingly, and re-run. Record what moved in the commit message — this is the intended way the 1.13 key set gets validated.

After the test, re-record a richer fixture (the receiver now emits RTCM, MON-*, NAV-EOE):
```bash
uv run mtrtk record --seconds 30 --out tests/fixtures/f9p_hpg113_base_30s.ubx
```
Expected output includes `'rtcm3': N` with N > 0. Add to `tests/unit/test_fixtures.py`:
```python
def test_base_fixture_contains_rtcm_and_eoe() -> None:
    framer = Framer()
    frames = framer.feed((FIXTURES / "f9p_hpg113_base_30s.ubx").read_bytes())
    idents = Counter(f.identity for f in frames)
    assert idents["NAV-EOE"] >= 25
    assert idents["1077"] >= 25 or idents["1074"] >= 25
    assert idents["1230"] >= 4
    assert framer.stats.checksum_errors == 0
```

- [ ] **Step 7: Lint and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/mtrtk/core/receiver.py tests/unit/test_receiver.py tests/hardware tests/fixtures/f9p_hpg113_base_30s.ubx tests/unit/test_fixtures.py src/mtrtk/core/ubx_config.py tests/unit/test_ubx_config.py
git commit -m "feat(core): ReceiverController with capability probe, bisecting profile apply and verify

Validated live against ZED-F9P HPG 1.13; adds a base-profile fixture with RTCM and NAV-EOE.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 15: Daemon supervisor, status printer and `run` / `base` / `rover` / `replay` / `doctor` commands

**Files:**
- Create: `src/mtrtk/daemon.py`, `src/mtrtk/core/exposure.py`, `src/mtrtk/doctor.py`, `tests/unit/test_daemon.py`, `tests/unit/test_doctor.py`
- Modify: `src/mtrtk/cli.py`

**Interfaces:**
- Consumes: `Settings`, `Bus`, `StateStore`, `ReceiverController`, `base_profile`, `rover_profile`, `SerialSource`, `FileReplaySource`, `find_ublox_port`.
- Produces: `Daemon(settings, source_factory=None, passive=None)` with `.bus`, `.store`, `.controller`, `.stop: asyncio.Event`, `await run()`; `StatusPrinter(bus, store, echo=print, interval_s=1.0)` (subscribes `state.epoch` and, when no NAV-EOE arrives, throttles on `state.position`); `tailscale_ipv4() -> str | None`; `doctor.run_checks(settings) -> list[Check]` where `Check(name, ok: bool | None, detail: str)` (`ok=None` = warning); CLI commands `run`, `base`, `rover`, `replay FILE [--speed] [--loop]`, `doctor`.

- [ ] **Step 1: Write the failing daemon test**

`tests/unit/test_daemon.py`:
```python
from pathlib import Path

import pytest
from click.testing import CliRunner

from mtrtk.cli import main
from mtrtk.config import Settings
from mtrtk.daemon import Daemon, StatusPrinter

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_raw_10s.ubx"


async def test_daemon_replays_file_to_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    settings = Settings(_env_file=None, mtrtk_source=f"file:{FIXTURE}", replay_speed=0)
    daemon = Daemon(settings)
    lines: list[str] = []
    printer = StatusPrinter(daemon.bus, daemon.store, echo=lines.append, interval_s=0)
    printer.start()
    await daemon.run()
    await printer.stop()
    assert daemon.store.state.fix.fix_type == 3
    assert daemon.store.state.sat_summary.tracked > 20
    assert any("3D" in line for line in lines)


def test_replay_command_runs_to_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    result = CliRunner().invoke(main, ["replay", str(FIXTURE), "--speed", "0"])
    assert result.exit_code == 0, result.output
    assert "3D" in result.output
    assert "replay finished" in result.output


def test_run_command_refuses_without_receiver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    monkeypatch.setenv("MTRTK_SOURCE", "auto")
    monkeypatch.setattr("mtrtk.daemon.find_ublox_port", lambda: None)
    result = CliRunner().invoke(main, ["run"])
    assert result.exit_code != 0
    assert "no u-blox receiver found" in result.output
```

`tests/unit/test_doctor.py`:
```python
import pytest

from mtrtk import doctor
from mtrtk.config import Settings


def test_run_checks_reports_each_area(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    monkeypatch.setattr(doctor, "find_ublox_port", lambda: None)
    monkeypatch.setattr(doctor, "tailscale_ipv4", lambda: "100.100.50.10")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    settings = Settings(_env_file=None, data_dir=tmp_path)
    checks = {c.name: c for c in doctor.run_checks(settings)}
    assert checks["receiver"].ok is False
    assert checks["tailscale"].ok is True and "100.100.50.10" in checks["tailscale"].detail
    assert checks["rtklib"].ok is None  # warning: only needed from Phase 5
    assert checks["data_dir"].ok is True
    assert checks["python"].ok is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_daemon.py tests/unit/test_doctor.py -q`
Expected: FAIL with `ModuleNotFoundError` for `mtrtk.daemon` / `mtrtk.doctor`.

- [ ] **Step 3: Write `src/mtrtk/core/exposure.py`**

```python
"""Network exposure helpers: which local IP the caster and web UI should bind to."""

from __future__ import annotations

import socket

import psutil

TAILSCALE_IFACE = "tailscale0"


def tailscale_ipv4() -> str | None:
    """IPv4 address of the tailscale0 interface, or None when Tailscale is not up."""
    addrs = psutil.net_if_addrs().get(TAILSCALE_IFACE, [])
    for addr in addrs:
        if addr.family == socket.AF_INET:
            return str(addr.address)
    return None
```

- [ ] **Step 4: Write `src/mtrtk/daemon.py`**

```python
"""Process supervisor: wires source -> router -> bus -> state per role, and prints status."""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Callable

from mtrtk.config import Role, Settings
from mtrtk.core.bus import Bus
from mtrtk.core.receiver import ReceiverController
from mtrtk.core.router import TOPIC_RAW_RTCM, TOPIC_RAW_UBX
from mtrtk.core.source import ByteSource, FileReplaySource, SerialSource, find_ublox_port
from mtrtk.core.statestore import StateStore
from mtrtk.core.ubx_config import base_profile, rover_profile

log = logging.getLogger(__name__)


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
        return (
            f"{utc} {s.fix.fix_type_name:<9} {s.fix.carr_soln_name:<9} "
            f"sats {s.sat_summary.used}/{s.sat_summary.tracked} "
            f"lat {lat} lon {lon} h {height} hAcc {hacc} rtcm {s.rtcm_out.bytes_per_s:.0f} B/s"
        )


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
        self._raw_sub = self.bus.subscribe(TOPIC_RAW_UBX, TOPIC_RAW_RTCM, maxsize=5000)
        passive = settings.source_is_file if passive is None else passive
        profile = base_profile(settings) if settings.role is Role.BASE else rover_profile(settings)
        self.controller = ReceiverController(
            self.bus,
            source_factory or self._default_source_factory(),
            profile=None if passive else profile,
            strict=settings.receiver_strict,
            passive=passive,
        )

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

    async def _state_loop(self) -> None:
        async for _, frame in self._raw_sub:
            self.store.apply(frame)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except (NotImplementedError, RuntimeError):  # not the main thread / not supported
                pass
        state_task = asyncio.create_task(self._state_loop(), name="state-loop")
        controller_task = asyncio.create_task(self.controller.run(self.stop), name="receiver")
        try:
            await controller_task  # returns on EOF (replay) or when stop is set
        finally:
            self.stop.set()
            self._raw_sub.close()  # state loop drains what is queued, then exits
            await asyncio.gather(state_task, return_exceptions=True)
```

- [ ] **Step 5: Write `src/mtrtk/doctor.py`**

```python
"""Environment checks for `mtrtk doctor`."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass

from mtrtk.config import Settings
from mtrtk.core.exposure import tailscale_ipv4
from mtrtk.core.source import find_ublox_port


@dataclass
class Check:
    name: str
    ok: bool | None  # None = warning
    detail: str


def run_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    v = sys.version_info
    checks.append(Check("python", v >= (3, 12), f"{v.major}.{v.minor}.{v.micro}"))

    if settings.source_is_file:
        path = settings.source_path
        checks.append(Check("receiver", path.exists(), f"replay file {path}"))
    else:
        port = settings.mtrtk_source if settings.mtrtk_source != "auto" else find_ublox_port()
        if port is None:
            checks.append(Check("receiver", False, "no u-blox receiver found (check USB cable, /dev/serial/by-id)"))
        else:
            readable = os.access(port, os.R_OK | os.W_OK)
            checks.append(Check("receiver", readable, f"{port} ({'read/write ok' if readable else 'no permission: add user to dialout'})"))

    ts_ip = tailscale_ipv4()
    needs_ts = "tailscale" in (settings.ntrip_bind, settings.web_bind)
    checks.append(Check("tailscale", (ts_ip is not None) if needs_ts else None, ts_ip or "tailscale0 has no IPv4 (is tailscaled running?)"))

    missing = [tool for tool in ("convbin", "rnx2rtkp") if shutil.which(tool) is None]
    checks.append(Check("rtklib", None if missing else True, "missing: " + ", ".join(missing) if missing else "convbin, rnx2rtkp found"))

    data_dir = settings.data_dir
    if data_dir.exists():
        usage = shutil.disk_usage(data_dir)
        free_gb = usage.free / 1e9
        checks.append(Check("data_dir", free_gb >= settings.min_free_gb, f"{data_dir}: {free_gb:.1f} GB free (min {settings.min_free_gb})"))
    else:
        checks.append(Check("data_dir", None, f"{data_dir} does not exist yet (created on first run)"))
    return checks
```

- [ ] **Step 6: Extend `src/mtrtk/cli.py`**

Replace the file's imports and add commands so the file reads:
```python
"""mtrtk command line interface."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

from mtrtk import __version__


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="mtrtk")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
def main(verbose: bool) -> None:
    """mtrtk - GNSS RTK/PPK/PPP toolkit for ZED-F9P base stations and rovers."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _load_settings(**overrides: object) -> "Settings":
    from pydantic import ValidationError

    from mtrtk.config import Settings

    try:
        return Settings(**{k: v for k, v in overrides.items() if v is not None})  # type: ignore[arg-type]
    except ValidationError as exc:
        raise click.ClickException(f"invalid configuration:\n{exc}") from exc


def _run_daemon(settings: "Settings") -> None:
    from mtrtk.daemon import Daemon, StatusPrinter

    async def go() -> None:
        try:
            daemon = Daemon(settings)
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        printer = StatusPrinter(daemon.bus, daemon.store, echo=click.echo)
        printer.start()
        try:
            await daemon.run()
        finally:
            await printer.stop()

    asyncio.run(go())


@main.command()
def run() -> None:
    """Run the daemon in the role given by ROLE (.env)."""
    _run_daemon(_load_settings())


@main.command()
def base() -> None:
    """Run as a base station (ROLE=base)."""
    _run_daemon(_load_settings(role="base"))


@main.command()
def rover() -> None:
    """Run as a rover (ROLE=rover)."""
    _run_daemon(_load_settings(role="rover"))


@main.command()
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--speed", default=1.0, show_default=True, type=float, help="Pace multiplier; 0 = as fast as possible.")
@click.option("--loop", is_flag=True, help="Restart the file when it ends.")
def replay(file: Path, speed: float, loop: bool) -> None:
    """Replay a recorded .ubx stream as if it were a live receiver (no configuration is sent)."""
    settings = _load_settings(mtrtk_source=f"file:{file}", replay_speed=speed, replay_loop=loop, ntrip_password="")
    _run_daemon(settings)
    click.echo("replay finished")


@main.command()
def doctor() -> None:
    """Check receiver access, Tailscale, RTKLIB and disk."""
    from mtrtk.doctor import run_checks

    failed = False
    for check in run_checks(_load_settings(ntrip_password="")):
        mark = {True: "OK  ", False: "FAIL", None: "WARN"}[check.ok]
        failed |= check.ok is False
        click.echo(f"[{mark}] {check.name:<10} {check.detail}")
    if failed:
        raise SystemExit(1)
```
Keep the existing `record` command below these (it already imports `Path`). Add `from typing import TYPE_CHECKING` and `if TYPE_CHECKING: from mtrtk.config import Settings` at the top so the string annotations resolve for mypy.

- [ ] **Step 7: Run tests**

Run: `uv run pytest -q`
Expected: all pass. `test_replay_command_runs_to_eof` needs the `state.position` fallback in `StatusPrinter` because the raw fixture has no NAV-EOE.

- [ ] **Step 8: Manual milestone check on the live receiver**

Run (Ctrl-C to stop after a few lines):
```bash
NTRIP_PASSWORD=x uv run mtrtk -v base
```
Expected: log lines `receiver ZED-F9P fw=HPG 1.13 ...`, then one status line per second such as
`16:47:34 3D        None      sats 27/50 lat 23.8373506 lon 90.2625502 h -36.27 hAcc 1.07 rtcm 1900 B/s`.
Also: `uv run mtrtk doctor` prints OK for python/receiver/tailscale.

- [ ] **Step 9: Lint and commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add src/mtrtk/daemon.py src/mtrtk/doctor.py src/mtrtk/core/exposure.py src/mtrtk/cli.py tests/unit/test_daemon.py tests/unit/test_doctor.py
git commit -m "feat: daemon supervisor, status printer, run/base/rover/replay/doctor commands

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 16: Frontend skeleton (Vite + React + TypeScript + Tailwind v4)

**Files:**
- Create: `web/package.json`, `web/vite.config.ts`, `web/tsconfig.json`, `web/tsconfig.app.json`, `web/tsconfig.node.json`, `web/index.html`, `web/src/main.tsx`, `web/src/App.tsx`, `web/src/index.css`, `web/src/lib/utils.ts`, `web/components.json`, `web/.gitignore`

**Interfaces:**
- Produces: `pnpm --dir web build` emits `web/dist/` (copied into the image by Task 17); path alias `@/` → `web/src/`; Tailwind v4 via `@tailwindcss/vite`; `components.json` ready for `pnpm dlx shadcn@latest add <component>` in Phase 4. Dev server proxies `/api` and `/ws` to `http://127.0.0.1:8080` (Phase 3 backend).

- [ ] **Step 1: Write the files**

`web/package.json`:
```json
{
  "name": "mtrtk-web",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "preview": "vite preview",
    "lint": "tsc -b --noEmit"
  },
  "dependencies": {
    "class-variance-authority": "^0.7.1",
    "clsx": "^2.1.1",
    "react": "^19.1.0",
    "react-dom": "^19.1.0",
    "tailwind-merge": "^3.3.0"
  },
  "devDependencies": {
    "@tailwindcss/vite": "^4.1.0",
    "@types/node": "^24.0.0",
    "@types/react": "^19.1.0",
    "@types/react-dom": "^19.1.0",
    "@vitejs/plugin-react": "^4.5.0",
    "tailwindcss": "^4.1.0",
    "typescript": "^5.8.0",
    "vite": "^6.3.0"
  }
}
```

`web/vite.config.ts`:
```ts
import path from "node:path";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8080",
      "/ws": { target: "ws://127.0.0.1:8080", ws: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
});
```

`web/tsconfig.json`:
```json
{
  "files": [],
  "references": [{ "path": "./tsconfig.app.json" }, { "path": "./tsconfig.node.json" }],
  "compilerOptions": { "baseUrl": ".", "paths": { "@/*": ["./src/*"] } }
}
```

`web/tsconfig.app.json`:
```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true,
    "skipLibCheck": true,
    "isolatedModules": true,
    "moduleDetection": "force",
    "noEmit": true,
    "baseUrl": ".",
    "paths": { "@/*": ["./src/*"] }
  },
  "include": ["src"]
}
```

`web/tsconfig.node.json`:
```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2023"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "types": ["node"],
    "strict": true,
    "skipLibCheck": true,
    "noEmit": true
  },
  "include": ["vite.config.ts"]
}
```

`web/index.html`:
```html
<!doctype html>
<html lang="en" class="dark">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>mtrtk</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

`web/src/main.tsx`:
```tsx
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
```

`web/src/App.tsx`:
```tsx
export default function App() {
  return (
    <main className="min-h-screen bg-background text-foreground flex items-center justify-center">
      <div className="text-center space-y-2">
        <h1 className="text-3xl font-semibold tracking-tight">mtrtk</h1>
        <p className="text-muted-foreground font-mono text-sm">web UI arrives in Phase 4 — backend not connected</p>
      </div>
    </main>
  );
}
```

`web/src/index.css`:
```css
@import "tailwindcss";

@theme {
  --color-background: oklch(0.13 0.02 250);
  --color-foreground: oklch(0.95 0.01 250);
  --color-muted-foreground: oklch(0.65 0.02 250);
}

html, body, #root { height: 100%; }
```

`web/src/lib/utils.ts`:
```ts
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
```

`web/components.json`:
```json
{
  "$schema": "https://ui.shadcn.com/schema.json",
  "style": "new-york",
  "rsc": false,
  "tsx": true,
  "tailwind": { "config": "", "css": "src/index.css", "baseColor": "zinc", "cssVariables": true },
  "aliases": { "components": "@/components", "utils": "@/lib/utils", "ui": "@/components/ui", "lib": "@/lib", "hooks": "@/hooks" },
  "iconLibrary": "lucide"
}
```

`web/.gitignore`:
```
node_modules/
dist/
```

- [ ] **Step 2: Install and build**

Run: `cd /home/nekosaif/github/mtrtk/web && pnpm install && pnpm build && ls dist/`
Expected: `pnpm-lock.yaml` created; `dist/index.html` and `dist/assets/*.js|*.css` present; no TypeScript errors.

- [ ] **Step 3: Commit**

```bash
cd /home/nekosaif/github/mtrtk
git add web/package.json web/pnpm-lock.yaml web/vite.config.ts web/tsconfig*.json web/index.html web/src web/components.json web/.gitignore
git commit -m "feat(web): Vite + React + TypeScript + Tailwind v4 skeleton with shadcn config

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 17: Docker image, Compose, `.env.example`, CI

**Files:**
- Create: `docker/Dockerfile`, `.dockerignore`, `docker-compose.yml`, `.env.example`, `.github/workflows/ci.yml`
- Modify: `README.md`

**Interfaces:**
- Produces: image `ghcr.io/<owner>/mtrtk` with entrypoint `mtrtk` (default command `run`), RTKLIB demo5 `convbin` + `rnx2rtkp` on PATH, SPA at `/app/src/mtrtk/web/static`; Compose service `mtrtk` (host network, `/dev` bind, cgroup rules, `./data:/data`, `env_file: .env`). CI runs ruff, mypy, pytest, web build, multi-arch image build (push on `main`).

- [ ] **Step 1: Write `docker/Dockerfile`**

```dockerfile
# syntax=docker/dockerfile:1.7

# ---------- 1. web UI ----------
FROM node:22-alpine AS web
WORKDIR /web
RUN corepack enable && corepack prepare pnpm@10 --activate
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

# ---------- 2. RTKLIB demo5 (convbin, rnx2rtkp) ----------
FROM debian:bookworm-slim AS rtklib
ARG RTKLIB_TAG=v2.5.1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch ${RTKLIB_TAG} https://github.com/rtklibexplorer/RTKLIB.git /rtklib
RUN make -C /rtklib/app/consapp/convbin/gcc -j"$(nproc)" \
    && make -C /rtklib/app/consapp/rnx2rtkp/gcc -j"$(nproc)" \
    && strip /rtklib/app/consapp/convbin/gcc/convbin /rtklib/app/consapp/rnx2rtkp/gcc/rnx2rtkp

# ---------- 3. runtime ----------
FROM python:3.12-slim-bookworm AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 DATA_DIR=/data PATH="/app/.venv/bin:${PATH}"
RUN apt-get update && apt-get install -y --no-install-recommends tini gzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
COPY --from=rtklib /rtklib/app/consapp/convbin/gcc/convbin /rtklib/app/consapp/rnx2rtkp/gcc/rnx2rtkp /usr/local/bin/
COPY --from=web /web/dist ./src/mtrtk/web/static
VOLUME ["/data"]
ENTRYPOINT ["tini", "--", "mtrtk"]
CMD ["run"]
```

`.dockerignore`:
```
.git
.venv
data
web/node_modules
web/dist
**/__pycache__
**/.pytest_cache
**/.mypy_cache
**/.ruff_cache
tests/fixtures
docs
```

- [ ] **Step 2: Write `docker-compose.yml`**

```yaml
services:
  mtrtk:
    image: ghcr.io/nekosaif/mtrtk:latest
    build:
      context: .
      dockerfile: docker/Dockerfile
    container_name: mtrtk
    network_mode: host          # binds directly to the Tailscale IP, sees tailscale0
    restart: unless-stopped
    stop_grace_period: 20s
    env_file: .env
    environment:
      DATA_DIR: /data
    volumes:
      - ./data:/data
      - /dev:/dev               # hot-plug safe: nodes appear inside the container on replug
    device_cgroup_rules:
      - "c 166:* rmw"           # ttyACM* (USB CDC, ZED-F9P over USB-C)
      - "c 188:* rmw"           # ttyUSB* (USB-serial adapters)
    healthcheck:
      test: ["CMD", "mtrtk", "--version"]
      interval: 60s
      timeout: 10s
      retries: 3
```

- [ ] **Step 3: Write `.env.example`**

```dotenv
# ---------------------------------------------------------------- role / receiver
ROLE=base                         # base | rover
MTRTK_SOURCE=auto                 # auto | /dev/serial/by-id/usb-u-blox_... | file:/data/replay.ubx
BAUD=115200
DATA_DIR=/data
STATION_ID=MTRK                   # 4 upper-case chars, used in file names and RINEX marker
COUNTRY=BGD                       # ISO 3166-1 alpha-3, used in RINEX 3 file names
MARKER_NAME=MTRK
ANTENNA_TYPE=NONE                 # IGS antenna code, NONE for uncalibrated antennas
ANTENNA_HEIGHT_M=0.0              # ARP height above the mark
OBSERVER=mtrtk
AGENCY=mtrtk
RECEIVER_STRICT=1                 # 0 = keep running even if a core CFG key is rejected

# ---------------------------------------------------------------- base station
BASE_MODE=survey-in               # survey-in | fixed | off
SVIN_MIN_DURATION_S=300
SVIN_ACC_LIMIT_M=2.0
ACTIVE_SITE=                      # name of a saved site when BASE_MODE=fixed
RTCM_MSM=7                        # 7 (1077/1087/1097/1127) or 4 (1074/1084/1094/1124)
RTCM_1230_RATE=5                  # seconds between RTCM 1230 (GLONASS biases)
RTCM_STATION_ID=0                 # RTCM DF003 reference station id (0-4095)

# ---------------------------------------------------------------- NTRIP caster
NTRIP_BIND=tailscale              # tailscale | lan | all | <ip>
NTRIP_PORT=2101
MOUNTPOINT=MTRK
NTRIP_USER=rover
NTRIP_PASSWORD=change-me          # empty value = anonymous access (tailnet only!)

# ---------------------------------------------------------------- web UI
WEB_BIND=tailscale                # tailscale | lan | all | <ip>
WEB_PORT=8080
WEB_PASSWORD=                     # required unless WEB_BIND=tailscale
WEB_ALLOW_INSECURE=0

# ---------------------------------------------------------------- raw logging
LOG_MESSAGES=RXM-RAWX,RXM-SFRBX,NAV-PVT,NAV-HPPOSLLH,NAV-SVIN,TIM-TM2,MON-VER
MIN_FREE_GB=5
FSYNC_INTERVAL_S=10

# ---------------------------------------------------------------- rover
ROVER_DRIVER=ublox                # ublox | sbg_ellipse | vectornav
ROVER_NAV_HZ=5
ROVER_DYNMODEL=portable           # portable | stationary | pedestrian | automotive | airborne1g | airborne2g | airborne4g
NTRIP_URL=                        # ntrip://user:pass@base-host:2101/MTRK
NTRIP_GGA_INTERVAL_S=10
NMEA_TCP_PORT=10110
NMEA_UDP_TARGETS=                 # host:port,host:port
NMEA_SERIAL=                      # e.g. /dev/ttyUSB1
JSON_UDP_PORT=

# ---------------------------------------------------------------- alerts / exposure
ALERT_WEBHOOK_URL=
PUBLIC_DOMAIN=                    # for the 'public' compose profile (Caddy TLS), Phase 9
TUNNEL_TOKEN=                     # for the 'cloudflare' compose profile, Phase 9
```

- [ ] **Step 4: Write `.github/workflows/ci.yml`**

```yaml
name: ci
on:
  push:
    branches: [main]
    tags: ["v*"]
  pull_request:

jobs:
  python:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with: { enable-cache: true }
      - run: uv sync --frozen
      - run: uv run ruff check . && uv run ruff format --check .
      - run: uv run mypy
      - run: uv run pytest -q

  web:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v4
        with: { version: 10 }
      - uses: actions/setup-node@v4
        with: { node-version: 22, cache: pnpm, cache-dependency-path: web/pnpm-lock.yaml }
      - run: pnpm --dir web install --frozen-lockfile
      - run: pnpm --dir web build

  image:
    needs: [python, web]
    runs-on: ubuntu-latest
    permissions: { contents: read, packages: write }
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-qemu-action@v3
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        if: github.event_name != 'pull_request'
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/metadata-action@v5
        id: meta
        with:
          images: ghcr.io/${{ github.repository_owner }}/mtrtk
          tags: |
            type=ref,event=branch
            type=semver,pattern={{version}}
            type=raw,value=latest,enable={{is_default_branch}}
      - uses: docker/build-push-action@v6
        with:
          context: .
          file: docker/Dockerfile
          platforms: linux/amd64,linux/arm64
          push: ${{ github.event_name != 'pull_request' }}
          tags: ${{ steps.meta.outputs.tags }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

- [ ] **Step 5: Build and smoke-test the image locally (amd64)**

Run:
```bash
cd /home/nekosaif/github/mtrtk
docker build -f docker/Dockerfile -t mtrtk:dev .
docker run --rm mtrtk:dev --version
docker run --rm --entrypoint convbin mtrtk:dev 2>&1 | head -3
docker run --rm --entrypoint rnx2rtkp mtrtk:dev 2>&1 | head -3
docker run --rm -e NTRIP_PASSWORD=x -v "$PWD/tests/fixtures:/fx:ro" mtrtk:dev replay /fx/f9p_hpg113_raw_10s.ubx --speed 0 | tail -3
```
Expected: `mtrtk, version 0.1.0`; convbin/rnx2rtkp print their usage; replay prints status lines ending with `replay finished`.

Then the live receiver through Compose:
```bash
cp .env.example .env   # then set NTRIP_PASSWORD
docker compose up --build
```
Expected: same status lines as `uv run mtrtk base` (the container reaches `/dev/ttyACM0` via the `/dev` bind). Ctrl-C, then `docker compose down`.

Optional arm64 check (slow, QEMU): `docker run --privileged --rm tonistiigi/binfmt --install arm64 && docker buildx build --platform linux/arm64 -f docker/Dockerfile -t mtrtk:arm64 --load .` — CI performs this on every push to `main`.

- [ ] **Step 6: Update `README.md` quick start**

Append:
```markdown
## Quick start (base station)

```bash
git clone https://github.com/nekosaif/mtrtk.git && cd mtrtk
cp .env.example .env            # set NTRIP_PASSWORD at minimum
docker compose up -d
docker compose logs -f          # one status line per second once the receiver is configured
```

Without Docker: `uv sync && uv run mtrtk doctor && uv run mtrtk base`.

Replay a recording with no hardware: `uv run mtrtk replay tests/fixtures/f9p_hpg113_raw_10s.ubx --speed 10`.

## Status

Phase 1 (receiver core) complete: framing, live state, receiver configuration with firmware capability
probing, replay mode, Docker image. Next: raw logging + NTRIP caster (Phase 2), web API (Phase 3), UI (Phase 4).
```

- [ ] **Step 7: Commit**

```bash
git add docker/Dockerfile .dockerignore docker-compose.yml .env.example .github/workflows/ci.yml README.md
git commit -m "build: multi-stage Docker image with RTKLIB demo5, compose service, env template and CI

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 18: Phase close-out

**Files:**
- Modify: `docs/superpowers/specs/2026-09-18-mtrtk-design.md` (only if the live test moved keys between core/optional — record the final HPG 1.13 key sets under *Implementation reference*)

- [ ] **Step 1: Full verification**

Run:
```bash
uv run pytest -q
uv run pytest -m hardware -q        # receiver attached
uv run ruff check . && uv run ruff format --check . && uv run mypy
pnpm --dir web build
docker build -f docker/Dockerfile -t mtrtk:dev .
```
Expected: everything green.

- [ ] **Step 2: Record deviations in the spec**

If Task 14 Step 6 moved any key into `OPTIONAL_FEATURES`, edit the spec's *CFG-VALSET key sets* list to match reality, and add one line to *Open items* saying which keys HPG 1.13 rejected.

- [ ] **Step 3: Tag and commit**

```bash
git add -A
git commit -m "docs: record Phase 1 outcomes in design spec

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git tag -a v0.1.0-phase1 -m "Phase 0-1: scaffold and receiver core"
```

Phase 2 (raw logging, retention, NTRIP caster, survey-in/sites, SQLite sampler, alerts) gets its own plan: `docs/superpowers/plans/2026-09-XX-phase2-base-daemon.md`, written after this plan lands so it can reference the real module APIs.
