"""Thin async wrapper around RTKLIB `convbin` for UBX -> RINEX conversion.

Two builds have to work: stock RTKLIB 2.4.3 b34, which Debian/Ubuntu ship as `rtklib` and CI
installs, and demo5 v2.5.1, which `docker/Dockerfile` builds. What follows was measured against
both on the Phase 1 fixtures, and every flag emitted here is accepted by both.

- `-scan` is real on 2.4.3 and an accepted no-op on demo5 (which always scans), yet neither
  build's usage banner lists it. Rather than trust the banner, `convbin_supports` asks the
  binary once per process; a build that answers with its banner does not get the flag.
- `-ts`/`-te` each take *two* argv tokens, `y/m/d` then `h:m:s`. One combined token is read as
  that date at midnight, the next flag is eaten as the time of day, and the window is ignored.
- `-ro` is one string. A second `-ro` replaces the first, so the options are joined into one.
- The start bound is inclusive on both builds; the end bound is inclusive on 2.4.3 and
  exclusive on demo5. `-te` is therefore written 10 ms before the end (convbin reads fractional
  seconds), which gives both builds the same window, [start, end), so back-to-back exports share
  no epoch.
- Header values are `strcpy`d into 32-byte buffers and split on "/" by `strtok`, which skips
  empty parts. An overlong value aborts 2.4.3 ("buffer overflow detected") and spills into the
  next field on demo5; an empty or slashed part shifts every field after it. `_fit` and
  `_parts` keep each value inside its RINEX field and in its own slot.
- RINEX 3 navigation data for every system goes to the one `-n` file (spec open item 2).
  RINEX 2 puts only GPS navigation there; other systems would need outputs of their own, so
  a RINEX 2 run that keeps them logs a warning naming what its navigation file lacks.
- `-o`/`-n` paths go through RTKLIB's keyword expansion (`%Y`, `%n`, ...): a "%" in one would
  send the file elsewhere, so such a path is refused.
- `-y` reads only the first letter of its value and ignores one it does not know; the system
  letters, the RINEX version and the frequency count are checked before convbin sees them.

The command is a list handed to `asyncio.create_subprocess_exec`: no shell, nothing quoted.

Times: `-ts`/`-te` are compared with the RINEX epoch stamps, which are GPST. A naive datetime
is written as it reads; an aware one as its UTC wall-clock time. The GPS-UTC offset is never
applied here - see `ConvbinOptions`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shlex
import shutil
import signal
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 900.0  # an hour of 1 Hz multi-GNSS raw converts in seconds; this is a leash
PROBE_TIMEOUT_S = 10.0  # a flag probe does no work: it prints one line and exits
REAP_TIMEOUT_S = 5.0  # after SIGKILL, how long to wait for the pipes to close
STDERR_TAIL_CHARS = 2000  # kept on the result for the job log
ERROR_TAIL_CHARS = 500  # quoted into the raised error

# RINEX header field widths, capped by convbin's 32-byte buffers (31 characters and a NUL).
MARKER_WIDTH = 60
FIELD_WIDTH = 20
AGENCY_WIDTH = 31  # RINEX allows 40
COMMENT_WIDTH = 60

# `-te` goes in this much before the window's end: [start, end) on both builds (module doc).
END_MARGIN = timedelta(milliseconds=10)

RINEX_VERSIONS = frozenset({"2.10", "2.11", "2.12", "3.00", "3.01", "3.02", "3.03", "3.04"})
SYSTEM_NAMES = {  # convbin's -y letters
    "G": "GPS",
    "R": "GLONASS",
    "E": "Galileo",
    "S": "SBAS",
    "J": "QZSS",
    "C": "BeiDou",
    "I": "NavIC",
}
MAX_FREQUENCIES = 5

# The progress record, `... O=60 N=23`. Anchored on both sides so an `O=` inside the input path
# convbin echoes first (`input file : /data/INFO=2/...`) is never taken for a count.
_SUMMARY_RE = re.compile(r"(?:^|[\s:])O=(\d+)(?:[ \t]+N=(\d+))?(?=\s|$)")
# What convbin prints instead of doing anything when it meets an option it does not know.
# 2.4.3 spells the heading "Synopsys", demo5 "Synopsis".
_USAGE_MARKERS = ("Synopsys", "Synopsis", "[option ...]")

# (resolved binary, flag) -> supported. Probing twice costs one 5 ms exec, so no lock: a
# module-level `asyncio.Lock` would bind to whichever event loop touched it first.
_flag_support: dict[tuple[str, str], bool] = {}


class ConvbinError(RuntimeError):
    """`convbin` is missing, exited badly, outran its timeout, or wrote no observation file."""


@dataclass(frozen=True)
class RinexHeader:
    """The header fields convbin can be told to write. A flag whose every part is blank is left
    out of the command, so convbin keeps its own default rather than stamping a blank field."""

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
    """What to convert. `start`/`end` bound the window [start, end) and are **GPST** wall-clock
    time, the scale of the RINEX epoch stamps convbin compares them with. A naive datetime is
    taken as GPST; an aware one is converted to UTC and that wall-clock time is used as GPST,
    with no leap-second offset applied. A caller holding a UTC window must add the GPS-UTC
    offset (18 s since 2017) itself, or accept a window that many seconds early."""

    header: RinexHeader
    version: str = "3.04"
    interval_s: float | None = None
    exclude_systems: tuple[str, ...] = ()  # convbin letters: G R E J S C I
    frequencies: int = 2
    start: datetime | None = None
    end: datetime | None = None
    receiver_options: tuple[str, ...] = ("-TADJ=1.0",)  # snap epochs onto whole seconds


@dataclass(frozen=True)
class ConvbinResult:
    obs_path: Path
    nav_path: Path
    obs_epochs: int
    nav_messages: int  # 0: the window carried no ephemeris and `nav_path` is an empty file
    stderr_tail: str


def convbin_available(binary: str = "convbin") -> bool:
    """True when `binary` names something we can actually execute - a name on PATH or a path to
    an executable file. A non-executable file of the same name does not count: it would only
    turn a clear "not found" into a raw OSError further down."""
    return shutil.which(binary) is not None


async def convbin_supports(flag: str, binary: str = "convbin") -> bool:
    """Ask the binary itself whether it knows the argument-less option `flag`, and cache the
    answer for this process.

    convbin answers an option it does not recognise by printing its usage banner and exiting 0;
    one it does recognise, with no input file to work on, by printing "no input file" and
    exiting non-zero. That difference is the probe. (An option that takes a value looks unknown
    to it here, so this only works for flags like `-scan`.) A probe that cannot run at all
    leaves the flag enabled and is not cached: the flags probed are known good on both builds.
    """
    resolved = shutil.which(binary) or binary
    key = (resolved, flag)
    cached = _flag_support.get(key)
    if cached is not None:
        return cached
    try:
        _, answer = await _exec([resolved, flag], PROBE_TIMEOUT_S)
    except OSError as exc:  # TimeoutError included
        log.warning("could not probe %s for %s: %s", resolved, flag, exc)
        return True
    supported = not any(marker in answer for marker in _USAGE_MARKERS)
    if not supported:
        # A departure from the mandated flag set: loud enough to show in an export job's log.
        log.warning("%s does not support %s; leaving it out", resolved, flag)
    _flag_support[key] = supported
    return supported


def _time_args(dt: datetime) -> list[str]:
    """`y/m/d` and `h:m:s` as two argv tokens; a fraction of a second is kept (convbin reads
    the seconds with `%lf`)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    clock = dt.strftime("%H:%M:%S")
    if dt.microsecond:
        clock += f".{dt.microsecond:06d}".rstrip("0")
    return [dt.strftime("%Y/%m/%d"), clock]


