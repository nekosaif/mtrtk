"""Tests for the RTKLIB `convbin` wrapper.

The conversion tests run the *real* `convbin` on the checked-in Phase 1 fixtures - 10 s and 60 s
of 1 Hz ZED-F9P raw, so each conversion takes well under a second - and assert on the RINEX that
came out. They skip, with a reason, when no `convbin` is on PATH. They pass against both builds
this project ships: stock RTKLIB 2.4.3 b34 (`apt install rtklib`, CI) and demo5 v2.5.1 (the
Docker image); run them against the second with its binary first on PATH.

Fixture facts, measured against both builds and pinned here: `f9p_hpg113_raw_60s.ubx` holds 60
epochs at 1 Hz from 2026-09-18 20:23:28 to 20:24:27 GPST (GPS, GLONASS, Galileo, QZSS, BeiDou)
and 23 navigation messages; `f9p_hpg113_raw_10s.ubx` holds 10 epochs and no ephemerides at all.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from mtrtk.rinex import convbin as convbin_module
from mtrtk.rinex.convbin import (
    ConvbinError,
    ConvbinOptions,
    RinexHeader,
    build_convbin_command,
    convbin_available,
    convbin_supports,
    parse_convbin_summary,
    run_convbin,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE = FIXTURES / "f9p_hpg113_raw_60s.ubx"
FIXTURE_NO_EPH = FIXTURES / "f9p_hpg113_raw_10s.ubx"
HEADER = RinexHeader(
    marker_name="MTRK",
    marker_number="00001",
    observer="mtrtk",
    agency="mtrtk",
    receiver_version="HPG 1.13",
    antenna_type="NONE",
    approx_xyz=(-26748.172, 5837156.618, 2561801.261),
    delta_hen=(0.05, 0.0, 0.0),
)
FIRST_EPOCH = datetime(2026, 9, 18, 20, 23, 28)
LAST_EPOCH = datetime(2026, 9, 18, 20, 24, 27)

_HAVE_CONVBIN = convbin_available() and FIXTURE.exists() and FIXTURE_NO_EPH.exists()
# CI sets this: there a missing convbin must fail the real-binary tests, not skip them quietly.
_REQUIRE_CONVBIN = os.environ.get("MTRTK_REQUIRE_CONVBIN") == "1"
needs_convbin = pytest.mark.skipif(
    not _HAVE_CONVBIN and not _REQUIRE_CONVBIN,
    reason="RTKLIB convbin is not installed (apt install rtklib) or a raw fixture is missing",
)
# A subprocess transport that outlives its event loop is reported as an unraisable exception
# at teardown. Here that is a failure, not a warning in the summary.
no_leaks = pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")


# --------------------------------------------------------------------------- helpers


@pytest.fixture(autouse=True)
def _cold_probe_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test probes for itself: an answer cached by an earlier test would hide whether
    this one's probe really ran."""
    monkeypatch.setattr(convbin_module, "_flag_support", {})
    yield


def _header_lines(path: Path) -> list[str]:
    lines = path.read_text().splitlines()
    end = next(i for i, line in enumerate(lines) if line[60:].strip() == "END OF HEADER")
    return lines[:end]


def _body_lines(path: Path) -> list[str]:
    """Everything after END OF HEADER that is not an epoch record - i.e. satellite records."""
    lines = path.read_text().splitlines()
    end = next(i for i, line in enumerate(lines) if line[60:].strip() == "END OF HEADER")
    return [line for line in lines[end + 1 :] if not line.startswith(">")]


def _field(header: list[str], label: str) -> list[str]:
    """Every header line carrying `label` in the RINEX label columns, value part only."""
    return [line[:60] for line in header if line[60:].strip() == label]


def _one(header: list[str], label: str) -> str:
    values = _field(header, label)
    assert len(values) == 1, f"expected one {label!r} line, got {values}"
    return values[0]


def _obs_types(path: Path) -> dict[str, list[str]]:
    types: dict[str, list[str]] = {}
    for line in _field(_header_lines(path), "SYS / # / OBS TYPES"):
        parts = line.split()
        types[parts[0]] = parts[2:]
    return types


def _header_time(header: list[str], label: str) -> datetime:
    y, mo, d, h, mi, sec = _one(header, label).split()[:6]
    return datetime(int(y), int(mo), int(d), int(h), int(mi), int(float(sec)))


def _epoch_times(path: Path) -> list[datetime]:
    out = []
    for line in path.read_text().splitlines():
        if line.startswith(">"):
            f = line.split()
            out.append(
                datetime(int(f[1]), int(f[2]), int(f[3]), int(f[4]), int(f[5]), int(float(f[6])))
            )
    return out


