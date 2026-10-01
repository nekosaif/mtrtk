import asyncio
import struct
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import pytest

from mtrtk.core.bus import Bus
from mtrtk.rover.drivers.ins_common import InsController
from mtrtk.rover.drivers.sbg import commands as C
from mtrtk.rover.drivers.sbg import logs as L
from mtrtk.rover.drivers.sbg.framer import SbgFramer, encode
from mtrtk.rover.drivers.sbg.ids import CLASS, CMD, LOG
from sbgdevice import INVALID_PARAMETER, FakeEllipse


def info_payload(firmware: int = 0x03010000, hardware: int = 0x02000000) -> bytes:
    return b"ELLIPSE-D-G4A3-B1".ljust(32, b"\0") + struct.pack(
        "<IIHBBII", 12345, 3, 2025, 6, 1, hardware, firmware
    )


def test_encoders_match_sbgecom_layouts() -> None:
    assert C.encode_output_conf(0, CLASS["LOG_ECOM_0"], LOG["EKF_NAV"], 20) == bytes(
        [0, 8, 0]
    ) + struct.pack("<H", 20)
    assert C.encode_output_conf_selector(0, CLASS["LOG_ECOM_0"], LOG["EKF_NAV"]) == bytes([0, 8, 0])
    assert C.encode_output_class_enable(0, CLASS["LOG_NMEA_0"], False) == bytes([0, 2, 0])
    inst = C.GnssInstallation((0.5, 0.0, -1.2), True, (0.0, 0.0, 0.0), 1)
    assert C.encode_gnss_installation(inst) == struct.pack(
        "<3f?3fB", 0.5, 0.0, -1.2, True, 0, 0, 0, 1
    )
    assert C.decode_gnss_installation(C.encode_gnss_installation(inst)) == inst
    al = C.ImuAlignment(0, 3, 0.0, 0.0, 0.01, (0.1, 0.2, 0.3))
    assert C.encode_imu_alignment(al) == struct.pack(
        "<BBfff3f", 0, 3, 0.0, 0.0, 0.01, 0.1, 0.2, 0.3
    )
    assert C.decode_imu_alignment(C.encode_imu_alignment(al)) == al
    aid = C.AidingAssignment(
        gps1_port=5,
        gps1_sync=5,
        dvl_port=0xFF,
        dvl_sync=0,
        rtcm_port=1,
        air_data_port=0xFF,
        odometer_pins=0,
    )
    raw = C.encode_aiding_assignment(aid)
    # gps1Port gps1Sync reserved(u32) dvlPort dvlSync rtcmPort airDataPort odometerPinsConf
    assert len(raw) == 11 and raw[2:6] == b"\0\0\0\0" and raw[8] == 1
    assert C.decode_aiding_assignment(raw) == aid
    assert C.encode_init_parameters(23.7, 90.4, 12.0, date(2026, 9, 19)) == struct.pack(
        "<dddHBB", 23.7, 90.4, 12.0, 2026, 9, 19
    )
    init = C.decode_init_parameters(struct.pack("<dddHBB", 23.7, 90.4, 12.0, 2026, 9, 19))
    assert (init.latitude, init.longitude, init.altitude) == (23.7, 90.4, 12.0)
    assert init.date == date(2026, 9, 19)
    assert C.encode_motion_profile(7) == struct.pack("<I", 7)
    assert C.decode_motion_profile(struct.pack("<I", 7)) == 7
    assert C.encode_settings_action(C.SAVE_SETTINGS) == b"\x01"


def test_decode_f32_is_the_shortest_decimal() -> None:
    """A f32 lever arm reads back as the decimal that was written, not -1.2000000476837158."""
    inst = C.decode_gnss_installation(struct.pack("<3f?3fB", 0.5, 0.0, -1.2, False, 0.1, 0, 0, 4))
    assert inst.lever_arm_primary == (0.5, 0.0, -1.2)
    assert inst.lever_arm_secondary == (0.1, 0.0, 0.0)
    assert inst.primary_precise is False and inst.secondary_mode == 4


