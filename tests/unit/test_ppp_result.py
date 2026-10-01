import io
import time
import zipfile
from pathlib import Path

import pytest

from mtrtk.rinex import ppp_result
from mtrtk.rinex.ppp_result import PppParseError, detect_format, parse_ppp_result

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "ppp"
X, Y, Z = -26748.1720, 5837156.6184, 2561801.2607
LAT, LON = 23.8373506, 90.2625502


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


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
    assert r.sigma_x == pytest.approx(0.0070 / 1.96, abs=1e-6)
    assert r.sigma_y == pytest.approx(0.0150 / 1.96, abs=1e-6)
    assert r.sigma_z == pytest.approx(0.0080 / 1.96, abs=1e-6)
    assert r.lat == pytest.approx(LAT, abs=1e-7) and r.lon == pytest.approx(LON, abs=1e-7)
    assert r.height_m == pytest.approx(-36.268, abs=1e-3)
    assert any("95" in n for n in r.notes)


def test_csrs_pos_uses_last_epoch() -> None:
    r = parse_ppp_result("MTRK.pos", (FIX / "csrs_sample.pos").read_bytes())
    assert r.format == "csrs-pos" and r.frame == "ITRF20"
    assert r.lat == pytest.approx(LAT, abs=1e-7) and r.lon == pytest.approx(LON, abs=1e-7)
    assert r.height_m == pytest.approx(-36.268, abs=1e-4)
    assert (r.x, r.y, r.z) == pytest.approx((X, Y, Z), abs=0.02)  # computed from LLH
    # N/E/U 95 % sigmas (0.006, 0.009, 0.021) rotated into ECEF at 23.8N 90.3E: up lies mostly
    # along +Y, so Y carries the height sigma (ruling: replaces the brief's sigma_y == 0.009/1.96).
    assert (r.sigma_x, r.sigma_y, r.sigma_z) == pytest.approx(
        (0.004592, 0.009878, 0.005157), abs=2e-6
    )
    assert r.epoch == "2026.7137"  # mid-span of the rows, same as the .sum reports
    assert any("N/E/U" in n for n in r.notes)


def test_sinex() -> None:
    r = parse_ppp_result("AUSPOS.SNX", (FIX / "auspos_sample.snx").read_bytes())
    assert r.source == "auspos" and r.format == "sinex"
    assert (r.x, r.y, r.z) == pytest.approx((X, Y, Z), abs=1e-4)
    assert (r.sigma_x, r.sigma_y, r.sigma_z) == (0.004, 0.006, 0.005)
    assert r.epoch == "2026.7137"  # 26:261:43200 -> 2026 + (260 + 0.5) / 365
    assert r.lat == pytest.approx(LAT, abs=1e-6)


def test_opus_prefers_itrf_column() -> None:
    r = parse_ppp_result("opus.txt", (FIX / "opus_sample.txt").read_bytes())
    assert r.source == "opus" and r.frame == "IGS20" and r.epoch == "2026.7137"
    assert (r.x, r.y, r.z) == pytest.approx((X, Y, Z), abs=1e-3)
    assert r.sigma_x == 0.005
    nad = parse_ppp_result("opus.txt", (FIX / "opus_sample.txt").read_bytes(), prefer_frame="nad83")
    assert nad.frame.startswith("NAD_83") and nad.x == pytest.approx(-26748.900, abs=1e-3)


def test_zip_picks_sum_first() -> None:
    content = _zip(
        {
            "MTRK.pos": (FIX / "csrs_sample.pos").read_bytes(),
            "MTRK.sum": (FIX / "csrs_sample.sum").read_bytes(),
            "MTRK.pdf": b"%PDF-1.4 not parsed",
        }
    )
    r = parse_ppp_result("result.zip", content)
    assert r.format == "csrs-sum"


def test_garbage_raises_with_hint() -> None:
    with pytest.raises(PppParseError) as exc:
        parse_ppp_result("weird.dat", b"hello world")
    assert "csrs" in exc.value.hint.lower()


def test_binary_content_is_refused_even_when_it_looks_like_a_result() -> None:
    with pytest.raises(PppParseError, match="binary") as exc:
        parse_ppp_result("x.snx", b"+SOLUTION/ESTIMATE\x00\x01-SOLUTION/ESTIMATE")
    assert exc.value.hint


