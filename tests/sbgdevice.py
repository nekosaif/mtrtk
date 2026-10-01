"""A scripted sbgECom device for the SBG command and configuration tests.

`FakeEllipse` is a `ByteSource`: `InsController` opens it, reads its replies and writes commands to
it. Each written frame is answered the way an Ellipse answers (sbgECom 5.8, `src/commands/*.c`):

- a GET carries only a selector (empty for most commands, 3 bytes for OUTPUT_CONF, 2 for
  OUTPUT_CLASS_ENABLE, 1 for UART_CONF); the reply is the same command id with the stored payload,
  or an ACK with `INVALID_PARAMETER` when nothing is stored for that selector (an unsupported
  message on the real unit);
- a SET carries the full payload and is answered by an ACK (`ack_error[cmd]`, default 0); an
  accepted SET is stored, so the next GET reads it back, unless the command is in `sticky` (the
  unit ACKs but keeps its old value).

The selector-length table lives here, in the fake, and never in production code: the device is
what tells a GET from a SET.
"""

from __future__ import annotations

import asyncio
import struct

from mtrtk.rover.drivers.sbg.framer import SbgFramer, encode
from mtrtk.rover.drivers.sbg.ids import CLASS, CMD

GET_SELECTOR_LEN = {CMD["OUTPUT_CONF"]: 3, CMD["OUTPUT_CLASS_ENABLE"]: 2, CMD["UART_CONF"]: 1}
INVALID_PARAMETER = 9  # SBG_INVALID_PARAMETER


class FakeEllipse:
    name = "fake-ellipse"
    ends_at_eof = False

    def __init__(self) -> None:
        self.values: dict[tuple[int, bytes], bytes] = {}
        self.ack_error: dict[int, int] = {}
        self.sticky: set[int] = set()
        self.silent = False  # record writes, never answer
        self.sets: list[tuple[int, bytes]] = []
        self.gets: list[tuple[int, bytes]] = []
        self.written: list[bytes] = []
        self._out: asyncio.Queue[bytes] = asyncio.Queue()
        self._framer = SbgFramer()

    # ------------------------------------------------------------ scripting
    def put(self, cmd: int, payload: bytes) -> None:
        """Store *payload* as the reply to a GET of *cmd* (keyed by its echoed selector)."""
        self.values[(cmd, payload[: GET_SELECTOR_LEN.get(cmd, 0)])] = payload

    def emit(self, frame: bytes) -> None:
        """Send an unsolicited frame (a log) to the host."""
        self._out.put_nowait(frame)

    def set_payloads(self, cmd: int) -> list[bytes]:
        return [p for c, p in self.sets if c == cmd]

    # ------------------------------------------------------------ ByteSource
    async def open(self) -> None:
        # A new connection starts with an empty line: nothing left from the last one (its
        # close sentinel included), so a test can reconnect the same device.
        self._out = asyncio.Queue()
        self._framer = SbgFramer()

    async def read(self) -> bytes:
        return await self._out.get()

    async def close(self) -> None:
        self._out.put_nowait(b"")

    async def write(self, data: bytes) -> None:
        self.written.append(data)
        if self.silent:
            return
        for frame in self._framer.feed(data):
            cmd, payload = frame.raw[2], frame.payload
            assert frame.raw[3] == CLASS["CMD_0"], "commands go out in class CMD_0"
            if len(payload) == GET_SELECTOR_LEN.get(cmd, 0):
                self._answer_get(cmd, payload)
            else:
                self._answer_set(cmd, payload)

    def _answer_get(self, cmd: int, selector: bytes) -> None:
        self.gets.append((cmd, selector))
        reply = self.values.get((cmd, selector))
        if reply is None:
            self._ack(cmd, INVALID_PARAMETER)
        else:
            self._out.put_nowait(encode(CLASS["CMD_0"], cmd, reply))

    def _answer_set(self, cmd: int, payload: bytes) -> None:
        self.sets.append((cmd, payload))
        code = self.ack_error.get(cmd, 0)
        if code == 0 and cmd not in self.sticky and cmd != CMD["SETTINGS_ACTION"]:
            self.put(cmd, payload)
        self._ack(cmd, code)

    def _ack(self, cmd: int, code: int) -> None:
        ack = struct.pack("<BBH", cmd, CLASS["CMD_0"], code)
        self._out.put_nowait(encode(CLASS["CMD_0"], CMD["ACK"], ack))
