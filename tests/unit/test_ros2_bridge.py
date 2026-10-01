"""The ROS 2 bridge node's pure helpers, and static checks on the `mtrtk_bridge` package.

No ROS here: the real build check is `colcon build` in a ROS image. These tests pin what the node
needs from its pure helpers (the WebSocket URL it dials, the NMEA lines it republishes, the twist
it publishes) and keep the package's metadata, parameter file and node source in step.
"""

import ast
import io
import math
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "ros2" / "mtrtk_bridge"
sys.path.insert(0, str(PKG))

from mtrtk_bridge.convert import (  # noqa: E402
    UNKNOWN_VARIANCE,
    finite_twist,
    iso_for_fromisoformat,
    stamp_from_iso,
    twist_fields,
)
from mtrtk_bridge.link import (  # noqa: E402
    NMEA_MAX_LINE,
    WS_TOPICS,
    nmea_sentences,
    nmea_text,
    parse_host_port,
    redact_url,
    ws_connect_url,
    ws_error_reason,
    ws_headers,
)

NODE = PKG / "mtrtk_bridge" / "node.py"
LAUNCH = PKG / "launch" / "bridge.launch.py"


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


# --------------------------------------------------------------------------- ws_connect_url
def test_ws_url_asks_for_the_bridge_topics() -> None:
    url = ws_connect_url("ws://127.0.0.1:8080/ws")
    assert url.startswith("ws://127.0.0.1:8080/ws?")
    assert _query(url) == {"topics": ["pvt,rtk"]}
    assert WS_TOPICS == ("pvt", "rtk")


def test_ws_url_keeps_a_token_already_in_the_url() -> None:
    # MTRTK_WS_URL=ws://...?token=<web token> is how the compose profile passes it.
    url = ws_connect_url("ws://rover:8080/ws?token=fromenv")
    assert _query(url) == {"token": ["fromenv"], "topics": ["pvt,rtk"]}


def test_ws_url_merges_the_topics_it_needs_into_the_callers() -> None:
    # A topic list without pvt would leave the bridge with nothing to publish.
    url = ws_connect_url("ws://rover:8080/ws?topics=sats")
    assert _query(url)["topics"] == ["sats,pvt,rtk"]
    url = ws_connect_url("ws://rover:8080/ws?topics=rtk,pvt")
    assert _query(url)["topics"] == ["rtk,pvt"]


def test_the_token_parameter_goes_in_a_header_not_the_url() -> None:
    # The daemon reads `Authorization: Bearer` first; a URL ends up in access logs.
    assert ws_headers("abc123") == ["Authorization: Bearer abc123"]
    assert ws_headers("  abc123 ") == ["Authorization: Bearer abc123"]
    assert ws_headers("") == []


@pytest.mark.parametrize("token", ["a\r\nX-Evil: 1", "a\nb"])
def test_a_token_cannot_inject_a_header(token: str) -> None:
    with pytest.raises(ValueError):
        ws_headers(token)


def test_redact_url_hides_the_token_only() -> None:
    url = ws_connect_url("ws://rover:8080/ws?token=s3cret")
    shown = redact_url(url)
    assert "s3cret" not in shown
    assert _query(shown) == {"token": ["***"], "topics": ["pvt,rtk"]}
    assert redact_url("ws://rover:8080/ws?topics=pvt") == "ws://rover:8080/ws?topics=pvt"