def test_decoders_reject_short_payloads() -> None:
    with pytest.raises(ValueError, match="INFO"):
        C.decode_info(b"\0" * 10)
    with pytest.raises(ValueError, match="AIDING_ASSIGNMENT"):
        C.decode_aiding_assignment(b"\0" * 10)
    with pytest.raises(ValueError, match="OUTPUT_CONF"):
        C.decode_output_conf(b"\0" * 4)


def test_decoders_accept_longer_payloads_from_newer_firmware() -> None:
    assert C.decode_motion_profile(struct.pack("<I", 2) + b"\xaa\xbb") == 2
    assert C.decode_info(info_payload() + b"\0\0").serial_number == 12345


def test_decode_info() -> None:
    info = C.decode_info(info_payload())
    assert info.product_code == "ELLIPSE-D-G4A3-B1" and info.serial_number == 12345
    assert info.firmware.startswith("3.1") and info.firmware == "3.1.0.0"
    assert info.firmware_raw == 0x03010000
    assert info.hardware_rev == "2.0.0.0" and info.hardware_rev_raw == 0x02000000
    assert info.calibration_rev == 3 and info.calibration_date == date(2025, 6, 1)


def test_info_type_is_the_log_parsers_one() -> None:
    """One SbgInfo for the INFO reply: the driver (Task 3) types `info` with logs.SbgInfo."""
    assert C.SbgInfo is L.SbgInfo
    parsed = L.parse_payload(CLASS["CMD_0"], CMD["INFO"], info_payload())
    assert C.decode_info(info_payload()) == parsed


def test_version_both_schemes() -> None:
    # sbgVersion.h: bit 31 set = software scheme (qualifier 3 bits @28, major 6 @22, minor 6 @16,
    # build 16 @0), printed "major.minor.build-qualifier"; otherwise four bytes "a.b.c.d".
    soft = (1 << 31) | (4 << 28) | (5 << 22) | (8 << 16) | 935
    assert L.decode_version(soft) == "5.8.935-stable"
    assert L.decode_version((1 << 31) | (5 << 28) | (3 << 22) | (1 << 16) | 2) == "3.1.2-hotfix"
    assert L.decode_version(0x01020304) == "1.2.3.4"


def test_decode_info_unset_calibration_date_is_none() -> None:
    raw = b"X".ljust(32, b"\0") + struct.pack("<IIHBBII", 1, 0, 0, 0, 0, 0, 0)
    assert C.decode_info(raw).calibration_date is None


def test_error_message_names_the_command_and_code() -> None:
    assert str(C.SbgCommandError(CMD["MOTION_PROFILE_ID"], 9)) == (
        "sbgECom MOTION_PROFILE_ID: error 9 (INVALID_PARAMETER)"
    )
    assert str(C.SbgCommandError(CMD["INFO"], None, "no reply after 3 attempts")) == (
        "sbgECom INFO: no reply after 3 attempts"
    )
    assert str(C.SbgCommandError(CMD["INFO"], None)) == "sbgECom INFO: timeout"


Body = Callable[[C.SbgCommands], Awaitable[Any]]


async def until(pred: Callable[[], bool], what: str) -> None:
    """An explicit sync point: yield to the loop until *pred* holds (no wall-clock margin)."""
    for _ in range(10_000):
        if pred():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"never happened: {what}")


def assert_no_waiter_resolved(cmds: C.SbgCommands) -> None:
    """A decoy that matched would resolve the waiter's future at once, but the awaiting task
    only resumes later: check the futures, not `task.done()`."""
    waiters = cmds.ctrl._waiters
    assert waiters and not any(fut.done() for _, fut in waiters)


async def run_with_device(dev: FakeEllipse, body: Body) -> Any:
    bus = Bus()
    ctrl = InsController(bus, lambda: dev, SbgFramer, None, rx_timeout_s=5)
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    await until(lambda: ctrl.connected, "the controller to connect")
    try:
        return await body(C.SbgCommands(ctrl))
    finally:
        stop.set()
        await task


