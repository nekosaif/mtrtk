import asyncio
import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_export import fixture_window, install_fixture_as_log
from webtest import client, make_ctx, make_log

from mtrtk.jobs import JobRunner
from mtrtk.rinex.convbin import convbin_available
from mtrtk.web.app import create_app

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_raw_60s.ubx"
PPP = Path(__file__).resolve().parents[1] / "fixtures" / "ppp"
H0 = datetime(2026, 9, 18, 10, tzinfo=UTC)
MB = 1024 * 1024

needs_convbin = pytest.mark.skipif(
    not convbin_available() or not FIXTURE.exists(), reason="convbin or fixture missing"
)


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
    tmp = tmp_path / "tmp"
    assert not tmp.exists() or list(tmp.iterdir()) == []


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
        # One byte over 20 MB reaches the route, which refuses it.
        over = await c.post("/api/base/ppp/import", files={"file": ("a.sum", b"x" * (20 * MB + 1))})
        assert over.status_code == 413 and "20 MB" in over.json()["detail"]
        # Far over is refused by the middleware before the body is read.
        huge = await c.post("/api/base/ppp/import", files={"file": ("a.sum", b"x" * (21 * MB))})
        assert huge.status_code == 413 and "20 MB" in huge.json()["detail"]
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
