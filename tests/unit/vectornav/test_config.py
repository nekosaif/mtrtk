from typing import Any

import pytest

from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.rover.drivers.vectornav import config as vn_config
from mtrtk.rover.drivers.vectornav.adapter import VnStateAdapter
from mtrtk.rover.drivers.vectornav.config import (
    DEFAULT_FIELDS,
    configure,
    divisor_for_hz,
    frame_bytes,
    vn_profile,
)
from mtrtk.rover.drivers.vectornav.driver import VnDriver

from .device import VnDevice, finish, start


def settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "ntrip_password": "x",
        "role": "rover",
        "rover_driver": "vectornav",
        "ins_port": "/dev/null-vn",
        "ins_output_hz": 10,
        "ins_raw_gnss": True,
        "ins_apply_config": True,  # the save tests; read-only runs pass apply=False
        "ins_lever_arm_gnss1": None,
        "ins_vn_rtcm": False,
        "ins_vn_scenario": None,
        "ins_vn_ahrs_aiding": None,
        "ins_vn_ref_rotation": None,
        "ins_vn_vpe": None,
    }
    base.update(kw)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def test_divisor() -> None:
    assert divisor_for_hz(10) == 80
    assert divisor_for_hz(200) == 4
    assert divisor_for_hz(1) == 800
    assert divisor_for_hz(7) == 100
    assert vn_profile(settings(ins_output_hz=10)).note is None
    note = vn_profile(settings(ins_output_hz=7)).note
    assert note is not None and "8 Hz" in note and "divisor 100" in note


def test_profile_fields_default_and_raw_off() -> None:
    on = vn_profile(settings())
    assert on.fields == DEFAULT_FIELDS
    assert on.fields == {
        "time": 0x02DE,
        "imu": 0x0611,
        "gps": 0x7ABA,
        "attitude": 0x0103,
        "ins": 0x0613,
    }
    assert on.gps_ext == 0x0001 and on.divisor == 80
    off = vn_profile(settings(ins_raw_gnss=False))
    assert off.gps_ext is None and not off.fields["gps"] & 0x8000
    assert on.antenna_offset is None and on.vpe is None and on.ins_basic is None
    assert on.ref_rotation is None


def test_profile_optional_items() -> None:
    p = vn_profile(
        settings(
            ins_lever_arm_gnss1="0.1,0.2,-1.0",
            ins_vn_scenario=1,
            ins_vn_ahrs_aiding=True,
            ins_vn_vpe="1,1,1,1",
            ins_vn_ref_rotation="0,1,0,1,0,0,0,0,-1",
        )
    )
    assert p.antenna_offset == (0.1, 0.2, -1.0)
    assert p.ins_basic == (1, True)
    assert p.vpe == (1, 1, 1, 1)
    assert p.ref_rotation == (0.0, 1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, -1.0)


def test_settings_reject_bad_vn_values() -> None:
    with pytest.raises(ValueError, match="INS_VN_REF_ROTATION"):
        settings(ins_vn_ref_rotation="1,0,0")
    with pytest.raises(ValueError, match="INS_VN_VPE"):
        settings(ins_vn_vpe="1,1,x,1")
    with pytest.raises(ValueError):
        settings(ins_vn_scenario=-1)
    assert settings(ins_vn_vpe="", ins_vn_ref_rotation=" ").ins_vn_vpe is None


def make_driver(controller: Any, *, rtcm: bool = False) -> VnDriver:
    adapter = VnStateAdapter(controller.bus, nav_hz_cap=5.0, raw_capture=None)
    return VnDriver(controller, adapter, rtcm_enabled=rtcm)


def reject_75(times: int) -> Any:
    left = {"n": times}

    def hook(cmd: str, args: list[str]) -> str | None:
        if cmd == "VNWRG" and args[0] == "75" and left["n"] > 0:
            left["n"] -= 1
            return "VNERR,07"
        return None

    return hook