async def test_get_and_set_roundtrip() -> None:
    dev = FakeEllipse()
    dev.put(CMD["MOTION_PROFILE_ID"], struct.pack("<I", 2))

    async def body(cmds: C.SbgCommands) -> None:
        assert await cmds.get_motion_profile() == 2
        await cmds.set_motion_profile(7)
        assert dev.sets[-1] == (CMD["MOTION_PROFILE_ID"], struct.pack("<I", 7))
        assert await cmds.get_motion_profile() == 7

    await run_with_device(dev, body)


async def test_typed_wrappers_roundtrip() -> None:
    dev = FakeEllipse()
    dev.put(CMD["INFO"], info_payload())
    dev.put(CMD["UART_CONF"], struct.pack("<BIB", 0, 921600, 1))
    dev.put(CMD["OUTPUT_CLASS_ENABLE"], bytes([0, CLASS["LOG_NMEA_0"], 1]))

    async def body(cmds: C.SbgCommands) -> None:
        assert (await cmds.get_info()).product_code == "ELLIPSE-D-G4A3-B1"
        assert await cmds.get_uart_conf(C.COM_A) == C.UartConf(0, 921600, 1)
        with pytest.raises(C.SbgCommandError) as ei:
            await cmds.get_output_conf(0, CLASS["LOG_ECOM_0"], LOG["EKF_NAV"])
        assert ei.value.code == INVALID_PARAMETER  # nothing stored: the unit NACKs the GET
        await cmds.set_output_conf(0, CLASS["LOG_ECOM_0"], LOG["EKF_NAV"], 20)
        assert await cmds.get_output_conf(0, CLASS["LOG_ECOM_0"], LOG["EKF_NAV"]) == 20
        assert await cmds.get_output_class_enable(0, CLASS["LOG_NMEA_0"]) is True
        await cmds.set_output_class_enable(0, CLASS["LOG_NMEA_0"], False)
        assert await cmds.get_output_class_enable(0, CLASS["LOG_NMEA_0"]) is False
        inst = C.GnssInstallation((0.5, 0.0, -1.2), True, (0.0, 0.0, 0.0), 1)
        await cmds.set_gnss_installation(inst)
        assert await cmds.get_gnss_installation() == inst
        al = C.ImuAlignment(0, 3, 0.0, 0.0, 0.01, (0.1, 0.2, 0.3))
        await cmds.set_imu_alignment(al)
        assert await cmds.get_imu_alignment() == al
        aid = C.AidingAssignment(5, 5, 0xFF, 0, 0, 0xFF, 0)
        await cmds.set_aiding_assignment(aid)
        assert await cmds.get_aiding_assignment() == aid
        await cmds.set_init_parameters(23.7, 90.4, 12.0, date(2026, 9, 19))
        assert (await cmds.get_init_parameters()).latitude == 23.7
        await cmds.settings_action(C.REBOOT_ONLY)
        assert dev.set_payloads(CMD["SETTINGS_ACTION"]) == [b"\x00"]

    await run_with_device(dev, body)


async def test_set_error_raises_without_retrying() -> None:
    dev = FakeEllipse()
    dev.ack_error[CMD["MOTION_PROFILE_ID"]] = 5

    async def body(cmds: C.SbgCommands) -> None:
        with pytest.raises(C.SbgCommandError) as ei:
            await cmds.set_motion_profile(99)
        assert ei.value.code == 5 and ei.value.cmd == CMD["MOTION_PROFILE_ID"]
        assert len(dev.sets) == 1  # an error ACK is an answer: sbgECom retries timeouts only

    await run_with_device(dev, body)


async def test_get_answered_by_an_ack_raises() -> None:
    """An error ACK for the command instead of its data refuses the GET (a code-0 ACK is
    ignored: see test_get_ignores_a_code_zero_ack)."""
    dev = FakeEllipse()

    async def body(cmds: C.SbgCommands) -> None:
        with pytest.raises(C.SbgCommandError) as ei:
            await cmds.get(CMD["MOTION_PROFILE_ID"])
        assert ei.value.code == INVALID_PARAMETER
        assert len(dev.gets) == 1  # an error ACK is an answer: not retried

    await run_with_device(dev, body)


