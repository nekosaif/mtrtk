import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pyubx2 import GET, UBXMessage

from mtrtk.ppk.events import (
    RawTimeMark,
    events_csv,
    events_geojson,
    extract_time_marks,
    gpst_datetime,
    interpolate_events,
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


def rec(t: float, lat: float, q: int = 1, lon: float = 90.0) -> Rec:
    t_gpst = T0 + timedelta(seconds=t)
    return Rec(t_gpst, lat, lon, -36.0, q, 12, 0.003, 0.003, 0.008, 0, 0, 0, 1.0, 5.0)


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


def test_interpolate_between_epochs() -> None:
    records = [rec(0, 23.0), rec(1, 23.000010), rec(2, 23.000020, q=2), rec(10, 23.000100)]
    t0 = records[0].time
    tow0 = tow_of(t0)
    marks = [
        RawTimeMark(1, 2436, tow0 + 0.5, 30),
        RawTimeMark(2, 2436, tow0 + 1.5, 30),
        RawTimeMark(3, 2436, tow0 + 5.0, 30),
        RawTimeMark(4, 2436, tow0 - 3.0, 30),
    ]
    events = interpolate_events(marks, records, max_gap_s=2.0)
    assert [e.status for e in events] == ["ok", "ok", "gap_too_large", "no_neighbours"]
    assert events[0].lat is not None and abs(events[0].lat - 23.000005) < 1e-9
    assert events[0].q == 1 and events[0].gap_s == 1.0
    assert events[1].q == 2  # worst neighbour quality
    assert events[0].time == t0.replace(microsecond=500000)
    assert [e.n for e in events] == [1, 2, 3, 4]


def test_interpolate_exact_epoch_and_antimeridian() -> None:
    records = [rec(0, 23.0, lon=179.9999), rec(1, 23.0, lon=-179.9999)]
    tow0 = tow_of(records[0].time)
    events = interpolate_events(
        [RawTimeMark(1, 2436, tow0, 30), RawTimeMark(2, 2436, tow0 + 0.5, 30)], records
    )
    assert events[0].status == "ok" and events[0].lon == 179.9999 and events[0].gap_s == 0.0
    assert events[1].lon is not None and abs(abs(events[1].lon) - 180.0) < 1e-9


def test_writers() -> None:
    records = [rec(0, 23.0), rec(1, 23.00001)]
    tow0 = tow_of(records[0].time)
    events = interpolate_events([RawTimeMark(7, 2436, tow0 + 0.25, 30)], records)
    rows = list(csv.DictReader(io.StringIO(events_csv(events))))
    assert rows[0]["count"] == "7" and rows[0]["status"] == "ok"
    assert rows[0]["lat"].startswith("23.0000025")
    assert rows[0]["time_utc"] == "2026-09-18T16:47:16.250000+00:00"
    gj = events_geojson(events)
    assert gj["features"][0]["properties"]["count"] == 7
    assert gj["features"][0]["geometry"]["type"] == "Point"


def test_writers_skip_unplaced_events_in_geojson() -> None:
    events = interpolate_events([RawTimeMark(1, 2436, 1.0, 30)], [])
    assert events[0].status == "no_neighbours"
    rows = list(csv.DictReader(io.StringIO(events_csv(events))))
    assert rows[0]["lat"] == "" and rows[0]["status"] == "no_neighbours"
    assert events_geojson(events)["features"] == []
