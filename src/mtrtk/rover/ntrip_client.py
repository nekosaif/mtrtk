"""NTRIP v2/v1 client: pulls RTCM from a caster, frames it and hands valid frames to the driver.

Protocol (spec "NTRIP"): ask as v2 (`HTTP/1.1`, `Host`, `Ntrip-Version: Ntrip/2.0`); accept
`ICY 200 OK` (raw stream) or `HTTP/1.x 200` (de-chunked when the caster says so). A sourcetable
answer means the mountpoint is missing and a 401 means bad credentials: both wait 60 s. Anything
else earns one immediate retry as pure v1, then the usual 1 -> 60 s jittered backoff. A stream
that is silent for 10 s is dropped and reconnected. Incoming bytes are re-framed with the CRC
check, so only whole, valid RTCM3 frames ever reach the receiver.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import logging
import random
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

from mtrtk import __version__
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer, Proto
from mtrtk.rover.drivers.base import RoverDriver

log = logging.getLogger(__name__)

NO_DATA_TIMEOUT_S = 10.0
CONNECT_TIMEOUT_S = 10.0
HEADER_TIMEOUT_S = 10.0
AUTH_BACKOFF_S = 60.0
MOUNT_BACKOFF_S = 60.0
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0
STATUS_INTERVAL_S = 5.0
STABLE_S = 30.0  # a stream that lasted this long resets the backoff when it drops
MAX_HEADERS = 64
MAX_CHUNK = 64 * 1024  # far above any real RTCM chunk; a bigger size is a broken caster
READ_SIZE = 4096
DEFAULT_PORT = 2101
SCHEMES = ("ntrip", "http")

# The backoff wait, as a seam tests replace. Only the backoff goes through it: patching
# `asyncio.sleep` itself would also turn the GGA loop's wait into a busy spin.
_sleep = asyncio.sleep


@dataclass(frozen=True)
class NtripClientConfig:
    host: str
    port: int
    mountpoint: str
    username: str | None
    password: str | None

    @classmethod
    def from_url(cls, url: str) -> NtripClientConfig:
        """`ntrip://user:pass@host:2101/MOUNT` (or `http://`); user and port are optional."""
        parsed = urlparse(url if "://" in url else f"ntrip://{url}")
        if parsed.scheme.lower() not in SCHEMES:
            raise ValueError(f"NTRIP URL scheme must be ntrip:// or http://, not {parsed.scheme}")
        mount = parsed.path.strip("/")
        if not parsed.hostname or not mount:
            raise ValueError("NTRIP URL must look like ntrip://user:pass@host:2101/MOUNTPOINT")
        port = parsed.port or DEFAULT_PORT  # .port raises ValueError for a non-numeric port
        user = unquote(parsed.username) if parsed.username is not None else None
        password = unquote(parsed.password) if parsed.password is not None else None
        return cls(parsed.hostname, port, mount, user, password)


@dataclass
class NtripClientStatus:
    connected: bool = False
    host: str = ""
    port: int = 0
    mountpoint: str = ""
    version: int | None = None
    bytes_received: int = 0
    frames_injected: int = 0
    # Frames the re-framer rejected for a bad checksum, cumulative across reconnects. In
    # practice these are RTCM3 CRC failures; a UBX or NMEA lookalike in the stream with a bad
    # checksum counts too, since either way it is corrupt data from the caster.
    crc_dropped: int = 0
    last_rtcm_mono: float | None = None
    last_error: str | None = None
    reconnects: int = 0
    next_retry_s: float | None = None
    since_mono: float | None = None


class _Response(Exception):
    """The caster answered, but not with a stream. `kind` is auth, mount or other."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class ProtocolError(ConnectionError):
    """The caster's bytes do not parse: a mangled chunk, an over-long line. Reconnecting may cure
    it, so it is a ConnectionError, and a plain ValueError from a driver bug is not mistaken
    for one."""


