"""rnx2rtkp option files tuned for ZED-F9P kinematic PPK.

Two RTKLIB builds matter: stock 2.4.3 b34 (`apt install rtklib`, this host and CI) and demo5
v2.5.1 (built into the Docker image). demo5 is preferred, stock is tolerated. Measured against
both on 2026-10-01:

- Every `BASE_OPTIONS` key exists in stock 2.4.3; demo5 v2.5.1 has all of them except
  `pos2-rejgdop`, which it dropped. rnx2rtkp's `loadopts()` silently skips a key it does not
  know, so a stray key is harmless - only a value it cannot parse is reported (`file:line`).
- No `DEMO5_OPTIONS` key exists in stock 2.4.3; demo5 v2.5.1 has every one of them.
- The builds spell some values differently: dual frequency is `l1+l2` on demo5 but `l1+2` on
  stock, and each rejects the other's spelling. Both accept the enum number (`2`), which is what
  `render_conf` writes. Stock's `pos2-gloarmode` takes only `off`/`on` (no `autocal`, no
  `fix-and-hold`); for a binary recognised as stock, `render_conf` falls back to `off`.
- Both builds keep each option name as its own NUL-terminated C string in the executable, which
  is what `rnx2rtkp_supports` looks for instead of trusting a version banner.

`gloarmode=on` (GLONASS integer AR) is only valid between two F9Ps, whose GLONASS inter-channel
biases match; a different base receiver wants `autocal` or `off` through `glonass_ar`.
"""

from __future__ import annotations

import math
import os
import re
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Literal

BASE_OPTIONS: dict[str, str] = {
    "pos1-posmode": "kinematic",
    "pos1-frequency": "l1+l2",
    "pos1-soltype": "combined",
    "pos1-elmask": "15",
    "pos1-snrmask_r": "off",
    "pos1-snrmask_b": "off",
    "pos1-dynamics": "on",
    "pos1-tidecorr": "off",
    "pos1-ionoopt": "brdc",
    "pos1-tropopt": "saas",
    "pos1-sateph": "brdc",
    "pos1-navsys": "45",
    "pos2-armode": "fix-and-hold",
    "pos2-gloarmode": "on",
    "pos2-bdsarmode": "on",
    "pos2-arthres": "3",
    "pos2-arlockcnt": "5",
    "pos2-arelmask": "15",
    "pos2-arminfix": "10",
    "pos2-elmaskhold": "15",
    "pos2-aroutcnt": "20",
    "pos2-maxage": "30",
    "pos2-rejionno": "1",
    "pos2-rejgdop": "30",
    "pos2-slipthres": "0.05",
    "pos2-niter": "1",
    "out-solformat": "llh",
    "out-outhead": "on",
    "out-outopt": "on",
    "out-timesys": "gpst",
    "out-timeform": "hms",
    "out-timendec": "3",
    "out-degform": "deg",
    "out-height": "ellipsoidal",
    "out-outstat": "residual",
    "out-solstatic": "all",
    "stats-errphase": "0.003",
    "stats-errphaseel": "0.003",
    "stats-prnaccelh": "3",
    "stats-prnaccelv": "1",
    "ant2-postype": "xyz",
    "ant2-maxaveep": "1",
    "misc-timeinterp": "off",
}

DEMO5_OPTIONS: dict[str, str] = {
    "pos2-arfilter": "on",
    "pos2-arthresmin": "3",
    "pos2-arthresmax": "3",
    "pos2-arthres1": "0.1",
    "pos2-varholdamb": "0.1",
    "pos2-gainholdamb": "0.01",
    "pos2-minfixsats": "4",
    "pos2-minholdsats": "5",
    "pos2-mindropsats": "10",
    "stats-eratio1": "300",
    "stats-eratio2": "300",
}

# The key demo5 is recognised by: present in every demo5 build, absent from stock 2.4.3.
_DEMO5_PROBE = "pos2-arfilter"

# Recognising stock 2.4.3: it knows this core key but not `_DEMO5_PROBE`. A binary that knows
# neither (a wrapper script, a packed executable, a test fake) is "unknown" and left alone.
_CORE_PROBE = "pos1-posmode"

Build = Literal["demo5", "stock", "unknown"]