async def test_configure_probe_fallbacks() -> None:
    dev = VnDevice()
    dev.hooks.append(reject_75(1))
    controller, stop, task = await start(dev)
    driver = make_driver(controller)
    events = controller.bus.subscribe("ins.config")
    report = await configure(controller, driver, settings(), apply=True)
    writes_75 = [c for c in dev.commands if c.startswith("VNWRG,75")]
    assert writes_75 == [
        "VNWRG,75,1,80,3E,2DE,611,FABA,1,103,613",
        "VNWRG,75,1,80,3E,2DE,611,7ABA,103,613",
    ]
    assert driver.capabilities.raw_gnss_log is False and driver.capabilities.sats is True
    assert report.raw_meas is False and report.sat_info is True
    assert dev.commands.count("VNWNV") == 1 and report.saved
    assert driver.info is not None and driver.info.model == "VN-200T-CR"
    assert driver.info.firmware == "2.0.0.0" and driver.info.serial == "0100012345"
    assert "binary_output_1" in report.applied and "async_output_type" in report.applied
    assert events.queue.get_nowait()[1] is report and driver.config_report is report
    await finish(stop, task)


async def test_configure_probe_drops_satinfo_after_two_refusals() -> None:
    dev = VnDevice()
    dev.hooks.append(reject_75(2))
    controller, stop, task = await start(dev)
    driver = make_driver(controller)
    report = await configure(controller, driver, settings(), apply=True)
    assert [c for c in dev.commands if c.startswith("VNWRG,75")][-1] == (
        "VNWRG,75,1,80,3E,2DE,611,3ABA,103,613"
    )
    assert driver.capabilities.sats is False and driver.capabilities.raw_gnss_log is False
    assert report.sat_info is False and report.raw_meas is False
    assert dev.commands.count("VNWNV") == 1
    await finish(stop, task)


async def test_configure_all_refused_leaves_ascii_on_and_does_not_save() -> None:
    """A unit that takes no binary output 1 must keep its ASCII output: turning it off and
    saving would leave a unit that says nothing, in flash."""
    dev = VnDevice()
    dev.hooks.append(reject_75(3))
    controller, stop, task = await start(dev)
    errors = controller.bus.subscribe("receiver.error")
    driver = make_driver(controller)
    report = await configure(controller, driver, settings(), apply=True)
    assert any("binary_output_1" in e for e in report.errors)
    assert "binary_output_1" not in report.applied
    assert errors.queue.qsize() >= 1
    assert "VNWRG,06,0" not in dev.commands and dev.regs[6] == ["14"]
    assert "async_output_type" not in report.applied
    assert any("ASCII" in n and "left on" in n for n in report.notes)
    assert dev.commands.count("VNWNV") == 0 and not report.saved
    await finish(stop, task)


async def test_ascii_goes_off_only_after_binary_output_streams() -> None:
    dev = VnDevice()
    controller, stop, task = await start(dev)
    driver = make_driver(controller)
    report = await configure(controller, driver, settings(), apply=True)
    writes = [c for c in dev.commands if c.startswith("VNWRG")]
    assert writes[0].startswith("VNWRG,75,") and writes[1] == "VNWRG,06,0"
    assert {"binary_output_1", "async_output_type"} <= set(report.applied)
    assert dev.commands.count("VNWNV") == 1
    await finish(stop, task)


