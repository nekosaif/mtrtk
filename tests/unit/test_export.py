"""Export pipeline: raw hourly logs -> RINEX files + manifest, per preset, on the real fixture."""

import asyncio
import gzip
import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from mtrtk.rinex.convbin import ConvbinError, ConvbinOptions, RinexHeader, convbin_available
from mtrtk.rinex.export import (
    EXPORT_ERRORS,
    ExportContext,
    ExportError,
    ExportRequest,
    export_to_dir,
    frequencies_from_state,
    header_from_settings,
    make_export_job,
)
from mtrtk.rinex.splice import NoDataError, SpliceError

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
    assert res.obs_epochs == 60 and res.version == "3.04" and res.interval_s is None
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
    assert not any("shorter than 1 h" in w for w in res.warnings)  # a PPP-service caveat only


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
    assert res.obs_epochs == 60
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


def first_last_obs(path: Path) -> tuple[datetime, datetime]:
    def stamp(line: str) -> datetime:
        y, mo, d, h, mi, sec = line.split()[:6]
        return datetime(int(y), int(mo), int(d), int(h), int(mi), int(float(sec)), tzinfo=UTC)

    lines = path.read_text().splitlines()
    first = next(line for line in lines if "TIME OF FIRST OBS" in line)
    last = next(line for line in lines if "TIME OF LAST OBS" in line)
    return stamp(first), stamp(last)


def listing(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir())


@needs_convbin
async def test_a_sub_window_is_clipped_at_both_edges_on_gpst(tmp_path: Path) -> None:
    fstart, _ = fixture_window()
    install_fixture_as_log(tmp_path / "data", fstart)
    start, end = fstart + timedelta(seconds=20), fstart + timedelta(seconds=40)
    res = await export_to_dir(
        ExportRequest(start=start, end=end, preset="generic"),
        context(tmp_path / "data"),
        tmp_path / "out",
    )
    assert res.obs_epochs == 20  # [start, end) at 1 Hz
    obs = next(f for f in res.files if f["role"] == "obs")
    first, last = first_last_obs(tmp_path / "out" / obs["name"])
    assert first == start + timedelta(seconds=18)  # GPST = UTC + 18 s
    assert last == end + timedelta(seconds=17)  # the end is exclusive


@needs_convbin
async def test_auspos_is_gzip_only(tmp_path: Path) -> None:
    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    res = await export_to_dir(
        ExportRequest(start=start, end=end, preset="auspos"),
        context(tmp_path / "data"),
        tmp_path / "out",
    )
    obs = next(f for f in res.files if f["role"] == "obs")
    nav = next(f for f in res.files if f["role"] == "nav")
    assert obs["name"].endswith("_30S_MO.rnx.gz") and nav["name"].endswith("_MN.rnx.gz")
    for f in (obs, nav):
        with gzip.open(tmp_path / "out" / f["name"], "rt") as fh:
            assert fh.readline().startswith("     3.04")
    assert any("shorter than 1 h" in w for w in res.warnings)
    assert listing(tmp_path / "out") == sorted(f["name"] for f in res.files)


@needs_convbin
async def test_generic_overrides_and_no_nav(tmp_path: Path) -> None:
    import hatanaka

    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    ctx = context(tmp_path / "data")
    gz = await export_to_dir(
        ExportRequest(start=start, end=end, preset="generic", interval_s=30, gzip=True),
        ctx,
        tmp_path / "gz",
    )
    names = [f["name"] for f in gz.files]
    assert names[0].endswith("_30S_MO.rnx.gz") and names[1].endswith("_MN.rnx.gz")
    assert 1 <= gz.obs_epochs <= 3 and gz.interval_s == 30

    crx = await export_to_dir(
        ExportRequest(start=start, end=end, preset="generic", hatanaka=True, include_nav=False),
        ctx,
        tmp_path / "crx",
    )
    assert [f["role"] for f in crx.files] == ["obs", "manifest"]
    obs = crx.files[0]["name"]
    assert obs.endswith("_MO.crx.gz")
    assert listing(tmp_path / "crx") == sorted([obs, "manifest.json"])
    text = hatanaka.decompress(tmp_path / "crx" / obs).decode()
    assert sum(1 for line in text.splitlines() if line.startswith(">")) == 60