# `pos1-frequency` is written as its enum number, which both builds accept and mean the same by
# (stock lists `1:l1,2:l1+2,3:l1+2+3,4:l1+2+3+4`, demo5 `1:l1,2:l1+l2,3:l1+l2+l5,4:l1+l2+l5+l6`),
# so a build that is not recognised still gets dual frequency instead of a rejected spelling.
_FREQUENCY_ENUM: dict[str, str] = {
    "l1": "1",
    "l1+l2": "2",
    "l1+2": "2",
    "l1+l2+l5": "3",
    "l1+2+3": "3",
    "l1+l2+l5+l6": "4",
    "l1+2+3+4": "4",
}

# Values stock 2.4.3 has no equivalent for, and the safe value used instead. Without GLONASS
# integer AR the GLONASS phase still contributes to the float solution.
_STOCK_FALLBACK: dict[str, tuple[frozenset[str], str]] = {
    "pos2-gloarmode": (frozenset({"autocal", "fix-and-hold"}), "off"),
}

# GPS 1 + GLONASS 4 + Galileo 8 + BeiDou 32; QZSS adds 16.
_NAVSYS_QZSS_BIT = 16

# An option name as RTKLIB spells them (`pos1-snrmask_r`, `file-rcvantfile`, `ant2-pos1`).
_KEY_RE = re.compile(r"[a-z0-9]+-[A-Za-z0-9_]+")
# A value may not end its line early (CR/LF/NUL) or be cut short by RTKLIB's comment marker.
_VALUE_FORBIDDEN = frozenset("\r\n\x00#")
_VALUE_MAX = 1024

# |ECEF| of any point a GNSS base can sit at: Earth's radius is 6357-6378 km.
_ECEF_RADIUS_M = (6.2e6, 6.5e6)

# Option names in the executable: each is a NUL-terminated C string, not preceded by a name byte.
_NAME_RE = re.compile(rb"(?<![A-Za-z0-9_-])([A-Za-z0-9_]+-[A-Za-z0-9_-]+)\x00")


def rnx2rtkp_available(binary: str = "rnx2rtkp") -> bool:
    """True when `binary` (a name on PATH, or a path) is an executable file."""
    return shutil.which(binary) is not None


@lru_cache(maxsize=8)
def _option_names(path: str, mtime_ns: int, size: int) -> frozenset[str]:
    # Keyed on the file's identity, so a binary installed, replaced or rebuilt under a running
    # daemon is read again. Raises OSError, which lru_cache does not cache.
    data = Path(path).read_bytes()
    return frozenset(m.decode("ascii") for m in _NAME_RE.findall(data))


def _binary_options(binary: str) -> frozenset[str]:
    path = shutil.which(binary) or binary
    try:
        real = os.path.realpath(path)
        st = os.stat(real)
        return _option_names(real, st.st_mtime_ns, st.st_size)
    except OSError:
        return frozenset()  # missing or unreadable now; not remembered, so a later install counts


def rnx2rtkp_supports(key: str, binary: str = "rnx2rtkp") -> bool:
    """Whether the executable knows option `key`, read from the option table it carries.

    RTKLIB keeps every option name as a NUL-terminated C string in the binary; demo5 adds keys
    stock 2.4.3 lacks. The match is on the whole name, so `pos2-arthres1` does not vouch for
    `pos2-arthres`. A missing or unreadable binary supports nothing, and that answer is not
    cached. The table is cached per resolved path, modification time and size.

    The first call per binary reads the whole executable (a few MB) from disk: async callers
    should run it, `detect_build` and `render_conf` through `asyncio.to_thread`.
    """
    return key in _binary_options(binary)


def detect_build(binary: str = "rnx2rtkp") -> Build:
    """`"demo5"`, `"stock"` (RTKLIB 2.4.3) or `"unknown"` (missing, unreadable, unrecognised).

    rnx2rtkp only warns (`invalid option value KEY (file:line)`) about a value it cannot parse
    and carries on with that option's default, exiting 0. Whoever runs it on a rendered file
    must treat `invalid option` in its output as a failure, above all for an `"unknown"` build.
    """
    names = _binary_options(binary)
    if _DEMO5_PROBE in names:
        return "demo5"
    if _CORE_PROBE in names:
        return "stock"
    return "unknown"


