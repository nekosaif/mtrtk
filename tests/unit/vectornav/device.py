"""A scripted VN-200 for the register and configuration tests: answers `$VN...` commands written
to it like the unit does, from a register table the test can preload and inspect."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from mtrtk.core.bus import Bus
from mtrtk.rover.drivers.ins_common import InsController
from mtrtk.rover.drivers.vectornav.checksum import finalize_ascii, verify_ascii
from mtrtk.rover.drivers.vectornav.framer import VnFramer

from .helpers import TIME, binary, time_group_payload

DEFAULT_REGS: dict[int, list[str]] = {
    1: ["VN-200T-CR"],
    2: ["4"],
    3: ["0100012345"],
    4: ["2.0.0.0"],
    6: ["14"],
    57: ["+0.000", "+0.000", "+0.000"],
    75: ["0", "0", "00"],
}

# While binary output 1 streams to mtrtk's port, every write is followed by one binary frame
# (a write of no bytes too, which is how configure waits for the stream).
BIN_FRAME = binary((TIME, time_group_payload()))

# A hook may answer for the device: return the reply body (no '$' or '*XX') or None to fall
# through to the default behaviour. "" means: say nothing (a lost reply); bodies separated
# by "\n" are sent as several lines, in order.
Hook = Callable[[str, list[str]], str | None]


class VnDevice:
    name = "vn-device"
    ends_at_eof = False

    def __init__(self, regs: dict[int, list[str]] | None = None) -> None:
        self.regs: dict[int, list[str]] = {k: list(v) for k, v in DEFAULT_REGS.items()}
        if regs:
            self.regs.update(regs)
        self.q: asyncio.Queue[bytes] = asyncio.Queue()
        self.commands: list[str] = []  # bodies between '$' and '*', as written
        self.hooks: list[Hook] = []
        self.written: list[bytes] = []
        self.port = 1  # the serial port mtrtk is connected to

    async def open(self) -> None:
        return None

    async def read(self) -> bytes:
        return await self.q.get()

    async def close(self) -> None:
        self.q.put_nowait(b"")

    async def write(self, data: bytes) -> None:
        self.written.append(data)
        if data.startswith(b"$VN") and verify_ascii(data):
            body = data[1 : data.rindex(b"*")].decode()
            self.commands.append(body)
            reply = self._answer(body)
            for line in (reply or "").split("\n"):
                if line:
                    self.q.put_nowait(finalize_ascii(line))
        if self.streaming():
            self.q.put_nowait(BIN_FRAME)
        await asyncio.sleep(0)

    def streaming(self) -> bool:
        """Binary output 1 is on and routed to the port mtrtk is on (`self.port`)."""
        conf = self.regs.get(75) or ["0"]
        mode = int(conf[0]) if conf[0].isdigit() else 0
        return mode == 3 or (mode != 0 and mode == self.port)

    def _answer(self, body: str) -> str | None:
        cmd, *args = body.split(",")
        for hook in self.hooks:
            got = hook(cmd, args)
            if got is not None:
                return got
        if cmd == "VNRRG":
            reg = int(args[0])
            if reg not in self.regs:
                return "VNERR,08"
            return ",".join(["VNRRG", f"{reg:02d}", *self.regs[reg]])
        if cmd == "VNWRG":
            reg = int(args[0])
            self.regs[reg] = list(args[1:])
            return ",".join(["VNWRG", f"{reg:02d}", *args[1:]])
        if cmd in ("VNWNV", "VNRST", "VNRFS"):
            return cmd
        return "VNERR,04"


async def start(device: VnDevice) -> tuple[InsController, asyncio.Event, asyncio.Task[None]]:
    """A real `InsController` over *device*, running until the returned event is set."""
    bus = Bus()
    controller = InsController(bus, lambda: device, VnFramer, configure=None, rx_timeout_s=30)
    stop = asyncio.Event()
    task = asyncio.create_task(controller.run(stop))
    for _ in range(100):
        if controller.connected:
            break
        await asyncio.sleep(0)
    assert controller.connected
    return controller, stop, task


async def finish(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
    stop.set()
    await asyncio.wait_for(task, 2)
