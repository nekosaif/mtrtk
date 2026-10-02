"""PPK API: defaults, streamed uploads, submission; results through /api/jobs."""

import asyncio
import csv
import gzip
import io
import os
import socket
import time
from collections import namedtuple
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_export import fixture_window, install_fixture_as_log
from webtest import client, make_ctx

from mtrtk.jobs import JobRunner
from mtrtk.ppk import pipeline
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
NAV = b"     3.04           N: GNSS NAV DATA    M: Mixed            RINEX VERSION / TYPE\n"
BOUNDARY = "mtrtk0boundary"
FORM_TYPE = {"content-type": f"multipart/form-data; boundary={BOUNDARY}"}
WINDOW = {"kind": "window", "start": "2026-09-18T10:00:00Z", "end": "2026-09-18T11:00:00Z"}
_Usage = namedtuple("_Usage", "total used free")


def _form(parts: list[tuple[str, str | None, bytes]], *, close: bool = True) -> bytes:
    """A multipart body by hand: `(field, filename or None, data)` per part."""
    out = b""
    for name, filename, data in parts:
        disp = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        out += f"--{BOUNDARY}\r\nContent-Disposition: {disp}\r\n\r\n".encode() + data + b"\r\n"
    return out + (f"--{BOUNDARY}--\r\n".encode() if close else b"")


