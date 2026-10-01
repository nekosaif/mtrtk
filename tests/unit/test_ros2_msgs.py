"""Static checks on the mtrtk_msgs interface files.

The real build check is `colcon build` in a ROS image. These tests run without ROS and pin the
parts of the message contract that a consumer relies on: which way vectors and headings point,
which time base a time mark is in, and which float fields read NaN when unknown.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "ros2" / "mtrtk_msgs"
LINE = re.compile(
    r"^(?P<type>[\w/\[\]]+)\s+(?P<name>\w+)(?:=(?P<const>\S+))?\s*(?:#\s*(?P<comment>.*))?$"
)


def _parse(name: str) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    """Return (fields, constants) as name -> (type, comment or value)."""
    fields: dict[str, tuple[str, str]] = {}
    consts: dict[str, tuple[str, str]] = {}
    for raw in (ROOT / "msg" / name).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = LINE.match(line)
        assert m, f"unparsed line in {name}: {raw!r}"
        if m["const"] is not None:
            consts[m["name"]] = (m["type"], m["const"])
        else:
            fields[m["name"]] = (m["type"], m["comment"] or "")
    return fields, consts


def test_rtk_status_relative_vector_points_base_to_rover() -> None:
    fields, _ = _parse("RtkStatus.msg")
    assert "heading_to_base" not in fields
    assert fields["rel_pos_heading"][0] == "float32"
    assert "base->rover" in fields["rel_pos_heading"][1]
    for axis in ("n", "e", "d"):
        assert "rover minus base" in fields[f"rel_pos_{axis}"][1]


def test_rtk_status_float_fields_document_nan_when_unknown() -> None:
    fields, _ = _parse("RtkStatus.msg")
    floats = [n for n, (t, _c) in fields.items() if t in ("float32", "float64")]
    assert floats
    for name in floats:
        assert "NaN" in fields[name][1], name


def test_rtk_status_fix_type_comment_lists_dead_reckoning() -> None:
    fields, _ = _parse("RtkStatus.msg")
    assert "1 DR only" in fields["fix_type"][1]


def test_time_mark_carries_its_time_base() -> None:
    fields, consts = _parse("TimeMark.msg")
    assert "gps_week" not in fields and "gps_tow" not in fields
    assert fields["time_base"][0] == "uint8"
    assert consts == {
        "TIME_BASE_RECEIVER": ("uint8", "0"),
        "TIME_BASE_GNSS": ("uint8", "1"),
        "TIME_BASE_UTC": ("uint8", "2"),
    }
    assert fields["week"][0] == "uint32" and "time_base" in fields["week"][1]
    assert fields["tow"][0] == "float64" and "time_base" in fields["tow"][1]


def test_package_builds_every_message_with_its_dependencies() -> None:
    cmake = (ROOT / "CMakeLists.txt").read_text()
    package = (ROOT / "package.xml").read_text()
    for msg in sorted((ROOT / "msg").glob("*.msg")):
        assert f'"msg/{msg.name}"' in cmake, msg.name
        if "std_msgs/" in msg.read_text():
            assert "DEPENDENCIES std_msgs" in cmake
            assert "<depend>std_msgs</depend>" in package
