"""Job runner: lifecycle, progress, result files, one at a time, delete, restore, shutdown."""

import asyncio
from pathlib import Path

import pytest

from mtrtk.core.bus import Bus
from mtrtk.jobs import Job, JobBusy, JobContext, JobRunner
from mtrtk.store.db import Database


@pytest.fixture
async def runner(tmp_path: Path):
    db = Database(tmp_path / "m.db")
    await db.open()
    bus = Bus()
    r = JobRunner(db, bus, tmp_path / "jobs")
    try:
        yield r, bus
    finally:
        await r.shutdown()
        await db.close()


async def settle(r: JobRunner, job_id: str, want: str = "done") -> Job:
    """Wait, briefly, for a job to reach `want`. Bounded so a wedged job fails the test."""
    for _ in range(200):
        job = await r.get(job_id)
        if job is not None and job.status == want:
            return job
        await asyncio.sleep(0.005)
    raise AssertionError(f"job {job_id} never reached {want!r}")


async def test_job_lifecycle_and_result_files(runner) -> None:
    r, bus = runner
    updates = bus.subscribe("jobs.update")

    async def work(ctx: JobContext) -> dict:
        await ctx.progress(0.5, "halfway")
        (ctx.dir / "out.txt").write_text("hello")
        return {"files": ["out.txt"], "n": 1}

    job = await r.submit("demo", {"a": 1}, work)
    assert job.status == "queued" and job.params == {"a": 1} and len(job.id) == 12
    for _ in range(50):
        await asyncio.sleep(0.01)
        current = await r.get(job.id)
        if current and current.status == "done":
            break
    assert current.status == "done" and current.progress == 1.0
    assert current.result == {"files": ["out.txt"], "n": 1}
    assert r.result_path(job.id, "out.txt").read_text() == "hello"
    drained = [updates.queue.get_nowait() for _ in range(updates.queue.qsize())]
    published = [item for _, item in drained]
    statuses = [item.status for item in published]
    assert statuses[0] == "queued" and "running" in statuses and statuses[-1] == "done"
    assert any(item.message == "halfway" and item.progress == 0.5 for item in published)
    listed = await r.list(kind="demo")
    assert [j.id for j in listed] == [job.id]


async def test_failed_job_records_error(runner) -> None:
    r, _ = runner

    async def boom(ctx: JobContext) -> dict:
        raise RuntimeError("convbin exited 1")

    job = await r.submit("export", {}, boom)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if (await r.get(job.id)).status == "failed":
            break
    failed = await r.get(job.id)
    assert failed.status == "failed" and failed.error == "RuntimeError: convbin exited 1"


async def test_jobs_run_one_at_a_time(runner) -> None:
    r, _ = runner
    running: list[int] = []
    peak = 0

    async def slow(ctx: JobContext) -> dict:
        nonlocal peak
        running.append(1)
        peak = max(peak, len(running))
        await asyncio.sleep(0.02)
        running.pop()
        return {}

    jobs = [await r.submit("slow", {}, slow) for _ in range(3)]
    for _ in range(100):
        await asyncio.sleep(0.01)
        # A genexp with an `await` in it is an async generator, which `all()` cannot consume.
        if all(s == "done" for s in [(await r.get(j.id)).status for j in jobs]):
            break
    assert peak == 1


async def test_delete_removes_dir_and_row(runner) -> None:
    r, _ = runner

    async def work(ctx: JobContext) -> dict:
        (ctx.dir / "x").write_text("x")
        return {}

    job = await r.submit("demo", {}, work)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if (await r.get(job.id)).status == "done":
            break
    assert (r.root / job.id).exists()
    await r.delete(job.id)
    assert not (r.root / job.id).exists() and await r.get(job.id) is None
    with pytest.raises(ValueError):
        r.result_path(job.id, "../../etc/passwd")


