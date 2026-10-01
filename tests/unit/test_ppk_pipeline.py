"""PPK pipeline end to end on the real 60 s fixture: zero baseline (rover = base) with RTKLIB."""

import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_export import fixture_window, install_fixture_as_log

from mtrtk.ppk import pipeline
from mtrtk.ppk.pipeline import (
    BaseSource,
    PpkContext,
    PpkError,
    PpkRequest,
    RoverSource,
    make_ppk_job,
    run_ppk,
)
from mtrtk.ppk.rtkconf import parse_conf, rnx2rtkp_available
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


class _Recording(httpx.ASGITransport):
    """An ASGI transport that keeps the query of every `GET /api/logs/window`."""

    def __init__(self, **kw):  # type: ignore[no-untyped-def]
        super().__init__(**kw)
        self.window_params: list[dict[str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/logs/window":
            self.window_params.append(dict(request.url.params))
        return await super().handle_async_request(request)


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
    # The rover window reached convbin on GPST: the solution spans the whole UTC window, shifted
    # +18 s. Read as GPST unshifted, convbin's -te would cut the last 18 epochs.
    _, end = fixture_window()
    first = datetime.fromisoformat(summary["summary"]["first_time"])
    last = datetime.fromisoformat(summary["summary"]["last_time"])
    assert abs((first - (start + timedelta(seconds=18))).total_seconds()) <= 2
    assert abs((last - (end - timedelta(seconds=1) + timedelta(seconds=18))).total_seconds()) <= 2
    assert summary["summary"]["epochs"] >= 55
    inputs = summary["inputs"]
    assert inputs["glonass_ar"] == "on" and "u-blox" in inputs["base_receiver"]
    # no host paths in a file handed to a client: an uploaded input is named, not located
    assert inputs["base"]["path_ubx"] == FIXTURE.name
    assert str(FIXTURE.parent) not in (out / "summary.json").read_text()
    # stock 2.4.3 loses the backward pass on a minute of data: the forward rerun is reported
    conf = (out / "ppk.conf").read_text()
    assert inputs["soltype"] in {"combined", "forward"}
    assert inputs["soltype"] == parse_conf(conf)["pos1-soltype"]  # what actually ran
    if inputs["soltype"] == "forward":
        assert any("forward-only" in w for w in summary["warnings"])
        assert "# combined solution was empty" in conf
        runs = [ln for ln in (out / "rnx2rtkp.log").read_text().splitlines() if "-k ppk.conf" in ln]
        assert len(runs) == 2


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
    transport = _Recording(app=app)
    ctx.http = lambda: httpx.AsyncClient(transport=transport, base_url="http://base")
    try:
        req = PpkRequest(rover=_window_rover(), base=BaseSource(kind="remote", url="http://base"))
        result = await run_ppk(req, ctx, tmp_path / "out")
        # the base is fetched from the hour before the window: its ephemerides
        froms = [p["from"] for p in transport.window_params]
        lead = (start - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        assert froms and datetime.fromisoformat(froms[0]) == lead
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


async def test_uploaded_rover_against_local_base_logs(
    ctx: PpkContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An uploaded rover file has no window of its own: it is read from its observations."""
    start, _ = fixture_window()
    install_fixture_as_log(tmp_path, start)
    renamed = tmp_path / "ellipse_gps1_raw.bin"  # the content decides, not the suffix
    shutil.copy(FIXTURE, renamed)
    req = PpkRequest(
        rover=RoverSource(kind="upload", path=renamed), base=BaseSource(kind="local"), base_xyz=XYZ
    )
    calls: list[dict[str, object]] = []
    real_splice = pipeline.splice_window

    def recording_splice(root, start, end, dest, lead_hours=1, station=None):  # type: ignore[no-untyped-def]
        calls.append({"start": start, "lead_hours": lead_hours})
        return real_splice(root, start, end, dest, lead_hours, station)

    monkeypatch.setattr(pipeline, "splice_window", recording_splice)
    result = await run_ppk(req, ctx, tmp_path / "out")
    # the local base splice takes the hour before the window too: its ephemerides
    assert [c["lead_hours"] for c in calls] == [1]
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
    assert any("APPROX POSITION XYZ" in w for w in result["warnings"])
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


async def _rinex(tmp_path: Path, stem: str, receiver: str | None = None) -> tuple[Path, Path]:
    """The fixture as a RINEX obs+nav pair; `receiver` rewrites its REC # / TYPE / VERS."""
    res = await run_convbin(
        FIXTURE, tmp_path / f"{stem}.obs", tmp_path / f"{stem}.nav", ConvbinOptions(header=HEADER)
    )
    if receiver is not None:
        lines = res.obs_path.read_text().splitlines(keepends=True)
        label = "REC # / TYPE / VERS"
        lines = [
            f"{'0':<20}{receiver:<20}{'1.0':<20}{label}\n" if label in ln else ln for ln in lines
        ]
        res.obs_path.write_text("".join(lines))
    return res.obs_path, res.nav_path


async def test_inputs_kept_in_out_are_never_deleted(ctx: PpkContext, tmp_path: Path) -> None:
    """An operator's raw logs that sit in --out as rover.ubx/base.ubx are inputs, not scratch:
    they survive a run that succeeds and one that fails."""
    out = tmp_path / "field"
    out.mkdir()
    rover, base = out / "rover.ubx", out / "base.ubx"
    shutil.copy(FIXTURE, rover)
    shutil.copy(FIXTURE, base)
    req = PpkRequest(
        rover=RoverSource(kind="upload", path=rover),
        base=BaseSource(kind="upload", path_ubx=base),
        base_xyz=XYZ,
    )
    result = await run_ppk(req, ctx, out)
    assert result["summary"]["epochs"] > 20
    assert rover.read_bytes() == FIXTURE.read_bytes() and base.read_bytes() == FIXTURE.read_bytes()
    with pytest.raises(PpkError, match="base position"):
        await run_ppk(req.model_copy(update={"base_xyz": None}), ctx, out)
    assert rover.exists() and base.exists()
    assert not [p for p in out.iterdir() if p.name.startswith(".")]  # no scratch left behind


async def test_rinex_rover_in_place_keeps_its_navigation_file(
    ctx: PpkContext, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    obs, nav = await _rinex(tmp_path, "r")
    shutil.copy(obs, out / "rover.rnx")
    shutil.copy(nav, out / "rover_MN.rnx")
    nav_bytes = (out / "rover_MN.rnx").read_bytes()
    assert nav_bytes
    req = PpkRequest(
        rover=RoverSource(kind="upload", path=out / "rover.rnx"),
        base=BaseSource(kind="upload", path_ubx=FIXTURE),
        base_xyz=XYZ,
    )
    result = await run_ppk(req, ctx, out)
    assert result["summary"]["epochs"] > 20
    assert (out / "rover_MN.rnx").read_bytes() == nav_bytes
    assert result["inputs"]["rover"]["format"] == "rinex"
    # events were asked for (the default), but a RINEX rover has no camera marks to read
    assert any("camera" in w and "RINEX" in w for w in result["warnings"])


async def test_an_input_where_an_output_goes_is_refused(ctx: PpkContext, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    raw = out / "track.csv"  # raw data under an output's name: writing the track would eat it
    shutil.copy(FIXTURE, raw)
    req = PpkRequest(
        rover=RoverSource(kind="upload", path=raw),
        base=BaseSource(kind="upload", path_ubx=FIXTURE),
        base_xyz=XYZ,
    )
    with pytest.raises(PpkError, match=r"track\.csv.*input"):
        await run_ppk(req, ctx, out)
    assert raw.read_bytes() == FIXTURE.read_bytes()


async def test_a_reused_out_holds_only_this_runs_outputs(ctx: PpkContext, tmp_path: Path) -> None:
    start, _ = fixture_window()
    install_fixture_as_log(tmp_path, start)
    out = tmp_path / "out"
    out.mkdir()
    for stale in ("events.csv", "events.geojson", "track.pos.stat", "summary.json"):
        (out / stale).write_text("from an earlier run\n")
    (out / "notes.txt").write_text("the operator's own\n")
    req = PpkRequest(
        rover=_window_rover(),
        base=BaseSource(kind="upload", path_ubx=FIXTURE),
        base_xyz=XYZ,
        events=False,
    )
    result = await run_ppk(req, ctx, out)
    names = {f["name"] for f in result["files"]}
    assert not (out / "events.csv").exists() and not (out / "events.geojson").exists()
    assert "events.csv" not in names and "notes.txt" not in names
    assert (out / "notes.txt").exists()  # not ours: neither removed nor reported
    assert "from an earlier run" not in (out / "track.pos.stat").read_text()


async def test_non_ublox_base_gets_glonass_autocal(ctx: PpkContext, tmp_path: Path) -> None:
    start, _ = fixture_window()
    install_fixture_as_log(tmp_path, start)
    obs, nav = await _rinex(tmp_path, "b", receiver="TRIMBLE NETR9")
    req = PpkRequest(
        rover=_window_rover(),
        base=BaseSource(kind="upload", path_obs=obs, path_nav=nav),
        base_xyz=XYZ,
    )
    result = await run_ppk(req, ctx, tmp_path / "out")
    _assert_autocal(result, tmp_path / "out", "TRIMBLE NETR9")
    assert result["inputs"]["base_receiver"] == "TRIMBLE NETR9"


async def test_non_ublox_rover_gets_glonass_autocal(ctx: PpkContext, tmp_path: Path) -> None:
    obs, _ = await _rinex(tmp_path, "r", receiver="SEPT POLARX5")
    req = PpkRequest(
        rover=RoverSource(kind="upload", path=obs),
        base=BaseSource(kind="upload", path_ubx=FIXTURE),
        base_xyz=XYZ,
        events=False,
    )
    result = await run_ppk(req, ctx, tmp_path / "out")
    _assert_autocal(result, tmp_path / "out", "SEPT POLARX5")


def _assert_autocal(result: dict, out: Path, receiver: str) -> None:  # type: ignore[type-arg]
    assert result["inputs"]["glonass_ar"] == "autocal"
    assert any(receiver in w and "autocal" in w for w in result["warnings"])
    mode = parse_conf((out / "ppk.conf").read_text())["pos2-gloarmode"]
    # stock 2.4.3 has no autocal and says so; demo5 keeps it
    stock = any("pos2-gloarmode=autocal needs RTKLIB demo5" in w for w in result["warnings"])
    assert mode == ("off" if stock else "autocal")


async def test_window_cap_and_open_hour_and_same_station(ctx: PpkContext, tmp_path: Path) -> None:
    start, end = fixture_window()
    long = RoverSource(kind="window", start=start, end=start + timedelta(days=8))
    with pytest.raises(PpkError, match="7 days"):
        await run_ppk(
            PpkRequest(rover=long, base=BaseSource(kind="local"), base_xyz=XYZ), ctx, tmp_path / "o"
        )
    install_fixture_as_log(tmp_path, start, complete=False)
    req = PpkRequest(rover=_window_rover(), base=BaseSource(kind="local"), base_xyz=XYZ)
    result = await run_ppk(req, ctx, tmp_path / "out")
    assert any("still being written" in w for w in result["warnings"])
    assert any("same station" in w for w in result["warnings"])


async def test_uploaded_rover_span_is_capped_too(
    ctx: PpkContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start, _ = fixture_window()
    monkeypatch.setattr(pipeline, "obs_span", lambda path: (start, start + timedelta(days=9)))
    req = PpkRequest(
        rover=RoverSource(kind="upload", path=FIXTURE), base=BaseSource(kind="local"), base_xyz=XYZ
    )
    with pytest.raises(PpkError, match="7 days"):
        await run_ppk(req, ctx, tmp_path / "out")


async def test_unusable_uploads_are_refused_clearly(ctx: PpkContext, tmp_path: Path) -> None:
    start, _ = fixture_window()
    install_fixture_as_log(tmp_path, start)
    gz = tmp_path / "base.ubx.gz"
    gz.write_bytes(b"\x1f\x8b\x08\x00rest")
    with pytest.raises(PpkError, match="gzip"):
        await run_ppk(
            PpkRequest(
                rover=_window_rover(), base=BaseSource(kind="upload", path_ubx=gz), base_xyz=XYZ
            ),
            ctx,
            tmp_path / "o1",
        )
    _, nav = await _rinex(tmp_path, "b")
    with pytest.raises(PpkError, match="navigation file goes with a RINEX base"):
        await run_ppk(
            PpkRequest(
                rover=_window_rover(),
                base=BaseSource(kind="upload", path_ubx=FIXTURE, path_nav=nav),
                base_xyz=XYZ,
            ),
            ctx,
            tmp_path / "o2",
        )
    # RINEX on both sides and no navigation file: say so, not "do the windows overlap?"
    robs, _ = await _rinex(tmp_path, "r")
    bobs, _ = await _rinex(tmp_path, "b2")
    with pytest.raises(PpkError, match="navigation"):
        await run_ppk(
            PpkRequest(
                rover=RoverSource(kind="upload", path=robs),
                base=BaseSource(kind="upload", path_obs=bobs),
                base_xyz=XYZ,
            ),
            ctx,
            tmp_path / "o3",
        )
    gone = tmp_path / "gone.ubx"
    with pytest.raises(PpkError, match=r"gone\.ubx") as info:
        await run_ppk(
            PpkRequest(
                rover=RoverSource(kind="upload", path=gone),
                base=BaseSource(kind="upload", path_ubx=FIXTURE),
                base_xyz=XYZ,
            ),
            ctx,
            tmp_path / "o4",
        )
    assert "cannot write" not in str(info.value)


async def test_remote_base_with_a_missing_chunk(ctx: PpkContext, tmp_path: Path) -> None:
    """Over 48 h the fetch is two requests; the second finds no hours (404): the run goes on
    with what the first brought and says the base had nothing for part of the window."""
    from webtest import make_ctx

    from mtrtk.web.app import create_app

    start, _ = fixture_window()
    install_fixture_as_log(tmp_path, start)
    base_ctx = await make_ctx(tmp_path / "basehost")
    install_fixture_as_log(tmp_path / "basehost", start)
    transport = _Recording(app=create_app(base_ctx))
    ctx.http = lambda: httpx.AsyncClient(transport=transport, base_url="http://base")
    try:
        rover = RoverSource(kind="window", start=start, end=start + timedelta(hours=49))
        req = PpkRequest(
            rover=rover, base=BaseSource(kind="remote", url="http://base"), base_xyz=XYZ
        )
        result = await run_ppk(req, ctx, tmp_path / "out")
        assert len(transport.window_params) == 2
        assert result["summary"]["epochs"] > 20
        assert any("no raw logs" in w for w in result["warnings"])
    finally:
        await base_ctx.db.close()


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (lambda req: httpx.Response(404, json={"detail": "no raw logs"}), "has no raw logs"),
        (lambda req: httpx.Response(500, text="disk on fire"), "refused.*500.*disk on fire"),
        (
            lambda req: httpx.Response(
                200,
                content=FIXTURE.read_bytes(),
                headers={"Content-Disposition": 'attachment; filename="MIXED_2026_2026.ubx"'},
            ),
            "more than one station",
        ),
    ],
)
async def test_remote_base_refusals(
    ctx: PpkContext,
    tmp_path: Path,
    handler,
    message: str,  # type: ignore[no-untyped-def]
) -> None:
    start, _ = fixture_window()
    install_fixture_as_log(tmp_path, start)
    transport = httpx.MockTransport(handler)
    ctx.http = lambda: httpx.AsyncClient(transport=transport)
    req = PpkRequest(
        rover=_window_rover(), base=BaseSource(kind="remote", url="http://base"), base_xyz=XYZ
    )
    with pytest.raises(PpkError, match=message):
        await run_ppk(req, ctx, tmp_path / "out")


async def test_remote_base_unreachable(ctx: PpkContext, tmp_path: Path) -> None:
    start, _ = fixture_window()
    install_fixture_as_log(tmp_path, start)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    ctx.http = lambda: httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    req = PpkRequest(
        rover=_window_rover(), base=BaseSource(kind="remote", url="http://base"), base_xyz=XYZ
    )
    with pytest.raises(PpkError, match="cannot fetch.*connection refused"):
        await run_ppk(req, ctx, tmp_path / "out")