def _check_options(opts: ConvbinOptions) -> None:
    """Refuse what convbin would misread rather than reject: it takes the first letter of a
    `-y` value (so "GPS" excludes GPS) and ignores a letter it does not know."""
    if opts.version not in RINEX_VERSIONS:
        raise ValueError(f"RINEX version {opts.version!r} is not one of {sorted(RINEX_VERSIONS)}")
    for letter in opts.exclude_systems:
        if len(letter) != 1 or letter not in SYSTEM_NAMES:
            raise ValueError(f"system {letter!r} is not one of {', '.join(SYSTEM_NAMES)}")
    if not 1 <= opts.frequencies <= MAX_FREQUENCIES:
        raise ValueError(f"frequencies must be 1..{MAX_FREQUENCIES}, not {opts.frequencies}")


def _fit(value: str, width: int) -> str:
    """One header value as convbin can hold it: printable ASCII (RINEX is ASCII; whitespace
    becomes a space, anything else "?"), at most `width` characters."""
    text = "".join(ch if " " <= ch <= "~" else " " if ch.isspace() else "?" for ch in value)
    return text.strip()[:width].rstrip()


def _parts(*parts: tuple[str, int]) -> str | None:
    """One header flag's value, or None when every part is blank. Where convbin splits the
    value on "/" (more than one part), a "/" inside a part becomes "-" and a blank part goes in
    as a space: `strtok` skips an empty one and would move the parts after it a field left."""
    fitted = [_fit(value, width) for value, width in parts]
    if not any(fitted):
        return None
    if len(fitted) == 1:
        return fitted[0]
    return "/".join(part.replace("/", "-") or " " for part in fitted)


