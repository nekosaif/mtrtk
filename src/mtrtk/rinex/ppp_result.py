"""Parse the result files PPP services send back into one PppResult (ECEF + LLH + 1-sigma).

The parsers are deliberately tolerant (keyword + number regexes) because the services change
their layouts between versions. Nothing here executes content: text is matched with regexes and
numbers go through ``float()``. Zip uploads are read in memory only (nothing is extracted to
disk), with the member count and the inflated size of the one member we read both bounded.

Sigma convention: every sigma on a PppResult is 1-sigma per axis in metres. CSRS-PPP reports
95 % and is divided by 1.96; SINEX STD_DEV and OPUS sigma columns are taken as given.
"""

from __future__ import annotations

import io
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import date
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

_NUM = r"[-+]?\d+(?:\.\d+)?"
_FRAME_RE = re.compile(
    r"(ITRF\s?\d{2,4}|IGS\s?\d{2}|IGb\d{2}|NAD_?83\S*)\s*\(?(?:EPOCH:?\s*)?(\d{4}\.\d+)?",
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


def _parse_csrs_sum(text: str) -> PppResult:
    def grab(label_re: str) -> list[str]:
        pattern = label_re + r"[^\n]*?(" + _NUM + r"(?:\s+" + _NUM + r")*)"
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).split() if m else []

    def angle(label: str, hemis: str) -> re.Match[str] | None:
        dms = r"\s+".join([f"({_NUM})"] * 3)
        pattern = rf"{label}[^\n]*?([{hemis}-]?)\s*{dms}(?:\s+({_NUM}))?"
        return re.search(pattern, text, re.IGNORECASE)

    lat_m, lon_m = angle("LATITUDE", "NS"), angle("LONGITUDE", "EW")
    hgt = grab(r"ELL(?:IPSOIDAL)?\.?\s*HEIGHT\s*\(m\)")
    xs, ys, zs = grab(r"\bX\s*\(m\)"), grab(r"\bY\s*\(m\)"), grab(r"\bZ\s*\(m\)")
    if not (lat_m and lon_m and hgt):
        raise PppParseError(
            "CSRS-PPP summary: could not find LATITUDE / LONGITUDE / ELL. HEIGHT lines"
        )
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
        horizontal = [v for v in (s_lat, s_lon) if v is not None]
        sx = sy = max(horizontal) if horizontal else None
        sz = float(hgt[1]) / CSRS_95_TO_1SIGMA if len(hgt) > 1 else None
    frame, epoch = _frame_epoch(text, "ITRF2020")
    return PppResult(
        "csrs-ppp", "csrs-sum", frame, epoch, x, y, z, sx, sy, sz, lat, lon, height, notes
    )


def _parse_csrs_pos(text: str) -> PppResult:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header_idx = next(
        (i for i, ln in enumerate(lines) if ln.startswith("DIR") and "LATDD" in ln), None
    )
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
        doy = date(yyyy, mm, dd).timetuple().tm_yday
        epoch = f"{yyyy + (doy - 0.5) / 365.25:.4f}"
    horizontal = [v for v in (s_lat, s_lon) if v is not None]
    sxy = max(horizontal) if horizontal else None
    notes = [
        "taken from the last epoch of the .pos file (static solution converges there)",
        "CSRS-PPP sigmas are 95 %; stored as 1σ",
    ]
    return PppResult(
        "csrs-ppp", "csrs-pos", frame, epoch, x, y, z, sxy, sxy, s_h, lat, lon, height, notes
    )


