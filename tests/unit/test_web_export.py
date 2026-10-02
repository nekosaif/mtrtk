import asyncio
import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from rinextest import needs_convbin
from test_export import fixture_window, install_fixture_as_log
from webtest import client, make_ctx, make_log

from mtrtk.jobs import JobRunner
from mtrtk.rinex.splice import NoDataError
from mtrtk.web.app import create_app

PPP = Path(__file__).resolve().parents[1] / "fixtures" / "ppp"
H0 = datetime(2026, 9, 18, 10, tzinfo=UTC)
MB = 1024 * 1024


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    c = await make_ctx(tmp_path)
    c.jobs = JobRunner(c.db, c.bus, tmp_path / "jobs")
    try:
        yield c
    finally:
        await c.jobs.shutdown()
        await c.db.close()


def window(start: datetime = H0, hours: float = 1) -> dict[str, str]:
    return {"start": start.isoformat(), "end": (start + timedelta(hours=hours)).isoformat()}


async def wait_for(c, job_id: str) -> dict:  # type: ignore[no-untyped-def]
    for _ in range(400):
        await asyncio.sleep(0.05)
        job = (await c.get(f"/api/jobs/{job_id}")).json()
        if job["status"] in ("done", "failed"):
            return job  # type: ignore[no-any-return]
    raise AssertionError(f"job {job_id} did not finish: {job}")