def _closed_port() -> int:
    """A loopback port nothing listens on: bound by this test, then released."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    # MIN_FREE_GB 0: the upload tests must not depend on the host's own free space.
    c = await make_ctx(
        tmp_path,
        role="rover",
        ntrip_url="ntrip://rover:pw@100.100.50.10:2101/MTRK",
        min_free_gb=0,
    )
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
        # Zero baseline: every epoch fixed or float (Q 1 or 2), none single/DGPS. RTKLIB never
        # attempts ambiguity resolution on identical observations (every double-difference
        # ambiguity is exactly 0, which it treats as not initialised), so this does NOT test the
        # fix rate (docs/ppk.md, "Reading the result"); the spec's >= 95 % fixed milestone is open.
        assert summary["single_pct"] == 0.0
        assert abs(summary["fixed_pct"] + summary["float_pct"] - 100.0) < 0.2
        names = [f["name"] for f in (await c.get(f"/api/jobs/{job['id']}/files")).json()]
        rows = list(
            csv.DictReader(
                io.StringIO((await c.get(f"/api/jobs/{job['id']}/files/track.csv")).text)
            )
        )
        assert len(rows) == summary["epochs"]
        assert {r["q"] for r in rows} <= {"1", "2"}
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
        # A file option points rnx2rtkp at a host file; the API takes none (the CLI may).
        for key in ("file-satantfile", "file-staposfile", "file-tempdir"):
            hostfile = await c.post(
                "/api/ppk",
                json={
                    "rover": {
                        "kind": "window",
                        "start": "2026-09-18T10:00:00Z",
                        "end": "2026-09-18T11:00:00Z",
                    },
                    "base": {"kind": "local"},
                    "conf_overrides": {key: "/etc/shadow"},
                },
            )
            assert hostfile.status_code == 422 and key in str(hostfile.json()["detail"])


async def test_remote_base_password_is_never_stored(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    secret = "hunter2-secret"
    start, end = fixture_window()
    install_fixture_as_log(tmp_path, start)
    async with client(create_app(ctx)) as c:
        rover = {"kind": "window", "start": start.isoformat(), "end": end.isoformat()}
        # Refusals that come from the base itself, the password in hand: none may echo it.
        for base in (
            {"kind": "remote", "password": secret},  # no url
            {"kind": "remote", "url": "ftp://base", "password": secret},  # not http(s)
        ):
            bad = await c.post("/api/ppk", json={"rover": rover, "base": base})
            assert bad.status_code == 422, bad.text
            assert bad.json()["detail"][0]["loc"][:2] == ["body", "base"], bad.json()
            assert secret not in bad.text
        both = await c.post(
            "/api/ppk",
            json={
                "rover": rover,
                "base": {"kind": "remote", "url": "http://b:8080", "password": secret},
                "base_site": "roof",
                "base_xyz": XYZ,
            },
        )
        assert both.status_code == 422 and secret not in both.text
        dead = f"http://127.0.0.1:{_closed_port()}"
        r = await c.post(
            "/api/ppk",
            json={
                "rover": rover,
                "base": {"kind": "remote", "url": dead, "password": secret},
                "base_xyz": XYZ,
            },
        )
        assert r.status_code == 200, r.text
        assert secret not in r.text
        job = await _wait(c, r.json()["id"])
        assert secret not in str(job)
        assert job["status"] == "failed" and job["params"]["base"]["url"] == dead
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
        obs_id = (await _upload(c, "base.obs", RINEX_OBS)).json()["upload_id"]
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
                "base": {"kind": "upload", "upload_id": obs_id, "nav_upload_id": ubx_id},
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


async def test_nav_file_with_a_raw_base_is_refused(ctx) -> None:  # type: ignore[no-untyped-def]
    """A UBX base carries its own ephemerides: a nav upload next to it is refused at once,
    not by a failed job."""
    async with client(create_app(ctx)) as c:
        nav_id = (await _upload(c, "base.nav", NAV)).json()["upload_id"]
        ubx_id = (await _upload(c, "base.ubx", UBX_HEAD)).json()["upload_id"]
        r = await c.post(
            "/api/ppk",
            json={
                "rover": {"kind": "upload", "upload_id": ubx_id},
                "base": {"kind": "upload", "upload_id": ubx_id, "nav_upload_id": nav_id},
                "base_xyz": XYZ,
            },
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"][0]["loc"] == ["body", "base", "nav_upload_id"]
        assert "ephemerides" in r.json()["detail"][0]["msg"]
        assert (await c.get("/api/jobs")).json() == []


async def test_window_longer_than_the_cap_is_refused_before_queueing(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    start, _ = fixture_window()
    install_fixture_as_log(tmp_path, start)
    end = start + pipeline.MAX_WINDOW + timedelta(seconds=1)
    async with client(create_app(ctx)) as c:
        r = await c.post(
            "/api/ppk",
            json={
                "rover": {"kind": "window", "start": start.isoformat(), "end": end.isoformat()},
                "base": {"kind": "local"},
                "base_xyz": XYZ,
            },
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"][0]["loc"] == ["body", "rover", "end"]
        assert "7 days" in r.json()["detail"][0]["msg"]
        assert (await c.get("/api/jobs")).json() == []


@pytest.fixture
def queued(monkeypatch) -> list[pipeline.PpkRequest]:  # type: ignore[no-untyped-def]
    """Every PpkRequest the API queues, with a job that does nothing (no RTKLIB needed)."""
    seen: list[pipeline.PpkRequest] = []

    def fake_job(req: pipeline.PpkRequest, pctx: pipeline.PpkContext) -> Any:
        seen.append(req)

        async def run(progress: Any) -> dict[str, Any]:
            return {}

        return run

    monkeypatch.setattr(ppk_api, "make_ppk_job", fake_job)
    return seen


async def test_uploaded_rinex_base_and_nav_reach_the_pipeline(ctx, queued) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        rover = (await _upload(c, "rover.ubx", UBX_HEAD, kind="rover")).json()
        obs = (await _upload(c, "base.obs", RINEX_OBS)).json()
        nav = (await _upload(c, "base.nav", NAV)).json()
        r = await c.post(
            "/api/ppk",
            json={
                "rover": {"kind": "upload", "upload_id": rover["upload_id"]},
                "base": {
                    "kind": "upload",
                    "upload_id": obs["upload_id"],
                    "nav_upload_id": nav["upload_id"],
                },
                "base_xyz": XYZ,
            },
        )
        assert r.status_code == 200, r.text
    (req,) = queued
    up = ctx.settings.data_dir / "uploads"
    assert req.base.path_obs == up / obs["upload_id"] / "base.obs"
    assert req.base.path_nav == up / nav["upload_id"] / "base.nav"
    assert req.base.path_ubx is None
    assert req.rover.path == up / rover["upload_id"] / "rover.ubx"


async def test_no_job_runner_is_a_409(tmp_path: Path) -> None:
    c = await make_ctx(tmp_path)
    try:
        async with client(create_app(c)) as http:
            r = await http.post("/api/ppk", json={"rover": WINDOW, "base": {"kind": "local"}})
        assert r.status_code == 409 and r.json()["detail"] == ppk_api.NO_RUNNER
    finally:
        await c.db.close()


async def test_a_window_covered_only_by_another_stations_logs_is_a_404(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    start, end = fixture_window()
    install_fixture_as_log(tmp_path, start, station="OTHR")
    async with client(create_app(ctx)) as c:
        r = await c.post(
            "/api/ppk",
            json={
                "rover": {"kind": "window", "start": start.isoformat(), "end": end.isoformat()},
                "base": {"kind": "local"},
                "base_xyz": XYZ,
            },
        )
    assert r.status_code == 404 and "no raw logs for station MTRK" in r.json()["detail"]


# ------------------------------------------------------------------- the streaming parser


async def _raw_upload(c: Any, body: Any, headers: dict[str, str] | None = None) -> Any:
    return await c.post("/api/ppk/upload", content=body, headers=headers or FORM_TYPE)


def _left(tmp_path: Path) -> list[str]:
    up = tmp_path / "uploads"
    return sorted(p.name for p in up.iterdir()) if up.is_dir() else []


@pytest.mark.parametrize(
    ("parts", "status", "words"),
    [
        (
            [("kind", None, b"base"), ("file", "a.ubx", UBX_HEAD), ("file", "b.ubx", UBX_HEAD)],
            422,
            "exactly one file",
        ),
        ([("kind", None, b"b" * 2048), ("file", "a.ubx", UBX_HEAD)], 422, "too long"),
        (
            [
                ("kind", None, b"base"),
                (
                    "file",
                    "a.crx",
                    b"1.0                 COMPACT RINEX FORMAT".ljust(60)
                    + b"CRINEX VERS   / TYPE\n",
                ),
            ],
            422,
            "Hatanaka",
        ),
    ],
    ids=["two-files", "long-field", "crinex"],
)
async def test_upload_refusals_leave_nothing(ctx, tmp_path: Path, parts, status, words) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await _raw_upload(c, _form(parts))
    assert r.status_code == status, r.text
    assert words in r.json()["detail"]
    assert _left(tmp_path) == []


async def test_upload_cut_short_is_refused(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A body that stops before its closing boundary is not a complete file."""
    body = _form([("kind", None, b"base"), ("file", "a.ubx", UBX_HEAD + bytes(100))], close=False)
    async with client(create_app(ctx)) as c:
        r = await _raw_upload(c, body)
    assert r.status_code == 422, r.text
    assert "closing boundary" in r.json()["detail"]
    assert _left(tmp_path) == []