async def test_binary_output_on_another_port_keeps_ascii_and_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Output 1 kept on async mode 2 (serial 2) while mtrtk is on serial 1: no binary frame
    arrives, so ASCII stays on, nothing is saved and the async mode is named."""
    monkeypatch.setattr(vn_config, "STREAM_WAIT_S", 0.1)
    dev = VnDevice({75: ["2", "16", "01", "0029"]})
    controller, stop, task = await start(dev)
    errors = controller.bus.subscribe("receiver.error")
    driver = make_driver(controller)
    report = await configure(controller, driver, settings(ins_raw_gnss=False), apply=True)
    assert "VNWRG,75,2,80,3E,2DE,611,7ABA,103,613" in dev.commands
    assert "VNWRG,06,0" not in dev.commands
    assert any("async mode 2" in e for e in report.errors)
    assert any("async mode 2" in str(item[1]) for item in _drain(errors))
    assert dev.commands.count("VNWNV") == 0
    await finish(stop, task)


def _drain(sub: Any) -> list[Any]:
    q = sub.queue
    return [q.get_nowait() for _ in range(q.qsize())]


def reject_first_75_with(code: str) -> Any:
    left = {"n": 1}

    def hook(cmd: str, args: list[str]) -> str | None:
        if cmd == "VNWRG" and args[0] == "75" and left["n"] > 0:
            left["n"] -= 1
            return f"VNERR,{code}"
        return None

    return hook


@pytest.mark.parametrize("code", ["0C", "06"])
async def test_insufficient_baud_rate_or_too_many_parameters_falls_back(code: str) -> None:
    dev = VnDevice()
    dev.hooks.append(reject_first_75_with(code))
    controller, stop, task = await start(dev)
    driver = make_driver(controller)
    report = await configure(controller, driver, settings(), apply=True)
    assert [c for c in dev.commands if c.startswith("VNWRG,75")] == [
        "VNWRG,75,1,80,3E,2DE,611,FABA,1,103,613",
        "VNWRG,75,1,80,3E,2DE,611,7ABA,103,613",
    ]
    assert "binary_output_1" in report.applied and report.raw_meas is False
    assert any("RawMeas" in n and "SatInfo" not in n for n in report.notes)
    await finish(stop, task)


async def test_fallback_note_names_only_what_was_dropped() -> None:
    """With INS_RAW_GNSS=0 RawMeas was never asked for: the note names SatInfo alone."""
    dev = VnDevice()
    dev.hooks.append(reject_75(1))
    controller, stop, task = await start(dev)
    driver = make_driver(controller)
    report = await configure(controller, driver, settings(ins_raw_gnss=False), apply=True)
    refused = [n for n in report.notes if "refused" in n]
    assert refused and all("SatInfo" in n and "RawMeas" not in n for n in refused)
    await finish(stop, task)


def test_frame_budget_note_against_the_baud_rate() -> None:
    """10 Hz with SatInfo and RawMeas does not fit 115200 baud: say so before writing."""
    notes = " ".join(vn_profile(settings()).notes)
    assert "INS_BAUD=115200" in notes and "B/s" in notes
    assert vn_profile(settings(ins_baud=921600)).notes == ()
    assert frame_bytes(DEFAULT_FIELDS, 0x0001, sats=25, meas=25) == 263 + 8 * 25 + 28 * 25


def test_motion_profile_is_reported_as_not_used() -> None:
    notes = " ".join(vn_profile(settings(ins_baud=921600, ins_motion_profile="automotive")).notes)
    assert "INS_MOTION_PROFILE" in notes and "INS_VN_SCENARIO" in notes


async def test_configure_read_only() -> None:
    dev = VnDevice()
    controller, stop, task = await start(dev)
    driver = make_driver(controller)
    report = await configure(controller, driver, settings(), apply=False)
    assert not any(c.startswith(("VNWRG", "VNWNV")) for c in dev.commands)
    assert "binary_output_1" in report.pending and "async_output_type" in report.pending
    assert report.applied == [] and not report.saved
    assert report.current["binary_output_1"] == {
        "async_mode": 0,
        "divisor": 0,
        "fields": {},
        "gps_ext": None,
    }
    assert report.current["antenna_offset"] == (0.0, 0.0, 0.0)
    assert driver.info is not None and driver.info.model == "VN-200T-CR"
    # read-only: capabilities follow what the unit is already set to output (nothing yet)
    assert driver.capabilities.raw_gnss_log is False and driver.capabilities.sats is False
    await finish(stop, task)


async def test_configure_unchanged_does_not_save() -> None:
    dev = VnDevice(
        {
            6: ["0"],
            75: ["1", "80", "3E", "02DE", "0611", "FABA", "0001", "0103", "0613"],
        }
    )
    controller, stop, task = await start(dev)
    driver = make_driver(controller)
    report = await configure(controller, driver, settings(), apply=True)
    assert not any(c.startswith(("VNWRG", "VNWNV")) for c in dev.commands)
    assert "binary_output_1" in report.unchanged and report.applied == []
    assert driver.capabilities.raw_gnss_log is True and driver.capabilities.sats is True
    await finish(stop, task)


async def test_configure_keeps_a_nonzero_async_port() -> None:
    dev = VnDevice({75: ["2", "16", "01", "0029"]})
    dev.port = 2  # mtrtk is on serial 2: output 1 on async mode 2 reaches it
    controller, stop, task = await start(dev)
    driver = make_driver(controller)
    report = await configure(controller, driver, settings(ins_raw_gnss=False), apply=True)
    assert "VNWRG,75,2,80,3E,2DE,611,7ABA,103,613" in dev.commands
    assert "VNWRG,06,0" in dev.commands and report.saved
    await finish(stop, task)


async def test_configure_optional_items_and_once_per_run_save() -> None:
    dev = VnDevice({35: ["1", "0", "1", "1"], 67: ["0", "0", "0", "0"], 26: ["1"] * 9})
    controller, stop, task = await start(dev)
    driver = make_driver(controller)
    s = settings(
        ins_lever_arm_gnss1="0.1,0.2,-1.0",
        ins_vn_scenario=1,
        ins_vn_ahrs_aiding=True,
        ins_vn_vpe="1,1,1,1",
        ins_vn_ref_rotation="1,0,0,0,1,0,0,0,1",
    )
    report = await configure(controller, driver, s, apply=True)
    assert "VNWRG,57,0.1,0.2,-1.0" in dev.commands
    assert "VNWRG,67,1,1,0,0" in dev.commands
    assert "VNWRG,35,1,1,1,1" in dev.commands
    assert "VNWRG,26,1.0,0.0,0.0,0.0,1.0,0.0,0.0,0.0,1.0" in dev.commands
    assert set(report.applied) >= {"antenna_offset", "ins_basic", "vpe", "ref_rotation"}
    assert report.mismatched == []
    assert dev.commands.count("VNWNV") == 1
    # a reconnect re-runs configure: a second change in the same run is applied but not saved
    dev.regs[57] = ["0", "0", "0"]
    report2 = await configure(controller, driver, s, apply=True)
    assert "antenna_offset" in report2.applied
    assert dev.commands.count("VNWNV") == 1 and not report2.saved
    await finish(stop, task)


async def test_configure_read_back_mismatch() -> None:
    dev = VnDevice()

    def stuck(cmd: str, args: list[str]) -> str | None:
        if cmd == "VNWRG" and args[0] == "57":
            return "VNWRG,57,0,0,0"  # echoes, but keeps the old value
        return None

    dev.hooks.append(stuck)
    controller, stop, task = await start(dev)
    errors = controller.bus.subscribe("receiver.error")
    driver = make_driver(controller)
    report = await configure(controller, driver, settings(ins_lever_arm_gnss1="1,2,3"), apply=True)
    assert "antenna_offset" in report.mismatched
    assert errors.queue.qsize() >= 1
    # other items applied, but a mismatch blocks the flash save: nothing unverified persists
    assert {"binary_output_1", "async_output_type"} <= set(report.applied)
    assert dev.commands.count("VNWNV") == 0 and not report.saved
    assert any("not saved" in n for n in report.notes)
    await finish(stop, task)


async def test_forced_apply_without_ins_apply_config_writes_ram_only() -> None:
    """`INS_APPLY_CONFIG=0` plus an explicit apply (the UI confirm, `mtrtk ins config --apply`)
    writes and verifies, but never sends `$VNWNV`: the changes last until the unit restarts,
    as the confirm dialog says. Same rule as SBG's SAVE_SETTINGS."""
    dev = VnDevice()
    controller, stop, task = await start(dev)
    driver = make_driver(controller)
    report = await configure(controller, driver, settings(ins_apply_config=False), apply=True)
    assert {"binary_output_1", "async_output_type"} <= set(report.applied)
    assert dev.commands.count("VNWNV") == 0 and not report.saved
    assert not driver.saved_this_run
    assert any("not saved to flash (INS_APPLY_CONFIG=0)" in n for n in report.notes)
    await finish(stop, task)


