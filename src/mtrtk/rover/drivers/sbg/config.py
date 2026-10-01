"""Declarative SBG Ellipse configuration: a profile built from `Settings`, read, applied, verified.

`configure()` always reads `INFO` and the current value of every profile item (the UI and
`mtrtk ins info` show them). With `apply=True` each item that differs is SET, read back and
compared; the read-back is the evidence, not the ACK. When at least one change took, nothing read
back wrong and `INS_APPLY_CONFIG=1`, the settings are saved to flash once per process
(`SETTINGS_ACTION SAVE_SETTINGS`, which also reboots the unit; `InsController` rides out or
reconnects across the reboot, and the next `configure()` finds everything unchanged).

Rules that keep a write from stranding or degrading the unit:

- the baud rate is never written (a wrong value strands the link: set Port A to 460800 in
  sbgCenter when `INS_OUTPUT_HZ > 50` or raw GNSS is enabled);
- items whose target depends on the unit's current value change only what mtrtk has a setting
  for: the GNSS secondary antenna and mode stay as set in sbgCenter unless `INS_LEVER_ARM_GNSS2`
  is given, the IMU misalignment angles are always the unit's own, and of the aiding assignment
  only the RTCM port is touched;
- an output or output class the unit refuses to report (older firmware without that log) is
  listed as `unsupported`, not as an error;
- Port A's baud is read first, before anything is written. Above 50 Hz on a link slower than
  460800 baud the over-rate outputs are not written (they would flood the link and starve the
  ACKs) and nothing is flashed; when the baud cannot be read, `INS_BAUD` (the rate the link is
  open at) stands in for it, so the hold fails closed;
- nothing is flashed when an item read back wrong. A SET that got no ACK is still read back: the
  read-back says whether it took;
- `INS_MOTION_PROFILE` is managed only when it is set: the `general` default leaves the unit's
  own profile alone;
- one `configure()` at a time per controller: the on-connect hook and a forced apply from the
  API never interleave (and `SbgCommands` keeps one command in flight per controller, since
  ACKs are matched by command id only).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol

from mtrtk.config import Settings
from mtrtk.rover.drivers.ins_common import Configure, InsController
from mtrtk.rover.drivers.sbg.commands import (
    AXIS,
    COM_A,
    GNSS_INSTALL_MODE,
    MODULE_PORT,
    MOTION_PROFILE,
    OUTPUT_MODE,
    PORT_A,
    SAVE_SETTINGS,
    AidingAssignment,
    GnssInstallation,
    ImuAlignment,
    InitParameters,
    SbgCommandError,
    SbgCommands,
    UartConf,
    Vec3,
)
from mtrtk.rover.drivers.sbg.ids import CLASS, LOG, LOG_NAME
from mtrtk.rover.drivers.sbg.logs import SbgInfo

log = logging.getLogger(__name__)

# INS_OUTPUT_HZ -> output mode (a divider of the 200 Hz main loop).
HZ_TO_MODE: dict[int, int] = {
    200: 1,
    100: 2,
    50: 4,
    40: 5,
    25: 8,
    20: 10,
    10: 20,
    5: 40,
    2: 100,
    1: 200,
}

# INS_MOTION_PROFILE -> SbgEComMotionProfileStdIds
MOTION_PROFILE_IDS: dict[str, int] = {
    "general": MOTION_PROFILE["GENERAL_PURPOSE"],
    "automotive": MOTION_PROFILE["AUTOMOTIVE"],
    "marine": MOTION_PROFILE["MARINE"],
    "airplane": MOTION_PROFILE["AIRPLANE"],
    "helicopter": MOTION_PROFILE["HELICOPTER"],
    "pedestrian": MOTION_PROFILE["PEDESTRIAN"],
    "uav": MOTION_PROFILE["UAV_ROTARY_WING"],
}

DEFAULT_IMU_AXIS = "xyz"  # leave the unit's axis alignment as it is
NMEA_CLASSES = ("LOG_NMEA_0", "LOG_NMEA_1", "LOG_NMEA_GNSS")
FLOAT_TOL = 1e-6  # f32 read-back vs the configured float64 (relative and absolute)
# An output (or output class) GET refused with one of these means "the unit has no such log"
# (older firmware, another model). Any other code (NOT_READY, INVALID_CRC, ...) is a failure.
UNSUPPORTED_OUTPUT_CODES = frozenset({9, 19})  # INVALID_PARAMETER, INCOMPATIBLE_HARDWARE
# The brief's link-budget rule: above 50 Hz the navigation logs need Port A at 460800 baud.
# On a slower link the over-rate outputs are not written and nothing is flashed.
FAST_OUTPUT_HZ = 50
FAST_OUTPUT_MIN_BAUD = 460800
MAIN_LOOP_HZ = 200  # output modes 1..200 divide the 200 Hz main loop


def hz_to_mode(hz: int) -> int:
    try:
        return HZ_TO_MODE[hz]
    except KeyError:
        raise ValueError(
            f"{hz} Hz is not an sbgECom output rate; choose from {sorted(HZ_TO_MODE)}"
        ) from None


def parse_imu_axis(value: str) -> tuple[int, int] | None:
    """INS_IMU_AXIS: "xyz" (the default) leaves the unit's axis alignment alone; otherwise
    "<x>,<y>" names the vehicle direction the IMU X and Y axes point to, from
    forward|backward|left|right|up|down ("forward,right" is the aligned mounting)."""
    text = value.strip().lower()
    if text == DEFAULT_IMU_AXIS:
        return None
    parts = [p.strip().upper() for p in text.split(",")]
    if len(parts) != 2 or not all(p in AXIS for p in parts):
        raise ValueError(
            f"INS_IMU_AXIS must be 'xyz' or '<x>,<y>' from {[a.lower() for a in AXIS]}, "
            f"got {value!r}"
        )
    x, y = AXIS[parts[0]], AXIS[parts[1]]
    if x // 2 == y // 2:  # FORWARD/BACKWARD, LEFT/RIGHT, UP/DOWN share an axis
        raise ValueError(f"INS_IMU_AXIS: X and Y cannot lie on the same axis, got {value!r}")
    return x, y


def _default_outputs(output_hz: int, raw_gnss: bool) -> list[tuple[int, int, int]]:
    ecom0, nav = CLASS["LOG_ECOM_0"], hz_to_mode(output_hz)
    off, new, hz1 = OUTPUT_MODE["DISABLED"], OUTPUT_MODE["NEW_DATA"], OUTPUT_MODE["DIV_200"]
    modes: list[tuple[str, int]] = [
        ("STATUS", hz1),
        ("UTC_TIME", hz1),
        ("IMU_SHORT", nav),
        ("EKF_EULER", nav),
        ("EKF_NAV", nav),
        ("EKF_QUAT", off),
        ("IMU_DATA", off),  # deprecated by IMU_SHORT
        ("SHIP_MOTION", off),
        ("GPS1_POS", new),
        ("GPS1_VEL", new),
        ("GPS1_HDT", new),
        ("GPS1_SAT", new),
        ("GPS1_RAW", new if raw_gnss else off),
        ("RTCM_RAW", new),  # echo of accepted corrections: the adapter counts them
        *((f"EVENT_{c}", new) for c in "ABCDE"),  # VERIFY(sbg-sync-in): the Ellipse-D's Sync Ins
        ("MAG", off),
    ]
    return [(ecom0, LOG[name], mode) for name, mode in modes]


@dataclass
class SbgProfile:
    output_hz: int
    outputs: list[tuple[int, int, int]]  # (class, msg id, mode) on Port A
    disable_classes: list[int]  # output classes switched off on Port A (NMEA)
    gnss1_lever_arm: Vec3 | None
    gnss2_lever_arm: Vec3 | None
    imu_axes: tuple[int, int] | None
    imu_lever_arm: Vec3 | None
    motion_profile: int | None
    aiding: dict[str, int] | None  # {"rtcm_port": MODULE_PORT}
    init_position: Vec3 | None  # lat, lon (deg), alt (m HAE)

    def gnss_installation_target(self, current: GnssInstallation) -> GnssInstallation | None:
        if self.gnss1_lever_arm is None and self.gnss2_lever_arm is None:
            return None
        want = current
        if self.gnss1_lever_arm is not None:  # a measured arm: trusted, not re-estimated
            want = dataclasses.replace(
                want, lever_arm_primary=self.gnss1_lever_arm, primary_precise=True
            )
        if self.gnss2_lever_arm is not None:
            want = dataclasses.replace(
                want,
                lever_arm_secondary=self.gnss2_lever_arm,
                secondary_mode=GNSS_INSTALL_MODE["DUAL_PRECISE"],
            )
        return want

    def imu_alignment_target(self, current: ImuAlignment) -> ImuAlignment | None:
        if self.imu_axes is None and self.imu_lever_arm is None:
            return None
        want = current
        if self.imu_axes is not None:
            want = dataclasses.replace(want, axis_x=self.imu_axes[0], axis_y=self.imu_axes[1])
        if self.imu_lever_arm is not None:
            want = dataclasses.replace(want, lever_arm=self.imu_lever_arm)
        return want

    def aiding_target(self, current: AidingAssignment) -> AidingAssignment | None:
        if self.aiding is None:
            return None
        return dataclasses.replace(current, rtcm_port=self.aiding["rtcm_port"])

    def init_target(self, current: InitParameters) -> InitParameters | None:
        if self.init_position is None:
            return None
        lat, lon, alt = self.init_position
        return InitParameters(lat, lon, alt, datetime.now(UTC).date())


def sbg_profile(settings: Settings) -> SbgProfile:
    try:
        hz_to_mode(settings.ins_output_hz)
    except ValueError as exc:
        raise ValueError(f"INS_OUTPUT_HZ: {exc}") from None
    return SbgProfile(
        output_hz=settings.ins_output_hz,
        outputs=_default_outputs(settings.ins_output_hz, settings.ins_raw_gnss),
        disable_classes=[CLASS[name] for name in NMEA_CLASSES],
        gnss1_lever_arm=settings.ins_lever_arm_gnss1,
        gnss2_lever_arm=settings.ins_lever_arm_gnss2,
        imu_axes=parse_imu_axis(settings.ins_imu_axis),
        imu_lever_arm=settings.ins_imu_lever_arm,
        # The `general` default is no choice: manage the profile only when it is configured.
        motion_profile=(
            MOTION_PROFILE_IDS[settings.ins_motion_profile]
            if "ins_motion_profile" in settings.model_fields_set
            else None
        ),
        # VERIFY(sbg-rtcm-port-a): RTCM on Port A (the sbgECom cable) is not documented;
        # Port B is the documented auxiliary RTCM input, used when INS_RTCM_PORT names a
        # second device.
        aiding={
            "rtcm_port": MODULE_PORT["PORT_B"] if settings.ins_rtcm_port else MODULE_PORT["PORT_A"]
        },
        init_position=settings.ins_init_position,
    )


# ------------------------------------------------------------------ report
def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


@dataclass
class SbgConfigReport:
    info: SbgInfo | None = None
    applied: list[str] = field(default_factory=list)  # set and read back equal
    unchanged: list[str] = field(default_factory=list)  # already as wanted, or not managed
    pending: list[str] = field(default_factory=list)  # differs; not applied (apply=False)
    mismatched: list[str] = field(default_factory=list)  # set, but read back differently
    unsupported: list[str] = field(default_factory=list)  # outputs the unit refuses to report
    errors: list[str] = field(default_factory=list)
    current: dict[str, Any] = field(default_factory=dict)  # item -> value read from the unit
    wanted: dict[str, Any] = field(default_factory=dict)  # item -> target, where it differs
    saved: bool = False  # SAVE_SETTINGS sent (and ACKed) by this call

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe form for the API, the WebSocket and the CLI."""
        return {f.name: _jsonable(getattr(self, f.name)) for f in dataclasses.fields(self)}


