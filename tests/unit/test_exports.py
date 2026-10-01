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


# ------------------------------------------------------------------ fix round 1
G = "{http://www.topografix.com/GPX/1/1}"
K = {"k": "http://www.opengis.net/kml/2.2"}


def test_xml_numbers_are_rounded_plain_decimals() -> None:
    noisy = P[0].model_copy(update={"lat": 23.83735061234567, "hmsl_m": 13.36312345})
    coords = ET.fromstring(to_kml([noisy])).find(".//k:coordinates", K).text
    assert coords == "90.2625502,23.837350612,13.3631"
    assert ET.fromstring(to_gpx([noisy])).find(f"{G}wpt").get("lat") == "23.837350612"
    # within ~11 m of the equator / prime meridian: never scientific notation (xsd:decimal)
    near = P[0].model_copy(update={"lat": 0.00005, "lon": -1.23e-05, "hmsl_m": 0.0})
    wpt = ET.fromstring(to_gpx([near])).find(f"{G}wpt")
    assert wpt.get("lat") == "0.00005" and wpt.get("lon") == "-0.0000123"
    assert ET.fromstring(to_kml([near])).find(".//k:coordinates", K).text == (
        "-0.0000123,0.00005,0"
    )
    zero = P[0].model_copy(update={"lat": -1e-12})
    assert ET.fromstring(to_gpx([zero])).find(f"{G}wpt").get("lat") == "0"


def test_gpx_geoidheight_desc_and_type() -> None:
    wpt = ET.fromstring(to_gpx(P)).find(f"{G}wpt")
    assert wpt.find(f"{G}geoidheight").text == "-49.631"  # ellipsoidal height - hMSL
    assert wpt.find(f"{G}desc").text == "brass disk" and wpt.find(f"{G}type").text == "BM"


def test_csv_zero_ids_and_spreadsheet_formulas() -> None:
    odd = P[0].model_copy(
        update={"id": 0, "session_id": 0, "name": '=HYPERLINK("x")', "code": "+cmd", "note": "@a"}
    )
    tabbed = P[1].model_copy(update={"name": "\tx", "code": "-1", "note": "\rn"})
    rows = list(csv.DictReader(io.StringIO(to_csv([odd, tabbed]))))
    assert rows[0]["id"] == "0" and rows[0]["session_id"] == "0"
    assert rows[0]["name"] == '\'=HYPERLINK("x")' and rows[0]["code"] == "'+cmd"
    assert rows[0]["note"] == "'@a"
    assert rows[1]["name"] == "'\tx" and rows[1]["code"] == "'-1" and rows[1]["note"] == "'\rn"
    assert rows[1]["id"] == "2" and P[0].name == "BM-1"  # ordinary names are left alone


def test_geojson_property_names() -> None:
    props = to_geojson(P)["features"][0]["properties"]
    assert "sd_n" not in props and "lat" not in props
    assert props["carr_soln_name"] == "RTK fixed" and props["fix_name"] == "3D"
    assert props["sd_n_m"] == 0.004 and props["id"] == 1 and props["session_id"] == 1
