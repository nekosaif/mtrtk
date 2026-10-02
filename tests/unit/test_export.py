"""Export pipeline: raw hourly logs -> RINEX files + manifest, per preset, on the real fixture."""

import asyncio
import gzip
import json
import os
import re
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from rinextest import FIXTURE, needs_convbin

from mtrtk.rinex.convbin import ConvbinError, ConvbinOptions, RinexHeader
from mtrtk.rinex.export import (
    EXPORT_ERRORS,
    ExportContext,
    ExportError,
    ExportRequest,
    export_to_dir,
    frequencies_from_state,
    header_from_settings,
    _PathNames,
    make_export_job,
)
from mtrtk.rinex.splice import NoDataError, SpliceError

HEADER = RinexHeader(
    marker_name="MTRK", observer="mtrtk", agency="mtrtk", receiver_version="HPG 1.13"
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
    site: str | None = None,
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
        site=site,
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


def test_header_from_settings_takes_the_site_and_never_the_live_fix() -> None:
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
    # The live fix is where the antenna is now, not where an older window was logged: with no
    # site the position is left to convbin, which computes it from the exported data.
    assert header_from_settings(settings, state, None).approx_xyz is None
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

    async def failing(src: Path, obs: Path, nav: Path, opts: ConvbinOptions, **_: Any) -> Any:
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
    assert not (tmp_path / "out").exists()  # the export made it, and took it away again


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
    assert not out.exists()


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
    assert not (tmp_path / "o").exists()


# --------------------------------------------------- final review: space, lock, coverage, leash


class _Usage:
    def __init__(self, free: int) -> None:
        self.free = free


async def test_an_export_the_card_has_no_room_for_is_refused_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mtrtk.rinex import export as export_mod

    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    raw = FIXTURE.stat().st_size
    seen: list[Path] = []

    def usage(path: Path) -> _Usage:
        seen.append(path)
        return _Usage(raw)  # room for the spliced UBX, not for the observation file as well

    monkeypatch.setattr(export_mod, "_disk_usage", usage)
    out = tmp_path / "out"
    with pytest.raises(ExportError, match="not enough free space") as err:
        await export_to_dir(
            ExportRequest(start=start, end=end, preset="generic"), context(tmp_path / "data"), out
        )
    assert "GB is free" in str(err.value)
    assert seen and seen[0] == tmp_path  # the nearest directory that exists
    assert not out.exists() or list(out.iterdir()) == []


def test_the_space_check_keeps_half_of_min_free_and_no_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MIN_FREE_GB itself is not the limit: retention holds a full card right at it, so that rule
    would refuse every export. Half of it stays free for live logging."""
    from mtrtk.rinex import export as export_mod

    need = 2_000_000_000
    monkeypatch.setattr(export_mod, "_disk_usage", lambda p: _Usage(5_000_000_000))
    export_mod._check_space(tmp_path, need, min_free_gb=5.0)  # 3 GB left >= 2.5 GB reserve
    with pytest.raises(ExportError, match="3.50 GB stays free"):
        export_mod._check_space(tmp_path, need, min_free_gb=7.0)  # 3 GB left < 3.5 GB


def test_space_needed_counts_the_lead_hour_and_shrinks_with_the_interval(tmp_path: Path) -> None:
    from mtrtk.rinex import export as export_mod
    from mtrtk.rinex.presets import resolve_options

    root = tmp_path / "data"
    install_fixture_as_log(root, H := datetime(2026, 9, 18, 10, tzinfo=UTC), data=b"x" * 1000)
    install_fixture_as_log(root, H + timedelta(hours=1), data=b"x" * 1000)
    install_fixture_as_log(root, H + timedelta(hours=1), station="OTHR", data=b"x" * 5000)
    req = ExportRequest(start=H + timedelta(hours=1), end=H + timedelta(hours=2), preset="generic")
    native = export_mod._space_needed(req, context(root), resolve_options("generic"))
    assert native == int(2000 * (1 + export_mod.OBS_PER_RAW))  # lead hour + window, one station
    decimated = export_mod._space_needed(req, context(root), resolve_options("csrs-ppp"))
    assert 2000 < decimated < native


async def test_a_second_export_on_the_same_data_dir_is_refused(tmp_path: Path) -> None:
    """`mtrtk export` next to a running daemon: both take DATA_DIR's export lock."""
    import fcntl

    from mtrtk.rinex.export import EXPORT_BUSY, LOCK_NAME

    start, end = fixture_window()
    root = tmp_path / "data"
    install_fixture_as_log(root, start)
    with (root / LOCK_NAME).open("ab") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ExportError) as err:
            await export_to_dir(
                ExportRequest(start=start, end=end, preset="generic"), context(root), tmp_path / "o"
            )
    assert str(err.value) == EXPORT_BUSY