class SbgConfigTarget(Protocol):
    """What `configure()` needs from the SBG driver."""

    info: SbgInfo | None
    saved_this_run: bool
    config_report: SbgConfigReport | None


def same(a: Any, b: Any) -> bool:
    """Equality for read-back: floats within `FLOAT_TOL` (f32 on the wire), dataclasses per
    compared field (an init position's date is not compared), sequences element-wise, anything
    else (ints, bools) exactly."""
    if dataclasses.is_dataclass(a) and not isinstance(a, type):
        if type(a) is not type(b):
            return False
        return all(
            same(getattr(a, f.name), getattr(b, f.name)) for f in dataclasses.fields(a) if f.compare
        )
    if isinstance(a, tuple | list) and isinstance(b, tuple | list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, float) or isinstance(b, float):
        try:
            return math.isclose(a, b, rel_tol=FLOAT_TOL, abs_tol=FLOAT_TOL)
        except TypeError:
            return False
    return bool(a == b)


# ------------------------------------------------------------------ configure
@dataclass
class _Item:
    name: str
    get: Callable[[], Awaitable[Any]]
    set: Callable[[Any], Awaitable[None]] | None  # None: read-only (shown, never written)
    target: Callable[[Any], Any]  # current -> wanted value, None when not managed
    optional: bool = False  # a GET refused with 9/19 means "the unit has no such log"
    fast: bool = False  # an output above FAST_OUTPUT_HZ: needs FAST_OUTPUT_MIN_BAUD on Port A


