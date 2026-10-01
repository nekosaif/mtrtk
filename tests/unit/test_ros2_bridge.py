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
    Staleness,
    nmea_sentences,
    nmea_text,
    parse_host_port,
    redact_url,
    seconds,
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


@pytest.mark.parametrize(
    "url",
    ["ws://[::1/ws", "ws://rover:port/ws", "http://rover:8080/ws", "rover:8080/ws", "", "ws:///ws"],
)
def test_a_bad_ws_url_is_a_value_error_that_does_not_echo_the_url(url: str) -> None:
    # The node checks ws_url at start-up and says why it will not connect, instead of the
    # WebSocket thread dying on it; the URL may carry a token, so the reason never quotes it.
    with pytest.raises(ValueError) as err:
        ws_connect_url(url + ("?token=s3cret" if url else ""))
    assert "s3cret" not in str(err.value)


def test_wss_and_an_ipv6_host_are_fine() -> None:
    assert ws_connect_url("wss://rover.ts.net/ws").startswith("wss://rover.ts.net/ws?")
    assert ws_connect_url("ws://[::1]:8080/ws").startswith("ws://[::1]:8080/ws?")


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
        ("host:65535", ("host", 65535)),
    ],
)
def test_parse_host_port(text: str, expected: tuple[str, int]) -> None:
    assert parse_host_port(text) == expected


@pytest.mark.parametrize(
    "text", ["", "host:", "host:port", "host:0", "host:65536", "host:70000", "[::1", "host:\u0663"]
)
def test_parse_host_port_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError):
        parse_host_port(text)


# --------------------------------------------------------------------------- nmea_text
GGA = b"$GNGGA,123519.00,2350.24104,N,09015.75301,E,1,12,0.9,13.4,M,-49.6,M,,*6F"
VTG = b"$GNVTG,,T,,M,0.0,N,0.0,K,A*3D"


def test_nmea_text_strips_the_line_ending() -> None:
    line = b"$GNGGA,123519.00,2350.24104,N,09015.75301,E,4,12,0.9,13.4,M,-49.6,M,1.0,0000*45\r\n"
    assert nmea_text(line) == line.decode().strip()


def test_nmea_text_checks_the_checksum_when_there_is_one() -> None:
    assert nmea_text(VTG + b"\r\n") == VTG.decode()
    assert nmea_text(VTG.lower().replace(b"$gnvtg", b"$GNVTG") + b"\n") is None  # body changed
    assert nmea_text(VTG[:-2] + b"3d\r\n") == VTG[:-2].decode() + "3d"  # either hex case
    assert nmea_text(b"$GPTXT,01,01,02,hello\r\n") == "$GPTXT,01,01,02,hello"  # NMEA allows none


@pytest.mark.parametrize("line", [b"", b"\r\n", b"garbage\r\n", b"\xb5b\x01\x07"])
def test_nmea_text_skips_what_is_not_a_sentence(line: bytes) -> None:
    assert nmea_text(line) is None


@pytest.mark.parametrize(
    "line",
    [
        b"$GNGGA,123519.00,2350.24104,N,09015.75301,E,4,12,0.9,13.4,M,-49.6,M,1.0,0000*5C\r\n",
        VTG[:-1] + b"\r\n",  # one digit
        VTG[:-1] + b"G\r\n",  # not hex
        VTG + VTG + b"\r\n",  # two sentences run together
        VTG + b" junk\r\n",
    ],
)
def test_nmea_text_drops_a_sentence_whose_checksum_does_not_hold(line: bytes) -> None:
    assert nmea_text(line) is None


def test_nmea_text_never_raises_on_bad_bytes() -> None:
    # Not ASCII is corrupt: no checksum over U+FFFD can match, so it is dropped, not raised.
    assert nmea_text(b"$GNRMC,\xff\xfe*00\r\n") is None
    assert nmea_text(b"$GNRMC,\xff\xfe\r\n") == "$GNRMC,��"  # no checksum to hold


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


def test_a_stream_that_ends_mid_sentence_does_not_publish_the_fragment() -> None:
    # The daemon restarting or the link dropping cuts the last line short: readline hands back
    # the piece it has, with no line ending, and that piece is not a sentence.
    stream = io.BytesIO(GGA + b"\r\n" + GGA[:20])
    assert list(nmea_sentences(stream.readline)) == [GGA.decode()]
    stream = io.BytesIO(GGA + b"\r\n" + GGA)  # even a whole one: its end was never seen
    assert list(nmea_sentences(stream.readline)) == [GGA.decode()]
    # ... nor when it ends inside an overlong line.
    stream = io.BytesIO(GGA + b"\r\n" + b"$" * 250)
    assert list(nmea_sentences(stream.readline, max_line=100)) == [GGA.decode()]


