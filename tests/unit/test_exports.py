import csv
import io
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from mtrtk.rover.exports import to_csv, to_geojson, to_gpx, to_kml
from mtrtk.store.models import Point

P = [
    Point(
        id=1,
        session_id=1,
        name="BM-1",
        code="BM",
        note="brass disk",
        ts_utc=datetime(2026, 9, 18, 16, 0, tzinfo=UTC),
        lat=23.8373506,
        lon=90.2625502,
        height_m=-36.268,
        hmsl_m=13.363,
        n_epochs=30,
        sd_n=0.004,
        sd_e=0.003,
        sd_u=0.009,
        fix_type=3,
        carr_soln=2,
        h_acc_m=0.012,
        v_acc_m=0.018,
    ),
    Point(
        id=2,
        session_id=1,
        name="Fence,corner",
        code=None,
        note=None,
        ts_utc=datetime(2026, 9, 18, 16, 5, tzinfo=UTC),
        lat=23.8374,
        lon=90.2626,
        height_m=-36.1,
        hmsl_m=13.5,
        n_epochs=10,
        sd_n=0.02,
        sd_e=0.02,
        sd_u=0.05,
        fix_type=3,
        carr_soln=1,
        h_acc_m=0.3,
        v_acc_m=0.5,
    ),
]


def test_csv() -> None:
    rows = list(csv.DictReader(io.StringIO(to_csv(P))))
    assert (
        rows[0]["name"] == "BM-1"
        and rows[0]["lat"] == "23.837350600"
        and rows[0]["carr_soln"] == "RTK fixed"
    )
    assert rows[1]["name"] == "Fence,corner" and rows[1]["code"] == ""
    assert set(rows[0]) >= {
        "id",
        "name",
        "code",
        "note",
        "time_utc",
        "lat",
        "lon",
        "height_m",
        "hmsl_m",
        "n_epochs",
        "sd_n_m",
        "sd_e_m",
        "sd_u_m",
        "fix",
        "carr_soln",
        "h_acc_m",
        "v_acc_m",
    }


def test_geojson() -> None:
    gj = to_geojson(P)
    assert gj["type"] == "FeatureCollection" and len(gj["features"]) == 2
    f = gj["features"][0]
    assert f["geometry"] == {"type": "Point", "coordinates": [90.2625502, 23.8373506, -36.268]}
    assert f["properties"]["name"] == "BM-1" and f["properties"]["sd_u_m"] == 0.009


def test_kml_and_gpx_are_valid_xml() -> None:
    kml = ET.fromstring(to_kml(P))
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    placemarks = kml.findall(".//k:Placemark", ns)
    assert len(placemarks) == 2 and placemarks[0].find("k:name", ns).text == "BM-1"
    assert placemarks[0].find(".//k:coordinates", ns).text.strip() == "90.2625502,23.8373506,13.363"
    gpx = ET.fromstring(to_gpx(P))
    wpts = gpx.findall(".//{http://www.topografix.com/GPX/1/1}wpt")
    assert (
        len(wpts) == 2
        and wpts[0].get("lat") == "23.8373506"
        and wpts[0].find("{http://www.topografix.com/GPX/1/1}ele").text == "13.363"
    )


def test_xml_survives_markup_and_control_characters_in_names() -> None:
    odd = P[0].model_copy(update={"name": "a<b>&\x01c", "note": "x & y", "hmsl_m": None})
    kml = ET.fromstring(to_kml([odd]))
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    mark = kml.find(".//k:Placemark", ns)
    assert mark is not None and mark.find("k:name", ns).text == "a<b>&c"
    # without hmsl the ellipsoidal height is the only height there is
    assert mark.find(".//k:coordinates", ns).text == "90.2625502,23.8373506,-36.268"
    gpx = ET.fromstring(to_gpx([odd]))
    g = "{http://www.topografix.com/GPX/1/1}"
    wpt = gpx.find(f"{g}wpt")
    assert wpt is not None and wpt.find(f"{g}desc").text == "x & y"
    assert wpt.find(f"{g}geoidheight") is None