async def test_convbin_gets_a_leash_that_grows_with_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mtrtk.rinex.convbin import DEFAULT_TIMEOUT_S
    from mtrtk.rinex.export import LEASH_BYTES_PER_S

    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    leashes: list[float] = []

    async def fake(src: Path, obs: Path, nav: Path, opts: ConvbinOptions, **kw: Any) -> Any:
        leashes.append(kw["timeout_s"])
        raise ConvbinError("stop here")

    monkeypatch.setattr("mtrtk.rinex.export.run_convbin", fake)
    with pytest.raises(ConvbinError):
        await export_to_dir(
            ExportRequest(start=start, end=end, preset="generic"),
            context(tmp_path / "data"),
            tmp_path / "out",
        )
    assert leashes == [DEFAULT_TIMEOUT_S + FIXTURE.stat().st_size / LEASH_BYTES_PER_S]


def test_coverage_warnings_name_the_data_actually_exported() -> None:
    from mtrtk.rinex.export import _coverage_warnings

    h = datetime(2026, 10, 1, 5, tzinfo=UTC)
    hour = ExportRequest(start=h, end=h + timedelta(hours=1), preset="auspos")
    # 14 min of data in a 1 h AUSPOS window: both the coverage and the 1 h minimum are named.
    span = (datetime(2026, 10, 1, 5, 46, 30), datetime(2026, 10, 1, 6, 0, 0))
    warns = _coverage_warnings(hour, 30.0, 28, span)
    assert any("covers only 14 min of the 60 min window" in w and "05:46:30" in w for w in warns)
    assert any("AUSPOS refuses less than 1 h" in w for w in warns)
    # A full hour says nothing.
    full = (datetime(2026, 10, 1, 5, 0, 0), datetime(2026, 10, 1, 5, 59, 30))
    assert _coverage_warnings(hour, 30.0, 120, full) == []
    # A day with four hours missing in the middle: the span is whole, the epoch count is not.
    day = ExportRequest(start=h, end=h + timedelta(hours=24), preset="csrs-ppp")
    whole = (datetime(2026, 10, 1, 5, 0, 0), datetime(2026, 10, 2, 4, 59, 30))
    gaps = _coverage_warnings(day, 30.0, 20 * 120, whole)
    assert len(gaps) == 1 and "covers only 20.0 h of the 24.0 h window" in gaps[0]
    # Generic at the native rate: no interval, so the span alone decides.
    generic = ExportRequest(start=h, end=h + timedelta(hours=1), preset="generic")
    assert len(_coverage_warnings(generic, None, 3600, span)) == 1
    assert _coverage_warnings(generic, None, 0, None) == []


@needs_convbin
async def test_a_partial_hour_export_says_how_little_it_holds(tmp_path: Path) -> None:
    """The live case: a 1 h AUSPOS window over logs that start part-way through the hour."""
    start, _ = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    hour = start.replace(minute=0, second=0, microsecond=0)
    res = await export_to_dir(
        ExportRequest(start=hour, end=hour + timedelta(hours=1), preset="auspos"),
        context(tmp_path / "data"),
        tmp_path / "out",
    )
    assert any("covers only 1 min of the 60 min window" in w for w in res.warnings), res.warnings
    assert any("AUSPOS refuses less than 1 h" in w for w in res.warnings)
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert manifest["warnings"] == res.warnings


@needs_convbin
async def test_shipped_rinex_carries_no_host_path(tmp_path: Path) -> None:
    """convbin copies its input path into a `log:` COMMENT; the files go to NRCan, GA and NGS."""
    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    res = await export_to_dir(
        ExportRequest(start=start, end=end, preset="generic"),
        context(tmp_path / "data"),
        tmp_path / "out",
    )
    for f in res.files:
        text = (tmp_path / "out" / f["name"]).read_text()
        assert str(tmp_path) not in text and "/tmp" not in text
    obs = next(f["name"] for f in res.files if f["role"] == "obs")
    assert "log: spliced.ubx" in (tmp_path / "out" / obs).read_text()


def test_frequencies_from_firmware() -> None:
    from mtrtk.rinex.export import frequencies_from_firmware

    assert frequencies_from_firmware("HPG 1.13") == 2
    assert frequencies_from_firmware("HPG 1.51") == 3
    assert frequencies_from_firmware("HPG 2.00") == 3
    assert frequencies_from_firmware("") == 2 and frequencies_from_firmware("TIM 2.20") == 2


