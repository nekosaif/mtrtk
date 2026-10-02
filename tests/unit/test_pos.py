import csv
import io
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

from mtrtk.ppk.pos import (
    Q_NAMES,
    PosRecord,
    parse_pos,
    summarize,
    track_csv,
    track_geojson,
    track_kml,
)

POS = """% program   : RTKLIB demo5 b34k
% inp file  : rover.rnx
% obs start : 2026/09/18 16:47:34.0 GPST (week2436 492472.0s)
% pos mode  : kinematic
%  GPST                  latitude(deg) longitude(deg)  height(m)   Q  ns   sdn(m)   sde(m)   sdu(m)  sdne(m)  sdeu(m)  sdun(m) age(s)  ratio
2026/09/18 16:47:34.000   23.837350600   90.262550200   -36.2680   1  12   0.0034   0.0029   0.0081  -0.0001   0.0002  -0.0003   1.00    4.5
2026/09/18 16:47:35.000   23.837350700   90.262550300   -36.2670   1  12   0.0035   0.0028   0.0080  -0.0001   0.0002  -0.0003   1.00    5.1
2026/09/18 16:47:36.000   23.837351000   90.262550500   -36.2600   2  11   0.1200   0.1100   0.2500  -0.0100   0.0200  -0.0300   2.00    1.2
2026/09/18 16:47:40.000   23.837352000   90.262551000   -36.2500   5   9   1.5000   1.4000   3.0000   0.0000   0.0000   0.0000   0.00    0.0
"""  # noqa: E501


def test_parse_pos_records() -> None:
    recs = parse_pos(POS)
    assert len(recs) == 4
    r = recs[0]
    assert r.time == datetime(2026, 9, 18, 16, 47, 34, tzinfo=UTC)
    assert r.lat == 23.8373506 and r.lon == 90.2625502 and r.height == -36.268
    assert r.q == 1 and r.ns == 12 and r.sdn == 0.0034 and r.age == 1.0 and r.ratio == 4.5
    assert recs[2].q == 2 and Q_NAMES[recs[3].q] == "single"


def test_parse_pos_tolerates_extra_columns_and_junk() -> None:
    text = (
        POS.splitlines()[5]
        + "  0.12  extra\n"
        + "not a record line\n"
        + "2026/09/18 16:47:35.000 1 2 3\n"
        + "2026/13/40 99:99:99.000 23.8 90.2 -36.2 1 12 0 0 0 0 0 0 1.0 4.5\n"
    )
    recs = parse_pos(text)
    assert len(recs) == 1 and recs[0].ratio == 4.5


def test_summary() -> None:
    s = summarize(parse_pos(POS), gap_s=2.0)
    assert s.epochs == 4 and s.duration_s == 6.0 and s.interval_s == 1.0
    assert s.fixed_pct == 50.0 and s.float_pct == 25.0 and s.single_pct == 25.0
    assert s.mean_sd_fixed == {"n": 0.00345, "e": 0.00285, "u": 0.00805}
    assert s.gaps == [
        (
            datetime(2026, 9, 18, 16, 47, 36, tzinfo=UTC),
            datetime(2026, 9, 18, 16, 47, 40, tzinfo=UTC),
            4.0,
        )
    ]


def test_summary_to_json() -> None:
    j = summarize(parse_pos(POS)).to_json()
    # GPST, so no UTC offset: "+00:00" would make every ISO reader take it as UTC, 18 s off.
    assert j["epochs"] == 4 and j["first_time"] == "2026-09-18T16:47:34"
    assert j["time_system"] == "GPST"
    assert j["gaps"] == [["2026-09-18T16:47:36", "2026-09-18T16:47:40", 4.0]]


def test_summary_empty() -> None:
    s = summarize([])
    assert s.epochs == 0 and s.fixed_pct == 0.0 and s.mean_sd_fixed is None and s.gaps == []
    assert s.to_json()["first_time"] is None


def test_track_csv() -> None:
    rows = list(csv.DictReader(io.StringIO(track_csv(parse_pos(POS)))))
    assert rows[0]["time_gpst"] == "2026-09-18T16:47:34.000"
    assert rows[0]["quality"] == "fixed" and rows[0]["lat"] == "23.837350600"
    assert set(rows[0]) >= {
        "time_gpst",
        "lat",
        "lon",
        "height_m",
        "q",
        "quality",
        "ns",
        "sdn_m",
        "sde_m",
        "sdu_m",
        "age_s",
        "ratio",
    }


def test_track_geojson_segments_by_quality() -> None:
    gj = track_geojson(parse_pos(POS), point_every=1)
    stamps = [f["properties"].get("time") or f["properties"]["start"] for f in gj["features"]]
    assert stamps and not any("+" in t or t.endswith("Z") for t in stamps)  # GPST: no offset
    assert gj["properties"]["time_system"] == "GPST"
    lines = [f for f in gj["features"] if f["geometry"]["type"] == "LineString"]
    points = [f for f in gj["features"] if f["geometry"]["type"] == "Point"]
    assert [ln["properties"]["quality"] for ln in lines] == ["fixed", "float", "single"]
    assert len(lines[0]["geometry"]["coordinates"]) == 2 and len(points) == 4
    assert points[2]["properties"]["q"] == 2
    # a single-epoch run still yields a valid LineString (>= 2 positions)
    assert len(lines[1]["geometry"]["coordinates"]) == 2


