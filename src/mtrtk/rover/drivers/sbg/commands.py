"""sbgECom commands (class CMD_0): GET/SET with ACK correlation, payload encoders and decoders.

Protocol (sbgECom 5.8.935, `src/commands/*.c`, `sbgEComCmdCommon.c`):

- GET: send the command id with its *selector* payload (empty for most commands). The unit
  answers with the same command id carrying the full payload, which echoes the selector first.
  An error ACK for the command instead of data refuses the GET. A code-0 ACK is ignored: it is
  the late ACK of a resent SET of the same command (`sbgEComReceiveCmd2` would treat any ACK as
  a refusal, which turns that straggler into a false failure of the read-back).
- SET: send the command id with the full payload; the unit answers `ACK` (id 0):
  `ackMsgId u8, ackMsgClass u8, errorCode u16` (0 = OK).
- Each command is retried on timeout only (3 trials of 500 ms by default); an error ACK is an
  answer and is not retried.
- One command is outstanding at a time per controller, whoever sends it (the configure hook, a
  CLI or API read): ACKs carry no selector, so two commands in flight could take each other's.
  The protocol still cannot tell a straggler from an answer: a late error ACK of a GET that
  timed out can refuse the next GET of the same command.

The encoders and decoders are pure module-level functions so the layouts are testable without
I/O. Decoders accept longer payloads (newer firmware appends fields) and raise `ValueError` on
shorter ones; `SbgCommands` turns that into `SbgCommandError`.
"""

from __future__ import annotations

import asyncio
import struct
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from mtrtk.core.frames import Frame, Proto
from mtrtk.rover.drivers.ins_common import InsController
from mtrtk.rover.drivers.sbg.framer import encode
from mtrtk.rover.drivers.sbg.ids import CLASS, CMD, CMD_NAME
from mtrtk.rover.drivers.sbg.logs import SbgInfo as SbgInfo  # one INFO type: Task 2's
from mtrtk.rover.drivers.sbg.logs import parse_payload

CMD_CLASS = CLASS["CMD_0"]
ACK = CMD["ACK"]
DEFAULT_TIMEOUT_S = 0.5  # SBG_ECOM_DEFAULT_CMD_TIME_OUT
DEFAULT_RETRIES = 3  # sbgEComInit: numTrials
SETTINGS_ACTION_TIMEOUT_S = 2.0  # one try only: a save/reboot is never resent

# SbgErrorCode (common/sbgErrorCodes.h), carried in ACK.errorCode.
ERROR_NAME: dict[int, str] = {
    0: "NO_ERROR",
    1: "ERROR",
    2: "NULL_POINTER",
    3: "INVALID_CRC",
    4: "INVALID_FRAME",
    5: "TIME_OUT",
    6: "WRITE_ERROR",
    7: "READ_ERROR",
    8: "BUFFER_OVERFLOW",
    9: "INVALID_PARAMETER",
    10: "NOT_READY",
    11: "MALLOC_FAILED",
    12: "CALIB_MAG_NOT_ENOUGH_POINTS",
    13: "CALIB_MAG_INVALID_TAKE",
    14: "CALIB_MAG_SATURATION",
    15: "CALIB_MAG_POINTS_NOT_IN_A_PLANE",
    16: "DEVICE_NOT_FOUND",
    17: "OPERATION_CANCELLED",
    18: "NOT_CONTINUOUS_FRAME",
    19: "INCOMPATIBLE_HARDWARE",
    20: "INVALID_VERSION",
}
SBG_ERROR = 1

# SbgEComOutputPort
OUTPUT_PORT: dict[str, int] = {"A": 0, "C": 2, "D": 3, "E": 4}
PORT_A = OUTPUT_PORT["A"]

# SbgEComOutputMode
OUTPUT_MODE: dict[str, int] = {
    "DISABLED": 0,
    "MAIN_LOOP": 1,  # 200 Hz
    "DIV_2": 2,
    "DIV_4": 4,
    "DIV_5": 5,
    "DIV_8": 8,
    "DIV_10": 10,
    "DIV_20": 20,  # 10 Hz
    "DIV_40": 40,
    "DIV_100": 100,  # 2 Hz, Ellipse firmware v3+
    "DIV_200": 200,  # 1 Hz
    "PPS": 10000,
    "NEW_DATA": 10001,
    "EVENT_IN_A": 10003,
    "EVENT_IN_B": 10004,
    "EVENT_IN_C": 10005,
    "EVENT_IN_D": 10006,
    "EVENT_IN_E": 10007,
}
OUTPUT_MODE_NAME: dict[int, str] = {v: k for k, v in OUTPUT_MODE.items()}

