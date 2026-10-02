"""Byte sources: a live serial receiver, or a recorded file replayed at receiver or host pace."""

from __future__ import annotations

import asyncio
import contextlib
import glob
import logging
import os
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import BinaryIO, Protocol

from serial import PortNotOpenError, SerialException
from serial.tools import list_ports
from serial_asyncio_fast import open_serial_connection

from mtrtk.core.crc import ubx_checksum
from mtrtk.core.frames import Frame, Framer, Proto

log = logging.getLogger(__name__)

UBLOX_VID = 0x1546
# What `MTRTK_SOURCE=auto` reports while no u-blox receiver is on USB: the event detail and the
# reason every open of `NoReceiverSource` fails with.
NO_UBLOX_RECEIVER = "no u-blox receiver found; set MTRTK_SOURCE to the serial device"
# The `receiver.disconnected` reason for a finite source that reached its end: the daemon closes
# a replay's last inferred epoch on it, and alerts treat it as the expected end of a run. Both
# controllers (u-blox and INS) publish it, so it is spelled once, here.
SOURCE_ENDED = "source ended"
NAV_PVT = (0x01, 0x07)
NAV_EOE = (0x01, 0x61)
REPLAY_PACES = ("itow", "host")
HOST_CHUNK = 1024  # bytes per read() of a host-paced replay
REPLAY_READ = 64 << 10  # bytes per file read of a replay: what bounds its memory
MAX_REPLAY_CHUNK = 256 << 10  # most bytes a UBX replay's read() returns (a marker-less run)
# A replay's reads in a row that frame nothing before it yields to the event loop: 1 MiB of a
# wrong or corrupt `file:` (random bytes, a gzip of a log) per step, not the whole file in one.
FRAMELESS_READS_PER_YIELD = 16
# How far into a replay open() looks for a NAV-EOE, the first epoch's marker: many epochs of
# any log, and read synchronously on the event loop, so never the whole of a GB concatenation.
REPLAY_HEAD_SCAN = 64 << 10
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