def _const(value: Any) -> Callable[[Any], Any]:
    return lambda _current: value


def _is_fast(mode: int) -> bool:
    return 1 <= mode <= MAIN_LOOP_HZ and MAIN_LOOP_HZ / mode > FAST_OUTPUT_HZ


def _items(cmds: SbgCommands, profile: SbgProfile) -> list[_Item]:
    async def get_uart() -> Any:
        return await cmds.get_uart_conf(COM_A)

    # First, before anything is written: the save and the fast outputs depend on Port A's baud.
    items: list[_Item] = [_Item("uart:COM_A", get_uart, None, _const(None))]  # never written
    for cls, msg, mode in profile.outputs:

        async def get_out(c: int = cls, m: int = msg) -> int:
            return await cmds.get_output_conf(PORT_A, c, m)

        async def set_out(v: int, c: int = cls, m: int = msg) -> None:
            await cmds.set_output_conf(PORT_A, c, m, v)

        name = f"output:{LOG_NAME[msg]}"
        items.append(
            _Item(name, get_out, set_out, _const(mode), optional=True, fast=_is_fast(mode))
        )
    class_names = {v: k for k, v in CLASS.items() if k in NMEA_CLASSES}
    for cls in profile.disable_classes:

        async def get_cls(c: int = cls) -> bool:
            return await cmds.get_output_class_enable(PORT_A, c)

        async def set_cls(v: bool, c: int = cls) -> None:
            await cmds.set_output_class_enable(PORT_A, c, v)

        name = f"class:{class_names[cls]}"
        items.append(_Item(name, get_cls, set_cls, _const(False), optional=True))
    items += [
        _Item(
            "motion_profile",
            cmds.get_motion_profile,
            cmds.set_motion_profile,
            _const(profile.motion_profile),
        ),
        _Item(
            "gnss_installation",
            cmds.get_gnss_installation,
            cmds.set_gnss_installation,
            profile.gnss_installation_target,
        ),
        _Item(
            "imu_alignment",
            cmds.get_imu_alignment,
            cmds.set_imu_alignment,
            profile.imu_alignment_target,
        ),
        _Item(
            "aiding_assignment",
            cmds.get_aiding_assignment,
            cmds.set_aiding_assignment,
            profile.aiding_target,
        ),
    ]

    async def set_init(v: InitParameters) -> None:
        assert v.date is not None
        await cmds.set_init_parameters(v.latitude, v.longitude, v.altitude, v.date)

    items.append(_Item("init_position", cmds.get_init_parameters, set_init, profile.init_target))
    return items