async def test_get_ignores_a_code_zero_ack() -> None:
    """A code-0 ACK says nothing about a GET: it is the late ACK of a resent SET of the same
    command, so it must neither answer nor fail the GET."""
    dev = FakeEllipse()
    dev.silent = True

    async def body(cmds: C.SbgCommands) -> None:
        task = asyncio.create_task(cmds.get(CMD["MOTION_PROFILE_ID"], timeout_s=0.5, retries=1))
        await until(lambda: len(dev.written) == 1, "the GET to go out")
        frames = cmds.ctrl.stats["frames"]
        dev._ack(CMD["MOTION_PROFILE_ID"], 0)
        await until(lambda: cmds.ctrl.stats["frames"] == frames + 1, "the stale ACK to be routed")
        assert_no_waiter_resolved(cmds)
        dev.emit(encode(CLASS["CMD_0"], CMD["MOTION_PROFILE_ID"], struct.pack("<I", 7)))
        assert await task == struct.pack("<I", 7)

    await run_with_device(dev, body)


async def test_read_back_survives_a_late_ack_of_a_resent_set() -> None:
    """SET ACKed twice (the second one late, while the read-back GET waits): the read-back
    still returns the value."""
    dev = FakeEllipse()
    dev.put(CMD["MOTION_PROFILE_ID"], struct.pack("<I", 2))
    dev.late_ack.add(CMD["MOTION_PROFILE_ID"])
    sel = C.encode_output_conf_selector(0, CLASS["LOG_ECOM_0"], LOG["EKF_NAV"])
    dev.put(CMD["OUTPUT_CONF"], sel + struct.pack("<H", 0))
    dev.late_ack.add(CMD["OUTPUT_CONF"])

    async def body(cmds: C.SbgCommands) -> None:
        await cmds.set_motion_profile(7)
        assert await cmds.get_motion_profile() == 7
        await cmds.set_output_conf(0, CLASS["LOG_ECOM_0"], LOG["EKF_NAV"], 20)
        assert await cmds.get_output_conf(0, CLASS["LOG_ECOM_0"], LOG["EKF_NAV"]) == 20

    await run_with_device(dev, body)


async def test_get_timeout_retries_then_raises() -> None:
    dev = FakeEllipse()
    dev.silent = True  # a plain recorder: never answers

    async def body(cmds: C.SbgCommands) -> None:
        with pytest.raises(C.SbgCommandError) as ei:
            await cmds.get(CMD["INFO"], timeout_s=0.02, retries=2)
        assert ei.value.code is None
        assert len(dev.written) == 2

    await run_with_device(dev, body)


async def test_set_timeout_retries_then_raises() -> None:
    dev = FakeEllipse()
    dev.silent = True

    async def body(cmds: C.SbgCommands) -> None:
        with pytest.raises(C.SbgCommandError, match="no ACK after 3 attempts"):
            await cmds.set(CMD["MOTION_PROFILE_ID"], struct.pack("<I", 1), timeout_s=0.02)
        assert len(dev.written) == 3

    await run_with_device(dev, body)


async def test_replies_for_other_commands_and_logs_are_not_matched() -> None:
    dev = FakeEllipse()
    dev.silent = True

    async def body(cmds: C.SbgCommands) -> None:
        task = asyncio.create_task(cmds.set(CMD["MOTION_PROFILE_ID"], b"\1\0\0\0", timeout_s=0.5))
        await until(lambda: len(dev.written) == 1, "the SET to go out")
        frames = cmds.ctrl.stats["frames"]
        dev.emit(encode(0, LOG["EKF_NAV"], b"\0" * 72))  # a log with id 7 would also be a trap
        dev.emit(encode(0, CMD["MOTION_PROFILE_ID"], b"\0" * 4))  # class 0 id 7 = EKF_QUAT
        dev._ack(CMD["AIDING_ASSIGNMENT"], 0)  # another command's ACK
        bad_class = struct.pack("<BBH", CMD["MOTION_PROFILE_ID"], 0x00, 0)
        dev.emit(encode(CLASS["CMD_0"], CMD["ACK"], bad_class))  # ACK for class 0, not CMD_0
        await until(lambda: cmds.ctrl.stats["frames"] == frames + 4, "the decoys to be routed")
        assert_no_waiter_resolved(cmds)
        dev._ack(CMD["MOTION_PROFILE_ID"], 0)
        await task

    await run_with_device(dev, body)


