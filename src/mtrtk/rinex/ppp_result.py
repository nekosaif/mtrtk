"""Parse the result files PPP services send back into one PppResult (ECEF + LLH + 1-sigma).

The parsers are deliberately tolerant (keyword + number regexes) because the services change
their layouts between versions. Nothing here executes content: text is matched with regexes and
numbers go through ``float()``. Zip uploads are read in memory only (nothing is extracted to
disk), with the member count and the inflated size of the one member we read both bounded.

Sigma convention: every sigma on a PppResult is 1-sigma per axis in metres. CSRS-PPP reports
95 % and is divided by 1.96; SINEX STD_DEV and OPUS sigma columns are taken as given.
"""

from __future__ import annotations

import calendar
import io
import lzma
import math
import re
import struct
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Literal

from mtrtk.core.geo import ecef_to_llh, llh_to_ecef

Source = Literal["csrs-ppp", "auspos", "opus", "manual"]
Format = Literal["csrs-sum", "csrs-pos", "sinex", "opus"]
PreferFrame = Literal["itrf", "nad83"]

CSRS_95_TO_1SIGMA = 1.96
MAX_ZIP_MEMBERS = 64
MAX_MEMBER_BYTES = 20 * 1024 * 1024  # same as the upload limit
_ZIP_RESULT_EXTS = (".sum", ".pos", ".snx", ".txt")
DEFAULT_HINT = (
    "Upload the CSRS-PPP .sum/.pos (or the .zip), an AUSPOS SINEX .snx, "
    "or the OPUS e-mail saved as .txt."
)

# Plausibility bounds for a base-station position (the Earth's radius is 6357-6378 km).
MIN_ECEF_RADIUS_M = 6.30e6
MAX_ECEF_RADIUS_M = 6.40e6
MIN_HEIGHT_M = -1000.0
MAX_HEIGHT_M = 10000.0

_LINE_RE = re.compile(r"[^\r\n]+")
_NUM_TOKEN_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_HEMI_TOKEN_RE = re.compile(r"([NSEWnsew-]?)(\d+(?:\.\d+)?)?")
_HEIGHT_LABEL_RE = re.compile(r"ELL(?:IPSOIDAL)?\.?[ \t]*HEIGHT[ \t]*\(m\)", re.IGNORECASE)
_AXIS_LABEL_RE = re.compile(r"([XYZ])[ \t]*\(m\)", re.IGNORECASE)
_SINEX_EPOCH_RE = re.compile(r"(\d{2}):(\d{3}):(\d{5})")
# A frame name must not run on into '.' or '_' so file names such as igs20.atx are skipped.
_FRAME_RE = re.compile(
    r"(ITRF\s?\d{2,4}(?![\d._])|IGS\s?\d{2}(?![\d._])|IGb\d{2}(?![\d._])|NAD_?83\S*)"
    r"\s*\(?(?:EPOCH:?\s*)?(\d{4}\.\d+)?",
    re.IGNORECASE,
)


class PppParseError(ValueError):
    def __init__(self, message: str, hint: str = DEFAULT_HINT) -> None:
        super().__init__(message)
        self.message = message
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
    negative = sign_token.upper() in ("S", "W", "-") or d.strip().startswith("-")
    return -value if negative else value


def _is_num(token: str) -> bool:
    return _NUM_TOKEN_RE.fullmatch(token) is not None


def _decimal_year(year: int, doy: int, seconds_of_day: float) -> str:
    """Decimal year over the real year length, as CSRS-PPP and OPUS label their epochs."""
    days = 366 if calendar.isleap(year) else 365
    return f"{year + (doy - 1 + seconds_of_day / 86400) / days:.4f}"


def _neu_to_ecef_sigmas(
    lat: float, lon: float, s_n: float | None, s_e: float | None, s_u: float | None
) -> tuple[float | None, float | None, float | None]:
    """Rotate diagonal north/east/up 1-sigmas into per-axis ECEF 1-sigmas (correlations dropped)."""
    if s_n is None or s_e is None or s_u is None:
        return None, None, None
    phi, lam = math.radians(lat), math.radians(lon)
    sp, cp, sl, cl = math.sin(phi), math.cos(phi), math.sin(lam), math.cos(lam)
    rows = ((-sp * cl, -sl, cp * cl), (-sp * sl, cl, cp * sl), (cp, 0.0, sp))
    sx, sy, sz = (math.sqrt((a * s_n) ** 2 + (b * s_e) ** 2 + (c * s_u) ** 2) for a, b, c in rows)
    return sx, sy, sz


