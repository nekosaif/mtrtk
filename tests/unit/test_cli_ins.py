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

    def serial(port: str, baud: int) -> FakeEllipse:
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
    """The golden frames, a second navigation epoch 0.25 s later, then a quiet live link."""

    name = "stream"
    ends_at_eof = False

    def __init__(self) -> None:
        lines = FIXTURE.read_text().split()
        self.chunks = [bytes.fromhex("".join(lines)), bytes.fromhex(lines[0])]  # + EKF_NAV
        self.written: list[bytes] = []

    async def open(self) -> None:
        return None

    async def read(self) -> bytes:
        await asyncio.sleep(0.01 if len(self.chunks) != 1 else 0.25)
        return self.chunks.pop(0) if self.chunks else b"\xff"  # a stray byte keeps it alive

    async def write(self, data: bytes) -> None:
        self.written.append(data)

    async def close(self) -> None:
        return None


def test_ins_monitor_prints_one_line_per_epoch(
    monkeypatch: pytest.MonkeyPatch, ins_env: FakeEllipse
) -> None:
    stream = Stream()
    monkeypatch.setattr(factory, "SerialSource", lambda port, baud: stream)
    result = CliRunner().invoke(main, ["ins", "monitor", "--seconds", "0.5"])
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

    monkeypatch.setattr(factory, "SerialSource", lambda port, baud: Dead())
    monkeypatch.setattr("mtrtk.cli.INS_CONNECT_TIMEOUT_S", 0.3)
    result = CliRunner().invoke(main, ["ins", "info"])
    assert result.exit_code != 0
    assert "cannot open /dev/ttyFAKE0" in result.output and "No such file" in result.output