async def test_restore_marks_interrupted(tmp_path: Path) -> None:
    db = Database(tmp_path / "m.db")
    await db.open()
    await db.execute(
        "INSERT INTO jobs (id, kind, status, created_utc, progress, params) "
        "VALUES ('abc', 'export', 'running', '2026-09-18T00:00:00+00:00', 0.3, '{}')"
    )
    await db.commit()
    r = JobRunner(db, Bus(), tmp_path / "jobs")
    await r.restore()
    job = await r.get("abc")
    assert job.status == "failed" and job.error == "interrupted by restart"
    await db.close()


# --------------------------------------------------------------------------- beyond the brief
async def test_restore_also_clears_queued_rows_and_says_so_on_the_bus(tmp_path: Path) -> None:
    """A row left `queued` has no coroutine behind it either: nothing requeues by itself."""
    db = Database(tmp_path / "m.db")
    await db.open()
    bus = Bus()
    updates = bus.subscribe("jobs.update")
    for job_id, status in (("q1", "queued"), ("r1", "running"), ("d1", "done")):
        await db.execute(
            "INSERT INTO jobs (id, kind, status, created_utc, progress, params) "
            "VALUES (?, 'export', ?, '2026-09-18T00:00:00+00:00', 0, '{}')",
            (job_id, status),
        )
    await db.commit()
    r = JobRunner(db, bus, tmp_path / "jobs")
    await r.restore()
    # All three rows share a `created_utc`, so the tie-break decides the order, not the status.
    assert dict((j.id, j.status) for j in await r.list()) == {
        "q1": "failed",
        "r1": "failed",
        "d1": "done",
    }
    published = {item.id for _, item in [updates.queue.get_nowait() for _ in range(2)]}
    assert published == {"q1", "r1"} and updates.queue.empty()
    await db.close()


async def test_shutdown_cancels_the_running_job_and_records_why(tmp_path: Path) -> None:
    db = Database(tmp_path / "m.db")
    await db.open()
    r = JobRunner(db, Bus(), tmp_path / "jobs")
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def hang(ctx: JobContext) -> dict:
        started.set()
        try:
            await asyncio.Event().wait()  # only the cancellation ever ends this
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {}

    job = await r.submit("slow", {}, hang)
    await asyncio.wait_for(started.wait(), 2.0)
    await r.shutdown()
    assert cancelled.is_set()
    stopped = await r.get(job.id)
    assert stopped.status == "failed" and stopped.error == "shutdown"
    await r.shutdown()  # idempotent: the daemon may stop twice on its way down
    await db.close()


async def test_shutdown_is_bounded_when_a_job_will_not_stop(tmp_path: Path, caplog) -> None:
    """A job that swallows its cancellation must not hold the daemon's shutdown open."""
    db = Database(tmp_path / "m.db")
    await db.open()
    r = JobRunner(db, Bus(), tmp_path / "jobs")
    started, release = asyncio.Event(), asyncio.Event()

    async def stubborn(ctx: JobContext) -> dict:
        started.set()
        while not release.is_set():
            try:
                await asyncio.wait_for(release.wait(), 10)
            except (TimeoutError, asyncio.CancelledError):
                continue  # deliberately ignores the cancellation
        return {}

    job = await r.submit("slow", {}, stubborn)
    await asyncio.wait_for(started.wait(), 2.0)
    await asyncio.wait_for(r.shutdown(grace_s=0.05), 2.0)
    assert "did not stop" in caplog.text
    release.set()  # let it finish so the loop closes with nothing still pending
    assert (await settle(r, job.id)).status == "done"
    await db.close()


async def test_delete_refuses_a_running_job(tmp_path: Path) -> None:
    db = Database(tmp_path / "m.db")
    await db.open()
    r = JobRunner(db, Bus(), tmp_path / "jobs")
    started, release = asyncio.Event(), asyncio.Event()

    async def gated(ctx: JobContext) -> dict:
        started.set()
        await release.wait()
        return {}

    job = await r.submit("export", {}, gated)
    await asyncio.wait_for(started.wait(), 2.0)
    with pytest.raises(JobBusy):
        await r.delete(job.id)
    release.set()
    await settle(r, job.id)
    await r.delete(job.id)
    assert await r.get(job.id) is None
    await r.shutdown()
    await db.close()