async def test_upload_names(ctx, tmp_path: Path, queued) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        dotted = await _upload(c, ".x.ubx", UBX_HEAD, kind="rover")
        assert dotted.status_code == 200 and dotted.json()["name"] == "x.ubx"
        # A dot-name would be hidden (taken for a partial file): it must resolve at submit.
        r = await c.post(
            "/api/ppk",
            json={
                "rover": {"kind": "upload", "upload_id": dotted.json()["upload_id"]},
                "base": {"kind": "local"},
                "base_xyz": XYZ,
            },
        )
        assert r.status_code == 200, r.text
        assert queued[0].rover.path is not None and queued[0].rover.path.name == "x.ubx"
        long = await _upload(c, "a" * 200 + ".ubx", UBX_HEAD)
        assert long.status_code == 200
        name = long.json()["name"]
        assert len(name) == ppk_api.NAME_MAX and name.endswith(".ubx")
        assert (tmp_path / "uploads" / long.json()["upload_id"] / name).exists()


async def test_upload_dropped_mid_body_leaves_nothing(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    class Hangup(Exception):
        pass

    async def body() -> Any:
        yield _form(
            [("kind", None, b"base"), ("file", "a.ubx", UBX_HEAD + bytes(64 * 1024))], close=False
        )
        raise Hangup

    async with client(create_app(ctx)) as c:
        with pytest.raises(Hangup):
            await _raw_upload(c, body())
    assert _left(tmp_path) == []


async def test_upload_refused_when_the_card_would_drop_below_min_free(
    ctx, tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    ctx.settings.min_free_gb = 5.0
    monkeypatch.setattr(ppk_api.shutil, "disk_usage", lambda _p: _Usage(64e9, 63e9, 1e9))
    async with client(create_app(ctx)) as c:
        r = await _upload(c, "a.ubx", UBX_HEAD)
    assert r.status_code == 409, r.text
    assert "MIN_FREE_GB" in r.json()["detail"]
    assert _left(tmp_path) == []


def test_an_upload_fits_on_a_card_that_retention_keeps_at_min_free(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Retention keeps a full card just above MIN_FREE_GB: refusing every upload that does not
    fit in that slack would refuse them all for good. Like an export, an upload may use the
    margin (retention then prunes the oldest unkept hours), never its last half."""
    monkeypatch.setattr(ppk_api.shutil, "disk_usage", lambda _p: _Usage(64e9, 59e9, 5e9 + 10e6))
    folder = ppk_api._make_folder(tmp_path / "uploads", 50_000_000, 5.0)
    ppk_api._release(folder)
    monkeypatch.setattr(ppk_api.shutil, "disk_usage", lambda _p: _Usage(64e9, 61e9, 2.5e9 + 10e6))
    with pytest.raises(ppk_api._FormRefused) as refused:
        ppk_api._make_folder(tmp_path / "uploads", 50_000_000, 5.0)
    assert refused.value.status == 409 and "MIN_FREE_GB" in refused.value.detail


async def test_uploads_in_flight_count_against_the_free_space(
    ctx, tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Two large uploads at once must not both pass a check made against the same free space."""
    ctx.settings.min_free_gb = 5.0
    monkeypatch.setattr(ppk_api.shutil, "disk_usage", lambda _p: _Usage(64e9, 54e9, 10e9))
    monkeypatch.setitem(ppk_api._IN_FLIGHT, "other0upload", int(8e9))  # another upload, 8 GB to go
    async with client(create_app(ctx)) as c:
        r = await _upload(c, "a.ubx", UBX_HEAD)
        assert r.status_code == 409, r.text
        monkeypatch.delitem(ppk_api._IN_FLIGHT, "other0upload")
        ok = await _upload(c, "a.ubx", UBX_HEAD)
        assert ok.status_code == 200, ok.text
    assert ppk_api._IN_FLIGHT == {}  # released once the upload is done


async def test_upload_without_a_length_is_checked_as_it_grows(
    ctx, tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    ctx.settings.min_free_gb = 5.0
    free = [10e9]
    monkeypatch.setattr(
        ppk_api.shutil, "disk_usage", lambda _p: _Usage(64e9, 64e9 - free[0], free[0])
    )
    monkeypatch.setattr(ppk_api, "FREE_CHECK_BYTES", 16 * 1024)

    async def body() -> Any:  # chunked: no Content-Length, so nothing to check up front
        yield _form(
            [("kind", None, b"base"), ("file", "a.ubx", UBX_HEAD + bytes(20 * 1024))], close=False
        )
        free[0] = 2e9  # the card filled meanwhile, past the half of MIN_FREE_GB kept free
        yield bytes(20 * 1024) + f"\r\n--{BOUNDARY}--\r\n".encode()

    async with client(create_app(ctx)) as c:
        r = await _raw_upload(c, body())
    assert r.status_code == 409, r.text
    assert "MIN_FREE_GB" in r.json()["detail"]
    assert _left(tmp_path) == []
    assert ppk_api._IN_FLIGHT == {}


def test_prune_removes_old_uploads_and_dropped_halves(tmp_path: Path) -> None:
    now = time.time()
    up = tmp_path / "uploads"

    def folder(name: str, age_s: float, files: list[str]) -> None:
        d = up / name
        d.mkdir(parents=True)
        for f in files:
            (d / f).write_bytes(b"x")
        os.utime(d, (now - age_s, now - age_s))

    folder("fresh0000000", 60, ["a.ubx"])
    folder("doneday20000", 2 * 86400, ["a.ubx"])  # complete, 2 days: kept
    folder("doneold00000", ppk_api.UPLOAD_TTL_S + 60, ["a.ubx"])  # past the TTL: removed
    folder("partnew00000", 3600, [".part-1234"])  # still arriving, 1 h: kept
    folder("partold00000", ppk_api.STALE_PART_S + 60, [".part-1234"])  # dropped: removed
    (up / "stray.txt").write_bytes(b"x")
    os.utime(up / "stray.txt", (now - 30 * 86400, now - 30 * 86400))  # a file, not an upload
    ppk_api._prune(up, now)
    assert sorted(p.name for p in up.iterdir()) == [
        "doneday20000",
        "fresh0000000",
        "partnew00000",
        "stray.txt",
    ]
