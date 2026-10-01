"""PPK pipeline end to end on the real 60 s fixture: zero baseline (rover = base) with RTKLIB."""

import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_export import fixture_window, install_fixture_as_log

from mtrtk.ppk.pipeline import (
    BaseSource,
    PpkContext,
    PpkError,
    PpkRequest,
    RoverSource,
    fetch_chunks,
    make_ppk_job,
    run_ppk,
)
from mtrtk.ppk.rtkconf import rnx2rtkp_available
from mtrtk.rinex.convbin import ConvbinOptions, RinexHeader, convbin_available, run_convbin
from mtrtk.rinex.rinexhdr import obs_span, read_header, sniff_format
from mtrtk.rover.sessions import SessionsRepo
from mtrtk.store.db import Database
from mtrtk.store.models import Site
from mtrtk.store.repos import SitesRepo

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_raw_60s.ubx"
HEADER = RinexHeader(
    marker_name="MTRK", observer="mtrtk", agency="mtrtk", receiver_version="HPG 1.13"
)
XYZ = (-26748.172, 5837156.618, 2561801.261)
REQUIRE = os.environ.get("MTRTK_REQUIRE_CONVBIN") == "1"
pytestmark = pytest.mark.skipif(
    not REQUIRE and not (convbin_available() and rnx2rtkp_available() and FIXTURE.exists()),
    reason="RTKLIB (convbin, rnx2rtkp) or the raw fixture is missing",
)


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    db = Database(tmp_path / "mtrtk.db")
    await db.open()
    c = PpkContext(root=tmp_path, station_id="MTRK", country="BGD", header=HEADER, db=db)
    try:
        yield c
    finally:
        await db.close()


def _window_rover() -> RoverSource:
    start, end = fixture_window()
    return RoverSource(kind="window", start=start, end=end)


async def test_zero_baseline_from_local_logs_and_uploaded_base(
    ctx: PpkContext, tmp_path: Path
) -> None:
    start, _ = fixture_window()
    install_fixture_as_log(tmp_path, start)
    await SitesRepo(ctx.db).add(Site.from_ecef("roof", *XYZ, source="manual"))
    progress: list[float] = []

    async def report(p: float, m: str | None) -> None:
        progress.append(p)

    req = PpkRequest(
        rover=_window_rover(), base=BaseSource(kind="upload", path_ubx=FIXTURE), base_site="roof"
    )
    result = await run_ppk(req, ctx, tmp_path / "out", progress=report)
    out = tmp_path / "out"
    for name in (
        "rover.rnx",
        "base.rnx",
        "ppk.conf",
        "track.pos",
        "track.csv",
        "track.geojson",
        "track.kml",
        "summary.json",
        "rnx2rtkp.log",
    ):
        assert (out / name).exists(), name
    assert not (out / "rover.ubx").exists()  # the spliced scratch copy is gone
    summary = json.loads((out / "summary.json").read_text())
    assert summary["summary"]["epochs"] > 20
    # zero baseline: nothing worse than float
    assert summary["summary"]["fixed_pct"] + summary["summary"]["float_pct"] >= 99.0
    assert summary["inputs"]["base_xyz"] == list(XYZ)
    assert summary["inputs"]["base_xyz_source"] == "site:roof"
    assert progress[-1] == 1.0 and result["files"]
    assert {f["name"] for f in result["files"]} >= {"track.pos", "track.csv", "ppk.conf"}
    # the rover window reached convbin on GPST, so the first solution is not 18 s late
    first = datetime.fromisoformat(summary["summary"]["first_time"])
    assert abs((first - (start + timedelta(seconds=18))).total_seconds()) <= 2