# SbgEComMotionProfileStdIds
MOTION_PROFILE: dict[str, int] = {
    "GENERAL_PURPOSE": 1,
    "AUTOMOTIVE": 2,
    "MARINE": 3,
    "AIRPLANE": 4,
    "HELICOPTER": 5,
    "PEDESTRIAN": 6,
    "UAV_ROTARY_WING": 7,
    "HEAVY_MACHINERY": 8,
    "STATIC": 9,
    "TRUCK": 10,
    "RAILWAY": 11,
    "OFF_ROAD_VEHICLE": 12,
    "UNDERWATER": 13,
}

# SbgEComModulePortAssignment (AIDING_ASSIGNMENT)
MODULE_PORT: dict[str, int] = {
    "PORT_A": 0,
    "PORT_B": 1,
    "PORT_C": 2,
    "PORT_D": 3,
    "PORT_E": 4,
    "INTERNAL": 5,
    "DISABLED": 0xFF,
}

# SbgEComAxisDirection (IMU_ALIGNMENT_LEVER_ARM)
AXIS: dict[str, int] = {"FORWARD": 0, "BACKWARD": 1, "LEFT": 2, "RIGHT": 3, "UP": 4, "DOWN": 5}

# SbgEComGnssInstallationMode
GNSS_INSTALL_MODE: dict[str, int] = {
    "SINGLE": 1,
    "DUAL_AUTO": 2,  # [Reserved]
    "DUAL_ROUGH": 3,  # [Deprecated]
    "DUAL_PRECISE": 4,
}

# SbgEComSettingsAction
REBOOT_ONLY = 0
SAVE_SETTINGS = 1  # saves to flash *and reboots*
RESTORE_DEFAULT = 2

# SbgEComPortId (UART_CONF) and SbgEComPortMode
COM_A, COM_B, COM_C, COM_D, COM_E = 0, 1, 2, 3, 4
UART_MODE: dict[int, str] = {0: "OFF", 1: "RS-232", 2: "RS-422"}


class SbgCommandError(RuntimeError):
    """A command the unit refused (`code` from its ACK) or never answered (`code is None`)."""

    def __init__(self, cmd: int, code: int | None, detail: str = "") -> None:
        name = CMD_NAME.get(cmd, str(cmd))
        if code is None:
            reason = detail or "timeout"
        else:
            reason = f"error {code} ({ERROR_NAME.get(code, 'unknown')})"
            if detail:
                reason += f" {detail}"
        super().__init__(f"sbgECom {name}: {reason}")
        self.cmd, self.code = cmd, code


# ------------------------------------------------------------------ payload types
Vec3 = tuple[float, float, float]


@dataclass(frozen=True)
class GnssInstallation:
    lever_arm_primary: Vec3  # IMU -> primary antenna, IMU X/Y/Z axes, metres
    primary_precise: bool  # True: the lever arm is trusted, not re-estimated online
    lever_arm_secondary: Vec3
    secondary_mode: int  # GNSS_INSTALL_MODE


@dataclass(frozen=True)
class ImuAlignment:
    axis_x: int  # AXIS: the vehicle direction the IMU X axis points to
    axis_y: int
    mis_roll: float  # residual misalignment, rad
    mis_pitch: float
    mis_yaw: float
    lever_arm: Vec3  # IMU -> vehicle reference point, metres


@dataclass(frozen=True)
class AidingAssignment:
    gps1_port: int  # MODULE_PORT; INTERNAL on an Ellipse-D
    gps1_sync: int
    dvl_port: int
    dvl_sync: int
    rtcm_port: int
    air_data_port: int
    odometer_pins: int


@dataclass(frozen=True)
class InitParameters:
    latitude: float  # degrees
    longitude: float
    altitude: float  # metres, height above the ellipsoid
    date: date | None = field(compare=False)  # the unit's notion of "today": not a setting


@dataclass(frozen=True)
class UartConf:
    interface: int
    baud: int
    mode: int  # UART_MODE


# ------------------------------------------------------------------ helpers
def _f32(x: float) -> float:
    """The shortest decimal that encodes to the same f32: -1.2 reads back as -1.2."""
    packed = struct.pack("<f", x)
    for digits in range(6, 10):
        candidate = float(f"{x:.{digits}g}")
        if struct.pack("<f", candidate) == packed:
            return candidate
    return x