def _header_args(h: RinexHeader) -> list[str]:
    args: list[str] = []
    for flag, value in (
        ("-hm", _parts((h.marker_name, MARKER_WIDTH))),
        ("-hn", _parts((h.marker_number, FIELD_WIDTH))),
        ("-ht", _parts((h.marker_type, FIELD_WIDTH))),
        ("-ho", _parts((h.observer, FIELD_WIDTH), (h.agency, AGENCY_WIDTH))),
        (
            "-hr",
            _parts(
                (h.receiver_number, FIELD_WIDTH),
                (h.receiver_type, FIELD_WIDTH),
                (h.receiver_version, FIELD_WIDTH),
            ),
        ),
        ("-ha", _parts((h.antenna_number, FIELD_WIDTH), (h.antenna_type, FIELD_WIDTH))),
    ):
        if value is not None:
            args += [flag, value]
    if h.approx_xyz:
        args += ["-hp", "/".join(f"{v:.4f}" for v in h.approx_xyz)]
    args += ["-hd", "/".join(f"{v:.4f}" for v in h.delta_hen)]
    comment = _parts((h.comment or "", COMMENT_WIDTH))
    if comment is not None:
        args += ["-hc", comment]
    return args


def build_convbin_command(
    input_path: Path,
    obs_out: Path,
    nav_out: Path,
    opts: ConvbinOptions,
    binary: str = "convbin",
    *,
    scan: bool = True,
) -> list[str]:
    """The full argv. `scan` is what `convbin_supports("-scan", binary)` said; the default
    leaves it on, which is right for both builds this project ships. Raises `ValueError` for
    a version, system letter or frequency count convbin cannot take."""
    _check_options(opts)
    cmd: list[str] = [
        binary,
        "-r",
        "ubx",
        "-v",
        opts.version,
        "-od",  # doppler
        "-os",  # snr
        "-oi",  # iono correction in the nav header
        "-ot",  # time correction in the nav header
        "-ol",  # leap seconds in the nav header
        "-f",
        str(opts.frequencies),
    ]
    if scan:
        cmd.append("-scan")
    if opts.receiver_options:
        cmd += ["-ro", " ".join(opts.receiver_options)]
    if opts.interval_s is not None and opts.interval_s > 0:
        cmd += ["-ti", f"{opts.interval_s:g}"]
    if opts.start:
        cmd += ["-ts", *_time_args(opts.start)]
    if opts.end:
        cmd += ["-te", *_time_args(opts.end - END_MARGIN)]
    for sysletter in opts.exclude_systems:
        cmd += ["-y", sysletter]
    cmd += _header_args(opts.header)
    cmd += ["-o", str(obs_out), "-n", str(nav_out), str(input_path)]
    return cmd


def parse_convbin_summary(stderr: str) -> tuple[int, int]:
    """Epoch and navigation-message counts out of convbin's last progress record (`O=… N=…`).
    The two counts have to be on one line: an `N=` further down belongs to a later record."""
    matches = _SUMMARY_RE.findall(stderr)
    if not matches:
        return 0, 0
    obs, nav = matches[-1]
    return int(obs), int(nav or 0)


async def _kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the child's whole process group - anything it started holds its pipes too - and
    drain the pipes to EOF, so the process is reaped and its transport closed before the
    caller's exception goes on. The group is the child's own (`start_new_session`), so its id
    is the child's pid and stays reserved for as long as any member is alive."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    try:
        await asyncio.wait_for(proc.communicate(), REAP_TIMEOUT_S)
    except TimeoutError:
        log.warning("convbin (pid %d) still holds its pipes after SIGKILL", proc.pid)