async def _capture(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    assert proc.returncode is not None
    return proc.returncode, out.decode("utf-8", "replace")


def _fake_convbin(path: Path, body: str) -> Path:
    """A stand-in binary that answers the `-scan` support probe the way a real convbin does
    (unknown option -> usage banner; known option with no input file -> "no input file")."""
    path.write_text(
        '#!/bin/sh\ncase "$1" in -scan) echo "no input file" >&2; exit 255 ;; esac\n' + body
    )
    path.chmod(0o755)
    return path


def _spy_processes(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.subprocess.Process]:
    """Every process the code under test starts, in order."""
    procs: list[asyncio.subprocess.Process] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        proc = await real(*args, **kwargs)  # type: ignore[arg-type]
        procs.append(proc)
        return proc

    monkeypatch.setattr(convbin_module.asyncio, "create_subprocess_exec", spy)
    return procs


def _reaped(proc: asyncio.subprocess.Process) -> bool:
    """Waited for (an exit status is known) and its transport closed - what has to be true of
    every process before an error about it propagates."""
    return proc.returncode is not None and proc._transport.is_closing()  # type: ignore[attr-defined]


def _gone(pid: int) -> bool:
    """True once `pid` has exited (a zombie waiting for its reaper counts). Reads /proc only;
    nothing is ever signalled from a test."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


async def _wait_gone(pid: int, within_s: float = 5.0) -> bool:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_gone, pid):
            return True
        await asyncio.sleep(0.05)
    return False


def _read_pids(path: Path) -> list[int]:
    try:
        return [int(line) for line in path.read_text().split()]
    except FileNotFoundError:
        return []


def _write_bytes(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def _arg(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


# --------------------------------------------------------------------------- command building


def test_build_command_full() -> None:
    opts = ConvbinOptions(
        version="3.04",
        interval_s=30,
        exclude_systems=("R", "E"),
        start=datetime(2026, 9, 18, 10, tzinfo=UTC),
        end=datetime(2026, 9, 18, 11, tzinfo=UTC),
        header=HEADER,
    )
    cmd = build_convbin_command(Path("in.ubx"), Path("out.rnx"), Path("nav.rnx"), opts)
    assert cmd[0] == "convbin"
    joined = " ".join(cmd)
    assert "-r ubx" in joined and "-v 3.04" in joined and "-od -os" in joined
    assert "-f 2" in joined and "-scan" in joined
    assert "-ro -TADJ=1.0" in joined
    assert "-ti 30" in joined
    # The window is [start, end): `-te` is the end less 10 ms, which both builds then treat
    # alike (2.4.3 would otherwise include an epoch on the end second, demo5 would not).
    assert "-ts 2026/09/18 10:00:00" in joined and "-te 2026/09/18 10:59:59.99" in joined
    assert "-oi" in cmd and "-ot" in cmd and "-ol" in cmd  # iono/time/leap in the nav header
    assert cmd.count("-y") == 2 and "R" in cmd and "E" in cmd
    assert "-hm MTRK" in joined and "-hn 00001" in joined and "-ht GEODETIC" in joined
    assert "-ho mtrtk/mtrtk" in joined and "-hr 0/u-blox ZED-F9P/HPG 1.13" in joined
    assert "-ha 0/NONE" in joined
    assert "-hp -26748.1720/5837156.6180/2561801.2610" in joined
    assert "-hd 0.0500/0.0000/0.0000" in joined
    assert cmd[-5:] == ["-o", "out.rnx", "-n", "nav.rnx", "in.ubx"]


def test_build_command_minimal_omits_optional_flags() -> None:
    cmd = build_convbin_command(
        Path("a.ubx"),
        Path("a.obs"),
        Path("a.nav"),
        ConvbinOptions(
            header=RinexHeader(marker_name="X", observer="o", agency="a", receiver_version="")
        ),
    )
    assert "-ti" not in cmd and "-ts" not in cmd and "-hp" not in cmd and "-y" not in cmd


def test_build_command_splits_each_time_into_two_arguments() -> None:
    """convbin reads `-ts`/`-te` as *two* argv tokens (`-ts y/m/d h:m:s`). One "y/m/d h:m:s"
    token is read as that date at midnight, the next flag is eaten as the time of day, and the
    window is silently ignored - and `" ".join(cmd)` cannot tell the two apart."""
    opts = ConvbinOptions(
        header=HEADER,
        start=datetime(2026, 9, 18, 10, tzinfo=UTC),
        end=datetime(2026, 9, 18, 11, 30, 5, tzinfo=UTC),
    )
    cmd = build_convbin_command(Path("in.ubx"), Path("o.rnx"), Path("n.rnx"), opts)
    assert cmd[cmd.index("-ts") : cmd.index("-ts") + 3] == ["-ts", "2026/09/18", "10:00:00"]
    assert cmd[cmd.index("-te") : cmd.index("-te") + 3] == ["-te", "2026/09/18", "11:30:04.99"]


def test_build_command_keeps_fractional_seconds() -> None:
    opts = ConvbinOptions(
        header=HEADER,
        start=datetime(2026, 9, 18, 10, 0, 0, 250000),
        end=datetime(2026, 9, 18, 10, 0, 30, 500000),
    )
    cmd = build_convbin_command(Path("in.ubx"), Path("o.rnx"), Path("n.rnx"), opts)
    assert cmd[cmd.index("-ts") + 2] == "10:00:00.25"
    assert cmd[cmd.index("-te") + 2] == "10:00:30.49"


def test_build_command_writes_an_aware_time_as_utc() -> None:
    """A Dhaka-local 16:00 is 10:00 UTC; written as it reads it would clip six hours off."""
    dhaka = timezone(timedelta(hours=6))
    opts = ConvbinOptions(
        header=HEADER,
        start=datetime(2026, 9, 18, 16, tzinfo=dhaka),
        end=datetime(2026, 9, 18, 10, 30),  # naive: taken as written
    )
    cmd = build_convbin_command(Path("in.ubx"), Path("o.rnx"), Path("n.rnx"), opts)
    assert cmd[cmd.index("-ts") + 1 : cmd.index("-ts") + 3] == ["2026/09/18", "10:00:00"]
    assert cmd[cmd.index("-te") + 1 : cmd.index("-te") + 3] == ["2026/09/18", "10:29:59.99"]


def test_build_command_passes_every_receiver_option_in_one_ro() -> None:
    """convbin keeps only the last `-ro`: a second one would silently drop `-TADJ=1.0`."""
    opts = ConvbinOptions(header=HEADER, receiver_options=("-TADJ=1.0", "-EPHALL"))
    cmd = build_convbin_command(Path("in.ubx"), Path("o.rnx"), Path("n.rnx"), opts)
    assert cmd.count("-ro") == 1 and _arg(cmd, "-ro") == "-TADJ=1.0 -EPHALL"
    none = build_convbin_command(
        Path("in.ubx"), Path("o.rnx"), Path("n.rnx"), replace(opts, receiver_options=())
    )
    assert "-ro" not in none


def test_build_command_leaves_out_scan_when_the_build_lacks_it() -> None:
    opts = ConvbinOptions(header=HEADER)
    assert "-scan" not in build_convbin_command(
        Path("in.ubx"), Path("o.rnx"), Path("n.rnx"), opts, scan=False
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"exclude_systems": ("GPS",)},  # convbin reads only the first letter: GPS, not nothing
        {"exclude_systems": ("r",)},  # lowercase is silently ignored
        {"exclude_systems": ("",)},
        {"version": "3.4"},
        {"version": "4.00"},
        {"frequencies": 0},
        {"frequencies": 6},
    ],
)
def test_build_command_rejects_options_convbin_would_misread(bad: dict[str, object]) -> None:
    opts = replace(ConvbinOptions(header=HEADER), **bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        build_convbin_command(Path("in.ubx"), Path("o.rnx"), Path("n.rnx"), opts)


def test_build_command_accepts_every_supported_version_and_system() -> None:
    for version in ("2.10", "2.11", "2.12", "3.00", "3.01", "3.02", "3.03", "3.04"):
        opts = ConvbinOptions(header=HEADER, version=version, exclude_systems=tuple("GRESJCI"))
        assert _arg(build_convbin_command(Path("i"), Path("o"), Path("n"), opts), "-v") == version


def test_header_values_are_made_to_fit_convbin() -> None:
    """convbin `strcpy`s each header value into a 32-byte buffer and splits on "/" with
    `strtok`, which skips empty parts. So: no value outgrows its RINEX field (an overlong one
    aborts 2.4.3 and corrupts the next field on demo5), a "/" inside a value becomes "-", and
    an empty part is passed as a blank so the parts after it stay in their own fields."""
    header = RinexHeader(
        marker_name="M" * 70,
        marker_number="",
        observer="",
        agency="Mongol/Tori " + "A" * 40,
        receiver_number="",
        receiver_version="HPG\t1.13\n",
        antenna_type="ঢাকা",  # four Bengali code points; RINEX is ASCII
        comment="c" * 70,
    )
    cmd = build_convbin_command(
        Path("in.ubx"), Path("o.rnx"), Path("n.rnx"), ConvbinOptions(header=header)
    )
    assert _arg(cmd, "-hm") == "M" * 60
    assert "-hn" not in cmd  # every part blank: convbin keeps its own default
    assert _arg(cmd, "-ho") == " /" + ("Mongol-Tori " + "A" * 40)[:31]
    assert _arg(cmd, "-hr") == " /u-blox ZED-F9P/HPG 1.13"
    assert _arg(cmd, "-ha") == "0/????"
    assert _arg(cmd, "-hc") == "c" * 60
    blank = replace(header, observer=" ", agency="", comment="  ")
    cmd = build_convbin_command(
        Path("in.ubx"), Path("o.rnx"), Path("n.rnx"), ConvbinOptions(header=blank)
    )
    assert "-ho" not in cmd and "-hc" not in cmd


# --------------------------------------------------------------------------- summary parsing


def test_parse_summary() -> None:
    stderr = "scanning: 2026/09/18 18:25:28 GREJC\n2026/09/18 18:25:09-09/18 18:25:28: O=20 N=3 \n"
    assert parse_convbin_summary(stderr) == (20, 3)
    assert parse_convbin_summary("garbage") == (0, 0)


def test_parse_summary_takes_the_last_record_and_tolerates_a_missing_nav_count() -> None:
    stderr = "20:23:47: O=20 \r20:23:48: O=21 N=5 \r20:23:28-20:24:27: O=60 N=23 \n"
    assert parse_convbin_summary(stderr) == (60, 23)
    assert parse_convbin_summary("20:23:28-20:23:47: O=20 \n") == (20, 0)
    # `N=` on a later line belongs to a later record, never to this `O=`.
    assert parse_convbin_summary("O=7 \nN=3 \n") == (7, 0)


def test_parse_summary_ignores_an_o_equals_inside_the_echoed_input_path() -> None:
    echo = "input file  : /data/INFO=2/O=7/x.ubx (u-blox UBX)\n->rinex obs : /data/O=9.obs\n"
    assert parse_convbin_summary(echo) == (0, 0)
    assert parse_convbin_summary(echo + "2026/09/18 20:23:28-09/18 20:24:27: O=60 N=23 \n") == (
        60,
        23,
    )


# --------------------------------------------------------------------------- flag probing


@needs_convbin
@no_leaks
async def test_real_convbin_accepts_scan_although_its_banner_omits_it(tmp_path: Path) -> None:
    """Ruling P5-R5: `-scan` is undocumented but real. Pinned by running the real binary with
    the wrapper's exact argv: a build that did not know `-scan` would print its usage banner,
    exit 0 and convert nothing. An option no build knows is the probe's control."""
    _, banner = await _capture("convbin", "-h")
    assert "[option ...]" in banner  # whether the banner lists -scan is beside the point
    obs, nav = tmp_path / "s.obs", tmp_path / "s.nav"
    cmd = build_convbin_command(FIXTURE_NO_EPH, obs, nav, ConvbinOptions(header=HEADER))
    assert "-scan" in cmd
    returncode, output = await _capture(*cmd)
    assert returncode == 0 and "O=10" in output
    assert await asyncio.to_thread(obs.is_file)
    assert await convbin_supports("-scan") is True
    assert await convbin_supports("-zzzz-no-such-option") is False


@no_leaks
async def test_flag_support_is_probed_once_per_binary(tmp_path: Path) -> None:
    calls = tmp_path / "calls.txt"
    fake = tmp_path / "fake-convbin"
    fake.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\necho "no input file" >&2\nexit 255\n')
    fake.chmod(0o755)
    assert await convbin_supports("-scan", str(fake)) is True
    assert await convbin_supports("-scan", str(fake)) is True
    assert calls.read_text().splitlines() == ["-scan"]


@no_leaks
async def test_unsupported_flag_is_dropped_from_the_command(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A build that answers `-scan` with the usage banner must not be handed `-scan`."""
    args = tmp_path / "args.txt"
    old = tmp_path / "old-convbin"
    old.write_text(
        "#!/bin/sh\n"
        'if [ "$#" -eq 1 ]; then echo " Synopsis" >&2; echo " convbin [option ...] file" >&2;'
        " exit 0; fi\n"
        f'echo "$@" > "{args}"\nexit 0\n'
    )
    old.chmod(0o755)
    with caplog.at_level(logging.WARNING, logger="mtrtk.rinex.convbin"):
        assert await convbin_supports("-scan", str(old)) is False
    assert any("-scan" in r.getMessage() for r in caplog.records)  # visible in a job log
    with pytest.raises(ConvbinError, match="no observation file"):
        await run_convbin(
            tmp_path / "x.ubx",
            tmp_path / "x.obs",
            tmp_path / "x.nav",
            ConvbinOptions(header=HEADER),
            binary=str(old),
        )
    sent = args.read_text().split()
    assert "-scan" not in sent and "-TADJ=1.0" in sent


async def test_a_probe_that_cannot_run_leaves_the_flag_on_and_caches_nothing(
    tmp_path: Path,
) -> None:
    """The flags probed are known good on both builds, so a probe that fails to run must not
    be read as "unsupported" - that would silently drop `-scan` on stock 2.4.3."""
    unrunnable = tmp_path / "convbin"
    unrunnable.write_text("#!/bin/sh\nexit 0\n")  # not executable: the exec itself fails
    assert await convbin_supports("-scan", str(unrunnable)) is True
    assert convbin_module._flag_support == {}


def test_convbin_available_reflects_path() -> None:
    assert convbin_available() == (shutil.which("convbin") is not None)
    assert convbin_available("/nonexistent/convbin") is False


def test_convbin_available_ignores_a_non_executable_file(tmp_path: Path) -> None:
    decoy = tmp_path / "convbin"
    decoy.write_text("not a program")
    assert convbin_available(str(decoy)) is False


# --------------------------------------------------------------------------- real conversions


@needs_convbin
@no_leaks
async def test_run_convbin_on_fixture(tmp_path: Path) -> None:
    res = await run_convbin(
        FIXTURE, tmp_path / "MTRK.rnx", tmp_path / "MTRK_MN.rnx", ConvbinOptions(header=HEADER)
    )
    assert res.obs_path.exists() and res.nav_path.exists()
    assert res.obs_epochs >= 50
    head = res.obs_path.read_text().splitlines()[:12]
    assert head[0].startswith("     3.04")
    assert any("MARKER NAME" in line and "MTRK" in line for line in head)
    nav_head = res.nav_path.read_text().splitlines()[0]
    assert nav_head.startswith("     3.04") and "M: Mixed" in nav_head
    nav_labels = {line[60:].strip() for line in _header_lines(res.nav_path)}
    assert {"IONOSPHERIC CORR", "TIME SYSTEM CORR"} <= nav_labels  # -oi, -ot
    epochs = [line for line in res.obs_path.read_text().splitlines() if line.startswith(">")]
    assert all(line.split()[6].endswith(".0000000") for line in epochs)  # -TADJ aligned


@needs_convbin
@no_leaks
async def test_run_convbin_writes_every_header_field(tmp_path: Path) -> None:
    res = await run_convbin(
        FIXTURE, tmp_path / "MTRK.obs", tmp_path / "MTRK.nav", ConvbinOptions(header=HEADER)
    )
    header = _header_lines(res.obs_path)
    assert res.obs_path.read_text().splitlines()[0][:40].split() == [
        "3.04",
        "OBSERVATION",
        "DATA",
    ]
    assert _one(header, "MARKER NAME").strip() == "MTRK"
    assert _one(header, "MARKER NUMBER").strip() == "00001"
    assert _one(header, "MARKER TYPE").strip() == "GEODETIC"
    observer = _one(header, "OBSERVER / AGENCY")
    assert (observer[:20].strip(), observer[20:60].strip()) == ("mtrtk", "mtrtk")
    rec = _one(header, "REC # / TYPE / VERS")
    assert (rec[:20].strip(), rec[20:40].strip(), rec[40:60].strip()) == (
        "0",
        "u-blox ZED-F9P",
        "HPG 1.13",
    )
    ant = _one(header, "ANT # / TYPE")
    assert (ant[:20].strip(), ant[20:40].strip()) == ("0", "NONE")
    xyz = [float(v) for v in _one(header, "APPROX POSITION XYZ").split()]
    assert xyz == pytest.approx([-26748.172, 5837156.618, 2561801.261], abs=1e-4)
    hen = [float(v) for v in _one(header, "ANTENNA: DELTA H/E/N").split()]
    assert hen == pytest.approx([0.05, 0.0, 0.0], abs=1e-4)


@needs_convbin
@no_leaks
async def test_run_convbin_survives_header_values_convbin_cannot_hold(tmp_path: Path) -> None:
    """Unfitted, this agency aborts 2.4.3 outright and spills into the receiver number on
    demo5, and the empty observer and receiver number shift every field after them."""
    header = replace(HEADER, observer="", agency="Mongol/Tori " + "A" * 40, receiver_number="")
    res = await run_convbin(
        FIXTURE_NO_EPH, tmp_path / "h.obs", tmp_path / "h.nav", ConvbinOptions(header=header)
    )
    lines = _header_lines(res.obs_path)
    observer = _one(lines, "OBSERVER / AGENCY")
    assert (observer[:20].strip(), observer[20:60].strip()) == (
        "",
        ("Mongol-Tori " + "A" * 40)[:31],
    )
    rec = _one(lines, "REC # / TYPE / VERS")
    assert (rec[:20].strip(), rec[20:40].strip(), rec[40:60].strip()) == (
        "",
        "u-blox ZED-F9P",
        "HPG 1.13",
    )


@needs_convbin
@no_leaks
async def test_run_convbin_emits_two_frequencies_for_every_system(tmp_path: Path) -> None:
    """Band digits, not signal codes: 2.4.3 writes GPS L2 as `2L` and Galileo as `1C`/`7Q`,
    demo5 as `2X` and `1X`/`7X` - same signals, different attribute letters."""
    res = await run_convbin(
        FIXTURE, tmp_path / "a.obs", tmp_path / "a.nav", ConvbinOptions(header=HEADER)
    )
    types = _obs_types(res.obs_path)
    bands = {"G": "12", "R": "12", "E": "17", "J": "12", "C": "27"}
    assert set(types) == set(bands)
    for sys, sys_types in types.items():
        assert [t[0] for t in sys_types] == list("CLDSCLDS")  # -od -os: code/phase/doppler/snr
        assert {t[1] for t in sys_types[:4]} == {bands[sys][0]}
        assert {t[1] for t in sys_types[4:]} == {bands[sys][1]}


@needs_convbin
@no_leaks
async def test_run_convbin_epoch_count_and_span_match_the_fixture(tmp_path: Path) -> None:
    res = await run_convbin(
        FIXTURE, tmp_path / "a.obs", tmp_path / "a.nav", ConvbinOptions(header=HEADER)
    )
    epochs = _epoch_times(res.obs_path)
    assert len(epochs) == 60 and res.obs_epochs == 60
    assert epochs[0] == FIRST_EPOCH and epochs[-1] == LAST_EPOCH
    assert {(b - a).total_seconds() for a, b in zip(epochs, epochs[1:], strict=False)} == {1.0}
    header = _header_lines(res.obs_path)
    assert _header_time(header, "TIME OF FIRST OBS") == FIRST_EPOCH
    assert _header_time(header, "TIME OF LAST OBS") == LAST_EPOCH
    assert res.nav_messages == 23
    assert "O=60 N=23" in res.stderr_tail


@needs_convbin
@no_leaks
async def test_run_convbin_decimates(tmp_path: Path) -> None:
    res = await run_convbin(
        FIXTURE,
        tmp_path / "a.rnx",
        tmp_path / "a_MN.rnx",
        ConvbinOptions(interval_s=10, header=HEADER),
    )
    assert 4 <= res.obs_epochs <= 7
    epochs = _epoch_times(res.obs_path)
    assert len(epochs) == res.obs_epochs == 6
    assert all(e.second % 10 == 0 for e in epochs)


@needs_convbin
@no_leaks
async def test_run_convbin_clips_to_the_requested_window(tmp_path: Path) -> None:
    """The window is [start, end) on both builds. Passed as given, the end bound would be
    inclusive on 2.4.3 and exclusive on demo5 (11 epochs against 10 here). Also the only thing
    that catches a `-ts`/`-te` passed as one argv token: the window is then ignored and all 60
    epochs come back."""
    start = datetime(2026, 9, 18, 20, 23, 40)
    end = datetime(2026, 9, 18, 20, 23, 50)
    res = await run_convbin(
        FIXTURE,
        tmp_path / "w.obs",
        tmp_path / "w.nav",
        ConvbinOptions(header=HEADER, start=start, end=end),
    )
    epochs = _epoch_times(res.obs_path)
    assert epochs[0] == start
    assert epochs[-1] == end - timedelta(seconds=1)
    assert res.obs_epochs == len(epochs) == (epochs[-1] - start).seconds + 1
    header = _header_lines(res.obs_path)
    assert _header_time(header, "TIME OF FIRST OBS") == epochs[0]
    assert _header_time(header, "TIME OF LAST OBS") == epochs[-1]


@needs_convbin
@no_leaks
async def test_consecutive_windows_share_no_epoch(tmp_path: Path) -> None:
    """Decimated and back to back, as the export splices them: [30 s, 60 s) then [60 s, 90 s)
    at 10 s hold 20:23:30..20:23:50 and 20:24:00..20:24:20, the boundary epoch exactly once."""
    t0 = datetime(2026, 9, 18, 20, 23, 30)
    seen: list[datetime] = []
    for i in range(2):
        res = await run_convbin(
            FIXTURE,
            tmp_path / f"{i}.obs",
            tmp_path / f"{i}.nav",
            ConvbinOptions(
                header=HEADER,
                interval_s=10,
                start=t0 + timedelta(seconds=30 * i),
                end=t0 + timedelta(seconds=30 * (i + 1)),
            ),
        )
        seen += _epoch_times(res.obs_path)
    assert seen == [t0 + timedelta(seconds=10 * k) for k in range(6)]


@needs_convbin
@no_leaks
async def test_run_convbin_names_an_empty_window(tmp_path: Path) -> None:
    """A window outside the log: exit 0 and no file, as for a corrupt log - but the error
    says which window was empty."""
    with pytest.raises(ConvbinError, match="no observation file.*no epochs between") as excinfo:
        await run_convbin(
            FIXTURE,
            tmp_path / "e.obs",
            tmp_path / "e.nav",
            ConvbinOptions(
                header=HEADER,
                start=datetime(2026, 9, 18, 21, 0),
                end=datetime(2026, 9, 18, 21, 10),
            ),
        )
    assert "2026-09-18 21:00:00" in str(excinfo.value)


@needs_convbin
@no_leaks
async def test_run_convbin_excludes_systems(tmp_path: Path) -> None:
    res = await run_convbin(
        FIXTURE,
        tmp_path / "y.obs",
        tmp_path / "y.nav",
        ConvbinOptions(header=HEADER, exclude_systems=("R", "E")),
    )
    assert set(_obs_types(res.obs_path)) == {"G", "J", "C"}
    body = _body_lines(res.obs_path)
    sats = {line[0] for line in body if len(line) > 3 and line[1:3].isdigit()}
    assert sats == {"G", "J", "C"}
    assert len(body) > 500  # and the records really are there to be checked
    assert res.obs_epochs == 60


@needs_convbin
@no_leaks
async def test_run_convbin_writes_rinex_2_11_gps_only(tmp_path: Path) -> None:
    """The OPUS shape: RINEX 2.11, every system but GPS excluded. RINEX 2 epochs carry no `>`,
    so the epoch count has to come from convbin's own summary."""
    res = await run_convbin(
        FIXTURE,
        tmp_path / "MTRK2610.26o",
        tmp_path / "MTRK2610.26n",
        ConvbinOptions(
            header=HEADER, version="2.11", exclude_systems=("R", "E", "J", "S", "C", "I")
        ),
    )
    header = _header_lines(res.obs_path)
    assert _one(header, "RINEX VERSION / TYPE")[:40].split() == ["2.11", "OBSERVATION", "DATA"]
    assert _one(header, "RINEX VERSION / TYPE")[40] == "G"
    assert res.obs_epochs == 60
    nav_head = res.nav_path.read_text().splitlines()[0]
    assert nav_head.split()[:2] == ["2.11", "N:"] and "GPS" in nav_head
    assert res.nav_messages > 0


@needs_convbin
@no_leaks
async def test_run_convbin_without_ephemerides_still_hands_back_a_nav_path(
    tmp_path: Path,
) -> None:
    """Ten seconds of log carry no ephemeris, and convbin then writes no navigation file."""
    res = await run_convbin(
        FIXTURE_NO_EPH, tmp_path / "n.obs", tmp_path / "n.nav", ConvbinOptions(header=HEADER)
    )
    assert res.obs_epochs == 10 and res.nav_messages == 0
    assert res.nav_path.is_file() and res.nav_path.read_text() == ""


@needs_convbin
@no_leaks
async def test_run_convbin_writes_the_comment_when_one_is_given(tmp_path: Path) -> None:
    res = await run_convbin(
        FIXTURE_NO_EPH,
        tmp_path / "c.obs",
        tmp_path / "c.nav",
        ConvbinOptions(header=replace(HEADER, comment="mtrtk phase5")),
    )
    comments = _field(_header_lines(res.obs_path), "COMMENT")
    assert any(line.strip() == "mtrtk phase5" for line in comments)


@needs_convbin
@no_leaks
async def test_run_convbin_creates_missing_output_directories(tmp_path: Path) -> None:
    res = await run_convbin(
        FIXTURE_NO_EPH,
        tmp_path / "obs" / "deep" / "a.obs",
        tmp_path / "nav" / "deep" / "a.nav",
        ConvbinOptions(header=HEADER),
    )
    assert res.obs_path.is_file() and res.nav_path.is_file()


# --------------------------------------------------------------------------- failure paths


async def test_run_convbin_missing_binary(tmp_path: Path) -> None:
    (tmp_path / "x.ubx").write_bytes(b"\x00")
    with pytest.raises(ConvbinError, match="not found"):
        await run_convbin(
            tmp_path / "x.ubx",
            tmp_path / "x.rnx",
            tmp_path / "x_MN.rnx",
            ConvbinOptions(header=HEADER),
            binary="/nonexistent/convbin",
        )


@needs_convbin
@no_leaks
async def test_run_convbin_surfaces_convbins_own_message(tmp_path: Path) -> None:
    junk = tmp_path / "junk.ubx"
    await asyncio.to_thread(_write_bytes, junk, bytes(range(256)) * 4)
    with pytest.raises(ConvbinError) as excinfo:
        await run_convbin(
            junk, tmp_path / "j.obs", tmp_path / "j.nav", ConvbinOptions(header=HEADER)
        )
    message = str(excinfo.value)
    assert "no observation file" in message
    assert "u-blox UBX" in message  # convbin's own report of what it opened
    assert not (tmp_path / "j.obs").exists()


@needs_convbin
@no_leaks
async def test_run_convbin_reports_a_missing_input_file(tmp_path: Path) -> None:
    with pytest.raises(ConvbinError, match="exited") as excinfo:
        await run_convbin(
            tmp_path / "gone.ubx",
            tmp_path / "g.obs",
            tmp_path / "g.nav",
            ConvbinOptions(header=HEADER),
        )
    assert "no input file" in str(excinfo.value)


@needs_convbin
@no_leaks
async def test_run_convbin_does_not_pass_off_a_stale_output(tmp_path: Path) -> None:
    obs, nav = tmp_path / "s.obs", tmp_path / "s.nav"
    obs.write_text("leftover from an earlier run\n")
    nav.write_text("leftover\n")
    junk = tmp_path / "junk.ubx"
    await asyncio.to_thread(_write_bytes, junk, bytes(range(256)) * 4)
    with pytest.raises(ConvbinError):
        await run_convbin(junk, obs, nav, ConvbinOptions(header=HEADER))
    assert not obs.exists() and not nav.exists()


@no_leaks
async def test_run_convbin_raises_on_a_nonzero_exit_and_discards_what_it_wrote(
    tmp_path: Path,
) -> None:
    obs, nav = tmp_path / "p.obs", tmp_path / "p.nav"
    broken = _fake_convbin(
        tmp_path / "broken-convbin",
        f'echo partial > "{obs}"\necho partial > "{nav}"\necho "file write error" >&2\nexit 1\n',
    )
    with pytest.raises(ConvbinError, match="exited 1: file write error"):
        await run_convbin(
            tmp_path / "x.ubx", obs, nav, ConvbinOptions(header=HEADER), binary=str(broken)
        )
    assert not obs.exists() and not nav.exists()


@no_leaks
async def test_run_convbin_names_the_signal_that_killed_convbin(tmp_path: Path) -> None:
    crashing = _fake_convbin(tmp_path / "crashing-convbin", "kill -TERM $$\n")
    with pytest.raises(ConvbinError, match="SIGTERM"):
        await run_convbin(
            tmp_path / "x.ubx",
            tmp_path / "k.obs",
            tmp_path / "k.nav",
            ConvbinOptions(header=HEADER),
            binary=str(crashing),
        )


@no_leaks
async def test_run_convbin_times_out_and_kills_everything_it_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child's own child holds the pipes too: killing only the child would leave the call
    waiting on them, and their transport outliving the event loop. The timeout leaves the shell
    ample time to start on a loaded runner; two pids in the file prove it got past writing obs
    (which it does first), so the discard check below is not vacuous."""
    procs = _spy_processes(monkeypatch)
    pids, obs = tmp_path / "pids", tmp_path / "t.obs"
    slow = _fake_convbin(
        tmp_path / "slow-convbin",
        f'echo partial > "{obs}"\necho $$ > "{pids}"\nsleep 30 &\necho $! >> "{pids}"\nwait\n',
    )
    started = time.monotonic()
    with pytest.raises(ConvbinError, match="timed out"):
        await run_convbin(
            tmp_path / "x.ubx",
            obs,
            tmp_path / "t.nav",
            ConvbinOptions(header=HEADER),
            binary=str(slow),
            timeout_s=2.0,
        )
    assert procs and all(_reaped(p) for p in procs)  # the probe and the conversion
    assert time.monotonic() - started < 10.0
    children = await asyncio.to_thread(_read_pids, pids)
    assert len(children) == 2
    for pid in children:
        assert await _wait_gone(pid), f"pid {pid} outlived the timeout"
    assert not obs.exists()


@no_leaks
async def test_cancelling_run_convbin_kills_and_reaps_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    procs = _spy_processes(monkeypatch)
    pids, obs = tmp_path / "pids", tmp_path / "c.obs"
    slow = _fake_convbin(
        tmp_path / "slow-convbin",
        f'echo partial > "{obs}"\necho $$ > "{pids}"\nsleep 30 &\necho $! >> "{pids}"\nwait\n',
    )
    task = asyncio.create_task(
        run_convbin(
            tmp_path / "x.ubx", obs, tmp_path / "c.nav", ConvbinOptions(header=HEADER), str(slow)
        )
    )
    deadline = time.monotonic() + 10.0
    while len(children := await asyncio.to_thread(_read_pids, pids)) < 2:
        assert time.monotonic() < deadline, "the fake convbin never started"
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert procs and all(_reaped(p) for p in procs)
    for pid in children:
        assert await _wait_gone(pid), f"pid {pid} outlived the cancellation"
    assert not obs.exists()


@no_leaks
async def test_run_convbin_reports_a_silent_failure(tmp_path: Path) -> None:
    """convbin answers a corrupt log with exit 0 and no file; the wrapper must still raise."""
    quiet = _fake_convbin(tmp_path / "quiet-convbin", 'echo "nothing to do" >&2\nexit 0\n')
    with pytest.raises(ConvbinError, match="no observation file: nothing to do"):
        await run_convbin(
            tmp_path / "x.ubx",
            tmp_path / "q.obs",
            tmp_path / "q.nav",
            ConvbinOptions(header=HEADER),
            binary=str(quiet),
        )


async def _exec_noting_reaped(
    cmd: list[str], timeout_s: float, procs: list[asyncio.subprocess.Process], seen: list[bool]
) -> None:
    """Run `_exec`, and note - in the very step its exception arrives, before the event loop
    gets another turn to tidy up behind it - whether every process it started is reaped."""
    try:
        await convbin_module._exec(cmd, timeout_s)
    except BaseException:
        seen.extend(_reaped(p) for p in procs)
        raise


@no_leaks
async def test_a_timeout_reaps_the_child_before_it_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    procs = _spy_processes(monkeypatch)
    slow = _fake_convbin(tmp_path / "slow-convbin", "sleep 30 &\nwait\n")
    seen: list[bool] = []
    with pytest.raises(TimeoutError):
        await _exec_noting_reaped([str(slow), "x"], 0.3, procs, seen)
    assert seen == [True]


@no_leaks
async def test_a_cancellation_reaps_the_child_before_it_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    procs = _spy_processes(monkeypatch)
    started = tmp_path / "started"
    slow = _fake_convbin(tmp_path / "slow-convbin", f': > "{started}"\nsleep 30 &\nwait\n')
    seen: list[bool] = []
    task = asyncio.create_task(_exec_noting_reaped([str(slow), "x"], 60.0, procs, seen))
    deadline = time.monotonic() + 10.0
    while not await asyncio.to_thread(started.exists):
        assert time.monotonic() < deadline, "the fake convbin never started"
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == [True]


@no_leaks
async def test_run_convbin_counts_epochs_itself_when_convbin_prints_no_summary(
    tmp_path: Path,
) -> None:
    obs = tmp_path / "f.obs"
    rinex = "     3.04           OBSERVATION DATA    M\n" + "".join(
        f"> 2026 09 18 20 23 {s:02d}.0000000  0  1\nG01  1.0\n" for s in (28, 29, 30)
    )
    terse = _fake_convbin(tmp_path / "terse-convbin", f'printf "%s" "{rinex}" > "{obs}"\n')
    res = await run_convbin(
        tmp_path / "x.ubx", obs, tmp_path / "f.nav", ConvbinOptions(header=HEADER), str(terse)
    )
    assert res.obs_epochs == 3 and res.nav_messages == 0


@no_leaks
async def test_rinex_2_with_systems_beyond_gps_warns_that_their_navigation_is_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    obs = tmp_path / "r.26o"
    ok = _fake_convbin(tmp_path / "ok-convbin", f'echo x > "{obs}"\n')
    with caplog.at_level(logging.WARNING, logger="mtrtk.rinex.convbin"):
        await run_convbin(
            tmp_path / "x.ubx",
            obs,
            tmp_path / "r.26n",
            ConvbinOptions(header=HEADER, version="2.11", exclude_systems=("R", "S", "I")),
            str(ok),
        )
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "Galileo, QZSS, BeiDou" in warnings[0] and "r.26n" in warnings[0]
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="mtrtk.rinex.convbin"):
        await run_convbin(  # the OPUS shape: GPS only, nothing to lose
            tmp_path / "x.ubx",
            obs,
            tmp_path / "r.26n",
            ConvbinOptions(header=HEADER, version="2.11", exclude_systems=tuple("RESJCI")),
            str(ok),
        )
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("which", ["obs", "nav"])
async def test_run_convbin_refuses_an_output_path_with_a_percent_sign(
    tmp_path: Path, which: str
) -> None:
    """convbin expands `%Y`, `%n`, ... in its output paths: it would write somewhere else."""
    obs, nav = tmp_path / "a.obs", tmp_path / "a.nav"
    odd = tmp_path / "100%Y" / f"a.{which}"
    never = _fake_convbin(tmp_path / "never-convbin", f': > "{tmp_path / "ran"}"\n')
    with pytest.raises(ConvbinError, match="'%'"):
        await run_convbin(
            tmp_path / "x.ubx",
            odd if which == "obs" else obs,
            odd if which == "nav" else nav,
            ConvbinOptions(header=HEADER),
            str(never),
        )
    assert not (tmp_path / "ran").exists() and not (tmp_path / "100%Y").exists()


async def test_run_convbin_refuses_outputs_that_collide(tmp_path: Path) -> None:
    raw = tmp_path / "x.ubx"
    raw.write_bytes(b"precious")
    never = _fake_convbin(tmp_path / "never-convbin", "exit 0\n")
    for obs, nav in (
        (raw, tmp_path / "n.nav"),
        (tmp_path / "o.obs", raw),
        (raw.parent / "s", raw.parent / "." / "s"),
    ):
        with pytest.raises(ConvbinError, match="same file"):
            await run_convbin(raw, obs, nav, ConvbinOptions(header=HEADER), str(never))
    assert raw.read_bytes() == b"precious"


async def test_run_convbin_reports_an_unwritable_output_as_a_convbin_error(tmp_path: Path) -> None:
    obs = tmp_path / "is-a-directory"
    obs.mkdir()
    never = _fake_convbin(tmp_path / "never-convbin", "exit 0\n")
    with pytest.raises(ConvbinError, match="cannot prepare"):
        await run_convbin(
            tmp_path / "x.ubx", obs, tmp_path / "d.nav", ConvbinOptions(header=HEADER), str(never)
        )
