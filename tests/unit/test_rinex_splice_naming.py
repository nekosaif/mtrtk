from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from webtest import make_log

from mtrtk.rawlog.index import LogFile
from mtrtk.rinex import splice
from mtrtk.rinex.naming import duration_code, period_code, rinex2_name, rinex3_name
from mtrtk.rinex.splice import MixedStationsError, NoDataError, splice_window

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


def test_duration_code_stays_two_digits() -> None:
    assert duration_code(30) == "30S"
    assert duration_code(90) == "90S"
    assert duration_code(120) == "02M"
    assert duration_code(5400) == "90M"
    assert duration_code(6000) == "02H"  # 100 min does not fit in minutes: round up
    assert duration_code(9000) == "03H"  # a 2.5 h export
    assert duration_code(4.5 * 86400) == "05D"
    assert duration_code(100 * 3600) == "05D"
    assert duration_code(0.4) == "01S"
    assert duration_code(1000 * 86400) == "99D"
    for s in (1, 59, 61, 3599, 3601, 9000, 86399, 86401, 388800, 10**7):
        assert len(duration_code(s)) == 3


def test_period_code_boundaries() -> None:
    assert period_code(60) == "01M"
    assert period_code(0) == "00U"
    assert period_code(3600) == "01H"
    assert period_code(0.05) == "05C"
    with pytest.raises(ValueError):
        period_code(1.5)  # 1.5 s and 2.5 s must not both become '02S'
    with pytest.raises(ValueError):
        period_code(150)  # not representable in two digits of any unit
    with pytest.raises(ValueError):
        period_code(0.001)


def test_rinex2_session_letters() -> None:
    assert rinex2_name("MTRK", H0.replace(hour=0), 3600) == "mtrk261a.26o"
    assert rinex2_name("MTRK", H0.replace(hour=23), 3600) == "mtrk261x.26o"
    assert rinex2_name("MTRK", H0.replace(hour=0), 86399) == "mtrk261a.26o"


def test_names_reject_bad_codes_and_naive_times() -> None:
    assert rinex3_name("mtrk", "bgd", H0, 3600, 30, source="S").startswith("MTRK00BGD_S_")
    with pytest.raises(ValueError):
        rinex3_name("AB", "BGD", H0, 3600, 30)
    with pytest.raises(ValueError):
        rinex3_name("MTRK", "BD", H0, 3600, 30)
    with pytest.raises(ValueError):
        rinex3_name("MTRKX", "BGD", H0, 3600, 30)
    with pytest.raises(ValueError):
        rinex3_name("MTRK", "BGD", H0.replace(tzinfo=None), 3600, 30)
    with pytest.raises(ValueError):
        rinex2_name("MTRK", H0.replace(tzinfo=None), 3600)


def test_names_normalise_to_utc() -> None:
    dhaka = timezone(timedelta(hours=6))
    local = H0.astimezone(dhaka)  # 16:00 +06:00 == 10:00 UTC
    assert rinex3_name("MTRK", "BGD", local, 3600, 30) == rinex3_name("MTRK", "BGD", H0, 3600, 30)
    assert rinex2_name("MTRK", local, 3600) == "mtrk261k.26o"


def test_splice_window_gap_with_only_lead_hour_is_no_data(tmp_path: Path) -> None:
    make_log(tmp_path, H0, size=10)
    dest = tmp_path / "out.ubx"
    with pytest.raises(NoDataError):
        splice_window(tmp_path, H0 + timedelta(hours=1), H0 + timedelta(hours=3), dest)
    assert not dest.exists()


def test_splice_window_logs_outside_window_is_no_data(tmp_path: Path) -> None:
    make_log(tmp_path, H0, size=10)
    make_log(tmp_path, H0 + timedelta(hours=1), size=10)
    dest = tmp_path / "out.ubx"
    with pytest.raises(NoDataError):
        splice_window(tmp_path, H0 + timedelta(hours=5), H0 + timedelta(hours=6), dest)
    assert not dest.exists()


