from pathlib import Path

import pytest
from click.testing import CliRunner

from mtrtk.cli import main


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    return tmp_path


def test_sites_add_list_activate_delete(env: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(
        main,
        [
            "sites",
            "add",
            "roof",
            "--ecef",
            "1234567.8912",
            "-987654.3234",
            "5555555.0",
            "--sigma",
            "0.004",
            "--source",
            "csrs-ppp",
            "--frame",
            "ITRF2020",
            "--epoch",
            "2026.71",
        ],
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        main, ["sites", "add", "field", "--llh", "23.8373506", "90.2625502", "-36.268"]
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(main, ["sites", "list"])
    assert (
        r.exit_code == 0 and "roof" in r.output and "field" in r.output and "csrs-ppp" in r.output
    )
    r = runner.invoke(main, ["sites", "activate", "field"])
    assert r.exit_code == 0 and "active" in r.output
    r = runner.invoke(main, ["sites", "list"])
    assert r.output.index("* field") >= 0
    r = runner.invoke(main, ["sites", "delete", "roof"])
    assert r.exit_code == 0
    r = runner.invoke(main, ["sites", "list"])
    assert "roof" not in r.output


def test_sites_add_requires_coordinates(env: Path) -> None:
    r = CliRunner().invoke(main, ["sites", "add", "x"])
    assert r.exit_code != 0 and "--ecef" in r.output


def test_sites_activate_unknown(env: Path) -> None:
    r = CliRunner().invoke(main, ["sites", "activate", "nope"])
    assert r.exit_code != 0 and "nope" in r.output


def test_sites_delete_unknown(env: Path) -> None:
    r = CliRunner().invoke(main, ["sites", "delete", "nope"])
    assert r.exit_code != 0 and "nope" in r.output
    assert "deleted" not in r.output


def test_sites_delete_refuses_the_active_site(env: Path) -> None:
    """Deleting the site the receiver is broadcasting would leave a base on an unnamed ARP."""
    runner = CliRunner()
    r = runner.invoke(main, ["sites", "add", "roof", "--ecef", "1.0", "2.0", "3.0"])
    assert r.exit_code == 0, r.output
    assert runner.invoke(main, ["sites", "activate", "roof"]).exit_code == 0
    r = runner.invoke(main, ["sites", "delete", "roof"])
    assert r.exit_code != 0 and "active site" in r.output and "deleted" not in r.output
    assert "roof" in runner.invoke(main, ["sites", "list"]).output