async def test_remote_base_over_http(ctx: PpkContext, tmp_path: Path) -> None:
    from webtest import make_ctx

    from mtrtk.web.app import create_app

    start, _ = fixture_window()
    install_fixture_as_log(tmp_path, start)  # rover logs
    base_ctx = await make_ctx(tmp_path / "basehost")
    install_fixture_as_log(tmp_path / "basehost", start)
    await SitesRepo(base_ctx.db).add(Site.from_ecef("remote-roof", *XYZ, source="csrs-ppp"))
    await SitesRepo(base_ctx.db).activate("remote-roof")
    app = create_app(base_ctx)
    transport = httpx.ASGITransport(app=app)
    ctx.http = lambda: httpx.AsyncClient(transport=transport, base_url="http://base")
    try:
        req = PpkRequest(rover=_window_rover(), base=BaseSource(kind="remote", url="http://base"))
        result = await run_ppk(req, ctx, tmp_path / "out")
        assert result["inputs"]["base_xyz_source"] == "remote-site:remote-roof"
        assert result["inputs"]["base"]["remote_bytes"] == FIXTURE.stat().st_size
        assert result["summary"]["epochs"] > 20
        assert not (tmp_path / "out" / "base.ubx").exists()
    finally:
        await base_ctx.db.close()


async def test_remote_base_with_a_password(ctx: PpkContext, tmp_path: Path) -> None:
    from webtest import make_ctx

    from mtrtk.web.app import create_app

    start, _ = fixture_window()
    install_fixture_as_log(tmp_path, start)
    base_ctx = await make_ctx(tmp_path / "basehost", web_password="s3cret")
    install_fixture_as_log(tmp_path / "basehost", start)
    transport = httpx.ASGITransport(app=create_app(base_ctx))
    ctx.http = lambda: httpx.AsyncClient(transport=transport, base_url="http://base")
    try:
        anonymous = PpkRequest(
            rover=_window_rover(), base=BaseSource(kind="remote", url="http://base"), base_xyz=XYZ
        )
        with pytest.raises(PpkError, match="password"):
            await run_ppk(anonymous, ctx, tmp_path / "out1")
        signed = PpkRequest(
            rover=_window_rover(),
            base=BaseSource(kind="remote", url="http://base", password="s3cret"),
            base_xyz=XYZ,
        )
        result = await run_ppk(signed, ctx, tmp_path / "out2")
        assert result["summary"]["epochs"] > 20
        assert "s3cret" not in (tmp_path / "out2" / "summary.json").read_text()
    finally:
        await base_ctx.db.close()


async def test_uploaded_rover_against_local_base_logs(ctx: PpkContext, tmp_path: Path) -> None:
    """An uploaded rover file has no window of its own: it is read from its observations."""
    start, _ = fixture_window()
    install_fixture_as_log(tmp_path, start)
    renamed = tmp_path / "ellipse_gps1_raw.bin"  # the content decides, not the suffix
    shutil.copy(FIXTURE, renamed)
    req = PpkRequest(
        rover=RoverSource(kind="upload", path=renamed), base=BaseSource(kind="local"), base_xyz=XYZ
    )
    result = await run_ppk(req, ctx, tmp_path / "out")
    assert result["summary"]["epochs"] > 20
    w0, w1 = (datetime.fromisoformat(t) for t in result["inputs"]["window"])
    assert abs((w0 - start).total_seconds()) <= 2 and w1 > w0


async def test_session_rover_and_rinex_base_with_header_position(
    ctx: PpkContext, tmp_path: Path
) -> None:
    start, end = fixture_window()
    install_fixture_as_log(tmp_path, start)
    base = await run_convbin(
        FIXTURE,
        tmp_path / "b.obs",
        tmp_path / "b.nav",
        ConvbinOptions(header=RinexHeader("BASE", "o", "a", "HPG 1.13", approx_xyz=XYZ)),
    )
    session = await SessionsRepo(ctx.db).start("walk", "rover")
    assert session.id is not None
    await ctx.db.execute(
        "UPDATE sessions SET start_utc = ?, end_utc = ? WHERE id = ?",
        (start.isoformat(), end.isoformat(), session.id),
    )
    req = PpkRequest(
        rover=RoverSource(kind="session", session_id=session.id),
        base=BaseSource(kind="upload", path_obs=base.obs_path, path_nav=base.nav_path),
    )
    result = await run_ppk(req, ctx, tmp_path / "out")
    assert result["inputs"]["base_xyz_source"] == "rinex-header"
    assert result["inputs"]["base_xyz"] == pytest.approx(list(XYZ), abs=1e-3)
    assert result["summary"]["epochs"] > 20
    with pytest.raises(PpkError, match="session 999"):
        await run_ppk(
            PpkRequest(
                rover=RoverSource(kind="session", session_id=999),
                base=BaseSource(kind="local"),
                base_xyz=XYZ,
            ),
            ctx,
            tmp_path / "out2",
        )


