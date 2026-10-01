import pytest

from mtrtk.rover.drivers.vectornav.registers import BinaryOutputConf, VnError, VnRegisters

from .device import VnDevice, finish, start


async def test_read_model() -> None:
    dev = VnDevice()
    controller, stop, task = await start(dev)
    regs = VnRegisters(controller)
    assert await regs.read(1) == ["VN-200T-CR"]
    assert await regs.model() == "VN-200T-CR"
    assert await regs.firmware() == "2.0.0.0"
    assert await regs.serial() == "0100012345"
    assert dev.commands[0] == "VNRRG,01"
    await finish(stop, task)


async def test_write_echo_returns() -> None:
    dev = VnDevice()
    controller, stop, task = await start(dev)
    regs = VnRegisters(controller)
    await regs.set_async_output_type(0)
    assert dev.commands == ["VNWRG,06,0"] and dev.regs[6] == ["0"]
    await finish(stop, task)


async def test_error_reply_raises_vn_error() -> None:
    dev = VnDevice()
    dev.hooks.append(lambda cmd, args: "VNERR,07" if cmd == "VNWRG" and args[0] == "75" else None)
    controller, stop, task = await start(dev)
    regs = VnRegisters(controller)
    with pytest.raises(VnError) as info:
        await regs.write(75, 1, 80, "A", "2DE")
    assert info.value.code == 7 and "InvalidParameter" in str(info.value)
    assert len(dev.commands) == 1  # a refusal is an answer: no retry
    await finish(stop, task)


async def test_timeout_retries_then_raises() -> None:
    dev = VnDevice()
    dev.hooks.append(lambda cmd, args: "" if cmd == "VNRRG" else None)  # never answers reads
    controller, stop, task = await start(dev)
    regs = VnRegisters(controller)
    with pytest.raises(TimeoutError):
        await regs.read(1, timeout_s=0.05, retries=3)
    assert dev.commands == ["VNRRG,01"] * 3
    await finish(stop, task)


async def test_timeout_then_answer_succeeds() -> None:
    dev = VnDevice()
    calls = {"n": 0}

    def flaky(cmd: str, args: list[str]) -> str | None:
        calls["n"] += 1
        return "" if calls["n"] == 1 else None

    dev.hooks.append(flaky)
    controller, stop, task = await start(dev)
    regs = VnRegisters(controller)
    # the dropped first attempt costs one timeout; a generous one keeps a loaded CI host
    # from timing out the answered second attempt too
    assert await regs.read(4, timeout_s=0.5) == ["2.0.0.0"]
    assert dev.commands[:2] == ["VNRRG,04", "VNRRG,04"]
    await finish(stop, task)


async def test_unrelated_reply_does_not_match() -> None:
    """An RRG reply for another register (an earlier, late answer) is not this request's."""
    dev = VnDevice()
    dev.hooks.append(lambda cmd, args: "VNRRG,02,4" if args[:1] == ["01"] else None)
    controller, stop, task = await start(dev)
    regs = VnRegisters(controller)
    with pytest.raises(TimeoutError):
        await regs.read(1, timeout_s=0.05, retries=1)
    await finish(stop, task)


async def test_write_binary_output_body() -> None:
    dev = VnDevice()
    controller, stop, task = await start(dev)
    regs = VnRegisters(controller)
    await regs.write_binary_output(1, 1, 80, {"time": 0x02DE, "gps": 0x7ABA}, gps_ext=1)
    assert dev.commands == ["VNWRG,75,1,80,A,2DE,FABA,1"]
    conf = await regs.read_binary_output(1)
    assert conf == BinaryOutputConf(
        async_mode=1, divisor=80, fields={"time": 0x02DE, "gps": 0x7ABA}, gps_ext=1
    )
    await finish(stop, task)


async def test_read_binary_output_off_and_padded_hex() -> None:
    dev = VnDevice({76: ["0", "0", "00"], 77: ["2", "16", "01", "0029"]})
    controller, stop, task = await start(dev)
    regs = VnRegisters(controller)
    assert await regs.read_binary_output(2) == BinaryOutputConf(0, 0, {}, None)
    assert await regs.read_binary_output(3) == BinaryOutputConf(2, 16, {"common": 0x29}, None)
    await finish(stop, task)


async def test_antenna_offset_and_other_helpers() -> None:
    dev = VnDevice()
    controller, stop, task = await start(dev)
    regs = VnRegisters(controller)
    await regs.set_antenna_offset(0.1, -0.25, 1.5)
    assert dev.commands[-1] == "VNWRG,57,0.1,-0.25,1.5"
    assert await regs.read_antenna_offset() == pytest.approx((0.1, -0.25, 1.5))
    await regs.set_reference_frame_rotation([1, 0, 0, 0, 1, 0, 0, 0, 1])
    assert dev.commands[-1] == "VNWRG,26,1,0,0,0,1,0,0,0,1"
    await regs.set_vpe_basic_control(1, 1, 1, 1)
    assert dev.commands[-1] == "VNWRG,35,1,1,1,1"
    await regs.set_ins_basic_config(1, True)
    assert dev.commands[-1] == "VNWRG,67,1,1,0,0"
    await regs.write_settings()
    assert dev.commands[-1] == "VNWNV"
    await regs.command("RST")
    assert dev.commands[-1] == "VNRST"
    with pytest.raises(ValueError):
        await regs.command("XYZ")
    with pytest.raises(ValueError):
        await regs.set_reference_frame_rotation([1, 0, 0])
    await finish(stop, task)


def test_vn_error_names() -> None:
    assert VnError(4).name == "InvalidCommand" and VnError(255).name == "ErrorBufferOverflow"
    assert VnError(99).name == "Unknown"


async def test_transport_error_is_retried_not_blamed_on_the_request() -> None:
    """`$VNERR` names no command: a stray OutputBufferOverflow (or an InvalidChecksum raised by
    RTCM bytes) must not fail the write that happens to be pending."""
    dev = VnDevice()
    left = {"n": 1}

    def stray(cmd: str, args: list[str]) -> str | None:
        if cmd == "VNWRG" and left["n"]:
            left["n"] -= 1
            return "VNERR,0B"
        return None

    dev.hooks.append(stray)
    controller, stop, task = await start(dev)
    regs = VnRegisters(controller)
    await regs.set_async_output_type(0)
    assert dev.commands == ["VNWRG,06,0", "VNWRG,06,0"] and dev.regs[6] == ["0"]
    await finish(stop, task)


async def test_stray_error_before_the_real_reply_is_retried() -> None:
    dev = VnDevice()
    left = {"n": 1}

    def stray(cmd: str, args: list[str]) -> str | None:
        if cmd == "VNRRG" and left["n"]:
            left["n"] -= 1
            return "VNERR,03\nVNRRG,06,14"
        return None

    dev.hooks.append(stray)
    controller, stop, task = await start(dev)
    regs = VnRegisters(controller)
    assert await regs.async_output_type() == 14
    await finish(stop, task)


async def test_persistent_transport_error_raises_after_the_retries() -> None:
    dev = VnDevice()
    dev.hooks.append(lambda cmd, args: "VNERR,02" if cmd == "VNRRG" else None)
    controller, stop, task = await start(dev)
    regs = VnRegisters(controller)
    with pytest.raises(VnError) as info:
        await regs.read(1, retries=3)
    assert info.value.code == 2 and dev.commands == ["VNRRG,01"] * 3
    await finish(stop, task)
