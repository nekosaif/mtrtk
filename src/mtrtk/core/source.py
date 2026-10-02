"""Byte sources: a live serial receiver, or a recorded file replayed at receiver or host pace."""

from __future__ import annotations

import asyncio
import contextlib
import glob
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from serial import PortNotOpenError, SerialException
from serial.tools import list_ports
from serial_asyncio_fast import open_serial_connection

from mtrtk.core.frames import Frame, Framer, Proto

log = logging.getLogger(__name__)

UBLOX_VID = 0x1546
NAV_PVT = (0x01, 0x07)
NAV_EOE = (0x01, 0x61)
REPLAY_PACES = ("itow", "host")
HOST_CHUNK = 1024  # bytes per read() of a host-paced replay
# Bytes per Framer.feed() when framing a replay file: the framer keeps only its last
# `max_buffer` (1 MiB) of one feed, so a whole hourly log (~6.5 MB) fed at once loses its head.
FRAME_CHUNK = 64 * 1024
CLOSE_TIMEOUT_S = 2.0  # how long close() waits for the serial transport to let go


class ByteSource(Protocol):
    name: str
    # True for a finite recording, False for a live device. It decides what an empty read
    # means: the end of the stream, or a link that has gone away and must be reconnected.
    ends_at_eof: bool

    async def open(self) -> None: ...

    async def read(self) -> bytes:
        """Return the next chunk; b"" means the source ended / disconnected."""
        ...

    async def write(self, data: bytes) -> None: ...

    async def close(self) -> None: ...


def find_ublox_port() -> str | None:
    """Stable by-id symlink first, then any port whose USB VID is u-blox."""
    by_id = sorted(glob.glob("/dev/serial/by-id/*u-blox*"))
    if by_id:
        return by_id[0]
    for port in list_ports.comports():
        if port.vid == UBLOX_VID:
            return str(port.device)
    return None