def _vec(values: tuple[Any, ...]) -> Vec3:
    return (_f32(values[0]), _f32(values[1]), _f32(values[2]))


def _unpack(fmt: str, raw: bytes, what: str) -> tuple[Any, ...]:
    size = struct.calcsize(fmt)
    if len(raw) < size:
        raise ValueError(f"{what} payload is {len(raw)} bytes, expected at least {size}")
    return struct.unpack_from(fmt, raw)


def _date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


# ------------------------------------------------------------------ encoders / decoders
INFO_FMT = "<32sIIHBBII"
OUTPUT_CONF_FMT = "<BBBH"  # outputPort, msgId, classId (id before class), mode
CLASS_ENABLE_FMT = "<BB?"
GNSS_INSTALL_FMT = "<3f?3fB"
IMU_ALIGN_FMT = "<BBfff3f"
AIDING_FMT = "<BBIBBBBB"
INIT_FMT = "<dddHBB"
UART_FMT = "<BIB"


def decode_info(raw: bytes) -> SbgInfo:
    """The INFO reply, decoded by the log parser's own INFO decoder (`logs.SbgInfo`)."""
    _unpack(INFO_FMT, raw, "INFO")  # a short payload raises ValueError, like every decoder
    info = parse_payload(CMD_CLASS, CMD["INFO"], raw)
    if not isinstance(info, SbgInfo):
        raise ValueError("INFO payload did not decode")
    return info


def encode_output_conf_selector(port: int, cls: int, msg_id: int) -> bytes:
    return struct.pack("<BBB", port, msg_id, cls)


def encode_output_conf(port: int, cls: int, msg_id: int, mode: int) -> bytes:
    return struct.pack(OUTPUT_CONF_FMT, port, msg_id, cls, mode)


def decode_output_conf(raw: bytes) -> tuple[int, int, int, int]:
    """-> (port, class, msg_id, mode)."""
    port, msg_id, cls, mode = _unpack(OUTPUT_CONF_FMT, raw, "OUTPUT_CONF")
    return int(port), int(cls), int(msg_id), int(mode)


def encode_output_class_enable(port: int, cls: int, enable: bool) -> bytes:
    return struct.pack(CLASS_ENABLE_FMT, port, cls, enable)


def decode_output_class_enable(raw: bytes) -> bool:
    return bool(_unpack(CLASS_ENABLE_FMT, raw, "OUTPUT_CLASS_ENABLE")[2])


def encode_gnss_installation(inst: GnssInstallation) -> bytes:
    return struct.pack(
        GNSS_INSTALL_FMT,
        *inst.lever_arm_primary,
        inst.primary_precise,
        *inst.lever_arm_secondary,
        inst.secondary_mode,
    )


def decode_gnss_installation(raw: bytes) -> GnssInstallation:
    v = _unpack(GNSS_INSTALL_FMT, raw, "GNSS_1_INSTALLATION")
    return GnssInstallation(
        _vec(v[0:3]),
        bool(v[3]),
        _vec(v[4:7]),
        int(v[7]),
    )


def encode_imu_alignment(al: ImuAlignment) -> bytes:
    return struct.pack(
        IMU_ALIGN_FMT, al.axis_x, al.axis_y, al.mis_roll, al.mis_pitch, al.mis_yaw, *al.lever_arm
    )


def decode_imu_alignment(raw: bytes) -> ImuAlignment:
    v = _unpack(IMU_ALIGN_FMT, raw, "IMU_ALIGNMENT_LEVER_ARM")
    return ImuAlignment(
        int(v[0]),
        int(v[1]),
        _f32(float(v[2])),
        _f32(float(v[3])),
        _f32(float(v[4])),
        _vec(v[5:8]),
    )


def encode_aiding_assignment(aid: AidingAssignment) -> bytes:
    return struct.pack(
        AIDING_FMT,
        aid.gps1_port,
        aid.gps1_sync,
        0,  # reserved
        aid.dvl_port,
        aid.dvl_sync,
        aid.rtcm_port,
        aid.air_data_port,
        aid.odometer_pins,
    )


