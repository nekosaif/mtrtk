"""rnx2rtkp option files tuned for ZED-F9P kinematic PPK.

Two RTKLIB builds matter: stock 2.4.3 b34 (`apt install rtklib`, this host and CI) and demo5
v2.5.1 (built into the Docker image). demo5 is preferred, stock is tolerated. Measured against
both on 2026-10-01:

- Every `BASE_OPTIONS` key exists in stock 2.4.3; demo5 v2.5.1 has all of them except
  `pos2-rejgdop`, which it dropped. rnx2rtkp's `loadopts()` silently skips a key it does not
  know, so a stray key is harmless - only a value it cannot parse is reported (`file:line`).
- No `DEMO5_OPTIONS` key exists in stock 2.4.3; demo5 v2.5.1 has every one of them.
- The builds spell some values differently: dual frequency is `l1+l2` on demo5 but `l1+2` on
  stock, and stock's `pos2-gloarmode` takes only `off`/`on` (no `autocal`, no `fix-and-hold`).
  `render_conf` writes the demo5 spelling and rewrites it for a binary it recognises as stock.
- Both builds keep each option name as its own NUL-terminated C string in the executable, which
  is what `rnx2rtkp_supports` looks for instead of trusting a version banner.

`gloarmode=on` (GLONASS integer AR) is only valid between two F9Ps, whose GLONASS inter-channel
biases match; a different base receiver wants `autocal` or `off` through `glonass_ar`.
"""

from __future__ import annotations

import re
import shutil
from functools import lru_cache
from pathlib import Path

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
# neither (missing, unreadable, a test fake) is left alone.
_CORE_PROBE = "pos1-posmode"

# demo5 spelling -> stock 2.4.3 spelling of the same value.
_STOCK_SPELLING: dict[str, dict[str, str]] = {
    "pos1-frequency": {"l1+l2": "l1+2", "l1+l2+l5": "l1+2+3", "l1+l2+l5+l6": "l1+2+3+4"},
}

# Values stock 2.4.3 has no equivalent for, and the safe value used instead. Without GLONASS
# integer AR the GLONASS phase still contributes to the float solution.
_STOCK_FALLBACK: dict[str, tuple[frozenset[str], str]] = {
    "pos2-gloarmode": (frozenset({"autocal", "fix-and-hold"}), "off"),
}

# GPS 1 + GLONASS 4 + Galileo 8 + BeiDou 32; QZSS adds 16.
_NAVSYS_QZSS_BIT = 16


def rnx2rtkp_available(binary: str = "rnx2rtkp") -> bool:
    """True when `binary` (a name on PATH, or a path) is an executable file."""
    return shutil.which(binary) is not None


@lru_cache(maxsize=8)
def _binary_bytes(binary: str) -> bytes:
    path = shutil.which(binary) or binary
    try:
        return Path(path).read_bytes()
    except OSError:
        return b""


def rnx2rtkp_supports(key: str, binary: str = "rnx2rtkp") -> bool:
    """Whether the executable knows option `key`, read from the option table it carries.

    RTKLIB keeps every option name as a NUL-terminated C string in the binary; demo5 adds keys
    stock 2.4.3 lacks. The match is on the whole name, so `pos2-arthres1` does not vouch for
    `pos2-arthres`. A missing or unreadable binary supports nothing. Cached per `binary` string.
    """
    pattern = rb"(?<![A-Za-z0-9_-])" + re.escape(key.encode()) + rb"\x00"
    return re.search(pattern, _binary_bytes(binary)) is not None


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
    2.4.3, values it spells differently are rewritten, and values it has no equivalent for are
    replaced by a safe one with a comment saying so.
    """
    options = dict(BASE_OPTIONS)
    options["pos2-gloarmode"] = glonass_ar
    if include_qzss:
        options["pos1-navsys"] = str(int(BASE_OPTIONS["pos1-navsys"]) | _NAVSYS_QZSS_BIT)
    demo5 = rnx2rtkp_supports(_DEMO5_PROBE, binary)
    if demo5:
        options.update(DEMO5_OPTIONS)
    x, y, z = base_xyz
    options["ant2-pos1"] = f"{x:.4f}"
    options["ant2-pos2"] = f"{y:.4f}"
    options["ant2-pos3"] = f"{z:.4f}"
    if overrides:
        options.update(overrides)
    notes: list[str] = []
    if not demo5 and rnx2rtkp_supports(_CORE_PROBE, binary):
        notes = _to_stock(options)
    lines = ["# rnx2rtkp options generated by mtrtk (ZED-F9P kinematic PPK)", *notes]
    lines += [f"{key:<20}={value}" for key, value in options.items()]
    return "\n".join(lines) + "\n"


def _to_stock(options: dict[str, str]) -> list[str]:
    """Rewrite `options` in place for stock 2.4.3; returns a comment line per downgrade."""
    notes: list[str] = []
    for key, spellings in _STOCK_SPELLING.items():
        if key in options:
            options[key] = spellings.get(options[key], options[key])
    for key, (unsupported, fallback) in _STOCK_FALLBACK.items():
        if options.get(key) in unsupported:
            notes.append(f"# {key}={options[key]} needs RTKLIB demo5; this build gets {fallback}")
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