class SerialSource:
    """A serial device. *exclusive* takes pyserial's advisory lock (flock) on the port, so a
    second process that also asks for it (`mtrtk ins` beside the daemon) is refused at once
    instead of splitting the byte stream, and the replies in it, with the first."""

    ends_at_eof = False

    def __init__(
        self, port: str, baud: int = 115200, read_size: int = 4096, *, exclusive: bool = False
    ) -> None:
        self.port = port
        self.baud = baud
        self.read_size = read_size
        self.exclusive = exclusive
        self.name = f"serial:{port}"
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def open(self) -> None:
        try:
            if self.exclusive:
                self._reader, self._writer = await open_serial_connection(
                    url=self.port, baudrate=self.baud, limit=1 << 16, exclusive=True
                )
            else:
                self._reader, self._writer = await open_serial_connection(
                    url=self.port, baudrate=self.baud, limit=1 << 16
                )
        except SerialException as exc:
            if self.exclusive and "exclusively lock" in str(exc):
                raise OSError(
                    f"{self.port} is in use by another process (is the mtrtk daemon running?)"
                ) from exc
            raise
        _write_without_spinning(self._writer)
        log.info("opened %s @ %d", self.port, self.baud)

    async def read(self) -> bytes:
        if self._reader is None:
            return b""
        return await self._reader.read(self.read_size)

    async def write(self, data: bytes) -> None:
        if self._writer is None:
            raise ConnectionError("serial port not open")
        self._writer.write(data)
        await self._writer.drain()

    async def close(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            writer.close()
            # The fd (and with it an exclusive lock) is released once the transport has
            # closed: a reconnect that reopened before then would find its own port busy.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), CLOSE_TIMEOUT_S)


def _write_without_spinning(writer: asyncio.StreamWriter | None) -> None:
    """Give the port's transport a write that cannot freeze the event loop.

    serial_asyncio_fast sets `write_timeout=0`, and pyserial's non-blocking `write` then loops
    on EAGAIN: a write that arrives while the kernel queue is full (a stalled tunnel, an adapter
    whose far end stopped reading, RTCM-sized writes landing exactly at the brim) spins for as
    long as the port stays full, with nothing else running. Writing the fd directly raises
    BlockingIOError there instead, and the transport buffers the bytes and waits for the port.
    """
    port = getattr(writer.transport, "serial", None) if writer is not None else None
    fd = getattr(port, "fd", None)
    if port is None or not isinstance(fd, int):
        return  # not a posix port (a socket:// or rfc2217:// URL): no fd to write

    def write(data: bytes | bytearray | memoryview) -> int:
        if not port.is_open:
            raise PortNotOpenError()
        return os.write(fd, data)

    port.write = write


class FileReplaySource:
    """Replays a recorded stream.

    `pace="itow"` (u-blox): one epoch per read(), paced on receiver time (NAV-PVT/NAV-EOE iTOW).
    `pace="host"`, the fallback for a stream with no UBX iTOW (an SBG or VectorNav capture): the
    file's bytes as they are, `HOST_CHUNK` per read(), each after the time it takes on an 8N1
    serial line at *baud* (host time). The bytes are not framed here, so the vendor's own framer
    downstream sees exactly what the unit sent.
    """

    ends_at_eof = True

    def __init__(
        self,
        path: str | Path,
        speed: float = 1.0,
        loop: bool = False,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        pace: str = "itow",
        baud: int = 115200,
    ) -> None:
        if pace not in REPLAY_PACES:
            raise ValueError(f"replay pace must be one of {REPLAY_PACES}, got {pace!r}")
        self.path = Path(path)
        self.speed = speed
        self.loop = loop
        self.pace = pace
        self.baud = baud
        self._data = b""
        self._pos = 0
        # Injected so a test can watch the pacing without patching `asyncio.sleep` for the
        # whole process - including for the event loop the test itself runs on.
        self._sleep = sleep
        self.name = f"file:{self.path.name}"
        self._frames: list[Frame] = []
        self._marker = NAV_PVT
        self._idx = 0
        self._last_itow: int | None = None

    async def open(self) -> None:
        if self.pace == "host":
            self._data, self._pos = self.path.read_bytes(), 0
            log.info("replaying %s: %d bytes, paced on host time", self.path, len(self._data))
            return
        data, framer = self.path.read_bytes(), Framer()
        self._frames = []
        for start in range(0, len(data), FRAME_CHUNK):
            self._frames += framer.feed(data[start : start + FRAME_CHUNK])
        has_eoe = any(f.proto is Proto.UBX and f.ubx_class_id == NAV_EOE for f in self._frames)
        self._marker = NAV_EOE if has_eoe else NAV_PVT
        self._idx = 0
        self._last_itow = None
        log.info(
            "replaying %s: %d frames, marker %s",
            self.path,
            len(self._frames),
            "NAV-EOE" if has_eoe else "NAV-PVT",
        )

    async def read(self) -> bytes:
        if self.pace == "host":
            return await self._read_host()
        if self._idx >= len(self._frames):
            if not self.loop:
                return b""
            self._idx = 0
            self._last_itow = None
        chunk = bytearray()
        while self._idx < len(self._frames):
            frame = self._frames[self._idx]
            self._idx += 1
            chunk += frame.raw
            if frame.proto is Proto.UBX and frame.ubx_class_id == self._marker:
                await self._pace(int.from_bytes(frame.raw[6:10], "little"))
                break
        if self.speed <= 0:
            # Unpaced replay has no other suspension point, so without this yield the reader
            # drains the whole file in a single event-loop step: consumers would see nothing
            # until EOF and every status line would show the same final snapshot.
            await self._sleep(0)
        return bytes(chunk)

    async def _read_host(self) -> bytes:
        if self._pos >= len(self._data):
            if not self.loop or not self._data:
                return b""
            self._pos = 0
        chunk = self._data[self._pos : self._pos + HOST_CHUNK]
        self._pos += len(chunk)
        # 10 bits a byte on an 8N1 line; speed 0 still yields (see `read`).
        await self._sleep(len(chunk) * 10 / self.baud / self.speed if self.speed > 0 else 0)
        return chunk

    async def _pace(self, itow: int) -> None:
        if self._last_itow is not None and self.speed > 0:
            delta_s = (itow - self._last_itow) / 1000.0
            if 0 < delta_s < 60:
                await self._sleep(delta_s / self.speed)
        self._last_itow = itow

    async def write(self, data: bytes) -> None:
        log.debug("replay source ignores %d bytes written", len(data))

    async def close(self) -> None:
        self._frames = []
        self._data = b""
