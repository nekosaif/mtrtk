import asyncio
import struct
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import pytest

from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.rover.drivers.ins_common import InsController
from mtrtk.rover.drivers.sbg import commands as C
from mtrtk.rover.drivers.sbg import config as K
from mtrtk.rover.drivers.sbg.framer import SbgFramer
from mtrtk.rover.drivers.sbg.ids import CLASS, CMD, LOG
from sbgdevice import FakeEllipse

ECOM0 = CLASS["LOG_ECOM_0"]


@pytest.fixture(autouse=True)
def _no_ins_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bench shell may export INS_* / ROVER_DRIVER: the settings under test come from here."""
    for key in ["ROLE", "ROVER_DRIVER", "NTRIP_URL"]:
        monkeypatch.delenv(key, raising=False)
    for name in Settings.model_fields:
        if name.startswith("ins_"):
            monkeypatch.delenv(name.upper(), raising=False)


def make(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "role": "rover",
        "rover_driver": "sbg_ellipse",
        "ins_port": "/dev/x",
        "ins_output_hz": 10,
        "ins_lever_arm_gnss1": "0.5,0,-1.2",
        "ins_motion_profile": "uav",
        "ins_raw_gnss": False,
    }
    base.update(kw)
    return Settings(_env_file=None, **base)


class Driver:
    def __init__(self) -> None:
        self.info: C.SbgInfo | None = None
        self.saved_this_run = False
        self.config_report: K.SbgConfigReport | None = None


def test_hz_to_mode() -> None:
    assert K.hz_to_mode(10) == 20
    assert K.hz_to_mode(200) == 1
    assert K.hz_to_mode(1) == 200
    table = {200: 1, 100: 2, 50: 4, 40: 5, 25: 8, 20: 10, 10: 20, 5: 40, 2: 100, 1: 200}
    assert all(K.hz_to_mode(hz) == mode for hz, mode in table.items())
    with pytest.raises(ValueError, match="7 Hz"):
        K.hz_to_mode(7)


def test_profile_from_settings() -> None:
    p = K.sbg_profile(make())
    assert p.output_hz == 10
    assert (ECOM0, LOG["EKF_NAV"], 20) in p.outputs
    assert (ECOM0, LOG["IMU_SHORT"], 20) in p.outputs
    assert (ECOM0, LOG["EKF_EULER"], 20) in p.outputs
    assert (ECOM0, LOG["STATUS"], 200) in p.outputs
    assert (ECOM0, LOG["UTC_TIME"], 200) in p.outputs
    assert (ECOM0, LOG["GPS1_RAW"], 0) in p.outputs
    assert (ECOM0, LOG["RTCM_RAW"], 10001) in p.outputs
    assert (ECOM0, LOG["EKF_QUAT"], 0) in p.outputs and (ECOM0, LOG["MAG"], 0) in p.outputs
    assert (ECOM0, LOG["IMU_DATA"], 0) in p.outputs
    assert (ECOM0, LOG["SHIP_MOTION"], 0) in p.outputs
    for name in ["GPS1_POS", "GPS1_VEL", "GPS1_HDT", "GPS1_SAT", *(f"EVENT_{c}" for c in "ABCDE")]:
        assert (ECOM0, LOG[name], 10001) in p.outputs
    assert len({msg for _, msg, _ in p.outputs}) == len(p.outputs)  # one entry per message
    assert p.disable_classes == [CLASS["LOG_NMEA_0"], CLASS["LOG_NMEA_1"], CLASS["LOG_NMEA_GNSS"]]
    assert p.gnss1_lever_arm == (0.5, 0.0, -1.2) and p.gnss2_lever_arm is None
    assert p.motion_profile == 7
    assert p.aiding == {"rtcm_port": 0}  # RTCM multiplexed on the sbgECom port (Port A)
    assert K.sbg_profile(make(ins_rtcm_port="/dev/y")).aiding == {"rtcm_port": 1}  # Port B
    assert p.imu_axes is None and p.imu_lever_arm is None and p.init_position is None
    raw = K.sbg_profile(make(ins_raw_gnss=True, ins_output_hz=50))
    assert (ECOM0, LOG["GPS1_RAW"], 10001) in raw.outputs
    assert (ECOM0, LOG["EKF_NAV"], 4) in raw.outputs


def test_profile_rejects_an_unsupported_rate() -> None:
    with pytest.raises(ValueError, match="INS_OUTPUT_HZ"):
        K.sbg_profile(make(ins_output_hz=7))


@pytest.mark.parametrize(
    ("setting", "profile_id"),
    [("general", 1), ("automotive", 2), ("marine", 3), ("airplane", 4), ("helicopter", 5)]
    + [("pedestrian", 6), ("uav", 7)],
)
def test_motion_profile_mapping(setting: str, profile_id: int) -> None:
    assert K.sbg_profile(make(ins_motion_profile=setting)).motion_profile == profile_id


def test_gnss_installation_target_keeps_what_is_not_configured() -> None:
    single = C.GnssInstallation((0.0, 0.0, 0.0), False, (0.0, 0.0, 0.0), 1)
    want = K.sbg_profile(make()).gnss_installation_target(single)
    assert want == C.GnssInstallation((0.5, 0.0, -1.2), True, (0.0, 0.0, 0.0), 1)  # SINGLE
    # A dual-antenna unit set up in sbgCenter keeps its secondary arm and mode when only
    # INS_LEVER_ARM_GNSS1 is configured: forcing SINGLE would silently drop dual heading.
    dual = C.GnssInstallation((0.1, 0.1, 0.1), False, (1.0, 0.0, 0.0), 4)
    want = K.sbg_profile(make()).gnss_installation_target(dual)
    assert want == C.GnssInstallation((0.5, 0.0, -1.2), True, (1.0, 0.0, 0.0), 4)
    both = K.sbg_profile(make(ins_lever_arm_gnss2="-0.5,0,-1.2"))
    assert both.gnss_installation_target(single) == C.GnssInstallation(
        (0.5, 0.0, -1.2), True, (-0.5, 0.0, -1.2), 4
    )
    assert K.sbg_profile(make(ins_lever_arm_gnss1=None)).gnss_installation_target(dual) is None


def test_imu_alignment_target() -> None:
    current = C.ImuAlignment(0, 3, 0.001, 0.002, 0.003, (0.0, 0.0, 0.0))
    assert K.sbg_profile(make()).imu_alignment_target(current) is None  # "xyz": leave it
    arm = K.sbg_profile(make(ins_imu_lever_arm="0.1,0.2,0.3"))
    assert arm.imu_alignment_target(current) == C.ImuAlignment(
        0, 3, 0.001, 0.002, 0.003, (0.1, 0.2, 0.3)
    )  # misalignment angles are the unit's own (mtrtk has no setting for them)
    axes = K.sbg_profile(make(ins_imu_axis="backward,left"))
    assert axes.imu_axes == (1, 2)
    assert axes.imu_alignment_target(current) == C.ImuAlignment(
        1, 2, 0.001, 0.002, 0.003, (0.0, 0.0, 0.0)
    )
    assert K.sbg_profile(make(ins_imu_axis="Forward, Right")).imu_axes == (0, 3)


@pytest.mark.parametrize("bad", ["forward", "forward,backward", "up,down", "north,east", "x,y,z"])
def test_imu_axis_rejects_bad_mappings(bad: str) -> None:
    with pytest.raises(ValueError, match="INS_IMU_AXIS"):
        K.sbg_profile(make(ins_imu_axis=bad))


def test_aiding_target_changes_only_the_rtcm_port() -> None:
    current = C.AidingAssignment(5, 5, 0xFF, 0, 0xFF, 0xFF, 0)
    want = K.sbg_profile(make()).aiding_target(current)
    assert want == C.AidingAssignment(5, 5, 0xFF, 0, 0, 0xFF, 0)
    want = K.sbg_profile(make(ins_rtcm_port="/dev/y")).aiding_target(current)
    assert want == C.AidingAssignment(5, 5, 0xFF, 0, 1, 0xFF, 0)


def info_payload() -> bytes:
    return b"ELLIPSE-D-G4A3-B1".ljust(32, b"\0") + struct.pack(
        "<IIHBBII", 12345, 3, 2025, 6, 1, 0x02000000, 0x03010000
    )


def load_matching(dev: FakeEllipse, settings: Settings) -> None:
    """Pre-load *dev* with INFO and with every profile item already at its wanted value."""
    p = K.sbg_profile(settings)
    dev.put(CMD["INFO"], info_payload())
    dev.put(CMD["UART_CONF"], struct.pack("<BIB", C.COM_A, 921600, 1))
    for cls, msg, mode in p.outputs:
        dev.put(CMD["OUTPUT_CONF"], C.encode_output_conf(0, cls, msg, mode))
    for cls in p.disable_classes:
        dev.put(CMD["OUTPUT_CLASS_ENABLE"], C.encode_output_class_enable(0, cls, False))
    dev.put(CMD["MOTION_PROFILE_ID"], C.encode_motion_profile(p.motion_profile or 1))
    single = C.GnssInstallation((0.0, 0.0, 0.0), False, (0.0, 0.0, 0.0), 1)
    gnss = p.gnss_installation_target(single) or single
    dev.put(CMD["GNSS_1_INSTALLATION"], C.encode_gnss_installation(gnss))
    dev.put(
        CMD["IMU_ALIGNMENT_LEVER_ARM"],
        C.encode_imu_alignment(C.ImuAlignment(0, 3, 0.0, 0.0, 0.0, (0.0, 0.0, 0.0))),
    )
    aiding = p.aiding_target(C.AidingAssignment(5, 5, 0xFF, 0, 0xFF, 0xFF, 0))
    assert aiding is not None
    dev.put(CMD["AIDING_ASSIGNMENT"], C.encode_aiding_assignment(aiding))
    dev.put(CMD["INIT_PARAMETERS"], C.encode_init_parameters(0.0, 0.0, 0.0, date(2026, 1, 1)))


Body = Callable[[InsController], Awaitable[Any]]


async def run_with_device(dev: FakeEllipse, body: Body, bus: Bus | None = None) -> Any:
    ctrl = InsController(bus or Bus(), lambda: dev, SbgFramer, None, rx_timeout_s=5)
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    for _ in range(100):
        if ctrl.connected:
            break
        await asyncio.sleep(0.001)
    try:
        return await body(ctrl)
    finally:
        stop.set()
        await task


def drain(sub: Any) -> list[tuple[str, Any]]:
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


async def test_configure_read_only_reports_current() -> None:
    settings = make()
    dev = FakeEllipse()
    load_matching(dev, settings)
    dev.put(CMD["MOTION_PROFILE_ID"], C.encode_motion_profile(2))  # differs: wants 7
    driver = Driver()
    bus = Bus()
    sub = bus.subscribe("ins.config", "receiver.error")

    async def body(ctrl: InsController) -> K.SbgConfigReport:
        return await K.configure(ctrl, driver, settings, apply=False)

    report = await run_with_device(dev, body, bus)
    assert report.applied == [] and dev.sets == []
    assert report.pending == ["motion_profile"]
    assert report.current["motion_profile"] == 2 and report.wanted["motion_profile"] == 7
    assert report.current["output:EKF_NAV"] == 20
    assert report.current["uart:COM_A"] == C.UartConf(0, 921600, 1)
    assert report.errors == [] and report.mismatched == [] and report.saved is False
    assert driver.info is not None and driver.info.product_code == "ELLIPSE-D-G4A3-B1"
    assert report.info == driver.info and driver.config_report is report
    events = drain(sub)
    assert events == [("ins.config", report)]  # nothing wrong: no receiver.error


async def test_configure_applies_only_differences_and_saves_once() -> None:
    settings = make(ins_apply_config=True)
    dev = FakeEllipse()
    load_matching(dev, settings)
    dev.put(CMD["MOTION_PROFILE_ID"], C.encode_motion_profile(2))
    dev.put(CMD["OUTPUT_CONF"], C.encode_output_conf(0, ECOM0, LOG["EKF_NAV"], 0))
    driver = Driver()

    async def body(ctrl: InsController) -> K.SbgConfigReport:
        return await K.configure(ctrl, driver, settings, apply=True)

    report = await run_with_device(dev, body)
    assert [cmd for cmd, _ in dev.sets] == [
        CMD["OUTPUT_CONF"],
        CMD["MOTION_PROFILE_ID"],
        CMD["SETTINGS_ACTION"],
    ]
    assert dev.set_payloads(CMD["SETTINGS_ACTION"]) == [bytes([C.SAVE_SETTINGS])]
    assert dev.set_payloads(CMD["OUTPUT_CONF"]) == [
        C.encode_output_conf(0, ECOM0, LOG["EKF_NAV"], 20)
    ]
    assert report.applied == ["output:EKF_NAV", "motion_profile"]
    assert report.saved is True and driver.saved_this_run is True
    assert report.current["motion_profile"] == 7 and report.pending == []
    assert "gnss_installation" in report.unchanged and report.errors == []

    # The unit rebooted and something drifted again: re-applied, but never saved twice a run.
    dev.sets.clear()
    dev.put(CMD["MOTION_PROFILE_ID"], C.encode_motion_profile(2))
    again = await run_with_device(dev, body)
    assert again.applied == ["motion_profile"] and again.saved is False
    assert [cmd for cmd, _ in dev.sets] == [CMD["MOTION_PROFILE_ID"]]


async def test_configure_without_ins_apply_config_does_not_save() -> None:
    """A forced apply (API `force`) writes RAM settings but persists only with INS_APPLY_CONFIG."""
    settings = make(ins_apply_config=False)
    dev = FakeEllipse()
    load_matching(dev, settings)
    dev.put(CMD["MOTION_PROFILE_ID"], C.encode_motion_profile(2))
    driver = Driver()

    async def body(ctrl: InsController) -> K.SbgConfigReport:
        return await K.configure(ctrl, driver, settings, apply=True)

    report = await run_with_device(dev, body)
    assert report.applied == ["motion_profile"] and report.saved is False
    assert dev.set_payloads(CMD["SETTINGS_ACTION"]) == [] and driver.saved_this_run is False


async def test_configure_readback_mismatch_is_reported() -> None:
    settings = make(ins_apply_config=True)
    dev = FakeEllipse()
    load_matching(dev, settings)
    dev.put(CMD["MOTION_PROFILE_ID"], C.encode_motion_profile(2))
    dev.sticky.add(CMD["MOTION_PROFILE_ID"])  # ACKs the SET, keeps returning 2
    driver = Driver()
    bus = Bus()
    sub = bus.subscribe("ins.config", "receiver.error")

    async def body(ctrl: InsController) -> K.SbgConfigReport:
        return await K.configure(ctrl, driver, settings, apply=True)

    report = await run_with_device(dev, body, bus)
    assert report.mismatched == ["motion_profile"] and report.applied == []
    assert report.current["motion_profile"] == 2
    assert dev.set_payloads(CMD["SETTINGS_ACTION"]) == [] and report.saved is False
    errors = [item for topic, item in drain(sub) if topic == "receiver.error"]
    assert errors == ["INS configuration: motion_profile: read back 2, wanted 7"]


async def test_configure_set_refused_and_unsupported_outputs() -> None:
    settings = make(ins_apply_config=True)
    dev = FakeEllipse()
    load_matching(dev, settings)
    # An older Ellipse without EVENT_E: the unit NACKs the GET for that output.
    del dev.values[(CMD["OUTPUT_CONF"], C.encode_output_conf_selector(0, ECOM0, LOG["EVENT_E"]))]
    dev.put(CMD["MOTION_PROFILE_ID"], C.encode_motion_profile(2))
    dev.put(CMD["OUTPUT_CONF"], C.encode_output_conf(0, ECOM0, LOG["EKF_NAV"], 0))
    dev.ack_error[CMD["MOTION_PROFILE_ID"]] = 9
    driver = Driver()
    bus = Bus()
    sub = bus.subscribe("receiver.error")

    async def body(ctrl: InsController) -> K.SbgConfigReport:
        return await K.configure(ctrl, driver, settings, apply=True)

    report = await run_with_device(dev, body, bus)
    assert report.unsupported == ["output:EVENT_E"]
    assert report.applied == ["output:EKF_NAV"]
    assert report.errors == [
        "motion_profile: sbgECom MOTION_PROFILE_ID: error 9 (INVALID_PARAMETER)"
    ]
    assert report.saved is True  # the change that did take is persisted
    (msg,) = [item for _, item in drain(sub)]
    assert msg.startswith("INS configuration: motion_profile: sbgECom MOTION_PROFILE_ID")


async def test_configure_class_enable_and_init_position() -> None:
    settings = make(ins_apply_config=True, ins_init_position="23.7,90.4,12.0")
    dev = FakeEllipse()
    load_matching(dev, settings)
    dev.put(CMD["OUTPUT_CLASS_ENABLE"], C.encode_output_class_enable(0, CLASS["LOG_NMEA_0"], True))
    driver = Driver()

    async def body(ctrl: InsController) -> K.SbgConfigReport:
        return await K.configure(ctrl, driver, settings, apply=True)

    report = await run_with_device(dev, body)
    assert report.applied == ["class:LOG_NMEA_0", "init_position"]
    assert dev.set_payloads(CMD["OUTPUT_CLASS_ENABLE"]) == [bytes([0, CLASS["LOG_NMEA_0"], 0])]
    (init,) = dev.set_payloads(CMD["INIT_PARAMETERS"])
    assert struct.unpack("<ddd", init[:24]) == (23.7, 90.4, 12.0)
    # The init position only differs by its date on the next connect: not rewritten daily.
    dev.sets.clear()
    dev.put(CMD["INIT_PARAMETERS"], C.encode_init_parameters(23.7, 90.4, 12.0, date(2020, 1, 1)))
    again = await run_with_device(dev, body)
    assert "init_position" in again.unchanged and dev.sets == []


async def test_configure_refused_get_is_an_error_not_a_crash() -> None:
    settings = make()
    dev = FakeEllipse()
    load_matching(dev, settings)
    del dev.values[(CMD["MOTION_PROFILE_ID"], b"")]  # the unit NACKs the GET
    driver = Driver()

    async def body(ctrl: InsController) -> K.SbgConfigReport:
        return await K.configure(ctrl, driver, settings, apply=False)

    report = await run_with_device(dev, body)
    assert report.errors == [
        "motion_profile: sbgECom MOTION_PROFILE_ID: error 9 (INVALID_PARAMETER)"
    ]
    assert "motion_profile" not in report.current


def test_report_as_dict_is_json_safe() -> None:
    import json

    report = K.SbgConfigReport(
        info=C.decode_info(info_payload()),
        applied=["motion_profile"],
        current={
            "motion_profile": 7,
            "init_position": C.InitParameters(1.0, 2.0, 3.0, date(2026, 9, 19)),
            "uart:COM_A": C.UartConf(0, 921600, 1),
        },
    )
    d = report.as_dict()
    json.dumps(d)
    assert d["info"]["product_code"] == "ELLIPSE-D-G4A3-B1"
    assert d["info"]["calibration_date"] == "2025-06-01"
    assert d["current"]["init_position"]["date"] == "2026-09-19"
    assert d["applied"] == ["motion_profile"]


async def test_make_configure_uses_ins_apply_config() -> None:
    settings = make()
    dev = FakeEllipse()
    load_matching(dev, settings)
    dev.put(CMD["MOTION_PROFILE_ID"], C.encode_motion_profile(2))
    driver = Driver()
    hook = K.make_configure(driver, settings)

    async def body(ctrl: InsController) -> None:
        await hook(ctrl)

    await run_with_device(dev, body)
    assert driver.config_report is not None and driver.config_report.pending == ["motion_profile"]
    assert dev.sets == []  # INS_APPLY_CONFIG defaults to 0: read-only
