"""In-process NTRIP caster (v1 + v2) that serves the receiver's RTCM3 frames to rovers."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from pynmeagps import NMEAReader

from mtrtk import __version__
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame
from mtrtk.store.repos import NtripLogRepo

log = logging.getLogger(__name__)

HEADER_LIMIT = 8192  # a rover's request head never comes near this
HEADER_TIMEOUT_S = 10.0
CLIENT_QUEUE_FRAMES = 64  # ~10 s of a 6 msg/s correction stream before the oldest frame goes
SLOW_CLIENT_BYTES = 256 * 1024  # unsent bytes in the socket buffer that mark a client as slow
SLOW_CLIENT_GRACE_S = 10.0  # ... and how long it may stay that way before we hang up
SLOW_CLIENT_POLL_S = 1.0  # how often a blocked write is re-checked against those two
SHUTDOWN_GRACE_S = 5.0
REALM = "mtrtk"
SERVER_NAME = f"mtrtk/{__version__}"
BAD_REQUEST = b"HTTP/1.0 400 Bad Request\r\nConnection: close\r\n\r\n"
NOT_SUPPORTED = b"ERROR - Not Supported\r\n"
NOT_FOUND = f"HTTP/1.1 404 Not Found\r\nServer: {SERVER_NAME}\r\nConnection: close\r\n\r\n".encode()


@dataclass
class CasterConfig:
    mountpoint: str
    username: str
    password: str  # "" = anonymous
    station_id: str
    country: str
    identifier: str = "mtrtk"
    format_details: str = "1005(1),1077(1),1087(1),1097(1),1127(1),1230(5)"
    nav_system: str = "GPS+GLO+GAL+BDS"
    receiver: str = "u-blox ZED-F9P"

    @property
    def anonymous(self) -> bool:
        return self.password == ""


@dataclass
class Request:
    method: str
    path: str
    v2: bool
    headers: dict[str, str]


def parse_request(head: bytes) -> Request:
    """Split an NTRIP request head into its verb, mountpoint path and lower-cased headers."""
    text = head.decode("latin-1")
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    method = parts[0].upper() if parts else ""
    path = parts[1] if len(parts) > 1 else "/"
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    # The one thing that tells the versions apart: v1 clients send no Ntrip-Version header.
    v2 = headers.get("ntrip-version", "").lower().startswith("ntrip/2")
    return Request(method, path, v2, headers)


@dataclass
class ClientInfo:
    id: int
    ip: str
    port: int
    mountpoint: str
    user_agent: str
    username: str | None
    version: int
    connected_utc: datetime
    bytes_sent: int = 0
    dropped_frames: int = 0
    last_gga_lat: float | None = None
    last_gga_lon: float | None = None
    last_gga_utc: datetime | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ip": self.ip,
            "port": self.port,
            "mountpoint": self.mountpoint,
            "user_agent": self.user_agent,
            "username": self.username,
            "version": self.version,
            "connected_utc": self.connected_utc.isoformat(),
            "bytes_sent": self.bytes_sent,
            "dropped_frames": self.dropped_frames,
            "last_gga_lat": self.last_gga_lat,
            "last_gga_lon": self.last_gga_lon,
            "last_gga_utc": self.last_gga_utc.isoformat() if self.last_gga_utc else None,
        }


class _Client:
    """One connected rover: its public stats, its socket and its own bounded frame queue."""

    def __init__(self, info: ClientInfo, writer: asyncio.StreamWriter, v2: bool) -> None:
        self.info = info
        self.writer = writer
        self.v2 = v2
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=CLIENT_QUEUE_FRAMES)
        self.dropped = 0
        self.log_row: int | None = None
        self.slow_since: float | None = None
        self.stop_reason: str | None = None

    def wire(self, raw: bytes) -> bytes:
        """One RTCM frame as it goes on the wire: a chunk for v2, the bare frame for v1.

        An empty frame is the shutdown sentinel; for v2 it encodes to `0\\r\\n\\r\\n`, which is
        exactly the terminating chunk, and for v1 to nothing at all.
        """
        if self.v2:
            return f"{len(raw):X}\r\n".encode() + raw + b"\r\n"
        return raw


class NtripCaster:
    """Serves `raw.rtcm` to NTRIP v1 and v2 rovers on one mountpoint."""

    def __init__(
        self,
        bus: Bus,
        config: CasterConfig,
        host: str,
        port: int = 2101,
        ntrip_log: NtripLogRepo | None = None,
        position: Callable[[], tuple[float, float] | None] = lambda: None,
        bitrate: Callable[[], float] = lambda: 0.0,
    ) -> None:
        self.bus = bus
        self.config = config
        self.host = host
        self._port = port
        self.ntrip_log = ntrip_log
        self._position = position
        self._bitrate = bitrate
        self.sub = bus.subscribe("raw.rtcm", maxsize=500)
        self.clients: dict[int, ClientInfo] = {}
        self._conns: dict[int, _Client] = {}
        self._next_id = 1
        self._last_1005: bytes | None = None
        self._last_1230: bytes | None = None
        self._server: asyncio.Server | None = None
        self._feed_task: asyncio.Task[None] | None = None
        self._handlers: set[asyncio.Task[Any]] = set()
        self._stopping = False

    # ------------------------------------------------------------------- lifecycle
    @property
    def port(self) -> int:
        """The port actually bound (the caller may have asked for 0)."""
        if self._server and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self._port

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_conn, self.host, self._port, limit=HEADER_LIMIT * 2
        )
        self._port = self.port  # remember the kernel's choice when the caller asked for 0
        self._feed_task = asyncio.create_task(self._feed(), name="ntrip-feed")
        log.info(
            "NTRIP caster listening on %s:%d mountpoint /%s (%s)",
            self.host,
            self.port,
            self.config.mountpoint,
            "anonymous" if self.config.anonymous else "auth required",
        )

    async def stop(self) -> None:
        self._stopping = True
        self.bus.unsubscribe(self.sub)
        if self._feed_task is not None:
            self._feed_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._feed_task
            self._feed_task = None
        if self._server is not None:
            self._server.close()  # stop accepting; connected rovers are closed below
        for conn in list(self._conns.values()):
            self._end(conn, "caster stopped")
        if self._handlers:
            # Each handler ends its stream, writes its log row and closes its socket. Bounded:
            # a rover that has stopped reading must not hold the daemon's shutdown open.
            await asyncio.wait(set(self._handlers), timeout=SHUTDOWN_GRACE_S)
        if self._server is not None:
            # 3.12's wait_closed() also waits for the handlers above, hence the timeout.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._server.wait_closed(), SHUTDOWN_GRACE_S)

    # ------------------------------------------------------------------- fan-out
    async def _feed(self) -> None:
        async for _, frame in self.sub:
            try:
                self._on_frame(frame)
            except Exception:  # one odd frame must never end the correction stream
                log.exception("NTRIP caster dropped a frame it could not fan out")

    def _on_frame(self, frame: Frame) -> None:
        raw = frame.raw
        msg_type = frame.rtcm_type
        # A rover that joins between two 1005/1230 cycles still needs them to fix: keep the last.
        if msg_type == 1005:
            self._last_1005 = raw
        elif msg_type == 1230:
            self._last_1230 = raw
        for conn in self._conns.values():
            self._offer(conn, raw)

    @staticmethod
    def _offer(conn: _Client, raw: bytes) -> None:
        """Queue one frame for *conn*, dropping its oldest when it cannot keep up."""
        try:
            conn.queue.put_nowait(raw)
        except asyncio.QueueFull:
            conn.queue.get_nowait()
            conn.dropped += 1
            conn.queue.put_nowait(raw)

    def _end(self, conn: _Client, reason: str) -> None:
        """Ask a client's write loop to finish: the empty frame both ends and wakes the stream."""
        if conn.stop_reason is None:
            conn.stop_reason = reason
        self._offer(conn, b"")

    # ------------------------------------------------------------------- sourcetable
    def sourcetable_body(self) -> bytes:
        pos = self._position()
        lat, lon = pos if pos else (0.0, 0.0)
        c = self.config
        line = (
            f"STR;{c.mountpoint};{c.identifier};RTCM 3.3;{c.format_details};2;{c.nav_system};"
            f"mtrtk;{c.country};{lat:.2f};{lon:.2f};0;0;{c.receiver};none;"
            f"{'N' if c.anonymous else 'B'};N;{self._bitrate():.0f};\r\n"
        )
        return line.encode() + b"ENDSOURCETABLE\r\n"

    def _sourcetable_response(self, v2: bool) -> bytes:
        body = self.sourcetable_body()
        if v2:
            head = (
                "HTTP/1.1 200 OK\r\n"
                "Ntrip-Version: Ntrip/2.0\r\n"
                f"Server: {SERVER_NAME}\r\n"
                "Content-Type: gnss/sourcetable\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            )
        else:
            head = (
                "SOURCETABLE 200 OK\r\n"
                f"Server: {SERVER_NAME}\r\n"
                "Content-Type: text/plain\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            )
        return head.encode() + body

    def _stream_response(self, v2: bool) -> bytes:
        if not v2:
            return b"ICY 200 OK\r\n\r\n"  # v1 says this and nothing else, then raw RTCM
        return (
            "HTTP/1.1 200 OK\r\n"
            "Ntrip-Version: Ntrip/2.0\r\n"
            f"Server: {SERVER_NAME}\r\n"
            "Content-Type: gnss/data\r\n"
            "Transfer-Encoding: chunked\r\n"
            "Cache-Control: no-store, no-cache, max-age=0\r\n"
            "Pragma: no-cache\r\n"
            "Connection: close\r\n\r\n"
        ).encode()

    # ------------------------------------------------------------------- auth
    def _authorized(self, req: Request) -> tuple[bool, str | None]:
        if self.config.anonymous:
            return True, None
        header = req.headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            return False, None
        try:
            user, _, password = base64.b64decode(header[6:].strip()).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False, None
        # `&`, not `and`: both digests are always compared, so a wrong user costs the same time.
        ok = hmac.compare_digest(
            user.encode(), self.config.username.encode()
        ) & hmac.compare_digest(password.encode(), self.config.password.encode())
        return bool(ok), user if ok else None

    @staticmethod
    def _unauthorized(v2: bool) -> bytes:
        status = "HTTP/1.1 401 Unauthorized" if v2 else "HTTP/1.0 401 Unauthorized"
        return (
            f"{status}\r\n"
            f'WWW-Authenticate: Basic realm="{REALM}"\r\n'
            f"Server: {SERVER_NAME}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()

    # ------------------------------------------------------------------- connections
    async def _handle_conn(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
            task.add_done_callback(self._handlers.discard)
        peer = writer.get_extra_info("peername") or ("?", 0)
        ip, port = str(peer[0]), int(peer[1])
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEADER_TIMEOUT_S)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            # OSError covers both the timeout and a peer that went away mid-request.
            await self._close(writer, BAD_REQUEST)
            return
        if len(head) > HEADER_LIMIT:
            await self._close(writer, BAD_REQUEST)
            return
        req = parse_request(head)
        if req.method != "GET":  # SOURCE uploads: this caster is the base's own, not a relay
            await self._close(writer, NOT_SUPPORTED)
            return
        mount = req.path.lstrip("/").split("?", 1)[0]
        if mount == "":
            await self._close(writer, self._sourcetable_response(req.v2))
            return
        if mount != self.config.mountpoint:
            await self._close(writer, NOT_FOUND if req.v2 else self._sourcetable_response(False))
            return
        ok, username = self._authorized(req)
        if not ok:
            await self._close(writer, self._unauthorized(req.v2))
            return
        await self._serve_stream(reader, writer, req, ip, port, mount, username)

    async def _serve_stream(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        req: Request,
        ip: str,
        port: int,
        mount: str,
        username: str | None,
    ) -> None:
        writer.write(self._stream_response(req.v2))
        await writer.drain()

        info = ClientInfo(
            id=self._next_id,
            ip=ip,
            port=port,
            mountpoint=mount,
            user_agent=req.headers.get("user-agent", ""),
            username=username,
            version=2 if req.v2 else 1,
            connected_utc=datetime.now(UTC),
        )
        self._next_id += 1
        conn = _Client(info, writer, req.v2)
        gga_task: asyncio.Task[None] | None = None
        reason = "client closed"
        # Everything from the registration on lives in this try: whatever fails in between,
        # the finally below is what takes the client back out again.
        try:
            self._conns[info.id] = conn
            self.clients[info.id] = info
            if self._stopping:  # accepted just as the caster went down: end it, never orphan it
                self._end(conn, "caster stopped")
            conn.log_row = await self._log_connected(info)
            log.info(
                "NTRIP client %d connected from %s (v%d, %s)",
                info.id,
                ip,
                info.version,
                info.user_agent or "no agent",
            )
            self._publish_clients()
            for cached in (self._last_1005, self._last_1230):
                if cached:
                    self._offer(conn, cached)

            gga_task = asyncio.create_task(
                self._read_gga(conn, reader), name=f"ntrip-gga-{info.id}"
            )
            reason = await self._write_loop(conn)
        except asyncio.CancelledError:
            reason = "cancelled"  # the daemon is going down: say so, then stay cancelled
            raise
        except OSError as exc:
            reason = type(exc).__name__
        finally:
            if gga_task is not None:
                gga_task.cancel()
                await asyncio.gather(gga_task, return_exceptions=True)
            self._conns.pop(info.id, None)
            self.clients.pop(info.id, None)
            writer.close()
            await self._log_disconnected(conn, reason)
            log.info(
                "NTRIP client %d disconnected (%s, %d bytes, %d frames dropped)",
                info.id,
                reason,
                info.bytes_sent,
                info.dropped_frames,
            )
            self._publish_clients()

    async def _log_connected(self, info: ClientInfo) -> int | None:
        """The client's log row id, or None: a failing database costs the row, not the stream."""
        if self.ntrip_log is None:
            return None
        try:
            return await self.ntrip_log.connected(
                info.ip, info.mountpoint, info.user_agent, info.username
            )
        except Exception:
            log.exception("NTRIP client %d: could not log the connection", info.id)
            return None

    async def _log_disconnected(self, conn: _Client, reason: str) -> None:
        if self.ntrip_log is None or conn.log_row is None:
            return
        info = conn.info
        try:
            await self.ntrip_log.disconnected(
                conn.log_row, info.bytes_sent, info.last_gga_lat, info.last_gga_lon, reason
            )
        except Exception:  # never let the log stop the rest of the teardown
            log.exception("NTRIP client %d: could not log the disconnection", info.id)

    async def _write_loop(self, conn: _Client) -> str:
        """Stream queued frames to one rover; returns why the stream ended."""
        writer = conn.writer
        transport = cast(asyncio.WriteTransport, writer.transport)
        # Back-pressure at our own threshold, not asyncio's 64 KB default: below it drain() must
        # return at once, or a stalled rover would park this loop inside drain() and never be
        # measured as slow. Above it drain() blocks, so it is polled.
        transport.set_write_buffer_limits(high=SLOW_CLIENT_BYTES)
        while True:
            raw = await conn.queue.get()
            if conn.stop_reason is not None:
                return await self._finish_stream(conn)
            writer.write(conn.wire(raw))
            conn.info.bytes_sent += len(raw)
            conn.info.dropped_frames = conn.dropped
            if writer.is_closing():
                return "client closed"
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(writer.drain(), SLOW_CLIENT_POLL_S)
            if transport.get_write_buffer_size() > SLOW_CLIENT_BYTES:
                now = time.monotonic()
                conn.slow_since = conn.slow_since or now
                if now - conn.slow_since > SLOW_CLIENT_GRACE_S:
                    return "slow client"
            else:
                conn.slow_since = None

    async def _finish_stream(self, conn: _Client) -> str:
        """End one stream: drop whatever is still queued, then close the body exactly once.

        A v2 body is closed by the terminating chunk `0\\r\\n\\r\\n`, which is what an empty frame
        encodes to; a v1 stream has no framing to close and simply stops.
        """
        while not conn.queue.empty():
            conn.queue.get_nowait()
        conn.writer.write(conn.wire(b""))
        with contextlib.suppress(OSError):  # TimeoutError is an OSError
            await asyncio.wait_for(conn.writer.drain(), SLOW_CLIENT_POLL_S)
        return conn.stop_reason or "caster stopped"

    async def _read_gga(self, conn: _Client, reader: asyncio.StreamReader) -> None:
        """Rovers push their position as NMEA GGA on the same socket; it is the only input."""
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                if line.startswith(b"$") and b"GGA" in line[:7]:
                    self._on_gga(conn, line)
        except (ValueError, OSError):  # an over-long line or a peer that vanished: same outcome
            return
        finally:
            self._end(conn, "client closed")

    def _on_gga(self, conn: _Client, line: bytes) -> None:
        try:
            msg = NMEAReader.parse(line.strip() + b"\r\n")
            lat, lon = float(msg.lat), float(msg.lon)
        except Exception:  # a malformed GGA from a rover is not our problem
            log.debug("ignoring bad GGA from NTRIP client %d", conn.info.id)
            return
        conn.info.last_gga_lat = lat
        conn.info.last_gga_lon = lon
        conn.info.last_gga_utc = datetime.now(UTC)
        self._publish_clients()

    def _publish_clients(self) -> None:
        self.bus.publish("ntrip.clients", list(self.clients.values()))

    @staticmethod
    async def _close(writer: asyncio.StreamWriter, payload: bytes) -> None:
        with contextlib.suppress(OSError):
            writer.write(payload)
            await writer.drain()
        writer.close()
