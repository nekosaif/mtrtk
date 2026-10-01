"""PPK API: defaults, streamed uploads, submission; results through /api/jobs."""

import asyncio
import gzip
import os
from pathlib import Path
from typing import Any

import pytest
from test_export import fixture_window, install_fixture_as_log
from webtest import client, make_ctx

from mtrtk.jobs import JobRunner
from mtrtk.ppk.rtkconf import rnx2rtkp_available
from mtrtk.rinex.convbin import convbin_available
from mtrtk.store.models import Site
from mtrtk.store.repos import SitesRepo
from mtrtk.web.api import ppk as ppk_api
from mtrtk.web.app import create_app

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_raw_60s.ubx"
XYZ = [-26748.172, 5837156.618, 2561801.261]
UBX_HEAD = b"\xb5\x62\x01\x07\x00\x00\x08\x1a"
RINEX_OBS = b"     3.04           OBSERVATION DATA    M: Mixed            RINEX VERSION / TYPE\n"
REQUIRE = os.environ.get("MTRTK_REQUIRE_CONVBIN") == "1"
needs_rtklib = pytest.mark.skipif(
    not REQUIRE and not (convbin_available() and rnx2rtkp_available() and FIXTURE.exists()),
    reason="RTKLIB or fixture missing",
)


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    c = await make_ctx(tmp_path, role="rover", ntrip_url="ntrip://rover:pw@100.100.50.10:2101/MTRK")
    c.jobs = JobRunner(c.db, c.bus, tmp_path / "jobs")
    try:
        yield c
    finally:
        await c.jobs.shutdown()
        await c.db.close()


async def _upload(c: Any, name: str, data: bytes, kind: str = "base") -> Any:
    return await c.post(
        "/api/ppk/upload",
        data={"kind": kind},
        files={"file": (name, data, "application/octet-stream")},
    )


async def _wait(c: Any, job_id: str) -> dict[str, Any]:
    job: dict[str, Any] = {}
    for _ in range(600):
        await asyncio.sleep(0.1)
        job = (await c.get(f"/api/jobs/{job_id}")).json()
        if job["status"] in ("done", "failed"):
            break
    return job