def test_suggested_site_name() -> None:
    r = parse_ppp_result("MTRK.sum", (FIX / "csrs_sample.sum").read_bytes())
    assert r.suggested_site_name("MTRK") == "MTRK-csrs-ppp-2026.71"


def test_zip_member_over_size_limit_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # A highly compressible member stands in for a decompression bomb: the parser must stop
    # reading at the limit instead of inflating the whole thing.
    monkeypatch.setattr(ppp_result, "MAX_MEMBER_BYTES", 1024)
    content = _zip({"MTRK.sum": b" " * 4096})
    assert len(content) < 1024
    sizes: list[int] = []
    returned: list[int] = []
    original = zipfile.ZipExtFile.read

    def spy(self: zipfile.ZipExtFile, n: int | None = -1) -> bytes:
        sizes.append(-1 if n is None else n)
        data = original(self, n)
        returned.append(len(data))
        return data

    monkeypatch.setattr(zipfile.ZipExtFile, "read", spy)
    with pytest.raises(PppParseError, match="larger than"):
        parse_ppp_result("result.zip", content)
    assert sizes and all(0 <= n <= 1024 + 1 for n in sizes)
    assert sum(returned) <= 1024 + 1


def test_zip_with_exactly_max_members_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ppp_result, "MAX_ZIP_MEMBERS", 3)
    members = {f"f{i}.pdf": b"x" for i in range(2)}
    members["MTRK.sum"] = (FIX / "csrs_sample.sum").read_bytes()
    assert parse_ppp_result("result.zip", _zip(members)).format == "csrs-sum"


def test_zip_with_too_many_members_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ppp_result, "MAX_ZIP_MEMBERS", 3)
    members = {f"f{i}.pdf": b"x" for i in range(4)}
    members["MTRK.sum"] = (FIX / "csrs_sample.sum").read_bytes()
    with pytest.raises(PppParseError, match="members"):
        parse_ppp_result("result.zip", _zip(members))


def test_zip_ignores_unsafe_paths_and_nested_zips() -> None:
    inner = _zip({"MTRK.sum": (FIX / "csrs_sample.sum").read_bytes()})
    content = _zip(
        {
            "../evil.sum": (FIX / "csrs_sample.sum").read_bytes(),
            "/abs/evil.sum": (FIX / "csrs_sample.sum").read_bytes(),
            "..\\win\\evil.sum": (FIX / "csrs_sample.sum").read_bytes(),
            "nested.zip": inner,
            "MTRK.snx": (FIX / "auspos_sample.snx").read_bytes(),
        }
    )
    assert parse_ppp_result("result.zip", content).format == "sinex"
    with pytest.raises(PppParseError, match="no .sum"):
        parse_ppp_result("result.zip", _zip({"nested.zip": inner}))


def test_bad_zip_raises() -> None:
    with pytest.raises(PppParseError, match="not a valid zip"):
        parse_ppp_result("result.zip", b"PK\x03\x04 truncated")


def test_zip_is_sniffed_by_content_not_only_by_name() -> None:
    content = _zip({"MTRK.sum": (FIX / "csrs_sample.sum").read_bytes()})
    assert parse_ppp_result("upload.bin", content).format == "csrs-sum"
    with pytest.raises(PppParseError, match="not a valid zip"):
        parse_ppp_result("upload.bin", b"PK\x03\x04 truncated")


def test_corrupt_zip_member_raises_parse_error() -> None:
    data = (FIX / "csrs_sample.sum").read_bytes() * 4
    content = bytearray(_zip({"MTRK.sum": data}))
    start = content.index(b"MTRK.sum") + len("MTRK.sum")
    for i in range(start + 4, start + 40):  # scribble over the deflate stream
        content[i] ^= 0xFF
    with pytest.raises(PppParseError, match="not a valid zip"):
        parse_ppp_result("result.zip", bytes(content))


