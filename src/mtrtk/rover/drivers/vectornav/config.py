"""VN-200 configuration: the profile mtrtk wants, read-only inspection, and apply with read-back.

`configure(controller, driver, settings, apply=...)` always reads the unit's identity
(model, hardware revision, serial, firmware -> `driver.info`), binary output 1, its async
ASCII output type and the GNSS antenna offset, plus whichever optional registers the
`INS_VN_*` settings ask for. Each profile item whose current value differs from the wanted
one is listed in `pending` (read-only) or, with `apply`, written, read back and filed under
`applied` or `mismatched`.

Binary output 1 comes first. It is probed: first with SatInfo and RawMeas (when
`INS_RAW_GNSS=1`), then without RawMeas, then without SatInfo, on each refusal that names the
request's size or content (`FALLBACK_ERRORS`: InvalidParameter, TooManyParameters,
InsufficientBaudRate); the driver's capabilities follow what the unit kept. Once it is in
place, configure waits up to `STREAM_WAIT_S` for a binary frame on mtrtk's own port. Only
then is ASCII async output turned off: a unit whose binary output could not be set, or
streams to the other serial port, keeps talking ASCII instead of going silent.

Settings go to flash (`$VNWNV`, no reboot) once per process, when `INS_APPLY_CONFIG=1`,
something was applied, no read-back mismatched and binary output 1 streams. An explicit apply
with `INS_APPLY_CONFIG=0` (the UI confirm, `mtrtk ins config --apply`) writes RAM only, as SBG
does: the changes last until the unit restarts. RTCM forwarding is held while configure
runs, so correction bytes cannot interleave with (or be blamed for) the register exchange.
The report is stored on `driver.config_report` and published as `ins.config`. Every
unit-side refusal, silence or unreadable reply is recorded in the report, never raised.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from mtrtk.config import Settings
from mtrtk.rover.drivers.ins_common import InsController
from mtrtk.rover.drivers.vectornav.driver import VnDriver, VnInfo
from mtrtk.rover.drivers.vectornav.fields import (
    EXT_RAWMEAS,
    FIELD_SIZES,
    GROUP_BITS,
    RAWMEAS_HEADER,
    RAWMEAS_RECORD,
    SATINFO_BIT,
    SATINFO_RECORD,
)
from mtrtk.rover.drivers.vectornav.registers import BinaryOutputConf, VnError, VnRegisters

log = logging.getLogger(__name__)

IMU_RATE_HZ = 800  # the divisor base of the binary outputs
SUPPORTED_HZ = (1, 2, 4, 5, 8, 10, 16, 20, 25, 32, 40, 50, 80, 100, 160, 200, 400, 800)
DIVISORS = tuple(sorted(IMU_RATE_HZ // hz for hz in SUPPORTED_HZ))
DEFAULT_ASYNC_MODE = 1  # serial port 1, used when output 1 is currently off
INVALID_PARAMETER = 7
# Refusals that mean "not this output": try the next, smaller variant. VERIFY(vn-baud-error)
# which code a VN-200 gives for an output that does not fit the baud rate (12 per the
# manual's list).
FALLBACK_ERRORS = frozenset((6, INVALID_PARAMETER, 12))
SATINFO = 1 << SATINFO_BIT  # GPS group bit 14
FLOAT_TOL = 1e-4
STREAM_WAIT_S = 2.0  # at least; also two output periods at the configured divisor
# The frame budget is checked at these counts: an open-sky multi-GNSS fix tracks about 25
# satellites, and RawMeas carries one record per signal (dual frequency: about two each).
BUDGET_SATS, BUDGET_MEAS = 25, 50
BUDGET_SHARE = 0.8  # of the line rate, leaving room for ASCII replies

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


def frame_bytes(fields: dict[str, int], gps_ext: int | None, *, sats: int, meas: int) -> int:
    """Bytes in one binary output frame with *fields*, sync to CRC, carrying *sats* SatInfo
    records and *meas* RawMeas records (when those fields are present)."""
    total = 1 + 1 + 2  # sync, groups byte, CRC
    for name, mask in fields.items():
        g = GROUP_BITS[name]
        total += 2
        sizes = FIELD_SIZES[g]
        total += sum(sizes[bit] for bit in range(15) if mask & (1 << bit))
        if name == "gps":
            if mask & SATINFO:
                total += SATINFO_RECORD * sats
            if gps_ext:
                total += 2  # the extension word
                if gps_ext & EXT_RAWMEAS:
                    total += RAWMEAS_HEADER + RAWMEAS_RECORD * meas
    return total


def _budget_note(settings: Settings, divisor: int, fields: dict[str, int], ext: int | None) -> str:
    """A note when binary output 1 at the configured rate would not fit `INS_BAUD` (8N1)."""
    hz = IMU_RATE_HZ / divisor
    line = settings.ins_baud / 10
    need = frame_bytes(fields, ext, sats=BUDGET_SATS, meas=BUDGET_MEAS) * hz
    if need <= BUDGET_SHARE * line:
        return ""
    carried = "SatInfo and RawMeas" if ext else "SatInfo"
    return (
        f"binary output 1 at {hz:g} Hz with {carried} needs about {need:.0f} B/s with "
        f"{BUDGET_SATS} satellites"
        + (f" and {BUDGET_MEAS} measurements" if ext else "")
        + f"; INS_BAUD={settings.ins_baud} carries about {line:.0f} B/s: raise INS_BAUD (and "
        "the unit's register 5) or lower INS_OUTPUT_HZ, or the unit may refuse the output "
        "or drop frames"
    )


@dataclass(frozen=True)
class VnProfile:
    divisor: int
    fields: dict[str, int]
    gps_ext: int | None
    antenna_offset: tuple[float, float, float] | None
    vpe: tuple[int, int, int, int] | None
    ins_basic: tuple[int | None, bool | None] | None
    ref_rotation: tuple[float, ...] | None
    note: str | None = None  # the output rate was rounded
    notes: tuple[str, ...] = ()  # other things the report should say (frame budget, ...)


def vn_profile(settings: Settings) -> VnProfile:
    hz = settings.ins_output_hz
    divisor = divisor_for_hz(hz)
    note = None
    if IMU_RATE_HZ // divisor != hz or IMU_RATE_HZ % divisor:
        note = (
            f"INS_OUTPUT_HZ={hz} is not a VectorNav output rate: using "
            f"{IMU_RATE_HZ / divisor:g} Hz (divisor {divisor})"
        )
    gps_ext = EXT_RAWMEAS if settings.ins_raw_gnss else None
    notes: list[str] = []
    budget = _budget_note(settings, divisor, DEFAULT_FIELDS, gps_ext)
    if budget:
        notes.append(budget)
    if settings.ins_motion_profile != "general":
        notes.append(
            f"INS_MOTION_PROFILE={settings.ins_motion_profile} is not used by the VectorNav "
            "driver: set INS_VN_SCENARIO (register 67) instead"
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
        gps_ext=gps_ext,
        antenna_offset=settings.ins_lever_arm_gnss1,
        vpe=vpe,
        ins_basic=ins_basic,
        ref_rotation=rotation,
        note=note,
        notes=tuple(notes),
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
    wanted: dict[str, Any] = field(default_factory=dict)  # the profile value of each differing item
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


# A reply the typed readers cannot decode (short, or another number format): recorded as an
# "unreadable reply" error, like a refusal, instead of escaping configure.
_UNREADABLE = (ValueError, IndexError, TypeError)


def _same(same: Callable[[Any, Any], bool], a: Any, b: Any) -> bool:
    try:
        return same(a, b)
    except _UNREADABLE:
        return False


def _dropped(full: BinaryOutputConf, kept: BinaryOutputConf) -> str:
    """What *kept* lacks of *full*: "RawMeas", "SatInfo" or both."""
    full_sat, full_raw = _outputs(full)
    kept_sat, kept_raw = _outputs(kept)
    pairs = (("RawMeas", full_raw, kept_raw), ("SatInfo", full_sat, kept_sat))
    return " and ".join(name for name, had, has in pairs if had and not has)


class _Run:
    def __init__(
        self,
        controller: InsController,
        driver: VnDriver,
        profile: VnProfile,
        apply: bool,
        *,
        save: bool = True,
    ) -> None:
        self.bus = controller.bus
        self.regs = VnRegisters(controller)
        self.driver = driver
        self.profile = profile
        self.apply = apply
        self.save_allowed = save  # INS_APPLY_CONFIG: a forced apply writes RAM only
        self.report = VnConfigReport(
            notes=([profile.note] if profile.note else []) + list(profile.notes)
        )
        self.streaming = False  # binary output 1 is in place and reaches mtrtk's port

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

    async def read(self, name: str, read: Callable[[], Awaitable[Any]], what: str) -> Any:
        """*read*(), or None with the failure recorded."""
        try:
            return await read()
        except (VnError, TimeoutError) as exc:
            self.error(f"{name}: {what} failed: {exc}")
        except _UNREADABLE as exc:
            self.error(f"{name}: unreadable reply ({what}): {exc!r}")
        return None

    async def item(
        self,
        name: str,
        read: Callable[[], Awaitable[Any]],
        desired: Any,
        write: Callable[[Any], Awaitable[None]],
        same: Callable[[Any, Any], bool],
    ) -> None:
        current = await self.read(name, read, "read")
        if current is None:
            return
        self.report.current[name] = current
        if desired is None:
            return
        await self.change(name, current, read, desired, write, same)

    async def change(
        self,
        name: str,
        current: Any,
        read: Callable[[], Awaitable[Any]],
        desired: Any,
        write: Callable[[Any], Awaitable[None]],
        same: Callable[[Any, Any], bool],
    ) -> None:
        if _same(same, current, desired):
            self.report.unchanged.append(name)
            return
        self.report.wanted[name] = desired
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
        back = await self.read(name, read, "read-back")
        if back is None:
            return False
        self.report.current[name] = back
        if _same(same, back, desired):
            self.report.applied.append(name)
            return True
        self.report.mismatched.append(name)
        self.error(f"{name}: read back {back!r}, wrote {desired!r}", alert=True)
        return False

    async def binary_output(self) -> BinaryOutputConf | None:
        """Bring binary output 1 to the profile. Returns the configuration now in place when
        it matches the profile (or the variant the unit kept); None when it does not, or this
        is a read-only run that would change it."""
        name = "binary_output_1"
        regs, p = self.regs, self.profile
        current: BinaryOutputConf | None = await self.read(
            name, lambda: regs.read_binary_output(1), "read"
        )
        if current is None:
            return None
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
            return current
        self.report.wanted[name] = asdict(candidates[0])
        if not self.apply:
            self.report.pending.append(name)
            return None
        refused: VnError | None = None
        for want in candidates:
            if want == current:  # an earlier, richer variant was refused: this one is in place
                self.report.unchanged.append(name)
                self.report.wanted.pop(name, None)
                return current
            try:
                await regs.write_binary_output(
                    1, want.async_mode, want.divisor, want.fields, want.gps_ext
                )
            except VnError as exc:
                if exc.code in FALLBACK_ERRORS:
                    log.info("VectorNav refused binary output %s (%s): trying less", want, exc)
                    refused = exc
                    continue
                self.error(f"{name}: write failed: {exc}", alert=True)
                return None
            except TimeoutError as exc:
                self.error(f"{name}: write failed: {exc}", alert=True)
                return None
            if refused is not None:
                self.report.notes.append(
                    f"the unit refused {_dropped(candidates[0], want)} in binary output 1 "
                    f"({refused.name})"
                )
            self.report.wanted[name] = asdict(want)  # the variant the unit was sent
            if await self.verify(
                name, lambda: regs.read_binary_output(1), want, lambda a, b: a == b
            ):
                self._follow(want)
                self.report.current[name] = asdict(want)
                return want
            return None
        codes = "/".join(str(c) for c in sorted(FALLBACK_ERRORS))
        self.error(f"{name}: refused by the unit in every variant (VnError {codes})", alert=True)
        return None

    async def check_stream(self, conf: BinaryOutputConf) -> bool:
        """Wait for one binary frame on this port: binary output 1 really reaches mtrtk."""
        wait = max(STREAM_WAIT_S, 2.0 * conf.divisor / IMU_RATE_HZ)
        if await self.regs.await_binary(wait):
            return True
        self.error(
            f"binary output 1 is set (async mode {conf.async_mode}: 1 = serial 1, 2 = serial 2,"
            f" 3 = both) but no binary frame arrived on this port within {wait:g}s; is mtrtk "
            "on the other serial port? ASCII output left as it is, nothing saved",
            alert=True,
        )
        return False

    async def async_output(self) -> None:
        """Turn ASCII async output off, but only once binary output 1 streams here."""
        name = "async_output_type"
        regs = self.regs
        current = await self.read(name, regs.async_output_type, "read")
        if current is None:
            return
        self.report.current[name] = current
        if self.apply and current != 0 and not self.streaming:
            self.report.notes.append(
                f"ASCII async output (register 6 = {current}) left on: binary output 1 is not "
                "streaming to mtrtk"
            )
            return
        await self.change(name, current, regs.async_output_type, 0, regs.set_async_output_type, _eq)

    def _follow(self, conf: BinaryOutputConf) -> None:
        sat, raw = _outputs(conf)
        self.driver.sat_info_output = self.report.sat_info = sat
        self.driver.raw_meas_output = self.report.raw_meas = raw

    async def run(self) -> VnConfigReport:
        regs, p = self.regs, self.profile
        if await self.info():
            conf = await self.binary_output()
            if conf is not None:
                self.streaming = await self.check_stream(conf)
            await self.async_output()
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
                    lambda a, b: len(a) >= 4 and tuple(a[:4]) == tuple(b),
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
        name = "ins_basic"
        regs = self.regs
        current = await self.read(name, regs.read_ins_basic_config, "read")
        if current is None:
            return
        self.report.current[name] = current
        if len(current) < 2:
            self.error(f"{name}: unreadable reply (read): {current!r}, expected 4 fields")
            return
        scenario = wanted[0] if wanted[0] is not None else current[0]
        aiding = wanted[1] if wanted[1] is not None else bool(current[1])
        await self.change(
            name,
            current,
            regs.read_ins_basic_config,
            (scenario, aiding),
            lambda v: regs.set_ins_basic_config(*v),
            lambda a, b: a[0] == b[0] and bool(a[1]) == bool(b[1]),
        )

    async def save(self) -> None:
        if not (self.apply and self.report.applied) or self.driver.saved_this_run:
            return
        if not self.save_allowed:
            self.report.notes.append(
                "settings not saved to flash (INS_APPLY_CONFIG=0): the applied changes last "
                "until the unit restarts"
            )
            return
        why = []
        if self.report.mismatched:
            why.append(f"{', '.join(self.report.mismatched)} read back different")
        if not self.streaming:
            why.append("binary output 1 is not streaming to mtrtk")
        if why:
            self.report.notes.append(
                f"settings not saved to flash ({'; '.join(why)}): the applied changes last "
                "until the unit restarts"
            )
            return
        try:
            await self.regs.write_settings()
        except (VnError, TimeoutError) as exc:
            self.error(f"write settings to flash failed: {exc}", alert=True)
            return
        self.driver.saved_this_run = self.report.saved = True


def _eq(a: Any, b: Any) -> bool:
    return bool(a == b)


async def configure(
    controller: InsController, driver: VnDriver, settings: Settings, *, apply: bool
) -> VnConfigReport:
    """Inspect the unit and (with *apply*) bring it to the mtrtk profile. A link drop raises
    `ConnectionError`; every unit-side refusal or silence is recorded in the report. RTCM
    forwarding is held (dropped and counted) for the duration."""
    driver.configuring = True
    try:
        return await _Run(
            controller, driver, vn_profile(settings), apply, save=settings.ins_apply_config
        ).run()
    finally:
        driver.configuring = False
