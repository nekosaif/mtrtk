"""Export pipeline: raw hourly logs -> RINEX files + manifest, per preset, on the real fixture."""

import asyncio
import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mtrtk.rinex.convbin import RinexHeader, convbin_available
from mtrtk.rinex.export import (
    ExportContext,
    ExportError,
    ExportRequest,
    export_to_dir,
    header_from_settings,
    make_export_job,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_raw_60s.ubx"
HEADER = RinexHeader(
    marker_name="MTRK", observer="mtrtk", agency="mtrtk", receiver_version="HPG 1.13"
)

needs_convbin = pytest.mark.skipif(
    not convbin_available() or not FIXTURE.exists(), reason="convbin or fixture missing"
)


def fixture_window() -> tuple[datetime, datetime]:
    """The fixture's own UTC window, read from its first/last NAV-PVT."""
    from pyubx2 import UBXReader

    from mtrtk.core.frames import Framer

    times = []
    for f in Framer().feed(FIXTURE.read_bytes()):
        if f.identity == "NAV-PVT":
            m = UBXReader.parse(f.raw)
            if m.validDate and m.validTime:
                times.append(datetime(m.year, m.month, m.day, m.hour, m.min, m.second, tzinfo=UTC))
    return times[0], times[-1] + timedelta(seconds=1)


def install_fixture_as_log(
    root: Path,
    start: datetime,
    *,
    station: str = "MTRK",
    data: bytes | None = None,
    complete: bool = True,
) -> None:
    from mtrtk.rawlog.writer import Sidecar, log_path, sidecar_path

    hour = start.replace(minute=0, second=0, microsecond=0)
    path = log_path(root, station, hour)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(FIXTURE.read_bytes() if data is None else data)
    Sidecar(
        station,
        "base",
        start.isoformat(),
        hour_utc=hour.isoformat(),
        bytes=path.stat().st_size,
        complete=complete,
    ).dump(sidecar_path(path))


def context(root: Path, country: str = "BGD") -> ExportContext:
    return ExportContext(root=root, station_id="MTRK", country=country, header=HEADER)


@needs_convbin
async def test_generic_export_writes_rinex_and_manifest(tmp_path: Path) -> None:
    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    progress: list[tuple[float, str | None]] = []

    async def report(p: float, m: str | None) -> None:
        progress.append((p, m))

    res = await export_to_dir(
        ExportRequest(start=start, end=end, preset="generic"),
        context(tmp_path / "data"),
        tmp_path / "out",
        progress=report,
    )
    obs = next(f for f in res.files if f["role"] == "obs")
    nav = next(f for f in res.files if f["role"] == "nav")
    assert obs["name"].endswith("_MO.rnx") and nav["name"].endswith("_MN.rnx")
    assert obs["name"].startswith("MTRK00BGD_R_")
    # 60 epochs of UTC window: the request is shifted onto GPST, or 18 of them would be lost.
    assert res.obs_epochs >= 50 and res.version == "3.04" and res.interval_s is None
    first_obs = next(
        line
        for line in (tmp_path / "out" / obs["name"]).read_text().splitlines()
        if "TIME OF FIRST OBS" in line
    )
    y, mo, d, h, mi, s = first_obs.split()[:6]
    first = datetime(int(y), int(mo), int(d), int(h), int(mi), int(float(s)), tzinfo=UTC)
    assert first == start + timedelta(seconds=18)  # GPST = UTC + 18 s
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert manifest["preset"] == "generic" and manifest["obs_epochs"] == res.obs_epochs
    assert manifest["files"] == res.files
    for f in res.files:  # every size is the size on disk, the manifest's own included
        assert (tmp_path / "out" / f["name"]).stat().st_size == f["bytes"]
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == sorted(
        f["name"] for f in res.files
    )
    assert progress[0][0] < progress[-1][0] == 1.0


@needs_convbin
async def test_csrs_preset_decimates_and_compresses(tmp_path: Path) -> None:
    import hatanaka

    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    res = await export_to_dir(
        ExportRequest(start=start, end=end, preset="csrs-ppp"),
        context(tmp_path / "data"),
        tmp_path / "out",
    )
    obs = next(f for f in res.files if f["role"] == "obs")
    assert obs["name"].endswith("_30S_MO.crx.gz")
    nav = next(f for f in res.files if f["role"] == "nav")
    assert nav["name"].endswith("_MN.rnx.gz")
    assert 1 <= res.obs_epochs <= 3  # 60 s at 30 s
    with gzip.open(tmp_path / "out" / nav["name"], "rt") as fh:
        assert fh.readline().startswith("     3.04")
    # The Hatanaka file decompresses back to RINEX 3.04 observations.
    text = hatanaka.decompress(tmp_path / "out" / obs["name"]).decode()
    assert text.startswith("     3.04") and "OBSERVATION DATA" in text.splitlines()[0]
    assert any("shorter than 1 h" in w for w in res.warnings)


@needs_convbin
async def test_opus_preset_is_rinex2_gps_only(tmp_path: Path) -> None:
    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    res = await export_to_dir(
        ExportRequest(start=start, end=end, preset="opus"),
        context(tmp_path / "data"),
        tmp_path / "out",
    )
    obs = next(f for f in res.files if f["role"] == "obs")
    assert obs["name"] == f"mtrk{start:%j}{chr(ord('a') + start.hour)}.{start:%y}o"
    text = (tmp_path / "out" / obs["name"]).read_text()
    assert text.startswith("     2.11") and "G: GPS" in text.splitlines()[0]
    assert any("L2C" in w for w in res.warnings)


async def test_export_request_validation() -> None:
    from pydantic import ValidationError

    t = datetime(2026, 9, 18, tzinfo=UTC)
    with pytest.raises(ValidationError):
        ExportRequest(start=t, end=t)
    with pytest.raises(ValidationError):
        ExportRequest(start=t, end=t + timedelta(days=8))
    with pytest.raises(ValidationError):
        ExportRequest(start=t, end=t + timedelta(hours=1), preset="csrs-ppp", interval_s=1)
    with pytest.raises(ValidationError):
        ExportRequest(start=t, end=t + timedelta(hours=1), preset="nope")
    with pytest.raises(ValidationError):
        ExportRequest(start=t.replace(tzinfo=None), end=t.replace(tzinfo=None) + timedelta(hours=1))


async def test_a_bad_country_setting_fails_before_any_work(tmp_path: Path) -> None:
    t = datetime(2026, 9, 18, tzinfo=UTC)
    request = ExportRequest(start=t, end=t + timedelta(hours=1), preset="generic")
    with pytest.raises(ExportError, match="COUNTRY"):
        await export_to_dir(request, context(tmp_path / "data", "Bangladesh"), tmp_path / "out")
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


@needs_convbin
async def test_a_window_without_ephemeris_says_so_and_ships_no_nav_file(tmp_path: Path) -> None:
    from mtrtk.core.frames import Framer

    start, end = fixture_window()
    no_sfrbx = b"".join(
        f.raw for f in Framer().feed(FIXTURE.read_bytes()) if f.identity != "RXM-SFRBX"
    )
    install_fixture_as_log(tmp_path / "data", start, data=no_sfrbx)
    res = await export_to_dir(
        ExportRequest(start=start, end=end, preset="generic"),
        context(tmp_path / "data"),
        tmp_path / "out",
    )
    assert res.nav_messages == 0
    assert [f["role"] for f in res.files] == ["obs", "manifest"]
    assert any("no navigation messages" in w for w in res.warnings)
    assert not any(p.name.endswith("_MN.rnx") for p in (tmp_path / "out").iterdir())


@needs_convbin
async def test_an_open_hour_warns_and_another_station_is_ignored(tmp_path: Path) -> None:
    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start, complete=False)
    install_fixture_as_log(tmp_path / "data", start, station="OTHR")
    res = await export_to_dir(
        ExportRequest(start=start, end=end, preset="generic"),
        context(tmp_path / "data"),
        tmp_path / "out",
    )
    assert res.obs_epochs >= 50
    assert any("still being written" in w for w in res.warnings)


