"""`mtrtk ins info | config | monitor` against a scripted Ellipse on the INS port."""

import asyncio
import struct
from pathlib import Path

import pytest
from click.testing import CliRunner

from mtrtk.cli import main
from mtrtk.rover.drivers import factory
from mtrtk.rover.drivers.sbg import commands as C
from mtrtk.rover.drivers.sbg.ids import CLASS, CMD, LOG
from sbgdevice import FakeEllipse

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ins" / "sbg_frames.hex"


def info_payload() -> bytes:
    return b"ELLIPSE-D-G4A3-B1".ljust(32, b"\0") + struct.pack(
        "<IIHBBII", 12345, 3, 2025, 6, 1, 0x02000000, 0x03010000
    )


@pytest.fixture
def ins_env(monkeypatch: pytest.MonkeyPatch) -> FakeEllipse:
    monkeypatch.setenv("ROLE", "rover")
    monkeypatch.setenv("ROVER_DRIVER", "sbg_ellipse")
    monkeypatch.setenv("INS_PORT", "/dev/ttyFAKE0")
    monkeypatch.setenv("INS_BAUD", "921600")
    dev = FakeEllipse()
    dev.put(CMD["INFO"], info_payload())
    # EKF_NAV is off on Port A: the profile wants it at the output rate.
    dev.put(CMD["OUTPUT_CONF"], C.encode_output_conf(0, CLASS["LOG_ECOM_0"], LOG["EKF_NAV"], 0))
    opened: list[tuple[str, int]] = []

    def serial(port: str, baud: int, **_: object) -> FakeEllipse:
        opened.append((port, baud))
        return dev

    monkeypatch.setattr(factory, "SerialSource", serial)
    dev.opened = opened  # type: ignore[attr-defined]
    return dev


def test_ins_info_prints_identity_and_configuration(ins_env: FakeEllipse) -> None:
    result = CliRunner().invoke(main, ["ins", "info"])
    assert result.exit_code == 0, result.output
    assert ins_env.opened == [("/dev/ttyFAKE0", 921600)]  # type: ignore[attr-defined]
    out = result.output
    assert "ELLIPSE-D-G4A3-B1" in out and "12345" in out
    assert "pending" in out and "output:EKF_NAV" in out
    assert ins_env.sets == []  # read-only


