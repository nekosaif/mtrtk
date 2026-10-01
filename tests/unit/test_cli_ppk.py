"""`mtrtk ppk`: the command line over the PPK pipeline."""

import os
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_export import fixture_window, install_fixture_as_log

from mtrtk.cli import main
from mtrtk.ppk.rtkconf import rnx2rtkp_available
from mtrtk.rinex.convbin import convbin_available

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_raw_60s.ubx"
REQUIRE = os.environ.get("MTRTK_REQUIRE_CONVBIN") == "1"
needs_rtklib = pytest.mark.skipif(
    not REQUIRE and not (convbin_available() and rnx2rtkp_available() and FIXTURE.exists()),
    reason="RTKLIB (convbin, rnx2rtkp) or the raw fixture is missing",
)
XYZ = ["-26748.172", "5837156.618", "2561801.261"]


@needs_rtklib
def test_ppk_cli_zero_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ROLE", "rover")
    start, end = fixture_window()
    install_fixture_as_log(tmp_path, start)
    r = CliRunner().invoke(
        main,
        [
            "ppk",
            "--from",
            start.isoformat(),
            "--to",
            end.isoformat(),
            "--base",
            str(FIXTURE),
            "--base-xyz",
            *XYZ,
            "--no-events",
            "--set",
            "pos1-elmask=12",
            "--out",
            str(tmp_path / "ppk"),
        ],
    )
    assert r.exit_code == 0, r.output
    assert "epochs" in r.output and (tmp_path / "ppk" / "track.pos").exists()
    conf_text = (tmp_path / "ppk" / "ppk.conf").read_text()
    assert "pos1-elmask" in conf_text and "=12" in conf_text
    assert not (tmp_path / "mtrtk.db").exists()  # a read-only command creates no database


@needs_rtklib
def test_ppk_cli_reports_pipeline_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    start, end = fixture_window()
    install_fixture_as_log(tmp_path, start)
    args = ["ppk", "--from", start.isoformat(), "--to", end.isoformat(), "--base", str(FIXTURE)]
    r = CliRunner().invoke(main, [*args, "--out", str(tmp_path / "x")])
    assert r.exit_code == 1 and "base position" in r.output and "Traceback" not in r.output
    r = CliRunner().invoke(main, [*args, "--site", "roof", "--out", str(tmp_path / "y")])
    assert r.exit_code == 1 and "roof" in r.output


def test_ppk_cli_requires_rover_and_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    r = CliRunner().invoke(main, ["ppk", "--out", str(tmp_path / "x")])
    assert r.exit_code != 0 and "rover" in r.output.lower()
    r = CliRunner().invoke(
        main,
        ["ppk", "--from", "2026-09-18T00:00:00Z", "--to", "2026-09-18T01:00:00Z"]
        + ["--out", str(tmp_path / "x")],
    )
    assert r.exit_code != 0 and "base" in r.output.lower()


GOOD = ["--from", "2026-09-18T00:00:00Z", "--to", "2026-09-18T01:00:00Z"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--from", "2026-09-18T00:00:00", "--to", "2026-09-18T01:00:00Z"], "timezone"),
        (["--from", "yesterday", "--to", "2026-09-18T01:00:00Z"], "ISO-8601"),
        ([*GOOD, "--set", "pos1-elmask"], "key=value"),
        ([*GOOD, "--site", "roof", "--base-xyz", *XYZ], "not both"),
        (["--from", "2026-09-18T00:00:00Z"], "--to"),
        (["--session", "1", *GOOD], "one rover"),
    ],
)
def test_ppk_cli_refuses_bad_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], message: str
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    r = CliRunner().invoke(
        main, ["ppk", *args, "--base", str(FIXTURE), "--out", str(tmp_path / "x")]
    )
    assert r.exit_code != 0 and message in r.output, r.output
    assert "Traceback" not in r.output


def test_ppk_cli_refuses_two_bases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    r = CliRunner().invoke(
        main, ["ppk", *GOOD, "--base-logs", "--base-url", "http://b", "--out", str(tmp_path / "x")]
    )
    assert r.exit_code != 0 and "one base" in r.output, r.output