def test_splice_window_rejects_bad_windows(tmp_path: Path) -> None:
    make_log(tmp_path, H0, size=10)
    dest = tmp_path / "out.ubx"
    with pytest.raises(ValueError):
        splice_window(tmp_path, H0, H0, dest)
    with pytest.raises(ValueError):
        splice_window(tmp_path, H0 + timedelta(hours=1), H0, dest)
    with pytest.raises(ValueError):
        splice_window(tmp_path, H0.replace(tzinfo=None), H0 + timedelta(hours=1), dest)
    with pytest.raises(ValueError):
        splice_window(tmp_path, H0, H0 + timedelta(hours=1), dest, lead_hours=-1)
    assert not dest.exists()


def test_splice_window_filters_by_station(tmp_path: Path) -> None:
    make_log(tmp_path, H0, size=10, station="MTRK")
    make_log(tmp_path, H0, size=7, station="ABCD")
    dest = tmp_path / "out.ubx"
    with pytest.raises(MixedStationsError, match="ABCD"):
        splice_window(tmp_path, H0, H0 + timedelta(hours=1), dest)
    assert not dest.exists()
    res = splice_window(tmp_path, H0, H0 + timedelta(hours=1), dest, station="MTRK")
    assert [f.station_id for f in res.files] == ["MTRK"]
    assert res.bytes == 10
    with pytest.raises(NoDataError):
        splice_window(tmp_path, H0, H0 + timedelta(hours=1), dest, station="ZZZZ")


def test_splice_window_lead_hours_and_chunking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(splice, "CHUNK", 3)
    for i in range(5):
        make_log(tmp_path, H0 + timedelta(hours=i), size=10)
    start, end = H0 + timedelta(hours=2), H0 + timedelta(hours=4)
    dest = tmp_path / "a" / "b" / "out.ubx"
    res0 = splice_window(tmp_path, start, end, dest, lead_hours=0)
    assert [f.hour_utc.hour for f in res0.files] == [12, 13]
    assert res0.lead_hours == 0
    assert dest.read_bytes() == bytes([12]) * 10 + bytes([13]) * 10
    res2 = splice_window(tmp_path, start, end, dest, lead_hours=2)
    assert [f.hour_utc.hour for f in res2.files] == [10, 11, 12, 13]
    assert res2.lead_hours == 2 and res2.bytes == 40
    assert dest.read_bytes() == b"".join(bytes([h]) * 10 for h in (10, 11, 12, 13))
    assert sorted(p.name for p in dest.parent.iterdir()) == ["out.ubx"]  # no .part left


def test_splice_window_source_vanishing_is_no_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_log(tmp_path, H0, size=10)
    gone = make_log(tmp_path, H0 + timedelta(hours=1), size=10)
    real = splice.files_for_window

    def listing_then_prune(root: Path, start: datetime, end: datetime) -> list[LogFile]:
        files = real(root, start, end)
        gone.unlink()  # retention prunes between the listing and the open
        return files

    monkeypatch.setattr(splice, "files_for_window", listing_then_prune)
    dest = tmp_path / "jobs" / "out.ubx"
    with pytest.raises(NoDataError, match="11"):
        splice_window(tmp_path, H0 + timedelta(hours=1), H0 + timedelta(hours=2), dest)
    assert not dest.exists()
    assert list(dest.parent.iterdir()) == []


def test_a_read_error_names_the_raw_log_not_the_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EIO from a failing SD card while reading an hour: the operator is pointed at that log."""
    import errno
    import io

    from mtrtk.rinex.splice import SpliceError

    make_log(tmp_path, H0, size=10)
    real_open = Path.open

    class Failing(io.BytesIO):
        def read(self, *_: object) -> bytes:
            raise OSError(errno.EIO, "Input/output error")

    def fake_open(self: Path, mode: str = "r", *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if self.suffix == ".ubx" and mode == "rb":
            return Failing()
        return real_open(self, mode, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(Path, "open", fake_open)
    dest = tmp_path / "out" / "w.ubx"
    with pytest.raises(SpliceError, match=r"cannot read the raw log MTRK_20260918_10\.ubx"):
        splice_window(tmp_path, H0, H0 + timedelta(hours=1), dest)
    assert not dest.exists() and not dest.with_name("w.ubx.part").exists()
