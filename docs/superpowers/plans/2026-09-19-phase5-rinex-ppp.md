# mtrtk Phase 5: RINEX Export and PPP Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn hourly raw UBX logs into RINEX files shaped for CSRS-PPP, AUSPOS, OPUS or generic post-processing (spliced window, decimated, Hatanaka/gzip, correctly named), run those exports as background jobs from the CLI, API and UI, and import the PPP result files those services return so the base gets centimetre-level fixed coordinates in two clicks.

**Architecture:** `rinex/splice.py` concatenates the hourly files overlapping a window (plus one hour of lead for ephemerides); `rinex/convbin.py` drives RTKLIB `convbin` (`-r ubx -v 3.04|2.11 -ro -TADJ=1.0 -ti …`), parsing its summary; `rinex/naming.py` produces RINEX 3 long names / RINEX 2 short names; `rinex/presets.py` holds the service presets; `rinex/export.py` is the job function (Phase 3 `JobRunner`) that strings these together and writes a `manifest.json`; `rinex/ppp_result.py` parses CSRS-PPP `.sum`/`.pos`, SINEX and OPUS text into one `PppResult`. The API adds export presets/submission, a bounded synchronous RINEX endpoint (used later by PPK over Tailscale), and PPP import/preview; the UI gains an export panel with live job progress and a PPP import dialog.

**Tech Stack:** Python asyncio subprocess (`convbin` from RTKLIB demo5 in Docker / `rtklib` apt package locally and in CI), `hatanaka` (RNXCMP bindings), FastAPI multipart upload (`python-multipart`), React (existing Phase 4 stack).

**Spec:** `docs/superpowers/specs/2026-09-18-mtrtk-design.md` — *rinex / PPP*, *convbin / RINEX* reference, *Phase 5*, open items 2, 4, 5. Prerequisites: Phases 1–4 complete. Facts verified during planning on real HPG 1.13 data with convbin 2.4.3: `-v 3.04 -n` writes ONE mixed nav file (open item 2 resolved), epochs are tagged `.998 s` without `-ro -TADJ=1.0` and exactly on the second with it, `-ti 5` decimation then works, RINEX 2.11 GPS-only via `-y R -y E -y J -y C -y S -y I` works, `hatanaka.compress_on_disk(path, compression="gz")` yields `<stem>.crx.gz` only when the input ends in `.rnx` (`.obs` is merely gzipped).

## Global Constraints

- Every convbin invocation uses `-r ubx -od -os -f 2 -scan -ro -TADJ=1.0` plus header flags from settings; `-f 3` only when the receiver reports L5 signals (HPG 1.51+; not on 1.13).
- Splice lead: include the hour before the window start so the nav file has ephemerides; clip with `-ts/-te`.
- Output names: RINEX 3 `{STATION}00{CCC}_R_{YYYY}{DDD}{HH}{MM}_{DUR}_{PER}_MO.rnx` / `…_MN.rnx`; RINEX 2 `{stat}{ddd}{s}.{yy}o` / `.{yy}n` where `s` is the hour letter (`a`..`x`) or `0` for a full day. Hatanaka output ends `.crx.gz`; gzip-only ends `.rnx.gz`.
- Presets (fixed): `csrs-ppp` (3.04, 30 s, all systems, Hatanaka+gz on), `auspos` (3.04, 30 s, exclude none, gz on), `opus` (2.11, 30 s, GPS only, no compression), `generic` (3.04, native interval, all systems, compression off; every option user-adjustable). Preset ids are stable strings used by API, CLI and UI.
- Sigmas stored on a `Site` are **1σ per axis in metres**: CSRS-PPP reports 95 % → divide by 1.96; SINEX STD_DEV and OPUS "peak-to-peak"/sigma columns are taken as given; frame and epoch are stored as reported (e.g. `ITRF2020`, `2026.71`).
- Export jobs run through `JobRunner` (kind `export`, one at a time); results live in `DATA_DIR/jobs/<id>/` with `manifest.json`; the synchronous `GET /api/export/rinex` is limited to windows ≤ 6 h.
- Uploads for PPP import are limited to 20 MB; parsing never executes content; unknown formats return 422 with the first 200 chars of the file and a hint.
- Commit per task, Conventional Commits, trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File structure (this plan)

| Path | Responsibility |
|---|---|
| `src/mtrtk/rinex/__init__.py`, `convbin.py` | `ConvbinOptions`, `build_convbin_command`, `run_convbin`, `convbin_available` |
| `src/mtrtk/rinex/splice.py` | `splice_window` → temp UBX + clip times |
| `src/mtrtk/rinex/naming.py` | RINEX 2/3 file names, duration/period codes |
| `src/mtrtk/rinex/presets.py` | `Preset`, `PRESETS`, `resolve_options` |
| `src/mtrtk/rinex/export.py` | `ExportRequest`, `run_export` (job function), `export_to_dir` (sync path for CLI), manifest |
| `src/mtrtk/rinex/ppp_result.py` | `PppResult`, `parse_ppp_result`, format detectors |
| `src/mtrtk/web/api/export.py`, `src/mtrtk/web/api/base.py` (modify) | export + PPP import endpoints |
| `src/mtrtk/cli.py` (modify) | `mtrtk export`, `mtrtk ppp-import` |
| `web/src/pages/Logs.tsx` (modify), `web/src/components/ExportPanel.tsx`, `web/src/components/JobsPanel.tsx`, `web/src/components/PppImportDialog.tsx`, `web/src/pages/Site.tsx` (modify) | UI |
| `tests/fixtures/ppp/*` | PPP result samples (reconstructed; replaced by real files when available) |
| `docs/ppp-workflow.md` | user guide |

---

### Task 1: convbin wrapper

**Files:**
- Create: `src/mtrtk/rinex/__init__.py` (empty), `src/mtrtk/rinex/convbin.py`, `tests/unit/test_convbin.py`
- Modify: `.github/workflows/ci.yml` (python job: `sudo apt-get install -y rtklib` before tests)

