"""VectorNav register I/O over the ASCII protocol: `$VNRRG` reads, `$VNWRG` writes, commands.

Each request is written through `InsController.request` and matched to the unit's reply: an
`$VNRRG,<reg>,...` / `$VNWRG,<reg>,...` line for the same register, the command echoed
(`$VNWNV`, `$VNRST`, `$VNRFS`), or `$VNERR,<code>`. A request that gets no reply in
`timeout_s` is resent, `retries` attempts in all, then `TimeoutError`.

`$VNERR` does not say which command it answers. A parameter error (5-9, 12: the unit read
this request and refused it) raises `VnError` at once. A transport error (`TRANSPORT_ERRORS`:
a serial or output buffer overflow, a checksum or command the unit could not read, which RTCM
bytes on the same port or a late error from an earlier attempt can also cause) is treated
like a lost reply: the request is resent, and `VnError` is raised only when every attempt
ended that way.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from mtrtk.core.frames import Frame, Proto
from mtrtk.rover.drivers.ins_common import InsController
from mtrtk.rover.drivers.vectornav.checksum import finalize_ascii
from mtrtk.rover.drivers.vectornav.fields import EXT_FLAG, GPS_GROUPS, GROUP_BITS, GROUP_NAMES
from mtrtk.rover.drivers.vectornav.parse import VnAscii

log = logging.getLogger(__name__)

REG_MODEL = 1
REG_HW_REV = 2
REG_SERIAL = 3
REG_FIRMWARE = 4
REG_BAUD = 5
REG_ASYNC_TYPE = 6
REG_ASYNC_FREQ = 7
REG_REF_ROTATION = 26
REG_COMM_CONTROL = 30
REG_VPE_BASIC = 35
REG_GNSS_CONFIG = 55
REG_ANTENNA_OFFSET = 57
REG_GNSS_LLA = 58
REG_INS_LLA = 63
REG_INS_BASIC = 67
REG_BINARY_OUTPUT = {1: 75, 2: 76, 3: 77}

COMMANDS = frozenset(("WNV", "RST", "RFS"))

ERROR_NAMES: dict[int, str] = {
    1: "HardFault",
    2: "SerialBufferOverflow",
    3: "InvalidChecksum",
    4: "InvalidCommand",
    5: "NotEnoughParameters",
    6: "TooManyParameters",
    7: "InvalidParameter",
    8: "InvalidRegister",
    9: "UnauthorizedAccess",
    10: "WatchdogReset",
    11: "OutputBufferOverflow",
    12: "InsufficientBaudRate",
    255: "ErrorBufferOverflow",
}


class VnError(Exception):
    """The unit answered `$VNERR,<code>`."""

    def __init__(self, code: int) -> None:
        self.code = code
        self.name = ERROR_NAMES.get(code, "Unknown")
        super().__init__(f"VectorNav error {code} ({self.name})")


# Errors about the link rather than the request's parameters: retried like a timeout.
TRANSPORT_ERRORS = frozenset((2, 3, 4, 11, 255))


@dataclass(frozen=True)
class BinaryOutputConf:
    """Registers 75-77. `fields` maps group name -> field mask without bit 15; `gps_ext` is the
    GPS group's extension word when one follows it (bit 15 set on the wire)."""

    async_mode: int  # 0 none, 1 serial 1, 2 serial 2, 3 both
    divisor: int  # of the 800 Hz IMU rate
    fields: dict[str, int] = field(default_factory=dict)
    gps_ext: int | None = None