async def test_defaults(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/ppk/defaults")).json()
    assert set(body) >= {"rnx2rtkp", "convbin", "demo5", "conf", "ntrip_base_url"}
    assert body["conf"]["pos1-posmode"] == "kinematic"
    assert body["ntrip_base_url"] == "http://100.100.50.10:8080"
    assert body["max_upload_bytes"] == ppk_api.MAX_UPLOAD


async def test_defaults_without_a_caster_url(tmp_path: Path) -> None:
    c = await make_ctx(tmp_path)
    try:
        async with client(create_app(c)) as http:
            body = (await http.get("/api/ppk/defaults")).json()
        assert body["ntrip_base_url"] is None
    finally:
        await c.db.close()


async def test_upload_detects_kind_and_rejects_junk(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await _upload(c, "base.ubx", UBX_HEAD)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["detected"] == "ubx" and body["bytes"] == len(UBX_HEAD)
        assert body["kind"] == "base" and body["name"] == "base.ubx"
        assert (tmp_path / "uploads" / body["upload_id"] / "base.ubx").read_bytes() == UBX_HEAD
        r2 = await _upload(c, "base.rnx", RINEX_OBS)
        assert r2.json()["detected"] == "rinex"
        junk = await _upload(c, "x.bin", b"hello")
        assert junk.status_code == 422 and "neither UBX nor RINEX" in junk.json()["detail"]
        gz = await _upload(c, "base.rnx.gz", gzip.compress(RINEX_OBS))
        assert gz.status_code == 422 and "gzip" in gz.json()["detail"]
    # A refused file leaves nothing behind.
    kept = sorted(p.name for p in (tmp_path / "uploads").iterdir())
    assert kept == sorted([body["upload_id"], r2.json()["upload_id"]])


async def test_upload_keeps_only_the_file_name(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await _upload(c, "../../etc/passwd.ubx", UBX_HEAD, kind="rover")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "passwd.ubx"
    assert (tmp_path / "uploads" / r.json()["upload_id"] / "passwd.ubx").exists()
    assert not (tmp_path / "etc").exists()


async def test_upload_larger_than_the_api_body_limit_streams_through(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    data = UBX_HEAD + bytes(700 * 1024)  # past the 256 KiB every other /api body is held to
    async with client(create_app(ctx)) as c:
        r = await _upload(c, "big.ubx", data)
    assert r.status_code == 200, r.text
    assert (tmp_path / "uploads" / r.json()["upload_id"] / "big.ubx").stat().st_size == len(data)


async def test_upload_over_the_cap_is_refused_and_removed(ctx, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(ppk_api, "MAX_UPLOAD", 1024)
    async with client(create_app(ctx)) as c:
        r = await _upload(c, "big.ubx", UBX_HEAD + bytes(4096))
    assert r.status_code == 413, r.text
    assert not any((tmp_path / "uploads").iterdir())


async def test_upload_form_errors(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        no_file = await c.post(
            "/api/ppk/upload", data={"kind": "base"}, files={"other": ("a", b"x")}
        )
        assert no_file.status_code == 422 and "file" in no_file.json()["detail"]
        bad_kind = await c.post(
            "/api/ppk/upload", data={"kind": "nav"}, files={"file": ("a.ubx", UBX_HEAD)}
        )
        assert bad_kind.status_code == 422 and "kind" in bad_kind.json()["detail"]
        not_form = await c.post("/api/ppk/upload", content=UBX_HEAD)
        assert not_form.status_code == 422


@needs_rtklib
async def test_submit_zero_baseline_job(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    start, end = fixture_window()
    install_fixture_as_log(tmp_path, start)
    async with client(create_app(ctx)) as c:
        up = await _upload(c, "base.ubx", FIXTURE.read_bytes())
        body = {
            "rover": {"kind": "window", "start": start.isoformat(), "end": end.isoformat()},
            "base": {"kind": "upload", "upload_id": up.json()["upload_id"]},
            "base_xyz": XYZ,
            "events": False,
        }
        r = await c.post("/api/ppk", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "ppk"
        job = await _wait(c, r.json()["id"])
        assert job["status"] == "done", job
        summary = job["result"]["summary"]
        assert summary["epochs"] > 20
        # zero baseline: nothing worse than float
        assert summary["fixed_pct"] + summary["float_pct"] >= 99.0
        names = [f["name"] for f in (await c.get(f"/api/jobs/{job['id']}/files")).json()]
        assert {"track.pos", "track.geojson", "track.csv", "summary.json"} <= set(names)
        geo = await c.get(f"/api/jobs/{job['id']}/files/track.geojson")
        assert geo.status_code == 200 and geo.json()["type"] == "FeatureCollection"


@needs_rtklib
async def test_submit_local_base_takes_this_hosts_active_site(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    start, end = fixture_window()
    install_fixture_as_log(tmp_path, start)
    repo = SitesRepo(ctx.db)
    await repo.add(Site.from_ecef("roof", *XYZ, source="manual"))
    await repo.activate("roof")
    async with client(create_app(ctx)) as c:
        body = {
            "rover": {"kind": "window", "start": start.isoformat(), "end": end.isoformat()},
            "base": {"kind": "local"},
            "events": False,
        }
        r = await c.post("/api/ppk", json=body)
        assert r.status_code == 200, r.text
        job = await _wait(c, r.json()["id"])
    assert job["status"] == "done", job
    assert job["result"]["inputs"]["base_xyz_source"] == "site:roof"


async def test_submit_validation(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/ppk", json={"rover": {"kind": "window"}, "base": {"kind": "local"}})
        assert r.status_code == 422
        assert any("start and end" in i["msg"] for i in r.json()["detail"]), r.json()
        missing = await c.post(
            "/api/ppk",
            json={"rover": {"kind": "upload", "upload_id": "nope"}, "base": {"kind": "local"}},
        )
        assert missing.status_code == 404
        no_id = await c.post(
            "/api/ppk", json={"rover": {"kind": "upload"}, "base": {"kind": "local"}}
        )
        assert no_id.status_code == 422 and "upload_id" in str(no_id.json()["detail"])
        both = await c.post(
            "/api/ppk",
            json={
                "rover": {
                    "kind": "window",
                    "start": "2026-09-18T10:00:00Z",
                    "end": "2026-09-18T11:00:00Z",
                },
                "base": {"kind": "local"},
                "base_site": "roof",
                "base_xyz": XYZ,
            },
        )
        assert both.status_code == 422 and "not both" in str(both.json()["detail"])
        pinned = await c.post(
            "/api/ppk",
            json={
                "rover": {
                    "kind": "window",
                    "start": "2026-09-18T10:00:00Z",
                    "end": "2026-09-18T11:00:00Z",
                },
                "base": {"kind": "local"},
                "conf_overrides": {"out-solformat": "xyz"},
            },
        )
        assert pinned.status_code == 422 and "out-solformat" in str(pinned.json()["detail"])


async def test_remote_base_password_is_never_stored(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    secret = "hunter2-secret"
    start, end = fixture_window()
    install_fixture_as_log(tmp_path, start)
    async with client(create_app(ctx)) as c:
        bad = await c.post(
            "/api/ppk",
            json={
                "rover": {"kind": "window"},
                "base": {"kind": "remote", "url": "http://100.100.50.10:8080", "password": secret},
            },
        )
        assert bad.status_code == 422 and secret not in bad.text
        r = await c.post(
            "/api/ppk",
            json={
                "rover": {"kind": "window", "start": start.isoformat(), "end": end.isoformat()},
                "base": {"kind": "remote", "url": "http://127.0.0.1:9", "password": secret},
                "base_xyz": XYZ,
            },
        )
        assert r.status_code == 200, r.text
        assert secret not in r.text
        job = await _wait(c, r.json()["id"])
        assert secret not in str(job)
        assert job["status"] == "failed" and job["params"]["base"]["url"] == "http://127.0.0.1:9"
        assert "password" not in job["params"]["base"]


async def test_window_without_raw_logs_is_refused_before_queueing(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.post(
            "/api/ppk",
            json={
                "rover": {
                    "kind": "window",
                    "start": "2026-09-18T10:00:00Z",
                    "end": "2026-09-18T11:00:00Z",
                },
                "base": {"kind": "local"},
                "base_xyz": XYZ,
            },
        )
        assert r.status_code == 404 and "no raw logs" in r.json()["detail"]
        assert (await c.get("/api/jobs")).json() == []


async def test_lat_lon_given_as_ecef_is_refused(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.post(
            "/api/ppk",
            json={
                "rover": {
                    "kind": "window",
                    "start": "2026-09-18T10:00:00Z",
                    "end": "2026-09-18T11:00:00Z",
                },
                "base": {"kind": "local"},
                "base_xyz": [23.78, 90.41, 12.0],
            },
        )
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "base_xyz"]


async def test_uploaded_sources_must_be_the_right_files(ctx) -> None:  # type: ignore[no-untyped-def]
    nav = b"     3.04           N: GNSS NAV DATA    M: Mixed            RINEX VERSION / TYPE\n"
    async with client(create_app(ctx)) as c:
        nav_id = (await _upload(c, "base.nav", nav)).json()["upload_id"]
        ubx_id = (await _upload(c, "base.ubx", UBX_HEAD)).json()["upload_id"]
        as_base = await c.post(
            "/api/ppk",
            json={
                "rover": {"kind": "upload", "upload_id": ubx_id},
                "base": {"kind": "upload", "upload_id": nav_id},
            },
        )
        assert as_base.status_code == 422 and "nav_upload_id" in str(as_base.json()["detail"])
        wrong_nav = await c.post(
            "/api/ppk",
            json={
                "rover": {"kind": "upload", "upload_id": ubx_id},
                "base": {"kind": "upload", "upload_id": ubx_id, "nav_upload_id": ubx_id},
            },
        )
        assert wrong_nav.status_code == 422 and "not a RINEX navigation" in str(
            wrong_nav.json()["detail"]
        )
        stray = await c.post(
            "/api/ppk",
            json={
                "rover": {"kind": "upload", "upload_id": ubx_id},
                "base": {"kind": "local", "nav_upload_id": nav_id},
            },
        )
        assert stray.status_code == 422
        traversal = await c.post(
            "/api/ppk",
            json={"rover": {"kind": "upload", "upload_id": "../../etc"}, "base": {"kind": "local"}},
        )
        assert traversal.status_code == 404