async def configure(
    controller: InsController, driver: SbgConfigTarget, settings: Settings, *, apply: bool
) -> SbgConfigReport:
    """Read (and with *apply*, write and verify) the Ellipse profile; see the module docstring.

    The report goes to `driver.config_report` and out as `ins.config`; read-back mismatches and
    command errors also as one `receiver.error`. A link that drops mid-way raises
    `ConnectionError` (`InsController` reports it and configures again on reconnect).
    """
    async with _lock_for(controller):
        return await _configure(controller, driver, settings, apply=apply)


_LOCKS: weakref.WeakKeyDictionary[InsController, asyncio.Lock] = weakref.WeakKeyDictionary()


def _lock_for(controller: InsController) -> asyncio.Lock:
    lock = _LOCKS.get(controller)
    if lock is None:
        lock = _LOCKS[controller] = asyncio.Lock()
    return lock


def _link_too_slow(report: SbgConfigReport, settings: Settings) -> str | None:
    """Why Port A cannot carry `INS_OUTPUT_HZ`, or None. Fails closed: when UART_CONF could
    not be read, `INS_BAUD` (the rate the host has the link open at) stands in for it."""
    if settings.ins_output_hz <= FAST_OUTPUT_HZ:
        return None
    uart = report.current.get("uart:COM_A")
    if isinstance(uart, UartConf):
        baud, source = uart.baud, f"Port A runs at {uart.baud} baud"
    else:
        baud, source = settings.ins_baud, f"INS_BAUD is {settings.ins_baud} (Port A unread)"
    if baud >= FAST_OUTPUT_MIN_BAUD:
        return None
    return (
        f"{source}, INS_OUTPUT_HZ={settings.ins_output_hz} needs {FAST_OUTPUT_MIN_BAUD} "
        "or more (set it in sbgCenter)"
    )


