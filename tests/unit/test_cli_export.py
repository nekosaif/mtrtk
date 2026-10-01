import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from rinextest import needs_convbin

from mtrtk.cli import main

PPP = Path(__file__).resolve().parents[1] / "fixtures" / "ppp"

# Two reference stations next to ours, the way AUSPOS reports them.
MULTI_SNX = """%=SNX 2.02 AUS 26:262:00000 AUS 26:261:00000 26:261:86370 P 00009 2 X
+SOLUTION/ESTIMATE
*INDEX TYPE__ CODE PT SOLN _REF_EPOCH__ UNIT S __ESTIMATED VALUE____ _STD_DEV___
     1 STAX   BAKO  A    1 26:261:43200 m    2 -1.83696900000000e+06 2.00000e-03
     2 STAY   BAKO  A    1 26:261:43200 m    2  6.06557600000000e+06 2.00000e-03
     3 STAZ   BAKO  A    1 26:261:43200 m    2 -7.16300000000000e+05 2.00000e-03
     4 STAX   MTRK  A    1 26:261:43200 m    2 -2.67481720000000e+04 4.00000e-03
     5 STAY   MTRK  A    1 26:261:43200 m    2  5.83715661840000e+06 6.00000e-03
     6 STAZ   MTRK  A    1 26:261:43200 m    2  2.56180126070000e+06 5.00000e-03
-SOLUTION/ESTIMATE
%ENDSNX
"""


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MTRTK_ENV_FILE", str(tmp_path / "test.env"))  # never the repo's .env
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    return tmp_path


def _export_args(out: Path) -> list[str]:
    from test_export import fixture_window

    start, end = fixture_window()
    return [
        "export",
        "--from",
        start.isoformat(),
        "--to",
        end.isoformat(),
        "--preset",
        "generic",
        "--interval",
        "10",
        "--out",
        str(out),
    ]


@needs_convbin
def test_export_cli_writes_files(env: Path) -> None:
    from test_export import fixture_window, install_fixture_as_log

    install_fixture_as_log(env, fixture_window()[0])
    out = env / "exp"
    r = CliRunner().invoke(main, _export_args(out))
    assert r.exit_code == 0, r.output
    assert "_10S_MO.rnx" in r.output and "manifest.json" in r.output
    manifest = json.loads((out / "manifest.json").read_text())
    for f in manifest["files"]:
        assert (out / f["name"]).exists()
        assert f["name"] in r.output


@needs_convbin
def test_export_cli_ppp_preset_compresses_and_prints_warnings(env: Path) -> None:
    from test_export import fixture_window, install_fixture_as_log

    start, end = fixture_window()
    install_fixture_as_log(env, start)
    out = env / "exp"
    args = ["export", "--from", start.isoformat(), "--to", end.isoformat(), "--out", str(out)]
    r = CliRunner().invoke(main, args)  # the default preset: csrs-ppp
    assert r.exit_code == 0, r.output
    assert "_30S_MO.crx.gz" in r.output
    assert "warning: window shorter than 1 h" in r.output  # 60 s is far short of a PPP session


@needs_convbin
def test_export_cli_refuses_a_populated_out_dir_without_overwrite(env: Path) -> None:
    from test_export import fixture_window, install_fixture_as_log

    install_fixture_as_log(env, fixture_window()[0])
    out = env / "exp"
    runner = CliRunner()
    assert runner.invoke(main, _export_args(out)).exit_code == 0
    first = (out / "manifest.json").read_text()

    r = runner.invoke(main, _export_args(out))
    assert r.exit_code == 1, r.output
    assert "already holds" in r.output and "--overwrite" in r.output
    assert "Traceback" not in r.output
    assert (out / "manifest.json").read_text() == first

    r = runner.invoke(main, [*_export_args(out), "--overwrite"])
    assert r.exit_code == 0, r.output
    assert (out / "manifest.json").read_text() != first  # a fresh manifest, newer created_utc