def render_conf(
    base_xyz: tuple[float, float, float],
    *,
    overrides: dict[str, str] | None = None,
    glonass_ar: str = "on",
    include_qzss: bool = False,
    binary: str = "rnx2rtkp",
) -> str:
    """An rnx2rtkp `-k` option file for an F9P rover against a base at ECEF `base_xyz` (m).

    The demo5 tunables are added only when `binary` is a demo5 build. `overrides` win over
    everything, including keys this module does not know. For a binary recognised as stock
    2.4.3, values it has no equivalent for are replaced by a safe one with a comment saying so;
    `render_conf_with_notes` also returns those downgrades. Raises `ValueError` for a base that
    is not a finite ECEF position or an override that is not a well-formed option line.
    """
    return render_conf_with_notes(
        base_xyz,
        overrides=overrides,
        glonass_ar=glonass_ar,
        include_qzss=include_qzss,
        binary=binary,
    )[0]


def render_conf_with_notes(
    base_xyz: tuple[float, float, float],
    *,
    overrides: dict[str, str] | None = None,
    glonass_ar: str = "on",
    include_qzss: bool = False,
    binary: str = "rnx2rtkp",
) -> tuple[str, list[str]]:
    """`render_conf`, plus one human-readable note per value changed or left out for the build.

    Each note is fit for a job warning, e.g. `pos2-gloarmode=autocal needs RTKLIB demo5; this
    build gets off`. The text's own `pos2-gloarmode` is the mode actually used.
    """
    _check_base(base_xyz)
    _check_overrides(overrides or {})
    build = detect_build(binary)
    options = dict(BASE_OPTIONS)
    options["pos2-gloarmode"] = glonass_ar
    if include_qzss:
        options["pos1-navsys"] = str(int(BASE_OPTIONS["pos1-navsys"]) | _NAVSYS_QZSS_BIT)
    if build == "demo5":
        options.update(DEMO5_OPTIONS)
    x, y, z = base_xyz
    options["ant2-pos1"] = f"{x:.4f}"
    options["ant2-pos2"] = f"{y:.4f}"
    options["ant2-pos3"] = f"{z:.4f}"
    if overrides:
        options.update(overrides)
    freq = options.get("pos1-frequency")
    if freq is not None:
        options["pos1-frequency"] = _FREQUENCY_ENUM.get(freq, freq)
    notes: list[str] = []
    if build == "stock":
        notes = _to_stock(options)
    elif build == "unknown":
        notes = [
            f"{binary} was not recognised as RTKLIB demo5 or stock 2.4.3; "
            "the demo5 tunables were left out"
        ]
    lines = ["# rnx2rtkp options generated by mtrtk (ZED-F9P kinematic PPK)"]
    lines += [f"# {note}" for note in notes]
    lines += [f"{key:<20}={value}" for key, value in options.items()]
    return "\n".join(lines) + "\n", notes


def _check_base(base_xyz: tuple[float, float, float]) -> None:
    if len(base_xyz) != 3 or not all(math.isfinite(v) for v in base_xyz):
        raise ValueError(f"base position must be three finite ECEF metres, got {base_xyz!r}")
    radius = math.sqrt(sum(v * v for v in base_xyz))
    lo, hi = _ECEF_RADIUS_M
    if not lo <= radius <= hi:
        raise ValueError(
            f"base position {base_xyz!r} is {radius / 1000:.0f} km from the Earth's centre; "
            "it must be ECEF metres, not latitude/longitude/height"
        )


def _check_overrides(overrides: dict[str, str]) -> None:
    for key, value in overrides.items():
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            raise ValueError(f"override key {key!r} is not an rnx2rtkp option name")
        if not isinstance(value, str):
            raise ValueError(f"override {key} must be a string, got {type(value).__name__}")
        if len(value) > _VALUE_MAX or _VALUE_FORBIDDEN.intersection(value):
            raise ValueError(
                f"override {key} has a value rnx2rtkp cannot read "
                f"(longer than {_VALUE_MAX} or contains CR, LF, NUL or '#')"
            )


def _to_stock(options: dict[str, str]) -> list[str]:
    """Rewrite `options` in place for stock 2.4.3; returns a note per downgrade."""
    notes: list[str] = []
    for key, (unsupported, fallback) in _STOCK_FALLBACK.items():
        if options.get(key) in unsupported:
            notes.append(f"{key}={options[key]} needs RTKLIB demo5; this build gets {fallback}")
            options[key] = fallback
    return notes


def parse_conf(text: str) -> dict[str, str]:
    """`key=value` pairs from an option file; `#` starts a comment, blank lines are skipped."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out
