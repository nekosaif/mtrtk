import asyncio
import math
from pathlib import Path

import pytest
from click.testing import CliRunner
from pyubx2 import GET, UBXMessage

from mtrtk import daemon as daemon_mod
from mtrtk.cli import main
from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer, Proto
from mtrtk.core.source import FileReplaySource
from mtrtk.core.statestore import StateStore
from mtrtk.daemon import Daemon, StatusPrinter
from ubxtest import ubx_frame

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_raw_10s.ubx"


async def test_daemon_replays_file_to_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    settings = Settings(_env_file=None, mtrtk_source=f"file:{FIXTURE}", replay_speed=0)
    daemon = Daemon(settings)
    lines: list[str] = []
    printer = StatusPrinter(daemon.bus, daemon.store, echo=lines.append, interval_s=0)
    printer.start()
    await daemon.run()
    await printer.stop()
    assert daemon.store.state.fix.fix_type == 3
    assert daemon.store.state.sat_summary.tracked > 20
    assert any("3D" in line for line in lines)


async def test_daemon_tracks_connected_flag_and_source_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ReceiverState.connected` / `.source` follow the receiver events, not just the wire."""
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    settings = Settings(_env_file=None, mtrtk_source=f"file:{FIXTURE}", replay_speed=0)
    at_eof = asyncio.Event()
    release = asyncio.Event()

    class GatedReplay(FileReplaySource):
        """The replay fixture, held at EOF so the test can look at a still-running session."""

        async def read(self) -> bytes:
            data = await super().read()
            if not data:
                at_eof.set()
                await release.wait()
            return data

    daemon = Daemon(settings, source_factory=lambda: GatedReplay(FIXTURE, speed=0))
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(at_eof.wait(), 5.0)
    assert daemon.store.state.connected is True
    assert daemon.store.state.source == f"file:{FIXTURE.name}"
    release.set()
    await asyncio.wait_for(task, 5.0)
    assert daemon.store.state.connected is False


def test_replay_command_runs_to_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    result = CliRunner().invoke(main, ["replay", str(FIXTURE), "--speed", "0"])
    assert result.exit_code == 0, result.output
    assert "3D" in result.output
    assert "replay finished" in result.output


def test_run_command_refuses_without_receiver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    monkeypatch.setenv("MTRTK_SOURCE", "auto")
    monkeypatch.setattr("mtrtk.daemon.find_ublox_port", lambda: None)
    result = CliRunner().invoke(main, ["run"])
    assert result.exit_code != 0
    assert "no u-blox receiver found" in result.output


BASE_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_base_30s.ubx"
EPOCH_DT = 0.2  # 5 Hz, the ROVER_NAV_HZ default


class FakeClock:
    """Replaces the daemon module's `time`, so only the printer sees the fake clock."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


async def test_status_printer_throttles_epoch_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """NAV-EOE at 5 Hz must still yield at most one line per interval, not five a second."""
    clock = FakeClock()
    monkeypatch.setattr(daemon_mod, "time", clock)
    bus = Bus()
    store = StateStore(bus)
    lines: list[str] = []
    printer = StatusPrinter(bus, store, echo=lines.append, interval_s=1.0)
    printer.start()
    epochs = 0
    for frame in Framer().feed(BASE_FIXTURE.read_bytes()):
        store.apply(frame)
        if frame.proto is Proto.UBX and frame.identity == "NAV-EOE":
            epochs += 1
            clock.now += EPOCH_DT
            await asyncio.sleep(0)  # let the printer drain this epoch
    await printer.stop()
    assert epochs >= 25 and store.state.epoch_count == epochs
    assert len(lines) <= math.ceil(epochs * EPOCH_DT / 1.0)
    assert 1 < len(lines) < epochs  # it printed, but nowhere near one line per epoch


async def test_status_printer_ignores_position_once_an_epoch_has_been_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(daemon_mod, "time", clock)
    bus = Bus()
    store = StateStore(bus)
    lines: list[str] = []
    printer = StatusPrinter(bus, store, echo=lines.append, interval_s=1.0)
    printer.start()

    bus.publish("state.position", store.state.position)
    await asyncio.sleep(0)
    assert len(lines) == 1  # no NAV-EOE yet: position is the fallback and does print

    clock.now += 5.0
    bus.publish("state.epoch", store.state)
    await asyncio.sleep(0)
    assert len(lines) == 2

    clock.now += 5.0  # well past interval_s: only `_saw_epoch` can suppress this
    bus.publish("state.position", store.state.position)
    await asyncio.sleep(0)
    await printer.stop()
    assert len(lines) == 2  # the epoch drives the line; position never doubles it up


async def test_unpaced_replay_of_a_long_file_drops_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file longer than the live queue must replay whole, not from its tail."""
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    epochs = 1700  # 3 frames each = 5100, past the 5000-deep bounded queue used for serial
    path = tmp_path / "long.ubx"
    path.write_bytes(
        b"".join(
            UBXMessage("NAV", "NAV-PVT", GET, iTOW=i * 1000).serialize()
            + ubx_frame(0x02, 0x15, b"\x00" * 16)
            + UBXMessage("NAV", "NAV-EOE", GET, iTOW=i * 1000).serialize()
            for i in range(1, epochs + 1)
        )
    )
    settings = Settings(_env_file=None, mtrtk_source=f"file:{path}", replay_speed=0)
    daemon = Daemon(settings)
    await daemon.run()
    assert daemon.store.state.epoch_count == epochs
    assert daemon.store.state.raw_epochs == epochs
    assert daemon._raw_sub.dropped == 0