def test_export_cli_no_data(env: Path) -> None:
    r = CliRunner().invoke(
        main,
        [
            "export",
            "--from",
            "2026-09-18T10:00:00+00:00",
            "--to",
            "2026-09-18T11:00:00+00:00",
            "--out",
            str(env / "x"),
        ],
    )
    assert r.exit_code == 1 and "no raw logs" in r.output
    assert "Traceback" not in r.output
    assert not (env / "x").exists() or not any((env / "x").iterdir())


def test_export_cli_rejects_bad_window(env: Path) -> None:
    r = CliRunner().invoke(
        main,
        [
            "export",
            "--from",
            "2026-09-18T10:00:00+00:00",
            "--to",
            "2026-09-18T10:00:00+00:00",
            "--out",
            str(env / "x"),
        ],
    )
    assert r.exit_code != 0 and "end must be after start" in r.output


@pytest.mark.parametrize(
    ("start", "expected"),
    [("yesterday", "not an ISO-8601 time"), ("2026-09-18T10:00:00", "timezone-aware")],
)
def test_export_cli_rejects_bad_times(env: Path, start: str, expected: str) -> None:
    r = CliRunner().invoke(
        main,
        ["export", "--from", start, "--to", "2026-09-18T11:00:00Z", "--out", str(env / "x")],
    )
    assert r.exit_code == 1 and expected in r.output, r.output
    assert "Traceback" not in r.output


def test_export_cli_refuses_options_a_fixed_preset_does_not_take(env: Path) -> None:
    r = CliRunner().invoke(
        main,
        [
            "export",
            "--from",
            "2026-09-18T10:00:00Z",
            "--to",
            "2026-09-18T11:00:00Z",
            "--preset",
            "csrs-ppp",
            "--interval",
            "1",
            "--out",
            str(env / "x"),
        ],
    )
    assert r.exit_code == 1 and "csrs-ppp" in r.output, r.output
    assert "Traceback" not in r.output