def decode_aiding_assignment(raw: bytes) -> AidingAssignment:
    g1, g1s, _reserved, dvl, dvls, rtcm, air, odo = _unpack(AIDING_FMT, raw, "AIDING_ASSIGNMENT")
    return AidingAssignment(int(g1), int(g1s), int(dvl), int(dvls), int(rtcm), int(air), int(odo))


def encode_motion_profile(model_id: int) -> bytes:
    return struct.pack("<I", model_id)


def decode_motion_profile(raw: bytes) -> int:
    return int(_unpack("<I", raw, "MOTION_PROFILE_ID")[0])


def encode_init_parameters(lat: float, lon: float, alt_hae: float, day: date) -> bytes:
    return struct.pack(INIT_FMT, lat, lon, alt_hae, day.year, day.month, day.day)


def decode_init_parameters(raw: bytes) -> InitParameters:
    lat, lon, alt, year, month, day = _unpack(INIT_FMT, raw, "INIT_PARAMETERS")
    return InitParameters(
        float(lat), float(lon), float(alt), _date(int(year), int(month), int(day))
    )


def encode_settings_action(action: int) -> bytes:
    return struct.pack("<B", action)


def decode_uart_conf(raw: bytes) -> UartConf:
    interface, baud, mode = _unpack(UART_FMT, raw, "UART_CONF")
    return UartConf(int(interface), int(baud), int(mode))


# ------------------------------------------------------------------ the command channel
def _ack_fields(frame: Frame) -> tuple[int, int, int]:
    """-> (ackMsgId, ackMsgClass, errorCode) of an ACK frame."""
    msg_id, msg_class, code = struct.unpack_from("<BBH", frame.payload)
    return msg_id, msg_class, code


def _is_cmd_frame(frame: Frame, msg_id: int) -> bool:
    return frame.proto is Proto.SBG and frame.raw[3] == CMD_CLASS and frame.raw[2] == msg_id


def _is_ack_for(frame: Frame, cmd: int) -> bool:
    if not _is_cmd_frame(frame, ACK) or len(frame.payload) < 4:
        return False
    msg_id, msg_class, _ = _ack_fields(frame)
    return msg_id == cmd and msg_class == CMD_CLASS


_COMMAND_LOCKS: weakref.WeakKeyDictionary[InsController, asyncio.Lock] = weakref.WeakKeyDictionary()


def command_lock(controller: InsController) -> asyncio.Lock:
    """The one-command-in-flight lock of *controller*, shared by every `SbgCommands` on it."""
    lock = _COMMAND_LOCKS.get(controller)
    if lock is None:
        lock = _COMMAND_LOCKS[controller] = asyncio.Lock()
    return lock


