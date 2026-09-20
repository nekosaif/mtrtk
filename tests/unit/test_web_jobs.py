"""Jobs API: listing, one job, result files, download, delete, and life without a runner."""

import asyncio
from pathlib import Path

import pytest
from webtest import client, make_ctx

from mtrtk.jobs import JobContext, JobRunner
from mtrtk.web.app import create_app


@pytest.fixture
async def ctx(tmp_path: Path):
    c = await make_ctx(tmp_path)
    c.jobs = JobRunner(c.db, c.bus, tmp_path / "jobs")
    try:
        yield c
    finally:
        await c.jobs.shutdown()
        await c.db.close()


async def settle(runner: JobRunner, job_id: str, want: str = "done") -> None:
    for _ in range(200):
        job = await runner.get(job_id)
        if job is not None and job.status == want:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"job {job_id} never reached {want!r}")


async def test_jobs_endpoints(ctx) -> None:
    async def work(jctx: JobContext) -> dict:
        (jctx.dir / "result.csv").write_text("a,b\n1,2\n")
        return {"rows": 1}

    job = await ctx.jobs.submit("export", {"preset": "csrs"}, work)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if (await ctx.jobs.get(job.id)).status == "done":
            break
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/jobs")).json()
        assert body[0]["id"] == job.id and body[0]["status"] == "done"
        assert body[0]["params"] == {"preset": "csrs"}
        assert (await c.get("/api/jobs", params={"kind": "ppk"})).json() == []
        assert (await c.get(f"/api/jobs/{job.id}")).json()["result"] == {"rows": 1}
        assert (await c.get("/api/jobs/nope")).status_code == 404
        files = (await c.get(f"/api/jobs/{job.id}/files")).json()
        assert files == [{"name": "result.csv", "bytes": 8}]
        r = await c.get(f"/api/jobs/{job.id}/files/result.csv")
        assert r.status_code == 200 and r.text == "a,b\n1,2\n"
        assert (await c.get(f"/api/jobs/{job.id}/files/..%2F..%2Fetc%2Fpasswd")).status_code in (
            404,
            422,
        )
        assert (await c.delete(f"/api/jobs/{job.id}")).json() == {"ok": True}
        assert (await c.get(f"/api/jobs/{job.id}")).status_code == 404


async def test_jobs_409_without_runner(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path)
    try:
        async with client(create_app(ctx)) as c:
            assert (await c.get("/api/jobs")).status_code == 409
    finally:
        await ctx.db.close()


# --------------------------------------------------------------------------- beyond the brief
async def test_every_route_says_409_when_this_daemon_runs_no_jobs(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path)
    try:
        async with client(create_app(ctx)) as c:
            for path in (
                "/api/jobs",
                "/api/jobs/abc",
                "/api/jobs/abc/files",
                "/api/jobs/abc/files/x",
            ):
                assert (await c.get(path)).status_code == 409, path
            assert (await c.delete("/api/jobs/abc")).status_code == 409
    finally:
        await ctx.db.close()


async def test_the_download_route_refuses_a_name_that_is_not_one_file(ctx) -> None:
    async def work(jctx: JobContext) -> dict:
        (jctx.dir / "ok.txt").write_text("fine")
        (jctx.dir / "sub").mkdir()
        (jctx.dir / "escape").symlink_to(jctx.dir.parent.parent)
        return {}

    job = await ctx.jobs.submit("export", {}, work)
    await settle(ctx.jobs, job.id)
    async with client(create_app(ctx)) as c:
        # `%2e%2e` is the one that reaches the route as `..`: httpx resolves a literal `..`
        # or `.` segment away on the client side, so those never leave the test.
        for name in ("%2e%2e", "%252e%252e", "sub", "escape", "missing.txt"):
            got = await c.get(f"/api/jobs/{job.id}/files/{name}")
            assert got.status_code in (404, 422), (name, got.status_code)
        # A directory and a symlink out of the job directory are not results either.
        assert (await c.get(f"/api/jobs/{job.id}/files")).json() == [{"name": "ok.txt", "bytes": 4}]
        assert (await c.get(f"/api/jobs/{job.id}/files/ok.txt")).text == "fine"


async def test_files_of_a_job_that_never_wrote_anything(ctx) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def gated(jctx: JobContext) -> dict:
        started.set()
        await release.wait()
        return {}

    job = await ctx.jobs.submit("export", {}, gated)
    await asyncio.wait_for(started.wait(), 2.0)
    async with client(create_app(ctx)) as c:
        assert (await c.get(f"/api/jobs/{job.id}/files")).json() == []
        assert (await c.get(f"/api/jobs/{job.id}")).json()["status"] == "running"
        # A running job is not deleted out from under itself.
        refused = await c.delete(f"/api/jobs/{job.id}")
        assert refused.status_code == 409 and "running" in refused.json()["detail"]
        assert (await c.get("/api/jobs/nope/files")).status_code == 404
    release.set()
    await settle(ctx.jobs, job.id)


async def test_delete_of_an_unknown_job_is_404_and_limit_is_validated(ctx) -> None:
    async with client(create_app(ctx)) as c:
        assert (await c.delete("/api/jobs/nope")).status_code == 404
        assert (await c.get("/api/jobs", params={"limit": 0})).status_code == 422
        assert (await c.get("/api/jobs", params={"limit": 501})).status_code == 422
        assert (await c.get("/api/jobs", params={"limit": 1})).json() == []


async def test_openapi_documents_the_failure_codes(ctx) -> None:
    async with client(create_app(ctx)) as c:
        paths = (await c.get("/api/openapi.json")).json()["paths"]
    assert {"404", "409"} <= set(paths["/api/jobs/{job_id}"]["get"]["responses"])
    assert {"404", "409"} <= set(paths["/api/jobs/{job_id}"]["delete"]["responses"])
    assert {"404", "409"} <= set(paths["/api/jobs/{job_id}/files/{name}"]["get"]["responses"])
    assert {"409", "422"} <= set(paths["/api/jobs"]["get"]["responses"])


async def test_the_job_routes_are_gated_like_every_other_api_route(tmp_path: Path) -> None:
    c = await make_ctx(tmp_path, web_password="hunter2", web_bind="lan")
    c.jobs = JobRunner(c.db, c.bus, tmp_path / "jobs")
    try:
        async with client(create_app(c)) as http:
            for path in ("/api/jobs", "/api/jobs/abc", "/api/jobs/abc/files"):
                assert (await http.get(path)).status_code == 401
            assert (await http.delete("/api/jobs/abc")).status_code == 401
    finally:
        await c.jobs.shutdown()
        await c.db.close()


async def test_deleting_an_id_no_job_could_have_is_a_404(ctx) -> None:  # type: ignore[no-untyped-def]
    """`job_dir` validates the id because that path reaches `rmtree`; a refusal is a 404.

    The row lookup normally catches these first, so the guard is only reachable with a row that
    claims an impossible id - which is what a hand-edited database, or a later bug, looks like.
    """
    from datetime import UTC, datetime

    from mtrtk.jobs import Job

    async def pretend(job_id: str) -> Job:
        return Job(id=job_id, kind="export", status="queued", created_utc=datetime.now(UTC))

    ctx.jobs.get = pretend
    async with client(create_app(ctx)) as c:
        # A backslash: one URL segment, but not one path component.
        r = await c.delete("/api/jobs/a%5Cb")
    assert r.status_code == 404 and r.json()["detail"] == "no job with that id"
