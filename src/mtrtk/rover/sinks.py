"""Where NMEA/JSON bytes go: a TCP server, UDP targets, a serial port or a pseudo-terminal.

Every sink is best effort and never blocks its caller: a consumer that stops reading is dropped
(TCP) or has bytes dropped for it (pty, serial), so one stuck reader can never hold up the
others or the epoch loop that feeds them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import tty
from typing import Protocol

from serial_asyncio_fast import open_serial_connection

log = logging.getLogger(__name__)

TCP_BACKLOG_LIMIT = 64 * 1024  # bytes queued for one TCP client before it is dropped
SERIAL_BACKLOG_LIMIT = 16 * 1024  # bytes queued for a serial port / pty before new data is dropped
UDP_RESOLVE_RETRY_S = 30.0


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

    Host names are resolved once at start (a blocking lookup per datagram would stall the
    loop); a name that does not resolve yet - a container that is not up - is retried every
    `UDP_RESOLVE_RETRY_S` instead of being given up on.
    """

    def __init__(self, targets: list[tuple[str, int]]) -> None:
        self.targets = list(targets)
        self._resolved: list[tuple[str, int]] = []
        self.unresolved: list[str] = []
        self._pending: list[tuple[str, int]] = []
        self._next_resolve = 0.0
        self._transport: asyncio.DatagramTransport | None = None

    @property
    def ready(self) -> bool:
        return self._transport is not None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, family=socket.AF_INET, allow_broadcast=True
        )
        self._resolved = []
        await self._resolve(self.targets)

    async def _resolve(self, targets: list[tuple[str, int]]) -> None:
        loop = asyncio.get_running_loop()
        pending: list[tuple[str, int]] = []
        for host, port in targets:
            try:
                infos = await loop.getaddrinfo(
                    host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM
                )
            except (OSError, UnicodeError) as exc:
                log.warning("UDP target %s:%d does not resolve (%s); will retry", host, port, exc)
                pending.append((host, port))
                continue
            addr = infos[0][4]
            self._resolved.append((str(addr[0]), int(addr[1])))
        self._pending = pending
        self.unresolved = [host for host, _ in pending]
        self._next_resolve = time.monotonic() + UDP_RESOLVE_RETRY_S

    async def write(self, data: bytes) -> None:
        if self._transport is None:
            return
        if self._pending and time.monotonic() >= self._next_resolve:
            await self._resolve(self._pending)
        for target in self._resolved:
            try:
                self._transport.sendto(data, target)
            except OSError as exc:  # unreachable network; the next epoch tries again
                log.debug("UDP send to %s:%d failed: %s", *target, exc)

    async def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None


class SerialSink:
    """`path == "pty"` creates a pseudo-terminal (open `.slave_path` with any NMEA consumer);
    any other path is a real serial port opened at `baud`."""

    def __init__(self, path: str, baud: int = 115200) -> None:
        self.path, self.baud = path, baud
        self.slave_path: str | None = None
        self.dropped_bytes = 0
        self._master_fd: int | None = None
        self._slave_fd: int | None = None
        self._pending = b""
        self._writer: asyncio.StreamWriter | None = None

    async def start(self) -> None:
        if self.path == "pty":
            master, slave = os.openpty()
            # Raw: no echo back into the master (nobody reads it) and no CR -> LF translation,
            # so readers get the sentences byte for byte, CRLF included.
            tty.setraw(slave)
            os.set_blocking(master, False)
            # The slave stays open here so the pty lives even when no consumer has it open.
            self._master_fd, self._slave_fd = master, slave
            self._pending = b""
            self.slave_path = os.ttyname(slave)
            log.info("NMEA pseudo-terminal at %s", self.slave_path)
        else:
            _, self._writer = await open_serial_connection(url=self.path, baudrate=self.baud)
            log.info("NMEA serial output on %s @ %d", self.path, self.baud)

    async def write(self, data: bytes) -> None:
        if self._master_fd is not None:
            self._write_pty(data)
        elif self._writer is not None:
            if self._writer.transport.get_write_buffer_size() > SERIAL_BACKLOG_LIMIT:
                self.dropped_bytes += len(data)  # the port is slower than the sentences
                return
            self._writer.write(data)

    def _write_pty(self, data: bytes) -> None:
        assert self._master_fd is not None
        # A short write leaves part of a sentence pending; it is finished before anything new
        # goes out, so a reader never sees two sentences spliced together.
        if len(self._pending) + len(data) > SERIAL_BACKLOG_LIMIT:
            self.dropped_bytes += len(data)
        else:
            self._pending += data
        try:
            written = os.write(self._master_fd, self._pending)
        except BlockingIOError:
            return  # nobody is reading the pty
        self._pending = self._pending[written:]

    async def close(self) -> None:
        for fd in (self._master_fd, self._slave_fd):
            if fd is not None:
                os.close(fd)
        self._master_fd = self._slave_fd = None
        self.slave_path = None
        self._pending = b""
        if self._writer is not None:
            writer, self._writer = self._writer, None
            writer.close()