# Everything one session can fail with that a reconnect may cure: socket errors and timeouts
# (TimeoutError is an OSError; so is ProtocolError), a caster that hangs up mid-read
# (IncompleteReadError is an EOFError) and an over-long read (LimitOverrunError).
_NETWORK_ERRORS = (OSError, EOFError, asyncio.LimitOverrunError)


async def _readline(reader: asyncio.StreamReader) -> bytes:
    try:
        return await reader.readline()
    except ValueError as exc:  # StreamReader.readline's way of saying the line is too long
        raise ProtocolError(f"line from caster too long: {exc}") from exc


async def decode_chunked(reader: asyncio.StreamReader) -> bytes:
    """Read one HTTP/1.1 chunk and return its payload; `b""` at the terminating chunk or EOF.

    Raises ProtocolError on a malformed or oversized size line or a chunk not followed by CRLF.
    """
    size_line = await _readline(reader)
    if not size_line:
        return b""
    try:
        size = int(size_line.split(b";", 1)[0].strip(), 16)
    except ValueError as exc:
        raise ProtocolError(f"bad chunk size line {size_line[:20]!r}") from exc
    if size == 0:
        return b""
    if size < 0 or size > MAX_CHUNK:
        raise ProtocolError(f"chunk of {size} bytes exceeds the {MAX_CHUNK} byte limit")
    data = await reader.readexactly(size)
    if await reader.readexactly(2) != b"\r\n":
        raise ProtocolError("chunk not terminated by CRLF")
    return data