# --------------------------------------------------------------------------- ws_error_reason
class _BadStatus(Exception):
    """What websocket-client raises for a refused handshake (its message dumps the headers)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"Handshake status {status_code} -+-+- {{'date': 'x'}} -+-+- b''")
        self.status_code = status_code


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_handshake_reads_as_a_missing_token(status: int) -> None:
    reason = ws_error_reason(_BadStatus(status))
    hint = "(is WEB_PASSWORD set? give the bridge its token)"
    assert reason == f"handshake refused with HTTP {status} {hint}"


def test_other_errors_keep_their_own_words() -> None:
    assert ws_error_reason(_BadStatus(502)) == "handshake refused with HTTP 502"
    assert ws_error_reason(ConnectionRefusedError(111, "Connection refused")) == (
        "[Errno 111] Connection refused"
    )
    assert ws_error_reason(TimeoutError()) == "TimeoutError"  # an exception with no message


# --------------------------------------------------------------------------- parse_host_port
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("127.0.0.1:10110", ("127.0.0.1", 10110)),
        ("rover.tailnet:10110", ("rover.tailnet", 10110)),
        (":10110", ("127.0.0.1", 10110)),
        ("10110", ("127.0.0.1", 10110)),
        ("[::1]:10110", ("::1", 10110)),
        (" 10.0.0.2:5000 ", ("10.0.0.2", 5000)),
    ],
)
def test_parse_host_port(text: str, expected: tuple[str, int]) -> None:
    assert parse_host_port(text) == expected


@pytest.mark.parametrize("text", ["", "host:", "host:port", "host:0", "host:70000", "[::1"])
def test_parse_host_port_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError):
        parse_host_port(text)


# --------------------------------------------------------------------------- nmea_text
def test_nmea_text_strips_the_line_ending() -> None:
    line = b"$GNGGA,123519.00,2350.24104,N,09015.75301,E,4,12,0.9,13.4,M,-49.6,M,1.0,0000*5C\r\n"
    assert nmea_text(line) == line.decode().strip()


@pytest.mark.parametrize("line", [b"", b"\r\n", b"garbage\r\n", b"\xb5b\x01\x07"])
def test_nmea_text_skips_what_is_not_a_sentence(line: bytes) -> None:
    assert nmea_text(line) is None


def test_nmea_text_never_raises_on_bad_bytes() -> None:
    assert nmea_text(b"$GNRMC,\xff\xfe*00\r\n") == "$GNRMC,��*00"


GGA = b"$GNGGA,123519.00,2350.24104,N,09015.75301,E,1,12,0.9,13.4,M,-49.6,M,,*6F"


def test_nmea_sentences_reads_the_stream_until_it_ends() -> None:
    stream = io.BytesIO(GGA + b"\r\n" + b"\xb5b junk\r\n\r\n" + GGA + b"\n")
    assert list(nmea_sentences(stream.readline)) == [GGA.decode(), GGA.decode()]


def test_an_overlong_line_is_skipped_whole_not_published_cut_short() -> None:
    # A `$` line past the limit (a corrupt or runaway stream) must not reach /nmea as a
    # truncated sentence, and nothing of its tail may either.
    long = b"$" * 250 + b"\r\n"  # read in 100-byte pieces, each of which starts with `$`
    stream = io.BytesIO(long + GGA + b"\r\n" + long + long + GGA + b"\r\n")
    assert list(nmea_sentences(stream.readline, max_line=100)) == [GGA.decode()] * 2
    # Exactly at the limit (line ending included) is still a whole line.
    assert list(nmea_sentences(io.BytesIO(GGA + b"\n").readline, max_line=len(GGA) + 1)) == [
        GGA.decode()
    ]
    assert NMEA_MAX_LINE >= 82 + 2  # NMEA 0183's maximum, with its CR LF


# --------------------------------------------------------------------------- finite_twist
def test_finite_twist_passes_a_known_velocity_through() -> None:
    pvt = {
        "velocity": {"vel_n_mps": 1.0, "vel_e_mps": 2.0, "vel_d_mps": -0.5},
        "accuracy": {"s_acc_mps": 0.1},
    }
    tw = finite_twist(twist_fields(pvt))
    assert tw["linear"] == (2.0, 1.0, 0.5)
    assert tw["covariance"][0] == tw["covariance"][7] == tw["covariance"][14] == pytest.approx(0.01)


def test_finite_twist_zeroes_an_unknown_axis_and_says_it_is_unknown() -> None:
    pvt = {
        "velocity": {"vel_n_mps": 1.0, "vel_e_mps": None, "vel_d_mps": None},
        "accuracy": {"s_acc_mps": 0.1},
    }
    src = twist_fields(pvt)
    tw = finite_twist(src)
    assert tw["linear"] == (0.0, 1.0, 0.0)
    assert tw["covariance"][0] == UNKNOWN_VARIANCE  # east
    assert tw["covariance"][7] == pytest.approx(0.01)  # north is known
    assert tw["covariance"][14] == UNKNOWN_VARIANCE  # up
    assert all(math.isfinite(v) for v in tw["linear"])
    assert src["covariance"][0] == pytest.approx(0.01)  # the input is not modified


# --------------------------------------------------------------------------- Python 3.10
# Humble runs the bridge on Ubuntu 22.04's Python 3.10, whose `datetime.fromisoformat` takes only
# `YYYY-MM-DDTHH:MM:SS[.ffffff]+HH:MM` - not the `Z` pydantic writes, nor other fraction lengths.
PY310_ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{6})?([+-]\d\d:\d\d)?$")


@pytest.mark.parametrize(
    ("iso", "expected"),
    [
        ("2026-10-01T12:00:00Z", "2026-10-01T12:00:00+00:00"),
        ("2026-10-01T12:00:00.400000Z", "2026-10-01T12:00:00.400000+00:00"),
        ("2026-10-01T12:00:00.4z", "2026-10-01T12:00:00.400000+00:00"),
        ("2026-10-01T12:00:00.123456789+06:00", "2026-10-01T12:00:00.123456+06:00"),
        ("2026-10-01T12:00:00.25", "2026-10-01T12:00:00.250000"),
        ("2026-10-01T12:00:00+00:00", "2026-10-01T12:00:00+00:00"),
    ],
)
def test_iso_is_rewritten_for_python_310(iso: str, expected: str) -> None:
    out = iso_for_fromisoformat(iso)
    assert out == expected
    assert PY310_ISO.match(out)


def test_stamp_from_iso_reads_what_pydantic_writes() -> None:
    assert stamp_from_iso("2026-09-18T16:47:34.250000Z") == (1789750054, 250000000)
    assert stamp_from_iso("2026-09-18T16:47:34.123456789Z") == (1789750054, 123456000)


PY311_NAMES = {("datetime", "UTC"), ("asyncio", "timeout"), ("asyncio", "TaskGroup")}
PY311_NAMES |= {("typing", "Self"), ("enum", "StrEnum")}


def test_the_bridge_uses_nothing_newer_than_python_310() -> None:
    for path in [*(PKG / "mtrtk_bridge").glob("*.py"), LAUNCH, PKG / "setup.py"]:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    assert (node.module, alias.name) not in PY311_NAMES, (path.name, alias.name)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert (node.value.id, node.attr) not in PY311_NAMES, (path.name, node.attr)
            elif isinstance(node, ast.Import):
                assert all(a.name != "tomllib" for a in node.names), path.name
            assert not isinstance(node, ast.TryStar), path.name


# --------------------------------------------------------------------------- the package
def _node_tree() -> ast.Module:
    return ast.parse(NODE.read_text())


def _declared_parameters() -> dict[str, Any]:
    params: dict[str, Any] = {}
    for node in ast.walk(_node_tree()):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "declare_parameter"
        ):
            name, default = (ast.literal_eval(a) for a in node.args[:2])
            params[name] = default
    return params


def test_parameter_file_matches_the_declared_parameters() -> None:
    config = yaml.safe_load((PKG / "config" / "bridge.yaml").read_text())
    assert config["mtrtk_bridge"]["ros__parameters"] == _declared_parameters()
    assert _declared_parameters() == {
        "ws_url": "ws://127.0.0.1:8080/ws",
        "token": "",
        "frame_id": "gnss",
        "namespace": "/mtrtk",
        "nmea_tcp": "",
        "reconnect_s": 2.0,
        "stale_s": 5.0,
    }


def test_node_publishes_every_topic_of_the_contract() -> None:
    topics = {}
    for node in ast.walk(_node_tree()):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_publisher"
        ):
            msg_type = node.args[0]
            topic = node.args[1]
            assert isinstance(msg_type, ast.Name) and isinstance(topic, ast.JoinedStr)
            suffix = topic.values[-1]
            assert isinstance(suffix, ast.Constant)
            topics[suffix.value] = msg_type.id
    assert topics == {
        "/fix": "NavSatFix",
        "/vel": "TwistWithCovarianceStamped",
        "/time_reference": "TimeReference",
        "/rtk_status": "RtkStatus",
        "/time_mark": "TimeMark",
        "/imu": "Imu",
        "/heading": "Float64",
        "/nmea": "Sentence",
    }


def _ros_imports(path: Path) -> set[str]:
    """Top-level packages imported by *path*, bar the stdlib and the bridge itself."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
    return names - set(sys.stdlib_module_names) - {"mtrtk_bridge", "__future__"}