def test_encrypted_zip_member_raises_parse_error() -> None:
    content = bytearray(_zip({"MTRK.sum": (FIX / "csrs_sample.sum").read_bytes()}))
    # Set the "encrypted" general-purpose flag in the local and the central header.
    content[content.index(b"PK\x03\x04") + 6] |= 0x1
    content[content.index(b"PK\x01\x02") + 8] |= 0x1
    with pytest.raises(PppParseError, match="not a valid zip"):
        parse_ppp_result("result.zip", bytes(content))


# --- prefer_frame is a plain str (Tasks 5/6 pass one) -------------------------------------


def test_prefer_frame_is_case_insensitive_and_validated() -> None:
    content = (FIX / "opus_sample.txt").read_bytes()
    frame: str = "NAD83"
    assert parse_ppp_result("opus.txt", content, prefer_frame=frame).frame.startswith("NAD_83")
    assert parse_ppp_result("opus.txt", content, prefer_frame="ITRF").frame == "IGS20"
    with pytest.raises(PppParseError, match="prefer_frame"):
        parse_ppp_result("opus.txt", content, prefer_frame="wgs84")


# --- hemisphere signs ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lat_txt", "lon_txt", "lat_sign", "lon_sign"),
    [
        ("S23 50 14.46220", "E90 15 45.18070", -1, 1),
        ("N23 50 14.46220", "W90 15 45.18070", 1, -1),
        ("-23 50 14.46220", "-90 15 45.18070", -1, -1),
        ("S 23 50 14.46220", "W 90 15 45.18070", -1, -1),
    ],
)
def test_csrs_sum_hemisphere_signs(
    lat_txt: str, lon_txt: str, lat_sign: int, lon_sign: int
) -> None:
    text = (FIX / "csrs_sample.sum").read_text()
    text = text.replace("N23 50 14.46220", lat_txt).replace("E90 15 45.18070", lon_txt)
    r = parse_ppp_result("MTRK.sum", text.encode())
    assert r.lat == pytest.approx(lat_sign * LAT, abs=1e-7)
    assert r.lon == pytest.approx(lon_sign * LON, abs=1e-7)
    assert (r.x, r.y, r.z) == pytest.approx((X, Y, Z), abs=1e-4)  # ECEF still from the file


def test_csrs_pos_negative_degrees() -> None:
    text = (FIX / "csrs_sample.pos").read_text()
    text = text.replace(
        "    23    50 14.46220    90    15 45.18070", "   -23    50 14.46220   -90    15 45.18070"
    )
    r = parse_ppp_result("MTRK.pos", text.encode())
    assert r.lat == pytest.approx(-LAT, abs=1e-7) and r.lon == pytest.approx(-LON, abs=1e-7)


def _sum_without_cartesian() -> str:
    text = (FIX / "csrs_sample.sum").read_text()
    head, _, tail = text.partition(" 3.3 Cartesian coordinates (m)")
    return head + tail[tail.index(" SECTION 4") :]


def test_csrs_sum_without_cartesian_computes_ecef_and_rotates_sigmas() -> None:
    r = parse_ppp_result("MTRK.sum", _sum_without_cartesian().encode())
    assert (r.x, r.y, r.z) == pytest.approx((X, Y, Z), abs=0.01)
    assert (r.sigma_x, r.sigma_y, r.sigma_z) == pytest.approx(
        (0.004592, 0.009878, 0.005157), abs=2e-6
    )
    assert any("from LLH" in n for n in r.notes) and any("N/E/U" in n for n in r.notes)


def test_csrs_sum_sigma_never_taken_from_the_next_line() -> None:
    text = _sum_without_cartesian()
    text = text.replace("-36.2680                0.0210", "-36.2680")
    text = text.replace(" SECTION 4", " 3.3 more\n SECTION 4")
    r = parse_ppp_result("MTRK.sum", text.encode())
    assert r.height_m == pytest.approx(-36.268)
    assert r.sigma_x is None and r.sigma_y is None and r.sigma_z is None


def test_csrs_sum_frame_comes_from_the_datum_line() -> None:
    text = (FIX / "csrs_sample.sum").read_text()
    text = text.replace(" Datum           :", " Antenna model   : igs20.atx\n Datum           :")
    r = parse_ppp_result("MTRK.sum", text.encode())
    assert r.frame == "ITRF20" and r.epoch == "2026.7137"