def fmt_value(value: object) -> str:
    """A register field as VN expects it: ints plain, floats without an exponent."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"not a finite number: {value}")
        text = repr(value)
        return f"{value:.6f}".rstrip("0").rstrip(".") if "e" in text else text
    return str(value)


def _is_reply(frame: Frame) -> VnAscii | None:
    if frame.proto is not Proto.VN or frame.raw[:1] != b"$":
        return None
    parsed = frame.parsed()
    return parsed if isinstance(parsed, VnAscii) else None


class VnRegisters:
    def __init__(self, controller: InsController) -> None:
        self.controller = controller

    # ------------------------------------------------------------- generic
    async def _exchange(
        self, body: str, match_cmd: str, reg: int | None, timeout_s: float, retries: int
    ) -> VnAscii:
        def match(frame: Frame) -> bool:
            reply = _is_reply(frame)
            if reply is None:
                return False
            if reply.error is not None:
                return True
            return reply.cmd == match_cmd and reply.register == reg

        line = finalize_ascii(body)
        attempts = max(1, retries)
        last: VnError | None = None
        for attempt in range(1, attempts + 1):
            try:
                frame = await self.controller.request(match, line, timeout_s)
            except TimeoutError:
                log.debug("no reply to %s (attempt %d/%d)", body, attempt, attempts)
                last = None
                continue
            reply = _is_reply(frame)
            assert reply is not None
            if reply.error is not None:
                if reply.error in TRANSPORT_ERRORS:
                    last = VnError(reply.error)
                    log.debug("%s to %s (attempt %d/%d)", last, body, attempt, attempts)
                    continue
                raise VnError(reply.error)
            return reply
        if last is not None:
            raise last
        raise TimeoutError(f"no reply to ${body} after {attempts} attempts")

    async def read(self, reg: int, timeout_s: float = 1.0, retries: int = 3) -> list[str]:
        reply = await self._exchange(f"VNRRG,{reg:02d}", "RRG", reg, timeout_s, retries)
        return reply.fields

    async def write(
        self, reg: int, *fields: object, timeout_s: float = 1.0, retries: int = 3
    ) -> list[str]:
        """Write *fields* to *reg*; returns the echoed register contents."""
        body = ",".join([f"VNWRG,{reg:02d}", *(fmt_value(f) for f in fields)])
        reply = await self._exchange(body, "WRG", reg, timeout_s, retries)
        return reply.fields

    async def command(self, name: str, timeout_s: float = 1.0, retries: int = 3) -> None:
        """`WNV` (save settings to flash), `RST` (reset) or `RFS` (restore factory)."""
        if name not in COMMANDS:
            raise ValueError(f"unknown VectorNav command {name!r}")
        await self._exchange(f"VN{name}", name, None, timeout_s, retries)

    async def await_binary(self, timeout_s: float) -> bool:
        """True once a binary output frame is routed on this port within *timeout_s*. Sends
        nothing (the request writes zero bytes): it only listens."""

        def match(frame: Frame) -> bool:
            return frame.proto is Proto.VN and frame.raw[:1] == b"\xfa"

        try:
            await self.controller.request(match, b"", timeout_s)
        except TimeoutError:
            return False
        return True

    # ------------------------------------------------------------- typed helpers
    async def model(self) -> str:
        return ",".join(await self.read(REG_MODEL))

    async def hardware_revision(self) -> str:
        return ",".join(await self.read(REG_HW_REV))

    async def serial(self) -> str:
        return ",".join(await self.read(REG_SERIAL))

    async def firmware(self) -> str:
        return ",".join(await self.read(REG_FIRMWARE))

    async def async_output_type(self) -> int:
        return int((await self.read(REG_ASYNC_TYPE))[0])

    async def set_async_output_type(self, value: int) -> None:
        await self.write(REG_ASYNC_TYPE, value)

    async def write_binary_output(
        self,
        n: int,
        async_mode: int,
        divisor: int,
        fields: dict[str, int],
        gps_ext: int | None = None,
    ) -> None:
        """`$VNWRG,<75+n-1>,<asyncMode>,<divisor>,<groups hex>,<field hex per group>`.

        Fields go in group-bit order as uppercase hex without padding (vnproglib `%X`). With
        *gps_ext* the GPS field is written with bit 15 set and followed by the extension word
        (VERIFY against the VN-200 manual's register 75 description: vnproglib 1.2 does not
        encode extensions).
        """
        out = encode_binary_output(BinaryOutputConf(async_mode, divisor, fields, gps_ext))
        await self.write(REG_BINARY_OUTPUT[n], *out)

    async def read_binary_output(self, n: int) -> BinaryOutputConf:
        return decode_binary_output(await self.read(REG_BINARY_OUTPUT[n]))

    async def set_antenna_offset(self, x: float, y: float, z: float) -> None:
        await self.write(REG_ANTENNA_OFFSET, x, y, z)

    async def read_antenna_offset(self) -> tuple[float, float, float]:
        x, y, z = (float(v) for v in (await self.read(REG_ANTENNA_OFFSET))[:3])
        return (x, y, z)

    async def set_reference_frame_rotation(self, matrix9: Sequence[float]) -> None:
        if len(matrix9) != 9:
            raise ValueError("the reference frame rotation is a 3x3 matrix: nine values")
        await self.write(REG_REF_ROTATION, *matrix9)

    async def read_reference_frame_rotation(self) -> tuple[float, ...]:
        return tuple(float(v) for v in await self.read(REG_REF_ROTATION))

    async def set_vpe_basic_control(
        self, enable: int, heading_mode: int, filtering_mode: int, tuning_mode: int
    ) -> None:
        await self.write(REG_VPE_BASIC, enable, heading_mode, filtering_mode, tuning_mode)

    async def read_vpe_basic_control(self) -> tuple[int, ...]:
        return tuple(int(v) for v in await self.read(REG_VPE_BASIC))

    async def set_ins_basic_config(self, scenario: int, ahrs_aiding: bool) -> None:
        """Register 67 on the VN-200: scenario, AHRS aiding, two reserved zeros. VERIFY the
        scenario values against the VN-200 manual."""
        await self.write(REG_INS_BASIC, scenario, int(ahrs_aiding), 0, 0)

    async def read_ins_basic_config(self) -> tuple[int, ...]:
        return tuple(int(v) for v in await self.read(REG_INS_BASIC))

    async def write_settings(self) -> None:
        await self.command("WNV")


def encode_binary_output(conf: BinaryOutputConf) -> list[str]:
    groups = 0
    words: list[str] = []
    for name in sorted(conf.fields, key=lambda k: GROUP_BITS[k]):
        g = GROUP_BITS[name]
        groups |= 1 << g
        mask = conf.fields[name] & ~EXT_FLAG
        if g == GROUP_BITS["gps"] and conf.gps_ext:
            words += [f"{mask | EXT_FLAG:X}", f"{conf.gps_ext:X}"]
        else:
            words.append(f"{mask:X}")
    return [str(conf.async_mode), str(conf.divisor), f"{groups:X}", *words]


def decode_binary_output(values: list[str]) -> BinaryOutputConf:
    async_mode, divisor, groups = int(values[0]), int(values[1]), int(values[2], 16)
    words = [int(v, 16) for v in values[3:]]
    fields: dict[str, int] = {}
    gps_ext: int | None = None
    i = 0
    for g in range(7):
        if not groups & (1 << g):
            continue
        mask = words[i]
        i += 1
        if mask & EXT_FLAG and g in GPS_GROUPS:
            ext = words[i]
            i += 1
            if g == GROUP_BITS["gps"]:
                gps_ext = ext
        fields[GROUP_NAMES[g]] = mask & ~EXT_FLAG
    return BinaryOutputConf(async_mode, divisor, fields, gps_ext)