_NEU_NOTE = "ECEF sigmas rotated from the reported N/E/U sigmas (correlations not available)"


def _ecef_checked(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Refuse an ECEF point that is not near the Earth's surface, then convert it to LLH."""
    if not all(math.isfinite(v) for v in (x, y, z)):
        raise PppParseError("coordinates are not finite numbers")
    radius = math.sqrt(x * x + y * y + z * z)
    if not MIN_ECEF_RADIUS_M <= radius <= MAX_ECEF_RADIUS_M:
        raise PppParseError(
            f"ECEF position is {radius / 1000:.1f} km from the geocentre, "
            "not on the Earth's surface"
        )
    return ecef_to_llh(x, y, z)


def _check(result: PppResult) -> PppResult:
    """Plausibility gate every parser's result passes before it is returned."""
    _ecef_checked(result.x, result.y, result.z)
    if not all(math.isfinite(v) for v in (result.lat, result.lon, result.height_m)):
        raise PppParseError("latitude/longitude/height are not finite numbers")
    if not (-90 <= result.lat <= 90 and -180 <= result.lon <= 360):
        raise PppParseError(f"latitude/longitude {result.lat}, {result.lon} out of range")
    if not MIN_HEIGHT_M <= result.height_m <= MAX_HEIGHT_M:
        raise PppParseError(
            f"ellipsoidal height {result.height_m:.1f} m is outside "
            f"{MIN_HEIGHT_M:.0f}..{MAX_HEIGHT_M:.0f} m"
        )
    for name in ("sigma_x", "sigma_y", "sigma_z"):
        value = getattr(result, name)
        if value is not None and not (math.isfinite(value) and value >= 0):
            raise PppParseError(f"{name} = {value} is not a non-negative finite sigma")
    return result


def detect_format(filename: str, text: str) -> Format:
    name = filename.lower()
    head = text[:4000]
    if "%=SNX" in head or "+SOLUTION/ESTIMATE" in text:
        return "sinex"
    if "DIR FRAME" in head and "LATDD" in text:
        return "csrs-pos"
    if "NGS OPUS" in head or ("REF FRAME:" in text and "EL HGT" in text):
        return "opus"
    if (
        "CSRS-PPP" in head
        or ("LATITUDE" in text and "ELL. HEIGHT" in text.upper())
        or name.endswith(".sum")
    ):
        return "csrs-sum"
    raise PppParseError(f"could not recognise {filename} as a PPP result")


def _frame_epoch(text: str, default_frame: str) -> tuple[str, str | None]:
    m = _FRAME_RE.search(text)
    if not m:
        return default_frame, None
    return m.group(1).replace(" ", ""), m.group(2)


# Every parser below works line by line and splits lines into tokens: regexes only ever see one
# short token or one line with no nested quantifiers, so parsing time is linear in the upload.
# Lines are produced one at a time and the parsers keep only what they use: a 20 MB upload
# split into a list of millions of lines (and of their tokens) would cost the daemon most of
# a Pi's memory.


def _lines(text: str) -> Iterator[str]:
    """The non-blank lines of `text`, lazily (splitlines would build them all at once)."""
    for m in _LINE_RE.finditer(text):
        yield m.group()


def _numbers_after(rest: str) -> list[str]:
    """The run of numeric tokens that starts at the first numeric token on this line."""
    tokens = rest.split()
    first = next((i for i, t in enumerate(tokens) if _is_num(t)), None)
    if first is None:
        return []
    run: list[str] = []
    for token in tokens[first:]:
        if not _is_num(token):
            break
        run.append(token)
    return run


def _angle(rest: str, hemis: str, label: str) -> tuple[float, float | None]:
    """Parse '[(deg min sec)] [N|S|-]DD MM SS.sss [sigma]' into degrees and the optional sigma."""
    tokens = rest.split()
    for i, token in enumerate(tokens):
        m = _HEMI_TOKEN_RE.fullmatch(token)
        if m is None or m.group(1).upper() not in hemis + "-":
            continue
        sign, digits = m.group(1), m.group(2)
        nums = tokens[i + 1 : i + 5]
        if not digits:  # 'N 23 50 14.4'
            if not nums or not _is_num(nums[0]):
                continue
            digits, nums = nums[0], nums[1:]
        if len(nums) < 2 or not (_is_num(nums[0]) and _is_num(nums[1])):
            raise ValueError(f"{label} needs degrees, minutes and seconds")
        sigma = float(nums[2]) if len(nums) > 2 and _is_num(nums[2]) else None
        return _dms_to_deg(sign, digits, nums[0], nums[1]), sigma
    raise ValueError(f"{label} line has no angle")


def _parse_csrs_sum(text: str) -> PppResult:
    lat_rest = lon_rest = datum = None
    hgt: list[str] = []
    xyz: dict[str, list[str]] = {}
    for line in _lines(text):
        if None not in (lat_rest, lon_rest, datum) and hgt and len(xyz) == 3:
            break  # everything this parser reads has been found
        s = line.strip()
        upper = s[:24].upper()
        if lat_rest is None and upper.startswith("LATITUDE"):
            lat_rest = s[len("LATITUDE") :]
        elif lon_rest is None and upper.startswith("LONGITUDE"):
            lon_rest = s[len("LONGITUDE") :]
        elif datum is None and upper.startswith("DATUM"):
            datum = s
        elif not hgt and (m := _HEIGHT_LABEL_RE.match(s)):
            hgt = _numbers_after(s[m.end() :])
        elif (m := _AXIS_LABEL_RE.match(s)) and m.group(1).upper() not in xyz:
            xyz[m.group(1).upper()] = _numbers_after(s[m.end() :])
    if lat_rest is None or lon_rest is None or not hgt:
        raise PppParseError(
            "CSRS-PPP summary: could not find LATITUDE / LONGITUDE / ELL. HEIGHT lines"
        )
    lat, s_lat = _angle(lat_rest, "NS", "LATITUDE")
    lon, s_lon = _angle(lon_rest, "EW", "LONGITUDE")
    height = float(hgt[0])
    notes = ["CSRS-PPP sigmas are 95 %; stored as 1σ (divided by 1.96)"]
    xs, ys, zs = xyz.get("X", []), xyz.get("Y", []), xyz.get("Z", [])
    sx: float | None
    sy: float | None
    sz: float | None
    if xs and ys and zs:
        x, y, z = float(xs[0]), float(ys[0]), float(zs[0])
        sx = float(xs[1]) / CSRS_95_TO_1SIGMA if len(xs) > 1 else None
        sy = float(ys[1]) / CSRS_95_TO_1SIGMA if len(ys) > 1 else None
        sz = float(zs[1]) / CSRS_95_TO_1SIGMA if len(zs) > 1 else None
    else:
        x, y, z = llh_to_ecef(lat, lon, height)
        notes.append("Cartesian coordinates computed from LLH (not present in the summary)")
        s_h = float(hgt[1]) if len(hgt) > 1 else None
        sx, sy, sz = _neu_to_ecef_sigmas(
            lat,
            lon,
            *(v / CSRS_95_TO_1SIGMA if v is not None else None for v in (s_lat, s_lon, s_h)),
        )
        notes.append(_NEU_NOTE if sx is not None else "sigmas incomplete in the summary")
    # The Datum line names the frame; ANTEX or product file names elsewhere must not win.
    frame, epoch = _frame_epoch(datum, "ITRF2020") if datum else ("ITRF2020", None)
    if not datum or not _FRAME_RE.search(datum):
        frame, epoch = _frame_epoch(text, "ITRF2020")
    return PppResult(
        "csrs-ppp", "csrs-sum", frame, epoch, x, y, z, sx, sy, sz, lat, lon, height, notes
    )


def _pos_seconds(hms: str) -> float:
    hh, mm, ss = hms.split(":")
    return int(hh) * 3600 + int(mm) * 60 + float(ss)


def _parse_csrs_pos(text: str) -> PppResult:
    lines = _lines(text)
    cols = next((ln.split() for ln in lines if ln.startswith("DIR") and "LATDD" in ln), None)
    if cols is None:
        raise PppParseError("CSRS-PPP .pos: header line with LATDD/LONDD columns not found")
    # Only rows with every column are trusted: a missing field would shift all later columns.
    # Only the first and the last are used, so only they are kept.
    first: list[str] | None = None
    last: list[str] = []
    width = len(cols)
    for ln in lines:
        parts = ln.split()
        if len(parts) == width:
            last = parts
            if first is None:
                first = parts
    if first is None:
        raise PppParseError("CSRS-PPP .pos: no epoch rows with all header columns")
    col = {name: i for i, name in enumerate(cols)}
    lat = _dms_to_deg("", last[col["LATDD"]], last[col["LATMN"]], last[col["LATSS"]])
    lon = _dms_to_deg("", last[col["LONDD"]], last[col["LONMN"]], last[col["LONSS"]])
    height = float(last[col["HGT(m)"]])
    x, y, z = llh_to_ecef(lat, lon, height)

    def sigma(name: str) -> float | None:
        return float(last[col[name]]) / CSRS_95_TO_1SIGMA if name in col else None

    sx, sy, sz = _neu_to_ecef_sigmas(
        lat, lon, sigma("SDLAT(95%)"), sigma("SDLON(95%)"), sigma("SDHGT(95%)")
    )
    frame = last[col["FRAME"]] if "FRAME" in col else "ITRF2020"
    epoch = None
    if "YEAR-MM-DD" in col:
        # A static solution refers to the middle of the data span (the .sum reports the same).
        stamps = []
        for row in (first, last):
            yyyy, mm, dd = (int(v) for v in row[col["YEAR-MM-DD"]].split("-"))
            secs = _pos_seconds(row[col["HR:MN:SS.SSS"]]) if "HR:MN:SS.SSS" in col else 43200.0
            stamps.append(datetime(yyyy, mm, dd) + timedelta(seconds=secs))
        mid = stamps[0] + (stamps[1] - stamps[0]) / 2
        day_secs = mid.hour * 3600 + mid.minute * 60 + mid.second + mid.microsecond / 1e6
        epoch = _decimal_year(mid.year, mid.timetuple().tm_yday, day_secs)
    notes = [
        "position from the last epoch of the .pos file (static solution converges there); "
        "epoch is the middle of the data span",
        "CSRS-PPP sigmas are 95 %; stored as 1σ",
        _NEU_NOTE if sx is not None else "sigmas incomplete in the .pos file",
    ]
    return PppResult(
        "csrs-ppp", "csrs-pos", frame, epoch, x, y, z, sx, sy, sz, lat, lon, height, notes
    )


def _pick_site(complete: list[str], filename: str, station_id: str | None) -> tuple[str, str]:
    """Choose the user's station among the SINEX sites; never guess between several."""
    wanted = station_id.strip().upper() if station_id else None
    if wanted and wanted in complete:
        return wanted, f"matched station id {wanted}"
    stem = PurePosixPath(filename.replace("\\", "/")).stem.upper()[:4]
    if stem in complete:
        return stem, f"matched the file name {filename}"
    if len(complete) == 1:
        why = f"only site in the file; does not match station id {wanted}" if wanted else ""
        return complete[0], why
    codes = ", ".join(sorted(complete))
    raise PppParseError(
        f"SINEX estimates several stations ({codes}) and none matches the station id or the "
        "file name",
        hint=f"Name the upload after your station (e.g. {complete[0]}.snx) or pass its id.",
    )


def _parse_sinex(text: str, filename: str, station_id: str | None) -> PppResult:
    # str.find instead of a DOTALL regex: one pass, however many unclosed openers there are.
    opener = text.find("+SOLUTION/ESTIMATE")
    closer = text.find("-SOLUTION/ESTIMATE", opener) if opener >= 0 else -1
    if closer < 0:
        raise PppParseError("SINEX: SOLUTION/ESTIMATE block not found")
    # Network solutions (AUSPOS) estimate the reference stations too: keep each site apart.
    sites: dict[str, dict[str, tuple[float, float | None, str]]] = {}
    block = text[opener + len("+SOLUTION/ESTIMATE") : closer]
    for line in _lines(block):
        parts = line.split()
        if len(parts) >= 9 and parts[1] in ("STAX", "STAY", "STAZ"):
            value = float(parts[8].replace("D", "E"))
            std = float(parts[9].replace("D", "E")) if len(parts) > 9 else None
            sites.setdefault(parts[2].upper(), {})[parts[1]] = (value, std, parts[5])
    complete = [code for code, v in sites.items() if set(v) == {"STAX", "STAY", "STAZ"}]
    if not complete:
        raise PppParseError("SINEX: STAX/STAY/STAZ estimates missing")
    code, why = _pick_site(complete, filename, station_id)
    x, sx, ref = sites[code]["STAX"]
    y, sy, _ = sites[code]["STAY"]
    z, sz, _ = sites[code]["STAZ"]
    epoch = None
    m = _SINEX_EPOCH_RE.match(ref)
    if m:
        yy, doy, sec = (int(v) for v in m.groups())
        epoch = _decimal_year(2000 + yy if yy < 80 else 1900 + yy, doy, sec)
    lat, lon, h = _ecef_checked(x, y, z)
    notes = ["SINEX STD_DEV taken as 1σ"]
    if not _FRAME_RE.search(text):
        notes.append("SINEX names no reference frame; assumed ITRF2020")
    frame, _ = _frame_epoch(text, "ITRF2020")
    others = [c for c in complete if c != code]
    used = f"used site {code}" + (f" ({why})" if why else "")
    if others:
        used += f"; also estimated: {', '.join(others)}"
    notes.append(used)
    return PppResult("auspos", "sinex", frame, epoch, x, y, z, sx, sy, sz, lat, lon, h, notes)


def _opus_frames(rest: str) -> tuple[str, str, str, str]:
    """'NAD_83(2011)(EPOCH:2010.0000)   IGS20 (EPOCH:2026.7137)' -> both frames and epochs."""
    parts = rest.split("(EPOCH:")
    if len(parts) < 3:
        raise PppParseError("OPUS: REF FRAME line does not name two frames")
    epoch1, _, frame2 = parts[1].partition(")")
    epoch2 = parts[2].partition(")")[0]
    return parts[0].strip(), epoch1.strip(), frame2.strip(), epoch2.strip()


def _parse_opus(text: str, prefer_frame: PreferFrame) -> PppResult:
    frames: tuple[str, str, str, str] | None = None
    axes: dict[str, list[str]] = {}
    for line in _lines(text):
        if frames is not None and len(axes) == 3:
            break  # everything this parser reads has been found
        s = line.strip()
        if frames is None and s.startswith("REF FRAME:"):
            frames = _opus_frames(s[len("REF FRAME:") :])
        elif s[:2] in ("X:", "Y:", "Z:") and s[0] not in axes:
            axes[s[0]] = s[2:].replace("(m)", " ").split()
    if frames is None:
        raise PppParseError("OPUS: REF FRAME line not found")
    use_second = prefer_frame != "nad83"
    frame = frames[2] if use_second else frames[0]
    epoch = frames[3] if use_second else frames[1]

    def axis(label: str) -> tuple[float, float]:
        values = axes.get(label)
        if values is None:
            raise PppParseError(f"OPUS: {label} line not found")
        if len(values) < 4 or not all(_is_num(v) for v in values[:4]):
            raise ValueError(f"OPUS {label} line needs two value/sigma pairs")
        if use_second:
            return float(values[2]), float(values[3])
        return float(values[0]), float(values[1])

    (x, sx), (y, sy), (z, sz) = axis("X"), axis("Y"), axis("Z")
    lat, lon, h = _ecef_checked(x, y, z)
    notes = [f"OPUS {frame} column used; sigmas taken as reported"]
    return PppResult("opus", "opus", frame, epoch, x, y, z, sx, sy, sz, lat, lon, h, notes)


def _is_zip(filename: str, content: bytes) -> bool:
    return filename.lower().endswith(".zip") or content[:4] in (b"PK\x03\x04", b"PK\x05\x06")


def _safe_member(info: zipfile.ZipInfo) -> bool:
    path = PurePosixPath(info.filename.replace("\\", "/"))
    return not info.is_dir() and not path.is_absolute() and ".." not in path.parts


_EOCD = b"PK\x05\x06"
_EOCD_SIZE = 22
_ZIP_COMMENT_MAX = 0xFFFF


def _declared_members(content: bytes) -> int | None:
    """The member count the end-of-central-directory record declares, read before `ZipFile`
    builds an object per entry - 200 000 entries cost it about 100 MB. None when not found."""
    tail = content[-(_EOCD_SIZE + _ZIP_COMMENT_MAX) :]
    at = tail.rfind(_EOCD)
    if at < 0 or len(tail) - at < _EOCD_SIZE:
        return None
    (total,) = struct.unpack_from("<H", tail, at + 10)
    return int(total)


def _read_zip_result(content: bytes) -> tuple[str, bytes]:
    declared = _declared_members(content)
    if declared is not None and declared > MAX_ZIP_MEMBERS:  # 0xFFFF (zip64) included
        raise PppParseError(f"zip has {declared} members; at most {MAX_ZIP_MEMBERS} are accepted")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_MEMBERS:
                raise PppParseError(
                    f"zip has {len(infos)} members; at most {MAX_ZIP_MEMBERS} are accepted"
                )
            candidates = [i for i in infos if _safe_member(i)]
            for ext in _ZIP_RESULT_EXTS:
                pick = next((i for i in candidates if i.filename.lower().endswith(ext)), None)
                if pick is None:
                    continue
                # Read at most limit+1 bytes: the declared size in the header is not trusted.
                with zf.open(pick) as member:
                    data = member.read(MAX_MEMBER_BYTES + 1)
                if len(data) > MAX_MEMBER_BYTES:
                    raise PppParseError(
                        f"{pick.filename} inflates to larger than {MAX_MEMBER_BYTES} bytes"
                    )
                return pick.filename, data
    # Every way a damaged archive fails to inflate: deflate (zlib.error), bzip2 (OSError),
    # LZMA (LZMAError), encryption (RuntimeError), unknown methods, truncation.
    except (
        zipfile.BadZipFile,
        zlib.error,
        lzma.LZMAError,
        OSError,
        RuntimeError,
        NotImplementedError,
        EOFError,
    ) as exc:
        raise PppParseError(f"not a valid zip file ({exc})") from exc
    raise PppParseError("zip contains no .sum, .pos, .snx or .txt result")


def _parse_text(
    filename: str, content: bytes, prefer_frame: PreferFrame, station_id: str | None
) -> PppResult:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    if "\x00" in text[:1000]:
        raise PppParseError(f"{filename} looks binary, not a text result")
    fmt = detect_format(filename, text)
    try:
        if fmt == "csrs-sum":
            result = _parse_csrs_sum(text)
        elif fmt == "csrs-pos":
            result = _parse_csrs_pos(text)
        elif fmt == "sinex":
            result = _parse_sinex(text, filename, station_id)
        else:
            result = _parse_opus(text, prefer_frame)
        return _check(result)
    except PppParseError:
        raise
    except (ValueError, IndexError, KeyError, ArithmeticError) as exc:
        raise PppParseError(f"{filename} looks like {fmt} but a field is malformed: {exc}") from exc


def parse_ppp_result(
    filename: str, content: bytes, prefer_frame: str = "itrf", station_id: str | None = None
) -> PppResult:
    """Parse one uploaded result file (or a .zip holding one) into a PppResult.

    ``prefer_frame`` ("itrf" or "nad83", any case) only matters for OPUS, which reports both
    NAD 83 and an ITRF-aligned frame. ``station_id`` picks the user's station out of a SINEX
    that also estimates reference stations (the file name's first four letters are tried next).
    Raises PppParseError (with a ``hint``) for anything it cannot read.
    """
    frame = prefer_frame.strip().lower()
    if frame not in ("itrf", "nad83"):
        raise PppParseError(
            f"prefer_frame must be 'itrf' or 'nad83', not {prefer_frame!r}",
            hint="Choose itrf (the default) or nad83; it only matters for OPUS reports.",
        )
    choice: PreferFrame = "nad83" if frame == "nad83" else "itrf"
    if _is_zip(filename, content):
        filename, content = _read_zip_result(content)
    return _parse_text(filename, content, choice, station_id)
