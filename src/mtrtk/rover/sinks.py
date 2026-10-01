"""Where NMEA/JSON bytes go: a TCP server, UDP targets, a serial port or a pseudo-terminal.

Every sink is best effort and never blocks its caller: a consumer that stops reading is dropped
(TCP) or has bytes dropped for it (pty, serial), so one stuck reader can never hold up the
others or the epoch loop that feeds them.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import socket
import struct
import termios
import tty
from typing import Protocol

import serial

log = logging.getLogger(__name__)

TCP_BACKLOG_LIMIT = 64 * 1024  # bytes queued for one TCP client before it is dropped
SERIAL_BACKLOG_LIMIT = 16 * 1024  # bytes held for a serial port / pty beyond the kernel's queue
UDP_RESOLVE_RETRY_S = 30.0
UDP_RESOLVE_TIMEOUT_S = 1.0  # one lookup at start; a resolver out of reach must not stall it
UDP_MAX_PAYLOAD = 1400  # bytes per datagram: under a 1500-byte MTU, so never IP-fragmented


class NmeaSink(Protocol):
    async def start(self) -> None: ...

    async def write(self, data: bytes) -> None: ...

    async def close(self) -> None: ...


class TcpBroadcastSink:
    """A TCP server that sends every write to every connected client (gpsd, QGIS, OpenCPN)."""

    def __init__(self, host: str, port: int) -> None:
        self.host, self._port = host, port
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self.dropped_clients = 0

    @property
    def port(self) -> int:
        if self._server is not None and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self._port

    @property
    def client_count(self) -> int:
        return len(self._writers)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, self.host, self._port)
        log.info("NMEA TCP server on %s:%d", self.host, self.port)

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        self._writers.add(writer)
        log.info("NMEA TCP client %s connected", peer)
        try:
            while await reader.read(1024):  # input is ignored; reading only detects the close
                pass
        except (ConnectionError, OSError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()
            log.info("NMEA TCP client %s disconnected", peer)

    async def write(self, data: bytes) -> None:
        for w in list(self._writers):
            if w.is_closing():
                self._writers.discard(w)
                continue
            if w.transport.get_write_buffer_size() > TCP_BACKLOG_LIMIT:
                log.warning(
                    "dropping NMEA TCP client %s: not reading", w.get_extra_info("peername")
                )
                self._writers.discard(w)
                self.dropped_clients += 1
                w.transport.abort()
                continue
            try:
                w.write(data)
            except (ConnectionError, OSError, RuntimeError):
                self._writers.discard(w)
                w.transport.abort()

    async def close(self) -> None:
        for w in list(self._writers):
            w.transport.abort()
        self._writers.clear()
        if self._server is not None:
            server, self._server = self._server, None
            server.close()
            await server.wait_closed()


class UdpSink:
    """Datagrams to fixed `(host, port)` targets; broadcast addresses work too.

    Host names are resolved at start, each lookup bounded by `UDP_RESOLVE_TIMEOUT_S` (a lookup
    per datagram would stall the epoch loop). A name that does not resolve yet - a container
    that is not up, a resolver out of reach in the field - is retried every
    `UDP_RESOLVE_RETRY_S` by a background task, so `write()` only ever sends.

    A payload longer than `UDP_MAX_PAYLOAD` is split on line ends into several datagrams, so
    an epoch with a long GSA/GSV block is never IP-fragmented (one lost fragment would lose
    the whole epoch, GGA included).
    """

    def __init__(self, targets: list[tuple[str, int]]) -> None:
        self.targets = list(targets)
        self._resolved: list[tuple[str, int]] = []
        self.unresolved: list[str] = []
        self._transport: asyncio.DatagramTransport | None = None
        self._resolver: asyncio.Task[None] | None = None

    @property
    def ready(self) -> bool:
        return self._transport is not None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, family=socket.AF_INET, allow_broadcast=True
        )
        self._resolved = []
        pending = await self._resolve(self.targets, first=True)
        if pending:
            self._resolver = asyncio.create_task(self._retry(pending), name="udp-resolve")

    async def _lookup(self, host: str, port: int, first: bool) -> tuple[str, int] | None:
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM),
                UDP_RESOLVE_TIMEOUT_S,
            )
        except (OSError, UnicodeError, TimeoutError) as exc:
            log.log(
                logging.WARNING if first else logging.DEBUG,
                "UDP target %s:%d does not resolve (%s); will retry",
                host,
                port,
                exc or "timed out",
            )
            return None
        addr = infos[0][4]
        return str(addr[0]), int(addr[1])

    async def _resolve(self, targets: list[tuple[str, int]], first: bool) -> list[tuple[str, int]]:
        """Resolve `targets` concurrently; returns the ones that did not resolve."""
        found = await asyncio.gather(*(self._lookup(h, p, first) for h, p in targets))
        pending = [t for t, addr in zip(targets, found, strict=True) if addr is None]
        # A new list, not an in-place append: write() may be iterating the old one.
        self._resolved = [*self._resolved, *(addr for addr in found if addr is not None)]
        self.unresolved = [host for host, _ in pending]
        return pending

    async def _retry(self, pending: list[tuple[str, int]]) -> None:
        while pending:
            await asyncio.sleep(UDP_RESOLVE_RETRY_S)
            pending = await self._resolve(pending, first=False)
            if not pending:
                log.info("UDP targets all resolved: %s", self._resolved)

    async def write(self, data: bytes) -> None:
        if self._transport is None:
            return
        for datagram in _datagrams(data):
            for target in self._resolved:
                try:
                    self._transport.sendto(datagram, target)
                except OSError as exc:  # unreachable network; the next epoch tries again
                    log.debug("UDP send to %s:%d failed: %s", *target, exc)

    async def close(self) -> None:
        if self._resolver is not None:
            task, self._resolver = self._resolver, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._transport is not None:
            self._transport.close()
            self._transport = None


def _datagrams(data: bytes) -> list[bytes]:
    """`data` cut on line ends into chunks of at most `UDP_MAX_PAYLOAD` (a longer line, or a
    payload without line ends such as a JSON document, goes whole)."""
    if len(data) <= UDP_MAX_PAYLOAD:
        return [data]
    out: list[bytes] = []
    chunk = b""
    start = 0
    while start < len(data):
        end = data.find(b"\n", start)
        end = len(data) if end < 0 else end + 1
        line, start = data[start:end], end
        if chunk and len(chunk) + len(line) > UDP_MAX_PAYLOAD:
            out.append(chunk)
            chunk = b""
        chunk += line
    if chunk:
        out.append(chunk)
    return out


class SerialSink:
    """`path == "pty"` creates a pseudo-terminal (open `.slave_path` with any NMEA consumer);
    any other path is a real serial port opened at `baud`.

    Both are written the same way: non-blocking `os.write` on a file descriptor, with a short
    write's remainder finished before anything new goes out (a reader never sees two sentences
    spliced together). When the kernel queue is full - nobody has the pty open, or the port is
    slower than the sentences - the queued bytes are discarded and the newest kept: a reader
    that attaches late, or catches up, wants the current epoch, not a backlog of old ones.

    pyserial only opens and configures a real port; its own `write()` with `write_timeout=0`
    loops forever on EAGAIN, which would freeze the event loop on a full port. A write error
    on a real port (the adapter was unplugged) raises `ConnectionError`, so the publisher sets
    the sink aside and reopens it later.
    """

    def __init__(self, path: str, baud: int = 115200) -> None:
        self.path, self.baud = path, baud
        self.slave_path: str | None = None
        self.dropped_bytes = 0
        self._fd: int | None = None  # what is written: the pty master or the serial port
        self._slave_fd: int | None = None
        self._port: serial.Serial | None = None
        self._pending = b""

    async def start(self) -> None:
        self._pending = b""
        if self.path == "pty":
            master, slave = os.openpty()
            # Raw: no echo back into the master (nobody reads it) and no CR -> LF translation,
            # so readers get the sentences byte for byte, CRLF included.
            tty.setraw(slave)
            os.set_blocking(master, False)
            # The slave stays open here so the pty lives even when no consumer has it open.
            self._fd, self._slave_fd = master, slave
            self.slave_path = os.ttyname(slave)
            log.info("NMEA pseudo-terminal at %s", self.slave_path)
        else:
            # SerialException is an OSError: a missing device fails start() as one.
            port = serial.Serial(self.path, self.baud, timeout=0, write_timeout=0)
            os.set_blocking(port.fileno(), False)
            self._port, self._fd = port, port.fileno()
            log.info("NMEA serial output on %s @ %d", self.path, self.baud)

    async def write(self, data: bytes) -> None:
        if self._fd is None:
            return
        self._pending += data
        try:
            try:
                self._send()
            except BlockingIOError:
                self._discard_queued()
                with contextlib.suppress(BlockingIOError):
                    self._send()
        except OSError as exc:
            raise ConnectionError(f"NMEA serial output {self.path}: {exc}") from exc
        if len(self._pending) > SERIAL_BACKLOG_LIMIT:  # one epoch larger than the kernel queue
            self._discard_queued()
            cut = self._pending.find(b"$", len(self._pending) - SERIAL_BACKLOG_LIMIT)
            cut = len(self._pending) if cut < 0 else cut
            self.dropped_bytes += cut
            self._pending = self._pending[cut:]

    def _send(self) -> None:
        assert self._fd is not None
        while self._pending:
            written = os.write(self._fd, self._pending)
            if written <= 0:
                return
            self._pending = self._pending[written:]

    def _discard_queued(self) -> None:
        """Drop what the kernel still holds (the oldest bytes), and the rest of a sentence
        whose start went with it."""
        if self._slave_fd is not None:  # pty: the bytes wait in the slave's input queue
            fd, request, queue = self._slave_fd, termios.FIONREAD, termios.TCIFLUSH
        else:  # serial port: in its output queue
            assert self._fd is not None
            fd, request, queue = self._fd, termios.TIOCOUTQ, termios.TCOFLUSH
        with contextlib.suppress(OSError):
            self.dropped_bytes += struct.unpack("i", fcntl.ioctl(fd, request, b"\0" * 4))[0]
        with contextlib.suppress(OSError, termios.error):
            termios.tcflush(fd, queue)
        if self._pending and not self._pending.startswith(b"$"):
            nl = self._pending.find(b"\n")
            cut = len(self._pending) if nl < 0 else nl + 1
            self.dropped_bytes += cut
            self._pending = self._pending[cut:]

    async def close(self) -> None:
        if self._port is not None:
            port, self._port = self._port, None
            with contextlib.suppress(OSError):
                port.close()
        else:
            for fd in (self._fd, self._slave_fd):
                if fd is not None:
                    os.close(fd)
        self._fd = self._slave_fd = None
        self.slave_path = None
        self._pending = b""