class NoReceiverSource:
    """What `MTRTK_SOURCE=auto` opens while the USB scan finds no u-blox receiver at all.

    Its `open()` fails with an `OSError`, exactly as a configured serial path that does not
    exist does: the controller reports the receiver down, backs off and asks the source
    factory again, which scans again. The daemon keeps serving the UI, the API and the caster
    meanwhile, rather than exiting at startup (and restart-looping under Docker) with nothing
    reachable to say why.
    """

    name = "auto (USB scan)"
    ends_at_eof = False

    async def open(self) -> None:
        raise OSError(NO_UBLOX_RECEIVER)

    async def read(self) -> bytes:
        return b""

    async def write(self, data: bytes) -> None:
        raise ConnectionError(NO_UBLOX_RECEIVER)

    async def close(self) -> None:
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

    The file is streamed, `REPLAY_READ` bytes at a time, never read whole: a capture of any size
    (hours of base logs run to tens of MB) replays every frame in bounded memory. Handing it to
    the framer in one piece kept only its last MiB, the framer's own buffer cap.
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
        # Injected so a test can watch the pacing without patching `asyncio.sleep` for the
        # whole process - including for the event loop the test itself runs on.
        self._sleep = sleep
        self.name = f"file:{self.path.name}"
        self._file: BinaryIO | None = None
        self._framer = Framer()
        self._pending: deque[Frame] = deque()  # framed from the file, not yet replayed
        self._eof = False  # the file's bytes are all in the framer (only `_pending` is left)
        # The file's marker as open() found it: NAV-EOE if its head holds one. It only sets
        # where the first pass starts; from there each epoch picks its own (see `read`).
        self._marker = NAV_PVT
        self._eoe_since_pvt = False  # a NAV-EOE has come since the last NAV-PVT
        self._last_itow: int | None = None
        self._frames = 0  # replayed in this pass

    async def open(self) -> None:
        await self.close()
        self._file = self.path.open("rb")
        size = os.fstat(self._file.fileno()).st_size
        if self.pace == "host":
            log.info("replaying %s: %d bytes, paced on host time", self.path, size)
            return
        has_eoe = _has_nav_eoe(self._file)
        self._marker = NAV_EOE if has_eoe else NAV_PVT
        self._rewind()
        log.info(
            "replaying %s: %d bytes, first marker %s",
            self.path,
            size,
            "NAV-EOE" if has_eoe else "NAV-PVT",
        )

    def _rewind(self) -> None:
        """Back to the first byte with a fresh framer: a cut last frame of the previous pass
        is dropped, never glued onto the first bytes of the next."""
        assert self._file is not None
        self._file.seek(0)
        self._framer = Framer()
        self._pending.clear()
        self._eof = False
        self._eoe_since_pvt = self._marker == NAV_EOE
        self._last_itow = None
        self._frames = 0

    async def _next_frame(self) -> Frame | None:
        """The next frame of this pass, or None at its end (a trailing partial frame dropped).

        A long stretch with no frame yields every `FRAMELESS_READS_PER_YIELD` reads, so a file
        that is not a log at all is scanned in steps the UI and API can run between.
        """
        frameless = 0
        while not self._pending:
            if self._eof or self._file is None:
                return None
            data = self._file.read(REPLAY_READ)
            if not data:
                self._eof = True
                log.info("replayed %s: %d frames", self.path, self._frames)
                return None
            self._pending.extend(self._framer.feed(data))
            frameless += 1
            if not self._pending and frameless >= FRAMELESS_READS_PER_YIELD:
                frameless = 0
                await self._sleep(0)
        self._frames += 1
        return self._pending.popleft()

    async def read(self) -> bytes:
        if self.pace == "host":
            return await self._read_host()
        chunk = bytearray()
        rewound = False
        marked = False  # the chunk ends on its epoch's marker (and was paced there)
        while True:
            frame = await self._next_frame()
            if frame is None:
                if chunk or not self.loop or rewound or self._file is None:
                    break  # the file's tail after its last marker, or the end
                self._rewind()
                rewound = True  # a file with no frame at all ends rather than spins
                continue
            if chunk and len(chunk) + len(frame.raw) > MAX_REPLAY_CHUNK:
                # A stretch with no marker: hand it on in pieces, unpaced; this frame next.
                self._pending.appendleft(frame)
                self._frames -= 1
                break
            chunk += frame.raw
            if frame.proto is not Proto.UBX:
                continue
            if frame.ubx_class_id == NAV_EOE:
                self._eoe_since_pvt = True
                await self._pace(_itow(frame.raw))
                marked = True
                break
            if frame.ubx_class_id == NAV_PVT:
                # The marker is chosen per epoch, from the stream: a NAV-EOE closes an epoch
                # that logs one, and a NAV-PVT one that does not. Hours logged before and
                # after NAV-EOE joined LOG_MESSAGES, joined with cat, are paced in both parts.
                # Pacing here too (an epoch that has a NAV-EOE then sleeps at its NAV-PVT and
                # not again at the NAV-EOE) keeps the clock when the stream changes sides.
                await self._pace(_itow(frame.raw))
                ends_epoch = not self._eoe_since_pvt
                self._eoe_since_pvt = False
                if ends_epoch:
                    marked = True
                    break
        if chunk and (self.speed <= 0 or not marked):
            # Unpaced replay has no other suspension point, so without this yield the reader
            # drains the whole file in a single event-loop step: consumers would see nothing
            # until EOF and every status line would show the same final snapshot. The same
            # holds at any speed for a chunk no marker ended (a capped run, the file's tail):
            # a long run without one would otherwise arrive in one step, all at once.
            await self._sleep(0)
        return bytes(chunk)

    async def _read_host(self) -> bytes:
        if self._file is None:
            return b""
        chunk = self._file.read(HOST_CHUNK)
        if not chunk:
            if not self.loop:
                return b""
            self._file.seek(0)
            chunk = self._file.read(HOST_CHUNK)
            if not chunk:
                return b""  # an empty file
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
        file, self._file = self._file, None
        self._pending.clear()
        self._framer = Framer()
        if file is not None:
            file.close()


def _itow(raw: bytes) -> int:
    """The iTOW (ms) that opens a NAV-PVT's or a NAV-EOE's payload."""
    return int.from_bytes(raw[6:10], "little")


_EOE_HEAD = bytes((0xB5, 0x62, *NAV_EOE, 4, 0))  # NAV-EOE: a 4-byte iTOW payload


def _has_nav_eoe(file: BinaryIO) -> bool:
    """Whether the file's first `REPLAY_HEAD_SCAN` bytes hold a checksum-valid NAV-EOE, found
    by a byte search in `REPLAY_READ` pieces (far faster than framing), the file left at its
    start. Only the head: the answer seeds the first epoch alone, and a later NAV-EOE (an old
    log joined before a new one) says nothing about it."""
    size = len(_EOE_HEAD) + 4 + 2
    tail = b""
    left = REPLAY_HEAD_SCAN
    try:
        while left > 0 and (data := file.read(min(REPLAY_READ, left))):
            left -= len(data)
            window = tail + data
            at = window.find(_EOE_HEAD)
            while 0 <= at <= len(window) - size:
                frame = window[at : at + size]
                if frame[-2:] == ubx_checksum(frame[2:-2]):
                    return True
                at = window.find(_EOE_HEAD, at + 1)
            # keep enough to finish a frame cut by this read (and a header cut by it)
            tail = window[-(size - 1) :]
        return False
    finally:
        file.seek(0)
