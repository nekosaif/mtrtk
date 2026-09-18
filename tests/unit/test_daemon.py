from pathlib import Path

import pytest
from click.testing import CliRunner

from mtrtk.cli import main
from mtrtk.config import Settings
from mtrtk.daemon import Daemon, StatusPrinter

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