def test_ppp_import_cli_prints_and_saves_site(env: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(main, ["ppp-import", str(PPP / "csrs_sample.sum")])
    assert r.exit_code == 0, r.output
    assert "ITRF20" in r.output and "-26748.1720" in r.output and "csrs-ppp" in r.output
    r = runner.invoke(
        main, ["ppp-import", str(PPP / "csrs_sample.sum"), "--save-site", "roof", "--activate"]
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(main, ["sites", "list"])
    assert "* roof" in r.output and "csrs-ppp" in r.output


def test_ppp_import_cli_stores_per_axis_sigmas_frame_and_epoch(env: Path) -> None:
    import asyncio

    from mtrtk.store.db import Database
    from mtrtk.store.models import Site
    from mtrtk.store.repos import SitesRepo

    r = CliRunner().invoke(
        main, ["ppp-import", str(PPP / "csrs_sample.sum"), "--save-site", "roof"]
    )
    assert r.exit_code == 0, r.output

    async def read() -> Site | None:
        db = Database(env / "mtrtk.db")
        await db.open()
        try:
            return await SitesRepo(db).get("roof")
        finally:
            await db.close()

    site = asyncio.run(read())
    assert site is not None
    assert site.x == pytest.approx(-26748.1720, abs=1e-4)
    # CSRS-PPP reports 95 %: the stored sigmas are 1-sigma ECEF per axis.
    assert site.sigma_x == pytest.approx(0.0070 / 1.96, abs=1e-3)
    assert site.sigma_y != site.sigma_x
    assert site.frame == "ITRF20" and site.epoch == "2026.7137"
    assert site.source == "csrs-ppp" and not site.active


def test_ppp_import_cli_refuses_a_taken_site_name(env: Path) -> None:
    runner = CliRunner()
    args = ["ppp-import", str(PPP / "csrs_sample.sum"), "--save-site", "roof"]
    assert runner.invoke(main, args).exit_code == 0
    r = runner.invoke(main, args)
    assert r.exit_code == 1 and "roof" in r.output, r.output
    assert "Traceback" not in r.output


@pytest.mark.parametrize(("station", "x"), [("MTRK", "-26748.1720"), ("BAKO", "-1836969.0000")])
def test_ppp_import_cli_picks_the_configured_station_from_a_multi_site_sinex(
    env: Path, monkeypatch: pytest.MonkeyPatch, station: str, x: str
) -> None:
    monkeypatch.setenv("STATION_ID", station)
    snx = env / "AUSPOS.SNX"
    snx.write_text(MULTI_SNX)
    r = CliRunner().invoke(main, ["ppp-import", str(snx)])
    assert r.exit_code == 0, r.output
    assert x in r.output and "auspos" in r.output


def test_ppp_import_cli_bad_file(env: Path) -> None:
    bad = env / "bad.txt"
    bad.write_text("nothing here")
    r = CliRunner().invoke(main, ["ppp-import", str(bad)])
    assert r.exit_code == 1 and "could not recognise" in r.output
    assert "Traceback" not in r.output


def test_ppp_import_cli_refuses_a_file_over_the_upload_limit(env: Path) -> None:
    big = env / "big.sum"
    with big.open("wb") as fh:
        fh.truncate(20 * 1024 * 1024 + 1)
    r = CliRunner().invoke(main, ["ppp-import", str(big)])
    assert r.exit_code == 1 and "20 MB" in r.output, r.output


# ---------------------------------------------- final review: read-only, firmware, quiet logs


@needs_convbin
def test_export_cli_neither_creates_nor_migrates_the_database(env: Path) -> None:
    """A read-only command next to a live daemon: no DB is made where none was, and the convbin
    argv and migration chatter stay out of the output."""
    from test_export import fixture_window, install_fixture_as_log

    install_fixture_as_log(env, fixture_window()[0])
    r = CliRunner().invoke(main, _export_args(env / "exp"))
    assert r.exit_code == 0, r.output
    assert not (env / "mtrtk.db").exists()
    assert "running" not in r.output and "migration" not in r.output


def test_export_cli_reads_the_active_site_without_writing(env: Path) -> None:
    import asyncio
    import os

    from mtrtk.cli import _active_site_readonly, _load_settings
    from mtrtk.store.db import Database
    from mtrtk.store.models import Site
    from mtrtk.store.repos import SitesRepo

    async def seed() -> None:
        db = Database(env / "mtrtk.db")
        await db.open()
        repo = SitesRepo(db)
        await repo.add(Site.from_ecef("roof", 1.0e6, 6.0e6, 1.5e6, source="manual"))
        await repo.activate("roof")
        await db.close()

    asyncio.run(seed())
    before = os.stat(env / "mtrtk.db").st_mtime_ns
    site = asyncio.run(_active_site_readonly(_load_settings(ntrip_password="")))
    assert site is not None and site.name == "roof"
    assert os.stat(env / "mtrtk.db").st_mtime_ns == before


def test_export_cli_takes_the_firmware_from_the_window_s_sidecars(env: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from webtest import make_log

    from mtrtk.cli import _load_settings, _window_firmware
    from mtrtk.rawlog.writer import Sidecar
    from mtrtk.rinex.export import ExportRequest

    h = datetime(2026, 9, 18, 10, tzinfo=UTC)
    for i, fw in enumerate(["HPG 1.13", "HPG 1.51", ""]):
        path = make_log(env, h + timedelta(hours=i))
        sc_path = path.with_suffix(".json")
        sc = Sidecar.load(sc_path)
        sc.firmware = fw
        sc.dump(sc_path)
    settings = _load_settings(ntrip_password="")
    whole = ExportRequest(start=h, end=h + timedelta(hours=3), preset="generic")
    assert _window_firmware(settings, whole) == "HPG 1.51"  # the newest hour that recorded one
    first = ExportRequest(start=h, end=h + timedelta(hours=1), preset="generic")
    assert _window_firmware(settings, first) == "HPG 1.13"