class NtripClient:
    def __init__(
        self,
        config: NtripClientConfig,
        bus: Bus,
        driver: RoverDriver,
        gga_provider: Callable[[], bytes | None],
        gga_interval_s: float = 10.0,
    ) -> None:
        self.config = config
        self.bus = bus
        self.driver = driver
        self.gga_provider = gga_provider
        self.gga_interval_s = gga_interval_s
        self.force_v1 = False
        self._sticky_v1 = False  # a v2 refusal was cured by v1: keep speaking v1 to this caster
        self._streamed = False  # the last session got as far as a 200
        self.status = NtripClientStatus(
            host=config.host, port=config.port, mountpoint=config.mountpoint
        )

    # ------------------------------------------------------------- lifecycle
    async def run(self, stop: asyncio.Event) -> None:
        try:
            await self._run(stop)
        finally:
            # Also on cancellation or an escaped error: never leave a stale connected=True.
            self._set_connected(False)
            self.status.next_retry_s = None
            self._publish()

    async def _run(self, stop: asyncio.Event) -> None:
        backoff = BACKOFF_MIN_S
        v1_retry = False
        while not stop.is_set():
            v2 = not (self.force_v1 or self._sticky_v1 or v1_retry)
            self._streamed = False
            try:
                await self._until_stop(self._session(v2), stop)
                break  # a session only ever returns because stop was set
            except _Response as exc:
                self.status.last_error = str(exc)
                if exc.kind == "other" and v2:
                    log.warning("NTRIP %s; retrying once as v1", exc)
                    v1_retry = True
                    self._set_connected(False)
                    self._publish()  # subscribers see the v2 refusal, not only the v1 outcome
                    continue
                if exc.kind == "auth":
                    delay = AUTH_BACKOFF_S
                elif exc.kind == "mount":
                    delay = MOUNT_BACKOFF_S
                else:
                    delay, backoff = self._jittered(backoff)
                log.warning("NTRIP %s (retry in %.0fs)", exc, delay)
            except _NETWORK_ERRORS as exc:
                self.status.last_error = f"{type(exc).__name__}: {exc}"
                since = self.status.since_mono
                if since is not None and time.monotonic() - since >= STABLE_S:
                    backoff = BACKOFF_MIN_S  # it was working: come straight back, not in 60 s
                delay, backoff = self._jittered(backoff)
                log.warning("NTRIP connection failed: %s (retry in %.1fs)", exc, delay)
            except Exception as exc:  # a bug (e.g. a driver that raises) must not end the task
                self.status.last_error = f"{type(exc).__name__}: {exc}"
                delay, backoff = self._jittered(backoff)
                log.exception("NTRIP session crashed (retry in %.1fs)", delay)
            if v1_retry and self._streamed:
                self._sticky_v1 = True
            v1_retry = False
            self._set_connected(False)
            self.status.reconnects += 1
            self.status.next_retry_s = delay
            self._publish()
            await self._pause(delay, stop)

    @staticmethod
    def _jittered(backoff: float) -> tuple[float, float]:
        """`(delay now, backoff for the next failure)`: +-20 % jitter, doubling up to 60 s."""
        return backoff * random.uniform(0.8, 1.2), min(backoff * 2, BACKOFF_MAX_S)

    @staticmethod
    async def _until_stop(coro: Coroutine[Any, Any, None], stop: asyncio.Event) -> None:
        """Run *coro* until it finishes or *stop* is set (then it is cancelled)."""
        work = asyncio.ensure_future(coro)
        stopping = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({work, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (work, stopping):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, stopping, return_exceptions=True)
        if not work.cancelled():
            work.result()  # re-raise whatever ended the session

    @staticmethod
    async def _pause(delay: float, stop: asyncio.Event) -> None:
        """Sleep *delay*, but wake at once on *stop*: a 60 s backoff must not delay shutdown."""
        sleeping = asyncio.ensure_future(_sleep(delay))
        stopping = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({sleeping, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeping.cancel()
            stopping.cancel()
            await asyncio.gather(sleeping, stopping, return_exceptions=True)

    def _set_connected(self, value: bool) -> None:
        if self.status.connected == value:
            return
        self.status.connected = value
        self.status.since_mono = time.monotonic() if value else None

    def _publish(self) -> None:
        # A copy: subscribers read it later, by which time this object has moved on.
        self.bus.publish("ntrip_client.status", dataclasses.replace(self.status))

    # ------------------------------------------------------------- one session
    def _request(self, v2: bool) -> bytes:
        c = self.config
        host = f"[{c.host}]" if ":" in c.host else c.host
        lines = [
            f"GET /{c.mountpoint} HTTP/1.{1 if v2 else 0}",
            f"Host: {host}:{c.port}",
            f"User-Agent: NTRIP mtrtk/{__version__}",
        ]
        if v2:
            lines += ["Ntrip-Version: Ntrip/2.0", "Connection: close"]
        if c.username is not None:
            token = base64.b64encode(f"{c.username}:{c.password or ''}".encode()).decode()
            lines.append(f"Authorization: Basic {token}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode()

    async def _read_head(self, reader: asyncio.StreamReader) -> tuple[str, dict[str, str]]:
        """Status line and lower-cased headers. A v1 `ICY 200 OK` may be followed straight by
        data with no blank line, so its headers are not read: the framer skips any text."""
        first = await _readline(reader)
        if not first:
            raise ConnectionError("caster closed the connection before answering")
        status_line = first.decode("latin-1").strip()
        headers: dict[str, str] = {}
        if status_line.startswith(("ICY ", "SOURCETABLE ")):
            return status_line, headers
        while True:
            line = await _readline(reader)
            if not line:
                raise ConnectionError("caster closed the connection mid-header")
            if line in (b"\r\n", b"\n"):
                return status_line, headers
            key, sep, value = line.decode("latin-1").partition(":")
            if sep:
                headers[key.strip().lower()] = value.strip()
            if len(headers) > MAX_HEADERS:
                raise _Response("other", "caster sent too many headers")

    def _classify(self, status_line: str, headers: dict[str, str]) -> tuple[bool, int]:
        """`(chunked, version)` for a stream; raises `_Response` for anything else."""
        c = self.config
        code = status_line.split(" ", 2)[1] if status_line.count(" ") >= 1 else ""
        if status_line.startswith("ICY ") and code == "200":
            return False, 1
        if (
            status_line.startswith("SOURCETABLE ")
            or code == "404"
            or (code == "200" and "sourcetable" in headers.get("content-type", "").lower())
        ):
            raise _Response("mount", f"mountpoint /{c.mountpoint} not found on {c.host}")
        if code == "401":
            raise _Response("auth", f"401 unauthorized for user {c.username!r}")
        if status_line.startswith("HTTP/1.") and code == "200":
            chunked = "chunked" in headers.get("transfer-encoding", "").lower()
            return chunked, 2 if "ntrip-version" in headers else 1
        raise _Response("other", f"unexpected response: {status_line[:80]!r}")

    async def _session(self, v2: bool) -> None:
        c = self.config
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(c.host, c.port), CONNECT_TIMEOUT_S
        )
        gga_task: asyncio.Task[None] | None = None
        try:
            writer.write(self._request(v2))
            await writer.drain()
            status_line, headers = await asyncio.wait_for(self._read_head(reader), HEADER_TIMEOUT_S)
            chunked, version = self._classify(status_line, headers)
            self._streamed = True
            self.status.version = version
            self.status.last_error = None
            self.status.next_retry_s = None
            self._set_connected(True)
            self._publish()
            log.info(
                "NTRIP connected to %s:%d/%s (v%d%s)",
                c.host,
                c.port,
                c.mountpoint,
                version,
                ", chunked" if chunked else "",
            )
            gga_task = asyncio.create_task(self._gga_loop(writer), name="ntrip-gga")
            await self._stream(reader, chunked)
        finally:
            if gga_task is not None:
                gga_task.cancel()
                await asyncio.gather(gga_task, return_exceptions=True)
            writer.close()
            await asyncio.gather(writer.wait_closed(), return_exceptions=True)

    async def _stream(self, reader: asyncio.StreamReader, chunked: bool) -> None:
        framer = Framer()
        crc_seen = 0
        last_status = time.monotonic()
        while True:
            try:
                data = await asyncio.wait_for(
                    decode_chunked(reader) if chunked else reader.read(READ_SIZE),
                    NO_DATA_TIMEOUT_S,
                )
            except TimeoutError as exc:
                raise ConnectionError(f"no data for {NO_DATA_TIMEOUT_S:.0f}s") from exc
            if not data:
                raise ConnectionError("caster closed the connection")
            self.status.bytes_received += len(data)
            for frame in framer.feed(data):
                if frame.proto is not Proto.RTCM3:
                    continue
                await self.driver.inject_rtcm(frame.raw)
                self.status.frames_injected += 1
                self.status.last_rtcm_mono = time.monotonic()
            # Cumulative across reconnects; the framer's own count restarts with each session.
            self.status.crc_dropped += framer.stats.checksum_errors - crc_seen
            crc_seen = framer.stats.checksum_errors
            now = time.monotonic()
            if now - last_status >= STATUS_INTERVAL_S:
                last_status = now
                self._publish()

    async def _gga_loop(self, writer: asyncio.StreamWriter) -> None:
        """Send the rover's position now and every `gga_interval_s` (VRS casters need it).

        An interval of 0 (or less) means "send no GGA", as in str2str: never a write loop with
        no pause between writes.
        """
        if self.gga_interval_s <= 0:
            return
        while True:
            try:
                gga = self.gga_provider()
                if gga is not None and not isinstance(gga, bytes):
                    raise TypeError(f"GGA provider returned {type(gga).__name__}, not bytes")
            except Exception:  # a broken position source (an OSError too) ends nothing
                log.exception("GGA provider failed")
                gga = None
            if gga:
                try:
                    writer.write(gga if gga.endswith(b"\r\n") else gga.rstrip() + b"\r\n")
                    await writer.drain()
                except OSError:  # the stream side notices the dead socket and reconnects
                    return
            await asyncio.sleep(self.gga_interval_s)
