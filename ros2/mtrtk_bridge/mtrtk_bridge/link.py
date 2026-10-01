"""Pure helpers for the node's two connections: the daemon's WebSocket and its NMEA TCP stream.

No rclpy and no sockets here, so all of it is unit-testable anywhere.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# What the bridge reads: the per-epoch pvt/rtk bundle, plus the `rtk` updates (NTRIP client
# status, time marks). Asking for less keeps the daemon from sending the satellite table.
WS_TOPICS = ("pvt", "rtk")
DEFAULT_NMEA_HOST = "127.0.0.1"
NMEA_MAX_LINE = 1024  # bytes, line ending included; NMEA 0183 allows 82 characters


def ws_connect_url(url: str, topics: tuple[str, ...] = WS_TOPICS) -> str:
    """The URL to dial: *url* with the `topics` the bridge needs.

    A `topics` already in *url* keeps its own entries and gains whichever of *topics* it lacks:
    without `pvt` the bridge would publish nothing. A `token` in *url* is kept (the compose
    profile may pass `MTRTK_WS_URL=...?token=<web token>`); the `token` parameter is not added
    here but sent as a header (`ws_headers`), so it stays out of the daemon's access log.
    """
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    asked = [t.strip() for k, v in query if k == "topics" for t in v.split(",") if t.strip()]
    asked += [t for t in topics if t not in asked]
    query = [(k, v) for k, v in query if k != "topics"] + [("topics", ",".join(asked))]
    return urlunsplit(parts._replace(query=urlencode(query, safe=",")))


def ws_headers(token: str) -> list[str]:
    """The handshake headers for *token*: `Authorization: Bearer`, which the daemon reads first."""
    token = token.strip()
    if not token:
        return []
    if any(c in token for c in "\r\n"):
        raise ValueError("token must be a single line")
    return [f"Authorization: Bearer {token}"]


def ws_error_reason(exc: BaseException) -> str:
    """Why a connection attempt or a connection failed, fit for one log line.

    A refused handshake (websocket-client's `WebSocketBadStatusException`, whose message dumps
    the response headers on newer versions) reads as its HTTP status, with a hint when that
    status means the daemon wants its web token.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        reason = f"handshake refused with HTTP {status}"
        if status in (401, 403):
            reason += " (is WEB_PASSWORD set? give the bridge its token)"
        return reason
    return str(exc) or type(exc).__name__


def redact_url(url: str) -> str:
    """*url* fit for a log line: a `token` in its query reads `***`."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if not any(k == "token" for k, _ in query):
        return url
    query = [(k, "***" if k == "token" else v) for k, v in query]
    return urlunsplit(parts._replace(query=urlencode(query, safe=",*")))


def parse_host_port(text: str, default_host: str = DEFAULT_NMEA_HOST) -> tuple[str, int]:
    """`host:port`, `:port`, `port` or `[v6]:port` -> (host, port). Raises ValueError."""
    text = text.strip()
    host, sep, port = text.rpartition(":")
    if not sep:
        host, port = "", text
    if host.startswith("[") or host.endswith("]"):
        if not (host.startswith("[") and host.endswith("]")):
            raise ValueError(f"bad address {text!r}")
        host = host[1:-1]
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"bad port in {text!r}: expected host:port")
    return host or default_host, int(port)


def nmea_text(line: bytes) -> str | None:
    """One line of the NMEA TCP stream -> the sentence without its line ending, or None.

    Anything that does not start with `$` (a blank line, a stray binary byte) is skipped; bytes
    that are not ASCII become U+FFFD instead of raising.
    """
    text = line.decode("ascii", "replace").strip()
    return text if text.startswith("$") else None


def nmea_sentences(
    readline: Callable[[int], bytes], max_line: int = NMEA_MAX_LINE
) -> Iterator[str]:
    """The sentences of a byte stream read with *readline* (a file's), until it ends.

    A line longer than *max_line* (a corrupt or runaway stream) is skipped whole: none of the
    pieces it is read in is published, so no sentence ever reaches ROS cut short.
    """
    overlong = False  # inside a line that did not fit
    while True:
        line = readline(max_line)
        if not line:
            return
        complete = line.endswith(b"\n")
        if overlong or (not complete and len(line) >= max_line):
            overlong = not complete
            continue
        text = nmea_text(line)
        if text is not None:
            yield text