async def test_configure_reports_the_rate_note() -> None:
    dev = VnDevice()
    controller, stop, task = await start(dev)
    driver = make_driver(controller)
    report = await configure(controller, driver, settings(ins_output_hz=7), apply=False)
    assert driver.config_report is report
    assert any("8 Hz" in n and "divisor 100" in n for n in report.notes)
    await finish(stop, task)


async def test_ins_basic_keeps_the_units_scenario_when_only_aiding_is_set() -> None:
    dev = VnDevice({67: ["2", "0", "0", "0"]})
    controller, stop, task = await start(dev)
    driver = make_driver(controller)
    report = await configure(controller, driver, settings(ins_vn_ahrs_aiding=True), apply=True)
    assert "VNWRG,67,2,1,0,0" in dev.commands and "ins_basic" in report.applied
    await finish(stop, task)


@pytest.mark.parametrize(
    ("regs", "extra", "name"),
    [
        ({6: []}, {}, "async_output_type"),
        ({57: ["0.1", "0.2"]}, {"ins_lever_arm_gnss1": "1,2,3"}, "antenna_offset"),
        ({35: ["1.0", "1", "1", "1"]}, {"ins_vn_vpe": "1,1,1,1"}, "vpe"),
        ({67: ["1"]}, {"ins_vn_scenario": 1}, "ins_basic"),
    ],
)
async def test_unreadable_register_reply_is_reported_not_raised(
    regs: dict[int, list[str]], extra: dict[str, Any], name: str
) -> None:
    dev = VnDevice(regs)
    controller, stop, task = await start(dev)
    driver = make_driver(controller)
    report = await configure(controller, driver, settings(**extra), apply=True)
    assert any(e.startswith(f"{name}: unreadable reply") for e in report.errors)
    assert driver.config_report is report and "binary_output_1" in report.current
    await finish(stop, task)