# --- parked minors (2026-10-02 triage): overwrite rollback, an out dir left behind, the lock ----


@needs_convbin
async def test_a_failed_overwrite_puts_back_the_files_it_was_replacing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`overwrite=True` with a move that fails half-way: the earlier export is still whole."""
    start, end = fixture_window()
    install_fixture_as_log(tmp_path / "data", start)
    request = ExportRequest(start=start, end=end, preset="generic")
    out = tmp_path / "out"
    await export_to_dir(request, context(tmp_path / "data"), out)
    (out / "keep.txt").write_text("mine")
    before = contents(out)
    real_replace = os.replace
    published: list[str] = []

    def failing_replace(src: Any, dst: Any) -> None:
        moving_in = Path(src).parent.name.startswith(".export-")
        if Path(dst).parent == out and moving_in and not Path(src).name.startswith(".old-"):
            published.append(Path(dst).name)
            if len(published) == 2:  # the second file of the export to land in out_dir
                raise OSError(28, "No space left on device", str(dst))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(ExportError, match="No space left"):
        await export_to_dir(request, context(tmp_path / "data"), out, overwrite=True)
    assert len(published) == 2
    assert contents(out) == before  # neither the new export nor a gap where the old one was


async def test_a_failed_export_removes_the_out_dir_it_created(tmp_path: Path) -> None:
    t = datetime(2026, 9, 18, 20, tzinfo=UTC)
    request = ExportRequest(start=t, end=t + timedelta(hours=1), preset="generic")
    (tmp_path / "data").mkdir()
    out = tmp_path / "exports" / "today"
    with pytest.raises(NoDataError):
        await export_to_dir(request, context(tmp_path / "data"), out)
    assert not (tmp_path / "exports").exists()  # every directory the export made is gone
    # A directory that was already there stays, even when the export left it empty.
    out.mkdir(parents=True)
    with pytest.raises(NoDataError):
        await export_to_dir(request, context(tmp_path / "data"), out)
    assert out.is_dir() and listing(out) == []


@needs_convbin
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@pytest.mark.parametrize("lock_exists", [False, True], ids=["no-lock-file", "read-only-lock"])
async def test_an_export_from_a_read_only_data_dir_works(tmp_path: Path, lock_exists: bool) -> None:
    """`mtrtk export` run by a user who can read DATA_DIR but not write it (a daemon-owned
    volume, a card mounted read-only): the lock is taken read-only, or skipped."""
    import fcntl

    from mtrtk.rinex.export import LOCK_NAME

    start, end = fixture_window()
    root = tmp_path / "data"
    install_fixture_as_log(root, start)
    if lock_exists:
        (root / LOCK_NAME).write_bytes(b"")
        (root / LOCK_NAME).chmod(0o444)
    root.chmod(0o555)
    try:
        res = await export_to_dir(
            ExportRequest(start=start, end=end, preset="generic"), context(root), tmp_path / "out"
        )
        assert res.obs_epochs > 0
        if lock_exists:  # still a real lock: a second exporter on that DATA_DIR is refused
            with (root / LOCK_NAME).open("rb") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with pytest.raises(ExportError, match="another export"):
                    await export_to_dir(
                        ExportRequest(start=start, end=end, preset="generic"),
                        context(root),
                        tmp_path / "out2",
                    )
    finally:
        root.chmod(0o755)


# ------------------------------------------- errors that reach an API client name no host path

# An absolute path: a "/" that starts the text or follows something other than a name character
# ("DATA_DIR/jobs/x" is relative), followed by a directory component.
HOST_PATH = re.compile(r"(?<![\w.~-])/[\w.-]+/")


def assert_no_host_path(msg: str, tmp_path: Path) -> None:
    assert str(tmp_path) not in msg, msg
    assert not HOST_PATH.search(msg), msg


async def test_an_export_jobs_refusal_names_its_directory_relative_to_data_dir(
    tmp_path: Path,
) -> None:
    from mtrtk.jobs import Job, JobContext

    root = tmp_path / "data"
    job_dir = root / "jobs" / "abc123"
    job_dir.mkdir(parents=True)
    (job_dir / "manifest.json").write_text("{}")

    async def report(p: float, m: str | None) -> None:
        pass

    t = datetime(2026, 9, 18, 20, tzinfo=UTC)
    request = ExportRequest(start=t, end=t + timedelta(hours=1), preset="generic")
    job = Job(id="abc123", kind="export", status="running", created_utc=datetime.now(UTC))
    with pytest.raises(ExportError) as err:
        await make_export_job(request, context(root))(JobContext(job, job_dir, report))
    assert "DATA_DIR/jobs/abc123 already holds manifest.json" in str(err.value)
    assert_no_host_path(str(err.value), tmp_path)


async def test_an_unwritable_dir_is_named_relative_to_data_dir_or_in_full(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    (root / "a-file").write_text("")
    out = root / "a-file" / "out"
    t = datetime(2026, 9, 18, 20, tzinfo=UTC)
    request = ExportRequest(start=t, end=t + timedelta(hours=1), preset="generic")
    with pytest.raises(ExportError) as err:
        await export_to_dir(request, context(root), out, relative_paths=True)
    msg = str(err.value)
    assert msg.startswith("cannot write the export into DATA_DIR/a-file/out: ")
    assert "(DATA_DIR/a-file/out)" in msg  # the failing path too, relative to DATA_DIR
    assert_no_host_path(msg, tmp_path)
    with pytest.raises(ExportError) as err:  # the CLI's: the path the operator typed
        await export_to_dir(request, context(root), out)
    assert f"cannot write the export into {out}: " in str(err.value)


async def test_an_out_dir_outside_data_dir_is_named_by_its_role(tmp_path: Path) -> None:
    out = tmp_path / "elsewhere"
    earlier_export(out)
    t = datetime(2026, 9, 18, 20, tzinfo=UTC)
    request = ExportRequest(start=t, end=t + timedelta(hours=1), preset="csrs-ppp")
    with pytest.raises(ExportError) as err:
        await export_to_dir(request, context(tmp_path / "data"), out, relative_paths=True)
    assert str(err.value).startswith("the export directory already holds ")
    assert_no_host_path(str(err.value), tmp_path)
    with pytest.raises(ExportError) as err:
        await export_to_dir(request, context(tmp_path / "data"), out)
    assert str(err.value).startswith(f"{out} already holds ")


async def test_a_convbin_message_naming_the_staging_dir_is_made_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start, end = fixture_window()
    root = tmp_path / "data"
    install_fixture_as_log(root, start)

    async def cannot_prepare(src: Path, obs: Path, nav: Path, *_: Any, **__: Any) -> Any:
        raise ConvbinError(
            f"cannot prepare the output files: [Errno 13] Permission denied: '{obs}'"
        )

    monkeypatch.setattr("mtrtk.rinex.export.run_convbin", cannot_prepare)
    request = ExportRequest(start=start, end=end, preset="generic")
    with pytest.raises(ConvbinError) as err:  # the same error type: callers map it as before
        await export_to_dir(request, context(root), root / "out", relative_paths=True)
    msg = str(err.value)
    assert "Permission denied: 'DATA_DIR/out/.export-" in msg and msg.endswith("_MO.rnx'")
    assert_no_host_path(msg, tmp_path)
    with pytest.raises(ConvbinError) as err:
        await export_to_dir(request, context(root), root / "out")
    assert f"'{root / 'out'}/.export-" in str(err.value)


@needs_convbin
async def test_a_missing_rnx2crx_is_named_without_its_install_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start, end = fixture_window()
    root = tmp_path / "data"
    install_fixture_as_log(root, start)
    monkeypatch.setattr("mtrtk.rinex.export.rnx2crx_binary", lambda: tmp_path / "gone" / "rnx2crx")
    with pytest.raises(ExportError, match="cannot run the bundled rnx2crx") as err:
        await export_to_dir(
            ExportRequest(start=start, end=end, preset="csrs-ppp"),
            context(root),
            root / "out",
            relative_paths=True,
        )
    assert_no_host_path(str(err.value), tmp_path)


def test_path_names_replace_only_whole_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DATA_DIR given as a relative path (`DATA_DIR=data` on a dev box) used to rewrite the
    word "data" in any message, and a path with no left boundary matched inside another one."""
    monkeypatch.chdir(tmp_path)
    shown = _PathNames(Path("data/out"), Path("data"), relative=True)
    message = "the window has no data in it; see /srv/mydata and data-1"
    assert shown.text(message) == message
    assert shown.text(f"cannot open {tmp_path}/data/jobs/f") == "cannot open DATA_DIR/jobs/f"

    shown = _PathNames(Path("/d/out"), Path("/d"), relative=True)
    assert shown.text("cannot open /x/d/jobs/f") == "cannot open /x/d/jobs/f"
    assert shown.text("cannot open '/d/jobs/f'") == "cannot open 'DATA_DIR/jobs/f'"
    assert shown.text("Permission denied: /d/out/x") == "Permission denied: DATA_DIR/out/x"