async def _configure(
    controller: InsController, driver: SbgConfigTarget, settings: Settings, *, apply: bool
) -> SbgConfigReport:
    cmds = SbgCommands(controller)
    report = SbgConfigReport()
    problems: list[str] = []
    try:
        report.info = driver.info = await cmds.get_info()  # always: `mtrtk ins info` shows it
    except SbgCommandError as exc:
        # A unit that does not answer INFO will not answer the rest (sbgECom input off, wrong
        # port): report that, and replace the last connection's report rather than keep it.
        driver.info = None
        report.errors.append(f"info: {exc}")
        controller.bus.publish("receiver.error", "INS configuration: " + report.errors[-1])
        log.warning("SBG configuration: %s", report.errors[-1])
        driver.config_report = report
        controller.bus.publish("ins.config", report)
        return report
    items: list[_Item] = []
    try:
        items = _items(cmds, sbg_profile(settings))
    except ValueError as exc:  # INS_OUTPUT_HZ / INS_IMU_AXIS the unit cannot take
        report.errors.append(f"profile: {exc}")
        problems.append(report.errors[-1])
    held: list[str] = []  # fast outputs not written: the link cannot carry them
    slow_link: str | None = None
    for item in items:
        try:
            current = await item.get()
        except SbgCommandError as exc:
            if item.optional and exc.code in UNSUPPORTED_OUTPUT_CODES:
                report.unsupported.append(item.name)
            else:
                report.errors.append(f"{item.name}: {exc}")
                problems.append(report.errors[-1])
            continue
        report.current[item.name] = current
        want = item.target(current)
        if want is None or item.set is None or same(want, current):
            report.unchanged.append(item.name)
            continue
        report.wanted[item.name] = want
        if not apply:
            report.pending.append(item.name)
            continue
        if item.fast:  # Port A's baud was read first (or INS_BAUD stands in for it)
            slow_link = _link_too_slow(report, settings)
            if slow_link is not None:
                report.pending.append(item.name)
                held.append(item.name)
                continue
        try:
            await item.set(want)
        except SbgCommandError as exc:
            if exc.code is not None:  # refused: the unit answered
                report.errors.append(f"{item.name}: {exc}")
                problems.append(report.errors[-1])
                continue
            # No ACK: the SET may still have landed. The read-back decides.
            log.warning("SBG %s: %s; reading back", item.name, exc)
        try:
            after = await item.get()
        except SbgCommandError as exc:
            report.errors.append(f"{item.name}: {exc}")
            problems.append(report.errors[-1])
            continue
        report.current[item.name] = after
        if same(want, after):
            report.applied.append(item.name)
        else:
            report.mismatched.append(item.name)
            problems.append(f"{item.name}: read back {after}, wanted {want}")
    if held:
        report.errors.append(f"outputs held: {', '.join(held)}: {slow_link}")
        problems.append(report.errors[-1])
    if (
        apply
        and report.applied
        and not report.mismatched
        and settings.ins_apply_config
        and not driver.saved_this_run
    ):
        blocker = _link_too_slow(report, settings)
        if blocker is not None:
            report.errors.append(f"save: held: {blocker}")
            problems.append(report.errors[-1])
        else:
            driver.saved_this_run = True  # once per process, even if the reboot eats the ACK
            writes = controller.stats["writes"]
            try:
                await cmds.settings_action(SAVE_SETTINGS)
                report.saved = True
            except ConnectionError:
                if controller.stats["writes"] == writes:  # never went out: nothing saved
                    driver.saved_this_run = False
                raise
            except SbgCommandError as exc:
                if exc.code is None:
                    report.errors.append(
                        "save: SAVE_SETTINGS sent, unconfirmed (no ACK; the unit may already "
                        "be rebooting)"
                    )
                else:
                    report.errors.append(f"save: {exc}")
                problems.append(report.errors[-1])
    if problems:
        controller.bus.publish("receiver.error", "INS configuration: " + "; ".join(problems))
    log.info(
        "SBG %s fw %s: %d applied, %d pending, %d mismatched, %d errors%s",
        report.info.product_code,
        report.info.firmware,
        len(report.applied),
        len(report.pending),
        len(report.mismatched),
        len(report.errors),
        ", saved to flash (unit reboots)" if report.saved else "",
    )
    driver.config_report = report
    controller.bus.publish("ins.config", report)
    return report


def make_configure(driver: SbgConfigTarget, settings: Settings) -> Configure:
    """The `InsController` configure hook: read-only unless `INS_APPLY_CONFIG=1`."""

    async def run(controller: InsController) -> None:
        await configure(controller, driver, settings, apply=settings.ins_apply_config)

    return run