# --------------------------------------------------------------------------- seconds
@pytest.mark.parametrize(("value", "expected"), [(5.0, 5.0), (2, 2.0), (0.01, 0.5), (-3, 0.5)])
def test_seconds_takes_an_int_or_a_float_and_clamps_it(value: object, expected: float) -> None:
    assert seconds(value, minimum=0.5) == expected


@pytest.mark.parametrize("value", ["abc", "5s", "5", True, None, float("nan"), float("inf"), [5]])
def test_seconds_rejects_what_is_not_a_number_of_seconds(value: object) -> None:
    # stale_s/reconnect_s take any type (`stale_s:=5` is an int); a string must not reach the
    # watchdog's float(), which would raise inside the timer and take the whole node down.
    with pytest.raises(ValueError, match="number of seconds"):
        seconds(value, minimum=0.5)


# --------------------------------------------------------------------------- Staleness
def test_staleness_is_counted_from_start_up() -> None:
    # A daemon that is down from the outset is a loss the consumers must see too.
    s = Staleness(5.0, now=100.0)
    assert s.check(105.0) == (False, False)  # exactly stale_s is not yet stale
    assert s.check(105.1) == (True, True)  # just past it: publish no fix, log it
    assert s.check(106.1) == (True, False)  # still stale: publish again, log once only
    assert s.silent(106.1) == pytest.approx(6.1)


def test_an_epoch_ends_a_stale_spell_once() -> None:
    s = Staleness(5.0, now=100.0)
    assert s.epoch(101.0) is False  # not stale: nothing to log
    assert s.check(106.0) == (False, False)  # counted from the epoch, not start-up
    assert s.check(106.5) == (True, True)
    assert s.epoch(107.0) is True  # recovered: log it
    assert s.epoch(108.0) is False
    assert s.check(112.9) == (False, False)
    assert s.check(113.1) == (True, True)  # a second spell is logged again


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


# A best-effort denylist of the 3.11+ stdlib names most likely to slip in (ruff's py310 target
# for ros2/** catches new syntax, not new APIs). It is no substitute for running the node on
# 3.10: P7T4's Humble image build (colcon build + ros2 run on Ubuntu 22.04) is that check.
PY311_NAMES = {("datetime", "UTC"), ("asyncio", "timeout"), ("asyncio", "TaskGroup")}
PY311_NAMES |= {("asyncio", "timeout_at"), ("asyncio", "Runner"), ("asyncio", "Barrier")}
PY311_NAMES |= {("typing", n) for n in ("Self", "Never", "LiteralString", "assert_never")}
PY311_NAMES |= {("typing", n) for n in ("assert_type", "reveal_type", "Required", "NotRequired")}
PY311_NAMES |= {("typing", n) for n in ("TypeVarTuple", "Unpack", "dataclass_transform")}
PY311_NAMES |= {("typing", "override"), ("typing", "TypeAliasType")}
PY311_NAMES |= {("enum", n) for n in ("StrEnum", "ReprEnum", "verify", "member", "nonmember")}
PY311_NAMES |= {("hashlib", "file_digest"), ("contextlib", "chdir"), ("operator", "call")}
PY311_NAMES |= {("math", "exp2"), ("math", "cbrt"), ("itertools", "batched")}
PY311_NAMES |= {("logging", "getLevelNamesMapping")}


def test_the_bridge_uses_nothing_newer_than_python_310() -> None:
    for path in [*(PKG / "mtrtk_bridge").glob("*.py"), LAUNCH, PKG / "setup.py"]:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    assert (node.module, alias.name) not in PY311_NAMES, (path.name, alias.name)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert (node.value.id, node.attr) not in PY311_NAMES, (path.name, node.attr)
            elif isinstance(node, ast.Import):
                assert all(a.name not in ("tomllib", "wsgiref.types") for a in node.names), path
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