async def test_base_position_required(ctx: PpkContext, tmp_path: Path) -> None:
    """A base converted here has convbin's own rough position in its header; that is not a
    survey position, so it is never used as one."""
    start, _ = fixture_window()
    install_fixture_as_log(tmp_path, start)
    req = PpkRequest(rover=_window_rover(), base=BaseSource(kind="upload", path_ubx=FIXTURE))
    with pytest.raises(PpkError, match="base position"):
        await run_ppk(req, ctx, tmp_path / "out")
    assert not (tmp_path / "out" / "rover.ubx").exists()


async def test_no_rover_logs_in_window(ctx: PpkContext, tmp_path: Path) -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    req = PpkRequest(
        rover=RoverSource(kind="window", start=start, end=start + timedelta(minutes=5)),
        base=BaseSource(kind="upload", path_ubx=FIXTURE),
        base_xyz=XYZ,
    )
    with pytest.raises(PpkError, match="rover: no raw logs"):
        await run_ppk(req, ctx, tmp_path / "out")


async def test_rnx2rtkp_failure_and_bad_option_are_errors(ctx: PpkContext, tmp_path: Path) -> None:
    start, _ = fixture_window()
    install_fixture_as_log(tmp_path, start)
    fake = tmp_path / "fake-rnx2rtkp"
    fake.write_text("#!/bin/sh\necho 'boom: cannot open obs' >&2\nexit 3\n")
    fake.chmod(0o755)
    ctx.rnx2rtkp = str(fake)
    req = PpkRequest(rover=_window_rover(), base=BaseSource(kind="upload", path_ubx=FIXTURE))
    req = req.model_copy(update={"base_xyz": XYZ})
    with pytest.raises(PpkError, match="exit 3.*boom"):
        await run_ppk(req, ctx, tmp_path / "out")
    assert "boom" in (tmp_path / "out" / "rnx2rtkp.log").read_text()
    ctx.rnx2rtkp = "rnx2rtkp"
    bad = req.model_copy(update={"conf_overrides": {"pos1-posmode": "nonsense"}})
    with pytest.raises(PpkError, match="invalid option"):
        await run_ppk(bad, ctx, tmp_path / "out3")
    with pytest.raises(PpkError, match="override"):
        await run_ppk(
            req.model_copy(update={"conf_overrides": {"bad key": "1"}}), ctx, tmp_path / "out4"
        )


async def test_rinex_header_reader(tmp_path: Path) -> None:
    res = await run_convbin(
        FIXTURE,
        tmp_path / "a.rnx",
        tmp_path / "a_MN.rnx",
        ConvbinOptions(
            header=RinexHeader(
                marker_name="MTRK",
                observer="o",
                agency="a",
                receiver_version="HPG 1.13",
                approx_xyz=XYZ,
            )
        ),
    )
    info = read_header(res.obs_path)
    assert info.marker == "MTRK" and "u-blox" in info.receiver_type
    assert info.approx_xyz == pytest.approx(XYZ, abs=1e-3) and info.version.startswith("3.04")
    span = obs_span(res.obs_path)
    assert span is not None and 50 <= (span[1] - span[0]).total_seconds() <= 70
    assert sniff_format(res.obs_path) == "rinex-obs"
    assert sniff_format(FIXTURE) == "raw"


def test_sniff_format_refuses_what_it_cannot_convert(tmp_path: Path) -> None:
    crx = tmp_path / "x.crx"
    crx.write_text(f"{'1.0':<20}{'COMPACT RINEX FORMAT':<40}CRINEX VERS   / TYPE\n")
    assert sniff_format(crx) == "crinex"
    gz = tmp_path / "x.gz"
    gz.write_bytes(b"\x1f\x8b\x08\x00rest")
    assert sniff_format(gz) == "gzip"
    nav = tmp_path / "x.nav"
    nav.write_text(f"{'     3.04':<20}{'N: GNSS NAV DATA':<40}RINEX VERSION / TYPE\n")
    assert sniff_format(nav) == "rinex-nav"