async def _exec(cmd: list[str], timeout_s: float) -> tuple[int, str]:
    """Run `cmd` to completion: (exit status, stderr then stdout). On timeout or cancellation
    the child and everything it started are killed and reaped before the error propagates."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
    except BaseException:
        await _kill_and_reap(proc)
        raise
    assert proc.returncode is not None  # communicate() returns only once the child is reaped
    return proc.returncode, (stderr + stdout).decode("utf-8", "replace")


def _check_paths(input_path: Path, obs_out: Path, nav_out: Path) -> None:
    for path in (obs_out, nav_out):
        if "%" in str(path):
            raise ConvbinError(
                f"output path {path} contains '%', which convbin expands as a keyword"
            )
    # Outputs are unlinked before the run: one that is the input would destroy it.
    named = [os.path.abspath(p) for p in (input_path, obs_out, nav_out)]
    if len(set(named)) != len(named):
        raise ConvbinError(
            f"input {input_path}, observation {obs_out} and navigation {nav_out} "
            "must not be the same file"
        )


def _lost_navigation(opts: ConvbinOptions) -> list[str]:
    """The systems whose navigation a RINEX 2 `-n` file leaves out (it holds GPS only)."""
    if not opts.version.startswith("2"):
        return []
    return [
        name
        for letter, name in SYSTEM_NAMES.items()
        if letter != "G" and letter not in opts.exclude_systems
    ]


def _prepare_outputs(*paths: Path) -> None:
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Anything left by an earlier attempt would otherwise be handed back as this run's.
        path.unlink(missing_ok=True)


def _discard(*paths: Path) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


def _settle(obs_out: Path, nav_out: Path, obs_epochs: int) -> int | None:
    """After a clean exit: the epoch count (counted from the file's RINEX 3 `>` records when
    convbin printed none - both builds always print one, RINEX 2 included), or None when there
    is no observation file. A missing navigation file - convbin writes none when the window
    carried no ephemeris - is created empty, as the caller is promised a path."""
    if not obs_out.is_file():
        return None
    if obs_epochs == 0:
        with obs_out.open(errors="replace") as fh:
            obs_epochs = sum(1 for line in fh if line.startswith(">"))
    if not nav_out.exists():
        nav_out.write_text("")
    return obs_epochs


def _exit_description(returncode: int) -> str:
    if returncode < 0:
        with contextlib.suppress(ValueError):
            return f"was killed by {signal.Signals(-returncode).name}"
    return f"exited {returncode}"


async def run_convbin(
    input_path: Path,
    obs_out: Path,
    nav_out: Path,
    opts: ConvbinOptions,
    binary: str = "convbin",
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ConvbinResult:
    """Convert one raw UBX log to a RINEX observation and navigation file pair.

    Raises `ConvbinError` - carrying convbin's own last words - when the binary is missing,
    exits non-zero, outruns `timeout_s`, or finishes without writing an observation file (which
    is how it answers a truncated or corrupt log, or a window with no data: exit 0 and nothing
    on disk); also when an output path is unusable. Whatever it wrote is removed first, on that
    and on cancellation alike. Raises `ValueError` for options convbin cannot take.
    """
    _check_options(opts)
    _check_paths(input_path, obs_out, nav_out)
    if not convbin_available(binary):
        raise ConvbinError(f"convbin not found ({binary}); install RTKLIB or use the Docker image")
    try:
        await asyncio.to_thread(_prepare_outputs, obs_out, nav_out)
    except OSError as exc:
        raise ConvbinError(f"cannot prepare the output files: {exc}") from exc
    if lost := _lost_navigation(opts):
        log.warning(
            "RINEX %s keeps only GPS navigation in %s; navigation for %s is not written",
            opts.version,
            nav_out,
            ", ".join(lost),
        )

    scan = await convbin_supports("-scan", binary)
    cmd = build_convbin_command(input_path, obs_out, nav_out, opts, binary, scan=scan)
    log.info("running: %s", shlex.join(cmd))
    try:
        returncode, text = await _exec(cmd, timeout_s)
    except TimeoutError as exc:  # before OSError, which it subclasses
        await asyncio.to_thread(_discard, obs_out, nav_out)
        raise ConvbinError(
            f"convbin timed out after {timeout_s:g}s converting {input_path}"
        ) from exc
    except OSError as exc:
        raise ConvbinError(f"cannot run convbin ({binary}): {exc}") from exc
    except asyncio.CancelledError:
        await asyncio.to_thread(_discard, obs_out, nav_out)
        raise

    tail = text[-STDERR_TAIL_CHARS:]
    last_words = tail.strip()[-ERROR_TAIL_CHARS:]
    if returncode != 0:
        await asyncio.to_thread(_discard, obs_out, nav_out)
        raise ConvbinError(f"convbin {_exit_description(returncode)}: {last_words}")
    obs_epochs, nav_messages = parse_convbin_summary(text)
    try:
        settled = await asyncio.to_thread(_settle, obs_out, nav_out, obs_epochs)
    except OSError as exc:
        await asyncio.to_thread(_discard, obs_out, nav_out)
        raise ConvbinError(f"cannot read back what convbin wrote: {exc}") from exc
    if settled is None:
        await asyncio.to_thread(_discard, nav_out)
        if opts.start or opts.end:
            # Indistinguishable from a corrupt log in convbin's output, so name both.
            window = f"between {opts.start or 'the start'} and {opts.end or 'the end'}"
            raise ConvbinError(
                f"convbin produced no observation file: no epochs {window} (GPST) in "
                f"{input_path}, or the log is unreadable: {last_words}"
            )
        raise ConvbinError(f"convbin produced no observation file: {last_words}")
    return ConvbinResult(obs_out, nav_out, settled, nav_messages, tail)