@needs_convbin
async def test_export_job_runs_through_the_job_runner(tmp_path: Path) -> None:
    from mtrtk.core.bus import Bus
    from mtrtk.jobs import JobRunner
    from mtrtk.store.db import Database

    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    request = ExportRequest(start=start, end=end, preset="csrs-ppp")
    db = Database(tmp_path / "m.db")
    await db.open()
    runner = JobRunner(db, Bus(), tmp_path / "jobs")
    try:
        job = await runner.submit(
            "export",
            request.model_dump(mode="json"),
            make_export_job(request, context(tmp_path / "data")),
        )
        for _ in range(1000):
            done = await runner.get(job.id)
            assert done is not None
            if done.status in ("done", "failed"):
                break
            await asyncio.sleep(0.01)
        assert done.status == "done", done.error
        assert done.result is not None
        on_disk = json.loads((runner.job_dir(job.id) / "manifest.json").read_text())
        assert done.result == on_disk
        names = {f["name"] for f in on_disk["files"]}
        assert {p.name for p in runner.job_dir(job.id).iterdir()} == names
    finally:
        await runner.shutdown()
        await db.close()


def test_header_from_settings_prefers_the_site_then_the_live_position() -> None:
    from mtrtk.config import Settings
    from mtrtk.core.state import ReceiverState
    from mtrtk.store.models import Site

    settings = Settings(
        _env_file=None,
        ntrip_password="",
        marker_name="ROOF",
        antenna_type="ANN-MB-00",
        antenna_height_m=1.234,
    )
    state = ReceiverState()
    state.firmware.fw_version = "HPG 1.13"
    state.position.ecef_x_m, state.position.ecef_y_m, state.position.ecef_z_m = 1.0, 2.0, 3.0
    site = Site(name="roof", x=10.0, y=20.0, z=30.0, source="manual")

    h = header_from_settings(settings, state, site)
    assert h.marker_name == "ROOF" and h.marker_number == "MTRK"
    assert h.antenna_type == "ANN-MB-00" and h.receiver_version == "HPG 1.13"
    assert h.approx_xyz == (10.0, 20.0, 30.0) and h.delta_hen == (1.234, 0.0, 0.0)
    assert header_from_settings(settings, state, None).approx_xyz == (1.0, 2.0, 3.0)
    offline = header_from_settings(settings, None, None)
    assert offline.approx_xyz is None and offline.receiver_version == "unknown"
