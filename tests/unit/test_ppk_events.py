import csv
import io
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pyubx2 import GET, UBXMessage

from mtrtk.ppk.events import (
    CSV_COLUMNS,
    RawTimeMark,
    events_csv,
    events_geojson,
    extract_time_marks,
    gpst_datetime,
    interpolate_events,
    missed_pulses,
)

T0 = datetime(2026, 9, 18, 16, 47, 34, tzinfo=UTC)


@dataclass(frozen=True)
class Rec:
    """Stand-in with the field layout of `mtrtk.ppk.pos.PosRecord` (built in a parallel task)."""

    time: datetime
    lat: float
    lon: float
    height: float
    q: int
    ns: int
    sdn: float
    sde: float
    sdu: float
    sdne: float
    sdeu: float
    sdun: float
    age: float
    ratio: float


def tm2(
    count: int,
    tow_ms: int,
    sub_ms_ns: int = 0,
    valid: int = 1,
    rising: int = 1,
    time_base: int = 1,
    week: int = 2436,
) -> bytes:
    return UBXMessage(
        "TIM",
        "TIM-TM2",
        GET,
        ch=0,
        run=1,
        time=valid,
        newRisingEdge=rising,
        timeBase=time_base,
        count=count,
        wnR=week,
        towMsR=tow_ms,
        towSubMsR=sub_ms_ns,
        accEst=30,
    ).serialize()


def rec(
    t: float,
    lat: float,
    q: int = 1,
    lon: float = 90.0,
    height: float = -36.0,
    sd: tuple[float, float, float] = (0.003, 0.003, 0.008),
) -> Rec:
    t_gpst = T0 + timedelta(seconds=t)
    return Rec(t_gpst, lat, lon, height, q, 12, *sd, 0, 0, 0, 1.0, 5.0)


def tow_of(t: datetime) -> float:
    return (t - datetime(1980, 1, 6, tzinfo=UTC)).total_seconds() - 2436 * 604800


def test_gpst_datetime() -> None:
    # GPST = UTC + 18 s in 2026
    assert gpst_datetime(2436, 492472.0) == datetime(2026, 9, 18, 16, 47, 52, tzinfo=UTC)


def test_extract_time_marks(tmp_path: Path) -> None:
    path = tmp_path / "rover.ubx"
    path.write_bytes(
        tm2(1, 492472123, 456000)
        + tm2(1, 492472123, 456000)
        + tm2(2, 492473000, valid=0)
        + tm2(3, 492474000, rising=0)
        + tm2(4, 492475500)
    )
    marks = extract_time_marks(path)
    assert [m.count for m in marks] == [1, 4]
    assert abs(marks[0].tow_s - 492472.123456) < 1e-9
    assert marks[0].week == 2436 and marks[0].acc_est_ns == 30


def test_extract_time_marks_larger_than_framer_buffer(tmp_path: Path) -> None:
    # A real hourly log is far larger than the framer's 1 MiB buffer: marks at the start
    # must survive (feeding the whole file in one call would drop all but the last MiB).
    filler = b"\x00" * (3 << 20)
    path = tmp_path / "rover.ubx"
    path.write_bytes(tm2(1, 492472000) + filler + tm2(2, 492473000) + filler + tm2(3, 492474000))
    assert [m.count for m in extract_time_marks(path)] == [1, 2, 3]


def test_extract_time_marks_counter_wrap_and_utc_base(tmp_path: Path) -> None:
    # The 16-bit counter wraps: the same count at a different time is a new pulse.
    # A UTC time base is moved onto GPST (+18 s) so it lines up with the .pos epochs.
    path = tmp_path / "rover.ubx"
    path.write_bytes(tm2(5, 492472000) + tm2(5, 492480000) + tm2(6, 604790000, time_base=2))
    marks = extract_time_marks(path)
    assert [(m.count, m.week, m.tow_s) for m in marks] == [
        (5, 2436, 492472.0),
        (5, 2436, 492480.0),
        (6, 2437, 8.0),
    ]


def test_extract_time_marks_sorts_out_of_order_frames(tmp_path: Path) -> None:
    path = tmp_path / "rover.ubx"
    path.write_bytes(tm2(3, 492474000) + tm2(1, 492472000) + tm2(2, 492473000))
    assert [m.count for m in extract_time_marks(path)] == [1, 2, 3]