def _parse_sinex(text: str) -> PppResult:
    block = re.search(r"\+SOLUTION/ESTIMATE(.*?)-SOLUTION/ESTIMATE", text, re.S)
    if not block:
        raise PppParseError("SINEX: SOLUTION/ESTIMATE block not found")
    # Network solutions (AUSPOS) estimate the reference stations too: keep each site apart.
    sites: dict[str, dict[str, tuple[float, float | None, str]]] = {}
    for line in block.group(1).splitlines():
        parts = line.split()
        if len(parts) >= 9 and parts[1] in ("STAX", "STAY", "STAZ"):
            value = float(parts[8].replace("D", "E"))
            std = float(parts[9].replace("D", "E")) if len(parts) > 9 else None
            sites.setdefault(parts[2], {})[parts[1]] = (value, std, parts[5])
    complete = [code for code, v in sites.items() if set(v) == {"STAX", "STAY", "STAZ"}]
    if not complete:
        raise PppParseError("SINEX: STAX/STAY/STAZ estimates missing")
    code = complete[0]
    x, sx, ref = sites[code]["STAX"]
    y, sy, _ = sites[code]["STAY"]
    z, sz, _ = sites[code]["STAZ"]
    epoch = None
    m = re.match(r"(\d{2}):(\d{3}):(\d{5})", ref)
    if m:
        yy, doy, sec = (int(v) for v in m.groups())
        year = 2000 + yy if yy < 80 else 1900 + yy
        epoch = f"{year + (doy - 1 + sec / 86400) / 365.25:.4f}"
    lat, lon, h = ecef_to_llh(x, y, z)
    notes = ["SINEX STD_DEV taken as 1σ"]
    if not _FRAME_RE.search(text):
        notes.append("SINEX names no reference frame; assumed ITRF2020")
    frame, _ = _frame_epoch(text, "ITRF2020")
    if len(complete) > 1:
        others = ", ".join(complete[1:])
        notes.append(f"SINEX holds several sites; used {code} (also estimated: {others})")
    return PppResult("auspos", "sinex", frame, epoch, x, y, z, sx, sy, sz, lat, lon, h, notes)


def _parse_opus(text: str, prefer_frame: PreferFrame) -> PppResult:
    frames = re.search(
        r"REF FRAME:\s*(\S+)\s*\(EPOCH:\s*([\d.]+)\)\s+(\S+)\s*\(EPOCH:\s*([\d.]+)\)", text
    )
    if not frames:
        raise PppParseError("OPUS: REF FRAME line not found")
    use_second = prefer_frame != "nad83"
    frame = frames.group(3) if use_second else frames.group(1)
    epoch = frames.group(4) if use_second else frames.group(2)

    def axis(label: str) -> tuple[float, float]:
        num_m = r"(" + _NUM + r")\(m\)"
        pattern = rf"^\s*{label}:\s*" + r"\s+".join([num_m] * 4)
        m = re.search(pattern, text, re.M)
        if not m:
            raise PppParseError(f"OPUS: {label} line not found")
        if use_second:
            return float(m.group(3)), float(m.group(4))
        return float(m.group(1)), float(m.group(2))

    (x, sx), (y, sy), (z, sz) = axis("X"), axis("Y"), axis("Z")
    lat, lon, h = ecef_to_llh(x, y, z)
    notes = [f"OPUS {frame} column used; sigmas taken as reported"]
    return PppResult("opus", "opus", frame, epoch, x, y, z, sx, sy, sz, lat, lon, h, notes)


def _is_zip(filename: str, content: bytes) -> bool:
    return filename.lower().endswith(".zip") or content[:4] in (b"PK\x03\x04", b"PK\x05\x06")


def _safe_member(info: zipfile.ZipInfo) -> bool:
    path = PurePosixPath(info.filename.replace("\\", "/"))
    return not info.is_dir() and not path.is_absolute() and ".." not in path.parts


def _read_zip_result(content: bytes) -> tuple[str, bytes]:
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
    except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError, EOFError) as exc:
        raise PppParseError(f"not a valid zip file ({exc})") from exc
    raise PppParseError("zip contains no .sum, .pos, .snx or .txt result")


def _parse_text(filename: str, content: bytes, prefer_frame: PreferFrame) -> PppResult:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    if "\x00" in text[:1000]:
        raise PppParseError(f"{filename} looks binary, not a text result")
    fmt = detect_format(filename, text)
    try:
        if fmt == "csrs-sum":
            return _parse_csrs_sum(text)
        if fmt == "csrs-pos":
            return _parse_csrs_pos(text)
        if fmt == "sinex":
            return _parse_sinex(text)
        return _parse_opus(text, prefer_frame)
    except PppParseError:
        raise
    except (ValueError, IndexError, KeyError) as exc:
        raise PppParseError(f"{filename} looks like {fmt} but a field is malformed: {exc}") from exc


def parse_ppp_result(
    filename: str, content: bytes, prefer_frame: PreferFrame = "itrf"
) -> PppResult:
    """Parse one uploaded result file (or a .zip holding one) into a PppResult.

    ``prefer_frame`` only matters for OPUS, which reports both NAD 83 and an ITRF-aligned frame.
    Raises PppParseError (with a ``hint``) for anything it cannot read.
    """
    if _is_zip(filename, content):
        filename, content = _read_zip_result(content)
    return _parse_text(filename, content, prefer_frame)