async def test_presets(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/export/presets")).json()
    assert [p["id"] for p in body] == ["csrs-ppp", "auspos", "opus", "generic"]
    assert body[0]["hatanaka"] is True and body[3]["adjustable"] is True
    assert set(body[0]) == {
        "id",
        "name",
        "description",
        "service_url",
        "version",
        "interval_s",
        "exclude_systems",
        "hatanaka",
        "gzip",
        "constraints",
        "adjustable",
    }
    assert isinstance(body[0]["constraints"], list) and body[0]["constraints"]
    assert body[2]["exclude_systems"] == ["R", "E", "J", "C", "S", "I"]


async def test_export_404_without_logs(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/export", json={**window(), "preset": "generic"})
    assert r.status_code == 404
    assert "no raw logs" in r.json()["detail"]
    assert await ctx.jobs.list() == []  # refused before anything was queued


async def test_a_lead_hour_alone_is_not_data(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The hour before the window feeds the navigation file; it does not make the window covered."""
    make_log(tmp_path, H0 - timedelta(hours=1))
    make_log(tmp_path, H0 + timedelta(hours=1), station="OTHR")  # another station's hour
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/export", json={**window(hours=2), "preset": "generic"})
    assert r.status_code == 404
    assert "MTRK" in r.json()["detail"]


async def test_export_409_without_a_job_runner(ctx) -> None:  # type: ignore[no-untyped-def]
    runner, ctx.jobs = ctx.jobs, None
    try:
        async with client(create_app(ctx)) as c:
            r = await c.post("/api/export", json={**window(), "preset": "generic"})
    finally:
        ctx.jobs = runner
    assert r.status_code == 409
    assert "job runner" in r.json()["detail"]


async def test_export_validation(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/export", json={**window(hours=0), "preset": "generic"})
        assert r.status_code == 422
        r = await c.post("/api/export", json={**window(), "preset": "csrs-ppp", "interval_s": 1})
        assert r.status_code == 422
        assert "fixed options" in str(r.json()["detail"])
        r = await c.post("/api/export", json={**window(), "preset": "nope"})
        assert r.status_code == 422


@needs_convbin
async def test_export_job_runs_and_files_download(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    start, end = fixture_window()
    install_fixture_as_log(tmp_path, start)
    async with client(create_app(ctx)) as c:
        r = await c.post(
            "/api/export",
            json={"start": start.isoformat(), "end": end.isoformat(), "preset": "generic"},
        )
        assert r.status_code == 200
        assert r.json()["kind"] == "export" and r.json()["params"]["preset"] == "generic"
        job = await wait_for(c, r.json()["id"])
        assert job["status"] == "done", job
        assert job["result"]["preset"] == "generic" and job["result"]["obs_epochs"] > 0
        files = (await c.get(f"/api/jobs/{job['id']}/files")).json()
        names = [f["name"] for f in files]
        assert any(n.endswith("_MO.rnx") for n in names) and "manifest.json" in names
        obs = next(n for n in names if n.endswith("_MO.rnx"))
        assert (await c.get(f"/api/jobs/{job['id']}/files/{obs}")).text.startswith("     3.04")


@needs_convbin
async def test_sync_rinex_zip(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    start, end = fixture_window()
    install_fixture_as_log(tmp_path, start)
    async with client(create_app(ctx)) as c:
        r = await c.get(
            "/api/export/rinex",
            params={
                "from": start.isoformat(),
                "to": end.isoformat(),
                "preset": "generic",
                "interval": 10,
            },
        )
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            names = zf.namelist()
            assert "manifest.json" in names
            obs = next(n for n in names if n.endswith("_10S_MO.rnx"))
            assert zf.read(obs).startswith(b"     3.04")
        stem = obs.split(".")[0]
        assert r.headers["content-disposition"] == f'attachment; filename="{stem}.zip"'
        too_long = await c.get(
            "/api/export/rinex",
            params={"from": start.isoformat(), "to": (start + timedelta(hours=7)).isoformat()},
        )
        assert too_long.status_code == 422
        assert "6 h" in too_long.json()["detail"]
    tmp = tmp_path / "tmp"
    assert not tmp.exists() or list(tmp.iterdir()) == []  # the working directory went away


async def test_sync_rinex_errors(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    q = {"from": H0.isoformat(), "to": (H0 + timedelta(hours=1)).isoformat()}
    async with client(create_app(ctx)) as c:
        r = await c.get("/api/export/rinex", params=q)
        assert r.status_code == 404 and "no raw logs" in r.json()["detail"]
        r = await c.get("/api/export/rinex", params={**q, "preset": "csrs-ppp", "interval": 1})
        assert r.status_code == 422
        assert "fixed options" in r.json()["detail"][0]["msg"]
        naive = {"from": "2026-09-18T10:00:00", "to": "2026-09-18T11:00:00"}
        r = await c.get("/api/export/rinex", params=naive)
        assert r.status_code == 422 and "timezone-aware" in r.json()["detail"][0]["msg"]
        r = await c.get("/api/export/rinex", params={**q, "from": "yesterday"})
        assert r.status_code == 422
    assert not (tmp_path / "tmp").exists()  # all refused before a working directory was made


async def test_an_export_error_is_a_409_with_its_message(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A setting that cannot name the files is the daemon's problem, in the operator's terms."""
    ctx.settings.country = "BANGLADESH"
    make_log(tmp_path, H0)
    async with client(create_app(ctx)) as c:
        r = await c.get(
            "/api/export/rinex",
            params={"from": H0.isoformat(), "to": (H0 + timedelta(hours=1)).isoformat()},
        )
    assert r.status_code == 409
    assert "COUNTRY" in r.json()["detail"]
    tmp = tmp_path / "tmp"
    assert tmp.is_dir() and list(tmp.iterdir()) == []  # made for the export, gone after it failed


async def test_ppp_import_preview_and_site_with_axis_sigmas(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        files = {"file": ("MTRK.sum", (PPP / "csrs_sample.sum").read_bytes(), "text/plain")}
        r = await c.post("/api/base/ppp/import", files=files)
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "csrs-ppp" and abs(body["x"] - (-26748.172)) < 1e-3
        assert body["suggested_name"] == "MTRK-csrs-ppp-2026.71"
        assert body["frame"] == "ITRF20" and body["epoch"] == "2026.7137"
        assert isinstance(body["notes"], list)
        site = await c.post(
            "/api/base/sites",
            json={
                "name": body["suggested_name"],
                "x": body["x"],
                "y": body["y"],
                "z": body["z"],
                "sigma_x": body["sigma_x"],
                "sigma_y": body["sigma_y"],
                "sigma_z": body["sigma_z"],
                "source": body["source"],
                "frame": body["frame"],
                "epoch": body["epoch"],
            },
        )
        assert site.status_code == 200
        saved = site.json()["site"]
        assert saved["sigma_y"] == pytest.approx(0.015 / 1.96, abs=1e-6)
        for axis in ("sigma_x", "sigma_y", "sigma_z"):
            assert saved[axis] == pytest.approx(body[axis])
        assert saved["frame"] == "ITRF20" and saved["epoch"] == "2026.7137"


async def test_ppp_import_refuses_what_it_cannot_read(ctx) -> None:  # type: ignore[no-untyped-def]
    content = b"nothing useful " * 40
    async with client(create_app(ctx)) as c:
        bad = await c.post("/api/base/ppp/import", files={"file": ("x.txt", content, "text/plain")})
        assert bad.status_code == 422
        detail = bad.json()["detail"]
        assert detail["hint"].startswith("Upload the CSRS-PPP")
        assert detail["message"]
        assert detail["head"] == content.decode()[:200]
        frame = await c.post(
            "/api/base/ppp/import",
            files={"file": ("MTRK.sum", (PPP / "csrs_sample.sum").read_bytes(), "text/plain")},
            data={"prefer_frame": "wgs84"},
        )
        assert frame.status_code == 422 and "nad83" in frame.json()["detail"]["hint"]
        missing = await c.post("/api/base/ppp/import", data={"prefer_frame": "itrf"})
        assert missing.status_code == 422


async def test_ppp_import_takes_more_than_the_api_body_limit_but_not_over_20_mb(ctx) -> None:  # type: ignore[no-untyped-def]
    from mtrtk.web.app import API_BODY_LIMIT

    async with client(create_app(ctx)) as c:
        # Above the 256 KiB every other /api body is held to, and still read and parsed.
        padded = (PPP / "csrs_sample.sum").read_bytes() + b"\n" * (API_BODY_LIMIT + 1)
        ok = await c.post("/api/base/ppp/import", files={"file": ("MTRK.sum", padded)})
        assert ok.status_code == 200, ok.text
        # Where 20 MB + 1 and 21 MB are refused: test_the_middleware_cuts_off_a_huge_upload...
        # The larger limit is that route's alone.
        other = await c.post("/api/base/sites", content=b"x" * (API_BODY_LIMIT + 1))
        assert other.status_code == 413


async def test_site_body_axis_sigmas(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.post(
            "/api/base/sites",
            json={"name": "a", "x": 1e6, "y": 6e6, "z": 1e6, "sigma_m": 0.5, "sigma_z": 0.02},
        )
        assert r.status_code == 200
        site = r.json()["site"]
        assert (site["sigma_x"], site["sigma_y"], site["sigma_z"]) == (0.5, 0.5, 0.02)
        r = await c.post(
            "/api/base/sites", json={"name": "b", "x": 1, "y": 2, "z": 3, "sigma_x": -1}
        )
        assert r.status_code == 422


async def test_the_zip_working_directory_goes_even_when_the_client_hangs_up(
    tmp_path: Path,
) -> None:
    from mtrtk.web.api.export import _ZipResponse

    work = tmp_path / "work"
    work.mkdir()
    archive = work / "x.zip"
    archive.write_bytes(b"PK\x05\x06" + b"\0" * 18)

    async def receive() -> dict:  # type: ignore[type-arg]
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:  # type: ignore[type-arg]
        if message["type"] == "http.response.body":
            raise OSError("client went away")

    scope = {"type": "http", "method": "GET", "headers": []}
    with pytest.raises(OSError):
        await _ZipResponse(archive, work)(scope, receive, send)
    assert not work.exists()


def test_a_working_directory_left_by_a_crash_is_cleared(tmp_path: Path) -> None:
    import os

    from mtrtk.web.api.export import STALE_WORK_S, _work_dir

    crashed = _work_dir(tmp_path)
    running = _work_dir(tmp_path)  # another export, still going: left alone
    old = (datetime.now(UTC) - timedelta(seconds=STALE_WORK_S + 60)).timestamp()
    os.utime(crashed, (old, old))
    fresh = _work_dir(tmp_path)
    assert not crashed.exists() and running.is_dir() and fresh.is_dir()


# --- fix round 1 ----------------------------------------------------------------------------


@pytest.fixture
def parsed(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Bytes of multipart file data Starlette's parser saw - zero means the body was never read."""
    from starlette.formparsers import MultiPartParser

    seen: list[int] = []
    real = MultiPartParser.on_part_data

    def spy(self, data: bytes, start: int, end: int) -> None:  # type: ignore[no-untyped-def]
        seen.append(end - start)
        real(self, data, start, end)

    monkeypatch.setattr(MultiPartParser, "on_part_data", spy)
    return seen


async def test_an_anonymous_upload_is_held_to_the_api_body_limit(tmp_path: Path, parsed) -> None:  # type: ignore[no-untyped-def]
    """The 20 MB allowance is for a caller who has logged in; nobody else gets past 256 KiB."""
    from mtrtk.web.app import API_BODY_LIMIT
    from mtrtk.web.auth import session_token

    ctx = await make_ctx(tmp_path, web_password="secret")
    try:
        big = (PPP / "csrs_sample.sum").read_bytes() + b"\n" * (API_BODY_LIMIT + 1)
        async with client(create_app(ctx)) as c:
            anon = await c.post("/api/base/ppp/import", files={"file": ("MTRK.sum", big)})
            assert anon.status_code == 413
            assert sum(parsed) == 0  # refused before the parser saw a byte
            wrong = await c.post(
                "/api/base/ppp/import",
                files={"file": ("MTRK.sum", big)},
                headers={"Authorization": "Bearer nope"},
            )
            assert wrong.status_code == 413 and sum(parsed) == 0
            small = await c.post("/api/base/ppp/import", files={"file": ("MTRK.sum", b"x")})
            assert small.status_code == 401
            ok = await c.post(
                "/api/base/ppp/import",
                files={"file": ("MTRK.sum", big)},
                headers={"Authorization": f"Bearer {session_token('secret')}"},
            )
            assert ok.status_code == 200, ok.text
            c.cookies.set("mtrtk_session", session_token("secret"))
            cookie = await c.post("/api/base/ppp/import", files={"file": ("MTRK.sum", big)})
            assert cookie.status_code == 200, cookie.text
    finally:
        await ctx.db.close()


async def test_the_middleware_cuts_off_a_huge_upload_before_the_parser(ctx, parsed) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        over = await c.post("/api/base/ppp/import", files={"file": ("a.sum", b"x" * (20 * MB + 1))})
        assert over.status_code == 413 and sum(parsed) > 20 * MB  # read, then the route refused
        parsed.clear()
        huge = await c.post("/api/base/ppp/import", files={"file": ("a.sum", b"x" * (21 * MB))})
        assert huge.status_code == 413 and "20 MB" in huge.json()["detail"]
        assert sum(parsed) == 0  # the middleware refused it from Content-Length alone


async def _hold_export(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Make `export_to_dir` wait until released, then fail with an ExportError."""
    from mtrtk.rinex.export import ExportError
    from mtrtk.web.api import export as export_api

    entered, release = asyncio.Event(), asyncio.Event()

    async def held(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        raise ExportError("held export done")

    monkeypatch.setattr(export_api, "export_to_dir", held)
    return entered, release


async def test_synchronous_exports_run_one_at_a_time(ctx, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    make_log(tmp_path, H0)
    entered, release = await _hold_export(monkeypatch)
    q = {"from": H0.isoformat(), "to": (H0 + timedelta(hours=1)).isoformat()}
    async with client(create_app(ctx)) as c:
        first = asyncio.create_task(c.get("/api/export/rinex", params=q))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            second = await asyncio.wait_for(c.get("/api/export/rinex", params=q), 5)
            assert second.status_code == 409
            assert "another export is running" in second.json()["detail"]
            job = await c.post("/api/export", json={**window(), "preset": "generic"})
            assert job.status_code == 409 and "another export is running" in job.json()["detail"]
            assert await ctx.jobs.list() == []
        finally:
            release.set()
        r = await first
        assert r.status_code == 409 and r.json()["detail"] == "held export done"
        # Released on the way out: the next one goes ahead.
        entered.clear()
        again = asyncio.create_task(c.get("/api/export/rinex", params=q))
        await asyncio.wait_for(entered.wait(), 5)
        assert (await again).status_code == 409


async def test_a_sync_export_waits_for_no_export_job(ctx, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    make_log(tmp_path, H0)
    release = asyncio.Event()

    async def job_fn(_jc):  # type: ignore[no-untyped-def]
        await release.wait()
        return {}

    job = await ctx.jobs.submit("export", {}, job_fn)
    q = {"from": H0.isoformat(), "to": (H0 + timedelta(hours=1)).isoformat()}
    async with client(create_app(ctx)) as c:
        r = await c.get("/api/export/rinex", params=q)
        assert r.status_code == 409 and "POST /api/export" in r.json()["detail"]
        release.set()
        assert (await wait_for(c, job.id))["status"] == "done"
        r = await c.get("/api/export/rinex", params={**q, "preset": "nope"})
        assert r.status_code == 422  # no longer busy: on to validation


async def test_a_job_runner_that_has_shut_down_is_a_409(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    make_log(tmp_path, H0)
    await ctx.jobs.shutdown()
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/export", json={**window(), "preset": "generic"})
    assert r.status_code == 409 and "shut down" in r.json()["detail"]


async def test_a_log_gone_before_the_splice_is_a_404(ctx, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from mtrtk.web.api import export as export_api

    make_log(tmp_path, H0)

    async def gone(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise NoDataError("no raw logs cover the window any more")

    monkeypatch.setattr(export_api, "export_to_dir", gone)
    async with client(create_app(ctx)) as c:
        r = await c.get(
            "/api/export/rinex",
            params={"from": H0.isoformat(), "to": (H0 + timedelta(hours=1)).isoformat()},
        )
    assert r.status_code == 404 and "any more" in r.json()["detail"]
    tmp = tmp_path / "tmp"
    assert tmp.is_dir() and list(tmp.iterdir()) == []


async def test_the_export_context_carries_l5_and_the_active_site(ctx) -> None:  # type: ignore[no-untyped-def]
    from mtrtk.core.state import Satellite, Signal
    from mtrtk.store.models import Site
    from mtrtk.store.repos import SitesRepo
    from mtrtk.web.api.export import _export_context

    assert (await _export_context(ctx)).frequencies == 2
    sigs = [Signal(sig_id=0, name="L1C/A"), Signal(sig_id=7, name="L5Q")]
    ctx.store.state.sats = [Satellite(gnss_id=0, gnss="GPS", sv_id=1, signals=sigs)]
    repo = SitesRepo(ctx.db)
    await repo.add(Site.from_ecef("here", 1.0e6, 6.0e6, 1.5e6, source="manual"))
    await repo.activate("here")
    export_ctx = await _export_context(ctx)
    assert export_ctx.frequencies == 3
    assert export_ctx.header.approx_xyz == (1.0e6, 6.0e6, 1.5e6)


def test_gzipped_members_are_stored_and_the_rest_deflated(tmp_path: Path) -> None:
    from mtrtk.rinex.export import ExportResult
    from mtrtk.web.api.export import _zip

    (tmp_path / "a.crx.gz").write_bytes(b"\x1f\x8b" + b"z" * 500)
    (tmp_path / "a.rnx").write_bytes(b"r" * 500)
    files = [{"name": "a.crx.gz", "role": "obs"}, {"name": "a.rnx", "role": "nav"}]
    result = ExportResult(files, 1, 1, 30.0, "3.04", "csrs-ppp", "s", "e", [])
    _zip(tmp_path, result, tmp_path / "out.zip")
    with zipfile.ZipFile(tmp_path / "out.zip") as zf:
        assert zf.getinfo("a.crx.gz").compress_type == zipfile.ZIP_STORED
        assert zf.getinfo("a.rnx").compress_type == zipfile.ZIP_DEFLATED


async def test_a_binary_upload_is_a_422_with_a_text_head(ctx) -> None:  # type: ignore[no-untyped-def]
    content = b"\x1f\x8b\x08" + bytes(range(256)) * 4
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/base/ppp/import", files={"file": ("x.gz", content)})
    assert r.status_code == 422
    head = r.json()["detail"]["head"]
    assert isinstance(head, str) and 0 < len(head) <= 200


async def test_a_multi_site_sinex_is_read_for_this_station(tmp_path: Path) -> None:
    from test_ppp_result import _MULTI_SNX

    for station, expect in (("MTRK", 200), ("ZZZZ", 422)):
        ctx = await make_ctx(tmp_path / station, station_id=station)
        try:
            async with client(create_app(ctx)) as c:
                r = await c.post(
                    "/api/base/ppp/import", files={"file": ("AUSPOS.SNX", _MULTI_SNX.encode())}
                )
        finally:
            await ctx.db.close()
        assert r.status_code == expect, r.text
        if expect == 200:
            assert r.json()["x"] == pytest.approx(-26748.172, abs=1e-3)
            assert r.json()["suggested_name"].startswith("MTRK-auspos-")
        else:
            assert "MTRK" in r.json()["detail"]["message"] + r.json()["detail"]["hint"]


async def test_an_auspos_sinex_for_another_station_is_previewed_with_a_warning(
    tmp_path: Path,
) -> None:
    """A network SINEX whose one free station is not STATION_ID is previewed (nothing is saved),
    with the mismatch in the notes the review dialog shows before the user saves it."""
    ctx = await make_ctx(tmp_path, station_id="MTRK")
    try:
        async with client(create_app(ctx)) as c:
            r = await c.post(
                "/api/base/ppp/import",
                files={"file": ("result.snx", (PPP / "auspos_v3_str1.snx").read_bytes())},
            )
    finally:
        await ctx.db.close()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["x"] == pytest.approx(-4467103.4134565, abs=1e-6)
    assert any(
        "used site STR1" in n and "does not match station id MTRK" in n for n in body["notes"]
    )
    assert body["suggested_name"].startswith("MTRK-auspos-")


async def test_prefer_frame_as_a_query_parameter_is_refused(ctx) -> None:  # type: ignore[no-untyped-def]
    files = {"file": ("MTRK.sum", (PPP / "csrs_sample.sum").read_bytes(), "text/plain")}
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/base/ppp/import?prefer_frame=nad83", files=files)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail[0]["loc"] == ["query", "prefer_frame"]
    assert "form field" in detail[0]["msg"]


def test_sync_validation_errors_name_the_query_parameter() -> None:
    from pydantic import ValidationError

    from mtrtk.rinex.export import ExportRequest
    from mtrtk.web.api.export import _issues

    with pytest.raises(ValidationError) as exc:
        ExportRequest(start=H0, end=H0 + timedelta(hours=1), interval_s="often")  # type: ignore[arg-type]
    assert _issues(exc.value)[0]["loc"] == ["query", "interval"]
    with pytest.raises(ValidationError) as exc:
        ExportRequest(start=H0, end=H0, preset="generic")
    assert _issues(exc.value)[0]["loc"] == ["query"]


# ---------------------------------------------------------------- final review: error paths


async def test_sync_export_refuses_a_cross_site_navigation(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """SameSite=Lax still sends the cookie on a top-level GET from another site."""
    make_log(tmp_path, H0)
    params = {"from": H0.isoformat(), "to": (H0 + timedelta(hours=1)).isoformat()}
    async with client(create_app(ctx)) as c:
        r = await c.get(
            "/api/export/rinex", params=params, headers={"Sec-Fetch-Site": "cross-site"}
        )
        assert r.status_code == 403 and "another site" in r.json()["detail"]
        r = await c.get("/api/export/rinex", params=params, headers={"Sec-Fetch-Site": "same-site"})
        assert r.status_code == 403
    assert not (tmp_path / "tmp").exists()  # refused before any work


async def test_sync_export_validation_names_the_query_parameter(ctx) -> None:  # type: ignore[no-untyped-def]
    params = {
        "from": H0.isoformat(),
        "to": (H0 + timedelta(hours=1)).isoformat(),
        "preset": "csrs-ppp",
        "interval": "5",
    }
    async with client(create_app(ctx)) as c:
        r = await c.get("/api/export/rinex", params=params)
    assert r.status_code == 422
    msg = r.json()["detail"][0]["msg"]
    assert not msg.startswith("Value error") and "'interval'" in msg and "interval_s" not in msg


async def test_sync_export_that_cannot_stage_is_a_409(  # type: ignore[no-untyped-def]
    ctx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno

    from mtrtk.web.api import export as export_api

    make_log(tmp_path, H0)

    def full(data_dir: Path) -> Path:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(export_api, "_work_dir", full)
    params = {"from": H0.isoformat(), "to": (H0 + timedelta(hours=1)).isoformat()}
    async with client(create_app(ctx)) as c:
        r = await c.get("/api/export/rinex", params=params)
        assert r.status_code == 409 and "No space left on device" in r.json()["detail"]
        # The slot was given back.
        r = await c.get("/api/export/rinex", params=params)
        assert r.status_code == 409 and "another export" not in r.json()["detail"]


async def test_an_export_job_that_fails_mid_way_is_failed_and_leaves_no_rinex(  # type: ignore[no-untyped-def]
    ctx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """convbin dies part-way through a job: the row says why, the job directory holds nothing."""
    from typing import Any

    from mtrtk.rinex.convbin import ConvbinError

    start, end = fixture_window()
    install_fixture_as_log(tmp_path, start)

    async def dies(src: Path, obs: Path, nav: Path, opts: Any, **_: Any) -> Any:
        await asyncio.to_thread(
            obs.write_text, "     3.04           OBSERVATION DATA    M\n"
        )  # half a file, then death
        raise ConvbinError("convbin was killed by SIGKILL: out of memory")

    monkeypatch.setattr("mtrtk.rinex.export.run_convbin", dies)
    async with client(create_app(ctx)) as c:
        r = await c.post(
            "/api/export",
            json={"start": start.isoformat(), "end": end.isoformat(), "preset": "generic"},
        )
        assert r.status_code == 200
        job = await wait_for(c, r.json()["id"])
        assert job["status"] == "failed"
        assert job["error"].startswith("ConvbinError: convbin was killed by SIGKILL")
        files = (await c.get(f"/api/jobs/{job['id']}/files")).json()
    assert files == []
    assert list((tmp_path / "jobs" / job["id"]).iterdir()) == []  # no staging left either


async def test_ppp_imports_are_read_one_at_a_time(  # type: ignore[no-untyped-def]
    ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each parse holds a 20 MB upload and a thread: a second one meanwhile is a 409."""
    import threading

    from mtrtk.web.api import base as base_api

    entered, release = threading.Event(), threading.Event()
    real = base_api.parse_ppp_result

    def slow(*a, **kw):  # type: ignore[no-untyped-def]
        entered.set()
        release.wait(5)
        return real(*a, **kw)

    monkeypatch.setattr(base_api, "parse_ppp_result", slow)
    content = (PPP / "csrs_sample.sum").read_bytes()
    files = {"file": ("MTRK.sum", content, "text/plain")}
    async with client(create_app(ctx)) as c:
        first = asyncio.create_task(c.post("/api/base/ppp/import", files=files))
        assert await asyncio.to_thread(entered.wait, 5)
        second = await c.post("/api/base/ppp/import", files=files)
        release.set()
        assert second.status_code == 409 and "being read" in second.json()["detail"]
        assert (await first).status_code == 200
        assert (await c.post("/api/base/ppp/import", files=files)).status_code == 200