async def test_deleting_a_queued_job_stops_it_before_it_ever_runs(tmp_path: Path) -> None:
    db = Database(tmp_path / "m.db")
    await db.open()
    r = JobRunner(db, Bus(), tmp_path / "jobs")
    started, release, ran = asyncio.Event(), asyncio.Event(), []

    async def gated(ctx: JobContext) -> dict:
        started.set()
        await release.wait()
        return {}

    async def second(ctx: JobContext) -> dict:
        ran.append(1)
        return {}

    first = await r.submit("export", {}, gated)
    queued = await r.submit("export", {}, second)
    await asyncio.wait_for(started.wait(), 2.0)
    assert (await r.get(queued.id)).status == "queued"
    await r.delete(queued.id)
    assert await r.get(queued.id) is None
    release.set()
    await settle(r, first.id)
    assert ran == []  # the semaphore freed up, but the job was gone by then
    await r.shutdown()
    await db.close()


async def test_a_result_that_is_not_json_fails_the_job_rather_than_the_task(runner) -> None:
    r, _ = runner

    async def work(ctx: JobContext) -> dict:
        return {"handle": object()}

    job = await r.submit("export", {}, work)
    failed = await settle(r, job.id, "failed")
    assert failed.error is not None and failed.error.startswith("TypeError")


async def test_a_job_that_returns_the_wrong_shape_fails(runner) -> None:
    r, _ = runner

    async def work(ctx: JobContext) -> dict:
        return ["not", "a", "dict"]  # type: ignore[return-value]

    job = await r.submit("export", {}, work)
    failed = await settle(r, job.id, "failed")
    assert failed.error == "TypeError: a job must return a dict, not list"


async def test_result_path_refuses_anything_but_one_file_in_the_job_directory(runner) -> None:
    r, _ = runner
    for name in ("../x", "..", ".", "", "a/b", "/etc/passwd", "sub/../../x", "x\x00y"):
        with pytest.raises(ValueError):
            r.result_path("abcdef123456", name)
    for job_id in ("..", "../other", "/abs", ""):
        with pytest.raises(ValueError):
            r.result_path(job_id, "out.txt")
        with pytest.raises(ValueError):
            r.job_dir(job_id)
    assert r.result_path("abcdef123456", "out.txt") == (r.root / "abcdef123456" / "out.txt")


async def test_result_path_refuses_a_symlink_out_of_the_job_directory(runner) -> None:
    r, _ = runner

    async def work(ctx: JobContext) -> dict:
        (ctx.dir / "escape").symlink_to(ctx.dir.parent.parent / "secret.txt")
        return {}

    (r.root.parent / "secret.txt").write_text("private")
    job = await r.submit("demo", {}, work)
    await settle(r, job.id)
    with pytest.raises(ValueError):
        r.result_path(job.id, "escape")


async def test_progress_is_clamped_and_listing_is_newest_first(runner) -> None:
    r, _ = runner

    async def work(ctx: JobContext) -> dict:
        await ctx.progress(7.5, "over")
        await ctx.progress(-1.0)
        return {}

    first = await r.submit("a", {}, work)
    await settle(r, first.id)
    second = await r.submit("b", {}, work)
    await settle(r, second.id)
    assert [j.kind for j in await r.list()] == ["b", "a"]
    assert [j.kind for j in await r.list(limit=1)] == ["b"]
    assert [j.id for j in await r.list(kind="a")] == [first.id]
    assert (await r.get(first.id)).progress == 1.0  # the done update is the last word


async def test_submit_is_refused_once_the_runner_has_shut_down(runner) -> None:
    r, _ = runner

    async def work(ctx: JobContext) -> dict:
        return {}

    await r.shutdown()
    with pytest.raises(RuntimeError, match="shut down"):
        await r.submit("demo", {}, work)
