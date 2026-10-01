import io
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
    assert r.sigma_y == pytest.approx(0.0090 / 1.96, abs=1e-6)


def test_sinex() -> None:
    r = parse_ppp_result("AUSPOS.SNX", (FIX / "auspos_sample.snx").read_bytes())
    assert r.source == "auspos" and r.format == "sinex"
    assert (r.x, r.y, r.z) == pytest.approx((X, Y, Z), abs=1e-4)
    assert (r.sigma_x, r.sigma_y, r.sigma_z) == (0.004, 0.006, 0.005)
    assert r.epoch is not None and (r.epoch == "2026.7137" or r.epoch.startswith("2026.7"))
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
        parse_ppp_result("weird.dat", b"\x00\x01binary")
    assert "csrs" in exc.value.hint.lower()


def test_suggested_site_name() -> None:
    r = parse_ppp_result("MTRK.sum", (FIX / "csrs_sample.sum").read_bytes())
    assert r.suggested_site_name("MTRK") == "MTRK-csrs-ppp-2026.71"


def test_zip_member_over_size_limit_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # A highly compressible member stands in for a decompression bomb: the parser must stop
    # reading at the limit instead of inflating the whole thing.
    monkeypatch.setattr(ppp_result, "MAX_MEMBER_BYTES", 1024)
    content = _zip({"MTRK.sum": b" " * 4096})
    assert len(content) < 1024
    with pytest.raises(PppParseError, match="larger than"):
        parse_ppp_result("result.zip", content)


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
            "nested.zip": inner,
            "MTRK.snx": (FIX / "auspos_sample.snx").read_bytes(),
        }
    )
    assert parse_ppp_result("result.zip", content).format == "sinex"
    with pytest.raises(PppParseError, match="no .sum"):
        parse_ppp_result("result.zip", _zip({"nested.zip": inner}))


def test_bad_zip_raises() -> None:
    with pytest.raises(PppParseError, match="zip"):
        parse_ppp_result("result.zip", b"PK\x03\x04 truncated")