async def test_get_ignores_a_class_0_log_with_the_same_id() -> None:
    """Class 0 id 7 is EKF_QUAT: a streaming log must not answer GET MOTION_PROFILE_ID (id 7)."""
    dev = FakeEllipse()
    dev.silent = True

    async def body(cmds: C.SbgCommands) -> None:
        task = asyncio.create_task(cmds.get_motion_profile())
        await until(lambda: len(dev.written) == 1, "the GET to go out")
        frames = cmds.ctrl.stats["frames"]
        dev.emit(encode(CLASS["LOG_ECOM_0"], CMD["MOTION_PROFILE_ID"], struct.pack("<I", 99)))
        await until(lambda: cmds.ctrl.stats["frames"] == frames + 1, "the log to be routed")
        assert_no_waiter_resolved(cmds)
        dev.emit(encode(CLASS["CMD_0"], CMD["MOTION_PROFILE_ID"], struct.pack("<I", 7)))
        assert await task == 7

    await run_with_device(dev, body)


async def test_commands_on_one_controller_are_serialised() -> None:
    """ACKs carry no selector and are matched by command id: two users of one controller (the
    configure hook, a CLI or API read) must never have commands outstanding at the same time."""
    dev = FakeEllipse()
    dev.silent = True

    async def body(cmds: C.SbgCommands) -> None:
        other = C.SbgCommands(cmds.ctrl)
        first = asyncio.create_task(cmds.get_motion_profile())
        second = asyncio.create_task(other.get_motion_profile())
        await until(lambda: len(dev.written) >= 1, "the first GET to go out")
        for _ in range(100):
            await asyncio.sleep(0)
        assert len(dev.written) == 1  # the second waits for the first to be answered
        dev.emit(encode(CLASS["CMD_0"], CMD["MOTION_PROFILE_ID"], struct.pack("<I", 7)))
        assert await first == 7
        await until(lambda: len(dev.written) == 2, "the second GET to go out")
        dev.emit(encode(CLASS["CMD_0"], CMD["MOTION_PROFILE_ID"], struct.pack("<I", 2)))
        assert await second == 2

    await run_with_device(dev, body)


async def test_get_matches_the_echoed_selector() -> None:
    """A late OUTPUT_CONF reply for another message must not answer this GET."""
    dev = FakeEllipse()
    dev.silent = True
    nav = C.encode_output_conf(0, CLASS["LOG_ECOM_0"], LOG["EKF_NAV"], 20)
    euler = C.encode_output_conf(0, CLASS["LOG_ECOM_0"], LOG["EKF_EULER"], 40)

    async def body(cmds: C.SbgCommands) -> None:
        task = asyncio.create_task(cmds.get_output_conf(0, CLASS["LOG_ECOM_0"], LOG["EKF_NAV"]))
        await until(lambda: len(dev.written) == 1, "the GET to go out")
        frames = cmds.ctrl.stats["frames"]
        dev.emit(encode(CLASS["CMD_0"], CMD["OUTPUT_CONF"], euler))
        await until(lambda: cmds.ctrl.stats["frames"] == frames + 1, "the decoy to be routed")
        assert_no_waiter_resolved(cmds)
        dev.emit(encode(CLASS["CMD_0"], CMD["OUTPUT_CONF"], nav))
        assert await task == 20

    await run_with_device(dev, body)


async def test_malformed_reply_is_a_command_error() -> None:
    dev = FakeEllipse()
    dev.put(CMD["INFO"], b"short")

    async def body(cmds: C.SbgCommands) -> None:
        with pytest.raises(C.SbgCommandError, match="malformed reply"):
            await cmds.get_info()

    await run_with_device(dev, body)


async def test_not_connected_raises_connection_error() -> None:
    ctrl = InsController(Bus(), FakeEllipse, SbgFramer, None)
    with pytest.raises(ConnectionError):
        await C.SbgCommands(ctrl).get_info()