class SbgCommands:
    """GET/SET sbgECom commands through an `InsController` (its single writer and its
    request/response correlation). A link that drops mid-command raises `ConnectionError`.

    Every `SbgCommands` on one controller shares one lock: a command (all its retries) is
    answered or given up before the next one is sent."""

    def __init__(self, controller: InsController) -> None:
        self.ctrl = controller
        self._lock = command_lock(controller)

    async def get(
        self,
        cmd: int,
        selector: bytes = b"",
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        retries: int = DEFAULT_RETRIES,
    ) -> bytes:
        """Send a GET and return the reply payload. The reply must echo *selector* (an
        OUTPUT_CONF answer for another message is not this one's)."""

        def match(f: Frame) -> bool:
            if _is_cmd_frame(f, cmd):
                return f.payload.startswith(selector)
            # Only an error ACK refuses a GET. A code-0 ACK for this command is the late ACK
            # of a resent SET (a read-back follows its SET at once): it answers nothing.
            return _is_ack_for(f, cmd) and _ack_fields(f)[2] != 0

        async with self._lock:
            for _ in range(retries):
                try:
                    reply = await self.ctrl.request(
                        match, encode(CMD_CLASS, cmd, selector), timeout_s
                    )
                except TimeoutError:
                    continue
                if reply.raw[2] == ACK:  # refused: an error ACK instead of data
                    raise SbgCommandError(cmd, _ack_fields(reply)[2])
                return reply.payload
        raise SbgCommandError(cmd, None, f"no reply after {retries} attempts")

    async def set(
        self,
        cmd: int,
        payload: bytes,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        retries: int = DEFAULT_RETRIES,
    ) -> None:
        """Send a SET and wait for its ACK; `SbgCommandError` on a non-zero code or no ACK."""
        async with self._lock:
            for _ in range(retries):
                try:
                    ack = await self.ctrl.request(
                        lambda f: _is_ack_for(f, cmd), encode(CMD_CLASS, cmd, payload), timeout_s
                    )
                except TimeoutError:
                    continue
                code = _ack_fields(ack)[2]
                if code:
                    raise SbgCommandError(cmd, code)
                return
        raise SbgCommandError(cmd, None, f"no ACK after {retries} attempts")

    async def _get_decoded[T](
        self, cmd: int, decode: Callable[[bytes], T], selector: bytes = b""
    ) -> T:
        raw = await self.get(cmd, selector)
        try:
            return decode(raw)
        except (ValueError, struct.error) as exc:
            raise SbgCommandError(cmd, None, f"malformed reply: {exc}") from exc

    # -------------------------------------------------------------- typed wrappers
    async def get_info(self) -> SbgInfo:
        return await self._get_decoded(CMD["INFO"], decode_info)

    async def get_output_conf(self, port: int, cls: int, msg_id: int) -> int:
        sel = encode_output_conf_selector(port, cls, msg_id)
        return (await self._get_decoded(CMD["OUTPUT_CONF"], decode_output_conf, sel))[3]

    async def set_output_conf(self, port: int, cls: int, msg_id: int, mode: int) -> None:
        await self.set(CMD["OUTPUT_CONF"], encode_output_conf(port, cls, msg_id, mode))

    async def get_output_class_enable(self, port: int, cls: int) -> bool:
        sel = struct.pack("<BB", port, cls)
        return await self._get_decoded(CMD["OUTPUT_CLASS_ENABLE"], decode_output_class_enable, sel)

    async def set_output_class_enable(self, port: int, cls: int, enable: bool) -> None:
        await self.set(CMD["OUTPUT_CLASS_ENABLE"], encode_output_class_enable(port, cls, enable))

    async def get_gnss_installation(self) -> GnssInstallation:
        return await self._get_decoded(CMD["GNSS_1_INSTALLATION"], decode_gnss_installation)

    async def set_gnss_installation(self, inst: GnssInstallation) -> None:
        await self.set(CMD["GNSS_1_INSTALLATION"], encode_gnss_installation(inst))

    async def get_imu_alignment(self) -> ImuAlignment:
        return await self._get_decoded(CMD["IMU_ALIGNMENT_LEVER_ARM"], decode_imu_alignment)

    async def set_imu_alignment(self, al: ImuAlignment) -> None:
        await self.set(CMD["IMU_ALIGNMENT_LEVER_ARM"], encode_imu_alignment(al))

    async def get_motion_profile(self) -> int:
        return await self._get_decoded(CMD["MOTION_PROFILE_ID"], decode_motion_profile)

    async def set_motion_profile(self, model_id: int) -> None:
        await self.set(CMD["MOTION_PROFILE_ID"], encode_motion_profile(model_id))

    async def get_aiding_assignment(self) -> AidingAssignment:
        return await self._get_decoded(CMD["AIDING_ASSIGNMENT"], decode_aiding_assignment)

    async def set_aiding_assignment(self, aid: AidingAssignment) -> None:
        await self.set(CMD["AIDING_ASSIGNMENT"], encode_aiding_assignment(aid))

    async def get_init_parameters(self) -> InitParameters:
        return await self._get_decoded(CMD["INIT_PARAMETERS"], decode_init_parameters)

    async def set_init_parameters(self, lat: float, lon: float, alt_hae: float, day: date) -> None:
        await self.set(CMD["INIT_PARAMETERS"], encode_init_parameters(lat, lon, alt_hae, day))

    async def settings_action(self, action: int) -> None:
        """SAVE_SETTINGS / RESTORE_DEFAULT / REBOOT_ONLY: the unit ACKs, then reboots (the link
        goes quiet for ~2-3 s).

        Sent once and never resent: a flash save can take longer than the usual 500 ms to ACK,
        and the reboot can swallow the ACK, so a resend would hit a unit that is saving or
        coming back up (a second save and reboot). A missing ACK raises `SbgCommandError` with
        `code None`: the action was sent but is unconfirmed."""
        await self.set(
            CMD["SETTINGS_ACTION"],
            encode_settings_action(action),
            timeout_s=SETTINGS_ACTION_TIMEOUT_S,
            retries=1,
        )

    async def get_uart_conf(self, interface: int) -> UartConf:
        sel = struct.pack("<B", interface)
        return await self._get_decoded(CMD["UART_CONF"], decode_uart_conf, sel)