def test_csrs_pos_rows_with_missing_columns_are_not_used() -> None:
    text = (FIX / "csrs_sample.pos").read_text().rstrip("\n")
    last = text.splitlines()[-1]
    text += "\n" + last.replace("MTRK", "", 1).replace("-36.2680", "-99.0000") + "\n"
    r = parse_ppp_result("MTRK.pos", text.encode())
    assert r.height_m == pytest.approx(-36.268, abs=1e-4)
    only_short = "\n".join(text.splitlines()[:3]) + "\n" + last.replace("MTRK", "", 1) + "\n"
    with pytest.raises(PppParseError, match="no epoch rows"):
        parse_ppp_result("MTRK.pos", only_short.encode())


# --- SINEX with several sites -------------------------------------------------------------

_MULTI_SNX = """%=SNX 2.02 AUS 26:262:00000 AUS 26:261:00000 26:261:86370 P 00009 2 X
+SOLUTION/ESTIMATE
*INDEX TYPE__ CODE PT SOLN _REF_EPOCH__ UNIT S __ESTIMATED VALUE____ _STD_DEV___
     1 STAX   ALIC  A    1 26:261:43200 m    2 -4.05205e+06 1.0e-03
     2 STAX   BAKO  A    1 26:261:43200 m    2 -1.83696900000000e+06 2.00000e-03
     3 STAY   BAKO  A    1 26:261:43200 m    2  6.06557600000000e+06 2.00000e-03
     4 STAZ   BAKO  A    1 26:261:43200 m    2 -7.16300000000000e+05 2.00000e-03
     5 STAX   MTRK  A    1 26:261:43200 m    2 -2.67481720000000e+04 4.00000e-03
     6 STAY   MTRK  A    1 26:261:43200 m    2  5.83715661840000e+06
     7 STAZ   MTRK  A    1 26:261:43200 m    2  2.56180126070000e+06 5.00000e-03
-SOLUTION/ESTIMATE
%ENDSNX
"""


def test_sinex_multi_site_needs_a_station_hint() -> None:
    with pytest.raises(PppParseError, match="BAKO.*MTRK") as exc:
        parse_ppp_result("AUSPOS.SNX", _MULTI_SNX.encode())
    assert exc.value.hint


@pytest.mark.parametrize(
    ("filename", "station_id"), [("AUSPOS.SNX", "mtrk"), ("MTRK2610.SNX", None)]
)
def test_sinex_multi_site_picks_the_named_station(filename: str, station_id: str | None) -> None:
    r = parse_ppp_result(filename, _MULTI_SNX.encode(), station_id=station_id)
    assert r.x == pytest.approx(X, abs=1e-4)
    assert r.sigma_x == 0.004 and r.sigma_y is None and r.sigma_z == 0.005
    assert any("BAKO" in n for n in r.notes)


def test_sinex_single_site_is_used_even_when_the_hint_differs() -> None:
    r = parse_ppp_result("AUSPOS.SNX", (FIX / "auspos_sample.snx").read_bytes(), station_id="ABCD")
    assert r.x == pytest.approx(X, abs=1e-4)
    assert any("ABCD" in n for n in r.notes)


# --- malformed and implausible content ----------------------------------------------------


def _sinex_with(value: str) -> bytes:
    text = (FIX / "auspos_sample.snx").read_text()
    for old in ("-2.67481720000000e+04", " 5.83715661840000e+06", " 2.56180126070000e+06"):
        text = text.replace(old, " " + value)
    return text.encode()


@pytest.mark.parametrize("value", ["nan", "inf", "1e999", "0", "1.0"])
def test_implausible_coordinates_are_refused(value: str) -> None:
    with pytest.raises(PppParseError) as exc:
        parse_ppp_result("x.snx", _sinex_with(value))
    assert exc.value.hint


def test_negative_or_nonfinite_sigma_is_refused() -> None:
    text = (FIX / "auspos_sample.snx").read_text().replace("4.00000e-03", "-4.00000e-03")
    with pytest.raises(PppParseError, match="sigma"):
        parse_ppp_result("x.snx", text.encode())
    text = (FIX / "auspos_sample.snx").read_text().replace("4.00000e-03", "nan")
    with pytest.raises(PppParseError, match="sigma"):
        parse_ppp_result("x.snx", text.encode())