async def test_events_are_written_when_marks_exist(ctx: PpkContext, tmp_path: Path) -> None:
    from pyubx2 import GET, UBXMessage

    from mtrtk.rawlog.index import list_logs

    start, end = fixture_window()
    install_fixture_as_log(tmp_path, start)
    # append synthetic TIM-TM2s to the rover log: one inside the window, one an hour later
    log = list_logs(tmp_path)[0].path
    with log.open("ab") as fh:
        for offset in (5, 3600):
            # GPST = UTC + 18 s leap
            tow = (start - datetime(1980, 1, 6, tzinfo=UTC)).total_seconds() + 18 + offset
            week, tow_in_week = divmod(tow, 604800)
            fh.write(
                UBXMessage(
                    "TIM",
                    "TIM-TM2",
                    GET,
                    ch=0,
                    run=1,
                    time=1,
                    newRisingEdge=1,
                    timeBase=1,
                    count=offset,
                    wnR=int(week),
                    towMsR=int(tow_in_week * 1000),
                    towSubMsR=0,
                    accEst=30,
                ).serialize()
            )
    req = PpkRequest(
        rover=RoverSource(kind="window", start=start, end=end + timedelta(seconds=1)),
        base=BaseSource(kind="upload", path_ubx=FIXTURE),
        base_xyz=XYZ,
    )
    result = await run_ppk(req, ctx, tmp_path / "out")
    assert result["events"]["total"] == 1 and result["events"]["ok"] == 1
    assert (tmp_path / "out" / "events.csv").exists()
    assert (tmp_path / "out" / "events.geojson").exists()
    no_events = req.model_copy(update={"events": False})
    result = await run_ppk(no_events, ctx, tmp_path / "out2")
    assert result["events"]["total"] == 0 and not (tmp_path / "out2" / "events.csv").exists()


async def test_make_ppk_job_writes_into_the_job_dir(ctx: PpkContext, tmp_path: Path) -> None:
    import asyncio

    from mtrtk.core.bus import Bus
    from mtrtk.jobs import JobRunner

    start, _ = fixture_window()
    install_fixture_as_log(tmp_path, start)
    runner = JobRunner(ctx.db, Bus(), tmp_path / "jobs")
    try:
        req = PpkRequest(
            rover=_window_rover(), base=BaseSource(kind="upload", path_ubx=FIXTURE), base_xyz=XYZ
        )
        job = await runner.submit("ppk", req.model_dump(mode="json"), make_ppk_job(req, ctx))
        done = None
        for _ in range(1200):  # bounded: a wedged job fails the test
            done = await runner.get(job.id)
            if done is not None and done.status in ("done", "failed"):
                break
            await asyncio.sleep(0.05)
        assert done is not None and done.status == "done", done and done.error
        assert done.result is not None and done.result["summary"]["epochs"] > 20
        assert runner.result_path(job.id, "track.pos").exists()
    finally:
        await runner.shutdown()


def test_request_validation() -> None:
    from pydantic import ValidationError

    naive = datetime(2026, 1, 1)
    with pytest.raises(ValidationError, match="timezone"):
        RoverSource(kind="window", start=naive, end=naive + timedelta(hours=1))
    aware = naive.replace(tzinfo=UTC)
    with pytest.raises(ValidationError, match="before"):
        RoverSource(kind="window", start=aware, end=aware)
    with pytest.raises(ValidationError, match="session_id"):
        RoverSource(kind="session")
    with pytest.raises(ValidationError, match="url"):
        BaseSource(kind="remote")
    with pytest.raises(ValidationError, match="url"):
        BaseSource(kind="remote", url="ftp://base")
    assert "password" not in BaseSource(kind="remote", url="http://b", password="x").model_dump()


def test_fetch_chunks_are_whole_hours_of_at_most_48() -> None:
    t0 = datetime(2026, 9, 18, 10, 30, tzinfo=UTC)
    chunks = fetch_chunks(t0, t0 + timedelta(hours=100))
    assert chunks[0][0] == datetime(2026, 9, 18, 10, tzinfo=UTC)
    assert chunks[-1][1] == t0 + timedelta(hours=100)
    assert all(b - a <= timedelta(hours=48) for a, b in chunks)
    assert all(a.minute == 0 for a, _ in chunks)
    assert all(chunks[i][1] == chunks[i + 1][0] for i in range(len(chunks) - 1))
    assert len(fetch_chunks(t0, t0 + timedelta(minutes=5))) == 1