def test_package_xml_declares_every_ros_dependency() -> None:
    xml = (PKG / "package.xml").read_text()
    declared = set(re.findall(r"<(?:exec_)?depend>([\w-]+)</(?:exec_)?depend>", xml))
    used = _ros_imports(NODE) | _ros_imports(LAUNCH)
    used = {"python3-websocket" if n == "websocket" else n for n in used}  # websocket-client
    assert used <= declared, used - declared
    assert "<build_type>ament_python</build_type>" in xml


def test_setup_installs_the_executable_launch_file_and_parameters() -> None:
    setup = (PKG / "setup.py").read_text()
    xml = (PKG / "package.xml").read_text()
    assert '"mtrtk_bridge = mtrtk_bridge.node:main"' in setup
    assert "def main(" in NODE.read_text()
    version = re.search(r"<version>([^<]+)</version>", xml)
    assert version and f'version="{version[1]}"' in setup
    assert (PKG / "resource" / "mtrtk_bridge").is_file()
    assert (PKG / "resource" / "mtrtk_bridge").read_bytes() == b""
    assert "$base/lib/mtrtk_bridge" in (PKG / "setup.cfg").read_text()


def test_the_bridge_never_imports_mtrtk() -> None:
    # Global constraint: the contract with the daemon is the WebSocket JSON, nothing else.
    for path in (PKG / "mtrtk_bridge").glob("*.py"):
        assert "mtrtk" not in _ros_imports(path), path.name
