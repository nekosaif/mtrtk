"""Byte sources: a live serial receiver, or a recorded file replayed at receiver pace."""

from __future__ import annotations

import asyncio
import glob
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from serial.tools import list_ports
from serial_asyncio_fast import open_serial_connection

from mtrtk.core.frames import Frame, Framer, Proto

log = logging.getLogger(__name__)

UBLOX_VID = 0x1546
NAV_PVT = (0x01, 0x07)
NAV_EOE = (0x01, 0x61)


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
    ends_at_eof = False

    def __init__(self, port: str, baud: int = 115200, read_size: int = 4096) -> None:
        self.port = port
        self.baud = baud
        self.read_size = read_size
        self.name = f"serial:{port}"
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def open(self) -> None:
        self._reader, self._writer = await open_serial_connection(
            url=self.port, baudrate=self.baud, limit=1 << 16
        )
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
        if self._writer is not None:
            self._writer.close()
        self._reader = None
        self._writer = None


class FileReplaySource:
    """Replays a recorded stream one epoch per read(), pacing on receiver time (iTOW)."""

    ends_at_eof = True

    def __init__(
        self,
        path: str | Path,
        speed: float = 1.0,
        loop: bool = False,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.path = Path(path)
        self.speed = speed
        self.loop = loop
        # Injected so a test can watch the pacing without patching `asyncio.sleep` for the
        # whole process - including for the event loop the test itself runs on.
        self._sleep = sleep
        self.name = f"file:{self.path.name}"
        self._frames: list[Frame] = []
        self._marker = NAV_PVT
        self._idx = 0
        self._last_itow: int | None = None

    async def open(self) -> None:
        self._frames = Framer().feed(self.path.read_bytes())
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