@needs_convbin
async def test_few_navigation_messages_warn(tmp_path: Path) -> None:
    from mtrtk.core.frames import Framer

    start, end = fixture_window()
    kept, out = 0, []
    for f in Framer().feed(FIXTURE.read_bytes()):
        if f.identity == "RXM-SFRBX":
            kept += 1
            if kept > 300:  # about 9 navigation messages survive
                continue
        out.append(f.raw)
    install_fixture_as_log(tmp_path / "data", start, data=b"".join(out))
    res = await export_to_dir(
        ExportRequest(start=start, end=end, preset="generic"),
        context(tmp_path / "data"),
        tmp_path / "out",
    )
    assert 0 < res.nav_messages < 20
    assert any(f"only {res.nav_messages} navigation messages" in w for w in res.warnings)


@needs_convbin
async def test_manifest_times_are_utc(tmp_path: Path) -> None:
    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    dhaka = timezone(timedelta(hours=6))
    res = await export_to_dir(
        ExportRequest(start=start.astimezone(dhaka), end=end.astimezone(dhaka), preset="generic"),
        context(tmp_path / "data"),
        tmp_path / "out",
    )
    assert res.start == start.isoformat() and res.end == end.isoformat()
    assert res.start.endswith("+00:00")


# --- failures: what passes through, what is wrapped, and what is left on disk ------------------


def earlier_export(out: Path) -> dict[str, bytes]:
    """A directory holding another export's files and something unrelated."""
    out.mkdir(parents=True)
    files = {
        "manifest.json": b'{"preset": "csrs-ppp"}',
        "MTRK00BGD_R_20262612000_01H_30S_MO.crx.gz": b"old obs",
        "MTRK00BGD_R_20262612000_01H_MN.rnx.gz": b"old nav",
        "keep.txt": b"mine",
    }
    for name, data in files.items():
        (out / name).write_bytes(data)
    return files


def contents(out: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in out.iterdir()}


async def test_no_data_passes_through_and_keeps_an_earlier_export(tmp_path: Path) -> None:
    out = tmp_path / "out"
    before = earlier_export(out)
    t = datetime(2026, 9, 18, 20, tzinfo=UTC)
    request = ExportRequest(start=t, end=t + timedelta(hours=1), preset="csrs-ppp")
    with pytest.raises(ExportError, match="already holds"):  # same names: refused up front
        await export_to_dir(request, context(tmp_path / "data"), out)
    assert contents(out) == before
    # Told to overwrite, the export still fails without a trace: the raw logs are gone (pruned),
    # and the files of the export it would have replaced must survive that.
    with pytest.raises(NoDataError):
        await export_to_dir(request, context(tmp_path / "data"), out, overwrite=True)
    assert contents(out) == before


@needs_convbin
async def test_an_earlier_export_is_not_overwritten_unless_asked(tmp_path: Path) -> None:
    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    request = ExportRequest(start=start, end=end, preset="csrs-ppp")
    out = tmp_path / "out"
    first = await export_to_dir(request, context(tmp_path / "data"), out)
    before = contents(out)
    with pytest.raises(ExportError, match="already holds"):
        await export_to_dir(request, context(tmp_path / "data"), out)
    assert contents(out) == before
    # A different export into the same directory is refused too: its manifest would replace ours.
    with pytest.raises(ExportError, match=r"manifest\.json"):
        await export_to_dir(
            ExportRequest(start=start, end=end, preset="auspos"), context(tmp_path / "data"), out
        )
    assert contents(out) == before
    again = await export_to_dir(request, context(tmp_path / "data"), out, overwrite=True)
    assert [f["name"] for f in again.files] == [f["name"] for f in first.files]
    assert listing(out) == sorted(f["name"] for f in again.files)


async def test_a_convbin_failure_passes_through_and_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    seen: list[ConvbinOptions] = []

    async def failing(src: Path, obs: Path, nav: Path, opts: ConvbinOptions) -> Any:
        seen.append(opts)
        await asyncio.to_thread(obs.write_text, "half an observation file")
        raise ConvbinError("convbin exited 1: boom")

    monkeypatch.setattr("mtrtk.rinex.export.run_convbin", failing)
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("mine")
    ctx = ExportContext(tmp_path / "data", "MTRK", "BGD", HEADER, frequencies=3)
    with pytest.raises(ConvbinError, match="boom"):
        await export_to_dir(ExportRequest(start=start, end=end, preset="generic"), ctx, out)
    assert listing(out) == ["keep.txt"]
    assert seen[0].frequencies == 3  # the context's frequency count reaches convbin
    assert seen[0].start == (start + timedelta(seconds=18)).replace(tzinfo=None)


async def test_convbin_missing_is_a_convbin_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    with pytest.raises(ConvbinError, match="not found"):
        await export_to_dir(
            ExportRequest(start=start, end=end, preset="generic"),
            context(tmp_path / "data"),
            tmp_path / "out",
        )
    assert listing(tmp_path / "out") == []