async def test_rtcm_is_held_while_configure_runs() -> None:
    dev = VnDevice()
    controller, stop, task = await start(dev)
    driver = make_driver(controller, rtcm=True)
    seen: list[bool] = []

    def probe(cmd: str, args: list[str]) -> str | None:
        seen.append(driver.configuring)
        return None

    dev.hooks.append(probe)
    await configure(controller, driver, settings(), apply=False)
    assert seen and all(seen) and not driver.configuring
    driver.configuring = True
    await driver.inject_rtcm(b"\xd3\x00\x01\x00")
    assert driver.dropped_bytes == 4 and not any(w.startswith(b"\xd3") for w in dev.written)
    driver.configuring = False
    await driver.inject_rtcm(b"\xd3\x00\x01\x00")
    assert dev.written[-1] == b"\xd3\x00\x01\x00"
    await finish(stop, task)


async def test_configure_unknown_register_is_reported_not_raised() -> None:
    dev = VnDevice()
    del dev.regs[3]  # a unit without a serial-number register answers VNERR,08
    controller, stop, task = await start(dev)
    driver = make_driver(controller)
    report = await configure(controller, driver, settings(), apply=False)
    assert driver.info is not None and driver.info.serial == ""
    assert any("serial" in e for e in report.errors)
    await finish(stop, task)


# --------------------------------------------------------------------- driver


class FakeController:
    def __init__(self, connected: bool = True) -> None:
        self.bus = Bus()
        self.connected = connected
        self.written: list[bytes] = []

    async def write(self, data: bytes) -> None:
        if not self.connected:
            raise ConnectionError("INS not connected")
        self.written.append(data)


async def test_driver_rtcm_disabled_counts_drops() -> None:
    ctl = FakeController()
    driver = make_driver(ctl)
    assert driver.name == "vectornav"
    caps = driver.capabilities
    assert not caps.accepts_rtcm and caps.attitude and caps.imu and not caps.spectrum
    await driver.inject_rtcm(b"\xd3\x00\x01\x00")
    assert ctl.written == [] and driver.dropped_bytes == 4


async def test_driver_rtcm_enabled_forwards_and_stays_unverified_until_rtk() -> None:
    ctl = FakeController()
    driver = make_driver(ctl, rtcm=True)
    assert driver.capabilities.accepts_rtcm and driver.rtcm_unverified
    await driver.inject_rtcm(b"\xd3\x00\x01\x00")
    assert ctl.written == [b"\xd3\x00\x01\x00"]
    assert driver.adapter.state.rtk.last_rtcm_mono is not None
    driver.adapter.rtk_fix_seen = True
    assert not driver.rtcm_unverified
    ctl.connected = False
    await driver.inject_rtcm(b"\xd3\x00")
    assert driver.dropped_bytes == 2
