"""Pure helpers for the node: its two connections (the daemon's WebSocket and its NMEA TCP
stream), its duration parameters and its stale-fix watchdog.

No rclpy and no sockets here, so all of it is unit-testable anywhere.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# What the bridge reads: the per-epoch pvt/rtk bundle, plus the `rtk` updates (NTRIP client
# status, time marks), and `ins`, whose per-epoch bundle carries an INS rover's attitude (for
# /mtrtk/imu and /mtrtk/heading; null on a u-blox rover). Asking for less keeps the daemon from
# sending the satellite table.
WS_TOPICS = ("pvt", "rtk", "ins")
DEFAULT_NMEA_HOST = "127.0.0.1"
NMEA_MAX_LINE = 1024  # bytes, line ending included; NMEA 0183 allows 82 characters


def ws_connect_url(url: str, topics: tuple[str, ...] = WS_TOPICS) -> str:
    """The URL to dial: *url* with the `topics` the bridge needs.

    A `topics` already in *url* keeps its own entries and gains whichever of *topics* it lacks:
    without `pvt` the bridge would publish nothing. A `token` in *url* is kept (the compose
    profile may pass `MTRTK_WS_URL=...?token=<web token>`); the `token` parameter is not added
    here but sent as a header (`ws_headers`), so it stays out of the daemon's access log.

    Raises ValueError for a URL that cannot be dialled, with a reason that never quotes the URL
    (it may carry a token).
    """
    parts = urlsplit(url)  # ValueError for a broken IPv6 host
    if parts.scheme not in ("ws", "wss"):
        got = f"{parts.scheme}://" if parts.scheme else "no scheme"
        raise ValueError(f"expected ws:// or wss://, got {got}")
    if not parts.hostname:
        raise ValueError("no host")
    try:
        _ = parts.port  # parsed, and checked, on access
    except ValueError:
        raise ValueError("bad port") from None
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
    if not (port.isascii() and port.isdigit()) or not 0 < int(port) < 65536:
        raise ValueError(f"bad port in {text!r}: expected host:port")
    return host or default_host, int(port)


_CHECKSUM = re.compile(r"\*([0-9A-Fa-f]{2})")


def nmea_text(line: bytes) -> str | None:
    """One line of the NMEA TCP stream -> the sentence without its line ending, or None.

    Anything that does not start with `$` (a blank line, a stray binary byte) is skipped, and so
    is a sentence whose `*hh` checksum does not hold (corrupt, cut, or two run together). A
    sentence with no `*` at all is passed (NMEA 0183 makes the checksum optional). Bytes that are
    not ASCII become U+FFFD instead of raising.
    """
    text = line.decode("ascii", "replace").strip()
    if not text.startswith("$"):
        return None
    body, star, tail = text[1:].partition("*")
    if not star:
        return text
    want = _CHECKSUM.fullmatch("*" + tail)
    got = 0
    for c in body:
        got ^= ord(c)
    return text if want and int(want[1], 16) == got else None


def nmea_sentences(
    readline: Callable[[int], bytes], max_line: int = NMEA_MAX_LINE
) -> Iterator[str]:
    """The sentences of a byte stream read with *readline* (a file's), until it ends.

    A line longer than *max_line* (a corrupt or runaway stream) is skipped whole: none of the
    pieces it is read in is published. Nor is a last line the stream ended before finishing
    (the daemon restarting, the link dropping): its end was never seen. So no sentence ever
    reaches ROS cut short.
    """
    overlong = False  # inside a line that did not fit
    while True:
        line = readline(max_line)
        if not line:
            return
        complete = line.endswith(b"\n")
        if overlong or not complete:  # too long, or the stream ended mid-line
            overlong = not complete
            continue
        text = nmea_text(line)
        if text is not None:
            yield text


def seconds(value: object, minimum: float) -> float:
    """A duration parameter -> seconds, at least *minimum*.

    The parameters take any type, so `reconnect_s:=2` (an int) works; anything that is not a
    finite int or float (`"5s"`, `true`) raises ValueError, at start-up rather than in a timer.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"expected a number of seconds, got {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"expected a finite number of seconds, got {value!r}")
    return max(minimum, float(value))


class Staleness:
    """When the watchdog publishes a no-fix: no epoch for more than *stale_s* seconds.

    Counted from start-up as well, since a daemon that is down from the outset is a loss the
    consumers must see too. Times are a monotonic clock's, passed in.
    """

    def __init__(self, stale_s: float, now: float) -> None:
        self.stale_s = stale_s
        self.last_epoch = now
        self.stale = False

    def epoch(self, now: float) -> bool:
        """An epoch arrived at *now*. True if it ends a stale spell (log the recovery once)."""
        self.last_epoch = now
        recovered, self.stale = self.stale, False
        return recovered

    def check(self, now: float) -> tuple[bool, bool]:
        """(publish a no-fix now, the spell just began so log it once)."""
        if self.silent(now) <= self.stale_s:
            return False, False
        began, self.stale = not self.stale, True
        return True, began

    def silent(self, now: float) -> float:
        """Seconds since the last epoch (or start-up)."""
        return now - self.last_epoch