def test_ins_config_dry_run_lists_the_writes(ins_env: FakeEllipse) -> None:
    result = CliRunner().invoke(main, ["ins", "config", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would write output:EKF_NAV: 0 -> 20" in result.output  # 10 Hz = mode 20
    assert ins_env.sets == []


def test_ins_config_apply_writes_and_reads_back(ins_env: FakeEllipse) -> None:
    result = CliRunner().invoke(main, ["ins", "config", "--apply"])
    assert result.exit_code == 0, result.output
    assert [cmd for cmd, _ in ins_env.sets] == [CMD["OUTPUT_CONF"]]
    assert "applied" in result.output and "output:EKF_NAV" in result.output
    assert "not saved to flash" in result.output  # INS_APPLY_CONFIG=0


def test_ins_config_flags_are_exclusive(ins_env: FakeEllipse) -> None:
    result = CliRunner().invoke(main, ["ins", "config", "--apply", "--dry-run"])
    assert result.exit_code != 0 and "either" in result.output


class Stream:
    """The golden frames, a second navigation epoch, then a quiet live link."""

    name = "stream"
    ends_at_eof = False

    def __init__(self) -> None:
        lines = FIXTURE.read_text().split()
        self.chunks = [bytes.fromhex("".join(lines)), bytes.fromhex(lines[0])]  # + EKF_NAV
        self.written: list[bytes] = []

    async def open(self) -> None:
        return None

    async def read(self) -> bytes:
        await asyncio.sleep(0.01)
        return self.chunks.pop(0) if self.chunks else b"\xff"  # a stray byte keeps it alive

    async def write(self, data: bytes) -> None:
        self.written.append(data)

    async def close(self) -> None:
        return None


def test_ins_monitor_prints_one_line_per_epoch(
    monkeypatch: pytest.MonkeyPatch, ins_env: FakeEllipse
) -> None:
    stream = Stream()
    monkeypatch.setattr(factory, "SerialSource", lambda port, baud, **_: stream)
    build = factory.build_ins

    def undecimated(*args: object, **kwargs: object) -> factory.InsBundle:
        # `state.epoch` is capped to ROVER_NAV_HZ on the host clock: lift the cap, so the test
        # does not race the clock between the two epochs.
        bundle = build(*args, **kwargs)  # type: ignore[arg-type]
        bundle.adapter.nav_hz_cap = 1e9
        return bundle

    monkeypatch.setattr(factory, "build_ins", undecimated)
    result = CliRunner().invoke(main, ["ins", "monitor", "--seconds", "1.0"])
    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.output.splitlines() if "lat" in ln]
    assert len(lines) == 2, result.output
    # The first epoch is the stream's first frame; the second one has heard everything else.
    assert lines[0].startswith("--:--:-- Nav position") and "gnss -" in lines[0]
    assert lines[1].startswith("10:30:15.25 Nav position")
    assert "lat 23.7275000 lon 90.3925000 hdg 90.0 fix 3D gnss RTK fixed" in lines[1]
    assert stream.written == []  # monitor never writes to the unit


def test_ins_commands_need_an_ins_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROVER_DRIVER", "ublox")
    result = CliRunner().invoke(main, ["ins", "info"])
    assert result.exit_code != 0 and "ROVER_DRIVER" in result.output


def test_ins_info_reports_a_port_that_will_not_open(
    monkeypatch: pytest.MonkeyPatch, ins_env: FakeEllipse
) -> None:
    class Dead(Stream):
        async def open(self) -> None:
            raise OSError("No such file or directory: '/dev/ttyFAKE0'")

    monkeypatch.setattr(factory, "SerialSource", lambda port, baud, **_: Dead())
    monkeypatch.setattr("mtrtk.cli.INS_CONNECT_TIMEOUT_S", 0.3)
    result = CliRunner().invoke(main, ["ins", "info"])
    assert result.exit_code != 0
    assert "cannot open /dev/ttyFAKE0" in result.output and "No such file" in result.output


@pytest.fixture
def vn_env(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    from vectornav.device import VnDevice

    monkeypatch.setenv("ROLE", "rover")
    monkeypatch.setenv("ROVER_DRIVER", "vectornav")
    monkeypatch.setenv("INS_PORT", "/dev/ttyFAKE1")
    monkeypatch.setenv("INS_LEVER_ARM_GNSS1", "0.1,0.2,-1.0")
    dev = VnDevice()
    monkeypatch.setattr(factory, "SerialSource", lambda port, baud, **_: dev)
    return dev


def test_vn_dry_run_names_the_register_values(vn_env) -> None:  # type: ignore[no-untyped-def]
    result = CliRunner().invoke(main, ["ins", "config", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "(profile)" not in result.output
    assert "would write antenna_offset: [0.0,0.0,0.0] -> [0.1,0.2,-1.0]" in result.output
    assert "would write binary_output_1: " in result.output and '"divisor":80' in result.output
    assert not any(c.startswith(("VNWRG", "VNWNV")) for c in vn_env.commands)


def test_vn_apply_without_ins_apply_config_is_not_saved(vn_env) -> None:  # type: ignore[no-untyped-def]
    result = CliRunner().invoke(main, ["ins", "config", "--apply"])
    assert result.exit_code == 0, result.output
    assert any(c.startswith("VNWRG,57") for c in vn_env.commands)
    assert "VNWNV" not in vn_env.commands
    assert "not saved to flash (INS_APPLY_CONFIG=0)" in result.output


def test_a_link_that_drops_during_configure_is_a_clean_error(vn_env) -> None:  # type: ignore[no-untyped-def]
    def unplug(cmd: str, args: list[str]) -> str | None:
        if cmd == "VNRRG" and args[0] == "75":
            raise ConnectionError("device reports readiness to read but returned no data")
        return None

    vn_env.hooks.append(unplug)
    result = CliRunner().invoke(main, ["ins", "info"])
    assert result.exit_code == 1, result.output
    assert "lost /dev/ttyFAKE1: device reports readiness" in result.output
    assert "Traceback" not in result.output


def test_ins_tools_open_ins_port_even_with_a_replay_configured(
    ins_env: FakeEllipse, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MTRTK_SOURCE=file: replays under `mtrtk run`; the INS tools always talk to the unit."""
    bin_fixture = FIXTURE.with_suffix(".bin")
    monkeypatch.setenv("MTRTK_SOURCE", f"file:{bin_fixture}")
    result = CliRunner().invoke(main, ["ins", "info"])
    assert result.exit_code == 0, result.output
    assert ins_env.opened == [("/dev/ttyFAKE0", 921600)]  # type: ignore[attr-defined]
    assert "ELLIPSE-D-G4A3-B1" in result.output


def test_ins_tools_without_ins_port_say_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROLE", "rover")
    monkeypatch.setenv("ROVER_DRIVER", "sbg_ellipse")
    monkeypatch.delenv("INS_PORT", raising=False)
    monkeypatch.setenv("MTRTK_SOURCE", f"file:{FIXTURE.with_suffix('.bin')}")
    result = CliRunner().invoke(main, ["ins", "info"])
    assert result.exit_code == 1 and "INS_PORT" in result.output, result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("args", [["ins", "info"], ["ins", "config", "--apply"]])
def test_a_unit_that_never_answers_is_a_failure(ins_env: FakeEllipse, args: list[str]) -> None:
    """A script running `mtrtk ins config --apply` must not read silence as success."""
    ins_env.silent_cmds.add(CMD["INFO"])
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 1, result.output
    assert "did not answer" in result.output and "Traceback" not in result.output
    assert ins_env.sets == []  # nothing is written to a unit that could not be identified
