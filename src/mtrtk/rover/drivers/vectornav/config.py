"""VN-200 configuration: the profile mtrtk wants, read-only inspection, and apply with read-back.

`configure(controller, driver, settings, apply=...)` always reads the unit's identity
(model, hardware revision, serial, firmware -> `driver.info`), its async ASCII output type,
binary output 1 and the GNSS antenna offset, plus whichever optional registers the
`INS_VN_*` settings ask for. Each profile item whose current value differs from the wanted
one is listed in `pending` (read-only) or, with `apply`, written, read back and filed under
`applied` or `mismatched`. Binary output 1 is probed: first with SatInfo and RawMeas (when
`INS_RAW_GNSS=1`), then without RawMeas, then without SatInfo, on each `VnError(7)`
(InvalidParameter); the driver's capabilities follow what the unit kept. Settings go to
flash (`$VNWNV`, no reboot) once per process, when something was applied. The report is
stored on `driver.config_report` and published as `ins.config`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from mtrtk.config import Settings
from mtrtk.rover.drivers.ins_common import InsController
from mtrtk.rover.drivers.vectornav.driver import VnDriver, VnInfo
from mtrtk.rover.drivers.vectornav.fields import EXT_RAWMEAS
from mtrtk.rover.drivers.vectornav.registers import BinaryOutputConf, VnError, VnRegisters

log = logging.getLogger(__name__)

IMU_RATE_HZ = 800  # the divisor base of the binary outputs
SUPPORTED_HZ = (1, 2, 4, 5, 8, 10, 16, 20, 25, 32, 40, 50, 80, 100, 160, 200, 400, 800)
DIVISORS = tuple(sorted(IMU_RATE_HZ // hz for hz in SUPPORTED_HZ))
DEFAULT_ASYNC_MODE = 1  # serial port 1, used when output 1 is currently off
INVALID_PARAMETER = 7
SATINFO = 1 << 14  # GPS group bit 14
FLOAT_TOL = 1e-4

# Binary output 1, per group: Time (TimeGps, GpsTow, GpsWeek, TimeSyncIn, TimeUTC, SyncInCnt,
# TimeStatus), IMU (ImuStatus, Temp, Accel, AngularRate), GPS (Tow, NumSats, Fix, PosLla,
# VelNed, PosU, TimeU, TimeInfo, DOP, SatInfo), Attitude (VpeStatus, YawPitchRoll, YprU),
# INS (InsStatus, PosLla, VelNed, PosU, VelU).
DEFAULT_FIELDS: dict[str, int] = {
    "time": 0x02DE,
    "imu": 0x0611,
    "gps": 0x7ABA,
    "attitude": 0x0103,
    "ins": 0x0613,
}


def divisor_for_hz(hz: int) -> int:
    """The rate divisor for *hz*: exact for a supported rate, else the next faster one."""
    if hz <= 0:
        raise ValueError(f"output rate must be > 0 Hz, got {hz}")
    ideal = IMU_RATE_HZ / hz
    fitting = [d for d in DIVISORS if d <= ideal]
    return fitting[-1] if fitting else DIVISORS[0]


@dataclass(frozen=True)
class VnProfile:
    divisor: int
    fields: dict[str, int]
    gps_ext: int | None
    antenna_offset: tuple[float, float, float] | None
    vpe: tuple[int, int, int, int] | None
    ins_basic: tuple[int | None, bool | None] | None
    ref_rotation: tuple[float, ...] | None
    note: str | None = None


def vn_profile(settings: Settings) -> VnProfile:
    hz = settings.ins_output_hz
    divisor = divisor_for_hz(hz)
    note = None
    if IMU_RATE_HZ // divisor != hz or IMU_RATE_HZ % divisor:
        note = (
            f"INS_OUTPUT_HZ={hz} is not a VectorNav output rate: using "
            f"{IMU_RATE_HZ / divisor:g} Hz (divisor {divisor})"
        )
    vpe = None
    if settings.ins_vn_vpe is not None:
        a, b, c, d = (int(p) for p in settings.ins_vn_vpe.split(","))
        vpe = (a, b, c, d)
    ins_basic = None
    if settings.ins_vn_scenario is not None or settings.ins_vn_ahrs_aiding is not None:
        ins_basic = (settings.ins_vn_scenario, settings.ins_vn_ahrs_aiding)
    rotation = None
    if settings.ins_vn_ref_rotation is not None:
        rotation = tuple(float(p) for p in settings.ins_vn_ref_rotation.split(","))
    return VnProfile(
        divisor=divisor,
        fields=dict(DEFAULT_FIELDS),
        gps_ext=EXT_RAWMEAS if settings.ins_raw_gnss else None,
        antenna_offset=settings.ins_lever_arm_gnss1,
        vpe=vpe,
        ins_basic=ins_basic,
        ref_rotation=rotation,
        note=note,
    )


@dataclass
class VnConfigReport:
    info: VnInfo | None = None
    applied: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)  # would be written (read-only run)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    current: dict[str, Any] = field(default_factory=dict)
    sat_info: bool | None = None  # binary output 1 carries SatInfo (None = unknown)
    raw_meas: bool | None = None  # ... and RawMeas
    saved: bool = False


def _close(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return len(a) == len(b) and all(abs(x - y) <= FLOAT_TOL for x, y in zip(a, b, strict=True))


def _outputs(conf: BinaryOutputConf) -> tuple[bool, bool]:
    """(SatInfo, RawMeas) carried by a binary output configuration."""
    if conf.async_mode == 0 or "gps" not in conf.fields:
        return False, False
    return bool(conf.fields["gps"] & SATINFO), bool((conf.gps_ext or 0) & EXT_RAWMEAS)


class _Run:
    def __init__(
        self, controller: InsController, driver: VnDriver, profile: VnProfile, apply: bool
    ) -> None:
        self.bus = controller.bus
        self.regs = VnRegisters(controller)
        self.driver = driver
        self.profile = profile
        self.apply = apply
        self.report = VnConfigReport(notes=[profile.note] if profile.note else [])

    def error(self, msg: str, *, alert: bool = False) -> None:
        log.warning("VectorNav: %s", msg)
        self.report.errors.append(msg)
        if alert:
            self.bus.publish("receiver.error", f"VectorNav {msg}")

    async def info(self) -> bool:
        """Read the identity registers. False when the unit does not answer at all."""
        info = VnInfo()
        readers: list[tuple[str, Callable[[], Awaitable[str]]]] = [
            ("model", self.regs.model),
            ("hardware_revision", self.regs.hardware_revision),
            ("serial", self.regs.serial),
            ("firmware", self.regs.firmware),
        ]
        for attr, read in readers:
            try:
                setattr(info, attr, await read())
            except VnError as exc:
                self.error(f"{attr}: {exc}")
            except TimeoutError as exc:
                self.error(f"{attr}: {exc}", alert=True)
                if attr == "model":
                    self.driver.info = self.report.info = info
                    return False  # nothing answers: wrong port or baud, skip the rest
        self.driver.info = self.report.info = info
        return True

    async def item(
        self,
        name: str,
        read: Callable[[], Awaitable[Any]],
        desired: Any,
        write: Callable[[Any], Awaitable[None]],
        same: Callable[[Any, Any], bool],
    ) -> None:
        try:
            current = await read()
        except (VnError, TimeoutError) as exc:
            self.error(f"{name}: read failed: {exc}")
            return
        self.report.current[name] = current
        if desired is None:
            return
        if same(current, desired):
            self.report.unchanged.append(name)
            return
        if not self.apply:
            self.report.pending.append(name)
            return
        try:
            await write(desired)
        except (VnError, TimeoutError) as exc:
            self.error(f"{name}: write failed: {exc}", alert=True)
            return
        await self.verify(name, read, desired, same)

    async def verify(
        self,
        name: str,
        read: Callable[[], Awaitable[Any]],
        desired: Any,
        same: Callable[[Any, Any], bool],
    ) -> bool:
        try:
            back = await read()
        except (VnError, TimeoutError) as exc:
            self.error(f"{name}: read-back failed: {exc}")
            return False
        self.report.current[name] = back
        if same(back, desired):
            self.report.applied.append(name)
            return True
        self.report.mismatched.append(name)
        self.error(f"{name}: read back {back!r}, wrote {desired!r}", alert=True)
        return False

    async def binary_output(self) -> None:
        name = "binary_output_1"
        regs, p = self.regs, self.profile
        try:
            current = await regs.read_binary_output(1)
        except (VnError, TimeoutError, ValueError, IndexError) as exc:
            self.error(f"{name}: read failed: {exc}")
            return
        self.report.current[name] = asdict(current)
        self._follow(current)
        mode = current.async_mode or DEFAULT_ASYNC_MODE
        if current.async_mode:
            self.report.notes.append(f"binary output 1 kept on async mode {mode}")
        without_sat = dict(p.fields, gps=p.fields["gps"] & ~SATINFO)
        candidates = [BinaryOutputConf(mode, p.divisor, dict(p.fields), p.gps_ext)]
        if p.gps_ext is not None:
            candidates.append(BinaryOutputConf(mode, p.divisor, dict(p.fields), None))
        candidates.append(BinaryOutputConf(mode, p.divisor, without_sat, None))
        if current == candidates[0]:
            self.report.unchanged.append(name)
            return
        if not self.apply:
            self.report.pending.append(name)
            return
        for want in candidates:
            if want == current:  # an earlier, richer variant was refused: this one is in place
                self.report.unchanged.append(name)
                return
            try:
                await regs.write_binary_output(
                    1, want.async_mode, want.divisor, want.fields, want.gps_ext
                )
            except VnError as exc:
                if exc.code == INVALID_PARAMETER:
                    log.info("VectorNav refused binary output %s: trying less", want)
                    continue
                self.error(f"{name}: write failed: {exc}", alert=True)
                return
            except TimeoutError as exc:
                self.error(f"{name}: write failed: {exc}", alert=True)
                return
            if want is not candidates[0]:
                dropped = "RawMeas" if want.fields["gps"] & SATINFO else "RawMeas and SatInfo"
                self.report.notes.append(f"the unit refused {dropped} in binary output 1")
            if await self.verify(
                name, lambda: regs.read_binary_output(1), want, lambda a, b: a == b
            ):
                self._follow(want)
                self.report.current[name] = asdict(want)
            return
        self.error(f"{name}: refused by the unit in every variant (VnError 7)", alert=True)

    def _follow(self, conf: BinaryOutputConf) -> None:
        sat, raw = _outputs(conf)
        self.driver.sat_info_output = self.report.sat_info = sat
        self.driver.raw_meas_output = self.report.raw_meas = raw

    async def run(self) -> VnConfigReport:
        regs, p = self.regs, self.profile
        if await self.info():
            await self.item(
                "async_output_type",
                regs.async_output_type,
                0,
                regs.set_async_output_type,
                lambda a, b: a == b,
            )
            await self.binary_output()
            await self.item(
                "antenna_offset",
                regs.read_antenna_offset,
                p.antenna_offset,
                lambda v: regs.set_antenna_offset(*v),
                _close,
            )
            if p.ins_basic is not None:
                await self._ins_basic(p.ins_basic)
            if p.vpe is not None:
                await self.item(
                    "vpe",
                    regs.read_vpe_basic_control,
                    p.vpe,
                    lambda v: regs.set_vpe_basic_control(*v),
                    lambda a, b: tuple(a[:4]) == tuple(b),
                )
            if p.ref_rotation is not None:
                await self.item(
                    "ref_rotation",
                    regs.read_reference_frame_rotation,
                    p.ref_rotation,
                    regs.set_reference_frame_rotation,
                    _close,
                )
            await self.save()
        self.driver.config_report = self.report
        self.bus.publish("ins.config", self.report)
        return self.report

    async def _ins_basic(self, wanted: tuple[int | None, bool | None]) -> None:
        regs = self.regs
        try:
            current = await regs.read_ins_basic_config()
        except (VnError, TimeoutError) as exc:
            self.error(f"ins_basic: read failed: {exc}")
            return
        scenario = wanted[0] if wanted[0] is not None else current[0]
        aiding = wanted[1] if wanted[1] is not None else bool(current[1])
        await self.item(
            "ins_basic",
            regs.read_ins_basic_config,
            (scenario, aiding),
            lambda v: regs.set_ins_basic_config(*v),
            lambda a, b: a[0] == b[0] and bool(a[1]) == bool(b[1]),
        )

    async def save(self) -> None:
        if not (self.apply and self.report.applied) or self.driver.saved_this_run:
            return
        try:
            await self.regs.write_settings()
        except (VnError, TimeoutError) as exc:
            self.error(f"write settings to flash failed: {exc}", alert=True)
            return
        self.driver.saved_this_run = self.report.saved = True


async def configure(
    controller: InsController, driver: VnDriver, settings: Settings, *, apply: bool
) -> VnConfigReport:
    """Inspect the unit and (with *apply*) bring it to the mtrtk profile. A link drop raises
    `ConnectionError`; every unit-side refusal or silence is recorded in the report."""
    return await _Run(controller, driver, vn_profile(settings), apply).run()