def fake_rnx2crx(tmp_path: Path, script: str) -> Path:
    path = tmp_path / "bin" / "rnx2crx"
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(0o755)
    return path


@needs_convbin
async def test_a_compression_failure_is_an_export_error_and_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    binary = fake_rnx2crx(tmp_path, "cat >/dev/null; echo 'ERROR : not a RINEX file' >&2; exit 1\n")
    monkeypatch.setattr("mtrtk.rinex.export.rnx2crx_binary", lambda: binary)
    out = tmp_path / "out"
    with pytest.raises(ExportError, match="not a RINEX file"):
        await export_to_dir(
            ExportRequest(start=start, end=end, preset="csrs-ppp"), context(tmp_path / "data"), out
        )
    assert listing(out) == []


@needs_convbin
async def test_hatanaka_warnings_reach_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    binary = fake_rnx2crx(tmp_path, "cat; echo 'WARNING : odd epoch skipped' >&2; exit 2\n")
    monkeypatch.setattr("mtrtk.rinex.export.rnx2crx_binary", lambda: binary)
    res = await export_to_dir(
        ExportRequest(start=start, end=end, preset="csrs-ppp"),
        context(tmp_path / "data"),
        tmp_path / "out",
    )
    assert any(w.startswith("Hatanaka compression:") and "odd epoch" in w for w in res.warnings)
    assert listing(tmp_path / "out") == sorted(f["name"] for f in res.files)  # no .rnx left
    assert res.files[0]["name"].endswith(".crx.gz")


async def test_an_unusable_out_dir_is_an_export_error(tmp_path: Path) -> None:
    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    (tmp_path / "a-file").write_text("")
    with pytest.raises(ExportError, match="cannot write the export"):
        await export_to_dir(
            ExportRequest(start=start, end=end, preset="generic"),
            context(tmp_path / "data"),
            tmp_path / "a-file" / "out",
        )


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
async def test_a_read_only_out_dir_is_an_export_error(tmp_path: Path) -> None:
    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    out = tmp_path / "ro"
    out.mkdir()
    out.chmod(0o555)
    try:
        with pytest.raises(ExportError, match="cannot write the export"):
            await export_to_dir(
                ExportRequest(start=start, end=end, preset="generic"),
                context(tmp_path / "data"),
                out,
            )
    finally:
        out.chmod(0o755)


async def test_cancelled_while_a_thread_works_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mtrtk.rinex import export

    started, finished = threading.Event(), threading.Event()

    def slow_splice(root: Path, start: Any, end: Any, dest: Path, *a: Any, **k: Any) -> Any:
        started.set()
        try:
            time.sleep(0.3)  # still working when the cancel arrives
            dest.write_bytes(b"spliced")
        finally:
            finished.set()
        raise NoDataError("the result is discarded: the export was cancelled")

    monkeypatch.setattr(export, "splice_window", slow_splice)
    out = tmp_path / "out"
    out.mkdir()
    t = datetime(2026, 9, 18, 20, tzinfo=UTC)
    task = asyncio.create_task(
        export_to_dir(
            ExportRequest(start=t, end=t + timedelta(hours=1), preset="generic"),
            context(tmp_path / "data"),
            out,
        )
    )
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()  # the cancelled export waited for its thread to let go
    assert listing(out) == []


def test_the_error_set_and_the_frequency_count() -> None:
    from mtrtk.core.state import ReceiverState, Satellite, Signal

    assert set(EXPORT_ERRORS) == {SpliceError, ConvbinError, ExportError}
    state = ReceiverState()
    assert frequencies_from_state(None) == 2 and frequencies_from_state(state) == 2
    l1l2 = [Signal(sig_id=0, name="L1C/A"), Signal(sig_id=3, name="L2CL")]
    state.sats = [Satellite(gnss_id=0, gnss="GPS", sv_id=1, signals=l1l2)]
    assert frequencies_from_state(state) == 2
    state.sats[0].signals.append(Signal(sig_id=7, name="L5Q"))
    assert frequencies_from_state(state) == 3


async def test_an_option_convbin_cannot_take_is_an_export_error(tmp_path: Path) -> None:
    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    ctx = ExportContext(tmp_path / "data", "MTRK", "BGD", HEADER, frequencies=9)
    with pytest.raises(ExportError, match="invalid conversion options: frequencies"):
        await export_to_dir(
            ExportRequest(start=start, end=end, preset="generic"), ctx, tmp_path / "o"
        )
    assert listing(tmp_path / "o") == []