def test_extract_time_marks_across_files_dedupes_the_boundary(tmp_path: Path) -> None:
    # One session spans hourly logs; the pulse repeated across the hour boundary is one mark,
    # and a frame cut in two by the rotation is reassembled.
    a, b = tmp_path / "h08.ubx", tmp_path / "h09.ubx"
    split = tm2(3, 492474000)
    a.write_bytes(tm2(1, 492472000) + tm2(2, 492473000) + split[:10])
    b.write_bytes(split[10:] + tm2(2, 492473000) + tm2(4, 492475000))
    assert [m.count for m in extract_time_marks([a, b])] == [1, 2, 3, 4]
    assert [m.count for m in extract_time_marks(a)] == [1, 2]


def test_extract_time_marks_warns_on_count_gaps_and_receiver_base(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "rover.ubx"
    path.write_bytes(tm2(65534, 492472000) + tm2(1, 492473000, time_base=0))
    with caplog.at_level(logging.WARNING, logger="mtrtk.ppk.events"):
        marks = extract_time_marks(path)
    assert [m.count for m in marks] == [65534, 1]
    assert missed_pulses(marks) == 2  # 65535 and 0 were never reported
    assert "2 camera pulse(s) missing" in caplog.text
    assert "1 time mark(s) on the receiver time base" in caplog.text


def test_extract_time_marks_warns_on_unparseable_frames(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mtrtk.core.frames import Frame

    def boom(self: Frame) -> object:
        raise ValueError("bad payload definition")

    monkeypatch.setattr(Frame, "parsed", boom)
    path = tmp_path / "rover.ubx"
    path.write_bytes(tm2(1, 492472000) + tm2(2, 492473000))
    with caplog.at_level(logging.WARNING, logger="mtrtk.ppk.events"):
        assert extract_time_marks(path) == []
    assert "2 TIM-TM2 frame(s) could not be parsed" in caplog.text


def test_missed_pulses() -> None:
    def mk(count: int) -> RawTimeMark:
        return RawTimeMark(count, 2436, float(count), 30)

    assert missed_pulses([]) == 0
    assert missed_pulses([mk(1), mk(2), mk(3)]) == 0
    assert missed_pulses([mk(1), mk(4), mk(5)]) == 2


def test_interpolate_between_epochs() -> None:
    records = [
        rec(0, 23.0),
        rec(1, 23.000010, q=2, height=-35.0, sd=(0.010, 0.020, 0.030)),
        rec(2, 23.000020, height=-34.0),
        rec(10, 23.000100),
    ]
    t0 = records[0].time
    tow0 = tow_of(t0)
    marks = [
        RawTimeMark(1, 2436, tow0 + 0.5, 30),
        RawTimeMark(2, 2436, tow0 + 1.5, 30),
        RawTimeMark(3, 2436, tow0 + 5.0, 30),
        RawTimeMark(4, 2436, tow0 - 3.0, 30),
        RawTimeMark(5, 2436, tow0 + 11.0, 30),
    ]
    # Records out of time order are sorted before the search.
    events = interpolate_events(marks, list(reversed(records)), max_gap_s=2.0)
    statuses = ["ok", "ok", "gap_too_large", "no_neighbours", "no_neighbours"]
    assert [e.status for e in events] == statuses
    assert events[0].lat == pytest.approx(23.000005, abs=1e-9)
    assert events[0].height == pytest.approx(-35.5, abs=1e-9)
    assert events[0].gap_s == 1.0
    # Worst neighbour quality and sigmas, whichever side the worse one is on.
    assert events[0].q == 2 and (events[0].sdn, events[0].sde, events[0].sdu) == (0.01, 0.02, 0.03)
    assert events[1].q == 2 and (events[1].sdn, events[1].sde, events[1].sdu) == (0.01, 0.02, 0.03)
    assert events[1].height == pytest.approx(-34.5, abs=1e-9)
    assert events[2].gap_s == 8.0 and events[2].lat is None
    assert events[4].lat is None and events[4].gap_s is None  # after the last epoch
    assert events[0].time == t0.replace(microsecond=500000)
    assert [e.n for e in events] == [1, 2, 3, 4, 5]


def test_interpolate_gap_equal_to_max_is_accepted() -> None:
    records = [rec(0, 23.0), rec(2, 23.00002)]
    tow0 = tow_of(records[0].time)
    events = interpolate_events([RawTimeMark(1, 2436, tow0 + 1.0, 30)], records, max_gap_s=2.0)
    assert events[0].status == "ok" and events[0].gap_s == 2.0


def test_interpolate_warns_when_no_mark_is_on_the_track(caplog: pytest.LogCaptureFixture) -> None:
    records = [rec(0, 23.0), rec(1, 23.00001)]
    # e.g. a BeiDou time grid: BDT is GPST - 14 s, so the mark lands before the track.
    tow0 = tow_of(records[0].time)
    with caplog.at_level(logging.WARNING, logger="mtrtk.ppk.events"):
        events = interpolate_events([RawTimeMark(1, 2436, tow0 - 14.0, 30)], records)
    assert events[0].status == "no_neighbours"
    assert "none of the 1 time mark(s) falls inside the track" in caplog.text


def test_interpolate_exact_epoch_and_antimeridian() -> None:
    records = [rec(0, 23.0, lon=179.9999), rec(1, 23.0, lon=-179.9999)]
    tow0 = tow_of(records[0].time)
    events = interpolate_events(
        [
            RawTimeMark(1, 2436, tow0, 30),
            RawTimeMark(2, 2436, tow0 + 0.5, 30),
            RawTimeMark(3, 2436, tow0 + 0.75, 30),
            RawTimeMark(4, 2436, tow0 + 0.25, 30),
        ],
        records,
    )
    assert events[0].status == "ok" and events[0].lon == 179.9999 and events[0].gap_s == 0.0
    assert events[1].lon is not None and abs(abs(events[1].lon) - 180.0) < 1e-9
    assert events[2].lon == pytest.approx(-179.99995, abs=1e-9)  # normalised into [-180, 180]
    assert events[3].lon == pytest.approx(179.99995, abs=1e-9)
    # ... and the other way round, east to west.
    back = [rec(0, 23.0, lon=-179.9999), rec(1, 23.0, lon=179.9999)]
    ev = interpolate_events([RawTimeMark(1, 2436, tow0 + 0.75, 30)], back)
    assert ev[0].lon == pytest.approx(179.99995, abs=1e-9)


def test_writers() -> None:
    records = [rec(0, 23.0), rec(1, 23.00001), rec(9, 23.00009)]
    tow0 = tow_of(records[0].time)
    events = interpolate_events(
        [RawTimeMark(7, 2436, tow0 + 0.25, 30), RawTimeMark(8, 2436, tow0 + 5.0, 30)], records
    )
    text = events_csv(events)
    assert next(csv.reader(io.StringIO(text))) == CSV_COLUMNS
    assert CSV_COLUMNS == [
        "n",
        "count",
        "gps_week",
        "gps_tow_s",
        "time_gpst",
        "lat",
        "lon",
        "height_m",
        "q",
        "sdn_m",
        "sde_m",
        "sdu_m",
        "interp_gap_s",
        "status",
        "time_utc",
    ]
    rows = list(csv.DictReader(io.StringIO(text)))
    assert rows[0]["count"] == "7" and rows[0]["status"] == "ok"
    assert rows[0]["lat"] == "23.000002500"
    assert rows[0]["lon"] == "90.000000000" and rows[0]["height_m"] == "-36.0000"
    assert rows[0]["sdn_m"] == "0.0030" and rows[0]["sdu_m"] == "0.0080"
    assert rows[0]["interp_gap_s"] == "1.000" and rows[0]["gps_week"] == "2436"
    assert rows[1]["status"] == "gap_too_large" and rows[1]["interp_gap_s"] == "8.000"
    assert rows[1]["lat"] == "" and rows[1]["q"] == ""
    assert rows[0]["time_utc"] == "2026-09-18T16:47:16.250000+00:00"
    # GPST is not UTC: no offset, so an ISO-8601 reader cannot take it for UTC.
    assert rows[0]["time_gpst"] == "2026-09-18T16:47:34.250000"
    assert datetime.fromisoformat(rows[0]["time_gpst"]).tzinfo is None
    gj = events_geojson(events)
    props = gj["features"][0]["properties"]
    assert len(gj["features"]) == 1  # the gap_too_large event has no geometry
    assert props["count"] == 7 and props["status"] == "ok" and props["interp_gap_s"] == 1.0
    assert props["gps_week"] == 2436 and props["gps_tow_s"] == pytest.approx(tow0 + 0.25)
    assert "time" not in props
    assert props["time_gpst"] == "2026-09-18T16:47:34.250000"
    assert props["time_utc"] == "2026-09-18T16:47:16.250000+00:00"
    assert gj["features"][0]["geometry"]["type"] == "Point"
    # GeoJSON is lon, lat, height.
    assert gj["features"][0]["geometry"]["coordinates"] == [
        90.0,
        pytest.approx(23.0000025, abs=1e-12),
        -36.0,
    ]


def test_writers_skip_unplaced_events_in_geojson() -> None:
    events = interpolate_events([RawTimeMark(1, 2436, 1.0, 30)], [])
    assert events[0].status == "no_neighbours"
    rows = list(csv.DictReader(io.StringIO(events_csv(events))))
    assert rows[0]["lat"] == "" and rows[0]["status"] == "no_neighbours"
    assert events_geojson(events)["features"] == []
