from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from webtest import make_log

from mtrtk.rinex.naming import duration_code, period_code, rinex2_name, rinex3_name
from mtrtk.rinex.splice import NoDataError, splice_window

H0 = datetime(2026, 9, 18, 10, tzinfo=UTC)


def test_codes() -> None:
    assert duration_code(3600) == "01H"
    assert duration_code(6 * 3600) == "06H"
    assert duration_code(86400) == "01D"
    assert duration_code(900) == "15M"
    assert duration_code(2 * 86400) == "02D"
    assert period_code(30) == "30S"
    assert period_code(1) == "01S"
    assert period_code(0.2) == "20C"
    assert period_code(None) == "00U"


def test_rinex3_and_rinex2_names() -> None:
    assert rinex3_name("MTRK", "BGD", H0, 86400, 30) == "MTRK00BGD_R_20262611000_01D_30S_MO.rnx"
    assert (
        rinex3_name("MTRK", "BGD", H0, 3600, None, kind="MN")
        == "MTRK00BGD_R_20262611000_01H_MN.rnx"
    )
    assert rinex2_name("MTRK", H0, 3600) == "mtrk261k.26o"  # hour 10 -> 'k'
    assert rinex2_name("MTRK", H0.replace(hour=0), 86400) == "mtrk2610.26o"
    assert rinex2_name("MTRK", H0, 3600, kind="n") == "mtrk261k.26n"


def test_splice_window_includes_lead_hour_and_concatenates(tmp_path: Path) -> None:
    for i in range(5):
        make_log(tmp_path, H0 + timedelta(hours=i), size=10)
    res = splice_window(
        tmp_path, H0 + timedelta(hours=2), H0 + timedelta(hours=4), tmp_path / "out.ubx"
    )
    assert [f.hour_utc for f in res.files] == [
        H0 + timedelta(hours=1),
        H0 + timedelta(hours=2),
        H0 + timedelta(hours=3),
    ]
    assert res.path.read_bytes() == bytes([11]) * 10 + bytes([12]) * 10 + bytes([13]) * 10
    assert res.bytes == 30
    assert res.lead_hours == 1


def test_splice_window_no_data(tmp_path: Path) -> None:
    with pytest.raises(NoDataError):
        splice_window(tmp_path, H0, H0 + timedelta(hours=1), tmp_path / "out.ubx")