**Interfaces:**
- Produces: `RinexHeader(marker_name, marker_number, marker_type="GEODETIC", observer, agency, receiver_number="0", receiver_type="u-blox ZED-F9P", receiver_version, antenna_number="0", antenna_type="NONE", approx_xyz: tuple|None, delta_hen=(0.0,0.0,0.0), comment=None)`; `ConvbinOptions(version="3.04", interval_s: float|None, exclude_systems: tuple[str,...]=(), frequencies=2, start: datetime|None, end: datetime|None, header: RinexHeader, receiver_options=("-TADJ=1.0",))`; `build_convbin_command(input_path, obs_out, nav_out, opts) -> list[str]`; `ConvbinResult(obs_path, nav_path, obs_epochs, nav_messages, stderr_tail)`; `async run_convbin(input_path, obs_out, nav_out, opts, binary="convbin") -> ConvbinResult` (raises `ConvbinError` on non-zero exit or missing output); `convbin_available(binary="convbin") -> bool`; `parse_convbin_summary(stderr) -> tuple[int, int]` (`O=…`, `N=…`).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_convbin.py`:
```python
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mtrtk.rinex.convbin import (
    ConvbinError,
    ConvbinOptions,
    RinexHeader,
    build_convbin_command,
    convbin_available,
    parse_convbin_summary,
    run_convbin,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_raw_60s.ubx"
HEADER = RinexHeader(marker_name="MTRK", marker_number="00001", observer="mtrtk", agency="mtrtk", receiver_version="HPG 1.13", antenna_type="NONE", approx_xyz=(-26748.172, 5837156.618, 2561801.261), delta_hen=(0.05, 0.0, 0.0))


def test_build_command_full() -> None:
    opts = ConvbinOptions(version="3.04", interval_s=30, exclude_systems=("R", "E"), start=datetime(2026, 9, 18, 10, tzinfo=UTC), end=datetime(2026, 9, 18, 11, tzinfo=UTC), header=HEADER)
    cmd = build_convbin_command(Path("in.ubx"), Path("out.rnx"), Path("nav.rnx"), opts)
    assert cmd[0] == "convbin"
    joined = " ".join(cmd)
    assert "-r ubx" in joined and "-v 3.04" in joined and "-od -os" in joined and "-f 2" in joined and "-scan" in joined
    assert "-ro -TADJ=1.0" in joined
    assert "-ti 30" in joined
    assert "-ts 2026/09/18 10:00:00" in joined and "-te 2026/09/18 11:00:00" in joined
    assert cmd.count("-y") == 2 and "R" in cmd and "E" in cmd
    assert "-hm MTRK" in joined and "-hn 00001" in joined and "-ht GEODETIC" in joined
    assert "-ho mtrtk/mtrtk" in joined and "-hr 0/u-blox ZED-F9P/HPG 1.13" in joined and "-ha 0/NONE" in joined
    assert "-hp -26748.1720/5837156.6180/2561801.2610" in joined and "-hd 0.0500/0.0000/0.0000" in joined
    assert cmd[-5:] == ["-o", "out.rnx", "-n", "nav.rnx", "in.ubx"]


def test_build_command_minimal_omits_optional_flags() -> None:
    cmd = build_convbin_command(Path("a.ubx"), Path("a.obs"), Path("a.nav"), ConvbinOptions(header=RinexHeader(marker_name="X", observer="o", agency="a", receiver_version="")))
    joined = " ".join(cmd)
    assert "-ti" not in cmd and "-ts" not in cmd and "-hp" not in cmd and "-y" not in cmd


def test_parse_summary() -> None:
    stderr = "scanning: 2026/09/18 18:25:28 GREJC\n2026/09/18 18:25:09-09/18 18:25:28: O=20 N=3 \n"
    assert parse_convbin_summary(stderr) == (20, 3)
    assert parse_convbin_summary("garbage") == (0, 0)


@pytest.mark.skipif(not convbin_available() or not FIXTURE.exists(), reason="convbin or fixture missing")
async def test_run_convbin_on_fixture(tmp_path: Path) -> None:
    res = await run_convbin(FIXTURE, tmp_path / "MTRK.rnx", tmp_path / "MTRK_MN.rnx", ConvbinOptions(header=HEADER))
    assert res.obs_path.exists() and res.nav_path.exists()
    assert res.obs_epochs >= 50
    head = res.obs_path.read_text().splitlines()[:12]
    assert head[0].startswith("     3.04") and any("MARKER NAME" in line and "MTRK" in line for line in head)
    assert res.nav_path.read_text().splitlines()[0].startswith("     3.04") and "M: Mixed" in res.nav_path.read_text().splitlines()[0]
    epochs = [line for line in res.obs_path.read_text().splitlines() if line.startswith(">")]
    assert all(line.split()[6].endswith(".0000000") for line in epochs)  # -TADJ aligned


@pytest.mark.skipif(not convbin_available() or not FIXTURE.exists(), reason="convbin or fixture missing")
async def test_run_convbin_decimates(tmp_path: Path) -> None:
    res = await run_convbin(FIXTURE, tmp_path / "a.rnx", tmp_path / "a_MN.rnx", ConvbinOptions(interval_s=10, header=HEADER))
    assert 4 <= res.obs_epochs <= 7


async def test_run_convbin_missing_binary(tmp_path: Path) -> None:
    (tmp_path / "x.ubx").write_bytes(b"\x00")
    with pytest.raises(ConvbinError, match="not found"):
        await run_convbin(tmp_path / "x.ubx", tmp_path / "x.rnx", tmp_path / "x_MN.rnx", ConvbinOptions(header=HEADER), binary="/nonexistent/convbin")


def test_convbin_available_reflects_path() -> None:
    assert convbin_available() == (shutil.which("convbin") is not None)
    assert convbin_available("/nonexistent/convbin") is False
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/unit/test_convbin.py -q` → `ModuleNotFoundError`.

- [ ] **Step 3: Write `src/mtrtk/rinex/convbin.py`**

```python
"""Thin async wrapper around RTKLIB `convbin` for UBX -> RINEX conversion."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_SUMMARY_RE = re.compile(r"O=(\d+)(?:\s+N=(\d+))?")


class ConvbinError(RuntimeError):
    pass


@dataclass(frozen=True)
class RinexHeader:
    marker_name: str
    observer: str
    agency: str
    receiver_version: str
    marker_number: str = "00001"
    marker_type: str = "GEODETIC"
    receiver_number: str = "0"
    receiver_type: str = "u-blox ZED-F9P"
    antenna_number: str = "0"
    antenna_type: str = "NONE"
    approx_xyz: tuple[float, float, float] | None = None
    delta_hen: tuple[float, float, float] = (0.0, 0.0, 0.0)
    comment: str | None = None


@dataclass(frozen=True)
class ConvbinOptions:
    header: RinexHeader
    version: str = "3.04"
    interval_s: float | None = None
    exclude_systems: tuple[str, ...] = ()  # convbin letters: G R E J S C I
    frequencies: int = 2
    start: datetime | None = None
    end: datetime | None = None
    receiver_options: tuple[str, ...] = ("-TADJ=1.0",)


@dataclass(frozen=True)
class ConvbinResult:
    obs_path: Path
    nav_path: Path
    obs_epochs: int
    nav_messages: int
    stderr_tail: str


def convbin_available(binary: str = "convbin") -> bool:
    return shutil.which(binary) is not None or Path(binary).is_file()


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%Y/%m/%d %H:%M:%S")


def build_convbin_command(input_path: Path, obs_out: Path, nav_out: Path, opts: ConvbinOptions, binary: str = "convbin") -> list[str]:
    h = opts.header
    cmd: list[str] = [binary, "-r", "ubx", "-v", opts.version, "-od", "-os", "-oi", "-ot", "-ol", "-f", str(opts.frequencies), "-scan"]
    for ro in opts.receiver_options:
        cmd += ["-ro", ro]
    if opts.interval_s:
        cmd += ["-ti", f"{opts.interval_s:g}"]
    if opts.start:
        cmd += ["-ts", _fmt_time(opts.start)]
    if opts.end:
        cmd += ["-te", _fmt_time(opts.end)]
    for sysletter in opts.exclude_systems:
        cmd += ["-y", sysletter]
    cmd += ["-hm", h.marker_name, "-hn", h.marker_number, "-ht", h.marker_type, "-ho", f"{h.observer}/{h.agency}", "-hr", f"{h.receiver_number}/{h.receiver_type}/{h.receiver_version}", "-ha", f"{h.antenna_number}/{h.antenna_type}"]
    if h.approx_xyz:
        cmd += ["-hp", "/".join(f"{v:.4f}" for v in h.approx_xyz)]
    cmd += ["-hd", "/".join(f"{v:.4f}" for v in h.delta_hen)]
    if h.comment:
        cmd += ["-hc", h.comment]
    cmd += ["-o", str(obs_out), "-n", str(nav_out), str(input_path)]
    return cmd


def parse_convbin_summary(stderr: str) -> tuple[int, int]:
    matches = _SUMMARY_RE.findall(stderr)
    if not matches:
        return 0, 0
    obs, nav = matches[-1]
    return int(obs), int(nav or 0)


async def run_convbin(input_path: Path, obs_out: Path, nav_out: Path, opts: ConvbinOptions, binary: str = "convbin") -> ConvbinResult:
    if not convbin_available(binary):
        raise ConvbinError(f"convbin not found ({binary}); install RTKLIB or use the Docker image")
    obs_out.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_convbin_command(input_path, obs_out, nav_out, opts, binary)
    log.info("running: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    text = (stderr + stdout).decode("utf-8", "replace")
    tail = text[-2000:]
    if proc.returncode not in (0, None) and not obs_out.exists():
        raise ConvbinError(f"convbin exited {proc.returncode}: {tail.strip()[-500:]}")
    if not obs_out.exists():
        raise ConvbinError(f"convbin produced no observation file: {tail.strip()[-500:]}")
    obs_epochs, nav_messages = parse_convbin_summary(text)
    if obs_epochs == 0:
        obs_epochs = sum(1 for line in obs_out.read_text(errors="replace").splitlines() if line.startswith(">"))
    if not nav_out.exists():
        nav_out.write_text("")  # convbin writes no nav file when the window has no ephemerides
    return ConvbinResult(obs_out, nav_out, obs_epochs, nav_messages, tail)
```

- [ ] **Step 4: CI**

In `.github/workflows/ci.yml` python job, before `uv sync`: `- run: sudo apt-get update && sudo apt-get install -y rtklib`.

- [ ] **Step 5: Run tests, lint, commit**

`uv run pytest tests/unit/test_convbin.py -q` → all pass (fixture tests run because the Phase 1 fixture exists); `uv run ruff check . && uv run ruff format . && uv run mypy`.
```bash
git add src/mtrtk/rinex .github/workflows/ci.yml tests/unit/test_convbin.py
git commit -m "feat(rinex): async convbin wrapper with header flags, TADJ alignment and summary parsing

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Splicing, naming and presets

**Files:**
- Create: `src/mtrtk/rinex/splice.py`, `src/mtrtk/rinex/naming.py`, `src/mtrtk/rinex/presets.py`, `tests/unit/test_rinex_splice_naming.py`, `tests/unit/test_presets.py`

**Interfaces:**
- Produces: `SpliceResult(path, files: list[LogFile], bytes, lead_hours=1)`; `splice_window(root, start, end, dest, lead_hours=1) -> SpliceResult` (raises `NoDataError` when no files); `duration_code(seconds) -> str` (`01H`, `06H`, `01D`, `15M`…), `period_code(seconds|None) -> str` (`01S`, `05S`, `30S`, `00U` for unknown); `rinex3_name(station, country, start, duration_s, interval_s, kind="MO", source="R") -> str`; `rinex2_name(station, start, duration_s, kind="o") -> str`; `Preset(id, name, service_url, description, version, interval_s, exclude_systems, hatanaka, gzip, constraints: list[str], adjustable: bool)`; `PRESETS: dict[str, Preset]`; `resolve_options(preset_id, interval_s=None, hatanaka=None, gzip=None) -> ResolvedOptions` (only the `generic` preset accepts overrides; others raise `ValueError`).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_rinex_splice_naming.py`:
```python
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from webtest import make_log  # noqa: F401  (helper defined in tests/unit/webtest.py)

from mtrtk.rinex.naming import duration_code, period_code, rinex2_name, rinex3_name
from mtrtk.rinex.splice import NoDataError, splice_window

H0 = datetime(2026, 9, 18, 10, tzinfo=UTC)


def test_codes() -> None:
    assert duration_code(3600) == "01H" and duration_code(6 * 3600) == "06H" and duration_code(86400) == "01D" and duration_code(900) == "15M" and duration_code(2 * 86400) == "02D"
    assert period_code(30) == "30S" and period_code(1) == "01S" and period_code(0.2) == "20C" and period_code(None) == "00U"


def test_rinex3_and_rinex2_names() -> None:
    assert rinex3_name("MTRK", "BGD", H0, 86400, 30) == "MTRK00BGD_R_20262611000_01D_30S_MO.rnx"
    assert rinex3_name("MTRK", "BGD", H0, 3600, None, kind="MN") == "MTRK00BGD_R_20262611000_01H_MN.rnx"
    assert rinex2_name("MTRK", H0, 3600) == "mtrk261k.26o"  # hour 10 -> 'k'
    assert rinex2_name("MTRK", H0.replace(hour=0), 86400) == "mtrk2610.26o"
    assert rinex2_name("MTRK", H0, 3600, kind="n") == "mtrk261k.26n"


def test_splice_window_includes_lead_hour_and_concatenates(tmp_path: Path) -> None:
    for i in range(5):
        make_log(tmp_path, H0 + timedelta(hours=i), size=10)
    res = splice_window(tmp_path, H0 + timedelta(hours=2), H0 + timedelta(hours=4), tmp_path / "out.ubx")
    assert [f.hour_utc for f in res.files] == [H0 + timedelta(hours=1), H0 + timedelta(hours=2), H0 + timedelta(hours=3)]
    assert res.path.read_bytes() == bytes([11]) * 10 + bytes([12]) * 10 + bytes([13]) * 10
    assert res.bytes == 30


def test_splice_window_no_data(tmp_path: Path) -> None:
    with pytest.raises(NoDataError):
        splice_window(tmp_path, H0, H0 + timedelta(hours=1), tmp_path / "out.ubx")
```

`tests/unit/test_presets.py`:
```python
import pytest

from mtrtk.rinex.presets import PRESETS, resolve_options


def test_preset_catalogue() -> None:
    assert set(PRESETS) == {"csrs-ppp", "auspos", "opus", "generic"}
    csrs = PRESETS["csrs-ppp"]
    assert csrs.version == "3.04" and csrs.interval_s == 30 and csrs.hatanaka and csrs.gzip and csrs.exclude_systems == ()
    opus = PRESETS["opus"]
    assert opus.version == "2.11" and opus.exclude_systems == ("R", "E", "J", "C", "S", "I") and not opus.hatanaka and not opus.gzip
    assert PRESETS["generic"].adjustable is True and PRESETS["csrs-ppp"].adjustable is False
    assert all(p.service_url.startswith("https://") or p.id == "generic" for p in PRESETS.values())


def test_resolve_options_generic_overrides() -> None:
    r = resolve_options("generic", interval_s=5, hatanaka=True, gzip=True)
    assert r.interval_s == 5 and r.hatanaka and r.gzip and r.version == "3.04"
    r2 = resolve_options("generic")
    assert r2.interval_s is None and not r2.hatanaka


def test_resolve_options_fixed_presets_reject_overrides() -> None:
    assert resolve_options("csrs-ppp").interval_s == 30
    with pytest.raises(ValueError, match="fixed"):
        resolve_options("csrs-ppp", interval_s=1)
    with pytest.raises(KeyError):
        resolve_options("nope")
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Write `src/mtrtk/rinex/naming.py`**

```python
"""RINEX file naming (RINEX 3 long names and RINEX 2 short names)."""

from __future__ import annotations

from datetime import datetime


def duration_code(seconds: float) -> str:
    s = int(round(seconds))
    if s % 86400 == 0 and s >= 86400:
        return f"{s // 86400:02d}D"
    if s % 3600 == 0 and s >= 3600:
        return f"{s // 3600:02d}H"
    if s % 60 == 0 and s >= 60:
        return f"{s // 60:02d}M"
    return f"{s:02d}S"


def period_code(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return "00U"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60):02d}M"
    if seconds >= 1:
        return f"{int(round(seconds)):02d}S"
    return f"{int(round(seconds * 100)):02d}C"  # centiseconds, e.g. 5 Hz -> 20C


def rinex3_name(station: str, country: str, start: datetime, duration_s: float, interval_s: float | None, kind: str = "MO", source: str = "R") -> str:
    base = f"{station.upper():<4}00{country.upper():<3}_{source}_{start:%Y%j%H%M}_{duration_code(duration_s)}"
    if kind == "MO":
        return f"{base}_{period_code(interval_s)}_MO.rnx"
    return f"{base}_{kind}.rnx"


def rinex2_name(station: str, start: datetime, duration_s: float, kind: str = "o") -> str:
    session = "0" if duration_s >= 86400 else chr(ord("a") + start.hour)
    return f"{station.lower()[:4]}{start:%j}{session}.{start:%y}{kind}"
```

- [ ] **Step 4: Write `src/mtrtk/rinex/splice.py`**

```python
"""Concatenate the hourly raw files that cover a time window (plus a lead hour for ephemerides)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from mtrtk.rawlog.index import LogFile, files_for_window

CHUNK = 1 << 20


class NoDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpliceResult:
    path: Path
    files: list[LogFile]
    bytes: int
    lead_hours: int


def splice_window(root: Path, start: datetime, end: datetime, dest: Path, lead_hours: int = 1) -> SpliceResult:
    files = files_for_window(root, start - timedelta(hours=lead_hours), end)
    if not files:
        raise NoDataError(f"no raw logs between {start.isoformat()} and {end.isoformat()}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with dest.open("wb") as out:
        for lf in files:
            with lf.path.open("rb") as fh:
                while chunk := fh.read(CHUNK):
                    out.write(chunk)
                    total += len(chunk)
    return SpliceResult(dest, files, total, lead_hours)
```

- [ ] **Step 5: Write `src/mtrtk/rinex/presets.py`**

```python
"""Export presets for the PPP services we target."""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    service_url: str
    description: str
    version: str
    interval_s: float | None
    exclude_systems: tuple[str, ...]
    hatanaka: bool
    gzip: bool
    constraints: tuple[str, ...]
    adjustable: bool = False


PRESETS: dict[str, Preset] = {
    "csrs-ppp": Preset(
        id="csrs-ppp", name="CSRS-PPP (NRCan)", service_url="https://webapp.csrs-scrs.nrcan-rncan.gc.ca/geod/tools-outils/ppp.php",
        description="Free global PPP. Static mode, ITRF2020 result at the observation epoch. Upload the .crx.gz file.",
        version="3.04", interval_s=30, exclude_systems=(), hatanaka=True, gzip=True,
        constraints=("24 h of data recommended (a few hours minimum)", "RINEX 2.11 or 3.x, Hatanaka + gzip accepted", "Choose 'Static' and ITRF outside Canada", "Result: .sum + .pos in a zip by e-mail"),
    ),
    "auspos": Preset(
        id="auspos", name="AUSPOS (Geoscience Australia)", service_url="https://gnss.ga.gov.au/auspos",
        description="Free global GPS PPP/relative processing. 1 h minimum, 2 h+ recommended, up to 7 days.",
        version="3.04", interval_s=30, exclude_systems=(), hatanaka=False, gzip=True,
        constraints=("1 h minimum, 7 days maximum", "GPS observations are used", "Result: PDF report + SINEX (.SNX) by e-mail"),
    ),
    "opus": Preset(
        id="opus", name="OPUS (NGS, USA)", service_url="https://geodesy.noaa.gov/OPUS/",
        description="US National Geodetic Survey. GPS L1/L2 only, RINEX 2.11, 15 min to 48 h. Works inside the continental US only.",
        version="2.11", interval_s=30, exclude_systems=("R", "E", "J", "C", "S", "I"), hatanaka=False, gzip=False,
        constraints=("Only for sites in the USA", "GPS only, dual-frequency; F9P L2C acceptance must be checked", "Antenna type NONE unless NGS-calibrated", "Result: text e-mail"),
    ),
    "generic": Preset(
        id="generic", name="Generic RINEX 3.04", service_url="",
        description="Full-rate mixed RINEX for RTKLIB PPK, Trimble RTX post-processing or any other tool. Interval and compression are adjustable.",
        version="3.04", interval_s=None, exclude_systems=(), hatanaka=False, gzip=False,
        constraints=("All constellations, native interval", "Hatanaka and gzip optional"), adjustable=True,
    ),
}


@dataclass(frozen=True)
class ResolvedOptions:
    preset: Preset
    version: str
    interval_s: float | None
    exclude_systems: tuple[str, ...]
    hatanaka: bool
    gzip: bool


def resolve_options(preset_id: str, interval_s: float | None = None, hatanaka: bool | None = None, gzip: bool | None = None) -> ResolvedOptions:
    preset = PRESETS[preset_id]
    overrides = {k: v for k, v in (("interval_s", interval_s), ("hatanaka", hatanaka), ("gzip", gzip)) if v is not None}
    if overrides and not preset.adjustable:
        raise ValueError(f"preset {preset_id!r} has fixed options; use 'generic' to adjust {sorted(overrides)}")
    resolved = replace(preset, **overrides) if overrides else preset
    return ResolvedOptions(preset, resolved.version, resolved.interval_s, resolved.exclude_systems, resolved.hatanaka, resolved.gzip)
```

- [ ] **Step 6: Run tests, lint, commit**

`uv run pytest tests/unit/test_rinex_splice_naming.py tests/unit/test_presets.py -q` → all pass.
```bash
git add src/mtrtk/rinex tests/unit/test_rinex_splice_naming.py tests/unit/test_presets.py
git commit -m "feat(rinex): window splicing, RINEX 2/3 naming and PPP service presets

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Export pipeline (job function + sync path)

**Files:**
- Create: `src/mtrtk/rinex/export.py`, `tests/unit/test_export.py`
- Modify: `pyproject.toml` (add `hatanaka>=2.8`)

**Interfaces:**
- Produces: `ExportRequest(start, end, preset="csrs-ppp", interval_s=None, hatanaka=None, gzip=None, include_nav=True)` (pydantic; `end > start`, ≤ 7 days); `ExportContext(root, station_id, country, header: RinexHeader)`; `async export_to_dir(request, ctx, out_dir, progress=None) -> ExportResult` where `ExportResult(files: list[dict(name, bytes, role: "obs"|"nav"|"manifest")], obs_epochs, nav_messages, interval_s, version, preset, start, end, warnings: list[str])` and `manifest.json` is written in `out_dir`; `make_export_job(request, ctx) -> JobFn` (wraps `export_to_dir` with `JobContext.progress`); `header_from_settings(settings, state, site) -> RinexHeader` (marker from settings, approx XYZ from the active site else the current ECEF position, `delta_hen=(antenna_height_m, 0, 0)`, receiver version from firmware).
- Compression: obs file written as `<name>.rnx` then `hatanaka.compress_on_disk(path, compression="gz", delete=True)` when `hatanaka` (→ `.crx.gz`), else plain gzip when `gzip` (→ `.rnx.gz`). Nav file gzipped when `gzip` (never Hatanaka).
- Warnings added to the result: window shorter than 1 h for csrs-ppp/auspos; fewer than 20 nav messages; OPUS L2C caveat.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_export.py`:
```python
import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mtrtk.rinex.convbin import RinexHeader, convbin_available
from mtrtk.rinex.export import ExportContext, ExportRequest, export_to_dir

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_raw_60s.ubx"
HEADER = RinexHeader(marker_name="MTRK", observer="mtrtk", agency="mtrtk", receiver_version="HPG 1.13")

pytestmark = pytest.mark.skipif(not convbin_available() or not FIXTURE.exists(), reason="convbin or fixture missing")


def fixture_window() -> tuple[datetime, datetime]:
    """The fixture's own UTC window, read from its first/last NAV-PVT."""
    from pyubx2 import UBXReader

    from mtrtk.core.frames import Framer

    times = []
    for f in Framer().feed(FIXTURE.read_bytes()):
        if f.identity == "NAV-PVT":
            m = UBXReader.parse(f.raw)
            if m.validDate and m.validTime:
                times.append(datetime(m.year, m.month, m.day, m.hour, m.min, m.second, tzinfo=UTC))
    return times[0], times[-1] + timedelta(seconds=1)


def install_fixture_as_log(root: Path, start: datetime) -> None:
    from mtrtk.rawlog.writer import Sidecar, log_path, sidecar_path

    hour = start.replace(minute=0, second=0, microsecond=0)
    path = log_path(root, "MTRK", hour)
    path.parent.mkdir(parents=True)
    path.write_bytes(FIXTURE.read_bytes())
    Sidecar("MTRK", "base", start.isoformat(), hour_utc=hour.isoformat(), bytes=path.stat().st_size, complete=True).dump(sidecar_path(path))


async def test_generic_export_writes_rinex_and_manifest(tmp_path: Path) -> None:
    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    ctx = ExportContext(root=tmp_path / "data", station_id="MTRK", country="BGD", header=HEADER)
    progress: list[tuple[float, str | None]] = []

    async def report(p: float, m: str | None) -> None:
        progress.append((p, m))

    res = await export_to_dir(ExportRequest(start=start, end=end, preset="generic"), ctx, tmp_path / "out", progress=report)
    obs = next(f for f in res.files if f["role"] == "obs")
    nav = next(f for f in res.files if f["role"] == "nav")
    assert obs["name"].endswith("_MO.rnx") and nav["name"].endswith("_MN.rnx")
    assert obs["name"].startswith("MTRK00BGD_R_")
    assert res.obs_epochs >= 50 and res.version == "3.04" and res.interval_s is None
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert manifest["preset"] == "generic" and manifest["obs_epochs"] == res.obs_epochs and manifest["files"] == res.files
    assert progress[0][0] < progress[-1][0] == 1.0


async def test_csrs_preset_decimates_and_compresses(tmp_path: Path) -> None:
    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    ctx = ExportContext(root=tmp_path / "data", station_id="MTRK", country="BGD", header=HEADER)
    res = await export_to_dir(ExportRequest(start=start, end=end, preset="csrs-ppp"), ctx, tmp_path / "out")
    obs = next(f for f in res.files if f["role"] == "obs")
    assert obs["name"].endswith("_30S_MO.crx.gz")
    nav = next(f for f in res.files if f["role"] == "nav")
    assert nav["name"].endswith("_MN.rnx.gz")
    assert 1 <= res.obs_epochs <= 3  # 60 s at 30 s
    with gzip.open(tmp_path / "out" / nav["name"], "rt") as fh:
        assert fh.readline().startswith("     3.04")
    assert any("shorter than 1 h" in w for w in res.warnings)


async def test_opus_preset_is_rinex2_gps_only(tmp_path: Path) -> None:
    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    ctx = ExportContext(root=tmp_path / "data", station_id="MTRK", country="BGD", header=HEADER)
    res = await export_to_dir(ExportRequest(start=start, end=end, preset="opus"), ctx, tmp_path / "out")
    obs = next(f for f in res.files if f["role"] == "obs")
    assert obs["name"].endswith(".26o") or obs["name"][-1] == "o"
    text = (tmp_path / "out" / obs["name"]).read_text()
    assert text.startswith("     2.11") and "G: GPS" in text.splitlines()[0]
    assert any("L2C" in w for w in res.warnings)


async def test_export_request_validation() -> None:
    from pydantic import ValidationError

    t = datetime(2026, 9, 18, tzinfo=UTC)
    with pytest.raises(ValidationError):
        ExportRequest(start=t, end=t)
    with pytest.raises(ValidationError):
        ExportRequest(start=t, end=t + timedelta(days=8))
    with pytest.raises(ValidationError):
        ExportRequest(start=t, end=t + timedelta(hours=1), preset="csrs-ppp", interval_s=1)
```
- [ ] **Step 2: Run to verify failure** — FAIL (`mtrtk.rinex.export` missing).

- [ ] **Step 3: Add `hatanaka>=2.8` to `pyproject.toml` dependencies and `uv sync`.** (Wheels exist for x86_64 and aarch64; the Docker runtime stage needs no compiler. If `uv sync` tries to build from source, add `build-essential` to the runtime stage before `uv sync` and remove it afterwards, and note it in the report.)

- [ ] **Step 4: Write `src/mtrtk/rinex/export.py`**

```python
"""Raw UBX window -> RINEX files for a PPP service or generic post-processing."""

from __future__ import annotations

import gzip
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import hatanaka
from pydantic import BaseModel, field_validator, model_validator

from mtrtk.jobs import JobContext, JobFn
from mtrtk.rinex.convbin import ConvbinOptions, RinexHeader, run_convbin
from mtrtk.rinex.naming import rinex2_name, rinex3_name
from mtrtk.rinex.presets import PRESETS, resolve_options
from mtrtk.rinex.splice import splice_window

log = logging.getLogger(__name__)
Progress = Callable[[float, str | None], Awaitable[None]]
MAX_WINDOW = timedelta(days=7)


class ExportRequest(BaseModel):
    start: datetime
    end: datetime
    preset: str = "csrs-ppp"
    interval_s: float | None = None
    hatanaka: bool | None = None
    gzip: bool | None = None
    include_nav: bool = True

    @field_validator("preset")
    @classmethod
    def _preset(cls, v: str) -> str:
        if v not in PRESETS:
            raise ValueError(f"unknown preset {v!r}; choose one of {sorted(PRESETS)}")
        return v

    @model_validator(mode="after")
    def _window(self) -> ExportRequest:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware (UTC)")
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.end - self.start > MAX_WINDOW:
            raise ValueError("window longer than 7 days")
        resolve_options(self.preset, self.interval_s, self.hatanaka, self.gzip)  # raises for fixed presets
        return self


@dataclass(frozen=True)
class ExportContext:
    root: Path
    station_id: str
    country: str
    header: RinexHeader


@dataclass
class ExportResult:
    files: list[dict[str, Any]]
    obs_epochs: int
    nav_messages: int
    interval_s: float | None
    version: str
    preset: str
    start: str
    end: str
    warnings: list[str]

    def to_manifest(self) -> dict[str, Any]:
        return {"preset": self.preset, "version": self.version, "interval_s": self.interval_s, "start": self.start, "end": self.end, "obs_epochs": self.obs_epochs, "nav_messages": self.nav_messages, "files": self.files, "warnings": self.warnings, "created_utc": datetime.now(UTC).isoformat()}


def _gzip_file(path: Path) -> Path:
    out = path.with_name(path.name + ".gz")
    with path.open("rb") as src, gzip.open(out, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()
    return out


async def export_to_dir(request: ExportRequest, ctx: ExportContext, out_dir: Path, progress: Progress | None = None) -> ExportResult:
    async def report(p: float, msg: str) -> None:
        if progress:
            await progress(p, msg)

    opts = resolve_options(request.preset, request.interval_s, request.hatanaka, request.gzip)
    out_dir.mkdir(parents=True, exist_ok=True)
    duration_s = (request.end - request.start).total_seconds()
    warnings: list[str] = []
    if request.preset in ("csrs-ppp", "auspos") and duration_s < 3600:
        warnings.append("window shorter than 1 h: PPP services want several hours (24 h recommended)")
    if request.preset == "opus":
        warnings.append("OPUS wants GPS L2 data; the F9P provides L2C (not L2P). Check acceptance before relying on it.")

    await report(0.05, "splicing raw logs")
    spliced = splice_window(ctx.root, request.start, request.end, out_dir / "spliced.ubx")

    if opts.version.startswith("2"):
        obs_name = rinex2_name(ctx.station_id, request.start, duration_s, "o")
        nav_name = rinex2_name(ctx.station_id, request.start, duration_s, "n")
    else:
        obs_name = rinex3_name(ctx.station_id, ctx.country, request.start, duration_s, opts.interval_s, "MO")
        nav_name = rinex3_name(ctx.station_id, ctx.country, request.start, duration_s, None, "MN")

    await report(0.2, "converting with convbin")
    convbin_opts = ConvbinOptions(header=ctx.header, version=opts.version, interval_s=opts.interval_s, exclude_systems=opts.exclude_systems, start=request.start, end=request.end)
    result = await run_convbin(spliced.path, out_dir / obs_name, out_dir / nav_name, convbin_opts)
    spliced.path.unlink(missing_ok=True)
    if result.nav_messages < 20 and duration_s >= 3600:
        warnings.append(f"only {result.nav_messages} navigation messages in the window; PPP services may reject the nav file (they usually fetch their own ephemerides)")

    await report(0.8, "compressing")
    obs_path = result.obs_path
    if opts.hatanaka:
        obs_path = Path(hatanaka.compress_on_disk(obs_path, compression="gz", delete=True))
    elif opts.gzip:
        obs_path = _gzip_file(obs_path)
    files: list[dict[str, Any]] = [{"name": obs_path.name, "bytes": obs_path.stat().st_size, "role": "obs"}]
    nav_path = result.nav_path
    if request.include_nav and nav_path.exists() and nav_path.stat().st_size > 0:
        if opts.gzip or opts.hatanaka:
            nav_path = _gzip_file(nav_path)
        files.append({"name": nav_path.name, "bytes": nav_path.stat().st_size, "role": "nav"})
    else:
        nav_path.unlink(missing_ok=True)

    res = ExportResult(files, result.obs_epochs, result.nav_messages, opts.interval_s, opts.version, request.preset, request.start.isoformat(), request.end.isoformat(), warnings)
    manifest = out_dir / "manifest.json"
    manifest.write_text(json.dumps(res.to_manifest(), indent=2))
    res.files.append({"name": "manifest.json", "bytes": manifest.stat().st_size, "role": "manifest"})
    manifest.write_text(json.dumps(res.to_manifest(), indent=2))
    await report(1.0, "done")
    return res


def make_export_job(request: ExportRequest, ctx: ExportContext) -> JobFn:
    async def job(jctx: JobContext) -> dict[str, Any]:
        result = await export_to_dir(request, ctx, jctx.dir, progress=jctx.progress)
        return result.to_manifest()

    return job


def header_from_settings(settings: Any, state: Any, site: Any | None) -> RinexHeader:
    if site is not None:
        approx: tuple[float, float, float] | None = (site.x, site.y, site.z)
    elif state is not None and state.position.ecef_x_m is not None:
        approx = (state.position.ecef_x_m, state.position.ecef_y_m, state.position.ecef_z_m)
    else:
        approx = None
    return RinexHeader(
        marker_name=settings.marker_name or settings.station_id,
        marker_number=settings.station_id,
        observer=settings.observer,
        agency=settings.agency,
        receiver_version=(state.firmware.fw_version if state is not None else "") or "unknown",
        antenna_type=settings.antenna_type,
        approx_xyz=approx,
        delta_hen=(settings.antenna_height_m, 0.0, 0.0),
        comment="mtrtk export",
    )
```

- [ ] **Step 5: Run tests, lint, commit**

`uv run pytest tests/unit/test_export.py -q` → `4 passed`; `uv run ruff check . && uv run ruff format . && uv run mypy`.
```bash
git add pyproject.toml uv.lock src/mtrtk/rinex/export.py tests/unit/test_export.py
git commit -m "feat(rinex): export pipeline with presets, decimation, Hatanaka/gzip and manifest

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: PPP result parsers

**Files:**
- Create: `src/mtrtk/rinex/ppp_result.py`, `tests/fixtures/ppp/csrs_sample.sum`, `tests/fixtures/ppp/csrs_sample.pos`, `tests/fixtures/ppp/auspos_sample.snx`, `tests/fixtures/ppp/opus_sample.txt`, `tests/unit/test_ppp_result.py`

**Interfaces:**
- Produces: `PppResult(source: Literal["csrs-ppp","auspos","opus","manual"], format: str, frame: str, epoch: str | None, x, y, z, sigma_x, sigma_y, sigma_z (1σ m, may be None), lat, lon, height_m, notes: list[str])` with `.suggested_site_name(station_id)`; `PppParseError(message, hint)`; `detect_format(filename, text) -> "csrs-sum" | "csrs-pos" | "sinex" | "opus"`; `parse_ppp_result(filename, content: bytes, prefer_frame="itrf") -> PppResult` (handles `.zip` by picking `.sum`, else `.pos`, else `.snx`/`.txt` inside).
- The fixtures are **reconstructions** of the services' formats built from their documentation and memory; the parsers are deliberately tolerant (keyword + number regexes). When the user's first real result arrives (Task 8), drop the real file into `tests/fixtures/ppp/` and adjust the regexes if a field is missed — that is expected, not a defect.

- [ ] **Step 1: Write the fixtures**

`tests/fixtures/ppp/csrs_sample.sum` (reconstructed):
```
 --------------------------------------------------------------------------------
 CSRS-PPP  Summary of the processing
 --------------------------------------------------------------------------------
 SECTION 1. FILE SUMMARY
 ...
 Observation file: MTRK00BGD_R_20262610000_01D_30S_MO.crx
 Processing mode : Static
 Frequency       : L1/L2
 Datum           : ITRF20 (epoch 2026.7137)

 SECTION 3. ESTIMATED POSITION COORDINATES

 3.2 Coordinate estimates

                                   ITRF20 (2026.7137)          Sigma(95%) (m)
 LATITUDE  (deg min sec)          N23 50 14.46220               0.0060
 LONGITUDE (deg min sec)          E90 15 45.18070               0.0090
 ELL. HEIGHT (m)                        -36.2680                0.0210

 3.3 Cartesian coordinates (m)
 X (m)                              -26748.1720                 0.0070
 Y (m)                             5837156.6184                 0.0150
 Z (m)                             2561801.2607                 0.0080

 SECTION 4. ...
```

`tests/fixtures/ppp/csrs_sample.pos` (reconstructed):
```
NOTE: Estimated positions are at the epoch of data
HDR GENERATED BY: CSRS-PPP ver.3.60.0 - 2026-09-19
DIR FRAME        STN         DOY YEAR-MM-DD HR:MN:SS.SSS NSV GDOP    RMSC(m)    RMSP(m)       DLAT(m)       DLON(m)       DHGT(m)         CLK(ns)   TZD(m)  SDLAT(95%)  SDLON(95%)  SDHGT(95%)  SDCLK(95%)  SDTZD(95%) LATDD LATMN    LATSS LONDD LONMN    LONSS   HGT(m) UTMZONE    UTM_EASTING   UTM_NORTHING UTM_SCLPNT UTM_SCLCMB
BWD ITRF20      MTRK         261 2026-09-18 00:00:00.000  18  1.6     0.0020     0.0900        0.0031        0.0020       -0.0100         12345.1    2.401      0.0060      0.0090      0.0210      0.3000      0.0100    23    50 14.46200    90    15 45.18000  -36.2700    46U    221150.294    2638912.702  0.99960   0.99961
BWD ITRF20      MTRK         261 2026-09-18 23:59:30.000  19  1.5     0.0021     0.0910        0.0002        0.0001       -0.0004         12345.6    2.398      0.0060      0.0090      0.0210      0.3000      0.0100    23    50 14.46220    90    15 45.18070  -36.2680    46U    221150.294    2638912.702  0.99960   0.99961
```

`tests/fixtures/ppp/auspos_sample.snx` (reconstructed, SINEX 2.02):
```
%=SNX 2.02 AUS 26:262:00000 AUS 26:261:00000 26:261:86370 P 00003 2 X
+SITE/ID
*CODE PT __DOMES__ T _STATION DESCRIPTION__ APPROX_LON_ APPROX_LAT_ _APP_H_
 MTRK  A           P mtrtk base             90 15 45.2  23 50 14.5   -36.3
-SITE/ID
+SOLUTION/ESTIMATE
*INDEX TYPE__ CODE PT SOLN _REF_EPOCH__ UNIT S __ESTIMATED VALUE____ _STD_DEV___
     1 STAX   MTRK  A    1 26:261:43200 m    2 -2.67481720000000e+04 4.00000e-03
     2 STAY   MTRK  A    1 26:261:43200 m    2  5.83715661840000e+06 6.00000e-03
     3 STAZ   MTRK  A    1 26:261:43200 m    2  2.56180126070000e+06 5.00000e-03
-SOLUTION/ESTIMATE
%ENDSNX
```

`tests/fixtures/ppp/opus_sample.txt` (reconstructed):
```
 FILE: mtrk261k.26o 000012345

 NGS OPUS SOLUTION REPORT
 ========================

                              USER: user@example.com                DATE: September 19, 2026
RINEX FILE: mtrk261k.26o                                     TIME: 01:23:45 UTC

  SOFTWARE: page5  2008.25 master349.pl 160321      START: 2026/09/18  10:00:00
 EPHEMERIS: igs24361.eph [precise]                   STOP: 2026/09/18  11:59:30
  NAV FILE: brdc2610.26n                          OBS USED: 12345 /  12800   :  96%
  ANT NAME: NONE            NONE                 # FIXED AMB:    98 /    101   :  97%
ARP HEIGHT: 0.000                                OVERALL RMS: 0.012(m)

 REF FRAME: NAD_83(2011)(EPOCH:2010.0000)              IGS20 (EPOCH:2026.7137)

         X:       -26748.900(m)   0.005(m)           -26748.172(m)   0.005(m)
         Y:      5837157.400(m)   0.009(m)          5837156.618(m)   0.009(m)
         Z:      2561801.900(m)   0.004(m)          2561801.261(m)   0.004(m)

       LAT:   23 50 14.47000      0.004(m)        23 50 14.46220      0.004(m)
     E LON:   90 15 45.19000      0.005(m)        90 15 45.18070      0.005(m)
     W LON:  269 44 14.81000      0.005(m)       269 44 14.81930      0.005(m)
    EL HGT:          -36.300(m)   0.010(m)               -36.268(m)   0.010(m)
```

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_ppp_result.py`:
```python
import io
import zipfile
from pathlib import Path

import pytest

from mtrtk.rinex.ppp_result import PppParseError, detect_format, parse_ppp_result

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "ppp"
X, Y, Z = -26748.1720, 5837156.6184, 2561801.2607


def test_detect_format() -> None:
    assert detect_format("x.sum", (FIX / "csrs_sample.sum").read_text()) == "csrs-sum"
    assert detect_format("x.pos", (FIX / "csrs_sample.pos").read_text()) == "csrs-pos"
    assert detect_format("x.snx", (FIX / "auspos_sample.snx").read_text()) == "sinex"
    assert detect_format("report.txt", (FIX / "opus_sample.txt").read_text()) == "opus"
    with pytest.raises(PppParseError):
        detect_format("x.txt", "hello world")


def test_csrs_sum() -> None:
    r = parse_ppp_result("MTRK.sum", (FIX / "csrs_sample.sum").read_bytes())
    assert r.source == "csrs-ppp" and r.format == "csrs-sum"
    assert (r.x, r.y, r.z) == pytest.approx((X, Y, Z), abs=1e-4)
    assert r.frame == "ITRF20" and r.epoch == "2026.7137"
    assert r.sigma_x == pytest.approx(0.0070 / 1.96, abs=1e-6) and r.sigma_z == pytest.approx(0.0080 / 1.96, abs=1e-6)
    assert r.lat == pytest.approx(23.8373506, abs=1e-7) and r.lon == pytest.approx(90.2625502, abs=1e-7) and r.height_m == pytest.approx(-36.268, abs=1e-3)
    assert any("95" in n for n in r.notes)


def test_csrs_pos_uses_last_epoch() -> None:
    r = parse_ppp_result("MTRK.pos", (FIX / "csrs_sample.pos").read_bytes())
    assert r.format == "csrs-pos" and r.frame == "ITRF20"
    assert r.lat == pytest.approx(23.8373506, abs=1e-7) and r.lon == pytest.approx(90.2625502, abs=1e-7) and r.height_m == pytest.approx(-36.268, abs=1e-4)
    assert (r.x, r.y, r.z) == pytest.approx((X, Y, Z), abs=0.02)  # computed from LLH
    assert r.sigma_y == pytest.approx(0.0090 / 1.96, abs=1e-6)


def test_sinex() -> None:
    r = parse_ppp_result("AUSPOS.SNX", (FIX / "auspos_sample.snx").read_bytes())
    assert r.source == "auspos" and r.format == "sinex"
    assert (r.x, r.y, r.z) == pytest.approx((X, Y, Z), abs=1e-4)
    assert (r.sigma_x, r.sigma_y, r.sigma_z) == (0.004, 0.006, 0.005)
    assert r.epoch == "2026.7137" or r.epoch.startswith("2026.7")
    assert r.lat == pytest.approx(23.8373506, abs=1e-6)


def test_opus_prefers_itrf_column() -> None:
    r = parse_ppp_result("opus.txt", (FIX / "opus_sample.txt").read_bytes())
    assert r.source == "opus" and r.frame == "IGS20" and r.epoch == "2026.7137"
    assert (r.x, r.y, r.z) == pytest.approx((X, Y, Z), abs=1e-3)
    assert r.sigma_x == 0.005
    nad = parse_ppp_result("opus.txt", (FIX / "opus_sample.txt").read_bytes(), prefer_frame="nad83")
    assert nad.frame.startswith("NAD_83") and nad.x == pytest.approx(-26748.900, abs=1e-3)


def test_zip_picks_sum_first() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("MTRK.pos", (FIX / "csrs_sample.pos").read_bytes())
        zf.writestr("MTRK.sum", (FIX / "csrs_sample.sum").read_bytes())
        zf.writestr("MTRK.pdf", b"%PDF-1.4 not parsed")
    r = parse_ppp_result("result.zip", buf.getvalue())
    assert r.format == "csrs-sum"


def test_garbage_raises_with_hint() -> None:
    with pytest.raises(PppParseError) as exc:
        parse_ppp_result("weird.dat", b"\x00\x01binary")
    assert "csrs" in exc.value.hint.lower()


def test_suggested_site_name() -> None:
    r = parse_ppp_result("MTRK.sum", (FIX / "csrs_sample.sum").read_bytes())
    assert r.suggested_site_name("MTRK") == "MTRK-csrs-ppp-2026.71"
```

- [ ] **Step 3: Run to verify failure** — FAIL.

- [ ] **Step 4: Write `src/mtrtk/rinex/ppp_result.py`**

```python
"""Parse the result files PPP services send back into one PppResult (ECEF + LLH + 1-sigma)."""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Literal

from mtrtk.core.geo import ecef_to_llh, llh_to_ecef

Source = Literal["csrs-ppp", "auspos", "opus", "manual"]
CSRS_95_TO_1SIGMA = 1.96
_NUM = r"[-+]?\d+(?:\.\d+)?"
_FRAME_RE = re.compile(r"(ITRF\s?\d{2,4}|IGS\s?\d{2}|IGb\d{2}|NAD_?83\S*)\s*\(?(?:EPOCH:?\s*)?(\d{4}\.\d+)?", re.IGNORECASE)


class PppParseError(ValueError):
    def __init__(self, message: str, hint: str = "Upload the CSRS-PPP .sum/.pos (or the .zip), an AUSPOS SINEX .snx, or the OPUS e-mail saved as .txt.") -> None:
        super().__init__(message)
        self.hint = hint


@dataclass
class PppResult:
    source: Source
    format: str
    frame: str
    epoch: str | None
    x: float
    y: float
    z: float
    sigma_x: float | None
    sigma_y: float | None
    sigma_z: float | None
    lat: float
    lon: float
    height_m: float
    notes: list[str] = field(default_factory=list)

    def suggested_site_name(self, station_id: str) -> str:
        epoch = f"-{self.epoch[:7]}" if self.epoch else ""
        return f"{station_id}-{self.source}{epoch}"


def _dms_to_deg(sign_token: str, d: str, m: str, s: str) -> float:
    value = abs(float(d)) + float(m) / 60 + float(s) / 3600
    negative = sign_token.upper() in ("S", "W") or d.strip().startswith("-")
    return -value if negative else value


def detect_format(filename: str, text: str) -> str:
    name = filename.lower()
    head = text[:4000]
    if "%=SNX" in head or "+SOLUTION/ESTIMATE" in text:
        return "sinex"
    if "DIR FRAME" in head and "LATDD" in text:
        return "csrs-pos"
    if "NGS OPUS" in head or ("REF FRAME:" in text and "EL HGT" in text):
        return "opus"
    if "CSRS-PPP" in head or ("LATITUDE" in text and "ELL. HEIGHT" in text.upper()) or name.endswith(".sum"):
        return "csrs-sum"
    raise PppParseError(f"could not recognise {filename} as a PPP result")


def _frame_epoch(text: str, default_frame: str) -> tuple[str, str | None]:
    m = _FRAME_RE.search(text)
    if not m:
        return default_frame, None
    return m.group(1).replace(" ", ""), m.group(2)


def _parse_csrs_sum(text: str) -> PppResult:
    def grab(label_re: str) -> list[str]:
        m = re.search(label_re + r"[^\n]*?(" + _NUM + r"(?:\s+" + _NUM + r")*)", text, re.IGNORECASE)
        return m.group(1).split() if m else []

    lat_m = re.search(r"LATITUDE[^\n]*?([NS-]?)\s*(" + _NUM + r")\s+(" + _NUM + r")\s+(" + _NUM + r")(?:\s+(" + _NUM + r"))?", text, re.IGNORECASE)
    lon_m = re.search(r"LONGITUDE[^\n]*?([EW-]?)\s*(" + _NUM + r")\s+(" + _NUM + r")\s+(" + _NUM + r")(?:\s+(" + _NUM + r"))?", text, re.IGNORECASE)
    hgt = grab(r"ELL(?:IPSOIDAL)?\.?\s*HEIGHT\s*\(m\)")
    xs, ys, zs = grab(r"\bX\s*\(m\)"), grab(r"\bY\s*\(m\)"), grab(r"\bZ\s*\(m\)")
    if not (lat_m and lon_m and hgt):
        raise PppParseError("CSRS-PPP summary: could not find LATITUDE / LONGITUDE / ELL. HEIGHT lines")
    lat = _dms_to_deg(lat_m.group(1), lat_m.group(2), lat_m.group(3), lat_m.group(4))
    lon = _dms_to_deg(lon_m.group(1), lon_m.group(2), lon_m.group(3), lon_m.group(4))
    height = float(hgt[0])
    notes = ["CSRS-PPP sigmas are 95 %; stored as 1σ (divided by 1.96)"]
    if xs and ys and zs:
        x, y, z = float(xs[0]), float(ys[0]), float(zs[0])
        sx = float(xs[1]) / CSRS_95_TO_1SIGMA if len(xs) > 1 else None
        sy = float(ys[1]) / CSRS_95_TO_1SIGMA if len(ys) > 1 else None
        sz = float(zs[1]) / CSRS_95_TO_1SIGMA if len(zs) > 1 else None
    else:
        x, y, z = llh_to_ecef(lat, lon, height)
        notes.append("Cartesian coordinates computed from LLH (not present in the summary)")
        s_lat = float(lat_m.group(5)) / CSRS_95_TO_1SIGMA if lat_m.group(5) else None
        s_lon = float(lon_m.group(5)) / CSRS_95_TO_1SIGMA if lon_m.group(5) else None
        s_h = float(hgt[1]) / CSRS_95_TO_1SIGMA if len(hgt) > 1 else None
        sx = sy = max(v for v in (s_lat, s_lon) if v is not None) if (s_lat or s_lon) else None
        sz = s_h
    frame, epoch = _frame_epoch(text, "ITRF2020")
    return PppResult("csrs-ppp", "csrs-sum", frame, epoch, x, y, z, sx, sy, sz, lat, lon, height, notes)


def _parse_csrs_pos(text: str) -> PppResult:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header_idx = next((i for i, ln in enumerate(lines) if ln.startswith("DIR") and "LATDD" in ln), None)
    if header_idx is None:
        raise PppParseError("CSRS-PPP .pos: header line with LATDD/LONDD columns not found")
    cols = lines[header_idx].split()
    rows = [ln.split() for ln in lines[header_idx + 1 :] if len(ln.split()) >= len(cols) - 2]
    if not rows:
        raise PppParseError("CSRS-PPP .pos: no epoch rows")
    last = rows[-1]
    col = {name: i for i, name in enumerate(cols)}
    lat = _dms_to_deg("", last[col["LATDD"]], last[col["LATMN"]], last[col["LATSS"]])
    lon = _dms_to_deg("", last[col["LONDD"]], last[col["LONMN"]], last[col["LONSS"]])
    height = float(last[col["HGT(m)"]])
    x, y, z = llh_to_ecef(lat, lon, height)
    def sigma(name: str) -> float | None:
        return float(last[col[name]]) / CSRS_95_TO_1SIGMA if name in col else None

    s_lat, s_lon, s_h = sigma("SDLAT(95%)"), sigma("SDLON(95%)"), sigma("SDHGT(95%)")
    frame = last[col["FRAME"]] if "FRAME" in col else "ITRF2020"
    epoch_txt = last[col["YEAR-MM-DD"]] if "YEAR-MM-DD" in col else None
    epoch = None
    if epoch_txt:
        yyyy, mm, dd = (int(v) for v in epoch_txt.split("-"))
        from datetime import date

        doy = date(yyyy, mm, dd).timetuple().tm_yday
        epoch = f"{yyyy + (doy - 0.5) / 365.25:.4f}"
    horizontal = [v for v in (s_lat, s_lon) if v is not None]
    sxy = max(horizontal) if horizontal else None
    notes = ["taken from the last epoch of the .pos file (static solution converges there)", "CSRS-PPP sigmas are 95 %; stored as 1σ"]
    return PppResult("csrs-ppp", "csrs-pos", frame, epoch, x, y, z, sxy, sxy, s_h, lat, lon, height, notes)


def _parse_sinex(text: str) -> PppResult:
    block = re.search(r"\+SOLUTION/ESTIMATE(.*?)-SOLUTION/ESTIMATE", text, re.S)
    if not block:
        raise PppParseError("SINEX: SOLUTION/ESTIMATE block not found")
    values: dict[str, tuple[float, float, str]] = {}
    for line in block.group(1).splitlines():
        parts = line.split()
        if len(parts) >= 9 and parts[1] in ("STAX", "STAY", "STAZ"):
            values[parts[1]] = (float(parts[8].replace("D", "E")), float(parts[9].replace("D", "E")) if len(parts) > 9 else 0.0, parts[5])
    if set(values) != {"STAX", "STAY", "STAZ"}:
        raise PppParseError("SINEX: STAX/STAY/STAZ estimates missing")
    x, sx, ref = values["STAX"]
    y, sy, _ = values["STAY"]
    z, sz, _ = values["STAZ"]
    epoch = None
    m = re.match(r"(\d{2}):(\d{3}):(\d{5})", ref)
    if m:
        yy, doy, sec = (int(v) for v in m.groups())
        year = 2000 + yy if yy < 80 else 1900 + yy
        epoch = f"{year + (doy - 1 + sec / 86400) / 365.25:.4f}"
    lat, lon, h = ecef_to_llh(x, y, z)
    frame, _ = _frame_epoch(text, "ITRF2020")
    return PppResult("auspos", "sinex", frame, epoch, x, y, z, sx, sy, sz, lat, lon, h, ["SINEX STD_DEV taken as 1σ"])


def _parse_opus(text: str, prefer_frame: str) -> PppResult:
    frames = re.search(r"REF FRAME:\s*(\S+)\s*\(EPOCH:\s*([\d.]+)\)\s+(\S+)\s*\(EPOCH:\s*([\d.]+)\)", text)
    if not frames:
        raise PppParseError("OPUS: REF FRAME line not found")
    use_second = prefer_frame != "nad83"
    frame = frames.group(3) if use_second else frames.group(1)
    epoch = frames.group(4) if use_second else frames.group(2)

    def axis(label: str) -> tuple[float, float]:
        m = re.search(rf"^\s*{label}:\s*(" + _NUM + r")\(m\)\s+(" + _NUM + r")\(m\)\s+(" + _NUM + r")\(m\)\s+(" + _NUM + r")\(m\)", text, re.M)
        if not m:
            raise PppParseError(f"OPUS: {label} line not found")
        return (float(m.group(3)), float(m.group(4))) if use_second else (float(m.group(1)), float(m.group(2)))

    (x, sx), (y, sy), (z, sz) = axis("X"), axis("Y"), axis("Z")
    lat, lon, h = ecef_to_llh(x, y, z)
    return PppResult("opus", "opus", frame, epoch, x, y, z, sx, sy, sz, lat, lon, h, [f"OPUS {frame} column used; sigmas taken as reported"])


def parse_ppp_result(filename: str, content: bytes, prefer_frame: str = "itrf") -> PppResult:
    if filename.lower().endswith(".zip") or content[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                names = zf.namelist()
                for ext in (".sum", ".pos", ".snx", ".txt"):
                    pick = next((n for n in names if n.lower().endswith(ext)), None)
                    if pick:
                        return parse_ppp_result(pick, zf.read(pick), prefer_frame)
        except zipfile.BadZipFile as exc:
            raise PppParseError("not a valid zip file") from exc
        raise PppParseError("zip contains no .sum, .pos, .snx or .txt result")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    if "\x00" in text[:1000]:
        raise PppParseError(f"{filename} looks binary, not a text result")
    fmt = detect_format(filename, text)
    if fmt == "csrs-sum":
        return _parse_csrs_sum(text)
    if fmt == "csrs-pos":
        return _parse_csrs_pos(text)
    if fmt == "sinex":
        return _parse_sinex(text)
    return _parse_opus(text, prefer_frame)
```

- [ ] **Step 5: Run tests, lint, commit**

`uv run pytest tests/unit/test_ppp_result.py -q` → `8 passed`.
```bash
git add src/mtrtk/rinex/ppp_result.py tests/fixtures/ppp tests/unit/test_ppp_result.py
git commit -m "feat(rinex): tolerant parsers for CSRS-PPP, AUSPOS SINEX and OPUS results

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Export and PPP import API

**Files:**
- Create: `src/mtrtk/web/api/export.py`, `tests/unit/test_web_export.py`
- Modify: `src/mtrtk/web/api/base.py` (PPP import endpoint; `SiteBody` gains `sigma_x/sigma_y/sigma_z`), `src/mtrtk/web/app.py` (add `export` to the router list), `src/mtrtk/store/models.py` (`Site.from_ecef` accepts `sigmas=(sx, sy, sz)`)

**Interfaces:**
- Produces: `GET /api/export/presets` → `[{id, name, description, service_url, version, interval_s, exclude_systems, hatanaka, gzip, constraints, adjustable}]`; `POST /api/export` body `ExportRequest` → `Job` JSON (404 when no raw logs cover the window, 409 without a job runner); `GET /api/export/rinex?from&to&preset=generic&interval=&hatanaka=&gzip=` → zip stream of the export (window ≤ 6 h, 404 no data, 422 bad params), `Content-Disposition` with the obs base name; `POST /api/base/ppp/import` multipart `file` (+ optional `prefer_frame`) → `PppResult` JSON + `suggested_name` (422 with `hint` on parse failure, 413 above 20 MB). `SiteBody` accepts `sigma_x, sigma_y, sigma_z` (per-axis) in addition to `sigma_m`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_web_export.py`:
```python
import asyncio
import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from webtest import client, make_ctx, make_log

from mtrtk.jobs import JobRunner
from mtrtk.rinex.convbin import convbin_available
from mtrtk.web.app import create_app

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_raw_60s.ubx"
PPP = Path(__file__).resolve().parents[1] / "fixtures" / "ppp"
H0 = datetime(2026, 9, 18, 10, tzinfo=UTC)


@pytest.fixture
async def ctx(tmp_path: Path):
    c = await make_ctx(tmp_path)
    c.jobs = JobRunner(c.db, c.bus, tmp_path / "jobs")
    try:
        yield c
    finally:
        await c.jobs.shutdown()
        await c.db.close()


async def test_presets(ctx) -> None:
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/export/presets")).json()
    assert [p["id"] for p in body] == ["csrs-ppp", "auspos", "opus", "generic"]
    assert body[0]["hatanaka"] is True and body[3]["adjustable"] is True


async def test_export_404_without_logs(ctx) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/export", json={"start": H0.isoformat(), "end": (H0 + timedelta(hours=1)).isoformat(), "preset": "generic"})
    assert r.status_code == 404


async def test_export_validation(ctx) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/export", json={"start": H0.isoformat(), "end": H0.isoformat(), "preset": "generic"})
        assert r.status_code == 422
        r = await c.post("/api/export", json={"start": H0.isoformat(), "end": (H0 + timedelta(hours=1)).isoformat(), "preset": "csrs-ppp", "interval_s": 1})
        assert r.status_code == 422


@pytest.mark.skipif(not convbin_available() or not FIXTURE.exists(), reason="convbin or fixture missing")
async def test_export_job_runs_and_files_download(ctx, tmp_path: Path) -> None:
    from test_export import fixture_window, install_fixture_as_log  # helpers from Task 3's test module

    start, end = fixture_window()
    install_fixture_as_log(tmp_path, start)
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/export", json={"start": start.isoformat(), "end": end.isoformat(), "preset": "generic"})
        assert r.status_code == 200
        job_id = r.json()["id"]
        for _ in range(200):
            await asyncio.sleep(0.05)
            job = (await c.get(f"/api/jobs/{job_id}")).json()
            if job["status"] in ("done", "failed"):
                break
        assert job["status"] == "done", job
        files = (await c.get(f"/api/jobs/{job_id}/files")).json()
        names = [f["name"] for f in files]
        assert any(n.endswith("_MO.rnx") for n in names) and "manifest.json" in names
        obs = next(n for n in names if n.endswith("_MO.rnx"))
        assert (await c.get(f"/api/jobs/{job_id}/files/{obs}")).text.startswith("     3.04")


@pytest.mark.skipif(not convbin_available() or not FIXTURE.exists(), reason="convbin or fixture missing")
async def test_sync_rinex_zip(ctx, tmp_path: Path) -> None:
    from test_export import fixture_window, install_fixture_as_log

    start, end = fixture_window()
    install_fixture_as_log(tmp_path, start)
    async with client(create_app(ctx)) as c:
        r = await c.get("/api/export/rinex", params={"from": start.isoformat(), "to": end.isoformat(), "preset": "generic", "interval": 10})
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            assert any(n.endswith("_10S_MO.rnx") for n in zf.namelist()) and "manifest.json" in zf.namelist()
        too_long = await c.get("/api/export/rinex", params={"from": start.isoformat(), "to": (start + timedelta(hours=7)).isoformat()})
        assert too_long.status_code == 422


async def test_ppp_import_preview_and_site_with_axis_sigmas(ctx) -> None:
    async with client(create_app(ctx)) as c:
        files = {"file": ("MTRK.sum", (PPP / "csrs_sample.sum").read_bytes(), "text/plain")}
        r = await c.post("/api/base/ppp/import", files=files)
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "csrs-ppp" and abs(body["x"] - (-26748.172)) < 1e-3 and body["suggested_name"] == "MTRK-csrs-ppp-2026.71"
        bad = await c.post("/api/base/ppp/import", files={"file": ("x.txt", b"nothing useful", "text/plain")})
        assert bad.status_code == 422 and "hint" in bad.json()["detail"]
        site = await c.post("/api/base/sites", json={"name": body["suggested_name"], "x": body["x"], "y": body["y"], "z": body["z"], "sigma_x": body["sigma_x"], "sigma_y": body["sigma_y"], "sigma_z": body["sigma_z"], "source": body["source"], "frame": body["frame"], "epoch": body["epoch"]})
        assert site.status_code == 200 and site.json()["sigma_y"] == pytest.approx(0.015 / 1.96, abs=1e-6)
```
Make the Task 3 test helpers importable: keep `fixture_window` and `install_fixture_as_log` as module-level functions in `tests/unit/test_export.py` (they already are; `pythonpath = ["tests"]` plus `tests/unit` being the rootdir of collection lets `from test_export import …` work — if not, move them to `tests/unit/webtest.py`).

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Extend `Site.from_ecef` and `SiteBody`**

In `src/mtrtk/store/models.py`, add a keyword `sigmas: tuple[float | None, float | None, float | None] | None = None` to `from_ecef`; when given it overrides `sigma_m` per axis. In `src/mtrtk/web/api/base.py`, add `sigma_x: float | None = None`, `sigma_y`, `sigma_z` to `SiteBody` and pass `sigmas=(self.sigma_x, self.sigma_y, self.sigma_z)` when any is set. Add the import endpoint to `base.py`:
```python
from fastapi import File, UploadFile

from mtrtk.rinex.ppp_result import PppParseError, parse_ppp_result

MAX_UPLOAD = 20 * 1024 * 1024


@router.post("/ppp/import")
async def ppp_import(request: Request, file: UploadFile = File(...), prefer_frame: str = "itrf") -> dict[str, Any]:
    content = await file.read(MAX_UPLOAD + 1)
    if len(content) > MAX_UPLOAD:
        raise HTTPException(413, "file larger than 20 MB")
    try:
        result = parse_ppp_result(file.filename or "result", content, prefer_frame=prefer_frame)
    except PppParseError as exc:
        raise HTTPException(422, {"message": str(exc), "hint": exc.hint}) from exc
    body = result.__dict__ | {"suggested_name": result.suggested_site_name(request.app.state.ctx.settings.station_id)}
    return body
```

- [ ] **Step 4: Write `src/mtrtk/web/api/export.py`**

```python
"""RINEX export: presets, background export jobs, and a bounded synchronous zip endpoint."""

from __future__ import annotations

import io
import tempfile
import zipfile
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from mtrtk.rawlog.index import files_for_window
from mtrtk.rinex.export import ExportContext, ExportRequest, export_to_dir, header_from_settings, make_export_job
from mtrtk.rinex.presets import PRESETS
from mtrtk.rinex.splice import NoDataError
from mtrtk.store.repos import SitesRepo

router = APIRouter(prefix="/api/export", tags=["export"])
SYNC_MAX = timedelta(hours=6)


async def _context(request: Request) -> ExportContext:
    ctx = request.app.state.ctx
    site = await SitesRepo(ctx.db).active()
    header = header_from_settings(ctx.settings, ctx.store.state, site)
    return ExportContext(root=ctx.settings.data_dir, station_id=ctx.settings.station_id, country=ctx.settings.country, header=header)


def _check_window(root: Path, req: ExportRequest) -> None:
    if not files_for_window(root, req.start - timedelta(hours=1), req.end):
        raise HTTPException(404, "no raw logs cover that window")


@router.get("/presets")
async def presets() -> list[dict[str, Any]]:
    return [asdict(p) for p in PRESETS.values()]


@router.post("")
async def submit(req: ExportRequest, request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    if ctx.jobs is None:
        raise HTTPException(409, "job runner not available")
    _check_window(ctx.settings.data_dir, req)
    export_ctx = await _context(request)
    job = await ctx.jobs.submit("export", req.model_dump(mode="json"), make_export_job(req, export_ctx))
    return job.model_dump(mode="json")


@router.get("/rinex")
async def rinex_zip(request: Request, from_: str = Query(alias="from"), to: str = Query(), preset: str = "generic", interval: float | None = None, hatanaka: bool | None = None, gzip: bool | None = None) -> StreamingResponse:
    try:
        req = ExportRequest(start=datetime.fromisoformat(from_), end=datetime.fromisoformat(to), preset=preset, interval_s=interval, hatanaka=hatanaka, gzip=gzip)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    if req.end - req.start > SYNC_MAX:
        raise HTTPException(422, "synchronous export is limited to 6 h; use POST /api/export for longer windows")
    ctx = request.app.state.ctx
    _check_window(ctx.settings.data_dir, req)
    export_ctx = await _context(request)
    with tempfile.TemporaryDirectory(prefix="mtrtk-export-") as tmp:
        try:
            result = await export_to_dir(req, export_ctx, Path(tmp))
        except NoDataError as exc:
            raise HTTPException(404, str(exc)) from exc
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in result.files:
                zf.write(Path(tmp) / f["name"], f["name"])
    buf.seek(0)
    stem = next(f["name"] for f in result.files if f["role"] == "obs").split(".")[0]
    return StreamingResponse(buf, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{stem}.zip"'})
```
Add `"export"` to the router name list in `web/app.py` (`_include_api_routers`).

- [ ] **Step 5: Run tests, lint, commit**

`uv run pytest tests/unit/test_web_export.py tests/unit/test_web_base.py -q` → all pass.
```bash
git add src/mtrtk/web/api/export.py src/mtrtk/web/api/base.py src/mtrtk/web/app.py src/mtrtk/store/models.py tests/unit/test_web_export.py
git commit -m "feat(web): RINEX export jobs, synchronous zip export and PPP result import endpoints

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: CLI — `mtrtk export` and `mtrtk ppp-import`

**Files:**
- Modify: `src/mtrtk/cli.py`
- Create: `tests/unit/test_cli_export.py`

**Interfaces:**
- Produces: `mtrtk export --from ISO --to ISO [--preset csrs-ppp] [--interval S] [--hatanaka/--no-hatanaka] [--gzip/--no-gzip] --out DIR` — runs `export_to_dir` with the header from settings/active site, prints each output file with size and any warnings, exit 1 on `NoDataError`/`ConvbinError`; `mtrtk ppp-import FILE [--prefer-frame itrf|nad83] [--save-site NAME] [--activate]` — prints the parsed result as a table and optionally stores/activates a site.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_cli_export.py`:
```python
from pathlib import Path

import pytest
from click.testing import CliRunner

from mtrtk.cli import main
from mtrtk.rinex.convbin import convbin_available

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_raw_60s.ubx"
PPP = Path(__file__).resolve().parents[1] / "fixtures" / "ppp"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    return tmp_path


@pytest.mark.skipif(not convbin_available() or not FIXTURE.exists(), reason="convbin or fixture missing")
def test_export_cli_writes_files(env: Path) -> None:
    from test_export import fixture_window, install_fixture_as_log

    start, end = fixture_window()
    install_fixture_as_log(env, start)
    out = env / "exp"
    r = CliRunner().invoke(main, ["export", "--from", start.isoformat(), "--to", end.isoformat(), "--preset", "generic", "--interval", "10", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "_10S_MO.rnx" in r.output and "manifest.json" in r.output
    assert (out / "manifest.json").exists()


def test_export_cli_no_data(env: Path) -> None:
    r = CliRunner().invoke(main, ["export", "--from", "2026-09-18T10:00:00+00:00", "--to", "2026-09-18T11:00:00+00:00", "--out", str(env / "x")])
    assert r.exit_code == 1 and "no raw logs" in r.output


def test_export_cli_rejects_bad_window(env: Path) -> None:
    r = CliRunner().invoke(main, ["export", "--from", "2026-09-18T10:00:00+00:00", "--to", "2026-09-18T10:00:00+00:00", "--out", str(env / "x")])
    assert r.exit_code != 0 and "end must be after start" in r.output


def test_ppp_import_cli_prints_and_saves_site(env: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(main, ["ppp-import", str(PPP / "csrs_sample.sum")])
    assert r.exit_code == 0, r.output
    assert "ITRF20" in r.output and "-26748.1720" in r.output and "csrs-ppp" in r.output
    r = runner.invoke(main, ["ppp-import", str(PPP / "csrs_sample.sum"), "--save-site", "roof", "--activate"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(main, ["sites", "list"])
    assert "* roof" in r.output and "csrs-ppp" in r.output


def test_ppp_import_cli_bad_file(env: Path) -> None:
    bad = env / "bad.txt"
    bad.write_text("nothing here")
    r = CliRunner().invoke(main, ["ppp-import", str(bad)])
    assert r.exit_code == 1 and "could not recognise" in r.output
```

- [ ] **Step 2: Run to verify failure** — FAIL (`No such command 'export'`).

- [ ] **Step 3: Add the commands to `src/mtrtk/cli.py`**

```python
@main.command()
@click.option("--from", "start", required=True, help="Window start, ISO-8601 with timezone (e.g. 2026-09-18T00:00:00Z).")
@click.option("--to", "end", required=True, help="Window end, ISO-8601 with timezone.")
@click.option("--preset", default="csrs-ppp", show_default=True, type=click.Choice(["csrs-ppp", "auspos", "opus", "generic"]))
@click.option("--interval", type=float, default=None, help="Observation interval in seconds (generic preset only).")
@click.option("--hatanaka/--no-hatanaka", default=None, help="Hatanaka-compress the observation file (generic preset only).")
@click.option("--gzip/--no-gzip", default=None, help="gzip the output files (generic preset only).")
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False, path_type=Path))
def export(start: str, end: str, preset: str, interval: float | None, hatanaka: bool | None, gzip: bool | None, out_dir: Path) -> None:
    """Export a raw-log window as RINEX for a PPP service or other post-processing."""
    from datetime import datetime

    from pydantic import ValidationError

    from mtrtk.rinex.convbin import ConvbinError
    from mtrtk.rinex.export import ExportContext, ExportRequest, export_to_dir, header_from_settings
    from mtrtk.rinex.splice import NoDataError
    from mtrtk.store.db import Database
    from mtrtk.store.repos import SitesRepo

    settings = _load_settings(ntrip_password="")
    try:
        request = ExportRequest(start=datetime.fromisoformat(start.replace("Z", "+00:00")), end=datetime.fromisoformat(end.replace("Z", "+00:00")), preset=preset, interval_s=interval, hatanaka=hatanaka, gzip=gzip)
    except (ValidationError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    async def go() -> None:
        db = Database(settings.data_dir / "mtrtk.db")
        await db.open()
        try:
            site = await SitesRepo(db).active()
        finally:
            await db.close()
        ctx = ExportContext(root=settings.data_dir, station_id=settings.station_id, country=settings.country, header=header_from_settings(settings, None, site))

        async def progress(p: float, msg: str | None) -> None:
            click.echo(f"[{p * 100:3.0f}%] {msg or ''}")

        try:
            result = await export_to_dir(request, ctx, out_dir, progress=progress)
        except (NoDataError, ConvbinError) as exc:
            raise click.ClickException(str(exc)) from exc
        for f in result.files:
            click.echo(f"{f['name']:<48} {f['bytes']:>10} bytes  ({f['role']})")
        click.echo(f"{result.obs_epochs} observation epochs, {result.nav_messages} navigation messages, RINEX {result.version}")
        for w in result.warnings:
            click.echo(f"warning: {w}")

    asyncio.run(go())


@main.command("ppp-import")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--prefer-frame", type=click.Choice(["itrf", "nad83"]), default="itrf", show_default=True)
@click.option("--save-site", "site_name", default=None, help="Store the result as a site with this name.")
@click.option("--activate", is_flag=True, help="Also make it the active site (used when BASE_MODE=fixed).")
def ppp_import(file: Path, prefer_frame: str, site_name: str | None, activate: bool) -> None:
    """Parse a PPP result (CSRS-PPP .sum/.pos/.zip, AUSPOS SINEX, OPUS text) and optionally save it as a site."""
    from mtrtk.rinex.ppp_result import PppParseError, parse_ppp_result
    from mtrtk.store.models import Site
    from mtrtk.store.repos import SitesRepo

    try:
        result = parse_ppp_result(file.name, file.read_bytes(), prefer_frame=prefer_frame)
    except PppParseError as exc:
        raise click.ClickException(f"{exc} — {exc.hint}") from exc
    click.echo(f"source   {result.source} ({result.format})")
    click.echo(f"frame    {result.frame}{' @ ' + result.epoch if result.epoch else ''}")
    click.echo(f"X        {result.x:15.4f} m   σ {result.sigma_x if result.sigma_x is None else f'{result.sigma_x:.4f}'}")
    click.echo(f"Y        {result.y:15.4f} m   σ {result.sigma_y if result.sigma_y is None else f'{result.sigma_y:.4f}'}")
    click.echo(f"Z        {result.z:15.4f} m   σ {result.sigma_z if result.sigma_z is None else f'{result.sigma_z:.4f}'}")
    click.echo(f"lat/lon  {result.lat:.9f}  {result.lon:.9f}   h {result.height_m:.4f} m")
    for note in result.notes:
        click.echo(f"note: {note}")
    if not site_name:
        return

    async def go(db) -> None:  # type: ignore[no-untyped-def]
        repo = SitesRepo(db)
        try:
            site = await repo.add(Site.from_ecef(site_name, result.x, result.y, result.z, source=result.source, frame=result.frame, epoch=result.epoch, notes=f"imported from {file.name}", sigmas=(result.sigma_x, result.sigma_y, result.sigma_z)))
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        if activate:
            await repo.activate(site.name)
        click.echo(f"saved site {site.name}{' (active)' if activate else ''}")

    _with_db(go)
```

- [ ] **Step 4: Run tests, lint, commit**

`uv run pytest tests/unit/test_cli_export.py tests/unit/test_cli_sites.py -q` → all pass.
```bash
git add src/mtrtk/cli.py tests/unit/test_cli_export.py
git commit -m "feat(cli): export and ppp-import commands

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: UI — export panel, jobs panel, PPP import dialog

**Files:**
- Create: `web/src/components/ExportPanel.tsx`, `web/src/components/JobsPanel.tsx`, `web/src/components/PppImportDialog.tsx`, `web/src/components/ExportPanel.test.tsx`, `web/src/components/PppImportDialog.test.tsx`
- Modify: `web/src/pages/Logs.tsx` (add ExportPanel + JobsPanel; read `?export=<preset>&hours=<n>`), `web/src/pages/Site.tsx` (step 2 → link `/logs?export=csrs-ppp&hours=24`; step 4 → `PppImportDialog`), `web/src/lib/types.ts` (`Preset`, `PppResult`), `web/src/lib/queries.ts` (`usePresets`)

**Interfaces:**
- Produces: `ExportPanel({initialPreset?, initialHours?, window?: [from,to], onSubmitted?})` — preset select (name + description + constraints), from/to inputs (UTC), generic-only options (interval, Hatanaka, gzip), warnings from constraints, `POST /api/export`; `JobsPanel({kind: "export" | "ppk"})` — merges `useJobs(kind)` with live `useLive().jobs`, shows status badge, progress bar (`Progress`), message, file list with download links to `/api/jobs/{id}/files/{name}`, warnings from `result.warnings`, delete; `PppImportDialog({onSaved})` — file input → `POST /api/base/ppp/import` (FormData) → preview (source/frame/epoch, X/Y/Z ± σ, lat/lon/h, notes) → name (prefilled `suggested_name`) + "activate" checkbox → `POST /api/base/sites` with per-axis sigmas → optional activate → toast + `onSaved()`.

- [ ] **Step 1: Types and query**

Append to `web/src/lib/types.ts`:
```ts
export interface Preset { id: string; name: string; service_url: string; description: string; version: string; interval_s: number | null; exclude_systems: string[]; hatanaka: boolean; gzip: boolean; constraints: string[]; adjustable: boolean }
export interface PppResult { source: string; format: string; frame: string; epoch: string | null; x: number; y: number; z: number; sigma_x: number | null; sigma_y: number | null; sigma_z: number | null; lat: number; lon: number; height_m: number; notes: string[]; suggested_name: string }
```
Append to `web/src/lib/queries.ts`: `export const usePresets = () => useQuery({ queryKey: ["export", "presets"], queryFn: () => get<Preset[]>("/api/export/presets"), staleTime: Infinity });` (import `Preset`).

- [ ] **Step 2: Write the failing tests**

`web/src/components/ExportPanel.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ExportPanel } from "./ExportPanel";

const presets = [
  { id: "csrs-ppp", name: "CSRS-PPP (NRCan)", service_url: "https://x", description: "Free global PPP.", version: "3.04", interval_s: 30, exclude_systems: [], hatanaka: true, gzip: true, constraints: ["24 h of data recommended"], adjustable: false },
  { id: "generic", name: "Generic RINEX 3.04", service_url: "", description: "Full-rate.", version: "3.04", interval_s: null, exclude_systems: [], hatanaka: false, gzip: false, constraints: [], adjustable: true },
];
let calls: [string, RequestInit | undefined][] = [];

describe("ExportPanel", () => {
  beforeEach(() => {
    calls = [];
    globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      calls.push([String(url), init]);
      if (String(url).endsWith("/api/export/presets")) return new Response(JSON.stringify(presets), { status: 200 });
      if (init?.method === "POST") return new Response(JSON.stringify({ id: "job1", kind: "export", status: "queued", created_utc: "", updated_utc: null, progress: 0, message: null, params: {}, result: null, error: null }), { status: 200 });
      return new Response("[]", { status: 200 });
    }) as typeof fetch;
  });

  it("submits a csrs-ppp export for the last 24 h and hides generic options", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const onSubmitted = vi.fn();
    render(<QueryClientProvider client={qc}><ExportPanel initialPreset="csrs-ppp" initialHours={24} onSubmitted={onSubmitted} /></QueryClientProvider>);
    expect(await screen.findByText(/24 h of data recommended/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/interval/i)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /start export/i }));
    const post = calls.find(([, i]) => i?.method === "POST")!;
    const body = JSON.parse(post[1]!.body as string);
    expect(body.preset).toBe("csrs-ppp");
    expect(Date.parse(body.end) - Date.parse(body.start)).toBe(24 * 3600 * 1000);
    expect(onSubmitted).toHaveBeenCalledWith("job1");
  });

  it("shows generic options when the generic preset is picked", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><ExportPanel initialPreset="generic" /></QueryClientProvider>);
    expect(await screen.findByLabelText(/interval/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/hatanaka/i)).toBeInTheDocument();
  });
});
```

`web/src/components/PppImportDialog.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { PppImportDialog } from "./PppImportDialog";

const result = { source: "csrs-ppp", format: "csrs-sum", frame: "ITRF20", epoch: "2026.7137", x: -26748.172, y: 5837156.6184, z: 2561801.2607, sigma_x: 0.0036, sigma_y: 0.0077, sigma_z: 0.0041, lat: 23.8373506, lon: 90.2625502, height_m: -36.268, notes: ["CSRS-PPP sigmas are 95 %; stored as 1σ"], suggested_name: "MTRK-csrs-ppp-2026.71" };
let calls: [string, RequestInit | undefined][] = [];

describe("PppImportDialog", () => {
  beforeEach(() => {
    calls = [];
    globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      calls.push([String(url), init]);
      if (String(url).endsWith("/api/base/ppp/import")) return new Response(JSON.stringify(result), { status: 200 });
      if (String(url).endsWith("/api/base/sites")) return new Response(JSON.stringify({ name: "MTRK-csrs-ppp-2026.71", active: false }), { status: 200 });
      if (String(url).endsWith("/activate")) return new Response(JSON.stringify({ name: "MTRK-csrs-ppp-2026.71", active: true }), { status: 200 });
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
  });

  it("uploads, previews and saves an active site", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const onSaved = vi.fn();
    render(<QueryClientProvider client={qc}><PppImportDialog onSaved={onSaved} /></QueryClientProvider>);
    await userEvent.click(screen.getByRole("button", { name: /import ppp result/i }));
    const file = new File(["fake"], "MTRK.sum", { type: "text/plain" });
    await userEvent.upload(screen.getByLabelText(/result file/i), file);
    expect(await screen.findByText("ITRF20 @ 2026.7137")).toBeInTheDocument();
    expect(screen.getByDisplayValue("MTRK-csrs-ppp-2026.71")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /save site/i }));
    const post = calls.find(([u, i]) => u.endsWith("/api/base/sites") && i?.method === "POST")!;
    expect(JSON.parse(post[1]!.body as string)).toMatchObject({ name: "MTRK-csrs-ppp-2026.71", x: -26748.172, sigma_y: 0.0077, frame: "ITRF20", epoch: "2026.7137", source: "csrs-ppp" });
    expect(calls.some(([u]) => u.endsWith("/activate"))).toBe(true);
    expect(onSaved).toHaveBeenCalled();
  });
});
```

- [ ] **Step 3: Run to verify failure** — FAIL.

- [ ] **Step 4: Write `web/src/components/ExportPanel.tsx`**

```tsx
import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { post } from "@/lib/api";
import { usePresets } from "@/lib/queries";
import type { Job } from "@/lib/types";

const toInput = (d: Date) => d.toISOString().slice(0, 16);

export function ExportPanel({ initialPreset = "csrs-ppp", initialHours = 24, window, onSubmitted }: { initialPreset?: string; initialHours?: number; window?: [string, string]; onSubmitted?: (jobId: string) => void }) {
  const presets = usePresets();
  const [preset, setPreset] = useState(initialPreset);
  const [now] = useState(() => new Date(Math.floor(Date.now() / 3600_000) * 3600_000));
  const [from, setFrom] = useState(window?.[0] ?? toInput(new Date(now.getTime() - initialHours * 3600_000)));
  const [to, setTo] = useState(window?.[1] ?? toInput(now));
  const [interval, setInterval] = useState("");
  const [hatanaka, setHatanaka] = useState(false);
  const [gz, setGz] = useState(false);
  useEffect(() => { if (window) { setFrom(window[0]); setTo(window[1]); } }, [window]);
  const chosen = presets.data?.find((p) => p.id === preset);
  const submit = useMutation({
    mutationFn: () => post<Job>("/api/export", { start: `${from}:00Z`, end: `${to}:00Z`, preset, ...(chosen?.adjustable ? { interval_s: interval ? Number(interval) : null, hatanaka, gzip: gz } : {}) }),
    onSuccess: (job) => { toast.success("Export started"); onSubmitted?.(job.id); },
    onError: (e) => toast.error(String(e)),
  });
  return (
    <div className="flex flex-col gap-3">
      <div>
        <Label htmlFor="export-preset">Target</Label>
        <select id="export-preset" value={preset} onChange={(e) => setPreset(e.target.value)} className="mt-1 w-full rounded-md border border-line bg-panel-2 px-2 py-1.5 text-[14px]">
          {(presets.data ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        {chosen ? <p className="mt-1 text-[12px] leading-4 text-ink-2">{chosen.description}</p> : null}
        {chosen?.constraints.length ? <ul className="mt-1 list-disc pl-4 text-[12px] leading-4 text-ink-2">{chosen.constraints.map((c) => <li key={c}>{c}</li>)}</ul> : null}
      </div>
      <div className="grid grid-cols-2 gap-2">
        <div><Label htmlFor="export-from">From (UTC)</Label><Input id="export-from" type="datetime-local" value={from} onChange={(e) => setFrom(e.target.value)} /></div>
        <div><Label htmlFor="export-to">To (UTC)</Label><Input id="export-to" type="datetime-local" value={to} onChange={(e) => setTo(e.target.value)} /></div>
      </div>
      {chosen?.adjustable ? (
        <div className="grid grid-cols-3 items-end gap-2">
          <div><Label htmlFor="export-interval">Interval (s)</Label><Input id="export-interval" inputMode="decimal" placeholder="native" value={interval} onChange={(e) => setInterval(e.target.value)} /></div>
          <label className="flex items-center gap-2 text-[14px]"><Switch aria-label="Hatanaka compression" checked={hatanaka} onCheckedChange={setHatanaka} />Hatanaka</label>
          <label className="flex items-center gap-2 text-[14px]"><Switch aria-label="gzip" checked={gz} onCheckedChange={setGz} />gzip</label>
        </div>
      ) : chosen ? <p className="text-[12px] leading-4 text-ink-2">RINEX {chosen.version}{chosen.interval_s ? `, ${chosen.interval_s} s interval` : ""}{chosen.hatanaka ? ", Hatanaka + gzip" : chosen.gzip ? ", gzip" : ""}{chosen.exclude_systems.length ? ", GPS only" : ""}.</p> : null}
      <div className="flex items-center justify-between gap-3">
        {chosen?.service_url ? <a href={chosen.service_url} target="_blank" rel="noreferrer" className="text-brass">Open {chosen.name}</a> : <span />}
        <Button onClick={() => submit.mutate()} disabled={submit.isPending || !chosen}>Start export</Button>
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Write `web/src/components/JobsPanel.tsx`**

```tsx
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { StatusBadge } from "@/components/StatusBadge";
import { del } from "@/lib/api";
import { fmtBytes, relTime } from "@/lib/format";
import { useLive } from "@/lib/live";
import { useJobs } from "@/lib/queries";
import type { Job } from "@/lib/types";

const LEVEL = { queued: "warning", running: "warning", done: "good", failed: "critical" } as const;

export function JobsPanel({ kind }: { kind: "export" | "ppk" }) {
  const qc = useQueryClient();
  const fetched = useJobs(kind);
  const live = useLive((s) => s.jobs);
  const jobs: Job[] = Object.values({ ...Object.fromEntries((fetched.data ?? []).map((j) => [j.id, j])), ...Object.fromEntries(Object.values(live).filter((j) => j.kind === kind).map((j) => [j.id, j])) }).sort((a, b) => (a.created_utc < b.created_utc ? 1 : -1));
  const remove = useMutation({ mutationFn: (id: string) => del(`/api/jobs/${id}`), onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }) });
  if (jobs.length === 0) return <p className="text-ink-2">No {kind} jobs yet.</p>;
  return (
    <ul className="flex flex-col gap-3">
      {jobs.map((j) => {
        const files = (j.result?.files as { name: string; bytes: number; role: string }[] | undefined) ?? [];
        const warnings = (j.result?.warnings as string[] | undefined) ?? [];
        return (
          <li key={j.id} className="rounded-md border border-line p-3">
            <div className="flex flex-wrap items-center gap-3">
              <StatusBadge level={LEVEL[j.status]} label={j.status} />
              <span className="num text-ink-2">{String(j.params.preset ?? j.kind)} · {relTime(j.created_utc)}</span>
              <span className="ml-auto text-[12px] text-ink-2">{j.message ?? ""}</span>
              <Button size="sm" variant="ghost" onClick={() => remove.mutate(j.id)}>Delete</Button>
            </div>
            {j.status === "running" || j.status === "queued" ? <Progress value={j.progress * 100} className="mt-2" /> : null}
            {j.error ? <p className="mt-2 text-status-critical">{j.error}</p> : null}
            {files.length ? <ul className="mt-2 flex flex-wrap gap-2">{files.map((f) => <li key={f.name}><a className="num text-brass" href={`/api/jobs/${j.id}/files/${encodeURIComponent(f.name)}`} download>{f.name}</a> <span className="text-[12px] text-ink-2">{fmtBytes(f.bytes)}</span></li>)}</ul> : null}
            {warnings.length ? <ul className="mt-2 list-disc pl-4 text-[12px] leading-4 text-status-warning">{warnings.map((w) => <li key={w}>{w}</li>)}</ul> : null}
          </li>
        );
      })}
    </ul>
  );
}
```

- [ ] **Step 6: Write `web/src/components/PppImportDialog.tsx`**

```tsx
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Stat } from "@/components/Stat";
import { api, post } from "@/lib/api";
import { fmtDms } from "@/lib/format";
import type { PppResult, Site } from "@/lib/types";

const sig = (v: number | null) => (v == null ? "—" : `± ${(v * 1000).toFixed(1)} mm`);

export function PppImportDialog({ onSaved }: { onSaved?: (site: Site) => void }) {
  const [open, setOpen] = useState(false);
  const [result, setResult] = useState<PppResult | null>(null);
  const [name, setName] = useState("");
  const [activate, setActivate] = useState(true);
  const upload = useMutation({
    mutationFn: async (file: File) => { const fd = new FormData(); fd.append("file", file); return api<PppResult>("/api/base/ppp/import", { method: "POST", body: fd }); },
    onSuccess: (r) => { setResult(r); setName(r.suggested_name); },
    onError: (e) => toast.error(String(e)),
  });
  const save = useMutation({
    mutationFn: async () => {
      const r = result!;
      let site = await post<Site>("/api/base/sites", { name, x: r.x, y: r.y, z: r.z, sigma_x: r.sigma_x, sigma_y: r.sigma_y, sigma_z: r.sigma_z, source: r.source, frame: r.frame, epoch: r.epoch, notes: `imported ${r.format}` });
      if (activate) site = await post<Site>(`/api/base/sites/${encodeURIComponent(name)}/activate`);
      return site;
    },
    onSuccess: (site) => { toast.success(`Saved site ${site.name}${activate ? " and activated it" : ""}`); setOpen(false); setResult(null); onSaved?.(site); },
    onError: (e) => toast.error(String(e)),
  });
  return (
    <Dialog open={open} onOpenChange={(o) => { setOpen(o); if (!o) setResult(null); }}>
      <DialogTrigger asChild><Button size="sm">Import PPP result</Button></DialogTrigger>
      <DialogContent className="max-w-xl">
        <DialogHeader><DialogTitle>Import a PPP result</DialogTitle></DialogHeader>
        <div><Label htmlFor="ppp-file">Result file (.sum, .pos, .zip, .snx or OPUS .txt)</Label><Input id="ppp-file" type="file" accept=".sum,.pos,.zip,.snx,.SNX,.txt" onChange={(e) => { const f = e.target.files?.[0]; if (f) upload.mutate(f); }} /></div>
        {upload.isPending ? <p className="text-ink-2">Reading…</p> : null}
        {result ? (
          <div className="flex flex-col gap-2">
            <Stat label="Source" value={`${result.source} (${result.format})`} />
            <Stat label="Frame" value={`${result.frame}${result.epoch ? ` @ ${result.epoch}` : ""}`} />
            <Stat label="X" value={`${result.x.toFixed(4)} m  ${sig(result.sigma_x)}`} />
            <Stat label="Y" value={`${result.y.toFixed(4)} m  ${sig(result.sigma_y)}`} />
            <Stat label="Z" value={`${result.z.toFixed(4)} m  ${sig(result.sigma_z)}`} />
            <Stat label="Position" value={`${fmtDms(result.lat, true)} ${fmtDms(result.lon, false)} · ${result.height_m.toFixed(3)} m`} />
            {result.notes.map((n) => <p key={n} className="text-[12px] leading-4 text-ink-2">{n}</p>)}
            <div><Label htmlFor="ppp-name">Site name</Label><Input id="ppp-name" value={name} onChange={(e) => setName(e.target.value)} /></div>
            <label className="flex items-center gap-2 text-[14px]"><input type="checkbox" checked={activate} onChange={(e) => setActivate(e.target.checked)} /> Activate and switch the base to this fixed position</label>
            <div className="flex justify-end"><Button onClick={() => save.mutate()} disabled={!name.trim() || save.isPending}>Save site</Button></div>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
```

- [ ] **Step 7: Wire into `Logs.tsx` and `Site.tsx`**

`Logs.tsx`: read `useSearchParams()`; `const exportPreset = params.get("export") ?? undefined; const hours = Number(params.get("hours") ?? 24)`; add two panels after "Download a raw window": `<Panel className="col-span-12 lg:col-span-6" title="Export RINEX"><ExportPanel initialPreset={exportPreset} initialHours={hours} window={selectedWindow} onSubmitted={() => qc.invalidateQueries({ queryKey: ["jobs"] })} /></Panel>` and `<Panel className="col-span-12 lg:col-span-6" title="Export jobs"><JobsPanel kind="export" /></Panel>`; when an availability hour is clicked, set `selectedWindow` to that hour as `[from, to]` in `datetime-local` format so both the raw download and the export panel follow it.
`Site.tsx`: step 2 text becomes "Export the observation file" with `<Link to="/logs?export=csrs-ppp&hours=24" className="text-brass">Export last 24 h for CSRS-PPP</Link>`; step 4 replaces the manual form trigger with `<PppImportDialog onSaved={() => invalidate()} />` (keep the manual "Enter PPP result" dialog as a secondary link for typing values by hand).

- [ ] **Step 8: Run, build, commit**

`cd web && pnpm test && pnpm lint && pnpm build`; then
```bash
cd /home/nekosaif/github/mtrtk && git add web && git commit -m "feat(web): RINEX export panel with live job progress and PPP result import dialog

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Real PPP round trip, docs, close-out

**Files:**
- Create: `docs/ppp-workflow.md`
- Modify: `README.md`, `docs/superpowers/specs/2026-09-18-mtrtk-design.md` (open items 2, 4, 5), `tests/fixtures/ppp/*` (replace reconstructions with real files when they arrive), `.env.example` (no change unless a setting was added)

- [ ] **Step 1: Produce a real 24 h export (hardware, runs while the base collects)**

With `mtrtk base` running for ≥ 24 h (survey-in or any mode; the raw log does not depend on the position mode):
```bash
uv run mtrtk export --from "$(date -u -d '25 hours ago' +%Y-%m-%dT%H:00:00Z)" --to "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:00:00Z)" --preset csrs-ppp --out /tmp/csrs
ls -la /tmp/csrs
```
Expected: `MTRK00BGD_R_<date>_01D_30S_MO.crx.gz` (~5–8 MB), `…_MN.rnx.gz`, `manifest.json` with ~2880 epochs and no warnings.

- [ ] **Step 2: Submit and import (user step, documented here for the executor to hand over)**

The user uploads the `.crx.gz` to CSRS-PPP (Static, ITRF) and receives a zip by e-mail. Then:
```bash
uv run mtrtk ppp-import ~/Downloads/<result>.zip --save-site roof-ppp --activate
```
If the parser misses a field: copy the real `.sum` and `.pos` into `tests/fixtures/ppp/` as `csrs_real.sum` / `csrs_real.pos` (strip the e-mail address), add a test for each, adjust regexes until they pass, and record the deviation in the spec's open item 5. The same applies to AUSPOS/OPUS results if the user tries them.

- [ ] **Step 3: Apply and verify**

Set `BASE_MODE=fixed` in `.env` (or use the Site page), restart or wait for the 10 s active-site poll; confirm the Site page shows "RTCM 1005 matches the active site" and the `site_verified` event fired. Compare the PPP position with the survey-in mean shown earlier — the difference is the survey-in's absolute error (expect 0.5–2 m).

- [ ] **Step 4: Write `docs/ppp-workflow.md`**

```markdown
# Centimetre-accurate base coordinates with PPP

Survey-in gives the base a position good to about a metre. Rovers inherit that error. Precise Point Positioning
(PPP) services compute the antenna position to a few millimetres from 24 hours of raw data.

1. **Collect 24 h.** Let the base log a full UTC day (Logs page shows hourly availability).
2. **Export.** Site page → "Export last 24 h for CSRS-PPP" (or `mtrtk export --preset csrs-ppp`). You get
   `MTRK00BGD_R_…_01D_30S_MO.crx.gz` (observations) and `…_MN.rnx.gz` (navigation, usually not needed).
3. **Submit.** CSRS-PPP (free, needs an NRCan account): Static mode, ITRF. AUSPOS (free): upload the .rnx.gz
   from the `auspos` preset. OPUS (USA only): `opus` preset, GPS-only RINEX 2.11.
4. **Import.** Site page → "Import PPP result" → drop the zip/.sum/.snx/.txt → check frame, epoch and sigmas →
   Save site (activate). Or `mtrtk ppp-import result.zip --save-site roof --activate`.
5. **Verify.** The base broadcasts RTCM 1005 with the new coordinates; the Site page confirms the match.

Notes: coordinates are in the service's frame (ITRF2020 at the observation epoch for CSRS-PPP). Your rovers
will be in that frame too. With `ANTENNA_TYPE=NONE` the result refers to the antenna reference point (the SMA
connector plane of the SparkFun antenna); set `ANTENNA_HEIGHT_M` if you want the mark below it.
```

- [ ] **Step 5: Spec and README updates, commit, tag**

Spec open items: mark 2 resolved ("convbin 2.4.3 -v 3.04 writes a single mixed nav file; verified 2026-09-19"), fill 5 with the real CSRS-PPP limits/format observed, and 4 with the OPUS outcome if tried. README status: "Phase 5 (RINEX export + PPP import) complete: CSRS-PPP/AUSPOS/OPUS/generic presets, background export jobs, result import, fixed-site verification. Next: F9P rover (Phase 6)."
```bash
git add docs/ppp-workflow.md README.md docs/superpowers/specs/2026-09-18-mtrtk-design.md tests/fixtures/ppp
git commit -m "docs: PPP workflow guide, real result fixtures, Phase 5 status

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git tag -a v0.5.0-phase5 -m "Phase 5: RINEX export and PPP import"
```

Phase 6 (F9P rover) is the next plan: `docs/superpowers/plans/2026-09-19-phase6-rover.md`.