def _parameter_file() -> dict[str, Any]:
    """config/bridge.yaml, read without PyYAML (not a dependency of this repo).

    The file is flat on purpose - `mtrtk_bridge:` / `ros__parameters:` / one scalar per line -
    so a line this cannot read is a line the test should fail on.
    """
    lines = [ln for ln in (PKG / "config" / "bridge.yaml").read_text().splitlines() if ln.strip()]
    assert lines[:2] == ["mtrtk_bridge:", "  ros__parameters:"]
    params: dict[str, Any] = {}
    for line in lines[2:]:
        m = re.fullmatch(r'    (\w+): ("(?:[^"\\]|\\.)*"|[-+\d.eE]+)\s*(#.*)?', line)
        assert m, line
        params[m[1]] = ast.literal_eval(m[2])
    return params


def test_parameter_file_matches_the_declared_parameters() -> None:
    assert _parameter_file() == _declared_parameters()
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
            msg_type, topic, depth = node.args
            assert isinstance(msg_type, ast.Name) and isinstance(topic, ast.JoinedStr)
            # f"{ns}/<name>": every topic under the `namespace` parameter
            assert len(topic.values) == 2, ast.unparse(topic)
            prefix, suffix = topic.values
            assert isinstance(prefix, ast.FormattedValue), ast.unparse(topic)
            assert isinstance(prefix.value, ast.Name) and prefix.value.id == "ns"
            assert isinstance(suffix, ast.Constant) and isinstance(depth, ast.Constant)
            topics[suffix.value] = (msg_type.id, depth.value)
    assert topics == {
        "/fix": ("NavSatFix", 10),
        "/vel": ("TwistWithCovarianceStamped", 10),
        "/time_reference": ("TimeReference", 10),
        "/rtk_status": ("RtkStatus", 10),
        "/time_mark": ("TimeMark", 50),  # bursts: a camera can fire faster than the epoch rate
        "/imu": ("Imu", 10),
        "/heading": ("Float64", 10),
        "/nmea": ("Sentence", 50),
    }
    # ... and `ns` is the namespace parameter, minus a trailing slash.
    assert 'ns = str(self.get_parameter("namespace").value).rstrip("/")' in NODE.read_text()


def _method(name: str) -> ast.FunctionDef:
    for node in ast.walk(_node_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"MtrtkBridge.{name} is gone")


def _calls(tree: ast.AST) -> set[str]:
    """`self.x.y(...)` / `self.y(...)` calls in *tree*, as "x.y" / "y"."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id == "self":
                found.add(node.func.attr)
            elif isinstance(owner, ast.Attribute) and isinstance(owner.value, ast.Name):
                found.add(f"{owner.attr}.{node.func.attr}")
    return found


def _under_the_lock(tree: ast.AST) -> list[ast.AST]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.With)
        and any(ast.unparse(item.context_expr) == "self._lock" for item in node.items)
    ]


def test_an_epoch_feeds_the_watchdog_and_every_message_flushes_time_marks() -> None:
    # The node glue cannot run here (no rclpy); this pins the calls its behaviour rests on:
    # without `staleness.epoch` the watchdog would interleave no-fix with every real fix.
    (locked,) = _under_the_lock(_method("_handle"))
    assert {"_staleness.epoch", "_publish_epoch", "_publish_time_marks"} <= _calls(locked)


def test_the_watchdog_publishes_its_no_fix_under_the_lock() -> None:
    # The WebSocket thread publishes epochs under the same lock: decided stale, then a fresh
    # fix, then the stale no-fix would leave consumers with "no fix" right after recovery.
    watchdog = _method("_watchdog")
    (locked,) = _under_the_lock(watchdog)
    assert {"_staleness.check", "pub_fix.publish"} <= _calls(locked)
    assert "get_parameter" not in _calls(watchdog)  # stale_s is read (and checked) once


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


def test_every_ros2_image_installs_the_bridges_python_dependencies() -> None:
    # The bridge needs websocket-client (apt python3-websocket), not websockets: an image built
    # without it would fail at `import websocket`. Checks P7T4's Dockerfile(s) once they exist.
    xml = (PKG / "package.xml").read_text()
    apt = set(re.findall(r"<exec_depend>(python3-[\w-]+)</exec_depend>", xml))
    assert "python3-websocket" in apt
    dockerfiles = sorted((ROOT / "ros2").glob("Dockerfile*"))
    if not dockerfiles:
        pytest.skip("no ros2/Dockerfile yet (Phase 7 Task 4)")
    for path in dockerfiles:
        installed = set(re.findall(r"\bpython3-[\w-]+", path.read_text()))
        assert apt <= installed, (path.name, apt - installed)