def test_track_kml_valid() -> None:
    root = ET.fromstring(track_kml(parse_pos(POS)))
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    assert len(root.findall(".//k:LineString", ns)) == 3
    assert len(root.findall(".//k:Style", ns)) >= 3


def test_kml_is_clamped_to_the_ground() -> None:
    """The heights are ellipsoidal; Google Earth reads `absolute` as above mean sea level."""
    text = track_kml(parse_pos(POS))
    assert "<altitudeMode>clampToGround</altitudeMode>" in text and "absolute" not in text


# ------------------------------------------- parked minors (2026-10-02): order, rate, coverage


def _rec(t: float, q: int = 1) -> PosRecord:
    """A record `t` seconds after 16:47:34 GPST."""
    return PosRecord(
        time=datetime(2026, 9, 18, 16, 47, 34, tzinfo=UTC) + timedelta(seconds=t),
        lat=23.8,
        lon=90.2,
        height=-36.0,
        q=q,
        ns=12,
        sdn=0.01,
        sde=0.01,
        sdu=0.02,
        sdne=0.0,
        sdeu=0.0,
        sdun=0.0,
        age=1.0,
        ratio=3.0,
    )


def test_summary_of_a_newest_first_solution() -> None:
    """A backward-only solution (pos1-soltype=backward) is written newest first."""
    s = summarize([_rec(t) for t in (10, 9, 8, 2, 1, 0)])
    assert s.duration_s == 10.0 and s.interval_s == 1.0
    assert s.first_time is not None and s.last_time is not None
    assert (s.last_time - s.first_time).total_seconds() == 10.0
    assert [g[2] for g in s.gaps] == [6.0]


def test_summary_of_a_slow_log_reports_outages_not_every_epoch() -> None:
    """At 0.2 Hz every 5 s step is longer than the 2 s default: only the outage is a gap."""
    s = summarize([_rec(t) for t in (0, 5, 10, 15, 60, 65)], gap_s=2.0)
    assert s.interval_s == 5.0
    assert [g[2] for g in s.gaps] == [45.0]
    # A step equal to the threshold is no gap; the interval is the median step, not the least.
    assert summarize([_rec(t) for t in (0, 1, 2, 4)], gap_s=2.0).gaps == []
    assert summarize([_rec(t) for t in (0, 2, 7, 12)], gap_s=2.0).interval_s == 5.0


def test_tracks_break_a_run_at_an_outage() -> None:
    """Same quality on both sides of a 10-minute hole: two lines, never one across it."""
    recs = [_rec(t) for t in (0, 1, 2, 600, 601)]
    lines = [f for f in track_geojson(recs)["features"] if f["geometry"]["type"] == "LineString"]
    assert [ln["properties"]["epochs"] for ln in lines] == [3, 2]
    root = ET.fromstring(track_kml(recs))
    assert len(root.findall(".//{http://www.opengis.net/kml/2.2}LineString")) == 2


def test_parse_pos_reads_times_without_a_fraction() -> None:
    line = POS.splitlines()[5].replace("16:47:34.000", "16:47:34")
    (r,) = parse_pos(line)
    assert r.time == datetime(2026, 9, 18, 16, 47, 34, tzinfo=UTC)


def test_parse_pos_skips_a_row_with_a_non_numeric_field() -> None:
    bad = POS.splitlines()[5].replace("0.0034", "nan?")
    assert parse_pos(bad + "\n" + POS.splitlines()[6]) == parse_pos(POS.splitlines()[6])


def test_geojson_points_every_nth_epoch() -> None:
    recs = parse_pos(POS)
    every2 = [
        f
        for f in track_geojson(recs, point_every=2)["features"]
        if f["geometry"]["type"] == "Point"
    ]
    assert [p["properties"]["time"] for p in every2] == [
        "2026-09-18T16:47:34",
        "2026-09-18T16:47:36",
    ]
    every0 = [
        f
        for f in track_geojson(recs, point_every=0)["features"]
        if f["geometry"]["type"] == "Point"
    ]
    assert len(every0) == len(recs)  # 0 (or less) falls back to every epoch


def test_kml_styles_each_run_by_its_quality() -> None:
    root = ET.fromstring(track_kml(parse_pos(POS)))
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    urls = [e.text for e in root.findall(".//k:Placemark/k:styleUrl", ns)]
    assert urls == ["#q1", "#q2", "#q5"]
    ids = {s.get("id") for s in root.findall(".//k:Style", ns)}
    assert {u.lstrip("#") for u in urls} <= ids
    assert _rec(0, q=0).quality == "0"  # an unlisted Q reads as its number