def test_implausible_height_is_refused() -> None:
    text = (FIX / "csrs_sample.pos").read_text().replace("-36.2680", "25000.0000")
    with pytest.raises(PppParseError, match="height"):
        parse_ppp_result("MTRK.pos", text.encode())


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("MTRK.pos", (FIX / "csrs_sample.pos").read_text().replace("-36.2680", "-36.2x80")),
        ("x.snx", (FIX / "auspos_sample.snx").read_text().replace("4.00000e-03", "4.0x")),
        (
            "MTRK.sum",
            (FIX / "csrs_sample.sum")
            .read_text()
            .replace("N23 50 14.46220               0.0060", "N23 50"),
        ),
    ],
    ids=["pos-height", "sinex-std-dev", "sum-latitude"],
)
def test_malformed_field_raises_parse_error(filename: str, content: str) -> None:
    with pytest.raises(PppParseError, match="malformed") as exc:
        parse_ppp_result(filename, content.encode())
    assert exc.value.hint


# --- adversarial input stays linear -------------------------------------------------------

_MB = 1024 * 1024


@pytest.mark.parametrize(
    "content",
    [
        b"CSRS-PPP\nLATITUDE " + b"1" * _MB,
        b"CSRS-PPP\nELL. HEIGHT (m) " + b"1 " * (_MB // 2),
        b"%=SNX\n" + b"+SOLUTION/ESTIMATE\n" * (_MB // 19),
        b"NGS OPUS\nREF FRAME: " + b"(EPOCH:1)" * (_MB // 18) + b" " + b"x" * (_MB // 2),
        b"NGS OPUS\nREF FRAME: A (EPOCH:1) B (EPOCH:2)\n" + b"\n" * _MB,
        b"DIR FRAME LATDD\n" + b"1 " * (_MB // 2),
    ],
    ids=[
        "sum-angle",
        "sum-number-run",
        "sinex-openers",
        "opus-frames",
        "opus-blank-lines",
        "pos-wide-row",
    ],
)
def test_adversarial_megabyte_parses_or_fails_fast(content: bytes) -> None:
    start = time.perf_counter()
    with pytest.raises(PppParseError):
        parse_ppp_result("x.txt", content)
    assert time.perf_counter() - start < 1.0


# ------------------------------------------------- final review: bounded memory, damaged zips


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
@pytest.mark.parametrize("offset", [39, 47, 55, 75])
def test_a_damaged_bzip2_or_lzma_member_is_a_parse_error(method: int, offset: int) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=method) as zf:
        zf.writestr("r.pos", (FIX / "csrs_sample.pos").read_bytes() * 20)
    data = bytearray(buf.getvalue())
    for i in range(offset, offset + 8):
        data[i] ^= 0xFF
    with pytest.raises(PppParseError):
        parse_ppp_result("r.zip", bytes(data))


def test_a_zip_declaring_too_many_members_is_refused_before_it_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end record's count is checked before ZipFile builds an object per entry."""
    built: list[int] = []
    real = zipfile.ZipFile.__init__

    def spy(self: zipfile.ZipFile, *a: object, **kw: object) -> None:
        built.append(1)
        real(self, *a, **kw)  # type: ignore[arg-type]

    content = _zip({f"f{i}.pdf": b"x" for i in range(ppp_result.MAX_ZIP_MEMBERS + 1)})
    monkeypatch.setattr(zipfile.ZipFile, "__init__", spy)
    with pytest.raises(PppParseError, match="members"):
        parse_ppp_result("result.zip", content)
    assert built == []


def test_parsing_a_large_pos_keeps_only_what_it_uses() -> None:
    """A 5 MB .pos of short rows used to peak at about 40x its size (every line and every split
    row kept); it now holds the text and a few rows."""
    import tracemalloc

    head = (FIX / "csrs_sample.pos").read_bytes()
    body = head + b"ab cd ef\n" * (5 * 1024 * 1024 // 9)
    tracemalloc.start()
    try:
        parse_ppp_result("big.pos", body)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 3 * len(body), peak
